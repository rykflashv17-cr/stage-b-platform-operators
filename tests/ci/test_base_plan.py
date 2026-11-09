from pathlib import Path
import subprocess

import pytest


def repository(root):
    path=root/'images/runtimes/example';path.mkdir(parents=True)
    (path/'VERSION').write_text('1.0.0\n');(path/'Dockerfile').write_text('FROM python:3.12-slim\n')
    (path/'check.sh').write_text('python --version\n')
    subprocess.run(['git','init','-q',str(root)],check=True)
    subprocess.run(['git','add','.'],cwd=root,check=True)
    subprocess.run(['git','-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','fixture'],cwd=root,check=True)


def test_mr_recipe_checks_do_not_publish_even_existing_version(tmp_path):
    from ci.base_images.planning import plan_recipes
    repository(tmp_path)
    result=plan_recipes(tmp_path,['runtime/example'],{'source':'merge_request_event','ref':'feature/base'},None)
    assert result['runtime/example']['action']=='check'
    assert result['runtime/example']['version']=='1.0.0'
    assert result['runtime/example']['parent'] is None


def test_main_requires_develop_receipt_and_never_builds_missing_base(tmp_path):
    from ci.base_images.planning import plan_recipes
    repository(tmp_path)
    class Evidence:
        class Registry:
            def resolve(self,*args):return None
        registry=Registry()
    with pytest.raises(ValueError,match='develop receipt'):
        plan_recipes(tmp_path,['runtime/example'],{'source':'push','ref':'main'},Evidence())


def test_develop_missing_receipt_plans_only_this_recipe(tmp_path):
    from ci.base_images.planning import plan_recipes
    repository(tmp_path)
    class Evidence:
        class Registry:
            def resolve(self,*args):return None
        registry=Registry()
    result=plan_recipes(tmp_path,['runtime/example'],{'source':'push','ref':'develop'},Evidence())
    assert list(result)==['runtime/example'] and result['runtime/example']['action']=='build'


def test_base_build_action_does_not_run_acceptance(tmp_path,monkeypatch):
    from ci.base_images.manage import build
    from ci.base_images.config import Recipe
    recipe=Recipe('runtime','example',tmp_path,'1.0.0',None)
    calls=[]
    monkeypatch.setattr('ci.base_images.manage.docker',lambda *a,**k:calls.append(a))
    monkeypatch.setattr('ci.base_images.manage.inspect',lambda ref:{'Id':'sha256:'+'a'*64})
    monkeypatch.setattr('ci.base_images.manage.smoke',lambda *a:pytest.fail('acceptance belongs to test job'))
    result=build(recipe,{'sourceCommit':'b'*40},'c'*40,None,accept=False)
    assert result=='sha256:'+'a'*64 and len(calls)==1


def test_existing_numeric_base_must_match_its_develop_receipt(tmp_path,monkeypatch):
    from ci.base_images.planning import plan_recipes,tree
    from ci.base_images.config import recipes
    repository(tmp_path);recipe=recipes(tmp_path)['runtime/example']
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=tmp_path,text=True).strip()
    receipt={'repository':recipe.repository,'version':'1.0.0','platform':'linux/amd64',
             'recipeTree':tree(tmp_path,recipe),'parent':None,'sourceCommit':commit,'digest':'sha256:'+'a'*64}
    class Evidence:
        class Registry:
            def resolve(self,*args):return 'sha256:'+'b'*64
        registry=Registry()
        def freeze_base(self,reference):return {'digest':'sha256:'+'c'*64}
    monkeypatch.setattr('ci.base_images.planning.read_receipt',lambda *args:receipt)
    with pytest.raises(ValueError,match='digest'):
        plan_recipes(tmp_path,['runtime/example'],{'source':'push','ref':'main'},Evidence())
