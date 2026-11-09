"""Read a trusted publication baseline without publishing or rebuilding images.

Protected pipelines use authenticated OCI reads (errors fail closed). Merge
requests first read the target branch's successful publication job artifacts
using same-project CI_JOB_TOKEN, then try anonymous OCI reads. An unavailable
MR baseline is explicit on stderr and falls back to a complete build.
"""
import argparse
import io
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import yaml

from ci.publish import manifest as pp


def validate_baseline(document, branch, commit, pipeline, project_id, root):
    pp.validate_document(document)
    repository, prefix, line = pp.route_for(branch, document['schemaVersion'])
    parent_commit = document["sourceCommit"]
    pp.require(document["sourceBranch"] == branch
               and document["releaseLine"] == line
               and document["pipeline"]["projectId"] == project_id
               and int(document["pipeline"]["pipelineId"]) < int(pipeline),
               "baseline-untrusted")
    for value in {parent_commit, *(r["sourceCommit"] for r in pp.component_index(document).values())}:
        pp.require(pp._git(root, "cat-file", "-e", value + "^{commit}")
                   and pp._git(root, "merge-base", "--is-ancestor", value, commit), "baseline-untrusted")
    return prefix + f'-p{int(document["pipeline"]["pipelineId"]):020d}-' + parent_commit[:12]


def artifact_baseline(env, branch, commit, pipeline, root):
    """Job artifacts authenticate the successful protected target-branch job."""
    pp.require(env.get("CI_PROJECT_PATH") == pp.PROJECT_PATH
               and pp._IDENTIFIER.fullmatch(env.get("CI_PROJECT_ID", "")) is not None
               and env.get("CI_API_V4_URL") == "https://gitlab.striding.internal/api/v4"
               and bool(env.get("CI_JOB_TOKEN")), "baseline-artifact-unavailable")
    # Download one archive so YAML and receipt cannot come from different jobs.
    url = (env["CI_API_V4_URL"] + "/projects/" + env["CI_PROJECT_ID"]
           + "/jobs/artifacts/" + urllib.parse.quote(branch, safe="")
           + "/download?job=" + ("publication-candidates" if branch != "main" else "publication-versions"))
    request = urllib.request.Request(url, headers={"JOB-TOKEN": env["CI_JOB_TOKEN"]})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None  # Never forward a job token to another origin.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(pp.MAX_PUBLICATION_BYTES * 3 + 1)
    pp.require(len(raw) <= pp.MAX_PUBLICATION_BYTES * 3, "baseline-artifact-too-large")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        def read(name):
            entry = archive.getinfo(name)
            pp.require(entry.file_size <= pp.MAX_PUBLICATION_BYTES, "baseline-artifact-too-large")
            return archive.read(entry)
        publication = read("build/publication/publication.yaml")
        receipt = json.loads(read("build/publication/receipt.json"))
    document = pp.validate_document(yaml.safe_load(publication))
    tag = validate_baseline(document, branch, commit, pipeline, env["CI_PROJECT_ID"], root)
    with tempfile.TemporaryDirectory() as tmp:
        packaged = pp.package_publication(Path(tmp) / "layout", tag, publication)
    pp.require(type(receipt) is dict and receipt.get("ok") is True
               and receipt.get("check") == "operator-publication"
               and receipt.get("tag") == tag
               and receipt.get("sourceCommit") == document["sourceCommit"]
               and receipt.get("pipeline") == document["pipeline"]
               and receipt.get("artifactDigest") == packaged["artifactDigest"]
               and receipt.get("layerDigest") == pp.digest(publication), "baseline-artifact-untrusted")
    return {"parent": {"artifactDigest": packaged["artifactDigest"],
                       "sourceCommit": document["sourceCommit"]},
            "components": pp.component_index(document), "schema_version": document['schemaVersion']}


