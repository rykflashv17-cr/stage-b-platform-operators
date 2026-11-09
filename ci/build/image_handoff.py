"""Upload a newly built image to a per-attempt tag; never publish acceptance."""
import hashlib
import json
import subprocess

from ci.build.bases import inspect_image
from ci.registry.handoff import temporary_target,inspect_manifest,check_local_image


def stage_image(registry, built, plan, env):
    key=built['operator']
    repository,tag=temporary_target(key,plan['execution'],env)
    local=inspect_image(built['tag'])
    if local['Id']!=built['image']:raise ValueError('build image changed before upload')
    previous=registry.resolve(repository,tag)
    if previous is None:
        target=repository+':'+tag
        subprocess.run(['docker','tag',built['image'],target],check=True,timeout=30)
        subprocess.run(['docker','push',target],check=True,timeout=900)
    digest=registry.resolve(repository,tag)
    if digest is None:raise ValueError('temporary upload did not produce a registry digest')
    record=inspect_manifest(registry,repository,digest)
    check_local_image(local,record)
    return {**record,'operator':key,'temporary_tag':tag,'build_inputs':built['build_inputs'],
            'input_digest':plan['operators'][key]['build_identity']['digest']}


def pull_image(record):
    subprocess.run(['docker','pull','--platform','linux/amd64',record['reference']],check=True,timeout=600)
    local=inspect_image(record['reference'])
    if record['reference'] not in local.get('RepoDigests',[]):raise ValueError('pulled image registry identity mismatch')
    check_local_image(local,record)
    return record['reference']
