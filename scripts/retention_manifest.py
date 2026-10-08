"""Verify producer receipts against read-only complete GitHub API metadata.

Verification functions have no I/O; CLI performs anonymous public metadata GET.
No video download, upload, deletion or expiry changes. Content verification
is performed by hourly_pack before receipts are created; digest below identifies
the ZIP produced by upload-artifact, not the uncompressed individual files.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

SHA256 = re.compile(r'^sha256:[0-9a-f]{64}$')


def file_receipts(paths):
    receipts = {}
    for source in paths:
        path = Path(source)
        size = path.stat().st_size
        if not path.is_file() or size <= 0 or path.name in receipts:
            raise ValueError('missing, empty or duplicate file')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        if path.stat().st_size != size:
            raise ValueError('file changed during receipt generation')
        receipts[path.name] = {'bytes': size, 'digest': 'sha256:'+digest.hexdigest()}
    return receipts


def fetch_artifact_pages(run_id):
    """Anonymous GET public metadata only. Private/rate-limit errors fail closed.

    This does not load environment tokens, CLI auth, secrets or video bytes.
    """
    if type(run_id) is not int or run_id <= 0:
        raise ValueError('invalid run identity')
    pages = []
    received = 0
    total = None
    for page in range(1, 101):
        url = f'https://api.github.com/repos/ktjdjtdjdjtjd/kicktoyoutube/actions/runs/{run_id}/artifacts?per_page=100&page={page}'
        request = Request(url, headers={'Accept': 'application/vnd.github+json',
                                       'User-Agent': 'BridgeClip-retention-metadata'})
        with urlopen(request, timeout=30) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('metadata page exceeds limit')
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('total_count')) is not int
                or value['total_count'] < 0 or not isinstance(value.get('artifacts'), list)
                or (total is not None and total != value['total_count'])):
            raise ValueError('invalid or changed metadata inventory')
        total = value['total_count']
        pages.append(value)
        received += len(value['artifacts'])
        if received == total:
            return pages
        if received > total or not value['artifacts']:
            break
    raise ValueError('incomplete artifact pagination')


def utc_start(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('missing source start')
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    # Kick's timezone-less API start_time is UTC (existing chat_fetch contract).
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def verified_manifest(index, meta, pages, run_attempt, now=None):
    now = now or datetime.now(timezone.utc)
    if type(run_attempt) is not int or run_attempt < 1:
        raise ValueError('invalid run attempt')
    if not isinstance(pages, list) or not pages:
        raise ValueError('artifact pages required')
    artifacts = []
    total = None
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get('artifacts'), list):
            raise ValueError('invalid API page')
        if type(page.get('total_count')) is not int or page['total_count'] < 0:
            raise ValueError('unknown API total')
        if total is not None and total != page['total_count']:
            raise ValueError('inventory changed during pagination')
        total = page['total_count']
        artifacts.extend(page['artifacts'])
    if len(artifacts) != total or len({a.get('id') for a in artifacts}) != total:
        raise ValueError('incomplete or duplicate API inventory')
    expected = sorted(s['idx'] for s in meta['segments'])
    if (expected != list(range(len(expected))) or not expected or len(expected) > 48
            or sorted(s['index'] for s in index['segments']) != expected):
        raise ValueError('all planned receipts required')
    result = dict(index)
    result.update({'complete': True, 'expectedSegmentIndices': expected,
                   'startedAt': utc_start(meta.get('start_time')),
                   'runAttempt': run_attempt, 'retentionDays': 90})
    segments = []
    expiries = []
    for segment in index['segments']:
        names = {segment.get('videoFile'), segment.get('commentsFile'), segment.get('candidatesFile')}
        files = segment.get('files')
        if (None in names or len(names) != 3 or not isinstance(files, dict) or set(files) != names
                or any(not isinstance(f, dict) or type(f.get('bytes')) is not int or f['bytes'] <= 0
                       or not SHA256.fullmatch(str(f.get('digest', ''))) for f in files.values())):
            raise ValueError('video/comments/candidates file receipts required')
        matches = [a for a in artifacts if a.get('name') == segment['artifactName']]
        if len(matches) != 1:
            raise ValueError('missing or duplicate segment artifact')
        artifact = matches[0]
        if (type(artifact.get('id')) is not int or artifact['id'] < 1
                or type(artifact.get('size_in_bytes')) is not int or artifact['size_in_bytes'] <= 0
                or artifact.get('expired') is not False
                or not SHA256.fullmatch(str(artifact.get('digest', '')))
                or (artifact.get('workflow_run') or {}).get('id') != index['runId']):
            raise ValueError('invalid segment artifact metadata')
        expiry = datetime.fromisoformat(str(artifact.get('expires_at', '')).replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= now:
            raise ValueError('expired or ambiguous segment artifact')
        expiries.append(expiry)
        row = dict(segment)
        row.update({'artifactId': artifact['id'], 'artifactDigest': artifact['digest'],
                    'artifactBytes': artifact['size_in_bytes'],
                    'artifactExpiresAt': artifact['expires_at']})
        segments.append(row)
    result['segments'] = segments
    result['availableUntil'] = min(expiries).isoformat().replace('+00:00', 'Z')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--pages-out', required=True)
    args = parser.parse_args()
    dest = Path(args.pages_out)
    if dest.exists(): raise FileExistsError('metadata output already exists')
    # Retrieval finishes before writing; any HTTP failure leaves no success file.
    pages = fetch_artifact_pages(args.run_id)
    dest.write_text(json.dumps(pages)+'\n', encoding='utf-8')
