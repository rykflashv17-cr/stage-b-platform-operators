"""Small image records instead of GitLab image archives.

Temporary tags are transport, never proof of acceptance. Consumers use exact
manifest digests. Docker's local Id may be a config or a manifest depending on
the storage driver; validate both against the actual registry manifest.
"""
import hashlib
import json
import re

from ci.registry.gitlab import identifier

PREFIX = 'harbor.striding.internal/data/data-operators/'
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
KEY = re.compile(r'([a-z][a-z0-9]*(?:_[a-z0-9]+)*)/v([0-9]+\.[0-9]+\.[0-9]+)')
MANIFESTS = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
INDEXES = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}


def repository_for(key):
    match = KEY.fullmatch(key)
    if not match:
        raise ValueError('invalid exact operator version')
    return PREFIX + match[1], match[2]


def temporary_target(key, execution, env):
    branch = execution['ref']
    if (execution['source'] == 'merge_request_event' or not execution.get('protected') or
            env.get('CI_PIPELINE_SOURCE') != 'parent_pipeline' or env.get('CI_COMMIT_REF_PROTECTED') != 'true' or
            env.get('CI_COMMIT_BRANCH') != branch or
            branch not in {'develop','release','main'}):
        raise ValueError('temporary image write requires matching protected execution')
    repository, version = repository_for(key)
    return repository, f"{version}-ci.{identifier(execution['parent_pipeline_id'])}.{identifier(env['CI_JOB_ID'])}"


def checked_json(raw, digest):
    if not DIGEST.fullmatch(digest) or 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('registry object digest mismatch')
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('registry metadata too large')
    return json.loads(raw)


def descriptor(value):
    if (not isinstance(value,dict) or not DIGEST.fullmatch(value.get('digest','')) or
            type(value.get('size')) is not int or value['size'] < 0 or not isinstance(value.get('mediaType'),str)):
        raise ValueError('invalid image descriptor')
    return value['digest']


def inspect_manifest(registry, repository, digest):
    raw = registry.fetch(repository, digest)
    manifest = checked_json(raw,digest)
    root_digest, platform_digest = digest, digest
    if manifest.get('mediaType') in INDEXES:
        candidates = []
        for item in manifest.get('manifests',[]):
            descriptor(item)
            if item.get('platform') == {'os':'linux','architecture':'amd64'}:
                candidates.append(item)
        if len(candidates) != 1:
            raise ValueError('image index must contain one linux/amd64 manifest')
        platform_digest = candidates[0]['digest']
        raw = registry.fetch(repository, platform_digest)
        if len(raw) != candidates[0]['size']:
            raise ValueError('image descriptor size mismatch')
        manifest = checked_json(raw,platform_digest)
    if manifest.get('schemaVersion') != 2 or manifest.get('mediaType') not in MANIFESTS or not isinstance(manifest.get('layers'),list):
        raise ValueError('invalid image manifest')
    for item in manifest['layers']: descriptor(item)
    config_digest = descriptor(manifest.get('config'))
    raw = registry.fetch(repository,config_digest,blob=True)
    if len(raw) != manifest['config']['size']:
        raise ValueError('image config size mismatch')
    config = checked_json(raw,config_digest)
    if (config.get('os'),config.get('architecture')) != ('linux','amd64'):
        raise ValueError('image platform must be linux/amd64')
    return {'repository':repository,'digest':root_digest,'platform_digest':platform_digest,
            'config_digest':config_digest,'platform':'linux/amd64','reference':repository+'@'+root_digest}


def check_local_image(local, record):
    if ((local.get('Os'),local.get('Architecture')) != ('linux','amd64') or
            local.get('Id') not in {record['digest'],record['platform_digest'],record['config_digest']}):
        raise ValueError('local image differs from recorded registry image')


def promote(registry, repository, digest, tag):
    if not repository.startswith(PREFIX) or '/' in repository[len(PREFIX):] or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-(?:dev|rc)\.[1-9][0-9]*)?',tag):
        raise ValueError('invalid operator promotion target')
    inspect_manifest(registry, repository, digest)
    previous = registry.resolve(repository,tag)
    if previous is not None and previous != digest:
        raise ValueError('published tag digest conflict')
    if previous is None:
        registry.tag(repository,digest,tag)
    if registry.resolve(repository,tag) != digest:
        raise ValueError('published tag digest mismatch')
    return digest
