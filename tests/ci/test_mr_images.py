from ci.plan.images import mr_container_plan, checked_versions


def test_mixed_source_and_dependency_change_builds_only_risky_image():
    plan = {"publication_versions": ["a/v1.0.0", "b/v1.0.0"],
            "build": ["a/v1.0.0", "b/v1.0.0"], "publish": ["a/v1.0.0", "b/v1.0.0"],
            "components": {"a/v1.0.0": {"buildVersion": "1.0.0"}, "b/v1.0.0": {"buildVersion": "1.0.0"}},
            "checks": {"containers": ["b/v1.0.0"]}}
    assert mr_container_plan(plan)["build"] == ["b/v1.0.0"]
    assert plan["build"] == ["a/v1.0.0", "b/v1.0.0"]
    plan["components"] = {}
    assert mr_container_plan(plan)["build"] == ["a/v1.0.0", "b/v1.0.0"]


def test_risky_mr_validates_newly_built_workflow_peers_in_source_and_wheel():
    plan = {"publication_versions": ["a/v1.0.0", "b/v1.0.0"],
            "build": ["a/v1.0.0", "b/v1.0.0"], "publish": [], "components": {},
            "checks": {"wheels": ["b/v1.0.0"], "containers": ["b/v1.0.0"]}}
    assert checked_versions(plan, merge_request=True) == ["a/v1.0.0", "b/v1.0.0"]
    plan["checks"]["containers"] = []
    assert checked_versions(plan, merge_request=True) == ["b/v1.0.0"]
    assert checked_versions(plan, merge_request=False) == ["a/v1.0.0", "b/v1.0.0"]
