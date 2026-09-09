// Isolated browser test: no request reaches the production board.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('C:/Users/KeNEe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
test('mobile edited videos, feedback retry, persisted draft, version mapping and XSS',async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}});
  const cid='a'.repeat(64),ver='b'.repeat(64),older='c'.repeat(64),history=[],sends=[];
  let fail=true;
  const movie=JSON.parse(fs.readFileSync('C:/Users/KeNEe/work/stream/20260909_board_d590b2cc9e205e81/DRAFT.json','utf8')).video;
  const edits=[{id:cid,version:ver,title:'海苔のアップ版',duration:60,status:'draft',created_at:'2026-09-09T12:00:00Z'},
    {id:cid,version:older,title:'海苔の旧版',duration:60,status:'draft',created_at:'2026-09-09T11:00:00Z'}];
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://review.test/**',async route=>{
   const r=route.request(),u=new URL(r.url());
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'index.html'))});
   if(u.pathname==='/api/candidates')return route.fulfill({json:{items:[],cursor:null}});
   if(u.pathname==='/api/edits')return route.fulfill({json:{items:edits,cursor:null}});
   if(u.pathname==='/api/feedback')return route.fulfill({json:{items:history,cursor:null}});
   if(u.pathname.startsWith('/edited-media/')){
    const size=fs.statSync(movie).size,range=/bytes=(\d+)-(\d*)/.exec(r.headers().range||'');
    const start=range?+range[1]:0,end=range&&range[2]?Math.min(+range[2],size-1):size-1;
    const fd=fs.openSync(movie,'r'),data=Buffer.alloc(end-start+1);fs.readSync(fd,data,0,data.length,start);fs.closeSync(fd);
    return route.fulfill({status:range?206:200,headers:{'Content-Type':'video/mp4','Accept-Ranges':'bytes','Content-Length':String(data.length),...(range?{'Content-Range':`bytes ${start}-${end}/${size}`}:{})},body:data});
   }
   if(u.pathname.startsWith('/feedback/')){
    sends.push({path:u.pathname,...r.postDataJSON()});
    if(fail){fail=false;return route.fulfill({status:500,json:{error:'test retry'}});}
    const f={id:u.pathname.split('/').pop(),candidate_id:cid,version:ver,text:r.postDataJSON().text,status:'queued',revision:'abcdef',created_at:new Date().toISOString()};
    history.push(f);return route.fulfill({json:f});
   }
   return route.fulfill({status:404,body:''});
  });
  await page.goto('http://review.test/');
  await page.getByRole('button',{name:'編集済み',exact:true}).click();
  const input=page.locator('textarea:visible');await input.waitFor();
  const content='もっとアップに <img src=x onerror="window.xss=1">';
  await input.fill(content);await page.reload();
  await page.getByRole('button',{name:'編集済み',exact:true}).click();
  await assert.doesNotReject(()=>page.waitForFunction(t=>document.querySelector('textarea')?.value===t,content));
  const send=page.locator('.feedback-send:visible');await send.click();
  await page.waitForFunction(()=>!document.querySelector('.feedback-send')?.disabled);
  assert.equal(await input.inputValue(),content);
  await send.click();await page.waitForFunction(()=>document.querySelector('.feedback-history')?.textContent.includes('もっとアップに'));
  assert.equal(sends.length,2);assert.equal(sends[0].path,sends[1].path);assert.ok(sends[0].path.includes(ver));
  assert.equal(await page.evaluate(()=>window.xss),undefined);
  assert.equal(await page.locator('.feedback-history img').count(),0);
  history[0]={...history[0],revision:'fedcba',status:'processing'};
  await page.evaluate(()=>loadEdits());
  await page.waitForFunction(()=>document.querySelector('.feedback-history')?.textContent.includes('編集中'));
  const video=page.locator('#edited video:visible');
  await video.evaluate(v=>v.play());
  await page.waitForFunction(()=>{const v=document.querySelector('#edited section:not([hidden]) video');return v?.currentTime>0.2;});
  assert.equal(await video.evaluate(v=>Math.round(v.duration)),60);
  assert.equal(await video.evaluate(v=>v.videoWidth),1080);
  await video.evaluate(v=>{v.pause();v.currentTime=18;});
  await page.waitForFunction(()=>!document.querySelector('#edited section:not([hidden]) video').seeking);
  await page.screenshot({path:path.join(__dirname,'../out_board/review-mobile.png'),fullPage:true});
  await page.locator('select').selectOption(older);
  assert.ok((await page.locator('#edited video:visible').getAttribute('src')).includes(older));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
});
