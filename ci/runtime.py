"""Per-attempt execution context; every consumer resolves the latest producer."""
import hashlib
import json
import os
from pathlib import Path

from ci.plan.execution import check_report,read_frozen_plan,report_path,report_payload,validate_runtime
from ci.registry.gitlab import GitLab


class Runtime:
    def __init__(self,root,plan,digest,client,env):
        self.root=Path(root).resolve();self.plan=plan;self.digest=digest;self.client=client;self.env=env
        self.name=env['CI_JOB_NAME'];self.pipeline=env['CI_PIPELINE_ID'];self.job_id=env['CI_JOB_ID']
        if self.name not in plan['jobs']:raise ValueError('job absent from frozen plan')
        self.spec=plan['jobs'][self.name];self.written=set();self.consumed={}

    @classmethod
    def start(cls,root,env=None):
        env=os.environ if env is None else env;root=Path(root)
        plan=read_frozen_plan(root/'build/plan.json',env['PLAN_SHA256'],env['PLAN_JOB_ID'])
        client=GitLab.environment(env)
        validate_runtime(plan,env['PLAN_SHA256'],client=client,env=env,root=root)
        return cls(root,plan,env['PLAN_SHA256'],client,env)

    def descriptor(self,job,kind,subject):
        matches=[row for row in self.plan['jobs'][job]['reports'] if row['kind']==kind and row['subject']==subject]
        if len(matches)!=1:raise ValueError('report is not uniquely assigned to this job')
        return matches[0]

    def destination(self,kind,subject):
        row=self.descriptor(self.name,kind,subject)
        return self.root/report_path(row['path'],self.job_id)

    def emit(self,kind,subject,payload,*,status='passed'):
        destination=self.destination(kind,subject);destination.parent.mkdir(parents=True,exist_ok=True)
        row=report_payload(self.plan,self.digest,job_id=self.job_id,pipeline_id=self.pipeline,
            job=self.name,kind=kind,subject=subject,payload=payload,status=status)
        destination.write_text(json.dumps(row,sort_keys=True,indent=2,allow_nan=False)+'\n')
        self.written.add((kind,subject))

    def assert_producers_current(self):
        for name,job_id in self.consumed.items():
            if str(self.client.successful_job(self.pipeline,name)['id'])!=job_id:
                raise ValueError('producer attempt changed during consumption; retry this consumer')

    def finish(self):
        self.assert_producers_current()
        required={(row['kind'],row['subject']) for row in self.spec['reports']}
        if self.written!=required:raise ValueError('missing assigned report before job completion')

    def consume(self,job,kind,subject):
        row=self.descriptor(job,kind,subject)
        attempt=self.client.successful_job(self.pipeline,job)
        if job in self.consumed and self.consumed[job]!=str(attempt['id']):
            raise ValueError('producer attempt changed during consumption; retry this consumer')
        if attempt.get('pipeline',{}).get('sha')!=self.plan['commit']:raise ValueError('producer commit mismatch')
        raw=self.client.artifact(attempt['id'],report_path(row['path'],attempt['id']).as_posix())
        report=json.loads(raw)
        payload=check_report(report,self.plan,self.digest,job_id=attempt['id'],pipeline_id=self.pipeline,
            job=job,kind=kind,subject=subject)
        self.consumed[job]=str(attempt['id'])
        return {'producer':report['producer'],'payload':payload}

    def packages(self,*,verify_files=True):
        report=self.consume('build-packages','packages','selected')
        wheels=report['payload'].get('wheels')
        if not isinstance(wheels,dict) or not wheels:raise ValueError('missing package wheel inventory')
        for name,digest in wheels.items():
            if Path(name).name!=name or not name.endswith('.whl'):raise ValueError('invalid wheel filename')
            if verify_files:
                path=self.root/'build/wheels'/name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                    raise ValueError('downloaded wheel digest differs from latest package producer')
        return report

    def image(self,key,evidence):
        row=self.plan['operators'][key];source=row['source']
        if source['kind']=='registry':
            expected=source['record']
            record=evidence.read_build(key,row['build_identity']['digest'],expected['record_digest'])
            if record is None:raise ValueError('frozen reusable image or record missing; start a new parent pipeline')
            if record!=expected:raise ValueError('frozen build record differs from plan')
            return {'image':record['image'],'producer':{'record_digest':record['record_digest']},
                'wheels':record['wheels'],'input_digest':record['input_digest'],'source_commit':record['provenance']['commit']}
        consumed=self.consume(source['job'],'image',key);payload=consumed['payload']
        if payload.get('input_digest')!=row['build_identity']['digest']:raise ValueError('image build input mismatch')
        return {**payload,'producer':consumed['producer'],'source_commit':self.plan['commit']}

    def evidence(self,*,writable=False):
        from ci.plan.evidence import Evidence
        from ci.registry.harbor import Registry
        branch=self.env.get('CI_MERGE_REQUEST_TARGET_BRANCH_NAME') if self.plan['execution']['source']=='merge_request_event' else self.plan['execution']['ref']
        registry=Registry(self.env.get('ORAS','build/toolchain/oras'),self.env['OPERATOR_REGISTRY_CONFIG'],
            self.root/'build/registry',writable=writable)
        return Evidence(registry,self.client,self.root,branch,self.plan['commit'],self.plan['execution']['parent_pipeline_id'])

    def assigned_tests(self):
        return {name:row for name,row in self.plan['tests'].items() if row.get('job')==self.name}
