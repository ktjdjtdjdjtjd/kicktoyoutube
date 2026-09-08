import {loginPage,makeSession,hasSession} from './login.mjs';
const ID=/^[a-f0-9]{64}$/;
const json=(value,status=200)=>Response.json(value,{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
const equal=(a,b)=>{let d=a.length^b.length;for(let i=0;i<Math.max(a.length,b.length);i++)d|=(a.charCodeAt(i)||0)^(b.charCodeAt(i)||0);return d===0;};
async function body(request,max){
  if(!request.headers.get('content-type')?.startsWith('application/json')) throw Error('JSON required');
  const reader=request.body.getReader();let size=0;const chunks=[];
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>max){await reader.cancel();throw Error('body too large');}chunks.push(value);}
  const bytes=new Uint8Array(size);let at=0;for(const c of chunks){bytes.set(c,at);at+=c.length;}
  return JSON.parse(new TextDecoder().decode(bytes));
}
export function validCandidate(c){
  if(!c||!ID.test(c.id)||typeof c.title!=='string'||c.title.length>160||typeof c.video!=='string')return false;
  if(!/^https:\/\/(kick\.com|www\.twitch\.tv)\//.test(c.video))return false;
  if(![c.start,c.end,c.preview_start,c.preview_end].every(Number.isFinite))return false;
  return c.preview_start>=0&&c.start>=c.preview_start&&c.end>c.start&&c.preview_end>=c.end&&c.preview_end-c.preview_start<=660;
}
export async function handle(request,env,html=''){
  const url=new URL(request.url),path=url.pathname,ingest=path.startsWith('/ingest/');
  if(!env.BOARD||!(ingest?env.INGEST_TOKEN:env.AUTH_PASS))return json({error:'未設定'},503);
  if(path==='/login'&&request.method==='POST'){
    if(request.headers.get('origin')!==url.origin)return json({error:'origin'},403);
    try{
      if(!request.headers.get('content-type')?.startsWith('application/x-www-form-urlencoded'))return json({error:'form'},400);
      const reader=request.body.getReader();let size=0;const parts=[];
      while(true){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>4096){await reader.cancel();return json({error:'size'},413);}parts.push(value);}
      const bytes=new Uint8Array(size);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}
      const form=new URLSearchParams(new TextDecoder().decode(bytes));
      if(!equal(form.get('username')||'','kick')||!equal(form.get('password')||'',env.AUTH_PASS))return loginPage(true);
      const session=await makeSession(env.AUTH_PASS);
      return new Response(null,{status:303,headers:{Location:'/', 'Cache-Control':'no-store','Set-Cookie':'clip_session='+session+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=604800'}});
    }catch{return json({error:'login'},400);}
  }
  const want=ingest?'Bearer '+env.INGEST_TOKEN:'Basic '+btoa('kick:'+env.AUTH_PASS);
  const authenticated=equal(request.headers.get('Authorization')||'',want)||(!ingest&&await hasSession(request,env.AUTH_PASS));
  if(!authenticated)return !ingest&&path==='/'&&request.method==='GET'?loginPage():json({error:'ログインが必要です'},401);
  if(!ingest&&!['GET','HEAD'].includes(request.method)&&request.headers.get('origin')!==url.origin)return json({error:'origin'},403);
  try{
    const match=path.match(/^\/(ingest|media|decision)\/([a-f0-9]{64})$/);
    if(path==='/'&&request.method==='GET')return new Response(html,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'",'X-Content-Type-Options':'nosniff'}});
    if(path==='/api/candidates'&&request.method==='GET'){
      const list=await env.BOARD.list({prefix:'candidates/',limit:20,cursor:url.searchParams.get('cursor')||undefined});
      const items=[];
      for(let i=0;i<list.objects.length;i+=3){
        const batch=await Promise.all(list.objects.slice(i,i+3).map(async o=>{
          const c=await env.BOARD.get(o.key);if(!c)return null;
          const candidate=await c.json();
          const d=await env.BOARD.get('decisions/'+o.key.split('/')[1]);
          return {...candidate,decision:d?await d.json():{status:'',ds:0,de:0},revision:d?.etag||null};
        }));
        items.push(...batch.filter(Boolean));
      }
      return json({items,cursor:list.truncated?list.cursor:null});
    }
    if(!match)return json({error:'not found'},404);
    const [,kind,id]=match,key='candidates/'+id;
    if(kind==='ingest'){
      const existing=await env.BOARD.head(key);
      if(request.method==='HEAD')return new Response(null,{status:existing?200:404});
      if(request.method!=='PUT')return json({error:'method'},405);
      if(existing)return json({id,duplicate:true});
      const input=await body(request,16*1024*1024),c=input.candidate;
      if(!validCandidate(c)||c.id!==id)return json({error:'candidate'},400);
      const video=Uint8Array.from(atob(input.mp4),x=>x.charCodeAt(0));
      if(video.length<24||new TextDecoder().decode(video.slice(4,8))!=='ftyp')return json({error:'mp4'},400);
      await env.BOARD.put('media/'+id,video,{onlyIf:{etagDoesNotMatch:'*'},httpMetadata:{contentType:'video/mp4'}});
      const clean={id,title:c.title,video:c.video,start:c.start,end:c.end,preview_start:c.preview_start,preview_end:c.preview_end,created_at:new Date().toISOString()};
      const saved=await env.BOARD.put(key,JSON.stringify(clean),{onlyIf:{etagDoesNotMatch:'*'}});
      return json({id,duplicate:!saved});
    }
    if(kind==='decision'&&request.method==='PUT'){
      const obj=await env.BOARD.get(key);if(!obj)return json({error:'not found'},404);
      const c=await obj.json(),d=await body(request,4096);
      if(!['','ok','hold','no'].includes(d.status)||![d.ds,d.de].every(x=>Number.isInteger(x)&&Math.abs(x)<=300))return json({error:'decision'},400);
      if(c.start+d.ds<c.preview_start||c.end+d.de>c.preview_end||c.end+d.de-c.start-d.ds<5)return json({error:'range'},400);
      if(d.revision!==null&&(typeof d.revision!=='string'||!/^[a-f0-9]+$/.test(d.revision)))return json({error:'revision'},400);
      const clean={status:d.status,ds:d.ds,de:d.de};
      const saved=await env.BOARD.put('decisions/'+id,JSON.stringify(clean),{onlyIf:d.revision?{etagMatches:d.revision}:{etagDoesNotMatch:'*'}});
      return saved?json({decision:clean,revision:saved.etag}):json({error:'別の端末で変更されました。再読み込みしてください'},409);
    }
    if(kind==='media'&&['GET','HEAD'].includes(request.method)){
      const obj=await env.BOARD.get('media/'+id,{range:request.headers});if(!obj)return json({error:'not found'},404);
      const headers=new Headers({'Content-Type':'video/mp4','Accept-Ranges':'bytes','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'});
      let status=200,length=obj.size;
      if(obj.range){status=206;const offset=obj.range.offset??Math.max(0,obj.size-obj.range.suffix),n=obj.range.length??Math.min(obj.range.suffix,obj.size);length=n;headers.set('Content-Range',`bytes ${offset}-${offset+n-1}/${obj.size}`);}
      headers.set('Content-Length',String(length));return new Response(request.method==='HEAD'?null:obj.body,{status,headers});
    }
    return json({error:'method'},405);
  }catch(e){return json({error:'処理できませんでした'},400);}
}
