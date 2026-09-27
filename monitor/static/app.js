const BASE='/pricepulse';
const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];
const esc=(v='')=>String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));

let state={monitors:[],events:[],stats:{},report:{},lab:{},product:{}};
let selectedId=null;
let watchFilter='all';

function token(){
  const input=$('#token');
  if(input&&input.value) localStorage.setItem('pp_token',input.value);
  return (input&&input.value)||localStorage.getItem('pp_token')||'';
}
if($('#token')) $('#token').value=localStorage.getItem('pp_token')||'';

function headers(json=true){
  const h={};
  if(json) h['Content-Type']='application/json';
  const t=token();
  if(t) h['X-Monitor-Token']=t;
  return h;
}

async function fetchJSON(path,opts={}){
  const r=await fetch(BASE+path,{cache:'no-store',...opts});
  let data={};
  try{data=await r.json()}catch{}
  if(!r.ok) throw new Error(data.error||('Request failed: '+r.status));
  return data;
}

function fmtTime(iso){
  if(!iso) return 'never';
  const d=new Date(iso),ms=Date.now()-d.getTime();
  const mins=Math.max(0,Math.round(ms/60000));
  if(mins<1) return 'now';
  if(mins<60) return mins+'m';
  const hrs=Math.round(mins/60);
  if(hrs<24) return hrs+'h';
  const days=Math.round(hrs/24);
  if(days<14) return days+'d';
  return d.toLocaleDateString();
}
function fmtFullTime(iso){return iso?new Date(iso).toLocaleString():'—'}
function fmtLatency(v){return v==null?'—':(Number(v)>=1000?(Number(v)/1000).toFixed(1)+'s':Math.round(Number(v))+'ms')}
function domain(url){try{return new URL(url).hostname.replace(/^www\./,'')}catch{return url||'—'}}
function fmtPrice(m){
  if(m.last_price==null) return '—';
  const sym={USD:'$',GBP:'£',EUR:'€',INR:'₹'}[m.currency]||'';
  return sym+Number(m.last_price).toLocaleString(undefined,{maximumFractionDigits:2});
}
function kindIcon(kind){
  return {listing_feed:'ti-list-search',price:'ti-currency-dollar',stock:'ti-package',keyword:'ti-text-scan-2',content:'ti-file-diff'}[kind]||'ti-radar';
}
function eventIcon(type){
  return {
    new_listing:'ti-square-rounded-plus',price_threshold:'ti-target-arrow',price_changed:'ti-arrows-exchange',
    stock_available:'ti-package-export',stock_unavailable:'ti-package-off',keyword_match:'ti-bell-check',
    keyword_lost:'ti-bell-off',content_changed:'ti-file-diff',baseline:'ti-camera',error:'ti-alert-triangle'
  }[type]||'ti-bolt';
}
function ruleLabel(m){
  if(m.kind==='listing_feed'){
    const bits=['New listing'];
    if(m.must_contain) bits.push('title: '+m.must_contain);
    if(m.max_price!=null) bits.push('≤ '+(m.currency||'')+' '+m.max_price);
    return bits.join(' · ');
  }
  if(m.kind==='price') return m.max_price!=null?'Price ≤ '+(m.currency||'')+' '+m.max_price:'Any price move';
  if(m.kind==='stock') return m.must_contain?'Availability: '+m.must_contain:'Availability state';
  if(m.kind==='keyword') return m.must_contain?'Text: '+m.must_contain:'Text change';
  return m.selector?'Selected content':'Page content';
}
function lastValue(m){
  if(m.kind==='price') return fmtPrice(m);
  if(m.kind==='stock'&&m.last_availability) return m.last_availability;
  if(m.kind==='listing_feed') return (m.change_count||0)+' new';
  return m.last_excerpt?m.last_excerpt.slice(0,40):'—';
}

