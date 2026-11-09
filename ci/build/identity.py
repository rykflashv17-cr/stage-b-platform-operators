"""Compute reusable image inputs before compiling any local wheel."""
from ci.plan.projects import operator_projects, build_inputs
from ci.build.bases import parse_dockerfile
import re
from ci.base_images.config import DIGEST
from ci.serialization import hash_json


def build_identity(root, key, bases):
    projects={f'{p.name}/v{p.version}':p for p in operator_projects(root)}
    if key not in projects:raise ValueError('unknown exact operator version')
    project=projects[key]
    references,_=parse_dockerfile((project.path/'Dockerfile').read_text())
    resolved={}
    for reference in references:
        base=bases.get(reference,{})
        digest=base.get('digest','')
        if not re.fullmatch(DIGEST,digest) or base.get('reference')!=reference.rsplit(':',1)[0]+'@'+digest:
            raise ValueError('missing or inconsistent frozen foundation identity')
        resolved[reference]=digest
    inputs={'policy':1,'operator':key,'platform':'linux/amd64',
            'files':build_inputs(root,project),'bases':resolved}
    return {'digest':hash_json(inputs),'inputs':inputs}
