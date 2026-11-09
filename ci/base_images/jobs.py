"""Recipe build/test/publish actions; version receipts keep their existing format."""
import json
import subprocess

from ci.base_images import manage
from ci.base_images.config import recipes
from ci.base_images.planning import read_receipt,validate_receipt
from ci.registry.handoff import inspect_manifest,check_local_image


def passed(recipe):return {'passed':2 if recipe.parent else 1,'failed':0,'exit_code':0}


def check_bases(rt):
    if rt.plan['execution']['source']!='merge_request_event':raise ValueError('combined recipe check is MR only')
    found=recipes(rt.root)
    for key in rt.spec['recipes']:
        row=rt.plan['base_images'][key];recipe=found[key]
        image=manage.build(recipe,{'sourceCommit':rt.plan['commit']},row['tree'],row['parent'])
        rt.emit('base-test',key,{'image':image,'tree':row['tree'],'parent':row['parent'],'result':passed(recipe)})


def build_bases(rt):
    if rt.plan['execution']['ref']!='develop' or not rt.plan['execution']['protected']:
        raise ValueError('new base builds require protected develop')
    registry=rt.evidence(writable=True).registry;found=recipes(rt.root)
    for key in rt.spec['recipes']:
        row=rt.plan['base_images'][key];recipe=found[key]
        image=manage.build(recipe,{'sourceCommit':rt.plan['commit']},row['tree'],row['parent'],accept=False)
        tag=f"{recipe.version}-ci.{rt.plan['execution']['parent_pipeline_id']}.{rt.job_id}"
        if registry.resolve(recipe.repository,tag) is not None:raise ValueError('unexpected existing base build attempt tag')
        reference=recipe.repository+':'+tag
        manage.docker('tag',image,reference);manage.docker('push',reference)
        digest=registry.resolve(recipe.repository,tag)
        if digest is None:raise ValueError('base temporary upload missing')
        actual=inspect_manifest(registry,recipe.repository,digest)
        check_local_image(manage.inspect(image),actual)
        rt.emit('base-build',key,{'image':actual,'tree':row['tree'],'parent':row['parent'],'temporary_tag':tag})


def build_report(rt,key):
    row=rt.plan['base_images'][key]
    return rt.consume('build-base-images:'+key+'/'+row['version'],'base-build',key)


def test_bases(rt):
    from ci.build.image_handoff import pull_image
    found=recipes(rt.root)
    for key in rt.spec['recipes']:
        report=build_report(rt,key);body=report['payload'];row=rt.plan['base_images'][key]
        if body['tree']!=row['tree'] or body['parent']!=row['parent']:raise ValueError('base build differs from frozen recipe')
        reference=pull_image(body['image']);recipe=found[key]
        parent=manage.parent_image(row['parent'])
        if parent:manage.pull(parent)
        manage.smoke(recipe,reference,parent)
        rt.emit('base-test',key,{'build_producer':report['producer'],'image_digest':body['image']['digest'],
            'tree':row['tree'],'parent':row['parent'],'result':passed(recipe)})


class ReceiptRegistry:
    def __init__(self,registry,recipe):self.registry=registry;self.recipe=recipe;self.repository=recipe.repository
    def tags(self):return self.registry.client(self.repository).repo_tags(self.repository)
    def resolve(self,tag):return self.registry.resolve(self.repository,tag)
    def read_receipt(self,version):
        digest=self.resolve('receipt-'+version)
        if digest is None:raise ValueError('missing foundation receipt')
        return read_receipt(self.registry,self.recipe,digest)
    def save_receipt(self,row):
        self.registry.run_write(self.repository,['push','--artifact-type',manage.RECEIPT_TYPE,'--annotation',
            manage.ANNOTATION+'='+json.dumps(row,sort_keys=True,separators=(',',':')),
            '--annotation','org.opencontainers.image.created=1970-01-01T00:00:00Z',self.repository+':receipt-'+row['version']])
    def tag(self,digest,tag):self.registry.tag(self.repository,digest,tag)
    def check_tag(self,tag,digest):
        if self.resolve(tag)!=digest:raise ValueError('foundation tag digest conflict')


def publish_bases(rt):
    execution=rt.plan['execution']
    if execution['source']=='merge_request_event' or execution['ref']!='develop' or not execution.get('protected'):
        raise ValueError('base publication requires protected develop')
    found=recipes(rt.root);evidence=rt.evidence(writable=True)
    for key in rt.spec['recipes']:
        planned=rt.plan['base_images'][key];recipe=found[key];built=build_report(rt,key)
        tested=rt.consume('test-base-images:'+key+'/'+planned['version'],'base-test',key)['payload']
        if tested.get('build_producer')!=built['producer'] or tested.get('image_digest')!=built['payload']['image']['digest']:
            raise ValueError('base acceptance belongs to another build attempt')
        if (tested.get('result')!=passed(recipe) or tested.get('tree')!=planned['tree'] or tested.get('parent')!=planned['parent']):
            raise ValueError('base acceptance incomplete or mismatched')
        image=built['payload']['image'];actual=inspect_manifest(evidence.registry,recipe.repository,image['digest'])
        if actual!=image:raise ValueError('base image differs from accepted content')
        fresh={'project':manage.PROJECT,'branch':'develop','sourceCommit':rt.plan['commit'],
            'pipelineId':execution['parent_pipeline_id'],'repository':recipe.repository,'version':recipe.version,
            'platform':'linux/amd64','recipeTree':planned['tree'],'digest':image['digest'],'parent':planned['parent']}
        rt.assert_producers_current()
        receipt=manage.publish(recipe,ReceiptRegistry(evidence.registry,recipe),lambda:fresh,
            lambda row:validate_receipt(rt.root,recipe,row,planned['tree'],planned['parent']))
        rt.emit('base-release',key,receipt)
        (rt.root/'build/base-images.json').write_text(json.dumps({'foundations':{key:receipt}},sort_keys=True,indent=2)+'\n')
