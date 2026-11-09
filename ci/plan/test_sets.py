"""Data-only discovery of exact-version checks and declared compatibility sets."""
import hashlib
import json
from pathlib import Path
import re

import yaml

from ci.registry.handoff import KEY
from ci.plan.impact import ACCEPTANCE_INPUTS

NAME = re.compile(r'[a-z][a-z0-9_-]*')
DEFAULT_REQUIREMENTS = {'platform':'linux/amd64','runner_tags':['software'],'environment':'ci/tooling/uv.lock'}


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key,str) or key in mapping:
                raise ValueError('duplicate or non-string YAML key')
            mapping[key] = self.construct_object(value_node,deep=deep)
        return mapping


def fields(value, required, optional=()):
    if not isinstance(value,dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError('invalid compatibility declaration fields')


def inside(root, path):
    p = Path(path)
    if p.is_absolute() or '..' in p.parts or not (root/p).resolve().is_relative_to(root.resolve()):
        raise ValueError('test path escapes its declared directory')
    return root/p


def identity(root, paths, selection):
    values={}
    for path in sorted(set(paths)):
        path=Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('test identity cannot use external or symlinked input')
        if path.is_file():
            values[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    raw=json.dumps({'policy':1,'files':values,'selection':selection},sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()


def test_files(folder):
    return [p for p in folder.rglob('*') if p.is_file() and not any(part in {'__pycache__','.pytest_cache','.venv'} for part in p.parts)
            and p.suffix not in {'.pyc','.md'}]


def version_test(root, key, mode):
    root=Path(root)
    if not KEY.fullmatch(key) or mode not in {'source','wheel','container'}:
        raise ValueError('invalid version test identity')
    from ci.plan.projects import operator_project
    project=operator_project(root,key).path
    folder=project/'tests'/mode
    if not folder.is_dir() or not list(folder.rglob('test_*.py')):
        raise ValueError(f'missing or empty required test set: {key}/{mode}')
    # Parent conftest and test helpers are part of the contract in every mode.
    paths=[p for p in test_files(project/'tests')
           if p.relative_to(project/'tests').parts[0] not in {'source','wheel','container'}]
    folders=[folder]
    if mode=='source' and (project/'pyproject.toml').is_file():
        from ci.plan.projects import dependencies,read_project
        folders += [owner.path/'tests/source' for owner in dependencies(root,read_project(project))
                    if (owner.path/'tests/source').is_dir()]
        paths += test_files(root/'tests/support')+test_files(project/'examples')
        paths += [project/'operator.yaml',root/'tools/check_operators.py',root/'tools/export_registration.py']
    paths += [p for directory in folders for p in test_files(directory)]+[project/'uv.lock',root/'ci/tooling/uv.lock']
    paths += [root/path for path in ACCEPTANCE_INPUTS[mode]]
    selection={'key':key,'mode':mode,'requirements':DEFAULT_REQUIREMENTS}
    return {'kind':mode,'subject':key,'path':folder.relative_to(root).as_posix(),
            'paths':[p.relative_to(root).as_posix() for p in folders],
            'requirements':dict(DEFAULT_REQUIREMENTS),'identity':identity(root,paths,selection)}


def discover(root, versions):
    root=Path(root); result={}
    for path in sorted((root/'tests/pipelines').glob('*/definition.yaml')):
        workflow=path.parent.name
        if not NAME.fullmatch(workflow) or path.stat().st_size>65536:
            raise ValueError('invalid compatibility definition')
        try:
            body=yaml.load(path.read_text(),Loader=UniqueLoader)
        except yaml.YAMLError as exc:
            raise ValueError('invalid compatibility YAML') from exc
        fields(body,{'requirements','combinations'},{'container_checks','fixtures'})
        requirements=body['requirements']
        fields(requirements,{'platform','runner_tags','environment'})
        if (requirements['platform']!='linux/amd64' or requirements['runner_tags']!=['software'] or
                requirements['environment'] not in {'ci/tooling/uv.lock',f'tests/pipelines/{workflow}/uv.lock'}):
            raise ValueError('unsupported compatibility execution requirements')
        if not isinstance(body['combinations'],dict) or not body['combinations']:
            raise ValueError('compatibility combinations must be a nonempty mapping')
        permits=body.get('container_checks',{})
        if not isinstance(permits,dict):raise ValueError('invalid container check permission')
        for key, permission in permits.items():
            fields(permission,{'merge_with_pipeline'})
            if key not in versions or type(permission['merge_with_pipeline']) is not bool:
                raise ValueError('invalid container check permission')
        fixtures=body.get('fixtures',[])
        if not isinstance(fixtures,list):raise ValueError('invalid fixture paths')
        shared=[]
        for name in fixtures:
            fixture=inside(root,name)
            if not fixture.exists():raise ValueError('missing compatibility fixture')
            shared.extend(test_files(fixture) if fixture.is_dir() else [fixture])
        for name, combination in body['combinations'].items():
            if not isinstance(name,str) or not NAME.fullmatch(name):raise ValueError('invalid combination name')
            fields(combination,{'versions','tests'},{'parameters','environment'})
            own_requirements=dict(requirements)
            if 'environment' in combination:
                lock=inside(root,combination['environment'])
                if not lock.is_relative_to(path.parent) or lock.name!='uv.lock' or not lock.is_file():
                    raise ValueError('invalid combination environment')
                own_requirements['environment']=combination['environment']
            members=combination['versions']
            if not isinstance(members,dict) or not members:raise ValueError('empty compatibility members')
            codes=[]
            for node,key in members.items():
                if not isinstance(node,str) or not NAME.fullmatch(node) or not isinstance(key,str) or key not in versions:
                    raise ValueError('unknown compatibility member')
                codes.append(key.split('/')[0])
            if len(codes)!=len(set(codes)):raise ValueError('duplicate operator in compatibility combination')
            tests=combination['tests']
            if not isinstance(tests,list) or not tests or len(tests)!=len(set(tests)):
                raise ValueError('invalid compatibility test paths')
            paths=[]
            for test_name in tests:
                item=inside(path.parent,test_name)
                if not item.is_file() or not item.name.startswith('test_') or item.suffix!='.py':
                    raise ValueError('missing compatibility test file')
                paths.append(item.relative_to(root).as_posix())
            parameters=combination.get('parameters',{})
            if not isinstance(parameters,dict):raise ValueError('invalid compatibility parameters')
            try:json.dumps(parameters,allow_nan=False)
            except (TypeError,ValueError) as exc:raise ValueError('parameters must be JSON values') from exc
            subject=workflow+'/'+name
            result[subject]={'kind':'pipeline','subject':subject,'workflow':workflow,'versions':members,'paths':paths,
                'parameters':parameters,'requirements':own_requirements,
                'merge_with_pipeline':{key:permits.get(key,{}).get('merge_with_pipeline',False) for key in members.values()},
                'identity':identity(root,test_files(path.parent)+shared+[root/own_requirements['environment']]+[root/item for item in ACCEPTANCE_INPUTS['pipeline']],combination)}
    return result


def select(definitions, affected):
    chosen=set()
    workflows={row['workflow'] for row in definitions.values()}
    for workflow in workflows:
        combinations={name:row for name,row in definitions.items() if row['workflow']==workflow}
        codes={key.split('/')[0] for row in combinations.values() for key in row['versions'].values()}
        for key in sorted(affected):
            if key.split('/')[0] not in codes:continue
            matches=[name for name,row in combinations.items() if key in row['versions'].values()]
            if not matches:raise ValueError(f'missing compatible combination for {key}')
            chosen.update(matches)
    return sorted(chosen)


def group_mr(definitions, selected, builds):
    groups=[]
    for name in selected:
        members=set(definitions[name]['versions'].values())
        overlap=[group for group in groups if members & group['members'] & set(builds)]
        group={'members':members,'combinations':{name}}
        for prior in overlap:
            group['members'].update(prior['members']);group['combinations'].update(prior['combinations']);groups.remove(prior)
        groups.append(group)
    covered={key for group in groups for key in group['members']}
    groups.extend({'members':{key},'combinations':set()} for key in sorted(set(builds)-covered))
    return [{'versions':sorted(group['members']),'combinations':sorted(group['combinations'])}
            for group in sorted(groups,key=lambda group:sorted(group['members']))]
