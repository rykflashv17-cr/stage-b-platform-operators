"""Bounded, read-only Job Token client. Never select an older successful retry."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def identifier(value):
    value = str(value)
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('invalid GitLab identifier')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitLab:
    def __init__(self, api, token, project, *, token_header="JOB-TOKEN"):
        parsed = urllib.parse.urlsplit(api)
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid GitLab API origin')
        if not token:
            raise ValueError('missing CI_JOB_TOKEN')
        if token_header not in {'JOB-TOKEN','PRIVATE-TOKEN'}:raise ValueError('invalid token header')
        self.token_header=token_header
        self.api, self.token, self.project = api.rstrip('/'), token, identifier(project)
        self.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def environment(cls, env=None):
        env = os.environ if env is None else env
        if env.get('CI_API_V4_URL') != 'https://gitlab.striding.internal/api/v4':
            raise ValueError('unexpected GitLab API origin')
        return cls(env['CI_API_V4_URL'], env.get('CI_JOB_TOKEN'), env['CI_PROJECT_ID'])

    def get(self, path, *, raw=False):
        if not path.startswith('/') or path.startswith('//') or '..' in path.split('/'):
            raise ValueError('invalid GitLab API path')
        req = urllib.request.Request(self.api + path, headers={self.token_header: self.token})
        try:
            with self.opener.open(req, timeout=30) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError('GitLab metadata response too large')
                return (body if raw else json.loads(body)), response.headers
        except urllib.error.HTTPError as error:
            raise ValueError(f'GitLab metadata HTTP {error.code}: {path.split("?")[0]}') from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ValueError(f'GitLab metadata unavailable: {type(error).__name__}') from None

    def _object(self, suffix, expected):
        row, _ = self.get(f'/projects/{self.project}/{suffix}')
        if not isinstance(row, dict) or str(row.get('id')) != identifier(expected):
            raise ValueError('GitLab metadata identity missing or mismatched')
        return row

    def pipeline(self, pipeline):
        row = self._object(f'pipelines/{identifier(pipeline)}', pipeline)
        if str(row.get('project_id')) != self.project:
            raise ValueError('GitLab pipeline belongs to another project')
        return row

    def current_job(self):
        row, _ = self.get('/job')
        if not isinstance(row, dict) or not isinstance(row.get('pipeline'), dict):
            raise ValueError('GitLab current job metadata missing')
        return row

    def _pages(self, path):
        rows, seen, total = [], set(), None
        for page in range(1, 101):
            batch, headers = self.get(f'{path}?include_retried=true&per_page=100&page={page}')
            if not isinstance(batch, list):
                raise ValueError('invalid GitLab metadata page')
            advertised = headers.get('X-Total')
            if advertised is not None:
                if not advertised.isdigit() or (total is not None and int(advertised) != total):
                    raise ValueError('GitLab pagination changed during read')
                total = int(advertised)
            for row in batch:
                if not isinstance(row, dict) or 'id' not in row or row['id'] in seen:
                    raise ValueError('GitLab pagination has missing or duplicate identities')
                identifier(row['id']); seen.add(row['id']); rows.append(row)
            following = headers.get('X-Next-Page')
            if following == '':
                if total is not None and len(rows) != total:
                    raise ValueError('GitLab pagination incomplete')
                return rows
            if following != str(page + 1):
                raise ValueError('GitLab pagination continuation missing or invalid')
        raise ValueError('GitLab pagination exceeds bounded read')

    def jobs(self, pipeline):
        pipeline = identifier(pipeline)
        rows = self._pages(f'/projects/{self.project}/pipelines/{pipeline}/jobs')
        for row in rows:
            if (str(row.get('pipeline', {}).get('id')) != pipeline or
                    str(row.get('pipeline', {}).get('project_id')) != self.project or
                    not isinstance(row.get('name'), str) or not isinstance(row.get('status'), str)):
                raise ValueError('GitLab job metadata identity missing or mismatched')
        return rows

    def successful_job(self, pipeline, name):
        attempts = [row for row in self.jobs(pipeline) if row['name'] == name]
        if not attempts:
            raise ValueError(f'required GitLab job missing: {name}')
        latest = max(attempts, key=lambda row: int(row['id']))
        if latest['status'] != 'success':
            raise ValueError(f'latest GitLab attempt is {latest["status"]}: {name}')
        return latest

    def first_child(self, parent, child, commit, *, trigger='run-jobs'):
        parent, child = identifier(parent), identifier(child)
        rows = self._pages(f'/projects/{self.project}/pipelines/{parent}/trigger_jobs')
        attempts = [row for row in rows if row.get('name') == trigger and row.get('downstream_pipeline')]
        if not attempts:
            raise ValueError('cannot verify first child pipeline')
        for row in attempts:
            upstream, downstream = row.get('pipeline', {}), row['downstream_pipeline']
            if (str(upstream.get('id')) != parent or str(upstream.get('project_id')) != self.project or
                    str(downstream.get('project_id')) != self.project or downstream.get('sha') != commit):
                raise ValueError('untrusted child pipeline relationship')
        first = min(attempts, key=lambda row: int(row['id']))
        if str(first['downstream_pipeline']['id']) != child:
            raise ValueError('recreated child rejected; start a new parent pipeline')
        return child

    def artifact(self, job, path):
        if path.startswith('/') or '..' in path.split('/'):
            raise ValueError('invalid artifact path')
        body, _ = self.get(f'/projects/{self.project}/jobs/{identifier(job)}/artifacts/' + urllib.parse.quote(path, safe='/'), raw=True)
        return body
