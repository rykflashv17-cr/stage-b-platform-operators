"""Current shared package identity and recovery of unfinished Nexus publications."""
import hashlib

from ci.plan.projects import shared_projects, source_files
from ci.serialization import hash_json
from ci.publish.package_records import validate_record


def package_identity(root,project):
    inputs={p.relative_to(project.path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source_files(project)}
    return {'name':project.distribution,'version':project.version,'filename':project.wheel,
            'source_path':project.path.relative_to(root).as_posix(),'source_digest':hash_json(inputs)}


def plan_packages(root,selected,execution,records):
    mr=execution['source']=='merge_request_event'
    pending=set(selected)
    for project in shared_projects(root):
        if mr and project.distribution not in selected:continue
        expected=package_identity(root,project)
        saved=records.read(project.distribution,project.version)
        if saved is None:
            if mr:continue
            if execution['ref']!='develop':
                raise ValueError('shared package must first be published on develop: '+project.distribution)
            pending.add(project.distribution)
        else:
            validate_record(saved,expected)
            if not mr and execution['ref'] in {'release','main'}:pending.discard(project.distribution)
    return sorted(pending)
