"""Validate the operator's Dockerfile and bind its exact Harbor tags to pulled content."""
import json
import os
import re
import shlex
import subprocess

from ci.base_images.manage import Harbor
from ci.base_images.config import BOOTSTRAP_DIGEST, DIGEST, NAME, PREFIX, VERSION

PLATFORM = "linux/amd64"
TAG = re.compile(rf"{re.escape(PREFIX)}/(?:builder|runtime)/{NAME}:{VERSION}")
FROM = re.compile(r"^\s*FROM\s+(?:--platform=linux/amd64\s+)?(?P<image>\S+)"
                  r"(?:\s+AS\s+(?P<alias>[a-z][a-z0-9_.-]*))?\s*$", re.I)


def instructions(source):
    current = ""
    for line in source.splitlines():
        if line.startswith("\ufeff") or re.match(r"\s*#\s*(?:escape|syntax)\s*=", line, re.I):
            raise ValueError("unsupported Dockerfile parser directive or BOM")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            current += line.rstrip()[:-1]
            continue
        instruction = current + line
        if current and re.match(r"\s*FROM\s", instruction, re.I):
            raise ValueError("put each operator FROM on a single line")
        current = ""
        yield instruction.strip()
    if current:
        raise ValueError("incomplete Dockerfile instruction")


def validate_build_inputs(command, arguments, aliases, prior_count):
    if command == "ADD":
        raise ValueError("operator contexts accept local files via COPY; ADD can fetch remote artifacts")
    tokens = shlex.shlex(arguments, posix=True)
    tokens.whitespace_split = True
    tokens.commenters = ""
    for flag in tokens:
        if not flag.startswith("--"):
            break
        option, _, value = flag.partition("=")
        if option.lower() == "--network" and value != "none":
            raise ValueError("operator RUN cannot override its offline build network")
        if option.lower() == "--mount":
            if "$" in value:
                raise ValueError("operator mount must use literal options")
            sources = [v.partition("=")[2] for v in value.split(",") if v.partition("=")[0].lower() == "from"]
        elif option.lower() == "--from":
            sources = [value]
        else:
            continue
        for ref in sources:
            if ref.lower() not in aliases and not (ref.isdecimal() and int(ref) < prior_count):
                raise ValueError(f"operator {command} can only use a previous build stage: {ref}")


def parse_dockerfile(source):
    aliases, references, final, stage_count = {}, [], None, 0
    for line in instructions(source):
        command, *arguments = line.split(None, 1)
        if command.upper() != "FROM":
            if command.upper() in {"COPY", "RUN", "ADD"}:
                validate_build_inputs(command.upper(), arguments[0], aliases, stage_count - 1)
            continue
        match = FROM.fullmatch(line)
        if not match:
            raise ValueError(f"invalid operator FROM: {line}")
        image, alias = match.group("image", "alias")
        external = image.lower() not in aliases
        if external and not TAG.fullmatch(image):
            raise ValueError(f"operator FROM must select an exact Harbor foundation X.Y.Z: {image}")
        parent = image if external else aliases[image.lower()]
        if alias:
            if alias.lower() in aliases:
                raise ValueError(f"duplicate Dockerfile stage: {alias}")
            aliases[alias.lower()] = parent
        if external and parent not in references:
            references.append(parent)
        final = parent
        stage_count += 1
    if not final or not final.startswith(PREFIX + "/runtime/"):
        raise ValueError("operator final stage must derive from a versioned runtime")
    return references, final


def pin_dockerfile(source, references):
    # Patch only FROM operands. Preserve stage aliases and every other instruction.
    lines = []
    for line in source.splitlines(keepends=True):
        match = FROM.fullmatch(line)
        if match and match["image"] in references:
            line = line[:match.start("image")] + references[match["image"]] + line[match.end("image"):]
        lines.append(line)
    return "".join(lines)


def inspect_image(ref):
    return json.loads(subprocess.check_output(["docker", "image", "inspect", ref], text=True))[0]


