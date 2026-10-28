const $ = s => document.querySelector(s);
let editingId = null, keysCache = [], logPage = 1, currentTab = 'ov', currentSettingsPage = 'budget';
const pageTitles = {ov:'总览',keys:'密钥管理',logs:'用量日志',set:'设置'};
const pageRoutes = {ov:'overview',keys:'keys',logs:'logs',set:'settings'};
function paintIcons(root=document){root.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=window.lucideIcons[el.dataset.icon];el.setAttribute('aria-hidden','true');});}
let allowanceTimer = null, allowanceLoading = false;
let reconBusy = false, reconReady = false, reconRemoteRunning = false, reconRetryAt = 0, reconTimer = null, reconVersion = 0;
let reconCsrf = '';
let configurationCsrf = '', configurationSnapshot = null, upstreamAccounts = [], editingUpstream = null;
let budgetSnapshot = null, runtimeSnapshot = null, announcementSnapshot = null, upstreamSource = null;
let keySaveBusy = false, confirmationResolve = null;
let viewVersion = 0, overviewRequestVersion = 0;
const configurationFields = [
  {name:'allow_img2img',group:'generation',label:'图生图 / 局部重绘',type:'bool',note:'单个 Key 还需开启图生图和 Anlas 权限'},
  {name:'safe_clamp',group:'generation',label:'免费图片参数自动调整',type:'bool'},
  {name:'max_pixels',group:'generation',label:'免费图片最大像素',min:4096,max:16777216,unit:'像素'},
  {name:'max_steps',group:'generation',label:'免费图片步数上限',min:1,max:28,unit:'步',note:'V5 High：此值与 23 取小；旧模型：此值与 28 取小。Medium 固定 14 步；付费和管理员 Key 不受此上限约束。'},
  {name:'default_image_model_scope',group:'defaults',label:'图片模型范围',type:'scope'},
  {name:'default_daily_images',group:'defaults',label:'每日免费图片',min:0,max:1000000,unit:'张'},
  {name:'default_daily_v5',group:'defaults',label:'每日 V5 额度',min:0,max:100000,unit:'单位'},
  {name:'default_daily_anlas',group:'defaults',label:'每日 Anlas',min:0,max:100000,step:'any',unit:'Anlas'},
  {name:'default_monthly_anlas',group:'defaults',label:'每月 Anlas',min:0,max:100000,step:'any',unit:'Anlas'},
  {name:'default_daily_text_tokens',group:'defaults',label:'每日文本 Token',min:0,max:100000000,unit:'Token'},
  {name:'default_rpm',group:'defaults',label:'每分钟请求数',min:1,max:600,unit:'次'},
  {name:'default_expires_days',group:'defaults',label:'有效天数',min:0,max:3650,unit:'天',note:'0 = 长期有效'},
  {name:'default_exclude_global_v5',group:'defaults',label:'独立 V5 额度',type:'bool',note:'不占用全站每日 V5 额度'},
  {name:'default_allow_anlas',group:'defaults',label:'允许消耗 Anlas',type:'bool'},
  {name:'default_allow_img2img',group:'defaults',label:'允许图生图 / 局部重绘',type:'bool',note:'需同时允许消耗 Anlas'},
  {name:'key_inactivity_delete_days',group:'cleanup',label:'闲置 Key 自动清理',min:0,max:3650,unit:'天'},
  {name:'global_concurrency',group:'advanced',label:'全站非图片请求并发',min:1,max:32,unit:'个'},
  {name:'key_concurrency',group:'advanced',label:'每个 Key 请求并发',min:1,max:32,unit:'个'},
  {name:'max_text_output_tokens',group:'advanced',label:'单次文本输出上限',min:1,max:65536,unit:'Token'},
  {name:'max_input_chars',group:'advanced',label:'文本输入长度上限',min:1,max:1000000,unit:'字符'},
];
const budgetFields = [
  {name:'global_monthly_anlas',label:'全站月度预算',unit:'Anlas'},
  {name:'global_daily_v5',label:'全站每日 V5 额度',unit:'单位'},
  {name:'v5_auto_medium',label:'V5 Full 自动节省',type:'bool'},
  {name:'v5_medium_threshold',label:'Medium 切换阈值',unit:'%'},
  {name:'v5_medium_multiplier',label:'Medium 每张扣除额度',unit:'单位'},
  {name:'v5_alert_threshold',label:'官方余量告警阈值',unit:'%'},
];
const runtimeFields = [
  {name:'queue_timeout',label:'单阶段排队等待上限',min:15,max:300,unit:'秒'},
  {name:'key_image_min_interval',label:'用户 Key 图片任务间隔',min:15,max:120,unit:'秒'},
  {name:'image_min_interval',label:'上游图片请求间隔',min:15,max:120,unit:'秒'},
  {name:'image_429_cooldown_seconds',label:'上游 429 最短冷却',min:60,max:3600,unit:'秒'},
];
// Built-in generation defaults from app/config.py.
const generationDefaults = {
  allow_img2img:false,safe_clamp:true,max_pixels:1048576,max_steps:28,
  queue_timeout:90,key_image_min_interval:15,image_min_interval:15,image_429_cooldown_seconds:60,
};

const reconReasons = {first:'已记下当前余额', accounts_changed:'账号列表有变化，已重新记录余额',
  cycle_changed:'订阅周期已更新，已重新记录余额', balance_increased:'余额增加了，已重新记录余额',
  ledger_reset:'本站用量记录有变动，已重新记录余额'};