def registry_baseline(oras, branch, commit, pipeline, project_id, root):
    repository, prefix, line = pp.route_for(branch)
    parent = pp.discover_parent(oras, repository, prefix, line, root, commit, pipeline, include_same_commit=True)
    if parent is not None:
        document = pp.trusted_parent(oras, repository, prefix, line, root, commit,
                                     pipeline, parent, project_id)
        return {"parent": parent, "components": pp.component_index(document), "schema_version": document['schemaVersion']}
    # Before the first v4 snapshot, carry the newest trusted v3 snapshot forward
    # so cumulative history rows survive the schema switch.
    _, legacy_prefix, legacy_line = pp.route_for(branch, 3)
    legacy_parent = pp.discover_parent(oras, repository, legacy_prefix, legacy_line, root, commit,
                                       pipeline, include_same_commit=True)
    if legacy_parent is not None:
        document = pp.trusted_parent(oras, repository, legacy_prefix, legacy_line, root, commit,
                                     pipeline, legacy_parent, project_id)
        return {"parent": legacy_parent, "components": pp.component_index(document), "schema_version": 3}
    # Before the first v3 snapshot, v2 may have replaced a code's older version.
    # Read every trusted v2 snapshot on this exact line, never just the newest.
    if branch == 'release':
        return {"parent": None, "components": {}, "schema_version": 3}
    _, legacy_prefix, legacy_line = pp.route_for(branch, 2)
    pattern = re.compile(re.escape(legacy_prefix) + r'-p([0-9]{20})-([a-f0-9]{12})\Z')
    tags = sorted(tag for tag in oras.repo_tags(repository) if pattern.fullmatch(tag))
    pp.require(len(tags) <= pp.MAX_HISTORY_SNAPSHOTS, 'history-scan-limit')
    components = {}; parent = None
    for tag in tags:
        if int(pattern.fullmatch(tag)[1]) >= int(pipeline):
            continue
        digest = oras.resolve(repository + ':' + tag)
        pp.require(digest is not None, 'history-artifact-missing')
        document, _ = pp.fetch_publication(oras, repository, digest)
        if document['pipeline']['projectId'] != project_id:
            continue
        pp.require(document['schemaVersion'] == 2 and document['sourceBranch'] == branch
                   and document['releaseLine'] == legacy_line, 'history-source-mismatch')
        pp.require(tag == f'{legacy_prefix}-p{int(document["pipeline"]["pipelineId"]):020d}-{document["sourceCommit"][:12]}',
                   'history-tag-mismatch')
        rows = pp.component_index(document)
        if any(not pp._git(root, 'cat-file', '-e', value + '^{commit}') or
               not pp._git(root, 'merge-base', '--is-ancestor', value, commit)
               for value in {document['sourceCommit'], *(r['sourceCommit'] for r in rows.values())}):
            continue
        for key, row in rows.items():
            previous = components.get(key)
            if branch == 'main' and previous is not None:
                pp.require(previous['digest'] == row['digest'], 'historical-version-conflict')
                continue
            components[key] = row
        parent = {'artifactDigest': digest, 'sourceCommit': document['sourceCommit']}
    return {'parent': parent, 'components': components, 'schema_version': 2 if parent else 3}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--registry-config", required=True)
    parser.add_argument("--oras", default="oras")
    parser.add_argument("--output", type=Path, default=Path("build/baseline.json"))
    parser.add_argument("--merge-request", action="store_true")
    args = parser.parse_args(argv)
    pp.require(pp._SHA.fullmatch(args.commit) is not None
               and pp._IDENTIFIER.fullmatch(args.pipeline_id) is not None, "baseline-input-invalid")
    env = dict(os.environ)
    value = None
    if args.merge_request:
        try:
            value = artifact_baseline(env, args.branch, args.commit, args.pipeline_id, args.root)
        except (pp.PublicationError, OSError, ValueError, KeyError, zipfile.BadZipFile, yaml.YAMLError):
            print('{"baseline":"target-job-artifact-unavailable","fallback":"anonymous-oci"}', file=sys.stderr)
    if value is None:
        try:
            with tempfile.TemporaryDirectory() as temporary:
                config = args.registry_config
                if args.merge_request:
                    config = str(Path(temporary) / "anonymous.json")
                    Path(config).write_text('{"auths":{}}')
                oras = pp.Oras(args.oras, config, temporary)
                oras.check_version()
                value = registry_baseline(oras, args.branch, args.commit, args.pipeline_id,
                                          env.get("CI_PROJECT_ID"), args.root)
        except pp.PublicationError:
            if not args.merge_request:
                raise
            print('{"baseline":"unavailable","fallback":"full-build"}', file=sys.stderr)
            value = {"parent": None, "components": {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except pp.PublicationError as error:
        print(json.dumps({"error": error.category}), file=sys.stderr)
        raise SystemExit(2)
