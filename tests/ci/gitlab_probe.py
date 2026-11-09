"""Opt-in native GitLab failure exercises; fixed bytes, no registry operations.

Use only in an unmerged probe branch whose root CI invokes ``plan`` here.
The production DAG generator/templates and Runtime validate real Job Tokens.
Every generated action is replaced by a local report fixture before rendering.
"""
import json
import os
from pathlib import Path
import sys
import time


def make_plan(execution, commit, scenario):
    from ci.plan.pipeline import assign_jobs
    if execution['source'] != 'merge_request_event':
        raise ValueError('fault exercises require an unprotected MR')
    if scenario not in {'failure', 'cancel', 'retry'}:
        raise ValueError('unknown probe scenario')
    key = 'example/v1.0.0'
    requirements = {'platform': 'linux/amd64', 'runner_tags': ['software'], 'environment': 'ci/tooling/uv.lock'}
    plan = assign_jobs({'format_version': 2, 'commit': commit,
        'execution': {**execution, 'source': 'push', 'ref': 'main', 'protected': True},
        'operators': {key: {'source': {'kind': 'build'}, 'publish_tags': ['1.0.0'],
                            'mirror_tags': ['1.0.0'], 'write_records': True}},
        'tests': {key+'/'+kind: {'kind': kind, 'subject': key, 'action': 'run',
                    'requirements': requirements} for kind in ('source','wheel','container')},
        'checks': {'wheels': [key], 'extra': False}, 'base_images': {},
        'publication': {'write_manifest': True, 'write_delivery_receipt': True}})
    plan['execution'] = execution
    plan['probe_scenario'] = scenario
    for spec in plan['jobs'].values():
        spec['probe_action'] = spec['action']
        spec['action'] = 'check-plan'
    return plan


def plan():
    from ci.plan.entry import parent_identity, write_plan
    from ci.registry.gitlab import GitLab
    import yaml
    root = Path.cwd()
    execution = parent_identity(GitLab.environment(), os.environ)
    frozen = make_plan(execution, os.environ['CI_COMMIT_SHA'], os.environ['PROBE_CASE'])
    write_plan(frozen, root/'build')
    child = yaml.safe_load((root/'build/child.yml').read_text())
    for name in frozen['jobs']:
        child[name]['script'] = ['python3.12 -m tests.ci.gitlab_probe job']
        child[name]['cache'] = []
        child[name]['timeout'] = '5 minutes'
    (root/'build/child.yml').write_text(yaml.safe_dump(child, sort_keys=False))
    print(json.dumps({'probe': frozen['probe_scenario'], 'jobs': list(frozen['jobs'])}))


def job():
    from ci.runtime import Runtime
    rt = Runtime.start(Path.cwd())
    if rt.plan['execution']['source'] != 'merge_request_event' or rt.spec['action'] != 'check-plan':
        raise ValueError('probe only permits report fixtures in MR')
    action = rt.spec['probe_action']
    scenario = rt.plan['probe_scenario']
    key = 'example/v1.0.0'
    payload = {'fixture': 'fixed-small-artifact'}
    if action == 'build-operator-images':
        payload['packages'] = rt.consume('build-packages', 'packages', 'selected')
    elif action == 'test-operators':
        if scenario == 'failure':
            raise ValueError('intentional child test failure')
        if scenario == 'cancel':
            print('ready-for-cancellation', flush=True)
            time.sleep(180)
            raise ValueError('probe was not canceled in time')
        payload['packages'] = rt.consume('build-packages', 'packages', 'selected')
    elif action == 'test-operator-images':
        payload['image'] = rt.consume('build-operator-images:'+key, 'image', key)
    elif action == 'publish-operator-images':
        packages = rt.consume('build-packages', 'packages', 'selected')
        image = rt.consume('build-operator-images:'+key, 'image', key)
        container = rt.consume('test-operator-images:'+key, 'container', key)
        for kind in ('source','wheel'):
            report = rt.consume('test-operators:'+key, kind, key)
            if report['payload']['packages'] != packages:
                raise ValueError('stale package acceptance rejected')
        if image['payload']['packages'] != packages or container['payload']['image'] != image:
            raise ValueError('stale image acceptance rejected')
        payload['image'] = image
    elif action in {'mirror-operator-images','publish-operator-manifest'}:
        dependency, kind = ('publish-operator-images','harbor') if action == 'mirror-operator-images' else ('mirror-operator-images','acr')
        payload['upstream'] = rt.consume(dependency, kind, 'delivery')
        attempts = [row for row in rt.client.jobs(rt.pipeline) if row['name'] == rt.name]
        if len(attempts) == 1:
            raise ValueError('intentional first delivery fixture failure')
    for report in rt.spec['reports']:
        rt.emit(report['kind'], report['subject'], payload)
    rt.finish()
    print(json.dumps({'probe_action': action, 'job_id': rt.job_id, 'consumed': rt.consumed, 'status': 'passed'}))


if __name__ == '__main__':
    {'plan': plan, 'job': job}[sys.argv[1]]()