const reconNumber = n => n == null ? '—' : Number(n).toLocaleString('zh-CN', {maximumFractionDigits:4});
const reconTime = n => new Date(n*1000).toLocaleString('zh-CN');
function updateReconciliationButton(){
  const seconds = Math.max(0, Math.ceil((reconRetryAt-Date.now())/1000));
  $('#reconRun').disabled = reconBusy || reconRemoteRunning || !reconReady || seconds>0;
  const label=reconBusy?'正在核对…':seconds?`${seconds} 秒后可再次查询`:'查询并核对';
  $('#reconRun').innerHTML=`<i data-icon="refresh"></i>${esc(label)}`;
  paintIcons($('#reconRun'));
  $('#reconReload').disabled = reconBusy;
  if(!seconds){ clearInterval(reconTimer); reconTimer=null; }
}
function renderReconciliation(d){
  reconCsrf=typeof d.csrf_token === 'string' ? d.csrf_token : '';
  reconReady=!!reconCsrf;
  reconRemoteRunning=d.running;
  reconRetryAt=Date.now() + d.retry_after*1000;
  clearInterval(reconTimer);
  if(d.retry_after>0) reconTimer=setInterval(updateReconciliationButton, 1000); // Local countdown only.
  updateReconciliationButton();
  const rows=d.history || [], latest=rows[0], c=latest?.comparison;
  const differenceText = !c || c.reason ? '' : c.difference === 0 ? '余额变化与本站记录一致。'
    : `官方余额减少了 ${reconNumber(c.balance_decrease)} Anlas，比本站记录${c.difference>0?'多':'少'} ${reconNumber(Math.abs(c.difference))} Anlas。`;
  $('#reconStatus').hidden=!!latest&&!d.running;
  $('#reconStatus').textContent=d.running?'正在核对…':!latest?'尚无核对记录':'';
  const matched=latest&&!c.reason&&c.difference===0;
  const mismatch=latest&&!c.reason&&c.difference!==0;
  const headline=!latest?'':c.reason?(reconReasons[c.reason]||'已记下当前余额'):matched?'余额变化与本站记录一致':'余额变化与本站记录有差异';
  $('#reconResult').innerHTML=!latest?'':`<div class="gate-recon-summary"><span class="gate-checkmark ${mismatch?'is-warning':matched?'':'is-pending'}"><i data-icon="${matched?'check':'clock'}"></i></span><div><strong>${esc(headline)}</strong><p>${esc(reconTime(latest.checked_at))}</p></div><div class="gate-recon-value"><span>官方余额</span><strong>${reconNumber(latest.balance)} <small>Anlas</small></strong></div></div>`;
  paintIcons($('#reconResult'));
  $('#reconComparisonDetails').innerHTML=!latest?'':c.reason ? `<p>${esc(reconReasons[c.reason] || '已记下当前余额')}。下次查询时，会核对这期间用了多少 Anlas。</p>`
    : `<p class="muted">这次核对的时间段：${esc(reconTime(c.since))} — ${esc(reconTime(latest.checked_at))}</p>
       <div class="cards" style="margin:12px 0">
        ${[['官方余额减少',c.balance_decrease],['本站记录消耗',c.recorded],['相差',c.difference]].map(([label,value])=>
          `<div class="card"><div class="k">${label}</div><div class="v">${reconNumber(value)} <small>Anlas</small></div></div>`).join('')}
       </div><p>${differenceText}</p>${c.unconfirmed_requests>0 ? `<div class="muted">有 ${reconNumber(c.unconfirmed_requests)} 笔失败请求可能产生费用，合计预估 ${reconNumber(c.unconfirmed_anlas)} Anlas。</div>` : ''}`;
  $('#reconHistory').innerHTML=rows.map(row=>{ const x=row.comparison; return `<tr>
    <td>${esc(reconTime(row.checked_at))}</td><td>${reconNumber(row.balance)}</td><td>${reconNumber(x.balance_decrease)}</td>
    <td>${reconNumber(x.recorded)}</td><td>${reconNumber(x.difference)}</td><td>${esc(reconReasons[x.reason] || '核对完成')}</td></tr>`; }).join('')
    || '<tr><td colspan="6" class="muted">尚无记录</td></tr>';
}
async function loadReconciliation(){
  if(reconBusy) return;
  const version=++reconVersion;
  try{
    const d=await api('/admin/api/reconciliation'); // Read local history.
    if(version===reconVersion) renderReconciliation(d);
  }catch(e){ if(version===reconVersion){$('#reconStatus').hidden=false;$('#reconStatus').textContent=e.message;} }
}
async function runReconciliation(){
  if($('#reconRun').disabled) return;
  ++reconVersion; reconBusy=true; updateReconciliationButton();
  $('#reconStatus').hidden=false;
  $('#reconStatus').textContent='正在核对，请稍候…';
  try{
    renderReconciliation(await api('/admin/api/reconciliation', {method:'POST', body:'{}',
      headers:{'Content-Type':'application/json', 'X-NAI-Admin-CSRF':reconCsrf}}));
  }catch(e){
    // Refresh the cooldown after failure, then restore the error message.
    try{ renderReconciliation(await api('/admin/api/reconciliation')); }catch(_){}
    $('#reconStatus').hidden=false; $('#reconStatus').textContent=e.message;
  }finally{ reconBusy=false; updateReconciliationButton(); }
}

function renderAllowance(d){
  const rows = d.accounts || [], box = $('#allowanceStatus');
  const warnings = rows.filter(p=>p.low || p.error);
  box.hidden = warnings.length === 0;
  box.textContent = warnings.map(p=>{
    const value = p.is_negative ? '已耗尽' : `低于 ${d.threshold}%（上次 ${p.percent}%）`;
    return `账号 ${p.account} · ${p.low ? 'V5 余量' + value : ''}${p.low && p.error ? '；' : ''}${p.error ? '额度查询失败，V5 生图需确认后继续' : p.uncertain ? '，待下次生成确认' : ''}`;
  }).join('\n');
  $('#allowanceDetails').textContent = rows.length ? rows.map(p=>{
    const value = p.percent == null ? '未查询 · 首次 V5 生图时确认'
      : `${p.is_negative ? '已耗尽' : p.percent + '%'}${p.uncertain ? '（上次记录）' : ''}`;
    const checked = p.checked_at ? ` · ${new Date(p.checked_at*1000).toLocaleTimeString()} 更新` : '';
    return `账号 ${p.account} · V5 ${value}${checked}`;
  }).join('\n') : 'V5 余量：未配置上游账号';
}
async function loadAllowance(){
  if(allowanceLoading || $('#app').hidden) return;
  allowanceLoading = true;
  try{ renderAllowance(await api('/admin/api/allowance')); }
  catch(e){
    const box = $('#allowanceStatus');
    $('#allowanceDetails').textContent = 'V5 状态暂时无法读取，稍后自动重试。';
    if(!box.hidden && !box.textContent.startsWith('状态刷新失败')) box.textContent = '状态刷新失败，以下告警为上次记录。\n' + box.textContent;
  }finally{ allowanceLoading = false; }
}

