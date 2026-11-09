"""Prepare one selected MCAP episode or render a pretraining data cleaning operator registration locally."""
import argparse
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from operator_sdk.pipeline_storage import relative_key
from pretraining_data_cleaning_common import require
from pretraining_data_cleaning_common.archive import pack_bundle
from tools.export_registration import registration


def pack_episode(output, dataset_root, mcap, source, episode_index, dataset_name, part="", task=""):
    require(source in ("jindian_marvin", "abc_130k") and type(episode_index) is int and episode_index >= 0,
            "invalid source or episode index")
    relative_key(dataset_name)
    root, selected = Path(dataset_root).absolute(), Path(mcap).absolute()
    relative = selected.relative_to(root)
    relative_key(relative.as_posix())
    require(selected.suffix == ".mcap" and (source != "abc_130k" or selected.name == "episode.mcap"),
            "invalid selected MCAP filename")
    paths = [selected]
    if source == "abc_130k":
        paths.append(selected.with_name("eef_pose.parquet"))
    for path in paths:
        require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
                "missing or unsafe selected source file")
    output = Path(output)
    with TemporaryDirectory(dir=output.parent) as tmp:
        sources = Path(tmp) / "sources"
        for path in paths:
            target = sources / dataset_name / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat()
            shutil.copyfile(path, target)
            after = path.stat()
            require(os.path.samestat(before, after) and before.st_mtime_ns == after.st_mtime_ns
                    and before.st_size == after.st_size == target.stat().st_size,
                    "selected source changed during packing")
        value = dict(episode_index=episode_index, dataset_root=dataset_name,
                     mcap_path=(Path(dataset_name)/relative).as_posix(),
                     rel_path=(relative.parent if source == "abc_130k" else relative).as_posix(),
                     part=part, task=task)
        staged = Path(tmp) / "episode.zip"
        pack_bundle(staged, value, {"sources": sources})
        os.link(staged, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reg = commands.add_parser("registration")
    reg.add_argument("declaration", type=Path)
    pack = commands.add_parser("pack-episode")
    for name in ("output", "dataset-root", "mcap"):
        pack.add_argument("--"+name, type=Path, required=True)
    pack.add_argument("--source", choices=["jindian_marvin", "abc_130k"], required=True)
    pack.add_argument("--episode-index", type=int, required=True)
    pack.add_argument("--dataset-name", required=True)
    pack.add_argument("--part", default="")
    pack.add_argument("--task", default="")
    args = vars(parser.parse_args())
    if args.pop("command") == "registration":
        print(json.dumps(registration(args["declaration"]), ensure_ascii=False, indent=2))
    else:
        pack_episode(**args)


if __name__ == "__main__":
    main()
