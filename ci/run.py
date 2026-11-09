"""One fixed child entrypoint. Authorization and identities precede preparation."""
import argparse
import importlib
import json
from pathlib import Path

from ci.runtime import Runtime

HANDLERS={
    'build-packages':('ci.build.jobs','build_packages'),
    'check-code':('ci.acceptance.jobs','check_code'),
    'test-operators':('ci.acceptance.jobs','test_operators'),
    'test-packages':('ci.acceptance.packages','test_packages'),
    'publish-packages':('ci.publish.packages','publish_packages'),
    'build-operator-images':('ci.build.jobs','build_images'),
    'test-operator-images':('ci.acceptance.jobs','test_images'),
    'test-pipelines':('ci.acceptance.jobs','test_images'),
    'check-images':('ci.acceptance.jobs','check_images'),
    'check-base-images':('ci.base_images.jobs','check_bases'),
    'build-base-images':('ci.base_images.jobs','build_bases'),
    'test-base-images':('ci.base_images.jobs','test_bases'),
    'publish-base-images':('ci.base_images.jobs','publish_bases'),
    'publish-operator-images':('ci.publish.delivery','publish_images'),
    'mirror-operator-images':('ci.publish.delivery','mirror_images'),
    'publish-operator-manifest':('ci.publish.delivery','publish_manifest'),
}


def execute(runtime):
    action=runtime.spec['action']
    if action!='check-plan':
        if action not in HANDLERS:raise ValueError('unknown planned action')
        module,name=HANDLERS[action]
        getattr(importlib.import_module(module),name)(runtime)
    runtime.finish()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--validate',action='store_true')
    args=parser.parse_args();runtime=Runtime.start(Path.cwd())
    if args.validate:print(json.dumps({'action':runtime.spec['action'],'job':runtime.name,'source':runtime.plan['execution']['source']}))
    else:execute(runtime)


if __name__=='__main__':main()
