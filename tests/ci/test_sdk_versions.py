from ci import ROOT
PROJECT=ROOT/"operators/mcap_delivery_accepted"

def test_v5_registration_retains_delivery_contract():
    from tools.export_registration import registration
    saved=registration(PROJECT/'operator.yaml')
    assert saved['operatorVersion']=='5.0.0'
    assert saved['functionConfig']['delivery']=={'type':'archive','format':'zip'}
    assert saved['ioDefinition']['outputs'][0]['name']=='archive'

def test_build_can_resolve_new_sdk_without_changing_legacy_lock():
    from ci.build.packages import sdk_pins
    from ci.plan.projects import read_project
    old=read_project(ROOT/'operators/mcap_read_normalize')
    new=read_project(PROJECT)
    assert {pin.version for pin in sdk_pins(ROOT,[old,new])}=={'0.3.0.dev7','0.3.0.dev9'}

def test_ci_tooling_uses_one_common_version_and_a_delivery_capable_sdk():
    from ci.build.environment import tools,tool_directory
    from ci.build.sdk_wheel import locked_sdk
    selected=tools(ROOT)
    names=[p.distribution for p in selected]
    assert len(names)==len(set(names))
    assert locked_sdk(ROOT,tool_directory(ROOT)).version=='0.3.0.dev9'
