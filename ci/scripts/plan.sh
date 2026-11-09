#!/bin/sh
set -eu
umask 077
mkdir -p build
diff_base=${CI_MERGE_REQUEST_DIFF_BASE_SHA:-${CI_COMMIT_BEFORE_SHA:-}}
case "$diff_base" in
  ''|0000000000000000000000000000000000000000)
    diff_base=$(git rev-parse --verify HEAD^ 2>/dev/null || git hash-object -w -t tree /dev/null) ;;
esac
if python3.12 -m ci.plan.entry --diff-base "$diff_base" --documents-only; then
  exit 0
else
  status=$?
  [ "$status" = 1 ] || exit "$status"
fi
. ci/scripts/uv.sh
python3.12 -m ci.build.metadata_environment
plan_python=.ci-tools/metadata-venv/bin/python
"$plan_python" -m ci.plan.entry --diff-base "$diff_base" --scope-only
set --
if "$plan_python" -c 'import json,sys; sys.exit(not json.load(open("build/scope.json"))["registry_needed"])'; then
  . ci/scripts/oras.sh
  auth_dir=$(mktemp -d)
  trap 'rm -rf "$auth_dir"; if [ -n "${ci_netrc_file:-}" ]; then rm -f "$ci_netrc_file"; fi' EXIT
  if [ "$CI_PIPELINE_SOURCE" = merge_request_event ]; then
    printf '%s\n' '{"auths":{}}' > "$auth_dir/config.json"
  else
    "$plan_python" -c 'import sys; from ci.publish.manifest import write_registry_config; write_registry_config(sys.argv[1])' "$auth_dir/config.json"
  fi
  set -- --registry-config "$auth_dir/config.json"
fi
"$plan_python" -m ci.plan.entry --diff-base "$diff_base" "$@"
