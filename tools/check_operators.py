"""Validate operator declarations and examples with the active locked SDK."""
from ci import ROOT
import json
from pathlib import Path
from operator_sdk.v2.declaration import load_operator
from operator_sdk.contracts import load,validate
from ci.plan.projects import operator_projects

def check_one(path):
    """Validate one declaration and its examples with the caller's locked SDK."""
    path=Path(path);count=0
    op=load_operator(path)
    for direction in ("inputs","outputs"):
        for name,schema in op.schemas[direction].items():
            for kind in ("valid","invalid"):
                samples=list((path.parent / "examples" / direction / name / kind).glob("*.yaml"))
                if not samples:raise ValueError(f"missing samples: {path} {direction}/{name}/{kind}")
                for sample in samples:
                    valid=True
                    try:validate(schema,load(sample))
                    except ValueError:valid=False
                    if valid!=(kind=="valid"):raise ValueError(f"unexpected example result: {sample}")
                    count+=1
    for kind in ("valid","invalid"):
        samples=list((path.parent / "examples/parameters" / kind).glob("*.yaml"))
        if not samples:raise ValueError(f"missing parameter samples: {path}")
        for sample in samples:
            valid=True
            try:op.parameters(load(sample))
            except ValueError:valid=False
            if valid!=(kind=="valid"):raise ValueError(f"unexpected example result: {sample}")
            count+=1
    return {"validation":"passed","examples":count}


def check(root):
    root=Path(root);projects=operator_projects(root)
    paths=[project.path/"operator.yaml" for project in projects]
    paths.append(root/"templates/python-operator/operator.yaml")
    count=sum(check_one(path)['examples'] for path in paths)
    return {"validation":"passed","operators":len(projects),"templates":1,"examples":count}

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("declaration",nargs="?",type=Path)
    args=parser.parse_args()
    try:print(json.dumps(check_one(args.declaration) if args.declaration else check(ROOT)))
    except Exception as exc:
        print(json.dumps({"validation":"failed","error":str(exc)}));raise SystemExit(1)
