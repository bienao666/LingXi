"""Content-addressed JSON objects. Optional S3 uses SigV4, private buckets only."""
import datetime
import hashlib
import hmac
import os
import re
import uuid
from urllib.parse import quote, urlsplit
import httpx
from lingxi.plugin_signing import MAX_ARTIFACT, sha
from .data import settings


def _path(digest):
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Invalid artifact digest')
    return 'artifacts/' + digest + '.json'


def _s3(method, key, body=b''):
    s = settings()
    target = urlsplit(s.s3_endpoint)
    if target.scheme not in ('http', 'https') or not target.netloc or target.path not in ('', '/') or target.query or target.fragment or target.username:
        raise ValueError('S3 endpoint must be a fixed origin')
    if not re.fullmatch('[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', s.s3_bucket):
        raise ValueError('Invalid S3 bucket')
    path = '/' + quote(s.s3_bucket, safe='') + '/' + quote(key, safe='/')
    clock = datetime.datetime.now(datetime.timezone.utc)
    date, stamp = clock.strftime('%Y%m%d'), clock.strftime('%Y%m%dT%H%M%SZ')
    payload = sha(body)
    headers = {'host': target.netloc, 'x-amz-content-sha256': payload, 'x-amz-date': stamp}
    names = ';'.join(sorted(headers))
    canonical = '\n'.join([method, path, '', ''.join(k+':'+headers[k]+'\n' for k in sorted(headers)), names, payload])
    scope = f'{date}/{s.s3_region}/s3/aws4_request'
    string = 'AWS4-HMAC-SHA256\n'+stamp+'\n'+scope+'\n'+sha(canonical.encode())
    def mac(key, value):
        return hmac.new(key, value.encode(), hashlib.sha256).digest()
    key = mac(mac(mac(mac(('AWS4'+s.s3_secret_key).encode(), date), s.s3_region), 's3'), 'aws4_request')
    headers['authorization'] = f'AWS4-HMAC-SHA256 Credential={s.s3_access_key}/{scope}, SignedHeaders={names}, Signature={hmac.new(key,string.encode(),hashlib.sha256).hexdigest()}'
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        with client.stream(method, s.s3_endpoint.rstrip('/')+path, headers=headers, content=body) as r:
            r.raise_for_status()
            result = bytearray()
            for chunk in r.iter_bytes():
                result.extend(chunk)
                if len(result) > MAX_ARTIFACT:
                    raise ValueError('Artifact response exceeds limit')
            return bytes(result)


def get(digest):
    key = _path(digest)
    s = settings()
    if s.s3_endpoint:
        data = _s3('GET', key)
    else:
        path = s.artifact_dir / key
        with path.open('rb') as stream:
            data = stream.read(MAX_ARTIFACT+1)
    if len(data) > MAX_ARTIFACT or sha(data) != digest:
        raise ValueError('Artifact integrity failure')
    return data


def put(digest, data):
    key = _path(digest)
    if len(data) > MAX_ARTIFACT or sha(data) != digest:
        raise ValueError('Artifact integrity failure')
    s = settings()
    if s.s3_endpoint:
        # Same digest means identical content; no mutable artifact names.
        _s3('PUT', key, data)
    else:
        path = s.artifact_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    if get(digest) != data:
        raise ValueError('Artifact verification after storage failed')
