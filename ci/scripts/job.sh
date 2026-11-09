#!/bin/sh
set -eu
umask 077
python3.12 -m ci.run --validate > build/runtime.json
source=$(python3.12 -c 'import json; print(json.load(open("build/runtime.json"))["source"])')
action=$(python3.12 -c 'import json; print(json.load(open("build/runtime.json"))["action"])')
if [ "$action" = check-plan ]; then
  python3.12 -m ci.run
  exit 0
fi
. ci/scripts/uv.sh
python3.12 -m ci.build.metadata_environment
job_python=.ci-tools/metadata-venv/bin/python
case "$action" in
  check-code|test-operator-images|test-pipelines|check-images)
    python3.12 -c 'from pathlib import Path; from ci.build.sdk_wheel import locked_sdk,prepare_sdk; root=Path.cwd(); prepare_sdk(root,locked_sdk(root,root/"ci/tooling"))'
    UV_PROJECT_ENVIRONMENT="$CI_PROJECT_DIR/.ci-tools/test-venv" uv sync --project ci/tooling --frozen --no-editable
    job_python=.ci-tools/test-venv/bin/python ;;
esac
case "$action" in
  build-operator-images|test-operator-images|test-pipelines|check-images|*base-images|publish-operator-*|mirror-operator-images)
    . ci/scripts/oras.sh
    export PATH="$CI_PROJECT_DIR/build/toolchain:$PATH"
    auth_dir=$(mktemp -d)
    trap 'rm -rf "$auth_dir"; if [ -n "${ci_netrc_file:-}" ]; then rm -f "$ci_netrc_file"; fi' EXIT
    OPERATOR_REGISTRY_CONFIG="$auth_dir/config.json"
    export OPERATOR_REGISTRY_CONFIG
    if [ "$source" != merge_request_event ] && [ "$CI_COMMIT_REF_PROTECTED" = true ]; then
      "$job_python" -c 'import sys; from ci.publish.manifest import write_registry_config; write_registry_config(sys.argv[1])' "$OPERATOR_REGISTRY_CONFIG"
    else
      printf '%s\n' '{"auths":{}}' > "$OPERATOR_REGISTRY_CONFIG"
    fi
    export DOCKER_CONFIG="$auth_dir"
    if [ "$action" = mirror-operator-images ]; then
      OPERATOR_MIRROR_CONFIG="$auth_dir/mirror.json"
      export OPERATOR_MIRROR_CONFIG
      "$job_python" -c 'import os; from ci.publish.mirror import write_registry_config; write_registry_config(os.environ["OPERATOR_MIRROR_CONFIG"])'
    fi ;;
esac
"$job_python" -m ci.run
git diff --exit-code -- '*.lock'