function setView(name,{sync=true}={}){
  const valid=['watchlist','activity','briefing','lab'];
  if(!valid.includes(name)) name='watchlist';
  $$('.pp-view').forEach(v=>v.classList.toggle('active',v.id==='view-'+name));
  $$('.pp-nav-item').forEach(b=>{
    const active=b.dataset.view===name;
    b.classList.toggle('active',active);
    if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');
  });
  const titles={watchlist:'Watchlist',activity:'Activity',briefing:'Briefing',lab:'Reliability lab'};
  $('#viewTitle').textContent=titles[name];
  if(sync&&location.hash!=='#'+name) history.replaceState(null,'','#'+name);
}
$$('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));

function renderSidebar(){
  const s=state.stats||{};
  $('#navMonitorCount').textContent=s.monitors||0;
  $('#sideActive').textContent=s.active||0;
  $('#sideSuccess').textContent=s.success_rate==null?'—':s.success_rate+'%';
}

function monitorMatches(m){
  const q=($('#monitorSearch')?.value||'').toLowerCase().trim();
  const hay=(m.name+' '+m.url+' '+ruleLabel(m)).toLowerCase();
  if(q&&!hay.includes(q)) return false;
  if(watchFilter==='all') return true;
  if(watchFilter==='listing_feed') return m.kind==='listing_feed';
  return m.health===watchFilter;
}

function watchRow(m){
  return '<div class="pp-watch-row '+(m.id===selectedId?'selected':'')+'" data-monitor-id="'+m.id+'">'+
    '<div class="pp-watch-main"><span class="pp-kind-icon"><i class="ti '+kindIcon(m.kind)+'"></i></span><div><strong>'+esc(m.name)+'</strong><small>'+esc(domain(m.url))+' · '+esc(ruleLabel(m))+'</small></div></div>'+
    '<div class="pp-watch-value">'+esc(lastValue(m))+'<small><span class="pp-dot '+esc(m.health)+'"></span>'+esc(m.health)+'</small></div>'+
    '<div class="pp-watch-time">'+esc(fmtTime(m.last_checked_iso))+'</div>'+
  '</div>';
}

function renderWatchlist(){
  const filtered=state.monitors.filter(monitorMatches);
  $('#watchRows').innerHTML=filtered.length?filtered.map(watchRow).join(''):'<div class="pp-empty">No monitors match this filter.</div>';
  $$('[data-monitor-id]').forEach(row=>row.addEventListener('click',()=>{
    selectedId=Number(row.dataset.monitorId);
    renderWatchlist();
    renderInspector();
  }));
  if(selectedId==null&&filtered.length){
    selectedId=filtered[0].id;
    renderWatchlist();
    renderInspector();
  }else if(selectedId!=null&&!state.monitors.some(m=>m.id===selectedId)){
    selectedId=filtered[0]?.id||null;
    renderInspector();
  }
}

async function renderInspector(){
  const box=$('#inspector');
  const m=state.monitors.find(x=>x.id===selectedId);
  if(!m){
    box.innerHTML='<div class="pp-inspector-empty"><i class="ti ti-pointer"></i><strong>Select a monitor</strong><span>Its current state, rule, evidence and history will appear here.</span></div>';
    return;
  }
  box.innerHTML='<div class="pp-empty">Loading monitor…</div>';
  try{
    const [history,listings]=await Promise.all([
      fetchJSON('/api/monitors/'+m.id+'/history?limit=15'),
      m.kind==='listing_feed'?fetchJSON('/api/monitors/'+m.id+'/listings?limit=20'):Promise.resolve([])
    ]);
    const events=history.events||[];
    const snaps=history.snapshots||[];
    const listingHtml=listings.length?listings.slice(0,12).map(item=>
      '<a class="pp-listing" href="'+esc(item.url)+'" target="_blank" rel="noopener"><span><strong>'+esc(item.title||item.item_key)+'</strong><small>first seen '+esc(fmtTime(item.first_seen_iso))+'</small></span><b>'+(item.price==null?'—':esc((item.currency||'')+' '+item.price))+'</b></a>'
    ).join(''):'<div class="pp-empty">Baseline captured; no newly discovered listing history yet.</div>';

    const eventHtml=events.length?events.slice(0,8).map(e=>
      '<div class="pp-history-item"><i class="ti '+eventIcon(e.event_type)+'"></i><div><strong>'+esc(e.summary)+'</strong><p>'+esc(eventDetail(e))+'</p></div><time>'+esc(fmtTime(e.created_at_iso))+'</time></div>'
    ).join(''):'<div class="pp-empty">No signals yet.</div>';

    box.innerHTML=
      '<div class="pp-inspector-head"><div class="pp-inspector-titleline"><span class="pp-kind-icon"><i class="ti '+kindIcon(m.kind)+'"></i></span><div class="pp-inspector-title"><h2>'+esc(m.name)+'</h2><a href="'+esc(m.url)+'" target="_blank" rel="noopener">'+esc(m.url)+'</a></div><span class="pp-dot '+esc(m.health)+'"></span></div>'+
      '<div class="pp-inspector-actions"><button class="btn btn-sm btn-outline-secondary" data-inspect-run="'+m.id+'"><i class="ti ti-player-play me-1"></i>Check now</button><button class="btn btn-sm btn-outline-secondary" data-inspect-toggle="'+m.id+'">'+(m.enabled?'Pause':'Resume')+'</button><button class="btn btn-sm btn-outline-danger ms-auto" data-inspect-delete="'+m.id+'"><i class="ti ti-trash"></i></button></div></div>'+
      '<div class="pp-inspector-summary"><div><span>Rule</span><strong>'+esc(ruleLabel(m))+'</strong></div><div><span>Schedule</span><strong>Every '+m.interval_min+'m</strong></div><div><span>Last check</span><strong>'+esc(fmtFullTime(m.last_checked_iso))+'</strong></div><div><span>Success</span><strong>'+(m.success_rate==null?'—':m.success_rate+'%')+'</strong></div></div>'+
      '<div class="pp-inspector-section"><h3>Current evidence</h3><div class="pp-rule-box">'+
        '<strong>'+esc(lastValue(m))+'</strong><div class="text-secondary mt-1">'+esc((m.last_excerpt||'No captured text yet.').slice(0,420))+'</div>'+
        (m.last_error?'<div class="text-danger mt-2">'+esc(m.last_error)+'</div>':'')+
      '</div></div>'+
      (m.kind==='listing_feed'?'<div class="pp-inspector-section"><h3>Seen listings</h3><div class="pp-listings">'+listingHtml+'</div></div>':'')+
      '<div class="pp-inspector-section"><h3>Recent events</h3>'+eventHtml+'</div>'+
      '<div class="pp-inspector-section"><h3>Check history</h3>'+(snaps.length?snaps.slice(0,8).map(x=>'<div class="pp-history-item"><i class="ti ti-clock-check"></i><div><strong>HTTP '+esc(x.http_status||'—')+' · '+esc(fmtLatency(x.latency_ms))+'</strong><p>'+(x.price==null?'No price value':esc('Price '+x.price))+(x.changed?' · content changed':'')+'</p></div><time>'+esc(fmtTime(x.created_at_iso))+'</time></div>').join(''):'<div class="pp-empty">No snapshots yet.</div>')+'</div>';

    $('[data-inspect-run]')?.addEventListener('click',()=>runNow(m.id));
    $('[data-inspect-toggle]')?.addEventListener('click',()=>toggleMonitor(m.id));
    $('[data-inspect-delete]')?.addEventListener('click',()=>removeMonitor(m.id));
  }catch(err){
    box.innerHTML='<div class="pp-empty text-danger">'+esc(err.message)+'</div>';
  }
}

function eventDetail(e){
  if(!e.detail) return '';
  try{
    const d=JSON.parse(e.detail);
    if(d.new_listings&&d.new_listings.length) return d.new_listings.slice(0,2).map(x=>x.title||x.url).join(' · ');
    if(d.diff&&d.diff.length) return d.diff.slice(0,2).join(' ');
    return d.excerpt||'';
  }catch{return e.detail}
}

function renderActivity(){
  const filter=$('#activityFilter')?.value||'all';
  const rows=state.events.filter(e=>filter==='all'||e.event_type===filter);
  $('#activityRows').innerHTML=rows.length?rows.map(e=>
    '<div class="pp-activity-row"><span>'+esc(fmtFullTime(e.created_at_iso))+'</span><i class="ti '+eventIcon(e.event_type)+'"></i><strong>'+esc(e.monitor_name)+'</strong><p>'+esc(e.summary)+(eventDetail(e)?'<br>'+esc(eventDetail(e)).slice(0,220):'')+'</p><span>'+esc(e.event_type)+'</span></div>'
  ).join(''):'<div class="pp-empty">No events in this filter.</div>';
}

function renderBriefing(){
  const r=state.report||{},s=state.stats||{};
  $('#briefTotal').textContent=r.total_signals||0;
  $('#briefActive').textContent=s.active||0;
  $('#briefSuccess').textContent=s.success_rate==null?'—':s.success_rate+'%';
  $('#briefChecks').textContent=(s.checks_total||0).toLocaleString();
  const labels={new_listing:'New listings',price_threshold:'Price targets',price_changed:'Price changes',stock_available:'Restocks',stock_unavailable:'Sold / unavailable',keyword_match:'Text appeared',keyword_lost:'Text disappeared',content_changed:'Content changes',error:'Errors'};
  const counts=Object.entries(r.type_counts||{});
  $('#briefBreakdown').innerHTML=counts.length?counts.map(([k,v])=>'<div class="pp-break-row"><span>'+esc(labels[k]||k)+'</span><strong>'+v+'</strong></div>').join(''):'<div class="pp-empty">No signals yet.</div>';
  const top=r.top_monitors||[];
  $('#briefTop').innerHTML=top.length?top.map(x=>'<div class="pp-break-row"><span>'+esc(x.name)+'</span><strong>'+x.signals+'</strong></div>').join(''):'<div class="pp-empty">No active signal history yet.</div>';
  const items=r.items||[];
  $('#briefItems').innerHTML=items.length?items.slice(0,30).map(x=>'<div class="pp-activity-row"><span>'+esc(fmtFullTime(x.created_at_iso))+'</span><i class="ti '+eventIcon(x.event_type)+'"></i><strong>'+esc(x.monitor)+'</strong><p>'+esc(x.summary)+'</p><span>'+esc(x.event_type)+'</span></div>').join(''):'<div class="pp-empty">Quiet week.</div>';
}

function renderLab(){
  const lab=state.lab||{},rolling=lab.rolling||{};
  $('#labPassRate').textContent=lab.pass_rate==null?'—':lab.pass_rate+'%';
  $('#labStability').textContent=lab.stability_rate==null?'—':lab.stability_rate+'%';
  $('#labPassRolling').textContent='rolling '+(rolling.pass_rate==null?'—':rolling.pass_rate+'%');
  $('#labStabilityRolling').textContent='rolling '+(rolling.stability_rate==null?'—':rolling.stability_rate+'%');
  $('#labSamples').textContent=rolling.samples||0;
  $('#labTargets').textContent=(lab.targets||[]).length+' targets';
  $('#labLastRun').textContent=fmtTime(lab.last_run_iso);
  const hist=new Map((rolling.targets||[]).map(x=>[x.target,x]));
  const targets=lab.targets||[];
  $('#labRows').innerHTML=targets.length?targets.map(t=>{
    const h=hist.get(t.target)||{};
    return '<div class="pp-lab-row"><span><strong>'+esc(t.target)+'</strong><small>'+esc(t.url)+'</small></span><span class="'+(t.ok?'pp-pass':'pp-fail')+'">'+(t.ok?'PASS':'FAIL')+'</span><span>HTTP '+esc(t.http_status||'—')+'</span><span>'+esc(fmtLatency(t.latency_ms))+'</span><span>'+(t.error?'<span class="pp-fail">'+esc(t.error)+'</span>':'history '+esc(h.checks||1)+'× · '+esc(h.pass_rate==null?'—':h.pass_rate+'%'))+'</span></div>';
  }).join(''):'<div class="pp-empty">Lab has not run yet.</div>';

  const p=state.product||{};
  $('#moneyViews').textContent=p.landing_views||0;
  $('#moneyClicks').textContent=p.pilot_clicks||0;
  $('#moneyCtr').textContent=p.pilot_ctr==null?'—':p.pilot_ctr+'%';
  $('#moneyProbes').textContent=p.pilot_submissions||0;
}

async function loadAll(){
  try{
    const [monitors,events,stats,report,lab,product]=await Promise.all([
      fetchJSON('/api/monitors'),
      fetchJSON('/api/events?limit=80'),
      fetchJSON('/api/stats'),
      fetchJSON('/api/report?days=7'),
      fetchJSON('/api/lab'),
      fetchJSON('/api/product-metrics')
    ]);
    state={monitors,events,stats,report,lab,product};
    renderSidebar();
    renderWatchlist();
    renderActivity();
    renderBriefing();
    renderLab();
  }catch(err){showToast(err.message,{tone:'error'})}
}

async function health(){
  try{
    const d=await fetchJSON('/health');
    $('#health').textContent='engine online · '+d.version;
    $('#healthDot').className='pp-health-dot ok';
  }catch{
    $('#health').textContent='engine unavailable';
    $('#healthDot').className='pp-health-dot bad';
  }
}

function showToast(message,{tone='default'}={}){
  const stack=$('#ppToastStack');
  if(!stack) return;
  const el=document.createElement('div');
  el.className='pp-toast '+(tone==='error'?'error':'');
  el.textContent=message;
  stack.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{el.classList.remove('show');setTimeout(()=>el.remove(),180)},2800);
}

