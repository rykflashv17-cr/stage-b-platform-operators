"""Publish immutable, tested Python wheels to the company's Nexus hosted index."""
import base64
import hashlib
import http.client
import netrc
import os
import ssl
import subprocess
from urllib.parse import quote

HOST = 'nexus.striding.internal'
REPOSITORY = '/repository/pypi-internal/'


def wheel_url(pin):
    return 'https://' + HOST + REPOSITORY + 'packages/' + '/'.join(
        quote(value, safe='') for value in (pin.name, pin.version, pin.filename))


def credentials():
    username, password = os.environ.get('NEXUS_PUBLISH_USERNAME'), os.environ.get('NEXUS_PUBLISH_PASSWORD')
    if username and password:
        return username, password
    if username or password:
        raise ValueError('incomplete Nexus publication credentials')
    auth = netrc.netrc(os.environ.get('NETRC')).authenticators(HOST)
    if not auth:
        raise ValueError('Nexus publication credentials are missing')
    return auth[0], auth[2]


def remote_digest(pin, *, credentials_provider=credentials):
    username, password = credentials_provider()
    auth = base64.b64encode(f'{username}:{password}'.encode()).decode()
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(HOST, timeout=60, context=context)
    try:
        conn.request('GET', wheel_url(pin).removeprefix('https://' + HOST),
                     headers={'Authorization': 'Basic ' + auth})
        response = conn.getresponse()
        if response.status == 404:
            return None
        if response.status != 200:
            raise ValueError(f'Nexus wheel lookup returned HTTP {response.status}')
        return hashlib.file_digest(response, 'sha256').hexdigest()
    finally:
        conn.close()


def publish_once(expected, lookup, upload):
    current = lookup()
    if current is None:
        try:
            upload()
        except subprocess.CalledProcessError:
            if lookup() != expected:
                raise
            return
        current = lookup()
    if current != expected:
        raise ValueError('Nexus contains different bytes for this package version')


def publish_wheel(path, pin):
    from ci.build.dependency_wheels import verify_wheel
    verify_wheel(path, pin)
    username, password = credentials()
    env = {**os.environ, 'UV_PUBLISH_USERNAME': username, 'UV_PUBLISH_PASSWORD': password}
    publish_once(pin.digest, lambda: remote_digest(pin), lambda: subprocess.run(
        ['uv', 'publish', '--publish-url', 'https://' + HOST + REPOSITORY, str(path)],
        env=env, check=True))


def publish_packages(rt):
    from ci.build.dependency_wheels import WheelPin
    from ci.build.context import verify_project_wheel
    from ci.plan.projects import shared_projects, dependencies
    from ci.plan.packages import package_identity
    from ci.publish.package_records import PackageRecords,validate_record
    if (rt.plan['execution']['source']=='merge_request_event' or
            not rt.plan['execution']['protected'] or rt.plan['execution']['ref']!='develop'):
        raise ValueError('Nexus publication requires protected develop')
    inventory=rt.packages()
    projects={p.distribution:p for p in shared_projects(rt.root)}
    selected=set(rt.spec['packages']);store=PackageRecords(writable=True);records=[]
    ordered=list({p.distribution:p for name in sorted(selected)
                  for p in [*dependencies(rt.root,projects[name]),projects[name]]}.values())
    paths=[p.path.relative_to(rt.root).as_posix() for p in ordered]
    subprocess.run(['git','diff','--exit-code','HEAD','--',*paths],cwd=rt.root,check=True)
    if subprocess.check_output(['git','ls-files','--others','--exclude-standard','--',*paths],cwd=rt.root).strip():
        raise ValueError('package sources contain uncommitted files')
    # Validate every selected report before the first external write.
    for name in selected:
        tested=rt.consume('test-packages:'+name,'package-test',name)['payload']
        if tested['inputs']['packages']!=inventory:
            raise ValueError('package tests used an older build attempt')
        result=tested['result']
        if result['name']!=name or result['version']!=projects[name].version:
            raise ValueError('package test identity mismatch')
        if {r['mode'] for r in result['results']}!={'source','wheel'} or any(
                r['exit_code'] or r['failed'] or not r['passed'] for r in result['results']):
            raise ValueError('package isolation tests are incomplete')
        if any(inventory['payload']['wheels'].get(file)!=digest for file,digest in result['wheels'].items()):
            raise ValueError('tested package wheel digest mismatch')
    rt.assert_producers_current()
    for project in ordered:
        path=verify_project_wheel(rt.root,project)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if inventory['payload']['wheels'].get(project.wheel)!=digest:
            raise ValueError('package inventory differs from source wheel')
        pin=WheelPin(project.distribution,project.version,project.wheel,digest)
        record={**package_identity(rt.root,project),'source_commit':rt.plan['commit'],'sha256':digest,
                'url':wheel_url(pin),'build_producer':inventory['producer'],
                'parent_pipeline_id':rt.plan['execution']['parent_pipeline_id']}
        saved=store.read(pin.name,pin.version)
        if saved is not None:validate_record(saved,record)
        if project.distribution not in selected:
            if saved is None or remote_digest(pin)!=digest:
                raise ValueError('unpublished shared dependency; include it in the package plan')
            continue
        publish_wheel(path,pin)
        records.append(store.create(record))
    from ci.publish.package_releases import PackageReleases,release_notes
    releases=PackageReleases.environment()
    synced=[releases.sync(record,notes=release_notes(rt.root,record)) for record in records]
    rt.emit('package-release','selected',{'packages':records,'gitlab_releases':synced,'inputs':{'packages':inventory}})
