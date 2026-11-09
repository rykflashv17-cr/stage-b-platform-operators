import hashlib
import json
import os
from subprocess import CompletedProcess
from zipfile import ZipFile

import pytest

from tests.pipelines.pretraining_data_cleaning.container_acceptance import read_report, require_delivery_manifest, require_failure_report, write_fixture


def test_nonroot_request_can_be_read_with_runner_umask(tmp_path):
    path = tmp_path / "request.json"
    previous = os.umask(0o077)
    try:
        write_fixture(path, {"fixture": True})
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("code,expected", [(125, 0), (1, 1)])
def test_container_startup_error_retains_diagnostics(code, expected):
    process = CompletedProcess([], code, "", "fixture could not be mounted")
    with pytest.raises(RuntimeError, match="fixture could not be mounted"):
        read_report(process, "marvin", "mcap_read_normalize", expected)

@pytest.mark.parametrize("filename", ["result.json", "quality.json", "quality_records.jsonl"])
def test_failed_container_cannot_publish_success_or_quality(tmp_path, filename):
    (tmp_path / "error.json").write_text("{}")
    require_failure_report(tmp_path)
    (tmp_path / filename).write_text("{}")
    with pytest.raises(AssertionError):
        require_failure_report(tmp_path)

def test_manifest_must_identify_the_accepted_original_in_the_frozen_input_order():
    digests = {"rejected.mcap": hashlib.sha256(b"bad").hexdigest(), "clean.mcap": hashlib.sha256(b"good").hexdigest()}
    keys = list(digests)
    attempt = "56510b31-9a30-4960-b373-98f5886e69c4"
    ref = {"attempt_id": attempt, "input_name": "raw_files", "input_index": 1,
           "logical_key": "clean.mcap", "sha256": digests["clean.mcap"]}
    manifest = {"format": "mcap_delivery_manifest_v1", "accepted": [{"episode_index": 0, "source_ref": ref}]}
    require_delivery_manifest(manifest, attempt, keys, digests, [(0, "clean.mcap")])
    ref["input_index"] = 0
    with pytest.raises(AssertionError):
        require_delivery_manifest(manifest, attempt, keys, digests, [(0, "clean.mcap")])


@pytest.mark.parametrize('index,archive_delivery,readable', [
    (0, False, True), (1, True, False), (2, True, False),
    (3, False, False), (3, True, True),
])
def test_container_source_access_matches_the_selected_delivery_contract(index, archive_delivery, readable):
    from tests.pipelines.pretraining_data_cleaning.container_acceptance import source_access
    config, mounts = source_access(index, archive_delivery, '/validation/case/inputs',
                                   ['clean.mcap', 'eef_pose.parquet'], 'fixture-volume', 'case')
    assert config['read_roots'] == (['/validation/case/inputs'] if readable else [])
    assert any('readonly' in item for item in mounts) == readable
    if index == 3 and archive_delivery:
        assert config['storage_files'] == {'clean.mcap': '/validation/case/inputs/clean.mcap'}
    else:
        assert 'storage_files' not in config


@pytest.mark.parametrize('problem', [None, 'empty', 'wrong_bytes', 'extra_member', 'source_index', 'size'])
def test_archive_acceptance_checks_actual_originals_and_manifest(tmp_path, problem):
    from tests.pipelines.pretraining_data_cleaning.container_acceptance import require_delivery_archive
    body = b'original'
    digest = hashlib.sha256(body).hexdigest()
    attempt = '56510b31-9a30-4960-b373-98f5886e69c4'
    ref = {'attempt_id': attempt, 'input_name': 'raw_files', 'input_index': 1,
           'logical_key': 'clean.mcap', 'sha256': digest}
    row = {'delivery_path': 'episode_000/clean.mcap', 'size_bytes': len(body), 'sha256': digest,
           'business': {'episode_index': 0, 'source_ref': ref}}
    if problem == 'source_index': ref['input_index'] = 0
    if problem == 'size': row['size_bytes'] += 1
    path = tmp_path / 'archive.zip'
    with ZipFile(path, 'w') as archive:
        if problem != 'empty':
            archive.writestr(row['delivery_path'], b'changed' if problem == 'wrong_bytes' else body)
        archive.writestr('delivery_manifest.json', json.dumps({
            'format': 'delivery_manifest_v1', 'files': [] if problem == 'empty' else [row]}))
        if problem == 'extra_member': archive.writestr('extra.log', b'log')
    expected = [] if problem == 'empty' else [(0, 'clean.mcap')]
    args = (path, attempt, ['rejected.mcap', 'clean.mcap'], {'clean.mcap': digest}, expected)
    if problem in (None, 'empty'):
        require_delivery_archive(*args)
    else:
        with pytest.raises(AssertionError): require_delivery_archive(*args)


def test_container_failure_retains_json_error_even_when_stderr_has_logs():
    process = CompletedProcess([], 1, '{"error":"original source unavailable"}', 'checking quality')
    with pytest.raises(RuntimeError, match='original source unavailable'):
        read_report(process, 'marvin', 'mcap_delivery_accepted', 0)


@pytest.mark.parametrize('problem', [None, 'missing_case', 'wrong_image', 'missing_peer'])
def test_incremental_container_acceptance_uses_explicit_multiversion_combinations(problem):
    from ci import ROOT
    from ci.plan.projects import operator_projects
    from tests.pipelines.pretraining_data_cleaning.container_acceptance import accept_plan
    from ci.plan.history import load_history
    current={f'{p.name}/v{p.version}' for p in operator_projects(ROOT)}
    keys=sorted(current | set(load_history(ROOT,current)))
    images={key:{'image':'sha256:'+str(i)*64,'operator':key} for i,key in enumerate(keys,1)}
    plan={'commit':'a'*40,'publication_versions':keys,'build':keys}
    calls=[]
    def accept(chain,label):
        calls.append(label)
        report={'images':{code:{'id':row['image'],'operator':row['operator'],
                               'operator_version':row['operator'].split('/v')[1]} for code,row in chain.items()},
                'cases':{case:{'ok':True} for case in ('marvin','gaps','abc','empty','input_error')}}
        if problem=='missing_case':report['cases'].pop('abc')
        if problem=='wrong_image':next(iter(report['images'].values()))['id']='sha256:'+'f'*64
        return report
    if problem=='missing_peer':images.pop(keys[0])
    if problem:
        with pytest.raises(ValueError):accept_plan(plan,images,accept)
    else:
        result=accept_plan(plan,images,accept)
        assert set(result['images'])==set(keys)
        assert len(calls)==2 and set(result['combinations'])=={'manifest_delivery','archive_delivery'}
        assert result['publication_versions']==keys and result['commit']==plan['commit']
        assert 'active' not in result and 'alternates' not in result
