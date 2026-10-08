"""Fixed approved source ZIP preservation; no extraction or video/API rendering."""
from datetime import datetime,timezone,timedelta
import hashlib,json,re,shutil,subprocess
from pathlib import Path
REPO='ktjdjtdjdjtjd/kicktoyoutube'
RUN=37725327068
ARTIFACT=11530498847
BYTES=95936109
DIGEST='sha256:6ec7623ea53d5a089008fef0dd631393d99eb19b65989efd6395856cabee9160'
def validate(value,now=None):
    if value.get('id')!=ARTIFACT or value.get('name')!='chatpack' or value.get('workflow_run',{}).get('id')!=RUN or value.get('expired') is not False or value.get('size_in_bytes')!=BYTES or value.get('digest')!=DIGEST:
        raise ValueError('Source identity/bytes/digest changed')
    expiry=datetime.fromisoformat(value['expires_at'].replace('Z','+00:00'))
    if expiry.tzinfo is None or expiry<=(now or datetime.now(timezone.utc))+timedelta(minutes=10):raise ValueError('Source expiry too near')
    return value

def verify_stream(source,output):
    total=0;digest=hashlib.sha256()
    while chunk:=source.read(1024*1024):
        total+=len(chunk)
        if total>BYTES:raise ValueError('Oversized source ZIP')
        output.write(chunk);digest.update(chunk)
    if total!=BYTES or 'sha256:'+digest.hexdigest()!=DIGEST:raise ValueError('Source ZIP bytes/digest mismatch')

def main():
    raw=subprocess.check_output(['gh','api',f'repos/{REPO}/actions/artifacts/{ARTIFACT}'])
    if len(raw)>2*1024**2:raise ValueError('Oversized metadata')
    value=validate(json.loads(raw));root=Path('preserved-chatpack')
    if root.exists():raise FileExistsError('New destination required')
    if shutil.disk_usage('.').free<2*BYTES+2*1024**3:raise ValueError('Insufficient runner disk')
    root.mkdir()
    with (root/'source-chatpack.zip').open('xb') as output:
        child=subprocess.Popen(['gh','api',f'repos/{REPO}/actions/artifacts/{ARTIFACT}/zip'],stdout=subprocess.PIPE)
        try:
            verify_stream(child.stdout,output)
            if child.wait()!=0:raise ValueError('Source download failed')
        except BaseException:child.kill();child.wait();raise
    (root/'provenance.json').write_text(json.dumps({'sourceArtifact':value,'verifiedInnerZIPBytes':BYTES,'verifiedInnerZIPDigest':DIGEST,'sourceKickID':'3f379880-205c-4f5b-ad79-700cfb098703','sourceStartedAtUTC':'2026-10-03T12:01:42Z','copiedAtUTC':datetime.now(timezone.utc).isoformat(),'originalZIPUnchanged':True},indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
