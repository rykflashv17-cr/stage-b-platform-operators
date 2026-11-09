set -eu
python - <<'PY'
import os
import platform
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

assert os.environ["TZ"] == "Asia/Shanghai"
assert Path("/etc/timezone").read_text().strip() == "Asia/Shanghai"
assert Path("/etc/localtime").resolve() == Path("/usr/share/zoneinfo/Asia/Shanghai")
assert datetime.fromtimestamp(0).isoformat() == "1970-01-01T08:00:00"
assert datetime.now().astimezone().utcoffset() == timedelta(hours=8)
assert time.localtime(0).tm_hour == 8
assert os.geteuid() == 10001
assert platform.machine() == "x86_64"
assert sys.version_info[:2] == (3, 12)
assert ssl.create_default_context().get_ca_certs()
assert sqlite3.connect(":memory:").execute("select 1").fetchone() == (1,)
PY
