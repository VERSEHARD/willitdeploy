const BASE='/pricepulse';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = (v='') => String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));

let state = {monitors:[],events:[],stats:{},report:{},lab:{}};

function token(){
  const input=$('#token');
  if(input && input.value) localStorage.setItem('pp_token',input.value);
  return (input && input.value) || localStorage.getItem('pp_token') || '';
}
if($('#token')) $('#token').value=localStorage.getItem('pp_token')||'';

function headers(json=true){
  const h={};
  if(json) h['Content-Type']='application/json';
  const t=token();
  if(t) h['X-Monitor-Token']=t;
  return h;
}

function fmtTime(iso){
  if(!iso) return 'Never';
  const d=new Date(iso), diff=Date.now()-d.getTime();
  const min=Math.round(diff/60000);
  if(min<1) return 'just now';
  if(min<60) return min+'m ago';
  const h=Math.round(min/60);
  if(h<24) return h+'h ago';
  const days=Math.round(h/24);
  if(days<7) return days+'d ago';
  return d.toLocaleDateString();
}
function fmtLatency(v){return v==null?'—':(v>=1000?(v/1000).toFixed(1)+'s':Math.round(v)+'ms')}
function fmtPrice(m){
  if(m.last_price==null) return '—';
  const symbols={USD:'$',GBP:'£',EUR:'€',INR:'₹'};
  return (symbols[m.currency]||'')+Number(m.last_price).toLocaleString(undefined,{maximumFractionDigits:2});
}
function ruleLabel(m){
  if(m.kind==='price') return m.max_price!=null?'Price ≤ '+(m.currency||'')+' '+m.max_price:'Price change';
  if(m.kind==='stock') return m.must_contain?'Wait for “'+m.must_contain+'”':'Stock change';
  if(m.kind==='keyword') return m.must_contain?'Keyword “'+m.must_contain+'”':'Keyword';
  return m.selector?'Selected content':'Any content change';
}
function kindIcon(kind){return {price:'ti-currency-dollar',stock:'ti-package',keyword:'ti-text-scan-2',content:'ti-file-diff'}[kind]||'ti-radar'}
function eventIcon(type){return {price_threshold:'ti-target-arrow',price_changed:'ti-arrows-exchange',stock_available:'ti-package-export',stock_unavailable:'ti-package-off',keyword_match:'ti-bell-check',keyword_lost:'ti-bell-off',content_changed:'ti-file-diff',baseline:'ti-camera',error:'ti-alert-triangle'}[type]||'ti-bolt'}
function statusHtml(m){return '<span class="pp-status '+esc(m.health)+'">'+esc(m.health)+'</span>'}

