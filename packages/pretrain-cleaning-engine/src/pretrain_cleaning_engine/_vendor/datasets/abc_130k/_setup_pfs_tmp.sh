# Redirect node-local /tmp scratch onto shared PFS tmp (sourced, not executed).
# Call after RANK is set. Override root with ABC130K_PFS_TMP.
#
#   /tmp/abc130k_video_cache_r${RANK}  ->  $ABC130K_PFS_TMP/abc130k_video_cache/${HOST}_r${RANK}
#   TMPDIR/TEMP/TMP                    ->  $ABC130K_PFS_TMP  (if unset or still under /tmp)

ABC130K_PFS_TMP="${ABC130K_PFS_TMP:-/mnt/pfs/Data/liuweilin/Dataclean/tmp}"
ABC130K_HOST="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
ABC130K_RANK_FOR_TMP="${RANK:-0}"

mkdir -p "$ABC130K_PFS_TMP"

if [[ -z "${TMPDIR:-}" || "$TMPDIR" == /tmp || "$TMPDIR" == /tmp/* ]]; then
    export TMPDIR="$ABC130K_PFS_TMP"
fi
export TEMP="${TEMP:-$TMPDIR}"
export TMP="${TMP:-$TMPDIR}"
if [[ "$TEMP" == /tmp || "$TEMP" == /tmp/* ]]; then
    export TEMP="$ABC130K_PFS_TMP"
fi
if [[ "$TMP" == /tmp || "$TMP" == /tmp/* ]]; then
    export TMP="$ABC130K_PFS_TMP"
fi
mkdir -p "$TMPDIR" "$TEMP" "$TMP"

_abc_relink_tmp_to_pfs() {
    local link="$1"
    local target="$2"
    mkdir -p "$target" "$(dirname "$link")"
    if [[ -L "$link" ]]; then
        local cur
        cur="$(readlink "$link" || true)"
        if [[ "$cur" != "$target" ]]; then
            ln -sfn "$target" "$link"
        fi
        return 0
    fi
    if [[ -e "$link" ]]; then
        echo "[tmp] replace local $link -> $target" >&2
        rm -rf "$link"
    fi
    ln -sfn "$target" "$link"
}

if [[ -z "${ABC130K_VIDEO_CACHE:-}" ]]; then
    _abc_pfs_video="${ABC130K_PFS_TMP}/abc130k_video_cache/${ABC130K_HOST}_r${ABC130K_RANK_FOR_TMP}"
    _abc_local_video="/tmp/abc130k_video_cache_r${ABC130K_RANK_FOR_TMP}"
    _abc_relink_tmp_to_pfs "$_abc_local_video" "$_abc_pfs_video"
    export ABC130K_VIDEO_CACHE="$_abc_local_video"
elif [[ "$ABC130K_VIDEO_CACHE" == /tmp || "$ABC130K_VIDEO_CACHE" == /tmp/* ]]; then
    _abc_name="$(basename -- "$ABC130K_VIDEO_CACHE")"
    _abc_pfs_video="${ABC130K_PFS_TMP}/abc130k_video_cache/${ABC130K_HOST}_${_abc_name}"
    _abc_relink_tmp_to_pfs "$ABC130K_VIDEO_CACHE" "$_abc_pfs_video"
fi

echo "[tmp] host=${ABC130K_HOST} RANK=${ABC130K_RANK_FOR_TMP} TMPDIR=${TMPDIR} ABC130K_VIDEO_CACHE=${ABC130K_VIDEO_CACHE:-<unset>} target=$(readlink -f "${ABC130K_VIDEO_CACHE:-$TMPDIR}")"
