import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {handle} from './api.mjs';
import {makeSession} from './login.mjs';
import {MAX_MEDIA} from './review.mjs';

const origin='https://board.invalid',id='a'.repeat(64),other='b'.repeat(64),fbid='c'.repeat(32);
const video=Buffer.concat([Buffer.from([0,0,0,24]),Buffer.from('ftypisom0000000000000')]);
const sha=b=>createHash('sha256').update(b).digest('hex'),version=sha(video);
const ek=(i=id,v=version)=>`edits/${i}/${v}.json`;
const mk=(i=id,v=version)=>`edited-media/${i}/${v}.mp4`;
const fk=f=>`feedback/${f}.json`;
class Bucket {
  data=new Map();writes=[];gets=[];beforePut=null;
  async head(key){const o=this.data.get(key);return o?{size:o.buf.length,etag:o.etag}:null;}
  async get(key,options={}){
    this.gets.push({key,options});const o=this.data.get(key);if(!o)return null;
    const range=options.range,body=range?o.buf.subarray(range.offset,range.offset+range.length):o.buf;
    return {size:o.buf.length,etag:o.etag,body,json:async()=>JSON.parse(o.buf.toString())};
  }
  async put(key,value,options={}){
    this.writes.push({key,options});if(this.beforePut)await this.beforePut(key,options);
    const old=this.data.get(key),condition=options.onlyIf;
    if(condition?.etagDoesNotMatch==='*'&&old||condition?.etagMatches&&condition.etagMatches!==old?.etag)return null;
    const buf=Buffer.from(value);
    if(options.sha256&&sha(buf)!==options.sha256)throw Error('checksum');
    const o={buf,etag:createHash('md5').update(buf).digest('hex')};this.data.set(key,o);return this.head(key);
  }
  async list({prefix,limit,cursor}){
    assert.equal(limit,20);const keys=[...this.data.keys()].filter(k=>k.startsWith(prefix)).sort(),start=Number(cursor||0);
    return {objects:keys.slice(start,start+limit).map(key=>({key})),truncated:start+limit<keys.length,cursor:String(start+limit)};
  }
}
const env=()=>({BOARD:new Bucket(),AUTH_PASS:'browser-test',INGEST_TOKEN:'writer-test'});
function req(path,method='GET',data,role='browser',headers={}){
  return new Request(origin+path,{method,headers:{Authorization:role==='writer'?'Bearer writer-test':role==='browser'?'Basic '+btoa('kick:browser-test'):'',Origin:origin,'Content-Type':Buffer.isBuffer(data)?'video/mp4':'application/json',...headers},body:data===undefined?undefined:Buffer.isBuffer(data)?data:JSON.stringify(data)});
}
async function seed(e,i=id){await e.BOARD.put('candidates/'+i,JSON.stringify({id:i}));}
const upload=(e,b=video,i=id,v=sha(b),headers={})=>handle(req(`/ingest-edit/${i}/${v}/media`,'PUT',b,'writer',headers),e);
const meta=(e,i=id,v=version,data={title:'edit',duration:12,status:'ready'})=>handle(req(`/ingest-edit/${i}/${v}`,'PUT',data,'writer'),e);
async function ready(e,i=id,b=video){await seed(e,i);assert.equal((await upload(e,b,i)).status,200);assert.equal((await meta(e,i,sha(b))).status,200);}
const post=(e,text='fix <script>alert(1)</script>',f=fbid,i=id,v=version)=>handle(req(`/feedback/${i}/${v}/${f}`,'POST',{text}),e);
const update=(e,data,f=fbid)=>handle(req('/ingest-feedback/'+f,'PUT',data,'writer'),e);

