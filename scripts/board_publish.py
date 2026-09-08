"""Generate padded, verified previews and append to the private Cloudflare board.

Called only for candidate-board-enabled Actions runs. Ingestion has no permission
to change decisions. No YouTube publishing, repository writes or secret logging.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def identity(video, start, end):
    url = urllib.parse.urlsplit(video)
    if url.scheme != 'https' or url.hostname not in ('kick.com', 'www.twitch.tv'):
        raise ValueError('unsupported source')
    canonical = urllib.parse.urlunsplit(('https', url.hostname, url.path.rstrip('/'), '', ''))
    return hashlib.sha256(f'{canonical}|{start:.3f}|{end:.3f}'.encode()).hexdigest()


def window(start, end):
    if not all(math.isfinite(x) for x in (start, end)) or start < 0 or not 5 <= end-start <= 600:
        raise ValueError('invalid candidate interval')
    return max(0, start-30), end+30


def verify(path, expected):
    info = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
    duration = float(info['format']['duration'])
    if video['codec_name'] != 'h264' or video['pix_fmt'] != 'yuv420p' or audio['codec_name'] != 'aac':
        raise ValueError('codec validation failed')
    if abs(duration-expected) > 1:
        raise ValueError('duration validation failed')
    subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if path.stat().st_size > 11*1024*1024:
        raise ValueError('preview too large')
    return duration


def remote(base, token, cid, payload=None):
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise ValueError('BOARD_URL must be an HTTPS origin')
    url = base.rstrip('/')+'/ingest/'+cid
    # Refuse redirects so a token cannot be forwarded to a different origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        request = urllib.request.Request(url, data=payload, method='PUT' if payload else 'HEAD',
                    headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json',
                             'User-Agent': 'kicktoyoutube-board/1.0'})
        try:
            with opener.open(request, timeout=120) as response:
                return response.status
        except urllib.error.HTTPError as e:
            if not payload and e.code == 404:
                return 404
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f'board HTTP {e.code}') from None
        except urllib.error.URLError:
            pass
        if attempt < 2:
            time.sleep(2**attempt)
    raise RuntimeError('board unavailable after 3 attempts')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--request', default='queue_shorts/request.json')
    ap.add_argument('--out', default='out_board')
    ap.add_argument('--source-meta', help='Existing chatpack meta.json; avoids resolving the VOD again')
    a = ap.parse_args()
    req = json.loads(Path(a.request).read_text(encoding='utf-8'))
    # This board is specifically for Jingisukan, not every monitored channel.
    url = urllib.parse.urlsplit(req['video'])
    if url.hostname != 'kick.com' or not url.path.startswith('/zingisukan2525/videos/'):
        print('board: other channel, skipped')
        return
    base, token = os.environ['BOARD_URL'], os.environ['BOARD_INGEST_TOKEN']
    from shorts_prep import resolve_source
    import kick_api
    kick_api.load_config('config.json')
    source = None
    vod_title = ''
    if a.source_meta:
        meta = json.loads(Path(a.source_meta).read_text(encoding='utf-8'))
        if meta.get('url') != req['video'] or not str(meta.get('source', '')).startswith('https://'):
            raise ValueError('source metadata does not match request')
        source, vod_title = meta['source'], str(meta.get('title', '候補'))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = []
    for seg in req['segments']:
        start, end = float(seg['start']), float(seg['end'])
        cid = identity(req['video'], start, end)
        try:
            if remote(base, token, cid) == 200:
                print(f'board: {cid[:12]} already present')
                continue
            ps, pe = window(start, end)
            if source is None:
                source, vod_title = resolve_source(req['platform'], req['video'])
            dest = out/(cid+'.mp4')
            # Fast source seek followed by re-encoding: complete interval + 30s each side.
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(ps), '-i', source,
                '-t', str(pe-ps), '-map', '0:v:0', '-map', '0:a:0', '-vf', 'scale=320:-2,fps=10',
                '-c:v', 'libx264', '-crf', '38', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '32k', '-ac', '1', '-ar', '24000', '-movflags', '+faststart',
                str(dest)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            verify(dest, pe-ps)
            candidate = {'id': cid, 'video': req['video'], 'start': start, 'end': end,
                         'preview_start': ps, 'preview_end': pe,
                         'title': str(seg.get('title') or f'{vod_title} · {int(start)//60}:{int(start)%60:02d}')[:160]}
            payload = json.dumps({'candidate': candidate, 'mp4': base64.b64encode(dest.read_bytes()).decode()}).encode()
            remote(base, token, cid, payload)
            print(f'board: {cid[:12]} added')
        except Exception as e:
            # Do not print command lines, source CDN query strings or auth data.
            failed.append(cid)
            print(f'board: {cid[:12]} failed ({type(e).__name__})')
    if failed:
        raise SystemExit(f'board: {len(failed)} failed; rerun safely, existing clips will be skipped')


if __name__ == '__main__':
    main()
