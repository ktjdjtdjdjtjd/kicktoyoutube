"""Dispatch-only preservation: copies existing artifacts, never renders/posts videos."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import zipfile
from hourly_pack import build_pack, candidate_board, probe_video_duration, _validate_pair, segment_entry, build_index
from retention_manifest import file_receipts, verified_manifest

REPO='ktjdjtdjdjtjd/kicktoyoutube'
ALLOWED={37396768744:'f98b7b7e-7274-401a-9f6b-7c48969b76db',37654712775:'ceba1856-2cbd-4e01-a4f9-26263b275e2e'}

def inventory(run):
    pages=[]
    for page in range(1,101):
        raw=subprocess.check_output(['gh','api',f'repos/{REPO}/actions/runs/{run}/artifacts?per_page=100&page={page}'])
        if len(raw)>2_000_000: raise ValueError('oversized metadata')
        value=json.loads(raw);pages.append(value)
        rows=[a for p in pages for a in p['artifacts']]
        if any(p['total_count']!=value['total_count'] for p in pages):raise ValueError('inventory changed')
        if len(rows)==value['total_count']:
            if len({a['id'] for a in rows})!=len(rows):raise ValueError('duplicate IDs')
            return pages
        if not value['artifacts']:break
    raise ValueError('incomplete inventory')

def select(pages,name,run,now=None):
    matches=[a for p in pages for a in p['artifacts'] if a['name']==name]
    if len(matches)!=1:raise ValueError('missing/duplicate source artifact '+name)
    a=matches[0];now=now or datetime.now(timezone.utc)
    if a.get('expired') is not False or a['workflow_run']['id']!=run or a['size_in_bytes']<=0:
        raise ValueError('invalid source artifact')
    expiry=datetime.fromisoformat(a['expires_at'].replace('Z','+00:00'))
    if expiry<=now+timedelta(minutes=10):raise ValueError('source expiry too near')
    if not str(a.get('digest','')).startswith('sha256:') or len(a['digest'])!=71:raise ValueError('missing digest')
    return a

def download(a,dest):
    dest=Path(dest)
    if dest.exists():raise FileExistsError(dest)
    if shutil.disk_usage(dest.parent).free<2*a['size_in_bytes']+2*1024**3:raise ValueError('insufficient runner disk')
    with dest.open('xb') as output:
        child=subprocess.Popen(['gh','api',f'repos/{REPO}/actions/artifacts/{a["id"]}/zip'],stdout=subprocess.PIPE)
        total=0;digest=hashlib.sha256()
        try:
            while chunk:=child.stdout.read(1024*1024):
                total+=len(chunk)
                if total>a['size_in_bytes']:raise ValueError('source ZIP exceeds metadata')
                output.write(chunk);digest.update(chunk)
            if child.wait()!=0:raise ValueError('source download failed')
        except BaseException:
            child.kill();child.wait();raise
    if total!=a['size_in_bytes'] or 'sha256:'+digest.hexdigest()!=a['digest']:raise ValueError('source ZIP digest/size mismatch')

def extract(source,target,expected=None):
    target=Path(target)
    if target.exists():raise FileExistsError(target)
    with zipfile.ZipFile(source) as z:
        rows=z.infolist();names=[r.filename for r in rows if not r.is_dir()]
        if len(set(names))!=len(names) or (expected is not None and set(names)!=set(expected)):raise ValueError('ZIP file set mismatch')
        if sum(r.file_size for r in rows)>shutil.disk_usage(target.parent).free-2*1024**3:raise ValueError('insufficient extract disk')
        for r in rows:
            p=PurePosixPath(r.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in r.filename or ':' in r.filename or stat.S_ISLNK(r.external_attr>>16):raise ValueError('unsafe ZIP path')
        target.mkdir();z.extractall(target)

def plan(run):
    if run not in ALLOWED:raise ValueError('source run not approved')
    pages=inventory(run)
    Path('context').mkdir();Path('out').mkdir()
    for name in ('bridgeclip-index','chatpack'):
        a=select(pages,name,run);download(a,Path(name+'.zip'))
        extract(name+'.zip','source-index' if name=='bridgeclip-index' else 'chat-source',{'bridgeclip-index.json'} if name=='bridgeclip-index' else None)
    idx=json.loads(Path('source-index/bridgeclip-index.json').read_text())
    meta=json.loads(Path('chat-source/meta.json').read_text())
    expected=list(range(len(meta['segments'])))
    if idx.get('runId')!=run or idx['kickId']!=ALLOWED[run] or meta['uuid']!=ALLOWED[run] or meta['slug']!='zingisukan2525' or expected!=list(range(13)) or sorted(s['index'] for s in idx['segments'])!=expected:raise ValueError('source full 13-segment plan mismatch')
    for i in expected:select(pages,f'seg-{i}',run)
    shutil.copyfile('source-index/bridgeclip-index.json','context/source-index.json')
    Path('context/source-inventory.json').write_text(json.dumps(pages))
    Path('context/source-run.json').write_text(json.dumps({'runId':run,'kickId':ALLOWED[run]}))
    # Retain every source chatpack file. No API chat collection or metadata changes.
    shutil.rmtree('out');shutil.copytree('chat-source','out')
    with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('segments='+json.dumps(expected)+'\n')

def segment(run,i):
    if run not in ALLOWED or i not in range(13):raise ValueError('unapproved source/segment')
    stored=json.loads(Path('context/source-inventory.json').read_text())
    old=select(stored,f'seg-{i}',run);live=select(inventory(run),f'seg-{i}',run)
    if any(old.get(k)!=live.get(k) for k in ('id','digest','size_in_bytes','expires_at')):raise ValueError('source inventory changed')
    base=f'seg_{i:03d}';names=[base+'.mp4',base+'-comments.json',base+'-candidates.json']
    download(live,Path('source.zip'));extract('source.zip','segment',names)
    meta=json.loads(Path('out/meta.json').read_text())
    with open('out/chat.jsonl') as f:chat=[json.loads(line) for line in f if line.strip()]
    pack=build_pack(meta,chat,i);actual=probe_video_duration(Path('segment')/names[0])
    if abs(actual-pack['durationSeconds'])>2:raise ValueError('source video duration mismatch')
    pack['durationSeconds']=round(actual,3)
    pack['messages']=[m for m in pack['messages'] if m['seconds']<pack['durationSeconds']]
    board=candidate_board(pack)
    _validate_pair(Path('segment')/names[1],Path('segment')/names[2],pack,board)
    entry=segment_entry(pack,board,i)
    source_index=json.loads(Path('context/source-index.json').read_text())
    matches=[row for row in source_index['segments'] if row['index']==i]
    if source_index.get('runId')!=run or source_index.get('kickId')!=ALLOWED[run] or len(matches)!=1 or any(matches[0].get(k)!=v for k,v in entry.items() if k!='kickId'):raise ValueError('source index/pair mismatch')
    entry['files']=file_receipts([Path('segment')/n for n in names])
    entry['preservedSourceArtifactId']=old['id'];entry['preservedSourceRunId']=run
    Path('receipt').mkdir();Path(f'receipt/{base}-index.json').write_text(json.dumps(entry))

def finalize(run,newrun,attempt):
    meta=json.loads(Path('out/meta.json').read_text())
    entries=[json.loads(p.read_text()) for p in Path('hours').glob('bridgeclip-hour-*/seg_*-index.json')]
    if len(entries)!=13 or any(e.get('preservedSourceRunId')!=run for e in entries):raise ValueError('missing source receipts')
    idx=build_index(meta,entries,newrun,require_all=True)
    result=verified_manifest(idx,meta,inventory(newrun),attempt)
    result['preservedSourceRunId']=run
    Path('bridgeclip-index.json').write_text(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['plan','segment','finalize']);p.add_argument('--source-run',type=int,required=True);p.add_argument('--segment',type=int);p.add_argument('--new-run',type=int);p.add_argument('--attempt',type=int);a=p.parse_args()
    if a.operation=='plan':plan(a.source_run)
    elif a.operation=='segment':segment(a.source_run,a.segment)
    else:finalize(a.source_run,a.new_run,a.attempt)
