"""Versioned package provenance in the existing Nexus Data hosted raw repository."""
import base64
import http.client
import json
import netrc
import os
from pathlib import PurePosixPath
import re
import ssl
from urllib.parse import quote, urlsplit

from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version, InvalidVersion

from ci.registry.gitlab import identifier

HOST='nexus.striding.internal'
PREFIX='https://'+HOST+'/repository/Data/operators/package-releases/'
MAX_RECORD=65536


def location(name, version):
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?',name):
        raise ValueError('invalid package name')
    if not isinstance(version,str):raise ValueError('invalid package version')
    try:version=str(Version(version))
    except InvalidVersion as exc:raise ValueError('invalid package version') from exc
    return PREFIX+canonicalize_name(name)+'/'+quote(version,safe='')+'/release.json'


def validate_record(record, expected):
    if not isinstance(record,dict):raise ValueError('invalid package provenance')
    try:
        location(record['name'],record['version'])
        name,version,_,_=parse_wheel_filename(record['filename'])
        if name!=canonicalize_name(record['name']) or version!=Version(record['version']):
            raise ValueError('package wheel identity mismatch')
        for key,size in [('source_commit',40),('source_digest',64),('sha256',64)]:
            if not isinstance(record[key],str) or not re.fullmatch(r'[a-f0-9]{'+str(size)+'}',record[key]):
                raise ValueError('invalid package '+key)
        path=PurePosixPath(record['source_path'])
        if path.is_absolute() or '..' in path.parts or len(path.parts)<2 or path.parts[0]!='packages':
            raise ValueError('invalid package source path')
        from ci.build.dependency_wheels import WheelPin
        from ci.publish.packages import wheel_url
        pin=WheelPin(record['name'],record['version'],record['filename'],record['sha256'])
        if record['url']!=wheel_url(pin):raise ValueError('invalid Nexus package URL')
        producer=record['build_producer']
        if producer['job']!='build-packages':raise ValueError('invalid package build producer')
        identifier(producer['job_id']);identifier(producer['pipeline_id']);identifier(record['parent_pipeline_id'])
    except (KeyError,TypeError) as exc:
        raise ValueError('incomplete package provenance') from exc
    if 'name' in expected and canonicalize_name(record['name'])!=canonicalize_name(expected['name']):
        raise ValueError('package record conflict: name')
    for key in ('version','filename','source_path','source_digest','sha256'):
        if key in expected and record[key]!=expected[key]:raise ValueError('package record conflict: '+key)
    return record


class PackageRecords:
    def __init__(self,*,writable=False):self.writable=writable

    def _credentials(self):
        if self.writable:
            from ci.publish.packages import credentials
            return credentials()
        username,password=os.environ.get('NEXUS_READ_USERNAME'),os.environ.get('NEXUS_READ_PASSWORD')
        if username and password:return username,password
        if username or password:raise ValueError('incomplete Nexus read credentials')
        try:auth=netrc.netrc(os.environ.get('NETRC')).authenticators(HOST)
        except (OSError,netrc.NetrcParseError):auth=None
        if not auth:raise ValueError('Nexus read credentials are missing')
        return auth[0],auth[2]

    def _request(self,method,url,body=None):
        username,password=self._credentials()
        auth=base64.b64encode(f'{username}:{password}'.encode()).decode()
        conn=http.client.HTTPSConnection(HOST,timeout=30,context=ssl.create_default_context())
        try:
            conn.request(method,urlsplit(url).path,body=body,
                         headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'})
            response=conn.getresponse()
            if method=='GET' and response.status==404:return None
            if response.status not in ({200} if method=='GET' else {200,201,204}):
                raise ValueError(f'Nexus package record {method} returned HTTP {response.status}')
            raw=response.read(MAX_RECORD+1)
            if len(raw)>MAX_RECORD:raise ValueError('Nexus package record exceeds metadata bound')
            return raw
        except (OSError,http.client.HTTPException):
            raise ValueError('Nexus package record request failed') from None
        finally:conn.close()

    def read(self,name,version):
        raw=self._request('GET',location(name,version))
        if raw is None:return None
        try:row=json.loads(raw)
        except (ValueError,UnicodeError):raise ValueError('invalid Nexus package record JSON') from None
        return validate_record(row,{'name':name,'version':version})

    def create(self,record):
        if not self.writable:raise ValueError('Nexus package record client is read only')
        validate_record(record,{})
        raw=(json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
        if len(raw)>MAX_RECORD:raise ValueError('package record exceeds metadata bound')
        existing=self.read(record['name'],record['version'])
        if existing is not None:return validate_record(existing,record)
        try:
            self._request('PUT',location(record['name'],record['version']),raw)
        except ValueError:
            existing=self.read(record['name'],record['version'])
            if existing is None:raise
            return validate_record(existing,record)
        return validate_record(self.read(record['name'],record['version']),record)
