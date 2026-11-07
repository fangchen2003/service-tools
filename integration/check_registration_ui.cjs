'use strict';
// Run: NODE_PATH=/path/to/node_modules node --test integration/check_registration_ui.cjs
// Requires Playwright + Chromium. Every browser request is intercepted; no server/OAuth/upstream.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const STATIC = path.resolve(__dirname, '../app/static');
const mime = {'.js':'text/javascript','.css':'text/css','.html':'text/html','.webp':'image/webp'};
function registration() {
  return {enabled:true, configured:true, daily_limit:12, today_count:3, total_count:42,
    day:'2026-11-07', timezone:'Asia/Shanghai', csrf_token:'registration-fixture-csrf',
    history:Array.from({length:30},(_,i)=>({day:new Date(Date.UTC(2026,9,9+i)).toISOString().slice(0,10),count:i}))};
}
async function fixture(t) {
  const browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH||undefined});
  t.after(()=>browser.close());
  const page=await browser.newPage({viewport:{width:1280,height:900},reducedMotion:'reduce'});
    const state={data:registration(),reads:0,writes:[],errors:[],forbidden:[],readError:false,writeError:false,publicRegistration:{day:'2026-11-07',timezone:'Asia/Shanghai',today_count:3,daily_limit:12,remaining:9,enabled:true,configured:true,can_register:true,status:'open',refreshed_at:Date.now()/1000},publicRegistrationError:false};
  page.on('pageerror',error=>state.errors.push(String(error)));
  await page.route('**/*',async route=>{
    const req=route.request(), url=new URL(req.url()), p=url.pathname;
    assert.equal(url.hostname,'registration-ui.invalid');
    const send=data=>route.fulfill({json:data});
    if(p==='/admin/api/registration') {
      if(req.method()==='GET') {
        state.reads++;
        if(state.readError) return route.fulfill({status:503,json:{detail:'fixture read unavailable'}});
        return send(state.data);
      }
      if(req.method()==='PUT') {
        state.writes.push({body:req.postDataJSON(),headers:req.headers()});
        if(state.writeError) return route.fulfill({status:403,json:{detail:'fixture csrf rejected'}});
        Object.assign(state.data,req.postDataJSON());
        if(state.afterWrite) state.afterWrite();
        return send({ok:true}); // Do not rely on PUT echo: real GET readback is required.
      }
    }
    if(req.method()!=='GET'){state.forbidden.push(req.url());return route.abort();}
    if(p==='/admin'||p==='/') return route.fulfill({path:path.join(STATIC,p==='/'?'home.html':'index.html'),contentType:'text/html'});
    if(p.startsWith('/admin/assets/')||p.startsWith('/assets/')) {
      const file=path.join(STATIC,p.startsWith('/admin/assets/')?'assets/'+path.basename(p):path.basename(p));
      if(fs.existsSync(file))return route.fulfill({path:file,contentType:mime[path.extname(file)]||'application/octet-stream'});
    }
    if(p==='/admin/api/me')return send({ok:true});
    if(p==='/admin/api/keys')return send({keys:[]});
    if(p==='/admin/api/allowance')return send({accounts:[],threshold:20});
    if(p==='/admin/api/upstream-tokens')return send({accounts:[],source:'panel'});
    if(p==='/admin/api/announcement')return send({html:'unchanged announcement'});
    if(p==='/admin/api/configuration')return send({values:{},sources:{},csrf_token:'other-csrf'});
    if(p==='/admin/api/settings'||p==='/admin/api/runtime-limits')return send({});
    if(p==='/api/public/registration')return state.publicRegistrationError?route.fulfill({status:503,json:{detail:'fixture unavailable'}}):send(state.publicRegistration);
    if(p==='/api/public/stats')return state.publicStats?send(state.publicStats):route.fulfill({status:503,json:{detail:'fixture only'}});
    state.forbidden.push(req.url());return route.abort();
  });
  const admin=async()=>{
    await page.goto('http://registration-ui.invalid/admin#settings');
    await page.waitForFunction(()=>!document.querySelector('#app').hidden);
    assert.equal(await page.locator('[data-setting-page="registration"]').count(),1,'registration settings navigation exists');
    await page.locator('[data-setting-page="registration"]').click();
  };
  const ready=()=>page.waitForFunction(()=>document.querySelector('#settings-registration').dataset.state==='ready');
  t.after(()=>{assert.deepEqual(state.errors,[]);assert.deepEqual(state.forbidden,[]);});
  return {page,state,admin,ready};
}
test('registration navigation loads authoritative counts, states and last 30 days',async t=>{
  const {page,state,admin,ready}=await fixture(t);
  await admin();await ready();
  assert.equal(await page.locator('#registrationToday').innerText(),'3');
  assert.equal(await page.locator('#registrationTotal').innerText(),'42');
  assert.match(await page.locator('#registrationState').innerText(),/已配置.*开放/);
  assert.equal(await page.locator('#registrationHistory tr').count(),29);
  assert.equal(await page.locator('#registrationHistory tr td:nth-child(2)').evaluateAll(cells=>cells.every(cell=>Number(cell.textContent)>0)),true,'zero-success dates are hidden');
  assert.match(await page.locator('#registrationMeta').innerText(),/2026-11-07.*Asia\/Shanghai/);
  assert.equal(await page.locator('#registrationEnabled').isChecked(),true);
  assert.equal(await page.locator('#registrationDailyLimit').inputValue(),'12');
  assert.equal(await page.locator('#registrationSave').isEnabled(),true);
  state.data.enabled=false;await page.locator('#registrationRefresh').click();await ready();
  assert.match(await page.locator('#registrationState').innerText(),/暂停/);
  state.data.configured=false;await page.locator('#registrationRefresh').click();await ready();
  assert.match(await page.locator('#registrationState').innerText(),/未配置/);
  state.data.history=state.data.history.map(row=>({...row,count:0}));
  await page.locator('#registrationRefresh').click();await ready();
  assert.equal(await page.locator('#registrationHistory tr').count(),1);
  assert.equal(await page.locator('#registrationHistory').innerText(),'暂无成功注册记录');
  assert.equal(await page.locator('#registrationToday').innerText(),'3','history filtering does not change summary counts');
});
test('save sends exact JSON and registration CSRF once, then verifies via GET',async t=>{
  const {page,state,admin,ready}=await fixture(t);
  await admin();await ready();
  const reads=state.reads;
  await page.locator('#registrationEnabled').uncheck();
  await page.locator('#registrationDailyLimit').fill('0');
  assert.equal(await page.evaluate(()=>typeof saveRegistration),'function');
  await page.evaluate(()=>Promise.all([saveRegistration(),saveRegistration()]));
  assert.equal(state.writes.length,1);
  assert.deepEqual(state.writes[0].body,{enabled:false,daily_limit:0});
  assert.equal(state.writes[0].headers['x-nai-admin-csrf'],'registration-fixture-csrf');
  assert.match(state.writes[0].headers['content-type'],/application\/json/);
  assert.equal(state.reads,reads+1,'must read GET after PUT, not trust write response');
  assert.equal(await page.locator('#registrationEnabled').isChecked(),false);
  assert.equal(await page.locator('#registrationDailyLimit').inputValue(),'0');
  assert.match(await page.locator('#registrationStatus').innerText(),/已保存.*核对/);
  for(const value of ['', '-1', '1.5', '100001']) {
    await page.locator('#registrationDailyLimit').fill(value);
    await page.evaluate(()=>saveRegistration());
    assert.equal(state.writes.length,1,'invalid limits never write');
  }
});
test('failed initial reads never invent counts; dirty controls survive refresh errors and recovery',async t=>{
  const {page,state,admin,ready}=await fixture(t);
  state.readError=true;
  await admin();
  await page.waitForFunction(()=>document.querySelector('#registrationStatus').textContent.includes('暂不可用'));
  assert.equal(await page.locator('#registrationToday').innerText(),'—');
  assert.equal(await page.locator('#registrationTotal').innerText(),'—');
  assert.equal(await page.locator('#registrationSave').isDisabled(),true);
  await page.evaluate(()=>saveRegistration());assert.equal(state.writes.length,0);
  state.readError=false;
  await page.locator('#registrationRefresh').click();await ready();
  await page.locator('#registrationEnabled').uncheck();
  await page.locator('#registrationDailyLimit').fill('19');
  state.readError=true;
  await page.locator('#registrationRefresh').click();
  await page.waitForFunction(()=>document.querySelector('#settings-registration').dataset.state==='stale');
  assert.match(await page.locator('#registrationStatus').innerText(),/上次记录/);
  assert.equal(await page.locator('#registrationToday').innerText(),'3');
  assert.equal(await page.locator('#registrationSave').isDisabled(),true);
  state.readError=false;state.data.today_count=4;
  await page.locator('#registrationRefresh').click();await ready();
  assert.equal(await page.locator('#registrationToday').innerText(),'4');
  assert.equal(await page.locator('#registrationEnabled').isChecked(),false,'refresh must preserve unsaved checkbox');
  assert.equal(await page.locator('#registrationDailyLimit').inputValue(),'19','refresh must preserve unsaved limit');
  assert.match(await page.locator('#registrationStatus').innerText(),/未保存/);
});
test('malformed registration reads are rejected before rendering or enabling writes',async t=>{
  const {page,state,admin}=await fixture(t);
  state.data={enabled:true,configured:true,daily_limit:12,today_count:null,total_count:42,history:[]};
  await admin();
  await page.waitForFunction(()=>!document.querySelector('#registrationRefresh').disabled);
  assert.equal(await page.locator('#settings-registration').getAttribute('data-state'),'unavailable');
  assert.equal(await page.locator('#registrationToday').innerText(),'—');
  assert.equal(await page.locator('#registrationSave').isDisabled(),true);
});
test('write failures and unverified readback never claim success; refresh recovers',async t=>{
  const {page,state,admin,ready}=await fixture(t);
  await admin();await ready();
  state.writeError=true;
  await page.locator('#registrationDailyLimit').fill('18');
  await page.evaluate(()=>saveRegistration());
  assert.match(await page.locator('#registrationStatus').innerText(),/保存未确认.*csrf rejected/);
  assert.equal(await page.locator('#registrationDailyLimit').inputValue(),'18');
  assert.equal(await page.locator('#registrationSave').isDisabled(),true);
  state.writeError=false;
  await page.locator('#registrationRefresh').click();await ready();
  state.afterWrite=()=>{state.readError=true;};
  await page.evaluate(()=>saveRegistration());
  assert.match(await page.locator('#registrationStatus').innerText(),/保存.*未确认/);
  assert.equal(await page.locator('#registrationSave').isDisabled(),true);
  state.readError=false;state.afterWrite=()=>{state.data.daily_limit=99;};
  await page.locator('#registrationRefresh').click();await ready();
  await page.locator('#registrationDailyLimit').fill('21');
  await page.evaluate(()=>saveRegistration());
  assert.match(await page.locator('#registrationStatus').innerText(),/保存未确认.*不一致/);
  assert.equal(await page.locator('#registrationSave').isDisabled(),true);
  assert.equal(await page.locator('#registrationDailyLimit').inputValue(),'99','display authoritative readback, not invented submitted values');
  state.afterWrite=null;
  await page.locator('#registrationRefresh').click();await ready();
  await page.locator('#registrationDailyLimit').fill('22');
  await page.evaluate(()=>saveRegistration());
  assert.match(await page.locator('#registrationStatus').innerText(),/已保存.*核对/);
});
test('homepage states today enrollment availability before Discord verification',async t=>{
  const {page,state}=await fixture(t);
  await page.goto('http://registration-ui.invalid/');
  await page.waitForFunction(()=>document.querySelector('#registration-notice')?.dataset.state==='open',null,{timeout:3000});
  const notice=page.locator('#registration-notice');
  assert.match(await notice.innerText(),/今日已注册 3 人/);
  assert.match(await notice.innerText(),/剩余 9/);
  assert.match(await notice.innerText(),/当前可以注册/);
  assert.equal(await page.evaluate(()=>document.querySelector('#registration-notice').compareDocumentPosition(document.querySelector('a[href="/self-register/verify"]'))&Node.DOCUMENT_POSITION_FOLLOWING),4);
  for(const [status,enabled,limit,count,remaining,pattern] of [
    ['full',true,3,3,0,/今日注册名额已满.*当前不可注册/],
    ['paused',false,3,1,2,/注册已暂停.*当前不可注册/],
    ['open',true,0,5,null,/不设每日人数上限.*当前可以注册/],
    ['unavailable',true,0,5,null,/注册服务暂不可用.*当前不可注册/],
  ]){
    Object.assign(state.publicRegistration,{status,enabled,daily_limit:limit,today_count:count,remaining,configured:status!=='unavailable',can_register:status==='open'});
    await page.locator('#refresh').click();
    await page.waitForFunction(expected=>document.querySelector('#registration-notice').dataset.state===expected,status);
    assert.match(await notice.innerText(),pattern);
  }
  state.publicRegistrationError=true;
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('#registration-notice').dataset.state==='error');
  assert.match(await notice.innerText(),/暂无法确认/);
  assert.doesNotMatch(await notice.innerText(),/当前可以注册/);
  state.publicRegistrationError=false;
  Object.assign(state.publicRegistration,{status:'open',configured:true,can_register:true});
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('#registration-notice').dataset.state==='open');
});

