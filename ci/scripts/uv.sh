# Source in the job shell so uv and pip share the temporary Nexus credentials.
# Resolve paths in the running Job, not in the upstream trigger environment.
export UV_CACHE_DIR="$CI_PROJECT_DIR/.ci-cache/uv"
export UV_TOOL_DIR="$CI_PROJECT_DIR/.ci-cache/tools"
export PATH="$CI_PROJECT_DIR/.ci-tools/bin:$PATH"
if [ -n "${NEXUS_READ_USERNAME:-}${NEXUS_READ_PASSWORD:-}" ]; then
    test -n "$NEXUS_READ_USERNAME" && test -n "$NEXUS_READ_PASSWORD" || exit 2
    umask 077
    ci_netrc_file=$(mktemp)
    NETRC=$ci_netrc_file
    export NETRC
    trap 'rm -f "$ci_netrc_file"' EXIT
    printf 'machine nexus.striding.internal login %s password %s\n' "$NEXUS_READ_USERNAME" "$NEXUS_READ_PASSWORD" > "$NETRC"
fi

export UV_DEFAULT_INDEX=https://nexus.striding.internal/repository/pypi/simple/
if [ "$(uv --version 2>/dev/null | cut -d' ' -f2)" != "0.12.6" ]; then
    python3.12 -m ci.build.bootstrap_uv
fi
