"""Bounded idempotent GitLab Release synchronization shared by packages/operators."""
import json
import urllib.error
import urllib.parse
import urllib.request
from ci.registry.gitlab import GitLab
WEB = 'https://gitlab.striding.internal/stridingai/data/data-platform-operators'

class GitLabReleases(GitLab):
    """Only create/fill releases; never move/delete tags or overwrite facts."""

    BEGIN = '<!-- release:start -->'
    END = '<!-- release:end -->'

    def __init__(self, api, token, project, *, token_header='JOB-TOKEN'):
        super().__init__(api, token, project, token_header=token_header)
        if self.project != '395': raise ValueError('unexpected operator GitLab project')
        if token_header not in {'JOB-TOKEN', 'PRIVATE-TOKEN'}: raise ValueError('invalid token header')
        self.token_header = token_header

    def _request(self, method, suffix, payload=None):
        req = urllib.request.Request(
            f'{self.api}/projects/{self.project}/{suffix}', method=method,
            data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
            headers={self.token_header: self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024: raise ValueError('GitLab Release response too large')
                value = json.loads(raw)
                if not isinstance(value, dict): raise ValueError('invalid GitLab Release response')
                return value
        except urllib.error.HTTPError as error:
            if method == 'GET' and error.code == 404: return None
            raise ValueError(f'GitLab Release {method} HTTP {error.code}') from None
        except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError):
            raise ValueError('GitLab Release request unavailable') from None

    def _tag(self, tag, commit, *, required=False):
        row = self._request('GET', 'repository/tags/' + urllib.parse.quote(tag, safe=''))
        if row is None:
            if required: raise ValueError('GitLab tag missing after release write')
        elif row.get('name') != tag or row.get('commit', {}).get('id') != commit:
            raise ValueError('GitLab tag commit conflict; published tags cannot move')
        return row

    @classmethod
    def _check(cls, row, tag, commit, title, block, links, *, legacy_title=None):
        BEGIN, END = cls.BEGIN, cls.END
        if row.get('tag_name') != tag or row.get('commit', {}).get('id') != commit:
            raise ValueError('GitLab Release commit conflict')
        if row.get('name') != title and (legacy_title is None or row.get('name') != legacy_title):
            raise ValueError('GitLab Release title conflict')
        description = row.get('description') or ''
        if not isinstance(description, str): raise ValueError('invalid GitLab Release description')
        if BEGIN in description or END in description:
            legacy_block = block.replace('\n## 安装与来源\n', '\n### 安装与来源\n', 1)
            if (description.count(BEGIN) != 1 or description.count(END) != 1 or
                    not any(expected in description for expected in (block, legacy_block))):
                raise ValueError('GitLab Release managed metadata conflict')
        assets = row.get('assets', {}).get('links')
        if not isinstance(assets, list): raise ValueError('invalid GitLab Release assets')
        for wanted in links:
            matches = [asset for asset in assets if asset.get('name') == wanted['name'] or asset.get('url') == wanted['url']]
            if len(matches) > 1 or any(any(asset.get(key) != value for key, value in wanted.items()) for asset in matches):
                raise ValueError('GitLab Release asset conflict: ' + wanted['name'])
        return description, assets

    def sync(self, record, *, notes='', first_assets=()):
        BEGIN, END = self.BEGIN, self.END
        tag, title, block, links = self.content(record, self.project)
        commit = record['source_commit']
        suffix = 'releases/' + urllib.parse.quote(tag, safe='')
        self._tag(tag, commit)
        current = self._request('GET', suffix)
        for link in first_assets:
            matches=[a for a in (current or {}).get('assets',{}).get('links',[]) if a.get('name')==link['name']]
            if len(matches)>1:raise ValueError('duplicate first delivery asset')
            links.append({k:matches[0][k] for k in ('name','url','link_type')} if matches else link)
        if current is None:
            notes = notes.strip() or '本版本未提供独立变更说明；源码及原发布证据见下方。'
            if BEGIN in notes or END in notes: raise ValueError('release notes contain reserved markers')
            self._request('POST', 'releases', {
                'tag_name': tag, 'ref': commit, 'name': title,
                'tag_message': self.tag_message(record, title),
                'description': notes + '\n\n' + block, 'assets': {'links': links},
            })
            current = self._request('GET', suffix)
            if current is None: raise ValueError('GitLab Release missing after create')
        # Preflight all facts/links before repairing anything. User notes and
        # unrelated assets remain intact; conflicting published facts never move.
        legacy_title = self.legacy_title(record)
        description, assets = self._check(current, tag, commit, title, block, links, legacy_title=legacy_title)
        patch = {}
        if current['name'] != title: patch['name'] = title
        if block not in description:
            legacy_block = block.replace('\n## 安装与来源\n', '\n### 安装与来源\n', 1)
            patch['description'] = description.replace(legacy_block, block) if legacy_block in description else description.rstrip() + '\n\n' + block
        if patch: self._request('PUT', suffix, patch)
        for link in links:
            if not any(asset['name'] == link['name'] for asset in assets):
                self._request('POST', suffix + '/assets/links', link)
        self._tag(tag, commit, required=True)
        saved = self._request('GET', suffix)
        if saved is None: raise ValueError('GitLab Release missing after synchronization')
        description, assets = self._check(saved, tag, commit, title, block, links)
        if block not in description or any(not any(asset.get('name') == link['name'] for asset in assets) for link in links):
            raise ValueError('GitLab Release synchronization incomplete')
        return {'tag_name': tag, 'source_commit': commit,
                'url': WEB + '/-/releases/' + urllib.parse.quote(tag, safe=''), 'assets': links}