function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.style.display = 'block';
  setTimeout(()=>t.style.display='none', 2200);
}
function esc(s){
  return String(s??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function askConfirm(message,{title='确认操作',confirmLabel='确认'}={}){
  const dialog=$('#confirmDialog');
  if(dialog.open) return Promise.resolve(false);
  dialog.returnValue='';
  $('#confirmTitle').textContent=title;
  $('#confirmMessage').textContent=message;
  $('#confirmAccept').textContent=confirmLabel;
  return new Promise(resolve=>{confirmationResolve=resolve;dialog.showModal();});
}
function finishConfirmation(accepted){
  const resolve=confirmationResolve;
  confirmationResolve=null;
  $('#confirmDialog').close(accepted?'confirmed':'cancelled');
  resolve?.(accepted);
}
function showKeyToken(token,title='新 Key'){
  $('#keyTokenTitle').textContent=title;
  $('#keyTokenValue').value=token;
  $('#keyTokenStatus').textContent='';
  $('#keyTokenDialog').returnValue='';
  $('#keyTokenDialog').showModal();
}
function clearKeyToken(){
  $('#keyTokenValue').value='';
  $('#keyTokenStatus').textContent='';
}
function closeKeyToken(){clearKeyToken();$('#keyTokenDialog').close();}
async function copyDisplayedKey(){
  const input=$('#keyTokenValue');
  if(!input.value) return;
  try{
    if(!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText(input.value);
    $('#keyTokenStatus').textContent='已复制';
  }catch(_error){
    $('#keyTokenStatus').textContent='请选中后手动复制';
    input.focus();input.select();
  }
}
function relativeTime(timestamp){
  if(!timestamp) return '从未使用';
  const seconds=Math.max(0,Math.floor(Date.now()/1000-timestamp));
  if(seconds<60) return '刚刚';
  if(seconds<3600) return `${Math.floor(seconds/60)} 分钟前`;
  if(seconds<86400) return `${Math.floor(seconds/3600)} 小时前`;
  return `${Math.floor(seconds/86400)} 天前`;
}
async function api(path, opts={}){
  const r = await fetch(path, {headers:{'Content-Type':'application/json'}, credentials:'same-origin', ...opts});
  if(r.status === 401 && path !== '/admin/api/login'){ showLogin(); throw new Error('未登录'); }
  const data = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error?.message || data.detail || ('HTTP '+r.status));
  return data;
}

/* ---------- auth ---------- */
async function doLogin(){
  $('#loginErr').textContent = '';
  try{
    await api('/admin/api/login', {method:'POST', body: JSON.stringify({password: $('#pwd').value})});
    if(!await boot()){
      $('#loginErr').textContent = location.protocol === 'http:'
        ? '登录状态未能保存。请使用 HTTPS，或由管理员开启 HTTP 登录兼容；同时检查浏览器 Cookie 设置。'
        : '登录状态未能保存，请检查浏览器是否允许本站 Cookie。';
    }
  }catch(e){ $('#loginErr').textContent = e.message; }
}
async function logout(){
  await api('/admin/api/logout', {method:'POST'}).catch(()=>{});
  showLogin();
}
function showLogin(){
  reconCsrf=''; reconReady=false; ++reconVersion;
  configurationCsrf=''; configurationSnapshot=null; closeUpstreamDialog();
  closeKeyToken(); finishConfirmation(false);
  clearInterval(allowanceTimer); allowanceTimer = null;
  clearInterval(reconTimer); reconTimer = null;
  $('#login').hidden=false; $('#app').hidden=true; closeModal();
}
async function boot(){
  try{ await api('/admin/api/me'); }catch(e){ return false; }
  $('#login').hidden=true; $('#app').hidden=false; $('#pwd').value='';
  clearInterval(allowanceTimer);
  loadAllowance();
  allowanceTimer = setInterval(()=>{ if(!document.hidden) loadAllowance(); }, 30000);
  const initialTab=Object.keys(pageRoutes).find(key=>pageRoutes[key]===location.hash.slice(1)) || 'ov';
  tab(initialTab);
  if(initialTab!=='ov') loadUpstreamConnection();
  return true;
}

/* ---------- tabs ---------- */
async function tab(name){
  const version=++viewVersion;
  document.querySelectorAll('.settings-page').forEach(page=>page.getAnimations().forEach(animation=>animation.cancel()));
  currentTab=name;
  document.querySelectorAll('.tabs button').forEach(button=>{
    const selected=button.dataset.tab===name;
    button.classList.toggle('active',selected);
    if(selected) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
  ['ov','keys','logs','set'].forEach(t=>{$('#tab-'+t).hidden=name==='ov'||t!==name;});
  $('#pageTitle').textContent=pageTitles[name];
  history.replaceState(null,'','#'+pageRoutes[name]);
  document.querySelector('.gate-main').scrollTo({top:0,behavior:'instant'});
  window.scrollTo({top:0,behavior:'instant'});
  if(name==='ov'){
    await loadOverview(version);
    return;
  }
  if(name==='keys') return loadKeys();
  if(name==='logs') return loadKeys().then(loadLogs);
  if(name==='set') return loadSettings();
}
async function refreshCurrentView(){
  $('#refreshView').disabled=true;
  try{
    if(currentTab==='ov') await loadOverview();
    if(currentTab==='keys') await loadKeys();
    if(currentTab==='logs') await loadLogs();
    if(currentTab==='set') await loadSettings();
  }finally{$('#refreshView').disabled=false;}
}

/* ---------- overview ---------- */
function renderUpstreamConnection(accounts){
  const available=accounts.some(account=>account.usable);
  $('#upstreamConnectionText').textContent=!accounts.length?'未配置上游':available?'上游可用':'上游暂不可用';
  $('#upstreamConnection').classList.toggle('is-unavailable',!available);
}
async function loadUpstreamConnection(){
  try{renderUpstreamConnection((await api('/admin/api/upstream-tokens')).accounts);}
  catch(_error){$('#upstreamConnectionText').textContent='状态暂不可用';$('#upstreamConnection').classList.add('is-unavailable');}
}
async function loadOverview(version=viewVersion){
  const requestVersion=++overviewRequestVersion;
  loadReconciliation();
  try{
    const [d,settings] = await Promise.all([api('/admin/api/overview'),api('/admin/api/settings')]);
    if(version!==viewVersion || requestVersion!==overviewRequestVersion) return;
    const t=d.today;
    $('#ovCards').innerHTML=[
      ['今日生成',t.images,'张','image'],['今日 Anlas',t.anlas,'Anlas','chart'],
      ['今日 V5 用量',t.v5||0,'单位','brush'],['启用中的 Key',d.keys_active,'/ '+d.keys_total,'key']
    ].map(([title,value,unit,icon])=>`<article class="gate-stat"><div class="gate-stat-title">${title}<i data-icon="${icon}"></i></div><strong>${value}<span>${unit}</span></strong></article>`).join('');
    $('#keyNavCount').textContent=d.keys_total;
    const maximum=Math.max(10,Math.ceil(Math.max(...d.week.map(w=>w.images),0)/10)*10);
    $('#chartDate').textContent=d.week.length?d.week[0].day.slice(5).replace('-','.')+' — '+d.week[d.week.length-1].day.slice(5).replace('-','.') : '';
    $('#chart').setAttribute('aria-label',d.week.map(w=>`${w.day}：${w.images} 张`).join('；'));
    $('#chart').innerHTML=`<div class="gate-chart-guides"><span>${maximum}</span><span>${maximum/2}</span><span>0</span></div><div class="gate-chart-bars">${d.week.map((w,index)=>`<div class="${index===d.week.length-1?'is-today':''}" title="${esc(w.day)}：${w.images} 张"><span class="gate-bar-value">${w.images}</span><span class="gate-bar" style="--bar-height:${w.images/maximum*100}%"></span><small>${index===d.week.length-1?'今天':w.day.slice(-2)+' 日'}</small></div>`).join('')}</div>`;
    const limited=d.v5_limit>0, remaining=Math.max(0,d.v5_limit-(t.v5||0)), percent=limited?Math.min(100,remaining/d.v5_limit*100):0;
    $('#v5Budget').innerHTML=`<div class="gate-budget-number">${limited?Number(remaining.toFixed(2)):'不限'}<span>${limited?' / '+d.v5_limit:''}</span></div>${limited?`<div class="gate-meter"><span style="width:${percent}%"></span></div>`:''}<p class="gate-budget-caption"><span>${limited?'今日剩余':'今日已用'}</span><strong>${limited?percent.toFixed(1)+'%':(t.v5||0)+' 单位'}</strong></p><div class="gate-budget-foot"><span class="gate-status-dot"></span><p>${settings.v5_auto_medium?'自动节省已开启':'自动节省已关闭'}${settings.v5_auto_medium?`<small>≤ ${settings.v5_medium_threshold}% 时切换 Medium</small>`:''}</p></div>`;
    renderUpstreamConnection(d.pool);
    $('#pool').innerHTML=!d.pool_configured?'<p class="empty-state">尚未配置上游账号</p>':`<table class="gate-pool-table gate-mobile-table"><thead><tr><th>上游账号</th><th>状态</th><th>免费 V5 每日上限</th><th>图片并发</th><th class="gate-align-right">操作</th></tr></thead><tbody>${d.pool.map(p=>`<tr><td><div class="gate-name-cell"><span class="gate-account-icon"><i data-icon="key"></i></span><div><strong>账号 ${String(p.position).padStart(2,'0')}</strong><small>${esc(p.token)}</small></div></div></td><td><span class="tag ${p.usable?'on':'off'}"><b></b>${!p.admin_enabled?'已停用':p.disabled?'已失效':p.blocked_for>0?'限流中':'可用'}</span></td><td data-label="每日 V5 上限"><label class="gate-inline-number"><input id="token-limit-${p.token_id}" type="number" min="0" max="100000" step="1" value="${p.v5_daily_limit}" aria-label="账号 ${p.position} 的 V5 日限额"><span>单位</span></label><small class="gate-mobile-field-note">0 = 不限</small></td><td data-label="图片并发"><select class="gate-compact-select" id="token-concurrency-${p.token_id}" aria-label="账号 ${p.position} 的图片并发">${[1,2,3,4].map(n=>`<option ${n===p.image_concurrency?'selected':''}>${n}</option>`).join('')}</select></td><td><div class="row-actions"><button class="btn gate-row-button" onclick="saveOverviewAccount('${p.token_id}',this)">保存</button><span class="gate-account-toggle"><button class="gate-switch ${p.admin_enabled?'is-on':''}" role="switch" aria-label="启用账号 ${p.position}" aria-checked="${p.admin_enabled}" onclick="setUpstreamEnabled('${p.token_id}',${!p.admin_enabled})"><span></span></button><span class="gate-mobile-label" aria-hidden="true">启用</span></span></div></td></tr>`).join('')}</tbody></table>`;
    $('#billingStatus').hidden=!(t.unconfirmed_requests>0);
    $('#billingStatus').textContent=`今日有 ${t.unconfirmed_requests||0} 笔费用待确认，预估 ${t.unconfirmed_anlas||0} Anlas。`;
    $('#usageDetails').innerHTML=`<dl><div><dt>本月 Anlas</dt><dd>${d.month.anlas} / ${d.anlas_budget>0?d.anlas_budget:'不限'}</dd></div><div><dt>今日文本 Token</dt><dd>${fmt(t.text_tokens)}</dd></div><div><dt>今日请求数</dt><dd>${t.requests}</dd></div><div><dt>本月待确认费用</dt><dd>${d.month.unconfirmed_anlas||0} Anlas · ${d.month.unconfirmed_requests||0} 笔</dd></div></dl>`;
    paintIcons($('#tab-ov'));
  }catch(e){
    if(version===viewVersion && requestVersion===overviewRequestVersion) toast(e.message);
  }finally{
    if(version===viewVersion && requestVersion===overviewRequestVersion && currentTab==='ov') $('#tab-ov').hidden=false;
  }
}
async function saveOverviewAccount(id,button){
  const limit=Number($('#token-limit-'+id).value), concurrency=Number($('#token-concurrency-'+id).value);
  if(!Number.isInteger(limit)||limit<0||limit>100000){toast('V5 日限额须为 0～100000 的整数');return;}
  button.disabled=true;
  try{
    await api('/admin/api/upstream-tokens/'+id+'/v5-limit',{method:'PUT',body:JSON.stringify({v5_daily_limit:limit})});
    await api('/admin/api/upstream-tokens/'+id+'/image-concurrency',{method:'PUT',body:JSON.stringify({image_concurrency:concurrency})});
    toast('账号设置已保存');
  }catch(e){toast(e.message);}
  finally{await loadOverview();button.disabled=false;}
}
async function setUpstreamEnabled(tokenId, enabled){
  if(!enabled && !await askConfirm('停用后不再接收新请求，已发出的请求会继续完成。',{title:'停用上游账号',confirmLabel:'停用'})) return;
  try{
    await api('/admin/api/upstream-tokens/' + tokenId + '/enabled', {
      method:'PUT', body:JSON.stringify({enabled})});
    toast(enabled?'上游 Key 已启用（401 失效仍需更换 Token）':'上游 Key 已停用');
    loadOverview();
  }catch(e){ toast(e.message); }
}
function fmt(n){ n=+n||0; return n>=1e6? (n/1e6).toFixed(1)+'M' : n>=1e3? (n/1e3).toFixed(1)+'k' : n; }

/* ---------- keys ---------- */
async function loadKeys(){
  try{
    const d=await api('/admin/api/keys');
    keysCache=d.keys;
    renderKeys();
    const selected=$('#logFilter').value;
    $('#logFilter').innerHTML='<option value="">全部 Key</option>'+d.keys.map(k=>`<option value="${k.id}">${esc(k.name)}</option>`).join('');
    if(d.keys.some(k=>String(k.id)===selected)) $('#logFilter').value=selected;
  }catch(e){toast(e.message);}
}
function renderKeys(){
  const query=$('#keySearch').value.trim().toLowerCase(), status=$('#keyStatusFilter').value;
  const rows=keysCache.filter(k=>k.name.toLowerCase().includes(query)&&(status==='all'||k.enabled===(status==='on')));
  $('#keysTable tbody').innerHTML=rows.map(k=>`<tr>
    <td><div class="gate-name-cell"><div><strong title="${esc(k.name)}">${esc(k.name)}</strong><small><button class="gate-copy" onclick="copyKey(${k.id})" aria-label="复制 ${esc(k.name)} 的 Key">${esc(mask(k.token))}</button></small></div></div></td>
    <td><span class="tag ${k.enabled?'on':'off'}"><b></b>${k.enabled?'已启用':'已停用'}</span></td>
    <td data-label="今日生成">${k.used.images} 张</td><td data-label="今日用量"><div class="gate-key-metric"><span>${k.used.anlas} Anlas</span><small>${k.image_model_scope==='all'?(k.used.v5||0)+' V5':''}</small></div></td>
    <td data-label="每日配额"><div class="gate-key-metric"><span>${k.is_admin?'不限额':k.image_model_scope==='all'&&!k.daily_images&&!k.daily_v5?'图片、V5 不限':k.daily_images?k.daily_images+' 张':'图片不限'}</span><small>${!k.is_admin&&k.image_model_scope==='all'&&(k.daily_images||k.daily_v5)?(k.daily_v5?k.daily_v5+' V5':'V5 不限'):''}</small></div></td>
    <td data-label="最近使用">${relativeTime(k.last_used_at)}</td>
    <td><div class="row-actions"><button class="btn gate-row-button" onclick="openEdit(${k.id})">编辑</button><button class="btn gate-row-button is-positive" title="重置今日 V5 / Anlas" aria-label="重置 ${esc(k.name)} 今日 V5 / Anlas" onclick="resetDailyImageQuota(${k.id},this)">重置</button><button class="btn gate-row-button ${k.enabled?'is-danger':'is-muted'}" onclick="toggleKey(${k.id},${k.enabled?0:1})">${k.enabled?'停用':'启用'}</button></div></td>
  </tr>`).join('')||'<tr><td colspan="7" class="empty-state">没有符合条件的 Key</td></tr>';
  $('#keyCount').textContent=query||status!=='all'?`显示 ${rows.length} / ${keysCache.length} 个 Key`:`共 ${keysCache.length} 个 Key`;
  $('#keyNavCount').textContent=keysCache.length;
}
function mask(token){return token.slice(0,8)+'••••'+token.slice(-4);}
function copyKey(id){
  const token=keysCache.find(key=>key.id===id)?.token;
  if(!token) return;
  if(!navigator.clipboard?.writeText){showKeyToken(token,'复制 Key');return;}
  navigator.clipboard.writeText(token).then(()=>toast('已复制'),()=>showKeyToken(token,'复制 Key'));
}
async function openCreate(){
  if(keySaveBusy) return;
  try{
    const data=await fetchConfiguration(), defaults=data.values;
    editingId = null;
    $('#keyOperations').hidden=true; $('#keyUsageDetail').hidden=true; $('#expiryLabel').textContent='有效天数 · 0 为长期';
    $('#modalTitle').textContent = '新建 Key';
    $('#modalSave').textContent = '创建 Key';
    $('#f_name').value='';
    const fields={daily_anlas:'default_daily_anlas',image_model_scope:'default_image_model_scope',
      daily_images:'default_daily_images',daily_v5:'default_daily_v5',monthly_anlas:'default_monthly_anlas',
      daily_text:'default_daily_text_tokens',rpm:'default_rpm',expires:'default_expires_days'};
    for(const [field,name] of Object.entries(fields)) $('#f_'+field).value=defaults[name];
    $('#f_expires').placeholder=''; $('#f_expires').required=true;
    $('#f_allow_anlas').checked=defaults.default_allow_anlas;
    $('#f_allow_img2img').checked=defaults.default_allow_img2img;
    $('#f_exclude_global_v5').checked=defaults.default_exclude_global_v5;
    $('#modalErr').textContent=''; $('#modal').showModal();
  }catch(e){toast(e.message);}
}
function openEdit(id){
  if(keySaveBusy) return;
  const k = keysCache.find(x=>x.id===id); if(!k) return;
  editingId = id;
  $('#keyOperations').hidden=false; $('#keyUsageDetail').hidden=false;
  $('#expiryLabel').textContent='续期天数 · '+(k.expires_at?new Date(k.expires_at*1000).toLocaleDateString('zh-CN')+' 到期':'长期有效');
  $('#keyUsageDetail').innerHTML=`<details><summary>用量详情</summary><p>累计生成 ${k.used.generated_images_total||0} 张 · 今日免费图 ${k.used.legacy_free_images||0} 张 · 文本 ${fmt(k.used.text_tokens)} Token${k.is_admin?' · 管理员 Key':''}</p></details>`;
  $('#keyOperations').innerHTML=`<button type="button" class="btn gate-row-button" onclick="regenerateKey(${id},this)">重新生成 Key</button><button type="button" class="btn gate-row-button is-danger" onclick="delKey(${id},this)">删除 Key</button>`;
  $('#modalTitle').textContent = '编辑 Key';
  $('#modalSave').textContent = '保存修改';
  $('#f_name').value = k.name;
  $('#f_image_model_scope').value = k.image_model_scope || 'legacy';
  $('#f_daily_anlas').value = k.daily_anlas;
  $('#f_daily_images').value = k.daily_images;
  $('#f_daily_v5').value = k.daily_v5;
  $('#f_monthly_anlas').value = k.monthly_anlas;
  $('#f_daily_text').value = k.daily_text_tokens;
  $('#f_rpm').value = k.rpm;
  $('#f_expires').value = '';
  $('#f_expires').placeholder = '留空不修改'; $('#f_expires').required=false;
  $('#f_allow_anlas').checked = k.allow_anlas;
  $('#f_allow_img2img').checked = k.allow_img2img;
  $('#f_exclude_global_v5').checked = k.exclude_global_v5;
  $('#modalErr').textContent=''; $('#modal').showModal();
}
function closeModal(){ $('#modal').close(); }
async function saveModal(){
  if(keySaveBusy || !$('#keyForm').reportValidity()) return;
  keySaveBusy=true; $('#modalSave').disabled=true; $('#modalErr').textContent='';
  const body = {
    name: $('#f_name').value,
    image_model_scope: $('#f_image_model_scope').value,
    daily_anlas: +$('#f_daily_anlas').value,
    daily_images: +$('#f_daily_images').value,
    daily_v5: +$('#f_daily_v5').value,
    monthly_anlas: +$('#f_monthly_anlas').value,
    daily_text_tokens: +$('#f_daily_text').value,
    rpm: +$('#f_rpm').value,
    allow_anlas: $('#f_allow_anlas').checked,
    allow_img2img: $('#f_allow_img2img').checked,
    exclude_global_v5: $('#f_exclude_global_v5').checked,
  };
  try{
    if(editingId){
      if($('#f_expires').value.trim() !== '') body.expires_days = +$('#f_expires').value;
      await api('/admin/api/keys/'+editingId, {method:'PATCH', body: JSON.stringify(body)});
      toast('已保存');
    }else{
      body.expires_days = +$('#f_expires').value;
      const d = await api('/admin/api/keys', {method:'POST', body: JSON.stringify(body)});
      await loadKeys(); closeModal();
      showKeyToken(d.key.token,'新 Key');
      return;
    }
    closeModal(); loadKeys();
  }catch(e){ $('#modalErr').textContent = e.message; }
  finally{keySaveBusy=false;$('#modalSave').disabled=false;}
}
async function toggleKey(id, enabled){
  await api('/admin/api/keys/'+id, {method:'PATCH', body: JSON.stringify({enabled})}).catch(e=>toast(e.message));
  loadKeys();
}
const regeneratingKeys = new Set();
async function regenerateKey(id, button){
  const key = keysCache.find(k=>k.id===id);
  if(!key || regeneratingKeys.has(id)) return;
  if(!await askConfirm(`重新生成“${key.name}”后，旧 Key 将失效。配额和历史用量保留。`,{title:'重新生成 Key',confirmLabel:'重新生成'})) return;
  regeneratingKeys.add(id); button.disabled = true;
  try{
    const data = await api('/admin/api/keys/'+id+'/regenerate', {method:'POST'});
    key.token = data.token;
    showKeyToken(data.token,'新的 Key');
    toast('Key 已重新生成');
  }catch(e){ toast(e.message); }
  finally{
    await loadKeys();
    regeneratingKeys.delete(id); button.disabled = false;
  }
}
async function resetDailyImageQuota(id,button){
  const key = keysCache.find(k=>k.id===id); if(!key) return;
  const name = key.name;
  if(!await askConfirm(`仅重置“${name}”今日 V5 和 Anlas 用量，保留生成张数和日志。`,{title:'重置今日额度',confirmLabel:'重置额度'})) return;
  button.disabled=true;
  try{
    await api('/admin/api/keys/'+id+'/reset-daily-image-quota', {method:'POST'});
    toast('已重置「'+name+'」今日的 V5 和 Anlas 用量');
    loadKeys();
  }catch(e){ toast(e.message); }
  finally{button.disabled=false;}
}
async function delKey(id,button){
  const key = keysCache.find(k=>k.id===id); if(!key) return;
  const name = key.name;
  if(!await askConfirm(`删除“${name}”后将无法恢复。`,{title:'删除 Key',confirmLabel:'删除 Key'})) return;
  button.disabled=true;
  try{await api('/admin/api/keys/'+id, {method:'DELETE'});closeModal();await loadKeys();}
  catch(e){toast(e.message);}
  finally{button.disabled=false;}
}

/* ---------- logs ---------- */
const modelNames={
  'nai-diffusion-5':'V5','nai-v5':'V5','nai-diffusion-5-full':'V5 Full',
  'nai-diffusion-5-curated':'V5 Curated','nai-diffusion-5-full-medium':'V5 Medium',
  'nai-diffusion-4-5':'V4.5','nai-diffusion-4-5-full':'V4.5 Full',
  'nai-diffusion-4-5-curated':'V4.5 Curated',
  'nai-diffusion-4-5-full-inpainting':'V4.5 Full · 局部重绘',
  'nai-diffusion-4-5-curated-inpainting':'V4.5 Curated · 局部重绘',
  'nai-diffusion-4':'V4','nai-diffusion-4-full':'V4 Full','nai-diffusion-4-curated':'V4 Curated',
  'nai-diffusion-4-curated-preview':'V4 Curated Preview',
  'nai-diffusion-4-full-inpainting':'V4 Full · 局部重绘',
  'nai-diffusion-4-curated-inpainting':'V4 Curated · 局部重绘',
  'nai-diffusion-3':'V3','nai-diffusion-3-inpainting':'V3 · 局部重绘',
  'nai-diffusion-furry-3':'V3 Furry','nai-diffusion-furry-3-inpainting':'V3 Furry · 局部重绘',
  'nai-diffusion-2':'V2','llama-3-erato-v1':'Erato','kayra-v1':'Kayra','clio-v1':'Clio',
  'nai-glm-4-6':'GLM 4.6','nai-xialong':'Xialong',
};
function logStatus(status){
  return status==='ok'?{label:'成功',style:'on'}:status==='rejected'?{label:'已拒绝',style:'warn'}:{label:'错误',style:'warn'};
}
async function loadLogs(){
  try{
    const kf = $('#logFilter').value;
    const d = await api('/admin/api/logs?' + (kf?`key_id=${kf}&`:'') + `page=${logPage}`);
    logPage = d.page;
    $('#logBody').innerHTML=d.logs.map(entry=>{
      const date=new Date(entry.ts*1000), status=logStatus(entry.status), detail=String(entry.detail||'');
      const kindNames={image:'图片',text:'文本',voice:'语音',tags:'标签',chat:'聊天'};
      const kind=Object.hasOwn(kindNames,entry.kind)?kindNames[entry.kind]:entry.kind;
      return `<tr><td data-label="时间" title="${esc(date.toLocaleString('zh-CN'))}">${esc(date.toLocaleTimeString('zh-CN',{hour12:false}))}<span class="gate-table-sub">${date.getMonth()+1} 月 ${date.getDate()} 日</span></td>
        <td title="${esc(entry.key_name)}">${esc(entry.key_name)}</td><td title="${esc(entry.model)}"><span class="gate-log-model-name">${esc(Object.hasOwn(modelNames,entry.model)?modelNames[entry.model]:entry.model)}</span><span class="gate-table-sub">${esc(kind)}${entry.tokens>0?' · '+esc(fmt(entry.tokens))+' Token':''}</span></td>
        <td><span class="tag ${status.style}"><b></b>${status.label}</span></td><td data-label="图片">${esc(entry.images??'—')}</td><td data-label="Anlas">${esc(entry.anlas??'—')}${entry.unconfirmed_anlas>0?'<span class="gate-table-sub log-pending">待确认 '+esc(entry.unconfirmed_anlas)+'</span>':''}</td>
        <td class="gate-log-note${detail.trim()?'':' is-empty'}">${esc(detail||'—')}</td></tr>`;
    }).join('')||'<tr><td colspan="7" class="empty-state">暂无记录</td></tr>';
    $('#logPager').innerHTML=`<span>共 ${d.total} 条</span><div>
      <button type="button" class="icon-button" aria-label="上一页" onclick="changeLogPage(-1)" ${d.page<=1?'disabled':''}><i data-icon="chevron-left"></i></button>
      <span>${d.page} / ${d.pages}</span>
      <button type="button" class="icon-button" aria-label="下一页" onclick="changeLogPage(1)" ${d.page>=d.pages?'disabled':''}><i data-icon="chevron-left" style="transform:rotate(180deg)"></i></button></div>`;
    paintIcons($('#logPager'));
  }catch(e){ toast(e.message); }
}
function changeLogPage(delta){ logPage=Math.max(1,logPage+delta); loadLogs(); }

/* ---------- settings ---------- */
function settingsPage(name){
  if(!$('#settings-'+name)) return;
  const changed=currentSettingsPage!==name; currentSettingsPage=name;
  document.querySelectorAll('.settings-page').forEach(page=>page.getAnimations().forEach(animation=>animation.cancel()));
  document.querySelectorAll('.settings-page').forEach(page=>{page.hidden=page.id!=='settings-'+name;});
  document.querySelectorAll('.settings-nav button').forEach(button=>{
    const selected=button.dataset.settingPage===name;
    button.classList.toggle('active',selected);
    if(selected) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
  if(changed&&currentTab==='set'&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
    $('#settings-'+name).animate([{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'translateY(0)'}],{duration:220,easing:'cubic-bezier(.22,1,.36,1)'});
  }
}
function renderConfigurationControls(){
  for(const group of ['generation','defaults','advanced']){
    $('#config-'+group).innerHTML=configurationFields.filter(field=>field.group===group).map(field=>{
      const id='c_'+field.name;
      const control=field.type==='bool'
        ? `<div class="setting-check"><input id="${id}" type="checkbox"></div>`
        : field.type==='scope'
        ? `<select id="${id}"><option value="legacy">V4.5 及以下</option><option value="all">全部当前模型（含 V5）</option></select>`
        : `<div class="setting-control"><input id="${id}" type="number" min="${field.min}" max="${field.max}" step="${field.step||1}" required>${field.unit?`<span>${esc(field.unit)}</span>`:''}</div>`;
      return `<div class="setting-row"><label for="${id}">${esc(field.label)}${field.note?`<small>${esc(field.note)}</small>`:''}</label>${control}</div>`;
    }).join('');
  }
}
async function fetchConfiguration(){
  const data=await api('/admin/api/configuration');
  configurationCsrf=data.csrf_token;
  return data;
}
function applyConfiguration(data,group='all'){
  configurationSnapshot=data;
  configurationCsrf=data.csrf_token;
  for(const field of configurationFields){
    if(group!=='all' && field.group!==group) continue;
    const input=$('#c_'+field.name), value=data.values[field.name];
    if(field.group==='cleanup'){
      $('#cleanupEnabled').checked=value>0;
      input.value=value>0?value:3;
      input.disabled=value===0;
      clearCleanupPreview();
    }else if(field.type==='bool') input.checked=value;
    else input.value=value;
  }
  renderConfigurationSummary();
}
function configurationValue(field,value){
  if(value===undefined || value===null) return '未读取';
  if(field.type==='bool') return value?'开启':'关闭';
  if(field.type==='scope') return value==='all'?'全部当前模型（含 V5）':'V4.5 及以下';
  if(field.name==='key_inactivity_delete_days' && value===0) return '关闭';
  if(field.name==='default_expires_days' && value===0) return '永久';
  return String(value)+(field.unit?' '+field.unit:'');
}
function renderConfigurationSummary(){
  const sourceNames={panel:'后台保存',environment:'服务器配置',default:'默认值'};
  const rows=[];
  const addRow=(field,value,source,mode)=>rows.push(`<tr><td>${field.name.startsWith('default_')?'新 Key · ':''}${esc(field.label)}</td><td>${esc(configurationValue(field,value))}</td><td>${esc(value===undefined||value===null?'未读取':source||'未提供')}</td><td>${esc(mode)}</td></tr>`);
  for(const field of budgetFields) addRow(field,budgetSnapshot?.[field.name],'服务配置','立即');
  for(const group of ['generation','defaults','cleanup','advanced']){
    for(const field of configurationFields.filter(item=>item.group===group)){
      addRow(field,configurationSnapshot?.values[field.name],sourceNames[configurationSnapshot?.sources[field.name]],group==='defaults'?'新建 Key':group==='cleanup'?'定时检查':'立即');
    }
    if(group==='generation') for(const field of runtimeFields) addRow(field,runtimeSnapshot?.[field.name],'服务配置','新请求');
  }
  rows.push(`<tr><td>上游账号</td><td>${upstreamSource===null?'未读取':upstreamAccounts.length+' 个'}</td><td>${esc(upstreamSource===null?'未读取':sourceNames[upstreamSource]||'未提供')}</td><td>立即</td></tr>`);
  rows.push(`<tr><td>站点公告</td><td>${announcementSnapshot===null?'未读取':announcementSnapshot.trim()?'已填写':'未填写'}</td><td>${announcementSnapshot===null?'未读取':'后台设置'}</td><td>立即</td></tr>`);
  $('#cfgInfo').innerHTML=`<table><thead><tr><th>设置</th><th>当前值</th><th>来源</th><th>生效方式</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
}
async function configurationWrite(path,method,body){
  if(!configurationCsrf) await fetchConfiguration();
  return api(path,{method,headers:{'Content-Type':'application/json','X-NAI-Admin-CSRF':configurationCsrf},body:JSON.stringify(body)});
}
function readConfigurationGroup(group){
  const body={};
  for(const field of configurationFields.filter(item=>item.group===group)){
    const input=$('#c_'+field.name);
    if(group==='cleanup' && !$('#cleanupEnabled').checked){body[field.name]=0;continue;}
    if(!input.checkValidity()){input.reportValidity();return null;}
    body[field.name]=field.type==='bool'?input.checked:field.type==='scope'?input.value:Number(input.value);
  }
  return body;
}
function restoreGenerationDefaults(){
  for(const field of configurationFields.filter(item=>item.group==='generation')){
    const input=$('#c_'+field.name);
    if(field.type==='bool') input.checked=generationDefaults[field.name];
    else input.value=generationDefaults[field.name];
  }
  for(const field of runtimeFields) $('#r_'+field.name).value=generationDefaults[field.name];
  toast('已填入默认值，保存后生效');
}
async function saveConfigurationGroup(group){
  if(!configurationSnapshot){toast('设置尚未读取，请刷新设置页');return;}
  const body=readConfigurationGroup(group);
  if(!body) return;
  const runtime=group==='generation'?readRuntimeLimits():null;
  if(group==='generation' && !runtime) return;
  const button=document.querySelector(`[data-save-config="${group}"]`);
  button.disabled=true;
  let configurationSaved=false;
  try{
    if(group==='cleanup'){
      const preview=await previewCleanup();
      if(!preview) return;
      if(body.key_inactivity_delete_days>0 && body.key_inactivity_delete_days!==configurationSnapshot.values.key_inactivity_delete_days &&
         !await askConfirm(`连续 ${body.key_inactivity_delete_days} 天未使用的 Key 将被永久删除。当前符合条件：${preview.count} 个。`,{title:'启用自动清理',confirmLabel:'保存清理设置'})) return;
    }
    applyConfiguration(await configurationWrite('/admin/api/configuration','PUT',body),group);
    configurationSaved=true;
    if(group==='generation') applyRuntimeLimits(await api('/admin/api/runtime-limits',{method:'PUT',body:JSON.stringify(runtime)}));
    toast('设置已保存');
  }catch(e){toast(group==='generation'&&configurationSaved?'生成策略已保存；排队与冷却保存未完成：'+e.message:e.message);}
  finally{button.disabled=false;}
}
function clearCleanupPreview(){
  $('#cleanupPreview').textContent='';
}
function updateCleanupControls(){
  $('#c_key_inactivity_delete_days').disabled=!$('#cleanupEnabled').checked;
  clearCleanupPreview();
}
async function previewCleanup(){
  const values=readConfigurationGroup('cleanup');
  if(!values) return null;
  try{
    const preview=await api('/admin/api/key-cleanup-preview?days='+values.key_inactivity_delete_days);
    $('#cleanupPreview').innerHTML=!preview.days?'按当前选择，不清理 Key。':!preview.count?'当前没有符合条件的 Key。':
      `<p>当前符合条件：${preview.count} 个 Key</p><div class="settings-table"><table><thead><tr><th>名称</th><th>最近使用</th></tr></thead><tbody>${preview.keys.map(key=>
        `<tr><td>${esc(key.name)}</td><td>${key.last_used_at?esc(new Date(key.last_used_at*1000).toLocaleString()):'从未使用（创建于 '+esc(new Date(key.created_at*1000).toLocaleDateString())+'）'}</td></tr>`
      ).join('')}</tbody></table></div>`;
    return preview;
  }catch(e){$('#cleanupPreview').textContent=e.message;return null;}
}
async function loadSettings(){
  try{
    const [a, s, r, c, accounts] = await Promise.all([
      api('/admin/api/announcement'), api('/admin/api/settings'),
      api('/admin/api/runtime-limits'), fetchConfiguration(), api('/admin/api/upstream-tokens')]);
    announcementSnapshot=typeof a.html==='string'?a.html:null;
    budgetSnapshot=s;
    $('#announce').value = a.html || '';
    $('#s_budget').value = s.global_monthly_anlas;
    $('#s_v5').value = s.global_daily_v5;
    $('#s_v5_alert').value = s.v5_alert_threshold;
    $('#s_auto_medium').checked = s.v5_auto_medium;
    $('#s_medium_threshold').value = s.v5_medium_threshold;
    $('#s_medium_multiplier').value = s.v5_medium_multiplier;
    syncBudgetFields();
    applyRuntimeLimits(r);
    applyConfiguration(c);
    renderUpstreamAccounts(accounts.accounts,accounts.source);
  }catch(e){ toast(e.message); }
}
async function saveBudget(){
  for(const input of document.querySelectorAll('#settings-budget input')){
    if(!input.checkValidity()){input.reportValidity();return;}
  }
  try{
  const d = await api('/admin/api/settings', {method:'PUT', body: JSON.stringify({
    global_monthly_anlas: +$('#s_budget').value,
    global_daily_v5: +$('#s_v5').value,
    v5_alert_threshold: +$('#s_v5_alert').value,
    v5_auto_medium: $('#s_auto_medium').checked,
    v5_medium_threshold: +$('#s_medium_threshold').value,
    v5_medium_multiplier: +$('#s_medium_multiplier').value,
  })});
  budgetSnapshot=d;
  renderConfigurationSummary();
  toast('设置已保存');
  loadAllowance();
  }catch(e){toast(e.message);}
}
function readRuntimeLimits(){
  const body = {};
  for(const field of runtimeFields){
    const input=$('#r_'+field.name);
    if(!input.checkValidity()){input.reportValidity();return null;}
    body[field.name]=Number(input.value);
  }
  return body;
}
function applyRuntimeLimits(values){
  runtimeSnapshot=values;
  for(const field of runtimeFields) $('#r_'+field.name).value=values[field.name];
  renderConfigurationSummary();
}
function syncBudgetFields(){
  const enabled=$('#s_auto_medium').checked;
  $('#s_medium_threshold').disabled=!enabled;
}
async function saveAnnounce(){
  try{
  await api('/admin/api/announcement', {method:'PUT', body: JSON.stringify({html: $('#announce').value})});
  const saved=await api('/admin/api/announcement');
  announcementSnapshot=typeof saved.html==='string'?saved.html:null;
  $('#announce').value=saved.html;
  renderConfigurationSummary();
  toast('设置已保存');
  }catch(e){toast(e.message);}
}

/* ---------- managed upstream credentials ---------- */
function renderUpstreamAccounts(accounts,source=upstreamSource){
  upstreamAccounts=accounts;
  upstreamSource=source;
  renderUpstreamConnection(accounts);
  $('#settingsUpstreamAccounts').innerHTML=accounts.length?accounts.map(account=>{
    const id=account.token_id, unavailable=account.admin_enabled || account.busy;
    const status=!account.admin_enabled?'已停用':account.disabled?'Token 已失效':account.blocked_for>0?'限流中':'已启用';
    return `<article class="settings-account">
      <div class="gate-account-heading"><div><strong>账号 ${String(account.position).padStart(2,'0')}</strong><small>${esc(account.token)}</small></div><span class="tag ${account.usable?'on':'off'}"><b></b>${status}</span></div>
      <div class="gate-setting-row"><label class="gate-setting-copy" for="account-v5-${id}">免费 V5 每日上限<small>0 = 不限</small></label><div class="gate-setting-control"><div class="gate-input-unit"><input id="account-v5-${id}" type="number" min="0" max="100000" step="1" required value="${account.v5_daily_limit}"><span>单位</span></div></div></div>
      <div class="gate-setting-row"><label class="gate-setting-copy" for="account-concurrency-${id}">图片并发</label><div class="gate-setting-control"><select id="account-concurrency-${id}">${[1,2,3,4].map(n=>`<option value="${n}" ${n===account.image_concurrency?'selected':''}>${n}</option>`).join('')}</select></div></div>
      <div class="gate-setting-row"><label class="gate-setting-copy" for="account-anlas-${id}">允许消耗 Anlas</label><div class="setting-check"><input id="account-anlas-${id}" type="checkbox" role="switch" ${account.allow_anlas?'checked':''}></div></div>
      <div class="gate-setting-row"><label class="gate-setting-copy" for="account-enabled-${id}">启用账号<small>更换或移除前先停用</small></label><div class="setting-check"><input id="account-enabled-${id}" type="checkbox" role="switch" ${account.admin_enabled?'checked':''} onchange="setManagedAccountEnabled('${id}',this.checked,this)"></div></div>
      <div class="gate-account-footer"><button type="button" class="btn gate-row-button" ${unavailable?'disabled':''} onclick="openUpstreamDialog('${id}')">更换 Token</button><button type="button" class="btn gate-row-button is-danger" ${unavailable?'disabled':''} onclick="removeUpstreamAccount('${id}',this)">移除账号</button><button type="button" class="btn gate-row-button" onclick="saveAccountPolicy('${id}',this)">保存账号设置</button></div>
      ${account.busy?'<p class="settings-note">请求尚未完成，请稍后刷新。</p>':''}
    </article>`;
  }).join(''):'还没有上游账号。';
  renderConfigurationSummary();
}
async function loadUpstreamAccounts(){
  try{const result=await api('/admin/api/upstream-tokens');renderUpstreamAccounts(result.accounts,result.source);}
  catch(e){toast(e.message);}
}
async function refreshUpstreamViews(){
  await loadUpstreamAccounts();
  await loadOverview();
  loadAllowance();
}
async function saveAccountPolicy(id,button){
  const account=upstreamAccounts.find(item=>item.token_id===id);
  if(!account) return;
  const input=$('#account-v5-'+id);
  if(!input.checkValidity()){input.reportValidity();return;}
  const limit=Number(input.value), concurrency=Number($('#account-concurrency-'+id).value), allow=$('#account-anlas-'+id).checked;
  button.disabled=true;
  try{
    if(!configurationCsrf) await fetchConfiguration();
    if(limit!==account.v5_daily_limit) await api('/admin/api/upstream-tokens/'+id+'/v5-limit',{method:'PUT',body:JSON.stringify({v5_daily_limit:limit})});
    if(concurrency!==account.image_concurrency) await api('/admin/api/upstream-tokens/'+id+'/image-concurrency',{method:'PUT',body:JSON.stringify({image_concurrency:concurrency})});
    if(allow!==account.allow_anlas) await configurationWrite('/admin/api/upstream-tokens/'+id+'/allow-anlas','PUT',{allow_anlas:allow});
    toast('账号设置已保存');
  }catch(e){toast(e.message);}
  finally{await refreshUpstreamViews();button.disabled=false;}
}
async function setManagedAccountEnabled(id,enabled,button){
  button.disabled=true;
  try{
    await api('/admin/api/upstream-tokens/'+id+'/enabled',{method:'PUT',body:JSON.stringify({enabled})});
    toast(enabled?'账号已启用':'账号已停用');
  }catch(e){toast(e.message);}
  finally{await refreshUpstreamViews();button.disabled=false;}
}
function openUpstreamDialog(id=null){
  const account=id?upstreamAccounts.find(item=>item.token_id===id):null;
  if(id && (!account || account.admin_enabled || account.busy)){toast('请先停用账号，并等待请求完成');return;}
  editingUpstream=id;
  $('#upstreamCredentialForm').reset();
  $('#upstreamCredential').value='';
  $('#upstreamCredentialError').textContent='';
  $('#upstreamDialogTitle').textContent=account?'更换账号 '+account.position+' 的 Token':'添加上游账号';
  $('#upstreamAddOptions').hidden=!!account;
  $('#upstreamNewV5').disabled=!!account;
  $('#upstreamCredentialSave').disabled=false;
  $('#upstreamDialog').showModal();
}
function closeUpstreamDialog(){
  $('#upstreamCredential').value='';
  $('#upstreamDialog').close();
  editingUpstream=null;
}
async function saveUpstreamCredential(event){
  event.preventDefault();
  const token=$('#upstreamCredential').value.trim();
  if(!token){$('#upstreamCredentialError').textContent='请输入 NovelAI Token';return;}
  const id=editingUpstream, body=id?{token}:{token,
    v5_daily_limit:Number($('#upstreamNewV5').value),image_concurrency:Number($('#upstreamNewConcurrency').value),
    allow_anlas:$('#upstreamNewAnlas').checked,enabled:$('#upstreamNewEnabled').checked};
  $('#upstreamCredentialSave').disabled=true;
  try{
    await configurationWrite('/admin/api/upstream-tokens'+(id?'/'+id+'/credential':''),id?'PUT':'POST',body);
    closeUpstreamDialog();
    toast(id?'Token 已更换，账号保持停用':'上游账号已保存');
    await refreshUpstreamViews();
  }catch(e){$('#upstreamCredentialError').textContent=e.message;}
  finally{$('#upstreamCredentialSave').disabled=false;}
}
async function removeUpstreamAccount(id,button){
  const account=upstreamAccounts.find(item=>item.token_id===id);
  if(!account || account.admin_enabled || account.busy){toast('请先停用账号，并等待请求完成');return;}
  if(!await askConfirm(`移除账号 ${account.position}？用户 Key 和历史用量会保留。`,{title:'移除上游账号',confirmLabel:'移除账号'})) return;
  button.disabled=true;
  try{
    await configurationWrite('/admin/api/upstream-tokens/'+id,'DELETE',{});
    toast('上游账号已移除');
  }catch(e){toast(e.message);}
  finally{await refreshUpstreamViews();button.disabled=false;}
}
$('#upstreamDialog').addEventListener('close',()=>{$('#upstreamCredential').value='';editingUpstream=null;});
$('#confirmAccept').addEventListener('click',()=>finishConfirmation(true));
$('#confirmCancel').addEventListener('click',()=>finishConfirmation(false));
$('#confirmDialog').addEventListener('cancel',event=>{event.preventDefault();finishConfirmation(false);});
$('#confirmDialog').addEventListener('close',()=>{if($('#confirmDialog').open)return;const resolve=confirmationResolve;confirmationResolve=null;resolve?.(false);});
$('#copyKeyToken').addEventListener('click',copyDisplayedKey);
$('#closeKeyToken').addEventListener('click',closeKeyToken);
$('#keyTokenDialog').addEventListener('cancel',clearKeyToken);
$('#keyTokenDialog').addEventListener('close',()=>{if(!$('#keyTokenDialog').open)clearKeyToken();});
$('#s_auto_medium').addEventListener('change',syncBudgetFields);

document.addEventListener('visibilitychange', ()=>{ if(!document.hidden && allowanceTimer) loadAllowance(); });
renderConfigurationControls();
renderConfigurationSummary();
paintIcons();
boot();
