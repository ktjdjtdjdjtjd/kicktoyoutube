"""Private edited-video delivery and version-bound feedback queue.

Feedback is editing data, never shell commands or permission to publish elsewhere.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import urllib.request
import urllib.error

BASE = 'https://zingisukan-selection.kirinuki-dashboard.workers.dev'
SECRETS = pathlib.Path('C:/Users/KeNEe/.claude/secrets')
ROOT = pathlib.Path('C:/Users/KeNEe/work/stream')
QUEUE = ROOT / '_board_edit_queue/queue.json'
MANIFEST = ROOT / '_board_edit_queue/review_versions.json'
sys.path.insert(0, 'C:/Users/KeNEe/.claude/ops')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(path, method='GET', data=None, binary=False):
    ingest = path.startswith('/ingest-')
    secret = (SECRETS / ('zingisukan-selection-ingest_token.txt' if ingest else
                         'zingisukan-selection-auth_pass.txt')).read_text().strip()
    auth = 'Bearer '+secret if ingest else 'Basic '+base64.b64encode(('kick:'+secret).encode()).decode()
    headers = {'Authorization': auth, 'User-Agent': 'kicktoyoutube-board/1.0'}
    if data is not None:
        headers['Content-Type'] = 'video/mp4' if binary else 'application/json'
        if not binary:
            data = json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE+path, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Board HTTP {e.code} on {method} {path}') from None


def listing(path):
    rows, seen = [], set()
    while True:
        data = request(path)
        rows.extend(data['items'])
        cursor = data.get('cursor')
        if not cursor:
            return rows
        if cursor in seen:
            raise ValueError('Repeated cursor')
        seen.add(cursor)
        path = path.split('?')[0]+'?cursor='+urllib.parse.quote(cursor, safe='')


def local_job(candidate):
    jobs = json.loads(QUEUE.read_text(encoding='utf-8-sig'))
    matches = [j for j in jobs.values() if j['candidate_id'] == candidate and j.get('active')]
    if len(matches) != 1:
        raise ValueError('An active, unambiguous local adoption is required')
    return matches[0]


def upload(candidate, video, spec, status):
    import approval_gate as gate
    if not re.fullmatch('[a-f0-9]{64}', candidate):
        raise ValueError('candidate id')
    job = local_job(candidate)
    if status == 'ready' and job.get('status') != 'complete':
        raise ValueError('Unfinished editing jobs must be uploaded as draft')
    project = pathlib.Path(job['project']).resolve()
    video, spec = pathlib.Path(video).resolve(), pathlib.Path(spec).resolve()
    if not project.is_relative_to(ROOT.resolve()) or not all(p.is_relative_to(project) for p in (video, spec)):
        raise ValueError('Files must belong to the adopted project')
    s = json.loads(spec.read_text(encoding='utf-8-sig'))
    duration = sum(v['end']-v['start'] for v in s['segments'])
    title = s['metadata']['title']
    if not isinstance(title, str) or not 1 <= len(title) <= 160:
        raise ValueError('title')
    if not 24 <= video.stat().st_size <= 95*1024*1024:
        raise ValueError('Video exceeds upload size limit; create a verified review encode')
    receipt = json.loads(pathlib.Path(str(video)+'.verified.json').read_text(encoding='utf-8-sig'))
    st = video.stat()
    if not (receipt.get('ok') and receipt.get('full_decode') and receipt.get('size') == st.st_size
            and abs(receipt.get('mtime', -1)-st.st_mtime) < .01):
        raise ValueError('Current full-decode verification required')
    raw = video.read_bytes()
    version = hashlib.sha256(raw).hexdigest()
    metadata = {'title': title, 'duration': duration, 'status': status}
    rec = gate.require_approval(kind='video', action='cloudboard.edit.upload',
        target=candidate+'/'+version, title=title, destination=BASE+'/編集済み',
        details={'file': str(video), 'content_hash': version, 'privacy': 'authenticated', **metadata})
    gate.mark_started(rec['id'])
    try:
        request(f'/ingest-edit/{candidate}/{version}/media', 'PUT', raw, True)
        result = request(f'/ingest-edit/{candidate}/{version}', 'PUT', metadata)
        rows = listing('/api/edits')
        if not any(v['id'] == candidate and v['version'] == version for v in rows):
            raise ValueError('Uploaded version missing from board')
        versions = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {}
        versions[candidate+'/'+version] = {'candidate_id': candidate, 'version': version,
            'project': str(project), 'video': str(video), 'spec': str(spec),
            'preset': str(project/'preset.json') if (project/'preset.json').exists() else str(project/'preset_draft.json'),
            'start': job['start'], 'end': job['end'], **metadata}
        tmp = MANIFEST.with_suffix('.tmp')
        tmp.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(MANIFEST)
        gate.mark_done(rec['id'], {'version': version})
        return {'version': version, 'id': candidate, 'result': result}
    except Exception as e:
        gate.mark_failed(rec['id'], type(e).__name__)
        raise


def feedback():
    versions = json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.exists() else {}
    rows = listing('/api/feedback')
    for row in rows:
        row['local'] = versions.get(row['candidate_id']+'/'+row['version'])
    return rows


def deploy():
    import approval_gate as gate
    folder = pathlib.Path(__file__).resolve().parents[1]/'cloudboard'
    files = sorted([*folder.glob('*.mjs'), folder/'index.html', folder/'wrangler.toml'])
    files = [f for f in files if '.test.' not in f.name]
    digest = hashlib.sha256(b''.join(f.name.encode()+b'\0'+f.read_bytes() for f in files)).hexdigest()
    args = ['node','C:/Users/KeNEe/dev/kirinuki-dashboard/node_modules/wrangler/bin/wrangler.js',
            'deploy','--config',str(folder/'wrangler.toml')]
    import os
    env = dict(os.environ, XDG_CONFIG_HOME=str(SECRETS/'cloudflare-config'),CI='1',WRANGLER_SEND_METRICS='false')
    rec = gate.require_approval(kind='deployment',action='cloudboard.review.deploy',target=digest,
        title='編集済み動画・テキストFB欄の追加',destination=BASE,
        details={'content_hash':digest,'files':[str(f) for f in files],'privacy':'existing authenticated board'})
    gate.mark_started(rec['id'])
    try:
        subprocess.run(args+['--dry-run'],env=env,stdin=subprocess.DEVNULL,timeout=120,check=True)
        subprocess.run(args,env=env,stdin=subprocess.DEVNULL,timeout=180,check=True)
        request('/api/edits');request('/api/feedback')
        gate.mark_done(rec['id'],{'url':BASE})
    except Exception as exc:
        gate.mark_failed(rec['id'],type(exc).__name__);raise
    return {'deployed':BASE}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('sync')
    sub.add_parser('edits')
    sub.add_parser('deploy')
    u = sub.add_parser('upload')
    u.add_argument('candidate');u.add_argument('--video', required=True);u.add_argument('--spec', required=True)
    u.add_argument('--status', choices=['draft','ready'], default='draft')
    t = sub.add_parser('status')
    t.add_argument('id');t.add_argument('--revision', required=True)
    t.add_argument('--status', choices=['processing','completed','blocked'], required=True)
    t.add_argument('--message', default='');t.add_argument('--result-version')
    a = ap.parse_args()
    if a.command == 'sync':
        rows = feedback()
        result = {'pending': [r for r in rows if r['status'] in ('queued','processing')],
                  'blocked': len([r for r in rows if r['status']=='blocked'])}
    elif a.command == 'edits': result = listing('/api/edits')
    elif a.command == 'deploy': result = deploy()
    elif a.command == 'upload': result = upload(a.candidate, a.video, a.spec, a.status)
    else:
        if not re.fullmatch('[a-f0-9]{32}', a.id): raise ValueError('feedback id')
        payload = {'revision': a.revision, 'status': a.status, 'message': a.message}
        if a.result_version: payload['result_version'] = a.result_version
        result = request('/ingest-feedback/'+a.id, 'PUT', payload)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
