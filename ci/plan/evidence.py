"""Resolve reusable evidence once; consumers use only frozen OCI digests."""
from pathlib import Path
import subprocess

from ci.build.bases import freeze
from ci.publish.manifest import route_for
from ci.registry import records
from ci.registry.handoff import DIGEST,repository_for
from ci.serialization import hash_json


def delivery_pointer(branch):
    route_for(branch)
    return 'delivered-'+hash_json(branch)


def is_ancestor(root,before,after):
    return subprocess.run(['git','merge-base','--is-ancestor',before,after],cwd=root,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0


class FrozenRecord:
    def __init__(self,registry,repository,digest):
        self.registry=registry;self.repository=repository;self.digest=digest

    def read_record(self,repository,reference):
        if repository!=self.repository:raise ValueError('frozen record repository mismatch')
        return self.registry.read_record(repository,self.digest)

    def __getattr__(self,name):return getattr(self.registry,name)


class Evidence:
    def __init__(self,registry,client,root,branch,commit,pipeline_id):
        self.registry=registry;self.client=client;self.root=Path(root);self.branch=branch
        self.commit=commit;self.pipeline_id=str(pipeline_id);self.verified=set()

    def verify(self,provenance):
        identity=hash_json(provenance)
        if identity not in self.verified:
            records.verify_provenance(provenance,self.client)
            if not is_ancestor(self.root,provenance['commit'],self.commit):
                raise ValueError('reused evidence is not an ancestor of this checkout')
            self.verified.add(identity)

    def freeze_base(self,reference):return freeze(self.registry,reference)

    def resolve_tag(self,repository,tag):return self.registry.resolve(repository,tag)

    def find_build(self,key,input_digest):
        repository,_=repository_for(key)
        digest=self.registry.resolve(repository,records.input_tag(key,input_digest))
        return self.read_build(key,input_digest,digest) if digest is not None else None

    def read_build(self,key,input_digest,digest):
        if not DIGEST.fullmatch(digest):raise ValueError('invalid frozen record digest')
        repository,_=repository_for(key)
        value=records.find_build(FrozenRecord(self.registry,repository,digest),key,input_digest,verify_provenance=self.verify)
        return {**value,'record_digest':digest} if value is not None else None

    def test_repository(self,required):
        return route_for('main')[0] if required['mode']=='pipeline' else repository_for(required['subject'])[0]

    def find_test(self,required):
        repository=self.test_repository(required)
        tag='test-'+hash_json(records.test_identity(required))
        digest=self.registry.resolve(repository,tag)
        return self.read_test(required,digest) if digest is not None else None

    def read_test(self,required,digest):
        if not DIGEST.fullmatch(digest):raise ValueError('invalid frozen record digest')
        repository=self.test_repository(required)
        value=records.find_test(FrozenRecord(self.registry,repository,digest),repository,required,verify_provenance=self.verify)
        return {**value,'record_digest':digest} if value is not None else None

    def delivery(self):
        repository=route_for(self.branch)[0]
        digest=self.registry.resolve(repository,delivery_pointer(self.branch))
        if digest is None:return None
        row=self.registry.read_record(repository,digest)
        if (not isinstance(row,dict) or row.get('format_version')!=1 or row.get('kind')!='delivery'
                or row.get('ref')!=self.branch or not isinstance(row.get('versions'),dict)):
            raise ValueError('invalid delivery receipt')
        self.verify(row['provenance'])
        if int(row['provenance']['parent_pipeline_id'])>=int(self.pipeline_id):
            raise ValueError('delivery receipt is from this or a newer pipeline')
        for key,value in row['versions'].items():
            repository_for(key)
            if (not records.HEX.fullmatch(value.get('input_digest','')) or
                    not DIGEST.fullmatch(value.get('image_digest','')) or
                    value.get('harbor') is not True or value.get('acr') is not True or
                    not isinstance(value.get('tag'),str)):
                raise ValueError('invalid per-version delivery receipt')
        return {**row,'record_digest':digest}

    def assert_delivery_current(self,frozen):
        repository=route_for(self.branch)[0]
        current=self.registry.resolve(repository,delivery_pointer(self.branch))
        if current!=(frozen or {}).get('record_digest'):
            raise ValueError('delivery baseline changed; start a new parent pipeline')