let confirmHandler=null;
function confirmAction({title='Confirm',body='Continue?',label='Continue'}){
  $('#confirmTitle').textContent=title;
  $('#confirmBody').textContent=body;
  $('#confirmActionBtn').textContent=label;
  const modal=bootstrap.Modal.getOrCreateInstance($('#confirmModal'));
  modal.show();
  return new Promise(resolve=>{
    let settled=false;
    confirmHandler=()=>{if(settled)return;settled=true;resolve(true);modal.hide()};
    $('#confirmModal').addEventListener('hidden.bs.modal',()=>{if(!settled){settled=true;resolve(false)}},{once:true});
  });
}
$('#confirmActionBtn').addEventListener('click',()=>{if(confirmHandler){const fn=confirmHandler;confirmHandler=null;fn()}});

async function runNow(id){
  showToast('Checking live page…');
  try{
    const r=await fetchJSON('/api/monitors/'+id+'/run',{method:'POST',headers:headers(false)});
    await loadAll();
    selectedId=id;
    renderWatchlist();
    await renderInspector();
    showToast(r.ok?'Check complete':'Check failed',{tone:r.ok?'default':'error'});
  }catch(err){showToast(err.message,{tone:'error'})}
}
async function toggleMonitor(id){
  try{
    const r=await fetchJSON('/api/monitors/'+id+'/toggle',{method:'POST',headers:headers(false)});
    await loadAll();selectedId=id;renderWatchlist();await renderInspector();
    showToast(r.enabled?'Monitor resumed':'Monitor paused');
  }catch(err){showToast(err.message,{tone:'error'})}
}
async function removeMonitor(id){
  const m=state.monitors.find(x=>x.id===id);
  const ok=await confirmAction({title:'Delete monitor?',body:'Delete '+(m?.name||'this monitor')+' and its stored history?',label:'Delete'});
  if(!ok)return;
  try{
    await fetchJSON('/api/monitors/'+id,{method:'DELETE',headers:headers(false)});
    if(selectedId===id)selectedId=null;
    await loadAll();renderInspector();showToast('Monitor deleted');
  }catch(err){showToast(err.message,{tone:'error'})}
}

