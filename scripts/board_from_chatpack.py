"""Select from already downloaded chat. Never contacts Kick's chat API."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from shorts_pick import find_candidates, to_segments

def build(meta, rows, n=8):
    url = meta['url']
    if not url.startswith('https://kick.com/zingisukan2525/videos/'):
        raise ValueError('Not a Jingisukan archive')
    segments = to_segments(rows, find_candidates(rows, topn=max(n*2,20)), n,70,30)
    if not segments:
        raise ValueError('No candidates in downloaded chat')
    for seg in segments:
        seg['title'] = str(meta.get('title','候補'))[:110]+' · '+str(int(seg['start'])//60)+'分'
    return {'video':url,'platform':'kick','segments':segments}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--dir',default='out');a=ap.parse_args()
    root=Path(a.dir);meta=json.loads((root/'meta.json').read_text(encoding='utf-8'))
    rows=[]
    for line in (root/'chat.jsonl').read_text(encoding='utf-8').splitlines():
        item=json.loads(line);rows.append((float(item['rel']),item['content']))
    req=build(meta,rows);path=root/'board_request.json';path.write_text(json.dumps(req,ensure_ascii=False),encoding='utf-8')
    print('Reused chat rows:',len(rows),'candidates:',len(req['segments']),flush=True)
    subprocess.run([sys.executable,str(Path(__file__).with_name('board_publish.py')),'--request',str(path),'--source-meta',str(root/'meta.json')],check=True)

if __name__=='__main__':main()