test('review authentication is private, Bearer-only ingest; browser writes require Origin',async()=>{
  const e=env();await ready(e);const session=await makeSession(e.AUTH_PASS);
  for(const path of ['/api/edits','/api/feedback',`/edited-media/${id}/${version}`]){
    assert.equal((await handle(req(path,'GET',undefined,'none'),e)).status,401);
    assert.equal((await handle(req(path,'GET',undefined,'writer'),e)).status,401);
    assert.equal((await handle(new Request(origin+path,{headers:{Cookie:'clip_session='+session}}),e)).status,200);
  }
  assert.equal((await handle(req(`/edited-media/${id}/${version}`,'HEAD',undefined,'none'),e)).status,401);
  for(const path of [`/ingest-edit/${id}/${version}`,`/ingest-edit/${id}/${version}/media`,`/ingest-feedback/${fbid}`]){
    assert.equal((await handle(req(path,'PUT',{}),e)).status,401);
    assert.equal((await handle(new Request(origin+path,{method:'PUT',headers:{Cookie:'clip_session='+session}}),e)).status,401);
    assert.equal((await handle(req(path,'PUT',{},'writer'),{...e,INGEST_TOKEN:''})).status,503);
  }
  for(const site of ['https://evil.invalid','null',''])assert.equal((await handle(req(`/feedback/${id}/${version}/${fbid}`,'POST',{text:'a'},'browser',{Origin:site}),e)).status,403);
  const missing=req(`/feedback/${id}/${version}/${fbid}`,'POST',{text:'a'});missing.headers.delete('Origin');assert.equal((await handle(missing,e)).status,403);
  const bearer=req(`/ingest-edit/${id}/${version}/media`,'PUT',video,'writer');bearer.headers.delete('Origin');assert.equal((await handle(bearer,{...e,AUTH_PASS:''})).status,200);
  assert.equal((await handle(req('/api/edits'),{...e,AUTH_PASS:''})).status,503);
});

test('media validates candidate, ftyp, size, content type, length and SHA before immutable put',async()=>{
  const e=env();assert.equal((await upload(e)).status,404);await seed(e);
  for(const b of [Buffer.alloc(23),Buffer.alloc(24),Buffer.concat([Buffer.from([0,0,0,255]),video.subarray(4)])])assert.equal((await upload(e,b)).status,400);
  assert.equal((await upload(e,video,id,'d'.repeat(64))).status,400);
  assert.equal((await upload(e,video,id,version,{'Content-Type':'application/json'})).status,400);
  assert.equal((await upload(e,video,id,version,{'Content-Length':String(MAX_MEDIA+1)})).status,413);
  for(const n of ['-1','1','30','abc'])assert.equal((await upload(e,video,id,version,{'Content-Length':n})).status,400);
  assert.equal(e.BOARD.data.has(mk()),false);
  assert.equal((await upload(e,video,id,version,{'Content-Length':String(video.length)})).status,200);
  const first=e.BOARD.data.get(mk());assert.equal((await (await upload(e)).json()).duplicate,true);assert.equal(e.BOARD.data.get(mk()),first);
  const writes=e.BOARD.writes.filter(w=>w.key===mk());assert.ok(writes.every(w=>w.options.onlyIf.etagDoesNotMatch==='*'&&w.options.sha256===version));
  assert.equal((await handle(req(`/edited-media/${id}/${version}`),e)).status,404);
});

test('streamed media enforces actual maximum, accepts exact 95 MiB',async()=>{
  const e=env();await seed(e);
  // Exercise the production incremental hashing path without requiring Workers in Node.
  const previous=crypto.DigestStream;
  crypto.DigestStream=class {
    constructor(){let resolve,reject;this.digest=new Promise((a,b)=>{resolve=a;reject=b;});const hash=createHash('sha256');this.stream=new WritableStream({write:c=>{hash.update(c);},close:()=>resolve(hash.digest()),abort:reject});}
    getWriter(){return this.stream.getWriter();}
  };
  try{
    const block=new Uint8Array(1024*1024),hash=createHash('sha256');hash.update(video);
    let remaining=MAX_MEDIA-video.length;while(remaining){const n=Math.min(remaining,block.length);hash.update(block.subarray(0,n));remaining-=n;}
    const v=hash.digest('hex');
    async function streamed(extra){let sent=0;const body=new ReadableStream({pull(c){if(sent===0){c.enqueue(video);sent+=video.length;return;}const n=Math.min(block.length,MAX_MEDIA+extra-sent);if(n>0){c.enqueue(block.subarray(0,n));sent+=n;}else c.close();}});
      return handle(new Request(`${origin}/ingest-edit/${id}/${v}/media`,{method:'PUT',duplex:'half',headers:{Authorization:'Bearer writer-test','Content-Type':'video/mp4'},body}),e);}
    assert.equal((await streamed(1)).status,413);assert.equal(e.BOARD.data.has(mk(id,v)),false);
    assert.equal((await streamed(0)).status,200);assert.equal((await e.BOARD.head(mk(id,v))).size,MAX_MEDIA);
  }finally{if(previous===undefined)delete crypto.DigestStream;else crypto.DigestStream=previous;}
});

