# Source this only on GitLab software runners.
tool_image='harbor.striding.internal/data/data-platform/data-platform-ci@sha256:6db3df84e0b558515844b403acc41cd29567a1746098e7b7a90a62a296d34048'
tools=build/toolchain
mkdir -p "$tools"
if [ ! -x "$tools/oras" ]; then
    docker pull -q "$tool_image" >/dev/null
    cid="$(docker create "$tool_image" noop)"
    # tar-stream like data-platform's setup-release-toolchain.sh so the
    # extracted binary is owned by the runner user, not root.
    docker cp "$cid":/usr/local/bin/oras - | tar -xO >"$tools/oras"
    chmod 0755 "$tools/oras"
    docker rm "$cid" >/dev/null
fi
"$tools/oras" version | grep -Eq '^Version:[[:space:]]+1[.]3[.]0$'
