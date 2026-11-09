"""Durable build/test evidence with deterministic, bounded Harbor lookup.

An input tag aliases a build record; it is not an extra index service. A missing
record/image needs work. Authorization, malformed evidence and mismatched
identities are errors, never cache misses. Historical job artifacts are optional.
"""
import hashlib
import json
import re

from ci.registry.gitlab import identifier
from ci.registry.handoff import DIGEST, inspect_manifest, repository_for
from ci.serialization import hash_json as hashed

HEX = re.compile(r'[a-f0-9]{64}')


def input_tag(key, input_digest):
    repository_for(key)
    if not HEX.fullmatch(input_digest):raise ValueError('invalid build input digest')
    return 'input-'+hashed([key,input_digest])


def build_tag(row):
    return 'build-'+hashed([row['operator'],row['image']['digest'],row['input_digest']])


def provenance(value):
    if (not isinstance(value,dict) or value.get('project_id')!='395' or
            value.get('source') not in {'push','web','api','schedule','pipeline'} or
            not isinstance(value.get('ref'),str) or
            (value['ref'] not in {'develop','release','main'} and not value['ref'].startswith('release/'))):
        raise ValueError('record must originate from a protected delivery branch')
    for name in ('project_id','parent_pipeline_id','pipeline_id'):identifier(value[name])
    if not re.fullmatch(r'[a-f0-9]{40}',value.get('commit','')):
        raise ValueError('invalid record source commit')
    if not isinstance(value.get('jobs'),list) or not value['jobs']:
        raise ValueError('missing evidence producing jobs')
    for row in value['jobs']:
        identifier(row['id'])
        if not isinstance(row.get('name'),str) or not row['name']:
            raise ValueError('invalid evidence producing job')


def verify_provenance(value, client):
    provenance(value)
    parent=client.pipeline(value['parent_pipeline_id'])
    child=client.pipeline(value['pipeline_id'])
    if (parent.get('sha')!=value['commit'] or parent.get('source')!=value['source'] or parent.get('ref')!=value['ref'] or
            child.get('sha')!=value['commit'] or child.get('ref')!=value['ref'] or child.get('source')!='parent_pipeline'):
        raise ValueError('record pipeline provenance mismatch')
    client.first_child(value['parent_pipeline_id'],value['pipeline_id'],value['commit'])
    jobs={str(row['id']):row for row in client.jobs(value['pipeline_id'])}
    for expected in value['jobs']:
        actual=jobs.get(expected['id'],{})
        if (actual.get('name')!=expected['name'] or actual.get('status')!='success' or
                str(actual.get('pipeline',{}).get('id'))!=value['pipeline_id'] or
                str(actual.get('pipeline',{}).get('project_id'))!=value['project_id'] or
                actual.get('pipeline',{}).get('sha')!=value['commit']):
            raise ValueError('record producing job was not a matching successful attempt')


def validate_build(row, key, input_digest):
    if (row.get('format_version')!=1 or row.get('kind')!='build' or row.get('operator')!=key or
            row.get('input_digest')!=input_digest):
        raise ValueError('build record input identity mismatch')
    repository,_=repository_for(key)
    image=row.get('image',{})
    if (image.get('repository')!=repository or image.get('platform')!='linux/amd64' or
            not DIGEST.fullmatch(image.get('digest','')) or not DIGEST.fullmatch(image.get('config_digest',''))):
        raise ValueError('build record image identity mismatch')
    if not isinstance(row.get('wheels'),dict) or not row['wheels'] or not all(HEX.fullmatch(v) for v in row['wheels'].values()):
        raise ValueError('build record missing actual wheel digests')
    input_tag(key,input_digest)
    provenance(row.get('provenance'))


def save_build(registry, row):
    validate_build(row,row['operator'],row['input_digest'])
    repository=row['image']['repository'];tag=build_tag(row)
    prior=registry.read_record(repository,tag)
    if prior is not None and prior!=row:
        # A second successful attempt of identical inputs may reuse the first
        # record, but cannot change its actual image/wheel content.
        validate_build(prior,row['operator'],row['input_digest'])
        # Candidate labels and producing attempts are delivery metadata. Keep
        # the first accepted record when retrying an interrupted alias write.
        metadata={'provenance','harbor_tag'}
        if {k:v for k,v in prior.items() if k not in metadata}!={k:v for k,v in row.items() if k not in metadata}:
            raise ValueError('immutable build record conflict')
    if prior is None:registry.write_record(repository,tag,row)
    lookup=input_tag(row['operator'],row['input_digest'])
    existing=registry.read_record(repository,lookup)
    if existing is not None:
        validate_build(existing,row['operator'],row['input_digest'])
        if registry.resolve(repository,existing['image']['digest']) is not None:
            return existing
    registry.alias_record(repository,tag,lookup)
    return registry.read_record(repository,lookup)


def find_build(registry,key,input_digest,*,verify_provenance):
    repository,_=repository_for(key)
    row=registry.read_record(repository,input_tag(key,input_digest))
    if row is None:return None
    validate_build(row,key,input_digest)
    verify_provenance(row['provenance'])
    if registry.resolve(repository,row['image']['digest']) is None:return None
    actual=inspect_manifest(registry,repository,row['image']['digest'])
    if actual['config_digest']!=row['image']['config_digest']:
        raise ValueError('build record image config mismatch')
    return row


def test_identity(row):
    if (row.get('format_version')!=1 or row.get('kind')!='test' or row.get('mode') not in {'source','wheel','container','pipeline'} or
            not isinstance(row.get('subject'),str) or not HEX.fullmatch(row.get('definition_digest','')) or
            not isinstance(row.get('images'),dict) or not row['images']):
        raise ValueError('invalid test record identity')
    for key,digest in row['images'].items():
        repository_for(key)
        if not DIGEST.fullmatch(digest):raise ValueError('invalid tested image digest')
    return {key:row[key] for key in ('subject','mode','definition_digest','images')}


def validate_test(row):
    test_identity(row);provenance(row.get('provenance'))
    result=row.get('result',{})
    if (type(result.get('passed')) is not int or result['passed']<1 or result.get('failed')!=0 or result.get('exit_code')!=0):
        raise ValueError('test record must contain passed nonempty results')


def save_test(registry,repository,row):
    validate_test(row)
    tag='test-'+hashed(test_identity(row))
    existing=registry.read_record(repository,tag)
    if existing is not None:
        validate_test(existing)
        if test_identity(existing)!=test_identity(row):raise ValueError('test record identity conflict')
        return existing
    registry.write_record(repository,tag,row)
    return row


def find_test(registry,repository,required,*,verify_provenance):
    expected=test_identity(required)
    row=registry.read_record(repository,'test-'+hashed(expected))
    if row is None:return None
    validate_test(row)
    if test_identity(row)!=expected:raise ValueError('test record identity mismatch')
    verify_provenance(row['provenance'])
    return row
