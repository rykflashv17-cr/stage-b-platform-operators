"""Fixed source adapters; ABC adds exact timeline propagation to the pinned source."""
from pathlib import Path
from pretrain_cleaning_common.spec import build_adapter as upstream_adapter, setup_imports, configure_source_environment, _load_module

def build_adapter(spec):
    if spec.package_dir.name != "abc_130k":
        return upstream_adapter(spec)
    setup_imports(spec)
    configure_source_environment(spec)
    module=_load_module(Path(__file__).with_name("abc_adapter.py"),"stage_b_v4_abc_adapter")
    return module.build_adapter(spec.package_dir)