def pull(ref):
    command = ["docker", "pull", "--platform", PLATFORM, ref]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        # Show registry errors even when the caller is a shell without captured stderr.
        raise subprocess.CalledProcessError(result.returncode, command, stderr=result.stderr)
    matches = re.findall(rf"^Digest: ({DIGEST})\s*$", result.stdout, re.M)
    if len(matches) != 1:
        raise ValueError(f"docker pull did not report one registry manifest digest for {ref}")
    return matches[0]


def resolve(root, ref, *, bootstrap=False):
    if bootstrap and os.environ.get("CI") and os.environ.get("CI_PIPELINE_SOURCE") != "merge_request_event":
        raise ValueError("runtime bootstrap is restricted to local and merge request verification")
    pulled = ref
    try:
        digest = pull(ref)
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or "").strip()
        artifact = ref.split("/", 1)[1]
        missing = (error == f"Error response from daemon: unknown: artifact {artifact} not found"
                   or error.endswith(f"manifest for {ref} not found: manifest unknown: manifest unknown")
                   or error.endswith(f"{ref}: not found"))
        if not bootstrap or not missing:
            raise RuntimeError(f"cannot pull {ref}: {error}") from exc
        from ci.base_images.config import bootstrap_reference
        pulled = bootstrap_reference(root, ref)
        if not pulled:
            raise ValueError(f"no verified bootstrap artifact for {ref}") from exc
        digest = pull(pulled)
    repository, version = ref.rsplit(":", 1)
    reference = repository + "@" + digest
    if pulled == ref:
        Harbor(repository).check_version(version, digest)
    if (bootstrap and ref == PREFIX + "/runtime/python312-cpu:1.0.0" and digest != BOOTSTRAP_DIGEST):
        raise ValueError(f"{ref}: registry digest differs from the approved bootstrap release")
    row = inspect_image(pulled)
    if (row["Os"], row["Architecture"]) != ("linux", "amd64"):
        raise ValueError(f"{ref}: expected {PLATFORM}")
    if reference not in (row.get("RepoDigests") or []) or not re.fullmatch(DIGEST, row["Id"]):
        raise ValueError(f"{ref}: pulled registry digest does not match local image")
    return {"reference": reference, "digest": digest, "image_id": row["Id"], "bootstrap": pulled != ref}


def materialize(ref, frozen):
    if (not TAG.fullmatch(ref) or not re.fullmatch(DIGEST,frozen.get('digest','')) or
            frozen.get('reference')!=ref.rsplit(':',1)[0]+'@'+frozen['digest']):
        raise ValueError('invalid frozen foundation reference')
    reference=frozen['reference']
    if pull(reference)!=frozen['digest']:raise ValueError('frozen foundation pull digest mismatch')
    row=inspect_image(reference)
    if (row.get('Os'),row.get('Architecture'))!=('linux','amd64') or reference not in row.get('RepoDigests',[]):
        raise ValueError('frozen foundation platform or registry identity mismatch')
    return {**frozen,'image_id':row['Id'],'bootstrap':False}


def freeze(registry, ref):
    """Read a released base and its receipt without starting a container."""
    from ci.base_images.manage import RECEIPT_TYPE,ANNOTATION,PROJECT
    from ci.registry.handoff import checked_json,inspect_manifest
    if not TAG.fullmatch(ref):raise ValueError('foundation requires an exact version tag')
    repository,version=ref.rsplit(':',1)
    receipt_digest=registry.resolve(repository,'receipt-'+version)
    if receipt_digest is None:raise ValueError(f'missing foundation receipt: {ref}')
    doc=checked_json(registry.fetch(repository,receipt_digest),receipt_digest)
    if doc.get('artifactType')!=RECEIPT_TYPE:raise ValueError('invalid foundation receipt type')
    row=json.loads(doc['annotations'][ANNOTATION])
    if (row.get('repository')!=repository or row.get('version')!=version or row.get('project')!=PROJECT or row.get('branch')!='develop' or
            not re.fullmatch(DIGEST,row.get('digest',''))):
        raise ValueError('foundation receipt identity mismatch')
    if registry.resolve(repository,version)!=row['digest']:raise ValueError('foundation tag and receipt disagree')
    inspect_manifest(registry,repository,row['digest'])
    return {'reference':repository+'@'+row['digest'],'digest':row['digest'],'receipt_digest':receipt_digest}
