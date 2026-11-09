"""Read a declaration from its exact source revision, never from a newer checkout."""
import hashlib
from pathlib import Path
import re
import subprocess
import yaml
from ci.registry.handoff import repository_for


def definition(root, key, commit=None):
    repository,version=repository_for(key);code=key.rsplit('/v',1)[0]
    paths=['operators/'+code] if commit is None else ['operators/'+code,'operators/'+key]
    if commit is not None and not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('invalid source commit')
    found=[]
    for path in paths:
        if commit is None:
            file=Path(root)/path/'operator.yaml'
            if not file.is_file():continue
            raw=file.read_bytes()
        else:
            process=subprocess.run(['git','show',commit+':'+path+'/operator.yaml'],cwd=root,capture_output=True)
            if process.returncode:continue
            raw=process.stdout
        if not 0<len(raw)<=1024*1024:raise ValueError('invalid operator definition size')
        body=yaml.safe_load(raw)
        if not isinstance(body,dict) or body.get('code')!=code or body.get('operatorVersion')!=version:
            if commit is None:raise ValueError('operator definition identity mismatch')
            continue
        found.append({'raw':raw,'path':path,'sha256':hashlib.sha256(raw).hexdigest(),'commit':commit})
    if len(found)!=1:raise ValueError('original operator definition missing or ambiguous: '+key)
    return found[0]
