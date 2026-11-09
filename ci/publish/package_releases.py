"""GitLab's browsable package releases; Nexus remains the publication authority."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from packaging.utils import canonicalize_name

from ci.publish.package_records import PackageRecords, location, validate_record
from ci.publish.tags import release_title
from ci.registry.gitlab import GitLab

API = 'https://gitlab.striding.internal/api/v4'
WEB = 'https://gitlab.striding.internal/stridingai/data/data-platform-operators'
BEGIN = '<!-- nexus-package-release:start -->'
END = '<!-- nexus-package-release:end -->'


def release_content(record, project):
    validate_record(record, {})
    name, version = canonicalize_name(record['name']), record['version']
    tag = f'packages/{name}/v{version}'
    commit, path = record['source_commit'], record['source_path']
    links = [
        {'name': 'wheel', 'url': record['url'], 'link_type': 'package'},
        {'name': '完整发布记录', 'url': location(name, version), 'link_type': 'other'},
        {'name': '本包源码（tar.gz）', 'url': f'{API}/projects/{project}/repository/archive.tar.gz?' +
         urllib.parse.urlencode({'sha': commit, 'path': path}), 'link_type': 'other'},
    ]
    producer = record['build_producer']
    block = '\n'.join([
        BEGIN, '## 安装与来源', '',
        '需先配置公司 Nexus Python 索引及访问认证。', '',
        f'```sh\npip install {name}=={version}\n```', '',
        f'- 原始构建提交：[{commit}]({WEB}/-/commit/{commit})',
        f'- 包目录：[{path}]({WEB}/-/tree/{commit}/{urllib.parse.quote(path, safe="/")})',
        f'- wheel：`{record["filename"]}`',
        f'- wheel SHA-256：`{record["sha256"]}`',
        f'- 源码摘要：`{record["source_digest"]}`',
        f'- [完整 Nexus 发布记录]({location(name, version)})',
        f'- [原构建作业]({WEB}/-/jobs/{producer["job_id"]})',
        f'- [原构建与测试子流水线]({WEB}/-/pipelines/{producer["pipeline_id"]})',
        f'- [原发布父流水线]({WEB}/-/pipelines/{record["parent_pipeline_id"]})', '',
        '验收入口来自原 Nexus 记录；历史作业日志或产物可能已过期，不据此声明新的测试结果。',
        '“本包源码”仅归档上述包目录；GitLab 默认 Source code 归档包含整个仓库。',
        'GitLab 登记时间不代表原包发布时间。包文件和发布事实以 Nexus 为准。', END,
    ])
    return tag, release_title('package', name, version), block, links


def release_notes(root, record):
    path = Path(root) / 'docs/releases/packages' / canonicalize_name(record['name']) / (record['version'] + '.md')
    return path.read_text().strip() if path.is_file() else '本版本未提供独立变更说明；源码及原发布证据见下方。'


from ci.publish.gitlab_releases import GitLabReleases


class PackageReleases(GitLabReleases):
    BEGIN, END = BEGIN, END
    content = staticmethod(release_content)

    @staticmethod
    def tag_message(record, title):
        return f'{title}\nSource: {record["source_commit"]}\nWheel SHA-256: {record["sha256"]}\nRecord: {location(record["name"], record["version"])}'

    @staticmethod
    def legacy_title(record):
        return f'{canonicalize_name(record["name"])} {record["version"]}'


def backfill(client, store, name, version, *, root=Path('.')):
    """Recover the presentation from Nexus without building or modifying Nexus."""
    from ci.build.dependency_wheels import WheelPin
    from ci.publish.packages import remote_digest
    record = store.read(name, version)
    if record is None: raise ValueError('Nexus package record missing')
    pin = WheelPin(record['name'], record['version'], record['filename'], record['sha256'])
    if remote_digest(pin, credentials_provider=store._credentials) != record['sha256']:
        raise ValueError('Nexus wheel missing or digest mismatch')
    return client.sync(record, notes=release_notes(root, record))


def main():
    parser = argparse.ArgumentParser(description='从 Nexus 补登记共享包 GitLab Release；不构建或发布 wheel')
    parser.add_argument('--package', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--token-file', type=Path, help='本地 GitLab API Token 文件；CI 使用 CI_JOB_TOKEN')
    args = parser.parse_args()
    if args.token_file:
        client = PackageReleases(API, args.token_file.read_text().strip(), '395', token_header='PRIVATE-TOKEN')
    else:
        client = PackageReleases.environment()
    result = backfill(client, PackageRecords(), args.package, args.version, root=Path(__file__).resolve().parents[2])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
