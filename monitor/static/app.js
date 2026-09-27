const $ = s => document.querySelector(s);
const esc = (v='') => String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));

function token() {
  const input = $('#token');
  if (!input) return localStorage.getItem('pp_token') || '';
  if (input.value) localStorage.setItem('pp_token', input.value);
  return input.value || localStorage.getItem('pp_token') || '';
}
if ($('#token')) $('#token').value = localStorage.getItem('pp_token') || '';

function authHeaders(json=true) {
  const h = {};
  if (json) h['Content-Type']='application/json';
  const t = token();
  if (t) h['X-Monitor-Token']=t;
  return h;
}

function ruleText(m) {
  const parts=[];
  if (m.selector) parts.push('selector '+m.selector);
  if (m.must_contain) parts.push('contains "'+m.must_contain+'"');
  if (m.max_price !== null && m.max_price !== undefined) parts.push('price ≤ '+m.max_price);
  if (!parts.length) parts.push('any content change');
  return parts.join(' · ');
}

async function loadAll() {
  const [mr, er] = await Promise.all([fetch('/api/monitors',{cache:'no-store'}), fetch('/api/events?limit=30',{cache:'no-store'})]);
  const monitors = await mr.json();
  const events = await er.json();

  $('#metricMonitors').textContent = monitors.length;
  $('#metricActive').textContent = monitors.filter(m=>m.enabled).length;
  $('#metricChanges').textContent = monitors.reduce((a,m)=>a+(m.change_count||0),0);
  $('#metricErrors').textContent = monitors.filter(m=>m.last_error).length;

  $('#monitorRows').innerHTML = monitors.length ? monitors.map(m => {
    const status = m.last_error
      ? '<span class="badge bg-red-lt text-red">ERROR</span>'
      : (m.last_checked ? '<span class="badge bg-green-lt text-green">OK</span>' : '<span class="badge bg-yellow-lt text-yellow">PENDING</span>');
    return '<tr>'+
      '<td><strong>'+esc(m.name)+'</strong><div class="small text-secondary">every '+m.interval_min+'m</div></td>'+
      '<td><div class="url-cell" title="'+esc(m.url)+'">'+esc(m.url)+'</div></td>'+
      '<td class="small">'+esc(ruleText(m))+(m.last_price!==null && m.last_price!==undefined ? '<div class="text-secondary">last price: '+esc(m.last_price)+'</div>' : '')+'</td>'+
      '<td class="small text-secondary">'+esc(m.last_checked_iso || 'not yet')+'</td>'+
      '<td>'+status+(m.last_error ? '<div class="small text-danger mt-1">'+esc(m.last_error)+'</div>' : '')+'</td>'+
      '<td><div class="btn-list flex-nowrap">'+
        '<button class="btn btn-sm btn-outline-secondary" onclick="runNow('+m.id+')">Run</button>'+
        '<button class="btn btn-sm btn-outline-danger" onclick="removeMonitor('+m.id+')">Delete</button>'+
      '</div></td>'+
    '</tr>';
  }).join('') : '<tr><td colspan="6" class="text-secondary">No monitors yet.</td></tr>';

  $('#eventRows').innerHTML = events.length ? events.map(e =>
    '<div class="list-group-item"><div class="d-flex justify-content-between gap-3">'+
      '<div><strong>'+esc(e.monitor_name)+'</strong> · '+esc(e.summary)+'<div class="small text-secondary mt-1">'+esc(e.detail || e.url || '')+'</div></div>'+
      '<div class="small text-secondary text-nowrap">'+esc(e.created_at_iso || '')+'</div>'+
    '</div></div>'
  ).join('') : '<div class="list-group-item text-secondary">No events yet.</div>';
}

window.runNow = async id => {
  const r = await fetch('/api/monitors/'+id+'/run',{method:'POST',headers:authHeaders(false)});
  const d = await r.json();
  if (!r.ok) alert(d.error || 'Run failed');
  await loadAll();
};

window.removeMonitor = async id => {
  if (!confirm('Delete this monitor?')) return;
  const r = await fetch('/api/monitors/'+id,{method:'DELETE',headers:authHeaders(false)});
  const d = await r.json();
  if (!r.ok) alert(d.error || 'Delete failed');
  await loadAll();
};

$('#monitorForm').addEventListener('submit', async e => {
  e.preventDefault();
  $('#formError').classList.add('d-none');
  const body = {
    name: $('#name').value.trim(),
    url: $('#url').value.trim(),
    interval_min: Number($('#interval').value || 5),
    selector: $('#selector').value.trim() || null,
    must_contain: $('#mustContain').value.trim() || null,
    price_regex: $('#priceRegex').value.trim() || null,
    max_price: $('#maxPrice').value === '' ? null : Number($('#maxPrice').value),
    webhook_url: $('#webhook').value.trim() || null
  };
  const r = await fetch('/api/monitors',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) {
    $('#formError').textContent=d.error || 'Could not create monitor';
    $('#formError').classList.remove('d-none');
    return;
  }
  bootstrap.Modal.getInstance($('#newMonitor')).hide();
  $('#monitorForm').reset();
  if ($('#token')) $('#token').value = localStorage.getItem('pp_token') || '';
  await fetch('/api/monitors/'+d.id+'/run',{method:'POST',headers:authHeaders(false)});
  await loadAll();
});

async function health() {
  try {
    const r=await fetch('/health',{cache:'no-store'});
    const d=await r.json();
    $('#health').textContent = r.ok ? 'Online · '+d.version : 'Unavailable';
  } catch {
    $('#health').textContent='Unavailable';
  }
}

health();
loadAll();
setInterval(loadAll,15000);
