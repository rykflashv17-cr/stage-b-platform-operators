"""Exact, immutable references needed after retiring a source directory.

This file does not declare a new version or prove publication: Harbor records
and their original GitLab producers are verified before they can be consumed.
"""
import json
from pathlib import Path
import re
from ci.registry.handoff import repository_for, DIGEST
from ci.registry.records import HEX


def load_history(root, current):
    path=Path(root)/'ci/archived-versions.json'
    if not path.exists():return {}
    if path.stat().st_size>1024*1024:raise ValueError('archived version references too large')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate archived version field')
            result[key]=value
        return result
    rows=json.loads(path.read_text(),object_pairs_hook=unique)
    if not isinstance(rows,dict):raise ValueError('invalid archived versions')
    for key,row in rows.items():
        repository_for(key)
        if key in current:raise ValueError('archived version has current source: '+key)
        if not isinstance(row,dict) or set(row)!={'build','source','tests'}:raise ValueError('invalid archived version')
        build,source,tests=row['build'],row['source'],row['tests']
        if (not isinstance(build,dict) or set(build)!={'record_digest','input_digest'} or
            not HEX.fullmatch(str(build['input_digest'])) or not DIGEST.fullmatch(str(build['record_digest'])) or
            not isinstance(source,dict) or set(source)!={'commit','path','definition_sha256'} or
            not re.fullmatch('[a-f0-9]{40}',str(source['commit'])) or
            source['path'] not in {'operators/'+key,'operators/'+key.split('/v')[0]} or
            not HEX.fullmatch(str(source['definition_sha256'])) or not isinstance(tests,dict) or
            set(tests)!={'source','wheel','container'}):raise ValueError('invalid archived version references')
        for test in tests.values():
            if (not isinstance(test,dict) or set(test)!={'record_digest','definition_digest'} or
                not HEX.fullmatch(str(test['definition_digest'])) or
                not DIGEST.fullmatch(str(test['record_digest']))):raise ValueError('invalid archived test reference')
    return rows


def accepted(root, key, pointer, evidence):
    record=evidence.read_build(key,pointer['build']['input_digest'],pointer['build']['record_digest'])
    if record is None:raise ValueError('archived build/image missing; restore original evidence: '+key)
    if record['provenance']['commit']!=pointer['source']['commit']:
        raise ValueError('archived source commit differs from build record')
    from ci.publish.operator_sources import definition
    source=definition(root,key,pointer['source']['commit'])
    if source['path']!=pointer['source']['path'] or source['sha256']!=pointer['source']['definition_sha256']:
        raise ValueError('archived declaration differs from original source')
    tests={}
    for mode,pin in pointer['tests'].items():
        required={'format_version':1,'kind':'test','subject':key,'mode':mode,
            'definition_digest':pin['definition_digest'],'images':{key:record['image']['digest']}}
        result=evidence.read_test(required,pin['record_digest'])
        if result is None:raise ValueError('archived acceptance missing: '+key+'/'+mode)
        tests[key+'/'+mode]={'kind':mode,'subject':key,'identity':pin['definition_digest'],
            'versions':{key.split('/v')[0]:key},'action':'reuse','record':result,'archived':True}
    return record,tests
