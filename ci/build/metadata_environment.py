"""Prepare only YAML/packaging from the existing tooling lock for metadata jobs."""
import os
from pathlib import Path
import re
import subprocess
import tomllib

from ci.build.sdk_wheel import INDEX


def requirements(lock):
    packages=tomllib.loads(Path(lock).read_text())['package'];result=[]
    for name in ('packaging','pyyaml'):
        matches=[p for p in packages if p['name']==name]
        if len(matches)!=1:raise ValueError('metadata tool is not uniquely locked')
        package=matches[0]
        if package['source'].get('registry','').rstrip('/')!=INDEX.rstrip('/'):
            raise ValueError('metadata tools must use Nexus')
        hashes=sorted({row['hash'] for row in package['wheels']})
        if not hashes or not all(re.fullmatch(r'sha256:[a-f0-9]{64}',value) for value in hashes):
            raise ValueError('metadata tool missing locked wheel digests')
        result.append(name+'=='+package['version']+' '+' '.join('--hash='+value for value in hashes))
    return '\n'.join(result)+'\n'


def main():
    root=Path.cwd();target=root/'.ci-tools/metadata-venv';target.parent.mkdir(parents=True,exist_ok=True)
    path=root/'.ci-tools/metadata-requirements.txt';path.write_text(requirements(root/'ci/tooling/uv.lock'))
    subprocess.run(['uv','venv','--allow-existing','--python','python3.12',str(target)],check=True)
    subprocess.run(['uv','pip','sync','--python',str(target/'bin/python'),'--only-binary',':all:',
        '--require-hashes','--default-index',INDEX,str(path)],check=True)


if __name__=='__main__':main()