test('homepage default quota guidance respects legacy-only scope and hot changes',async t=>{
  const {page,state}=await fixture(t);
  state.publicStats={v5_images_today:0,shared_v5_used:0,shared_v5_limit:150,medium_cost:.6,
    personal_v5_limit:8,personal_legacy_limit:17,personal_image_model_scope:'legacy',
    refreshed_at:Date.now()/1000,reset_at:Date.now()/1000+3600,timezone:'Asia/Shanghai',day:'2026-11-07'};
  await page.goto('http://registration-ui.invalid/');
  await page.waitForFunction(()=>document.querySelector('#live-state').dataset.state==='ok');
  assert.equal(await page.locator('#personal-v5-policy').count(),1);
  assert.equal(await page.locator('#personal-v5-policy').isHidden(),true);
  assert.match(await page.locator('#personal-model-note').innerText(),/仅 V4.5 及以下/);
  assert.equal(await page.locator('#personal-legacy').innerText(),'17');
  state.publicStats.personal_image_model_scope='all';state.publicStats.personal_v5_limit=4;
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('#personal-v5').textContent==='4');
  assert.equal(await page.locator('#personal-v5-policy').isVisible(),true);
});

test('homepage remains generic and fails closed on malformed enrollment data',async t=>{
  const {page,state}=await fixture(t);
  await page.goto('http://registration-ui.invalid/');
  await page.waitForFunction(()=>document.querySelector('#registration-notice').dataset.state==='open');
  assert.equal(await page.title(),'NAI Gate — 让灵感成画');
  assert.equal(await page.locator('#api-origin').innerText(),'http://registration-ui.invalid');
  assert.match(await page.locator('.art-image').getAttribute('src'),/^\/assets\/gate-atelier-v1\.webp\?/);
  assert.match(await page.locator('.rules').innerText(),/未开通 Anlas[\s\S]*已开通 Anlas[\s\S]*allow_anlas/);
  const valid={...state.publicRegistration};
  for(const invalid of [{today_count:null},{daily_limit:-1},{can_register:false},{status:'full'},{remaining:10}]){
    state.publicRegistration={...valid,...invalid};
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#registration-notice').dataset.state==='error');
    assert.doesNotMatch(await page.locator('#registration-notice').innerText(),/当前可以注册/);
    state.publicRegistration={...valid};
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#registration-notice').dataset.state==='open');
  }
});

