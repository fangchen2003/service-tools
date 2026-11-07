'use strict';
// Offline execution of the real admin script; no server or upstream calls.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'app/static/assets/gate.js'), 'utf8');
function harness() {
  const elements = new Map(), listeners = {}, timers = new Map();
  let timerId = 0;
  function element(selector) {
    if (!elements.has(selector)) {
      const classes = new Set();
      elements.set(selector, {hidden:false, innerHTML:'', textContent:'', value:'24', dataset:{}, style:{}, disabled:false,
        attrs:{}, classList:{add:(s)=>classes.add(s), remove:(s)=>classes.delete(s), contains:(s)=>classes.has(s),
          toggle:(s,on)=>on?classes.add(s):classes.delete(s)},
        setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(k){delete this.attrs[k];},
        querySelectorAll:()=>[], getAnimations:()=>[], scrollTo(){}, close(){this.open=false;}, addEventListener(){}});
    }
    return elements.get(selector);
  }
  const document = {hidden:false, querySelector:element, querySelectorAll:()=>[],
    addEventListener:(event, fn)=>{(listeners[event] ||= []).push(fn);}};
  const context = vm.createContext({document, console, Intl, Date, Math, Number, String, Set, Promise, AbortController,
    window:{lucideIcons:{}, scrollTo(){}}, history:{replaceState(){}}, location:{hash:'#overview'},
    setInterval:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;}, clearInterval:(id)=>timers.delete(id),
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;}, clearTimeout:(id)=>timers.delete(id),
    fetch:()=>{throw new Error('Unexpected network access');}});
  // Keep all real event registration and rendering; skip only automatic login bootstrap.
  vm.runInContext(source.replace(/boot\(\);\s*$/, ''), context);
  return {context, element, timers, run:(s)=>vm.runInContext(s,context),
    visibility(hidden){document.hidden=hidden;(listeners.visibilitychange||[]).forEach(fn=>fn());}};
}
function fixture(hours=24) {
  const now = Math.floor(Date.now()/1000);
  const summary = {enqueued:80, dispatched:75, finished:74, timed_out:5, cancelled:1, expired:2, interrupted:1,
    active_keys:12, wait_p50_seconds:0, wait_p95_seconds:45.25, wait_max_seconds:90, wait_avg_seconds:12,
    timeout_rate:0.0625, timeout_denominator:80, samples:7200, observed_seconds:36000,
    coverage_ratio:0.75, busy_ratio:0.6, unavailable_ratio:0, queue_max:9, queue_avg:2};
  return {generated_at:now, time_zone:'Asia/Shanghai', window_hours:hours, since:now-hours*3600,
    started_at:now-36000, retention_days:30, sample_interval_seconds:5,
    health:{pending_events:0,dropped_events:0,last_error:null,last_sample_at:now}, summary,
    hourly:[{hour_start:now-3600,...summary}], current:{queued:3,ready:1,active:2,total:6,capacity:4},
    assessment:{level:'roomy',label:'余量较宽松',reason:'等待较短，继续关注高峰。'}};
}
test('populated queue metrics use real values, zero waits, Shanghai dates and escaped assessment', ()=>{
  const h=harness(), data=fixture();
  data.assessment.reason='<img src=x onerror=alert(1)>';
  h.context.data=data;
  h.run('renderQueueMetrics(data)');
  const html=h.element('#queueMetricsSummary').innerHTML;
  assert.match(html,/P50[\s\S]*?0 秒/);
  assert.match(html,/P95[\s\S]*?45\.3 秒/);
  assert.match(html,/90 秒/);
  assert.match(html,/6\.3%/);
  assert.match(html,/9/);
  assert.match(html,/60\.0%/);
  assert.match(html,/12/);
  assert.match(h.element('#queueMetricsMeta').textContent,/75\.0%/);
  assert.match(h.element('#queueMetricsMeta').textContent,/Asia\/Shanghai/);
  assert.match(h.element('#queueMetricsHourly').innerHTML,/45\.3 秒/);
  assert.match(h.element('#queueMetricsAssessment').innerHTML,/&lt;img/);
  assert.doesNotMatch(h.element('#queueMetricsAssessment').innerHTML,/<img/);
  assert.match(h.element('#queueMetricsCurrent').textContent,/3/);
});
test('configured telemetry timezone formats all timestamps and labels, invalid zones safely fall back', ()=>{
  for(const [configured,expectedZone] of [['America/New_York','America/New_York'],['UTC','UTC'],
      ['Invalid/Zone','Asia/Shanghai'],['<img src=x onerror=alert(1)>','Asia/Shanghai'],
      [undefined,'Asia/Shanghai'],[null,'Asia/Shanghai'],[{},'Asia/Shanghai']]){
    const h=harness(), data=fixture();
    data.time_zone=configured;
    data.started_at=data.generated_at-36000;
    data.health.last_sample_at=data.generated_at-5;
    data.hourly[0].hour_start=data.generated_at-3600;
    h.context.data=data;h.run('renderQueueMetrics(data)');
    const formatted=n=>new Date(n*1000).toLocaleString('zh-CN',{timeZone:expectedZone,hour12:false});
    const meta=h.element('#queueMetricsMeta').textContent;
    assert.ok(meta.includes(`开始记录 ${formatted(data.started_at)}`),`start time must use ${expectedZone}`);
    assert.ok(meta.includes(`更新 ${formatted(data.generated_at)}（${expectedZone}）`));
    assert.ok(h.element('#queueMetricsHealth').textContent.includes(`最近采样 ${formatted(data.health.last_sample_at)}`));
    assert.ok(h.element('#queueMetricsHourly').innerHTML.includes(`<td>${formatted(data.hourly[0].hour_start)}</td>`));
    assert.equal(h.element('#queueMetrics').dataset.state,'ready');
    assert.doesNotMatch(meta,/<img|Invalid\/Zone/);
  }
});
test('empty observations collect without inventing durations; health warnings never show roomy', ()=>{
  const h=harness(), data=fixture();
  Object.assign(data.summary,{samples:0,observed_seconds:0,dispatched:0,wait_p50_seconds:null,
    wait_p95_seconds:null,wait_max_seconds:null,timeout_rate:null,busy_ratio:null,queue_max:null});
  data.started_at=null;data.hourly=[];h.context.data=data;
  h.run('renderQueueMetrics(data)');
  assert.equal(h.element('#queueMetrics').dataset.state,'collecting');
  assert.doesNotMatch(h.element('#queueMetricsSummary').innerHTML,/0 秒|NaN|undefined/);
  assert.match(h.element('#queueMetricsSummary').innerHTML,/—/);
  assert.match(h.element('#queueMetricsAssessment').innerHTML,/数据不足/);
  data.health.last_error='<script>disk failure</script>';
  h.run('renderQueueMetrics(data)');
  assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
  assert.match(h.element('#queueMetricsHealth').textContent,/disk failure/);
});
test('fresh empty snapshots stay collecting through polling and visibility until response expiry', async()=>{
  const h=harness(), data=fixture();
  let now=data.generated_at*1000;
  h.context.Date=class extends Date {static now(){return now;}};
  Object.assign(data.summary,{samples:0,observed_seconds:0,dispatched:0});
  data.health.last_sample_at=null;data.started_at=null;data.hourly=[];
  h.context.data=data;h.run('api=async()=>data');
  await h.run('loadQueueMetrics()');
  h.run('syncQueueMetricsPolling()');
  assert.equal(h.element('#queueMetrics').dataset.state,'collecting','sync must not require a nonexistent sample');
  h.visibility(true);
  assert.equal(h.element('#queueMetrics').dataset.state,'collecting');
  h.visibility(false);await h.run('loadQueueMetrics()');
  assert.equal(h.element('#queueMetrics').dataset.state,'collecting');
  assert.equal(h.element('#queueMetricsAssessment').dataset.level,'insufficient');
  now+=59999;
  h.run('api=()=>new Promise(()=>{})');
  [...h.timers.values()].find(t=>t.ms===30000).fn();
  assert.equal(h.element('#queueMetrics').dataset.state,'collecting','freshness window must remain collecting');
  now+=2;
  [...h.timers.values()].find(t=>t.ms===30000).fn();
  assert.equal(h.element('#queueMetrics').dataset.state,'stale','empty response must still expire');
  assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
  h.run('cancelQueueMetricsRequest()');
});
test('stale or invalid response timestamps are stale on arrival even with no samples', ()=>{
  for(const samples of [0,7200]){
    for(const generated_at of [null, undefined, NaN, Infinity, Math.floor(Date.now()/1000)-61]){
      const h=harness(), data=fixture();
      data.summary.samples=samples;data.generated_at=generated_at;
      if(!samples) data.health.last_sample_at=null;
      h.context.data=data;h.run('renderQueueMetrics(data)');
      assert.equal(h.element('#queueMetrics').dataset.state,'stale',`samples=${samples}, generated_at=${generated_at}`);
      assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
      h.run('queueMetricsSnapshot=data;syncQueueMetricsPolling()');
      assert.equal(h.element('#queueMetrics').dataset.state,'stale');
    }
  }
});
test('populated snapshots require finite fresh sample timestamps on arrival and sync', ()=>{
  for(const last_sample_at of [null,undefined,NaN,Infinity,'invalid',Math.floor(Date.now()/1000)-61]){
    const h=harness(), data=fixture();data.health.last_sample_at=last_sample_at;h.context.data=data;
    h.run('renderQueueMetrics(data)');
    assert.equal(h.element('#queueMetrics').dataset.state,'stale',`last_sample_at=${last_sample_at}`);
    assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
    h.run('queueMetricsSnapshot=data;syncQueueMetricsPolling()');
    assert.equal(h.element('#queueMetrics').dataset.state,'stale');
  }
});
test('failed requests show unavailable, preserve stale values and revoke green assessment', async()=>{
  const h=harness();
  h.run("api=async()=>{throw new Error('fixture offline');}");
  await h.run('loadQueueMetrics()');
  assert.equal(h.element('#queueMetrics').dataset.state,'unavailable');
  assert.match(h.element('#queueMetricsStatus').textContent,/暂不可用/);
  assert.match(h.element('#queueMetricsSummary').innerHTML,/—/);
  assert.doesNotMatch(h.element('#queueMetricsSummary').innerHTML,/0 秒/);
  h.context.data=fixture();h.run('api=async()=>data');await h.run('loadQueueMetrics()');
  const summary=h.element('#queueMetricsSummary').innerHTML;
  h.run("api=async()=>{throw new Error('fixture offline');}");await h.run('loadQueueMetrics()');
  assert.equal(h.element('#queueMetrics').dataset.state,'stale');
  assert.match(h.element('#queueMetricsStatus').textContent,/上次|过期/);
  assert.equal(h.element('#queueMetricsSummary').innerHTML,summary);
  assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
});
test('window request races never overwrite a newer selection, duplicate loads share one request', async()=>{
  const h=harness(), pending=[];
  h.context.request=(url,opts)=>new Promise((resolve,reject)=>pending.push({url,opts,resolve,reject}));
  h.run('api=request');
  const first=h.run('loadQueueMetrics()');
  const duplicate=h.run('loadQueueMetrics()');
  assert.equal(pending.length,1);
  const second=h.run('changeQueueMetricsWindow("168")');
  assert.equal(pending.length,2);
  assert.equal(pending[0].url,'/admin/api/queue-metrics?hours=24');
  assert.equal(pending[0].opts.cache,'no-store');
  assert.equal(pending[1].url,'/admin/api/queue-metrics?hours=168');
  pending[1].resolve(fixture(168));await second;
  const html=h.element('#queueMetricsSummary').innerHTML;
  pending[0].reject(new Error('old window failure'));await first;await duplicate;
  assert.match(h.element('#queueMetricsMeta').textContent,/7 天/);
  assert.equal(h.element('#queueMetricsSummary').innerHTML,html);
  assert.equal(h.element('#queueMetrics').dataset.state,'ready');
});
test('polling is one 30-second timer only for visible authenticated overview; logout invalidates pending reads', async()=>{
  const h=harness();
  h.run('api=async()=>({keys:[],accounts:[],threshold:20})');
  h.run('syncQueueMetricsPolling();syncQueueMetricsPolling()');
  assert.equal(h.timers.size,1);
  assert.equal([...h.timers.values()][0].ms,30000);
  h.visibility(true);
  assert.equal(h.timers.size,0);
  h.context.data=fixture();h.run('api=async()=>data');
  h.visibility(false);await h.run('loadQueueMetrics()');
  assert.equal(h.timers.size,1);
  h.run('api=async()=>({keys:[]})');
  await h.run("tab('keys')");
  assert.equal(h.timers.size,0);
  await h.run('loadQueueMetrics()');
  h.run("currentTab='ov'; document.querySelector('#tab-ov').hidden=false;syncQueueMetricsPolling()");
  assert.equal(h.timers.size,1);
  let resolve;
  h.context.request=()=>new Promise(r=>{resolve=r;});h.run('api=request');
  const pending=h.run('loadQueueMetrics()');
  h.run('showLogin()');
  assert.equal(h.timers.size,0);
  resolve(fixture());await pending;
  assert.notEqual(h.element('#queueMetrics').dataset.state,'ready');
  h.visibility(false);
  assert.equal(h.timers.size,0);
});
test('under half selected-window coverage continues collecting despite enough tasks and observed hours', ()=>{
  const h=harness(), data=fixture();data.summary.coverage_ratio=.499;h.context.data=data;
  h.run('renderQueueMetrics(data)');
  assert.equal(h.element('#queueMetrics').dataset.state,'collecting');
  assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
  assert.match(h.element('#queueMetricsMeta').textContent,/无可用通道[\s\S]*?0\.0%/);
});
test('old or absent latest sample cannot paint a green capacity assessment', ()=>{
  for(const last_sample_at of [null, Math.floor(Date.now()/1000)-120]){
    const h=harness(), data=fixture();data.health.last_sample_at=last_sample_at;h.context.data=data;
    h.run('renderQueueMetrics(data)');
    assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
    assert.match(h.element('#queueMetricsStatus').textContent,/采样|过期/);
  }
});
test('malformed API response is unavailable rather than a zero or green result', async()=>{
  const h=harness();h.context.data={window_hours:24,summary:{},assessment:{level:'roomy'}};
  h.run('api=async()=>data');await h.run('loadQueueMetrics()');
  assert.equal(h.element('#queueMetrics').dataset.state,'unavailable');
  assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
});
test('hung refresh expires green capacity locally, times out and recovers on next poll', async()=>{
  const h=harness();
  let now=Date.now();
  h.context.Date=class extends Date {static now(){return now;}};
  h.context.data=fixture();h.run('api=async()=>data');
  await h.run('loadQueueMetrics()');
  assert.equal(h.element('#queueMetricsAssessment').dataset.level,'roomy');
  h.run('syncQueueMetricsPolling()');
  let signal;
  h.context.hang=(url,opts)=>{signal=opts.signal;return new Promise(()=>{});};
  h.run('api=hang');
  const pending=h.run('loadQueueMetrics()');
  now+=150000;
  [...h.timers.values()].find(t=>t.ms===30000).fn();
  assert.equal(h.element('#queueMetrics').dataset.state,'stale');
  assert.notEqual(h.element('#queueMetricsAssessment').dataset.level,'roomy');
  assert.match(h.element('#queueMetricsStatus').textContent,/过期|上次/);
  const timeout=[...h.timers.values()].find(t=>t.ms===10000);
  assert.ok(timeout,'metrics refresh must have a bounded timeout');
  timeout.fn();await pending;
  assert.equal(signal.aborted,true);
  assert.equal(h.run('queueMetricsRequest'),null);
  const fresh=fixture();fresh.generated_at=now/1000;fresh.health.last_sample_at=now/1000;
  h.context.data=fresh;h.run('api=async()=>data');
  await h.run('loadQueueMetrics()');
  assert.equal(h.element('#queueMetrics').dataset.state,'ready');
});

test('manual overview refresh reads metrics without waiting for them or breaking old overview', async()=>{
  const h=harness(), calls=[];
  h.context.request=async(url)=>{
    calls.push(url);
    if(url.startsWith('/admin/api/queue-metrics')) return new Promise(()=>{});
    if(url==='/admin/api/reconciliation') return {history:[],retry_after:0};
    if(url==='/admin/api/settings') return {v5_auto_medium:false};
    if(url==='/admin/api/overview') return {today:{images:4,anlas:0,v5:0,requests:4,text_tokens:0},
      keys_active:1,keys_total:1,week:[],pool:[],pool_configured:false,month:{anlas:0}};
    throw new Error('Unexpected '+url);
  };
  h.run('api=request');
  await h.run('refreshCurrentView()');
  assert.ok(calls.includes('/admin/api/queue-metrics?hours=24'));
  assert.match(h.element('#ovCards').innerHTML,/今日生成/);
  assert.equal(h.element('#tab-ov').hidden,false);
  assert.equal(h.element('#refreshView').disabled,false);
});
