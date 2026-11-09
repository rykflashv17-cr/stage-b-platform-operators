"""Plan independently released recipes using only metadata reads."""
import json
from pathlib import Path
import subprocess

from ci.base_images.config import recipes
from ci.base_images.manage import ANNOTATION,RECEIPT_TYPE,PROJECT
from ci.registry.handoff import checked_json


def tree(root,recipe,commit='HEAD'):
    return subprocess.check_output(['git','rev-parse',f'{commit}:{recipe.path.relative_to(root)}'],cwd=root,text=True).strip()


def read_receipt(registry,recipe,digest):
    manifest=checked_json(registry.fetch(recipe.repository,digest),digest)
    if manifest.get('artifactType')!=RECEIPT_TYPE:raise ValueError('invalid foundation receipt type')
    row=json.loads(manifest['annotations'][ANNOTATION])
    if row.get('project')!=PROJECT or row.get('branch')!='develop':raise ValueError('untrusted develop receipt')
    return row


def validate_receipt(root,recipe,row,expected_tree,parent):
    if (row.get('repository')!=recipe.repository or row.get('version')!=recipe.version or
            row.get('platform')!='linux/amd64' or row.get('recipeTree')!=expected_tree or row.get('parent')!=parent):
        raise ValueError('develop receipt does not match the recipe')
    if tree(root,recipe,row['sourceCommit'])!=expected_tree:raise ValueError('foundation source tree mismatch')
    subprocess.run(['git','merge-base','--is-ancestor',row['sourceCommit'],'HEAD'],cwd=root,check=True)


def plan_recipes(root,keys,execution,evidence):
    root=Path(root);all_recipes=recipes(root);result={}
    mr=execution['source']=='merge_request_event'
    for key in sorted(keys):
        recipe=all_recipes[key];parent=None
        if recipe.parent:
            # Publish a new runtime series/version first, then reference it in
            # builder/operator changes. No unpublished numeric FROM can be frozen.
            frozen=evidence.freeze_base(recipe.parent)
            parent={'reference':recipe.parent,'digest':frozen['digest']}
        row={'version':recipe.version,'repository':recipe.repository,'tree':tree(root,recipe),'parent':parent,'action':'check'}
        if not mr:
            digest=evidence.registry.resolve(recipe.repository,'receipt-'+recipe.version)
            if digest is None:
                if execution['ref']!='develop':raise ValueError('missing develop receipt; publish the base on develop first')
                row['action']='build'
            else:
                receipt=read_receipt(evidence.registry,recipe,digest)
                validate_receipt(root,recipe,receipt,row['tree'],parent)
                actual=evidence.freeze_base(recipe.repository+':'+recipe.version)
                if actual['digest']!=receipt.get('digest'):
                    raise ValueError('numeric base digest differs from develop receipt')
                row.update(action='verify',receipt_digest=digest,receipt=receipt)
        result[key]=row
    return result