function setKind(kind){
  const valid=['listing_feed','price','stock','keyword','content'];
  if(!valid.includes(kind))kind='listing_feed';
  $('#kind').value=kind;
  $$('.pp-kind-option').forEach(btn=>btn.classList.toggle('active',btn.dataset.kind===kind));
  $('#ruleListing').classList.toggle('d-none',kind!=='listing_feed');
  $('#rulePrice').classList.toggle('d-none',kind!=='price');
  $('#ruleText').classList.toggle('d-none',!['stock','keyword'].includes(kind));
  $('#ruleContent').classList.toggle('d-none',kind!=='content');
  if(kind==='stock'){
    $('#mustContainLabel').textContent='Availability text';
    $('#mustContain').placeholder='Sold';
    $('#mustContainHelp').textContent='Watch for the state phrase the listing uses, such as Sold or In stock.';
  }else if(kind==='keyword'){
    $('#mustContainLabel').textContent='Text to watch';
    $('#mustContain').placeholder='Applications open';
    $('#mustContainHelp').textContent='Alert when this phrase appears or disappears.';
  }
}
$$('.pp-kind-option').forEach(btn=>btn.addEventListener('click',()=>setKind(btn.dataset.kind)));

$('#probeBtn').addEventListener('click',async()=>{
  const btn=$('#probeBtn'),url=$('#url').value.trim();
  if(!url)return;
  fetch(BASE+'/api/product-event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_type:'probe_started',meta:{source:'workspace'}}),keepalive:true}).catch(()=>{});
  btn.disabled=true;btn.textContent='Testing…';
  try{
    const d=await fetchJSON('/api/probe',{method:'POST',headers:headers(),body:JSON.stringify({url,selector:$('#selector').value.trim()||null,price_regex:$('#priceRegex').value.trim()||null})});
    if(!$('#name').value&&d.title)$('#name').value=d.title.slice(0,90);
    if(d.currency&&!$('#currency').value)$('#currency').value=d.currency;
    $('#probeResult').classList.remove('d-none');
    $('#probeResult').innerHTML='<div class="pp-probe-grid"><div><span>HTTP</span><strong>'+esc(d.http_status)+'</strong></div><div><span>Monitorability</span><strong>'+esc(d.monitorability)+'</strong></div><div><span>Price</span><strong>'+esc(d.price==null?'—':((d.currency||'')+' '+d.price))+'</strong></div><div><span>State</span><strong>'+esc(d.availability||'—')+'</strong></div></div><div class="pp-probe-preview">'+esc(d.preview||'')+'</div>';
  }catch(err){
    $('#probeResult').classList.remove('d-none');
    $('#probeResult').innerHTML='<span class="text-danger">'+esc(err.message)+'</span>';
  }finally{btn.disabled=false;btn.textContent='Test URL'}
});

$('#monitorForm').addEventListener('submit',async e=>{
  e.preventDefault();
  $('#formError').classList.add('d-none');
  const btn=$('#createMonitorBtn'),old=btn.textContent;
  btn.disabled=true;btn.textContent='Creating…';
  const kind=$('#kind').value;
  const listingKeyword=$('#listingKeyword').value.trim();
  const body={
    name:$('#name').value.trim()||domain($('#url').value),
    url:$('#url').value.trim(),
    kind,
    interval_min:Number($('#interval').value||15),
    selector:$('#selector').value.trim()||null,
    must_contain:kind==='listing_feed'?(listingKeyword||null):($('#mustContain').value.trim()||null),
    price_regex:$('#priceRegex').value.trim()||null,
    max_price:kind==='listing_feed'?($('#listingMaxPrice').value===''?null:Number($('#listingMaxPrice').value)):($('#maxPrice').value===''?null:Number($('#maxPrice').value)),
    currency:$('#currency').value||null,
    ignore_regex:$('#ignoreRegex').value.trim()||null,
    webhook_url:$('#webhook').value.trim()||null
  };
  try{
    const created=await fetchJSON('/api/monitors',{method:'POST',headers:headers(),body:JSON.stringify(body)});
    bootstrap.Modal.getOrCreateInstance($('#newMonitor')).hide();
    $('#monitorForm').reset();$('#interval').value='15';setKind('listing_feed');if($('#token'))$('#token').value=localStorage.getItem('pp_token')||'';
    $('#probeResult').classList.add('d-none');
    selectedId=created.id;
    await runNow(created.id);
    setView('watchlist');
  }catch(err){
    $('#formError').textContent=err.message;
    $('#formError').classList.remove('d-none');
  }finally{btn.disabled=false;btn.textContent=old}
});

$('#monitorSearch').addEventListener('input',renderWatchlist);
$$('[data-filter]').forEach(btn=>btn.addEventListener('click',()=>{
  watchFilter=btn.dataset.filter;
  $$('[data-filter]').forEach(b=>b.classList.toggle('active',b===btn));
  selectedId=null;
  renderWatchlist();
}));
$('#activityFilter').addEventListener('change',renderActivity);
$('#refreshBtn').addEventListener('click',()=>{health();loadAll()});

const initial=(location.hash||'').replace('#','');
setView(['watchlist','activity','briefing','lab'].includes(initial)?initial:'watchlist',{sync:false});
health();
loadAll();
setInterval(loadAll,15000);
