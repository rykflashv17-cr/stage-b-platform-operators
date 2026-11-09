import pytest


def test_base_publication_rejects_test_of_previous_build_attempt(tmp_path,monkeypatch):
    from ci.base_images.jobs import publish_bases
    from tests.ci.test_base_plan import repository
    repository(tmp_path)
    class Runtime:
        root=tmp_path;spec={'recipes':['runtime/example']}
        plan={'execution':{'ref':'develop','source':'push','protected':True},
            'base_images':{'runtime/example':{'version':'1.0.0','tree':'a'*40,'parent':None}}}
        def evidence(self,**kw):return None
        def consume(self,job,kind,subject):
            if kind=='base-build':return {'producer':{'job_id':'32'},'payload':{'image':{'digest':'sha256:'+'a'*64}}}
            return {'payload':{'build_producer':{'job_id':'31'},'image_digest':'sha256:'+'a'*64}}
    with pytest.raises(ValueError,match='build attempt'):publish_bases(Runtime())


def test_mr_base_check_has_no_registry_writer(tmp_path,monkeypatch):
    from ci.base_images.jobs import check_bases
    from tests.ci.test_base_plan import repository
    repository(tmp_path);seen=[]
    class Runtime:
        root=tmp_path;spec={'recipes':['runtime/example']}
        plan={'commit':'b'*40,'execution':{'source':'merge_request_event'},
            'base_images':{'runtime/example':{'version':'1.0.0','tree':'a'*40,'parent':None}}}
        def emit(self,*args,**kwargs):seen.append(args)
    monkeypatch.setattr('ci.base_images.manage.build',lambda *a,**kw:'sha256:'+'a'*64)
    check_bases(Runtime())
    assert seen[0][0]=='base-test' and seen[0][2]['result']['passed']==1
