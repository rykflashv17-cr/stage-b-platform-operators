"""Install the pinned uv CLI on a Linux shell Runner, using only the Python stdlib."""
import hashlib
from io import BytesIO
from pathlib import Path
import platform
import tempfile
from ci.build.downloads import nexus_request, urlopen
from zipfile import ZipFile

VERSION = "0.12.6"
WHEELS = {
    "x86_64": ("f6/6b/dbcffa319e640b3f38fc8764b99031773d4449725ca21854309e436c08f9",
               "manylinux_2_17_x86_64.manylinux2014_x86_64",
               "8cb1c4af10a1037d2e875ac86da32ea0df20a9623e11769d33515d14158cd3c2"),
    "aarch64": ("ae/3c/fe2c8b17df5302ea438ac595b5978210bcde9b23adf4fff755e592574fbf",
                "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64",
                "1226946a479a5ae9be4132dafb1a02303be2eb4d39e931d4310088d1c5e4d3a7"),
}


def fetch_binary(arch):
    path, tag, digest = WHEELS[arch]
    url = f"https://nexus.striding.internal/repository/pypi/packages/uv/{VERSION}/uv-{VERSION}-py3-none-{tag}.whl"
    with urlopen(nexus_request(url), timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("uv bootstrap wheel digest mismatch")
    with ZipFile(BytesIO(data)) as archive:
        return archive.read(f"uv-{VERSION}.data/scripts/uv")


def main():
    arch = platform.machine()
    if platform.system() != "Linux" or arch not in WHEELS:
        raise SystemExit("CI bootstrap requires Linux x86_64 or aarch64")
    binary = fetch_binary(arch)
    directory = Path(".ci-tools/bin")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as staged:
        staged.write(binary)
    p = Path(staged.name)
    p.chmod(0o755)
    p.replace(directory / "uv")


if __name__ == "__main__":
    main()
