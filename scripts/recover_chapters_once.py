"""One approved VOD recovery. No retry, fallback, upload, or normal state save."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

UUID = '5035af28-d0bf-4308-a59c-e169618d8e1e'
VIDEO = 'kTmq4pE5mfo'
REPO = 'ktjdjtdjdjtjd/kicktoyoutube'
MODEL = 'gemini-3.8-flash'
LEDGER = f'.recovery/{UUID}.json'
STATE = f'state/{UUID}.json'
HEAD = 'タイムスタンプ▽'
TAIL = '元配信▽'
MAX_INPUT = 100000
MAX_OUTPUT = 8192
MAX_COST = .10572
FIELDS = ('title', 'categoryId', 'tags', 'defaultLanguage', 'defaultAudioLanguage')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def request_json(url, headers, body=None, method=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP operation failed ({exc.code}); no automatic retry') from None
    except Exception:
        raise RuntimeError('HTTP operation failed; outcome may be unknown; no automatic retry') from None


class Github:
    def __init__(self):
        self.headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                        'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                        'X-GitHub-Api-Version': '2022-11-28'}

    def get(self, path):
        item = request_json(f'https://api.github.com/repos/{REPO}/contents/{path}?ref=main', self.headers)
        require(item.get('type') == 'file', 'Expected remote file')
        return json.loads(base64.b64decode(item['content'])), item['sha']

    def put(self, path, value, sha):
        body = {'message': f'recovery: {UUID[:8]} {path.split("/")[0]} [skip ci]',
                'branch': 'main', 'sha': sha,
                'content': base64.b64encode(json.dumps(value, ensure_ascii=False, indent=2).encode()).decode()}
        return request_json(f'https://api.github.com/repos/{REPO}/contents/{path}', self.headers, body, 'PUT')


def validate_state(st, allow_done=False):
    require(st.get('uuid') == UUID and st.get('status') == 'done' and not st.get('parts'),
            'State binding/status/parts mismatch')
    require(st.get('yt_url') in (f'https://youtu.be/{VIDEO}', f'https://www.youtube.com/watch?v={VIDEO}'),
            'YouTube binding mismatch')
    allowed = ['retryable-transcription-failed'] + (['done'] if allow_done else [])
    require((st.get('chapters') or {}).get('result') in allowed, 'State not approved for recovery')


def validate_ledger(ledger):
    require(ledger.get('uuid') == UUID and ledger.get('video_id') == VIDEO, 'Ledger binding mismatch')
    require(ledger.get('approved_budget_usd') == 10 and ledger.get('model') == MODEL,
            'Missing approved budget/model')
    reserved = ledger.get('reserved_usd')
    attempts = ledger.get('generation_attempts')
    require(type(reserved) in (int, float) and math.isfinite(reserved) and 0 <= reserved <= 10,
            'Invalid ledger reservation')
    require(type(attempts) is int and attempts in (0, 1), 'Invalid generation count')
    require((attempts == 0 and reserved == 0) or (attempts == 1 and reserved == 1),
            'Reservation/attempt mismatch')


def collect_segments(plan, files):
    require(plan.get('uuid') == UUID and plan.get('n_segments') == 13, 'Unexpected plan')
    require(plan.get('yt_url') in (f'https://youtu.be/{VIDEO}', f'https://www.youtube.com/watch?v={VIDEO}'),
            'Plan video mismatch')
    duration = plan.get('duration_s')
    require(type(duration) in (int, float) and math.isfinite(duration) and duration == 33716,
            'Unexpected duration')
    seen, rows = set(), []
    require(len(files) == 13, 'Expected exactly 13 segment artifacts')
    for file in files:
        seg = read_json(file)
        idx = seg.get('idx')
        require(type(idx) is int and 0 <= idx < 13 and idx not in seen and seg.get('uuid') == UUID,
                'Invalid/duplicate segment identity')
        start, end = idx * 2700, min((idx + 1) * 2700, duration)
        require(seg.get('start') == start and seg.get('end') == end, 'Segment bounds mismatch')
        require(isinstance(seg.get('lines'), list), 'Invalid transcript lines')
        for row in seg['lines']:
            require(isinstance(row, list) and len(row) == 2 and type(row[0]) in (int, float)
                    and math.isfinite(row[0]) and start <= row[0] <= end
                    and isinstance(row[1], str) and row[1].strip(), 'Invalid transcript row')
            rows.append(row)
        seen.add(idx)
    require(seen == set(range(13)) and len(rows) >= 20, 'Incomplete/too short transcript')
    return sorted(rows, key=lambda row: row[0])


def prepare(args, gh):
    import chapters
    state, _ = gh.get(STATE)
    validate_state(state)
    ledger, _ = gh.get(LEDGER)
    validate_ledger(ledger)
    require(ledger['generation_attempts'] == 0 and ledger['reserved_usd'] == 0 and ledger.get('phase') == 'ready',
            'Recovery generation already reserved')
    plan = read_json(Path(args.plan_dir) / 'chapters_plan.json')
    require(plan.get('slug') == state.get('slug'), 'Plan channel mismatch')
    lines = collect_segments(plan, sorted(Path(args.seg_dir).rglob('seg_*.json')))
    transcript = chapters.format_transcript(state, lines)
    compact = chapters.bucketize(lines, bucket=30, max_chars=80)
    representative = '\n'.join(f'{int(t)} {text.replace(chr(10), " ").replace(chr(13), " ")}'
                               for t, text in compact)
    prompt = ('Return only JSON: {"chapters":[{"seconds":0,"title":"..."}]}. '
              'Write Japanese chapter titles, maximum 40 characters each. Aim for 15 to 35 chapters '
              'covering the full nine-hour stream, including its later sections. '
              'Use ONLY integer seconds explicitly appearing in the transcript, except zero. '
              'Separate chapters by at least 60 seconds. These are representative excerpts every '
              '30 seconds; omitted text is unknown. Do not invent events or fill missing information. '
              'Each line starts with its actual transcript timestamp in integer seconds.\n' + representative)
    prepared = {'uuid': UUID, 'video_id': VIDEO, 'duration_s': plan['duration_s'],
                'lines': lines, 'prompt': prompt, 'state_hash': digest(state)}
    write_json(Path(args.out_dir) / 'prepared.json', prepared)
    Path(args.out_dir, 'transcript.md').write_text(transcript, encoding='utf-8')


def gemini(action, body=None):
    headers = {'x-goog-api-key': os.environ['GEMINI_API_KEY'], 'Content-Type': 'application/json'}
    return request_json(f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}{action}',
                        headers, body)


def validate_output(payload, prepared):
    import chapters
    require(isinstance(payload, dict) and isinstance(payload.get('chapters'), list), 'Invalid chapter JSON')
    allowed = {int(t) for t, _ in prepared['lines']} | {0}
    raw = []
    previous = -60
    for row in payload['chapters']:
        sec, label = row.get('seconds'), row.get('title')
        require(type(sec) is int and sec in allowed and 0 <= sec <= prepared['duration_s']
                and sec - previous >= 60 and isinstance(label, str) and 0 < len(label) <= 40
                and not any(c in label for c in '\r\n'), 'Invalid chapter timestamp/title')
        raw.append(f'{chapters.hms(sec)} {label}')
        previous = sec
    require(len(raw) >= 3 and payload['chapters'][0]['seconds'] == 0, 'At least three chapters starting at zero required')
    result = chapters.validate_chapters('\n'.join(raw), prepared['duration_s'])
    require(len(result) == len(raw), 'Chapter normalization changed output')
    return result


def generate(args, gh):
    prepared = read_json(Path(args.out_dir) / 'prepared.json')
    require(prepared.get('uuid') == UUID and prepared.get('video_id') == VIDEO, 'Prepared binding mismatch')
    state, _ = gh.get(STATE)
    validate_state(state)
    require(digest(state) == prepared['state_hash'], 'State changed after preparation')
    ledger, sha = gh.get(LEDGER)
    validate_ledger(ledger)
    require(ledger['generation_attempts'] == 0 and ledger['reserved_usd'] == 0 and ledger.get('phase') == 'ready',
            'Second generation prohibited')
    model = gemini('')
    require(model.get('name') == f'models/{MODEL}' and 'generateContent' in model.get('supportedGenerationMethods', []),
            'Pinned model unavailable')
    contents = [{'role': 'user', 'parts': [{'text': prepared['prompt']}]}]
    counted = gemini(':countTokens', {'contents': contents})
    tokens = counted.get('totalTokens')
    require(type(tokens) is int and 0 < tokens <= MAX_INPUT, 'Input token cap exceeded or missing')
    # Optimistic contents SHA is the durable global one-request lock. Never refund.
    ledger.update(reserved_usd=1, generation_attempts=1, phase='generation_reserved',
                  input_tokens=tokens, prepared_hash=digest(prepared), state_hash=prepared['state_hash'],
                  max_output_tokens=MAX_OUTPUT)
    gh.put(LEDGER, ledger, sha)
    response = gemini(':generateContent', {'contents': contents, 'serviceTier': 'standard', 'generationConfig': {
        'maxOutputTokens': MAX_OUTPUT, 'candidateCount': 1, 'responseMimeType': 'application/json',
        'thinkingConfig': {'thinkingLevel': 'low'}}})
    candidates = response.get('candidates', [])
    require(len(candidates) == 1 and candidates[0].get('finishReason') == 'STOP', 'Truncated/blocked generation')
    parts = candidates[0].get('content', {}).get('parts', [])
    require(parts and all('text' in p and not p.get('thought') for p in parts), 'Unexpected response parts')
    result = validate_output(json.loads(''.join(p['text'] for p in parts)), prepared)
    usage = response.get('usageMetadata', {})
    inp = usage.get('promptTokenCount')
    out = usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0)
    require(type(inp) is int and type(out) is int and 0 <= inp <= MAX_INPUT and 0 <= out <= MAX_OUTPUT,
            'Missing/excessive usage accounting')
    receipt = {'chapters': result, 'usage': usage, 'estimated_spent_usd': (inp * .75 + out * 3.75) / 1e6,
               'maximum_generation_usd': MAX_COST, 'prepared_hash': digest(prepared)}
    ledger, sha = gh.get(LEDGER)
    require(ledger.get('prepared_hash') == digest(prepared) and ledger.get('phase') == 'generation_reserved',
            'Ledger changed during generation')
    ledger.update(phase='prepared', generated=receipt)
    gh.put(LEDGER, ledger, sha)
    write_json(Path(args.out_dir) / 'generated.json', receipt)


def youtube_client():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_info(json.loads(os.environ['YT_TOKEN_JSON_WAINAINA']))
    if not creds.valid:
        creds.refresh(Request())
    return build('youtube', 'v3', credentials=creds, cache_discovery=False)


def video_snapshot(yt):
    items = yt.videos().list(part='snippet,status', id=VIDEO).execute(num_retries=0).get('items', [])
    require(len(items) == 1 and items[0].get('id') == VIDEO, 'YouTube video missing/mismatched')
    require(isinstance(items[0].get('etag'), str) and items[0]['etag'], 'YouTube ETag missing')
    return {'snippet': items[0]['snippet'], 'status': items[0]['status'], 'etag': items[0]['etag']}


def description_change(original, chapter_rows):
    import chapters
    require(original.count(HEAD) == 1 and original.count(TAIL) == 1
            and original.index(HEAD) < original.index(TAIL), 'Description markers missing/ambiguous')
    desired = chapters.inject_description(original, chapter_rows)
    require(desired.split(HEAD)[0] == original.split(HEAD)[0]
            and desired.split(TAIL)[1] == original.split(TAIL)[1] and len(desired) <= 5000,
            'Description changes outside approved block')
    return desired


def publish(args, gh):
    state, _ = gh.get(STATE)
    validate_state(state, allow_done=True)
    ledger, sha = gh.get(LEDGER)
    validate_ledger(ledger)
    require(ledger.get('phase') in ('prepared', 'publish_attempted', 'done') and ledger['generation_attempts'] == 1,
            'Generation not completed')
    yt = youtube_client()
    current = video_snapshot(yt)
    if ledger['phase'] == 'prepared':
        validate_state(state)
        require(digest(state) == ledger['state_hash'], 'State changed after generation')
        original = current
        desired = description_change(current['snippet']['description'], ledger['generated']['chapters'])
        ledger.update(phase='publish_attempted', original_video=original, desired_description=desired,
                      state_before_publish=state)
        gh.put(LEDGER, ledger, sha)  # Durable baseline before any YouTube mutation.
        write_json(Path(args.out_dir) / 'youtube-original.json', original)
        require(video_snapshot(yt) == original, 'Concurrent YouTube edit detected')
        snippet = {k: original['snippet'][k] for k in FIELDS if k in original['snippet']}
        require('title' in snippet and 'categoryId' in snippet, 'Required snippet fields missing')
        snippet['description'] = desired
        update = yt.videos().update(part='snippet', body={'id': VIDEO, 'snippet': snippet})
        update.headers['If-Match'] = original['etag']
        update.execute(num_retries=0)
    verified = video_snapshot(yt)
    original = ledger['original_video']
    require(verified['snippet'].get('description') == ledger['desired_description'],
            'Description not verified; no repeated update allowed')
    require(all(verified['snippet'].get(k) == original['snippet'].get(k) for k in FIELDS)
            and verified['status'] == original['status'], 'Protected YouTube fields changed')
    state, state_sha = gh.get(STATE)
    validate_state(state, allow_done=True)
    before = ledger['state_before_publish']
    require({k: v for k, v in state.items() if k != 'chapters'} ==
            {k: v for k, v in before.items() if k != 'chapters'}, 'Concurrent state edit detected')
    if (state.get('chapters') or {}).get('result') == 'done':
        require(state['chapters'].get('recovery_uuid') == UUID
                and state['chapters'].get('description_sha256') ==
                hashlib.sha256(ledger['desired_description'].encode()).hexdigest(),
                'Unrelated chapter success must be preserved')
    if (state.get('chapters') or {}).get('result') != 'done':
        state['chapters'] = {'result': 'done', 'recovery_uuid': UUID,
                             'chapter_count': len(ledger['generated']['chapters']),
                             'description_sha256': hashlib.sha256(ledger['desired_description'].encode()).hexdigest()}
        gh.put(STATE, state, state_sha)
    final_state, _ = gh.get(STATE)
    require(final_state == state, 'State completion not verified')
    ledger, sha = gh.get(LEDGER)
    ledger.update(phase='done', youtube_verified=True, state_verified=True)
    gh.put(LEDGER, ledger, sha)
    write_json(Path(args.out_dir) / 'receipt.json', ledger)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', required=True, choices=['prepare', 'generate', 'publish'])
    parser.add_argument('--plan-dir', default='out')
    parser.add_argument('--seg-dir', default='segs')
    parser.add_argument('--out-dir', default='out/recovery')
    args = parser.parse_args()
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    try:
        globals()[args.mode](args, Github())
    except Exception as exc:
        # Do not expose Google exception URLs, tokens, prompt, or remote response bodies.
        print('Recovery stopped safely: ' + (str(exc) if type(exc) is RuntimeError else type(exc).__name__))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
