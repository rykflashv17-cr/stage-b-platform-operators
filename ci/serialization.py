"""Canonical JSON identities shared by planning and durable records."""
import hashlib
import json


def hash_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
