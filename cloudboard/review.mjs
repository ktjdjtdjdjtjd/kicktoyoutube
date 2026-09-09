// Called only after api.mjs has enforced authentication and browser Origin checks.
const HEX64='[a-f0-9]{64}',HEX32='[a-f0-9]{32}';
const editPath=new RegExp(`^/(ingest-edit|edited-media)/(${HEX64})/(${HEX64})(/media)?$`);
const feedbackPath=new RegExp(`^/feedback/(${HEX64})/(${HEX64})/(${HEX32})$`);
const updatePath=new RegExp(`^/ingest-feedback/(${HEX32})$`);
export const MAX_MEDIA=95*1024*1024;
const json=(data,status=200)=>Response.json(data,{status,headers:{'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}});
const fail=(status,message)=>{throw Object.assign(new Error(message),{status});};
const editKey=(id,version)=>`edits/${id}/${version}.json`;
const mediaKey=(id,version)=>`edited-media/${id}/${version}.mp4`;
const feedbackKey=id=>`feedback/${id}.json`;
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const hex64=value=>typeof value==='string'&&new RegExp(`^${HEX64}$`).test(value);
const now=()=>new Date().toISOString();

async function readBody(request,max,media=false){
  const type=request.headers.get('content-type')?.split(';')[0].trim().toLowerCase();
  if(type!==(media?'video/mp4':'application/json'))fail(400,'content-type');
  const raw=request.headers.get('content-length');
  if(raw!==null&&!/^\d+$/.test(raw))fail(400,'content-length');
  const declared=raw===null?null:Number(raw);
  if(declared!==null&&(!Number.isSafeInteger(declared)||declared>max))fail(413,'size');
  if(!request.body)fail(400,'body');
  // One bounded allocation; DigestStream avoids a second 95 MiB hash buffer on Workers.
  const bytes=new Uint8Array(declared??max),reader=request.body.getReader();
  const digest=media&&typeof crypto.DigestStream==='function'?new crypto.DigestStream('SHA-256'):null;
  const writer=digest?.getWriter();
  if(digest)digest.digest.catch(()=>{});
  let size=0;
  try{
    while(true){
      const {done,value}=await reader.read();if(done)break;
      if(size+value.length>max)fail(413,'size');
      if(size+value.length>bytes.length)fail(400,'content-length');
      bytes.set(value,size);size+=value.length;
      if(writer)await writer.write(value);
    }
    if(declared!==null&&size!==declared)fail(400,'content-length');
    if(writer)await writer.close();
  }catch(e){await reader.cancel().catch(()=>{});if(writer)await writer.abort(e).catch(()=>{});throw e;}
  const value=bytes.subarray(0,size);
  if(!media){try{return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(value));}catch{fail(400,'JSON');}}
  if(size<24||String.fromCharCode(...value.subarray(4,8))!=='ftyp')fail(400,'mp4');
  const box=new DataView(value.buffer,value.byteOffset,value.byteLength).getUint32(0);
  if(box<16||box>size)fail(400,'ftyp size');
  const hash=await (digest?digest.digest:crypto.subtle.digest('SHA-256',value));
  return {value,sha256:Array.from(new Uint8Array(hash),v=>v.toString(16).padStart(2,'0')).join('')};
}

async function list(bucket,url,prefix){
  const page=await bucket.list({prefix,limit:20,cursor:url.searchParams.get('cursor')||undefined}),items=[];
  const valid=prefix==='edits/'?new RegExp(`^edits/${HEX64}/${HEX64}\\.json$`):new RegExp(`^feedback/${HEX32}\\.json$`);
  for(const entry of page.objects){
    if(!valid.test(entry.key))continue;
    const obj=await bucket.get(entry.key);if(!obj)continue;
    const data=await obj.json();items.push(prefix==='feedback/'?{...data,revision:obj.etag}:data);
  }
  return json({items,cursor:page.truncated?page.cursor:null});
}

async function mediaResponse(request,bucket,id,version){
  // Orphan uploads are never served before edit metadata has been committed.
  if(!await bucket.head(editKey(id,version)))return json({error:'edit not found'},404);
  const key=mediaKey(id,version),head=await bucket.head(key);
  if(!head)return json({error:'media not found'},404);
  const headers=new Headers({'Content-Type':'video/mp4','Accept-Ranges':'bytes','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','ETag':head.httpEtag||`"${head.etag}"`});
  const raw=request.headers.get('range');let range,status=200,length=head.size;
  if(raw!==null){
    const m=/^bytes=(\d*)-(\d*)$/.exec(raw);
    let start,end;
    if(m&&(m[1]||m[2])){
      if(!m[1]){const suffix=Number(m[2]);if(Number.isSafeInteger(suffix)&&suffix>0){start=Math.max(0,head.size-suffix);end=head.size-1;}}
      else {start=Number(m[1]);end=m[2]?Number(m[2]):head.size-1;}
    }
    if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<0||start>=head.size||end<start){
      headers.set('Content-Range',`bytes */${head.size}`);return new Response(null,{status:416,headers});
    }
    end=Math.min(end,head.size-1);length=end-start+1;range={offset:start,length};status=206;
    headers.set('Content-Range',`bytes ${start}-${end}/${head.size}`);
  }
  headers.set('Content-Length',String(length));
  if(request.method==='HEAD')return new Response(null,{status,headers});
  const obj=await bucket.get(key,range?{range}:{});
  return obj?new Response(obj.body,{status,headers}):json({error:'media not found'},404);
}

async function immutableJSON(bucket,key,clean,same){
  const saved=await bucket.put(key,JSON.stringify(clean),{onlyIf:{etagDoesNotMatch:'*'}});
  if(saved)return json({...clean,revision:saved.etag});
  const current=await bucket.get(key);
  if(current){const data=await current.json();if(same(data))return json({...data,revision:current.etag,duplicate:true});}
  return json({error:'conflict'},409);
}

export async function reviewRoute(request,env){
  const url=new URL(request.url),path=url.pathname,bucket=env.BOARD;
  const edit=editPath.exec(path),feedback=feedbackPath.exec(path),update=updatePath.exec(path);
  const listing=path==='/api/edits'||path==='/api/feedback';
  if(!edit&&!feedback&&!update&&!listing)return null;
  try{
    if(listing)return request.method==='GET'?list(bucket,url,path==='/api/edits'?'edits/':'feedback/'):json({error:'method'},405);
    if(edit){
      const [,kind,id,version,suffix]=edit;
      if(kind==='edited-media')return suffix?json({error:'path'},404):['GET','HEAD'].includes(request.method)?mediaResponse(request,bucket,id,version):json({error:'method'},405);
      if(request.method!=='PUT')return json({error:'method'},405);
      if(!await bucket.head('candidates/'+id))return json({error:'candidate not found'},404);
      if(suffix){
        const {value,sha256}=await readBody(request,MAX_MEDIA,true);
        if(sha256!==version)return json({error:'SHA-256 mismatch'},400);
        const key=mediaKey(id,version);
        const saved=await bucket.put(key,value,{onlyIf:{etagDoesNotMatch:'*'},sha256:version,httpMetadata:{contentType:'video/mp4',cacheControl:'private, no-store'}});
        if(!saved){const existing=await bucket.head(key);if(!existing||existing.size!==value.length)return json({error:'conflict'},409);}
        return json({id,version,duplicate:!saved});
      }
      const input=await readBody(request,16384);
      if(!object(input)||typeof input.title!=='string'||input.title.length>160||!Number.isFinite(input.duration)||input.duration<=0||!['draft','ready'].includes(input.status))return json({error:'edit'},400);
      if(!await bucket.head(mediaKey(id,version)))return json({error:'media not found'},404);
      const key=editKey(id,version),current=await bucket.get(key),old=current?await current.json():null;
      const clean={id,version,title:input.title,duration:input.duration,status:input.status,created_at:old?.created_at||now()};
      if(old&&old.title===clean.title&&old.duration===clean.duration&&old.status===clean.status)return json({...clean,duplicate:true});
      // Metadata may advance draft -> ready; the video bytes remain immutable.
      const saved=await bucket.put(key,JSON.stringify(clean),{onlyIf:current?{etagMatches:current.etag}:{etagDoesNotMatch:'*'}});
      if(saved)return json(clean);
      const raced=await bucket.get(key),data=raced?await raced.json():null;
      return data&&data.title===clean.title&&data.duration===clean.duration&&data.status===clean.status?json({...data,duplicate:true}):json({error:'conflict'},409);
    }
    if(feedback){
      if(request.method!=='POST')return json({error:'method'},405);
      const [,id,version,fbid]=feedback,input=await readBody(request,32768);
      if(!object(input)||typeof input.text!=='string'||input.text.length<1||input.text.length>4000)return json({error:'text'},400);
      if(!await bucket.head(editKey(id,version)))return json({error:'edit not found'},404);
      const stamp=now(),clean={id:fbid,candidate_id:id,version,text:input.text,status:'queued',created_at:stamp,updated_at:stamp};
      return immutableJSON(bucket,feedbackKey(fbid),clean,old=>old.candidate_id===id&&old.version===version&&old.text===clean.text);
    }
    if(request.method!=='PUT')return json({error:'method'},405);
    const input=await readBody(request,32768);
    if(!object(input)||typeof input.revision!=='string'||!input.revision||input.revision.length>256||!['processing','completed','blocked'].includes(input.status)||
      (input.message!==undefined&&(typeof input.message!=='string'||input.message.length>4000))||(input.result_version!==undefined&&!hex64(input.result_version)))return json({error:'feedback update'},400);
    const key=feedbackKey(update[1]),obj=await bucket.get(key);if(!obj)return json({error:'feedback not found'},404);
    const current=await obj.json();
    if(input.revision!==obj.etag)return json({error:'revision conflict'},409);
    if(input.status!==current.status&&!(current.status==='queued'&&input.status==='processing')&&!(current.status==='processing'&&['completed','blocked'].includes(input.status)))return json({error:'transition'},409);
    const clean={...current,status:input.status,updated_at:now()};
    if(input.message!==undefined)clean.message=input.message;
    if(input.result_version!==undefined)clean.result_version=input.result_version;
    if(clean.status==='completed'&&!hex64(clean.result_version))return json({error:'result_version required'},400);
    if(clean.result_version&&!await bucket.head(editKey(current.candidate_id,clean.result_version)))return json({error:'result edit not found'},404);
    const saved=await bucket.put(key,JSON.stringify(clean),{onlyIf:{etagMatches:input.revision}});
    return saved?json({...clean,revision:saved.etag}):json({error:'revision conflict'},409);
  }catch(e){return json({error:e.status?e.message:'invalid request'},e.status||400);}
}
