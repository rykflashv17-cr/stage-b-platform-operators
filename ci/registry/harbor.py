"""Bounded metadata and tag operations in the existing Harbor data project."""
import json
from pathlib import Path
import re
import subprocess
import tempfile

from ci.publish.manifest import Oras, PUBLICATION_PREFIX, DEFINITIONS_PREFIX
from ci.registry.handoff import PREFIX, checked_json, DIGEST

RECORD_TYPE='application/vnd.striding.operator-ci.record.v1'
ANNOTATION='striding.operator-ci.record'
MAX_RECORD=65536


class Registry:
    def __init__(self, binary, config, work, *, writable=False):
        self.binary=str(binary);self.config=str(config);self.work=Path(work);self.writable=writable
        self.work.mkdir(parents=True,exist_ok=True)

    def client(self, repository):
        if not repository.startswith((PREFIX, PUBLICATION_PREFIX, DEFINITIONS_PREFIX)):
            raise ValueError('registry operation outside existing Harbor repositories')
        tail=repository.split('/',1)[1]
        if not re.fullmatch(r'[a-z0-9][a-z0-9_./-]*',tail) or '..' in tail or '//' in tail:
            raise ValueError('invalid Harbor repository')
        # Existing publications use the same Harbor through its controlled HTTP
        # endpoint. Operator image traffic retains the HTTPS endpoint.
        registry=Oras(self.binary,self.config,self.work)
        registry._flags=lambda: (['--plain-http'] if repository.startswith((PUBLICATION_PREFIX,DEFINITIONS_PREFIX)) else [])+['--registry-config',self.config]
        return registry

    def resolve(self, repository, reference):
        separator='@' if DIGEST.fullmatch(reference) else ':'
        return self.client(repository).resolve(repository+separator+reference)

    def fetch(self, repository, digest, *, blob=False):
        return self.client(repository).fetch(repository+'@'+digest,digest,blob=blob)

    def run_write(self, repository, command):
        if not self.writable:raise ValueError('registry client is read only')
        client=self.client(repository)
        result=client.run([self.binary,*command,*client._flags()])
        if result.returncode:raise ValueError('Harbor write failed')

    def tag(self,repository,digest,tag):
        if not DIGEST.fullmatch(digest) or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}',tag):
            raise ValueError('invalid registry tag target')
        self.run_write(repository,['tag',repository+'@'+digest,tag])

    def read_record(self,repository,tag):
        digest=self.resolve(repository,tag)
        if digest is None:return None
        doc=checked_json(self.fetch(repository,digest),digest)
        raw=doc.get('annotations',{}).get(ANNOTATION)
        if doc.get('artifactType')!=RECORD_TYPE or not isinstance(raw,str) or len(raw.encode())>MAX_RECORD:
            raise ValueError('invalid Harbor CI record')
        row=json.loads(raw)
        if not isinstance(row,dict):raise ValueError('invalid Harbor CI record payload')
        return row

    def write_record(self,repository,tag,row,*,replace=False):
        raw=json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw.encode())>MAX_RECORD:raise ValueError('CI record exceeds metadata bound')
        previous=self.read_record(repository,tag)
        if previous==row:return self.resolve(repository,tag)
        if previous is not None and not replace:raise ValueError('immutable CI record conflict')
        self.run_write(repository,['push','--artifact-type',RECORD_TYPE,'--annotation',ANNOTATION+'='+raw,
            '--annotation','org.opencontainers.image.created=1970-01-01T00:00:00Z',repository+':'+tag])
        if self.read_record(repository,tag)!=row:raise ValueError('published CI record differs from supplied content')
        return self.resolve(repository,tag)

    def alias_record(self,repository,source,tag):
        if self.read_record(repository,source) is None:raise ValueError('missing source CI record')
        digest=self.resolve(repository,source)
        self.tag(repository,digest,tag)
        if self.resolve(repository,tag)!=digest:raise ValueError('CI record alias digest mismatch')