test('metadata requires media; all versions paginate and retry never changes timestamps',async()=>{
  const e=env();await seed(e);assert.equal((await meta(e)).status,404);await upload(e);
  for(const data of [null,[],{title:'x',duration:0,status:'ready'},{title:'x',duration:1,status:'public'},{title:'x'.repeat(161),duration:1,status:'draft'}])assert.equal((await meta(e,id,version,data)).status,400);
  const first=await (await meta(e)).json();assert.match(first.created_at,/^\d{4}-/);
  assert.equal((await (await meta(e)).json()).created_at,first.created_at);
  const changed=await (await meta(e,id,version,{title:'different',duration:12,status:'draft'})).json();assert.equal(changed.status,'draft');assert.equal(changed.created_at,first.created_at);
  assert.equal((await meta(e)).status,200);
  for(let n=0;n<41;n++){const b=Buffer.concat([video,Buffer.from([n])]);await upload(e,b);assert.equal((await meta(e,id,sha(b),{title:String(n),duration:1,status:'draft'})).status,200);}
  let cursor=null,items=[];do{const page=await (await handle(req('/api/edits'+(cursor?'?cursor='+cursor:'')),e)).json();items.push(...page.items);cursor=page.cursor;}while(cursor);
  assert.equal(items.length,42);assert.equal(new Set(items.map(x=>x.version)).size,42);assert.ok(items.every(x=>x.id===id));
});

test('private full/HEAD and closed/open/suffix ranges, invalid ranges and missing media',async()=>{
  const e=env();await ready(e);const path=`/edited-media/${id}/${version}`;
  const full=await handle(req(path),e);assert.equal(full.status,200);assert.deepEqual(Buffer.from(await full.arrayBuffer()),video);assert.equal(full.headers.get('Cache-Control'),'private, no-store');
  const before=e.BOARD.gets.length,head=await handle(req(path,'HEAD'),e);assert.equal(head.headers.get('Content-Length'),String(video.length));assert.equal(await head.text(),'');assert.equal(e.BOARD.gets.length,before);
  for(const [r,start,n] of [['bytes=0-7',0,8],['bytes=8-',8,video.length-8],['bytes=-8',video.length-8,8],['bytes=0-999',0,video.length],['bytes=-999',0,video.length]]){
    for(const method of ['GET','HEAD']){const res=await handle(req(path,method,undefined,'browser',{Range:r}),e);assert.equal(res.status,206);assert.equal(res.headers.get('Content-Range'),`bytes ${start}-${start+n-1}/${video.length}`);assert.equal((await res.arrayBuffer()).byteLength,method==='HEAD'?0:n);}
  }
  for(const r of ['bytes=999-','bytes=8-2','bytes=-0','bytes=-','bytes=0-1,3-4','words=0-1','bytes=9007199254740992-']){const res=await handle(req(path,'GET',undefined,'browser',{Range:r}),e);assert.equal(res.status,416);assert.equal(res.headers.get('Content-Range'),`bytes */${video.length}`);}
  e.BOARD.data.delete(mk());assert.equal((await handle(req(path),e)).status,404);
});

test('feedback immutable identity, text bounds, idempotence after processing and base association',async()=>{
  const e=env();assert.equal((await post(e)).status,404);await ready(e);
  for(const text of ['',null,1,'x'.repeat(4001)])assert.equal((await post(e,text)).status,400);
  const a=await (await post(e)).json();assert.equal(a.text,'fix <script>alert(1)</script>');assert.equal(a.status,'queued');assert.equal(a.created_at,a.updated_at);
  assert.equal((await (await post(e)).json()).duplicate,true);assert.equal((await post(e,'different')).status,409);
  await ready(e,other);assert.equal((await post(e,a.text,fbid,other)).status,409);
  const processing=await (await update(e,{revision:a.revision,status:'processing'})).json();assert.equal(processing.status,'processing');
  const retry=await (await post(e)).json();assert.equal(retry.status,'processing');assert.equal(retry.created_at,a.created_at);
  assert.equal((await post(e,'x'.repeat(4000),'d'.repeat(32))).status,200);
  assert.ok(e.BOARD.writes.filter(w=>w.key===fk(fbid)&&!w.options.onlyIf.etagMatches).every(w=>w.options.onlyIf.etagDoesNotMatch==='*'));
});

