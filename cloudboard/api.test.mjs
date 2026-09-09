import {test} from 'node:test';
import assert from 'node:assert/strict';
import {handle} from './api.mjs';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';

class Bucket {
  data=new Map();
  async head(k){return this.data.get(k)||null;}
  async get(k,opts={}){const o=this.data.get(k);if(!o)return null;let buf=o.buf,range;
    const r=opts.range?.get('Range')?.match(/^bytes=(\d+)-(\d+)$/);
    if(r){range={offset:+r[1],length:+r[2]-r[1]+1};buf=buf.slice(range.offset,range.offset+range.length);}
    return {...o,range,body:buf,json:async()=>JSON.parse(o.buf.toString())};}
  async put(k,v,opts={}){const old=this.data.get(k),c=opts.onlyIf;
    if(c?.etagDoesNotMatch==='*'&&old||c?.etagMatches&&old?.etag!==c.etagMatches)return null;
    const buf=Buffer.from(v),o={buf,size:buf.length,etag:createHash('md5').update(buf).digest('hex')};this.data.set(k,o);return o;}
  async list(){return {objects:[...this.data.keys()].filter(k=>k.startsWith('candidates/')).map(key=>({key})),truncated:false};}
}
const id='a'.repeat(64),origin='https://example.workers.dev';
const c={id,video:'https://kick.com/zingisukan2525/videos/test',title:'候補',start:100,end:150,preview_start:70,preview_end:180};
const mp4=Buffer.concat([Buffer.from([0,0,0,24]),Buffer.from('ftypisom000000000000')]).toString('base64');
const env=()=>({BOARD:new Bucket(),AUTH_PASS:'test-only-browser',INGEST_TOKEN:'test-only-ingest'});
function req(path,method='GET',body,ingest=false){return new Request(origin+path,{method,headers:{Authorization:ingest?'Bearer test-only-ingest':'Basic '+btoa('kick:test-only-browser'),Origin:origin,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});}
const add=e=>handle(req('/ingest/'+id,'PUT',{candidate:c,mp4},true),e);
test('47 candidates paginate without exceeding open-stream or per-request limits',async()=>{
 const e=env();let open=0,peak=0,reads=0;
 const original=e.BOARD.get.bind(e.BOARD);
 e.BOARD.get=async(...args)=>{reads++;const o=await original(...args);if(!o)return null;open++;peak=Math.max(peak,open);assert.ok(open<=3);const consume=o.json;return {...o,json:async()=>{try{return await consume();}finally{open--;}}};};
 e.BOARD.list=async({limit,cursor})=>{assert.equal(limit,20);const keys=[...e.BOARD.data.keys()].filter(k=>k.startsWith('candidates/'));const start=Number(cursor||0);return {objects:keys.slice(start,start+limit).map(key=>({key})),truncated:start+limit<keys.length,cursor:String(start+limit)};};
 for(let i=0;i<47;i++){const key=i.toString(16).padStart(64,'0');await e.BOARD.put('candidates/'+key,JSON.stringify({...c,id:key}));}
 let cursor=null,total=0;
 do{reads=0;const d=await (await handle(req('/api/candidates'+(cursor?'?cursor='+cursor:'')),e)).json();total+=d.items.length;cursor=d.cursor;assert.ok(reads<=40);assert.equal(open,0);}while(cursor);
 assert.equal(total,47);assert.ok(peak<=3);
});
test('persistent login form, secure cookie, invalid credentials and forged cookie',async()=>{
 const e=env();await add(e);
 const page=await handle(new Request(origin+'/'),e);assert.equal(page.status,200);assert.equal(page.headers.get('WWW-Authenticate'),null);assert.match(await page.text(),/type="password"/);
 const login=(password,site=origin)=>handle(new Request(origin+'/login',{method:'POST',headers:{Origin:site,'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({username:'kick',password})}),e);
 assert.equal((await login('wrong')).status,401);assert.equal((await login(e.AUTH_PASS,'https://other.invalid')).status,403);
 const r=await login(e.AUTH_PASS);assert.equal(r.status,303);const cookie=r.headers.get('Set-Cookie');assert.match(cookie,/Secure; HttpOnly; SameSite=Strict/);
 const headers={Cookie:cookie.split(';')[0]};assert.equal((await handle(new Request(origin+'/api/candidates',{headers}),e)).status,200);
 assert.equal((await handle(new Request(origin+'/media/'+id,{headers}),e)).status,200);
 assert.equal((await handle(new Request(origin+'/ingest/'+id,{headers}),e)).status,401);
 assert.equal((await handle(new Request(origin+'/api/candidates',{headers:{Cookie:headers.Cookie+'x'}}),e)).status,401);
 assert.equal((await handle(new Request(origin+'/api/candidates',{headers}),{...e,AUTH_PASS:'changed'})).status,401);
});
test('fail closed for unset auth; browser and writer roles separated',async()=>{
 assert.equal((await handle(req('/'),{BOARD:new Bucket()})).status,503);
 assert.equal((await handle(new Request(origin+'/media/'+id),env())).status,401);
 assert.equal((await handle(req('/api/candidates','GET',undefined,true),env())).status,401);
 assert.equal((await handle(req('/ingest/'+id,'HEAD'),env())).status,401);
});
test('ingest is idempotent and never resets saved decisions; stale write rejected',async()=>{
 const e=env();assert.equal((await add(e)).status,200);
 const result=await handle(req('/decision/'+id,'PUT',{status:'hold',ds:-10,de:10,revision:null}),e);assert.equal(result.status,200);
 const saved=await result.json();assert.equal((await add(e)).status,200);
 const listing=await (await handle(req('/api/candidates'),e)).json();assert.equal(listing.items.length,1);assert.equal(listing.items[0].decision.ds,-10);
 assert.equal((await handle(req('/decision/'+id,'PUT',{status:'ok',ds:0,de:0,revision:null}),e)).status,409);
 assert.equal((await handle(req('/decision/'+id,'PUT',{status:'ok',ds:0,de:0,revision:saved.revision}),e)).status,200);
});
test('range validation, CSRF and media byte-range',async()=>{
 const e=env();await add(e);
 assert.equal((await handle(req('/decision/'+id,'PUT',{status:'ok',ds:-40,de:0,revision:null}),e)).status,400);
 const csrf=req('/decision/'+id,'PUT',{status:'ok',ds:0,de:0,revision:null});csrf.headers.set('Origin','https://other.invalid');assert.equal((await handle(csrf,e)).status,403);
 const media=req('/media/'+id);media.headers.set('Range','bytes=0-7');const r=await handle(media,e);assert.equal(r.status,206);assert.equal((await r.arrayBuffer()).byteLength,8);assert.match(r.headers.get('Content-Range'),/^bytes 0-7\//);
});
test('page script parses; no embedded video or legacy DB',()=>{
 const s=readFileSync(new URL('index.html',import.meta.url),'utf8');new Function(s.match(/<script>([\s\S]*?)<\/script>/)[1]);assert.ok(!s.includes('base64'));assert.ok(!s.includes('claude.use'));});
