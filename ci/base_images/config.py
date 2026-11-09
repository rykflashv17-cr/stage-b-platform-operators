"""Standalone foundation recipes and the audited first-runtime migration."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

PREFIX = "harbor.striding.internal/data/data-operators/base"
DIGEST = r"sha256:[0-9a-f]{64}"
NAME = r"[a-z0-9]+(?:-[a-z0-9]+)*"
VERSION = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"
# Publication before e2a404f3 committed this digest to images/pins.json.
BOOTSTRAP_DIGEST = "sha256:b8b1fbf77bfdf929d1dea6d711c1564773fa40a3af72ed3ce8b08e59bd812b33"
BOOTSTRAP_REVISION = "fb7fe5612cda48ba7f1e42157d128577d0bf6520"


def repo(kind, name):
    return f"{PREFIX}/{kind}/{name}"


@dataclass(frozen=True)
class Recipe:
    kind: str
    name: str
    path: Path
    version: str
    parent: str | None

    @property
    def repository(self):
        return repo(self.kind, self.name)


def recipes(root):
    found = {}
    for kind in ("runtime", "builder"):
        for path in sorted((Path(root) / "images" / (kind + "s")).glob("*")):
            if not path.is_dir():
                continue
            if path.is_symlink() or not re.fullmatch(NAME, path.name):
                raise ValueError(f"invalid {kind} directory: {path}")
            required = ["VERSION", "Dockerfile", "check.sh"]
            if kind == "builder":
                required.append("check-build.sh")
            for name in required:
                entry = path / name
                if not entry.is_file() or entry.is_symlink():
                    raise ValueError(f"missing regular file {entry}")
            raw = (path / "VERSION").read_bytes()
            if not re.fullmatch(rf"{VERSION}\n?".encode(), raw):
                raise ValueError(f"{path}/VERSION must be a single X.Y.Z")
            version = raw.decode().strip()
            parent = None
            if kind == "builder":
                dockerfile = (path / "Dockerfile").read_text()
                stages = re.findall(r"^\s*FROM[ \t]+([^\n]+)", dockerfile, re.I | re.M)
                match = re.fullmatch(rf"({re.escape(PREFIX)}/runtime/{NAME}:{VERSION})(?:[ \t]+[Aa][Ss][ \t]+[\w.-]+)?",
                                     stages[0].strip() if len(stages) == 1 else "")
                if not match:
                    raise ValueError(f"{path}: builder requires one FROM a versioned Harbor runtime")
                parent = match[1]
            found[f"{kind}/{path.name}"] = Recipe(kind, path.name, path, version, parent)
    return found


def bootstrap_reference(root, exact_tag):
    repository = repo("runtime", "python312-cpu")
    if exact_tag != repository + ":1.0.0":
        return None
    path = Path(root) / "images/runtimes/python312-cpu"
    # Only the unchanged original Dockerfile is eligible. VERSION and check.sh
    # are metadata; that Dockerfile copies nothing from the context.
    if (set(p.name for p in path.iterdir()) != {"Dockerfile", "VERSION", "check.sh"}
            or any(p.is_symlink() or not p.is_file() for p in path.iterdir())
            or (path / "VERSION").read_bytes() not in (b"1.0.0", b"1.0.0\n")
            or hashlib.sha256((path / "Dockerfile").read_bytes()).hexdigest()
            != "4b0d7a10b08e47e47043e68f2c802eb332fe547687491fd38998380d67fd2c0f"):
        raise ValueError("bootstrap recipe differs from pinned build; bump VERSION")
    return repository + "@" + BOOTSTRAP_DIGEST