test('feedback CAS transitions, stale/racing writers and result candidate association',async()=>{
  const e=env();await ready(e);const a=await (await post(e)).json();
  assert.equal((await update(e,{revision:a.revision,status:'completed',result_version:version})).status,409);
  assert.equal((await update(e,{revision:a.revision,status:'blocked'})).status,409);
  const p=await (await update(e,{revision:a.revision,status:'processing',message:'working'})).json();
  assert.equal((await update(e,{revision:a.revision,status:'processing'})).status,409);
  const p2=await (await update(e,{revision:p.revision,status:'processing',message:'still working'})).json();assert.equal(p2.status,'processing');
  assert.equal((await update(e,{revision:p2.revision,status:'completed'})).status,400);
  assert.equal((await update(e,{revision:p2.revision,status:'completed',result_version:'f'.repeat(64)})).status,404);
  const b=Buffer.concat([video,Buffer.from('result')]),v=sha(b);await ready(e,other,b);
  assert.equal((await update(e,{revision:p2.revision,status:'completed',result_version:v})).status,404);
  await upload(e,b);await meta(e,id,v);
  const done=await (await update(e,{revision:p2.revision,status:'completed',result_version:v})).json();assert.equal(done.status,'completed');assert.equal(done.candidate_id,id);
  assert.equal((await update(e,{revision:done.revision,status:'processing'})).status,409);
  assert.equal((await update(e,{revision:done.revision,status:'completed'})).status,200);
  const fid='e'.repeat(32),q=await (await post(e,'another',fid)).json();
  const responses=await Promise.all([update(e,{revision:q.revision,status:'processing',message:'worker1'},fid),update(e,{revision:q.revision,status:'processing',message:'worker2'},fid)]);
  assert.deepEqual(responses.map(r=>r.status).sort(),[200,409]);
  const current=await e.BOARD.get(fk(fid));const blocked=await (await update(e,{revision:current.etag,status:'blocked'},fid)).json();assert.equal(blocked.status,'blocked');
  assert.equal((await update(e,{revision:blocked.revision,status:'completed',result_version:v},fid)).status,409);
  assert.equal((await update(e,{revision:blocked.revision,status:'blocked'},fid)).status,200);
  assert.ok(e.BOARD.writes.filter(w=>w.key===fk(fid)&&w.options.onlyIf.etagMatches).length>=3);
});

test('feedback pagination exposes etag revisions, no metadata-only race bypass',async()=>{
  const e=env();await ready(e);
  for(let n=0;n<43;n++)assert.equal((await post(e,String(n),n.toString(16).padStart(32,'0'))).status,200);
  let cursor=null,items=[];do{const page=await (await handle(req('/api/feedback'+(cursor?'?cursor='+cursor:'')),e)).json();items.push(...page.items);cursor=page.cursor;}while(cursor);
  assert.equal(items.length,43);for(const item of items)assert.equal(item.revision,(await e.BOARD.head(fk(item.id))).etag);
  const item=items[0];e.BOARD.beforePut=async(key,opts)=>{if(key===fk(item.id)&&opts.onlyIf.etagMatches){e.BOARD.beforePut=null;await e.BOARD.put(key,JSON.stringify({...item,status:'processing',message:'raced'}));}};
  assert.equal((await update(e,{revision:item.revision,status:'processing'},item.id)).status,409);
});

test('invalid IDs/paths, methods, malformed and oversized JSON never write',async()=>{
  const e=env();await ready(e);const count=e.BOARD.writes.length;
  for(const path of [`/ingest-edit/${id.toUpperCase()}/${version}`,`/ingest-edit/${id}/${version}x/media`,`/ingest-edit/${id}/%2e%2e%2fmedia`,`/feedback/${id}/${version}/${fbid}x`,`/feedback/${id}/${version}/%2F${fbid}`,`/ingest-feedback/${id}`,`/edited-media/${id}/${version}/media`])assert.equal((await handle(req(path,'PUT',{},path.startsWith('/ingest-')?'writer':'browser'),e)).status,404);
  assert.equal((await handle(req('/api/edits','POST',{}),e)).status,405);
  assert.equal((await handle(req(`/feedback/${id}/${version}/${fbid}`,'GET'),e)).status,405);
  assert.equal((await handle(req(`/edited-media/${id}/${version}`,'PUT',{}),e)).status,405);
  for(const body of ['{','null','[]',JSON.stringify({text:'x'.repeat(33000)})]){const r=await handle(new Request(`${origin}/feedback/${id}/${version}/${fbid}`,{method:'POST',headers:{Authorization:'Basic '+btoa('kick:browser-test'),Origin:origin,'Content-Type':'application/json'},body}),e);assert.ok([400,413].includes(r.status));}
  for(const value of [{status:'processing'}, {revision:'x',status:'queued'}, {revision:'x',status:'processing',result_version:'../x'}, {revision:'x',status:'processing',message:'x'.repeat(4001)}])assert.equal((await update(e,value)).status,400);
  assert.equal(e.BOARD.writes.length,count);
});
