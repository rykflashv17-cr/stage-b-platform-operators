"""Registry tags and human-readable GitLab Release titles."""
import re


PUBLICATION_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)


def release_title(kind, name, version):
    from packaging.version import Version
    labels = {'package': 'Package', 'operator': 'Operator'}
    if kind not in labels or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise ValueError('invalid release identity')
    parsed = Version(version)
    if parsed.is_devrelease:
        stage = 'Dev'
    elif parsed.pre:
        stage = {'a': 'Alpha', 'b': 'Beta', 'rc': 'RC'}[parsed.pre[0]]
    else:
        stage = 'Stable'
    return f'[{labels[kind]}][{stage}] {name} v{version}'


def publication_tag(version, branch, pipeline_id):
    if not re.fullmatch(r"[0-9]+", pipeline_id):
        raise ValueError("invalid GitLab pipeline ID")
    if branch == "main":
        tag = version
    elif branch == "develop":
        tag = f"{version}-dev.{pipeline_id}"
    elif branch == "release":
        tag = f"{version}-rc.{pipeline_id}"
    else:
        raise ValueError(f"unsupported publication branch: {branch}")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError(f"invalid registry tag for {version} on {branch}: {tag}")
    return tag