test('homepage expires enrollment availability even while a refresh is hung',async t=>{
  const {page}=await fixture(t);
  await page.goto('http://registration-ui.invalid/');
  await page.waitForFunction(()=>document.querySelector('#registration-notice').dataset.state==='open');
  await page.evaluate(()=>{
    const originalFetch=window.fetch;
    window.fetch=(url,...args)=>url==='/api/public/registration'?new Promise(()=>{}):originalFetch(url,...args);
    const originalNow=Date.now;
    Date.now=()=>originalNow()+61000;
    document.dispatchEvent(new Event('visibilitychange'));
  });
  assert.equal(await page.locator('#registration-notice').getAttribute('data-state'),'error');
  assert.match(await page.locator('#registration-notice').innerText(),/注册状态已过期/);
  assert.doesNotMatch(await page.locator('#registration-notice').innerText(),/当前可以注册/);
});

test('home verification and registration settings fit mobile and both admin themes',async t=>{
  const {page,admin,ready}=await fixture(t);
  await page.goto('http://registration-ui.invalid/');
  assert.equal(await page.locator('a[href="/workbench/"]').count(),1);
  assert.equal(await page.locator('a[href="/self-register/verify"]').count(),1);
  assert.match(await page.locator('.discord-verify').innerText(),/Discord 身份验证[\s\S]*验证指定身份组后加入服务器，再使用 \/register 领取 Key/);
  assert.doesNotMatch(await page.content(),/discord\.gg|discord\.com\/invite/);
  for(const width of [1280,640,390,320]){
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'home overflow '+width);
    assert.equal(await page.locator('.discord-verify a').isVisible(),true);
  }
  if(process.env.REGISTRATION_UI_SCREENSHOT_DIR){
    fs.mkdirSync(process.env.REGISTRATION_UI_SCREENSHOT_DIR,{recursive:true});
    await page.screenshot({path:path.join(process.env.REGISTRATION_UI_SCREENSHOT_DIR,'registration-home-mobile.png'),fullPage:true});
  }
  await admin();await ready();
  for(const theme of ['light','dark']){
    await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
    for(const width of [1280,640,390,320]){
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'admin overflow '+theme+' '+width);
      assert.equal(await page.locator('#registrationDailyLimit').isVisible(),true);
      assert.equal(await page.locator('#registrationHistory').evaluate(el=>el.closest('.gate-table-scroll').scrollWidth<=el.closest('.gate-table-scroll').clientWidth),true,'two-column registration history must fit without hidden counts '+width);
    }
  }
  if(process.env.REGISTRATION_UI_SCREENSHOT_DIR)await page.screenshot({path:path.join(process.env.REGISTRATION_UI_SCREENSHOT_DIR,'registration-admin-mobile.png'),fullPage:true});
});
