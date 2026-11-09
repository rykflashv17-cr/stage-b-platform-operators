from __future__ import annotations


import json


import os


import stat


from contextlib import contextmanager


from datetime import datetime, timezone


from pathlib import Path


from typing import Any


class UnsafeSourceError(ValueError):
    """The selected pathname cannot safely be used for destructive processing."""


def _add_cleanup_error(primary: BaseException, secondary: BaseException, operation: str) -> None:
    errors = list(getattr(primary, "cleanup_errors", []))
    errors.append(f"{operation}: {type(secondary).__name__}: {secondary}")
    primary.cleanup_errors = errors


@contextmanager
def _close_preserving_failure(close, operation: str):
    """Never replace an earlier failure with a later close failure."""
    try:
        yield
    except BaseException as primary:
        try:
            close()
        except BaseException as secondary:
            _add_cleanup_error(primary, secondary, operation)
        raise
    else:
        close()


def _absolute(path: Path) -> Path:
    # Keep symlinks visible. Path.resolve() would conceal a symlink input.
    path = Path(path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise UnsafeSourceError(f"directory is missing, not a directory, or a symlink: {path}") from exc
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_parent(path: Path, input_root: Path) -> tuple[int, str]:
    path, root = _absolute(path), _absolute(input_root)
    # Validate the original root spelling before normalizing benign '..'
    # components for containment. A symlink followed by '..' must stay visible.
    root_fd = _open_directory(root)
    os.close(root_fd)
    try:
        rel = Path(os.path.normpath(path)).relative_to(Path(os.path.normpath(root)))
    except ValueError as exc:
        raise UnsafeSourceError(f"source is outside input_root: {path}") from exc
    if not rel.parts or path.suffix != ".mcap":
        raise UnsafeSourceError(f"source must be an MCAP file below input_root: {path}")
    return _open_directory(path.parent), path.name


def _identity(st: os.stat_result) -> dict[str, int]:
    return {key: int(getattr(st, key)) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}


def capture_source_identity(path: Path, input_root: Path) -> dict[str, int]:
    """lstat a regular MCAP without following its path or parent symlinks."""
    fd, name = _open_parent(path, input_root)
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeSourceError(f"source is not a regular MCAP (symlinks are never deleted): {path}")
        return _identity(st)
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_durable(path: Path, payload: Any) -> None:
    """Create a report exactly once and durably finish it before deletion."""
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)

