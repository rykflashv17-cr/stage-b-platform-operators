"""Render a Pipeline registration from one versioned operator declaration."""
import argparse
import json
from pathlib import Path

from operator_sdk.v2.declaration import load_operator


def registration(path):
    declaration = load_operator(path)
    op = declaration.raw
    mode = op.get("batchMode", "none")
    if mode == "chunk":
        raise ValueError("Pipeline registration supports DATA and BATCH; chunk requires node batchSize scheduling")
    resources = op["resources"]
    return {
        "operatorCode": op["code"],
        "operatorVersion": op["operatorVersion"],
        "name": op["name"],
        "description": op["description"],
        "upgradeDescription": op["description"],
        "nodeKind": "OPERATOR",
        "artifactType": "IMAGE",
        "type": "GPU" if "nvidia.com/gpu" in resources["limits"] else "CPU",
        "functionType": "DATA_PROCESSING",
        "executionScope": "BATCH" if mode == "all" else "DATA",
        "imageRef": op["image"],
        "functionConfig": {"delivery": op["delivery"]} if "delivery" in op else {},
        "executionConfig": {"job": {"command": op["command"], "resources": resources}},
        "ioDefinition": declaration.platform_io(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("declaration", type=Path)
    print(json.dumps(registration(parser.parse_args().declaration), ensure_ascii=False, indent=2))
