"""Frozen plan and per-attempt evidence shared by every pipeline consumer.

plan Job -> raw plan SHA -> first child -> actual producing Job -> report.
Neither a filename nor a successful older retry can replace this chain.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from ci.registry.gitlab import GitLab, identifier


def read_frozen_plan(path, expected_digest, plan_job):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('frozen plan digest mismatch')
    plan = json.loads(raw)
    if plan.get('format_version') != 2 or str(plan.get('execution', {}).get('plan_job_id')) != str(plan_job):
        raise ValueError('frozen plan producer mismatch')
    return plan


def plan_identity(plan, plan_sha256):
    execution = plan['execution']
    return {'project_id': str(execution['project_id']), 'parent_pipeline_id': str(execution['parent_pipeline_id']),
            'plan_job_id': str(execution['plan_job_id']), 'commit': plan['commit'], 'plan_sha256': plan_sha256}


def report_payload(plan, plan_sha256, *, job_id, pipeline_id, job, kind, subject, payload, status='passed'):
    return {'format_version': 1, 'plan': plan_identity(plan, plan_sha256),
            'producer': {'job_id': identifier(job_id), 'pipeline_id': identifier(pipeline_id), 'job': job},
            'kind': kind, 'subject': subject, 'status': status, 'payload': payload}


def check_report(report, plan, plan_sha256, *, job_id, pipeline_id, job, kind, subject):
    expected = report_payload(plan, plan_sha256, job_id=job_id, pipeline_id=pipeline_id, job=job,
                              kind=kind, subject=subject, payload=report.get('payload'))
    if report != expected or not isinstance(report.get('payload'), dict):
        raise ValueError('report identity or result does not match required attempt')
    return report['payload']


def report_path(template, job_id):
    path = Path(template.replace('{job_id}', identifier(job_id)))
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'build' or '{' in str(path):
        raise ValueError('invalid report path')
    return path


def read_report(client, plan, plan_sha256, *, pipeline_id, job, path, kind, subject):
    attempt = client.successful_job(pipeline_id, job)
    if attempt['pipeline'].get('sha') != plan['commit']:
        raise ValueError('report producer commit mismatch')
    raw = client.artifact(attempt['id'], report_path(path, attempt['id']).as_posix())
    return check_report(json.loads(raw), plan, plan_sha256, job_id=attempt['id'],
                        pipeline_id=pipeline_id, job=job, kind=kind, subject=subject)


def validate_runtime(plan, plan_sha256, *, client=None, env=None, root=None):
    env = os.environ if env is None else env
    client = client or GitLab.environment(env)
    execution = plan['execution']
    if client.project != str(execution['project_id']) or plan_sha256 != env.get('PLAN_SHA256'):
        raise ValueError('runtime does not match frozen plan identity')
    current = client.current_job()
    pipeline_id = identifier(env['CI_PIPELINE_ID'])
    if (str(current.get('id')) != env.get('CI_JOB_ID') or current.get('name') != env.get('CI_JOB_NAME') or
            str(current['pipeline'].get('id')) != pipeline_id or str(current['pipeline'].get('project_id')) != client.project):
        raise ValueError('current Job Token identity mismatch')
    parent = client.pipeline(execution['parent_pipeline_id'])
    child = client.pipeline(pipeline_id)
    if (parent.get('sha') != plan['commit'] or child.get('sha') != plan['commit'] or
            parent.get('source') != execution['source'] or parent.get('ref') != execution['ref'] or
            child.get('source') != 'parent_pipeline' or child.get('ref') != parent.get('ref')):
        raise ValueError('pipeline source does not match frozen plan')
    client.first_child(execution['parent_pipeline_id'], pipeline_id, plan['commit'])
    producer = client.successful_job(execution['parent_pipeline_id'], 'plan-changes')
    if (str(producer.get('id')) != str(execution['plan_job_id']) or producer.get('status') != 'success' or producer.get('name') != 'plan-changes' or
            str(producer.get('pipeline', {}).get('project_id')) != client.project or
            str(producer.get('pipeline', {}).get('id')) != str(execution['parent_pipeline_id']) or
            producer.get('pipeline', {}).get('sha') != plan['commit']):
        raise ValueError('plan producer was not a successful matching parent job')
    if root is not None:
        actual = subprocess.check_output(['git','rev-parse','HEAD'], cwd=root, text=True).strip()
        if actual != plan['commit']:
            raise ValueError('checkout differs from frozen plan')
    if execution['source'] != 'merge_request_event':
        if env.get('CI_COMMIT_REF_PROTECTED') != 'true' or not execution.get('protected'):
            raise ValueError('unprotected execution cannot consume delivery plan')
        if execution['ref'] not in {'develop','release','main'}:
            raise ValueError('unexpected delivery branch')
    return current
