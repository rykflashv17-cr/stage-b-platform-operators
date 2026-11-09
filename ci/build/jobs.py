"""Package and image production actions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

def build_packages(rt):
    from ci.build.environment import prepare
    files=prepare(rt.root,rt.spec['versions'],template=rt.spec['template'],environments=rt.spec['environments'],packages=rt.spec.get('packages',[]))
    rt.emit('packages','selected',{'versions':rt.spec['versions'],'environments':rt.spec['environments'],
        'wheels':{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in files}})


def project_wheels(root,key,inventory):
    from ci.plan.projects import operator_projects,wheel_paths
    projects={f'{p.name}/v{p.version}':p for p in operator_projects(root)}
    result={path.name:inventory[path.name] for path in wheel_paths(root,projects[key])}
    if not result:raise ValueError('empty project wheel inventory')
    return result


def build_local(rt,keys,packages):
    from ci.build.images import build,build_plan
    from ci.build.identity import build_identity
    bases={}
    for key in keys:
        row=rt.plan['operators'][key];bases.update(row['bases'])
        if build_identity(rt.root,key,row['bases'])!=row['build_identity']:
            raise ValueError('build inputs changed after planning')
    built=build(rt.root,build_plan(rt.root,keys),'ci-'+rt.job_id,
        frozen_bases=bases,wheel_cache=rt.root/'.ci-cache/wheels')
    return {row['operator']:row for row in built.values()}


def build_images(rt):
    from ci.build.image_handoff import stage_image
    packages=rt.packages();evidence=rt.evidence(writable=True)
    for key,built in build_local(rt,rt.spec['versions'],packages).items():
        image=stage_image(evidence.registry,built,rt.plan,rt.env)
        descriptor={name:image[name] for name in ('repository','digest','platform_digest','config_digest','platform','reference')}
        rt.emit('image',key,{'image':descriptor,'input_digest':image['input_digest'],
            'temporary_tag':image['temporary_tag'],'build_inputs':image['build_inputs'],
            'wheels':project_wheels(rt.root,key,packages['payload']['wheels']),'inputs':{'packages':packages}})