function setView(name){
  $$('.pp-view').forEach(v=>v.classList.toggle('active',v.id==='view-'+name));
  $$('.pp-nav-item').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
  const titles={overview:'Overview',monitors:'Monitors',activity:'Activity',briefing:'Briefing',lab:'Reliability lab'};
  $('#viewTitle').textContent=titles[name]||'PricePulse';
  if(innerWidth<720) window.scrollTo({top:0,behavior:'smooth'});
}
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$$('[data-view-jump]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.viewJump)));

async function fetchJSON(path,opts={}){
  const r=await fetch(BASE+path,{cache:'no-store',...opts});
  let data={};
  try{data=await r.json()}catch{}
  if(!r.ok) throw new Error(data.error||('Request failed: '+r.status));
  return data;
}

function renderMetrics(){
  const s=state.stats;
  $('#metricActive').textContent=s.active||0;
  $('#metricMonitorsSub').textContent=(s.monitors||0)+' total';
  $('#metricSuccess').textContent=s.success_rate==null?'—':s.success_rate+'%';
  $('#metricChecksSub').textContent=(s.checks_total||0).toLocaleString()+' checks';
  $('#metricSignals').textContent=s.signals_24h||0;
  $('#metricLatency').textContent=fmtLatency(s.avg_latency_ms);
  $('#navMonitorCount').textContent=s.monitors||0;
}

function monitorRow(m,compact=false){
  const lastValue=m.kind==='price'?fmtPrice(m):(m.kind==='stock'&&m.last_availability?esc(m.last_availability):(m.last_excerpt?esc(m.last_excerpt.slice(0,46)):'—'));
  if(compact){
    return '<tr data-open-monitor="'+m.id+'"><td><div class="pp-monitor-name"><span class="pp-site-icon"><i class="ti '+kindIcon(m.kind)+'"></i></span><div><strong>'+esc(m.name)+'</strong><small>'+esc(m.url)+'</small></div></div></td><td><span class="pp-muted">'+esc(ruleLabel(m))+'</span></td><td>'+esc(fmtTime(m.last_checked_iso))+'</td><td>'+statusHtml(m)+'</td></tr>';
  }
  return '<tr data-open-monitor="'+m.id+'"><td><div class="pp-monitor-name"><span class="pp-site-icon"><i class="ti '+kindIcon(m.kind)+'"></i></span><div><strong>'+esc(m.name)+'</strong><small>'+esc(m.url)+'</small></div></div></td>'+
    '<td><span class="pp-muted">'+esc(ruleLabel(m))+'</span></td>'+
    '<td><span class="pp-value">'+lastValue+'</span><div class="small text-secondary">'+(m.change_count||0)+' signals</div></td>'+
    '<td><span>'+m.interval_min+'m</span><div class="small text-secondary">'+esc(fmtTime(m.last_checked_iso))+'</div></td>'+
    '<td>'+statusHtml(m)+(m.success_rate!=null?'<div class="small text-secondary">'+m.success_rate+'% checks</div>':'')+'</td>'+
    '<td><div class="btn-list flex-nowrap" onclick="event.stopPropagation()"><button class="btn btn-sm btn-ghost-secondary" data-run="'+m.id+'" title="Run now"><i class="ti ti-player-play"></i></button><button class="btn btn-sm btn-ghost-secondary" data-toggle="'+m.id+'" title="'+(m.enabled?'Pause':'Resume')+'"><i class="ti '+(m.enabled?'ti-player-pause':'ti-player-play-filled')+'"></i></button><button class="btn btn-sm btn-ghost-danger" data-delete="'+m.id+'" title="Delete"><i class="ti ti-trash"></i></button></div></td></tr>';
}

function renderMonitors(){
  const list=state.monitors;
  $('#overviewMonitorRows').innerHTML=list.length?list.slice(0,6).map(m=>monitorRow(m,true)).join(''):'<tr><td colspan="4"><div class="pp-empty">No monitors yet. Add a page to start watching.</div></td></tr>';

  const q=($('#monitorSearch')?.value||'').toLowerCase().trim();
  const hf=$('#healthFilter')?.value||'all';
  const filtered=list.filter(m=>(!q||(m.name+' '+m.url).toLowerCase().includes(q))&&(hf==='all'||m.health===hf));
  $('#monitorRows').innerHTML=filtered.length?filtered.map(m=>monitorRow(m,false)).join(''):'<tr><td colspan="6"><div class="pp-empty">No monitors match this view.</div></td></tr>';

  $$('[data-open-monitor]').forEach(r=>r.addEventListener('click',()=>openMonitor(Number(r.dataset.openMonitor))));
  $$('[data-run]').forEach(b=>b.addEventListener('click',()=>runNow(Number(b.dataset.run))));
  $$('[data-toggle]').forEach(b=>b.addEventListener('click',()=>toggleMonitor(Number(b.dataset.toggle))));
  $$('[data-delete]').forEach(b=>b.addEventListener('click',()=>removeMonitor(Number(b.dataset.delete))));
}

function eventItem(e,full=false){
  let detail=e.detail||'';
  try{const d=JSON.parse(detail); detail=(d.diff&&d.diff.length?d.diff.slice(0,3).join(' '):d.excerpt)||detail}catch{}
  const body=esc(detail).slice(0,full?500:120);
  return '<div class="'+(full?'pp-activity-item':'pp-feed-item')+'"><span class="pp-event-icon"><i class="ti '+eventIcon(e.event_type)+'"></i></span><div class="'+(full?'pp-activity-content':'pp-event-main')+'"><strong>'+esc(e.monitor_name)+'</strong>'+(full?'<h4>'+esc(e.summary)+'</h4>':'<p>'+esc(e.summary)+'</p>')+(full&&body?'<p>'+body+'</p>':'')+'</div><span class="'+(full?'pp-activity-meta':'pp-event-time')+'">'+esc(fmtTime(e.created_at_iso))+'</span></div>';
}

function renderEvents(){
  const events=state.events;
  $('#overviewEvents').innerHTML=events.length?events.slice(0,7).map(e=>eventItem(e,false)).join(''):'<div class="pp-empty">No signals yet.</div>';
  $('#activityRows').innerHTML=events.length?events.map(e=>eventItem(e,true)).join(''):'<div class="pp-empty">No activity yet.</div>';
}

function renderBrief(){
  const r=state.report||{};
  const total=r.total_signals||0;
  $('#briefTotal').textContent=total;
  $('#briefHeadline').textContent=total?(total+' meaningful signal'+(total===1?'':'s')+' across the last '+r.days+' days.'):'No meaningful signals in the last '+(r.days||7)+' days.';
  $('#briefSub').textContent=total?'Use the timeline below to see what moved and which monitors were most active.':'This is good when your watchlist is quiet — no alert fatigue.';
  const labels={price_threshold:'Price targets',keyword_match:'Keyword / stock',content_changed:'Content changes',error:'Errors'};
  const counts=Object.entries(r.type_counts||{});
  $('#briefBreakdown').innerHTML=counts.length?counts.map(([k,v])=>'<div class="pp-break-row"><span>'+esc(labels[k]||k)+'</span><strong>'+v+'</strong></div>').join(''):'<div class="pp-empty">Nothing to summarize yet.</div>';
  const top=r.top_monitors||[];
  $('#briefTop').innerHTML=top.length?top.map(x=>'<div class="pp-break-row"><span>'+esc(x.name)+'</span><strong>'+x.signals+'</strong></div>').join(''):'<div class="pp-empty">No active monitors yet.</div>';
  const items=r.items||[];
  $('#briefItems').innerHTML=items.length?items.map(x=>'<div class="pp-activity-item"><span class="pp-event-icon"><i class="ti '+eventIcon(x.event_type)+'"></i></span><div class="pp-activity-content"><strong>'+esc(x.monitor)+'</strong><h4>'+esc(x.summary)+'</h4></div><span class="pp-activity-meta">'+esc(fmtTime(x.created_at_iso))+'</span></div>').join(''):'<div class="pp-empty">Your weekly intelligence will appear here.</div>';
}

function renderLab(){
  const lab=state.lab||{};
  $('#labPassRate').textContent=lab.pass_rate==null?'—':lab.pass_rate+'%';
  $('#labStability').textContent=lab.stability_rate==null?'—':lab.stability_rate+'%';
  $('#labTargets').textContent=(lab.targets||[]).length;
  $('#labLastRun').textContent=fmtTime(lab.last_run_iso);
  $('#proofPassRate').textContent=lab.pass_rate==null?'—':lab.pass_rate+'%';
  $('#proofRunTime').textContent=lab.last_run_iso?'last run '+fmtTime(lab.last_run_iso):'waiting for first run';
  const targets=lab.targets||[];
  $('#labCards').innerHTML=targets.length?targets.map(t=>'<div class="col-12 col-md-6 col-xl-3"><div class="pp-lab-card"><div class="pp-lab-card-head"><div><h3>'+esc(t.target)+'</h3><p>'+esc(t.url)+'</p></div><span class="badge '+(t.ok?'bg-green-lt text-green':'bg-red-lt text-red')+'">'+(t.ok?'PASS':'FAIL')+'</span></div><div class="pp-lab-meta"><div><span>HTTP</span><strong>'+esc(t.http_status||'—')+'</strong></div><div><span>Latency</span><strong>'+esc(fmtLatency(t.latency_ms))+'</strong></div><div><span>Stable</span><strong>'+(t.stable==null?'—':(t.stable?'YES':'NO'))+'</strong></div></div>'+(t.error?'<div class="small text-danger mt-3">'+esc(t.error)+'</div>':'')+'</div></div>').join(''):'<div class="col-12"><div class="pp-empty">Reliability run is starting…</div></div>';

  const real=state.monitors.find(m=>m.name==='Books demo under 60');
  $('#proofText').textContent=real&&real.last_checked_iso?'Live sample page fetched '+fmtTime(real.last_checked_iso)+' · detected '+fmtPrice(real)+' · '+(real.last_error?'error: '+real.last_error:'healthy check'):'Waiting for the external demo monitor.';
}

async function loadAll(){
  try{
    const [monitors,events,stats,report,lab]=await Promise.all([
      fetchJSON('/api/monitors'),fetchJSON('/api/events?limit=60'),fetchJSON('/api/stats'),fetchJSON('/api/report?days=7'),fetchJSON('/api/lab')
    ]);
    state={monitors,events,stats,report,lab};
    renderMetrics();renderMonitors();renderEvents();renderBrief();renderLab();
  }catch(err){console.error(err)}
}

async function health(){
  try{
    const d=await fetchJSON('/health');
    $('#health').textContent='Engine online · v'+d.version;
    $('#healthDot').className='pp-health-dot ok';
  }catch{
    $('#health').textContent='Engine unavailable';
    $('#healthDot').className='pp-health-dot bad';
  }
}

async function runNow(id){
  try{await fetchJSON('/api/monitors/'+id+'/run',{method:'POST',headers:headers(false)});await loadAll()}catch(err){alert(err.message)}
}
async function toggleMonitor(id){
  try{await fetchJSON('/api/monitors/'+id+'/toggle',{method:'POST',headers:headers(false)});await loadAll()}catch(err){alert(err.message)}
}
async function removeMonitor(id){
  const m=state.monitors.find(x=>x.id===id);
  if(!confirm('Delete '+(m?.name||'this monitor')+' and its history?')) return;
  try{await fetchJSON('/api/monitors/'+id,{method:'DELETE',headers:headers(false)});await loadAll()}catch(err){alert(err.message)}
}

function sparkline(points){
  const nums=points.filter(x=>x.price!=null).reverse().map(x=>Number(x.price));
  if(nums.length<2) return '<div class="pp-empty">Price history needs at least two priced checks.</div>';
  const w=700,h=90,pad=8,min=Math.min(...nums),max=Math.max(...nums),range=(max-min)||1;
  const coords=nums.map((v,i)=>[(i/(nums.length-1))*(w-pad*2)+pad,h-pad-((v-min)/range)*(h-pad*2)]);
  return '<svg class="pp-spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><polyline fill="none" stroke="#6f96ff" stroke-width="2" vector-effect="non-scaling-stroke" points="'+coords.map(p=>p.join(',')).join(' ')+'"/></svg>';
}

async function openMonitor(id){
  const m=state.monitors.find(x=>x.id===id);
  if(!m) return;
  $('#detailTitle').textContent=m.name;
  $('#detailBody').innerHTML='<div class="pp-empty">Loading history…</div>';
  bootstrap.Modal.getOrCreateInstance($('#monitorDetail')).show();
  try{
    const d=await fetchJSON('/api/monitors/'+id+'/history?limit=25');
    const snaps=d.snapshots||[], ev=d.events||[];
    $('#detailBody').innerHTML='<div class="pp-detail-grid">'+
      '<div class="pp-detail-stat"><span>Health</span><strong>'+esc(m.health)+'</strong></div>'+
      '<div class="pp-detail-stat"><span>Success</span><strong>'+(m.success_rate==null?'—':m.success_rate+'%')+'</strong></div>'+
      '<div class="pp-detail-stat"><span>Last price</span><strong>'+esc(fmtPrice(m))+'</strong></div>'+
      '<div class="pp-detail-stat"><span>Average fetch</span><strong>'+esc(fmtLatency(m.avg_latency_ms))+'</strong></div>'+
      '</div>'+sparkline(snaps)+
      '<div class="row g-4"><div class="col-lg-7"><div class="pp-panel"><div class="pp-panel-head"><div><span class="pp-panel-kicker">SNAPSHOTS</span><h3>Version history</h3></div></div><div style="padding:0 18px">'+
      (snaps.length?snaps.map(x=>'<div class="pp-history-row"><span>'+esc(fmtTime(x.created_at_iso))+'</span><span>'+esc(x.price==null?'—':x.price)+'</span><span>'+esc(fmtLatency(x.latency_ms))+'</span><span class="pp-muted">'+esc((x.excerpt||'').slice(0,130))+'</span></div>').join(''):'<div class="pp-empty">No snapshots yet.</div>')+
      '</div></div></div><div class="col-lg-5"><div class="pp-panel"><div class="pp-panel-head"><div><span class="pp-panel-kicker">EVENTS</span><h3>Monitor signals</h3></div></div>'+
      (ev.length?ev.slice(0,10).map(x=>'<div class="pp-feed-item"><span class="pp-event-icon"><i class="ti '+eventIcon(x.event_type)+'"></i></span><div class="pp-event-main"><strong>'+esc(x.summary)+'</strong><p>'+esc(fmtTime(x.created_at_iso))+'</p></div></div>').join(''):'<div class="pp-empty">No signal events yet.</div>')+
      '</div></div></div>';
  }catch(err){$('#detailBody').innerHTML='<div class="alert alert-danger">'+esc(err.message)+'</div>'}
}

$('#probeBtn').addEventListener('click',async()=>{
  const btn=$('#probeBtn'), url=$('#url').value.trim();
  if(!url) return;
  btn.disabled=true;btn.innerHTML='<span class="spinner-border spinner-border-sm me-1"></span>Testing';
  try{
    const d=await fetchJSON('/api/probe',{method:'POST',headers:headers(),body:JSON.stringify({url,selector:$('#selector').value.trim()||null,price_regex:$('#priceRegex').value.trim()||null})});
    if(!$('#name').value && d.title) $('#name').value=d.title.slice(0,80);
    if(d.currency && !$('#currency').value) $('#currency').value=d.currency;
    $('#probeResult').classList.remove('d-none');
    $('#probeResult').innerHTML='<div class="pp-probe-grid"><div><span>Status</span><strong>'+d.http_status+'</strong></div><div><span>Monitorability</span><strong>'+esc(d.monitorability)+'</strong></div><div><span>Detected price</span><strong>'+esc(d.price==null?'—':((d.currency||'')+' '+d.price))+'</strong></div><div><span>Availability</span><strong>'+esc(d.availability||'—')+'</strong></div></div><div class="pp-probe-preview">'+esc(d.preview||'')+'</div>';
  }catch(err){
    $('#probeResult').classList.remove('d-none');$('#probeResult').innerHTML='<div class="text-danger small">'+esc(err.message)+'</div>';
  }finally{btn.disabled=false;btn.textContent='Test page'}
});

$('#monitorForm').addEventListener('submit',async e=>{
  e.preventDefault();$('#formError').classList.add('d-none');
  const body={
    name:$('#name').value.trim()||new URL($('#url').value).hostname,
    url:$('#url').value.trim(),
    kind:$('#kind').value,
    interval_min:Number($('#interval').value||5),
    selector:$('#selector').value.trim()||null,
    must_contain:$('#mustContain').value.trim()||null,
    price_regex:$('#priceRegex').value.trim()||null,
    max_price:$('#maxPrice').value===''?null:Number($('#maxPrice').value),
    currency:$('#currency').value||null,
    ignore_regex:$('#ignoreRegex').value.trim()||null,
    webhook_url:$('#webhook').value.trim()||null
  };
  try{
    const d=await fetchJSON('/api/monitors',{method:'POST',headers:headers(),body:JSON.stringify(body)});
    bootstrap.Modal.getOrCreateInstance($('#newMonitor')).hide();
    $('#monitorForm').reset();$('#interval').value='5';$('#kind').value='price';if($('#token')) $('#token').value=localStorage.getItem('pp_token')||'';
    $('#probeResult').classList.add('d-none');
    await runNow(d.id);
    setView('monitors');
  }catch(err){$('#formError').textContent=err.message;$('#formError').classList.remove('d-none')}
});

$('#monitorSearch').addEventListener('input',renderMonitors);
$('#healthFilter').addEventListener('change',renderMonitors);
$('#refreshBtn').addEventListener('click',()=>{health();loadAll()});

health();loadAll();setInterval(loadAll,15000);
