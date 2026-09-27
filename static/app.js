const $ = (s) => document.querySelector(s);
const resultArea = $('#resultArea');
const historyEl = $('#history');
const logStore = new Map();
const logModalEl = $('#logModal');

function token() {
  const input = $('#tokenInput');
  if (!input) return '';
  if (input.value) localStorage.setItem('wid_scan_token', input.value);
  return input.value || localStorage.getItem('wid_scan_token') || '';
}
if ($('#tokenInput')) $('#tokenInput').value = localStorage.getItem('wid_scan_token') || '';

function headers(json = true) {
  const h = {};
  if (json) h['Content-Type'] = 'application/json';
  const t = token();
  if (t) h['X-Scan-Token'] = t;
  return h;
}
function esc(v = '') {
  return String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));
}
function badgeFor(status) {
  if (status === 'pass') return '<span class="badge bg-green-lt text-green">PASS</span>';
  if (status === 'completed') return '<span class="badge bg-green-lt text-green">DONE</span>';
  if (status === 'unsupported_package_manager') return '<span class="badge bg-secondary-lt">SKIP</span>';
  if (status === 'queued' || status === 'running') return '<span class="badge bg-yellow-lt text-yellow">RUNNING</span>';
  return '<span class="badge bg-red-lt text-red">FAIL</span>';
}

async function checkHealth() {
  const dot = $('#healthDot');
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const r = await fetch('/api/health', {signal: controller.signal, cache: 'no-store'});
    clearTimeout(timer);
    const h = await r.json();
    if (!r.ok) throw new Error('health check failed');
    dot.className = 'health-dot is-online';
    $('#healthText').textContent = `Online · Python ${h.python}`;
  } catch {
    dot.className = 'health-dot is-offline';
    $('#healthText').textContent = 'Scanner unavailable';
  }
}

async function loadHistory() {
  try {
    const r = await fetch('/api/scans?limit=12');
    const rows = await r.json();
    historyEl.innerHTML = rows.length ? rows.map(row => `
      <button class="list-group-item list-group-item-action text-start py-3" onclick="loadScan('${row.id}')">
        <div class="d-flex justify-content-between align-items-start gap-3">
          <strong class="repo-link">${esc(row.repo_url.replace('https://github.com/',''))}</strong>
          ${badgeFor(row.status)}
        </div>
        <div class="text-secondary history-meta mt-1">${esc(row.mode)} · Node ${row.runtimes.join('/')} ${row.project_path ? '· ' + esc(row.project_path) : ''}</div>
      </button>`).join('') : '<div class="p-4 text-secondary small">No experiments yet.</div>';
  } catch {
    historyEl.innerHTML = '<div class="p-4 text-danger small">Could not load history.</div>';
  }
}

function renderStatic(s) {
  if (!s.node_project_found) {
    return `<div class="card mb-3"><div class="card-body"><h3 class="card-title">No Node project found</h3><div class="text-secondary small">${esc((s.risk_reasons || [])[0] || '')}</div></div></div>`;
  }
  const risky = (s.native_or_risky_dependencies || []).map(x => {
    const versions = (x.versions || []).length ? ` <span class="text-secondary">(${esc(x.versions.join(', '))})</span>` : '';
    const paths = (x.paths || []).slice(0, 3).map(p => `<div class="text-secondary mt-1">↳ ${esc(Array.isArray(p) ? p.join(' → ') : p)}</div>`).join('');
    const info = x.informational ? ' <span class="badge bg-secondary-lt">informational</span>' : '';
    return `<li class="mb-2"><code>${esc(x.package)}</code>${versions}${info} — ${esc(x.why)}${paths}</li>`;
  }).join('');
  const reasons = (s.risk_reasons || []).map(x => `<li>${esc(x)}</li>`).join('');
  const pins = (s.runtime_pins || []).map(x => `<li><code>${esc(x.value)}</code> <span class="text-secondary">from ${esc(x.source)}</span></li>`).join('');
  const registry = s.registry_resolution ? `<div class="text-secondary small mt-3">npm metadata resolver: ${s.registry_resolution.visited_packages} packages inspected${s.registry_resolution.truncated ? ' · capped' : ''}${s.registry_resolution.lookup_errors ? ' · ' + s.registry_resolution.lookup_errors + ' lookup errors' : ''}</div>` : '';
  const projects = (s.discovered_projects || []).length > 1 ? `<div class="text-secondary small mt-2">Detected projects: ${esc(s.discovered_projects.join(', '))}</div>` : '';
  return `<div class="card mb-3">
    <div class="card-header"><h3 class="card-title">Static analysis</h3></div>
    <div class="card-body">
      <div class="row g-4 align-items-start">
        <div class="col-auto"><div class="text-secondary small text-uppercase">Risk score</div><div class="risk-score">${s.risk_score}/100</div></div>
        <div class="col text-end text-secondary small">${s.dependency_count} declared dependencies<br>${esc(s.lockfile || 'no npm lockfile')}<br>${esc((s.package_manager || {}).name || 'unknown')} · ${esc(s.selected_project || '.')}</div>
      </div>
      ${reasons ? `<hr><div class="fw-semibold mb-2">Signals</div><ul class="mb-0">${reasons}</ul>` : ''}
      ${pins ? `<hr><div class="fw-semibold mb-2">Runtime pins</div><ul class="small mb-0">${pins}</ul>` : ''}
      ${risky ? `<hr><div class="fw-semibold mb-2">Runtime-sensitive dependency evidence</div><ul class="small mb-0">${risky}</ul>` : ''}
      ${registry}${projects}
    </div></div>`;
}

function renderMatrix(matrix) {
  logStore.clear();
  if (!matrix) return '';
  if (!matrix.length) return '<div class="alert alert-secondary">No build matrix was run for this repository.</div>';

  const cards = matrix.map((r, i) => {
    const logs = (r.status === 'fail_install' || r.status === 'fail_toolchain')
      ? r.install.output
      : (r.status === 'fail_build' && r.build ? r.build.output : (r.status === 'unsupported_package_manager' ? r.install.output : ''));
    const sigs = (r.failure_signatures || []).map(s => '<li>' + esc(s.explanation) + '</li>').join('');
    const npmLabel = r.npm_requested || r.npm_version || 'bundled';
    const key = 'widlog-' + i + '-' + r.node_major + '-' + String(npmLabel).replace(/[^a-zA-Z0-9]/g, '');

    if (logs) {
      logStore.set(key, {
        title: 'Node ' + r.node_major + ' · npm ' + npmLabel,
        meta: r.node_version + ' · actual npm ' + (r.npm_version || 'unknown'),
        text: logs
      });
    }

    const logButton = logs
      ? '<div class="mt-auto pt-3"><button class="btn btn-sm btn-outline-secondary wid-open-log" type="button" data-log-key="' + esc(key) + '">Open log</button></div>'
      : '';

    return '<div class="col-12 col-md-6 col-xxl-4">' +
      '<div class="card matrix-card border-secondary-subtle h-100"><div class="card-body d-flex flex-column">' +
      '<div class="d-flex justify-content-between align-items-start gap-2"><div>' +
      '<strong>Node ' + r.node_major + ' · npm ' + esc(npmLabel) + '</strong>' +
      '<div class="small text-secondary mt-1">' + esc(r.node_version) + '<br>actual npm ' + esc(r.npm_version || '') + '</div>' +
      '</div>' + badgeFor(r.status) + '</div>' +
      '<div class="small mt-3">install: ' + (r.install.code === 0 ? '✓' : '✕') + ' · ' + r.install.duration_seconds + 's</div>' +
      (r.build ? '<div class="small">build: ' + (r.build.code === 0 ? '✓' : '✕') + ' · ' + r.build.duration_seconds + 's</div>' : '<div class="small text-secondary">build: not run</div>') +
      (sigs ? '<ul class="small mt-3 mb-0 ps-3">' + sigs + '</ul>' : '<div class="small text-secondary mt-3">No failure signature.</div>') +
      logButton +
      '</div></div></div>';
  }).join('');

  return '<div class="mb-3"><div class="d-flex justify-content-between align-items-center mb-2">' +
    '<div class="fw-semibold">Node × npm results</div><div class="small text-secondary">' + matrix.length + ' isolated toolchains</div>' +
    '</div><div class="row g-3">' + cards + '</div></div>';
}

window.openLog = function(key) {
  const item = logStore.get(key);
  if (!item || !logModalEl) return;
  $('#logModalLabel').textContent = item.title;
  $('#logModalMeta').textContent = item.meta;
  $('#logModalText').textContent = item.text;
  if (typeof logModalEl.showModal === 'function') logModalEl.showModal();
  else logModalEl.setAttribute('open', '');
};

document.addEventListener('click', (event) => {
  const button = event.target.closest('.wid-open-log');
  if (button) {
    window.openLog(button.dataset.logKey);
    return;
  }
  if (event.target && event.target.id === 'logModalClose' && logModalEl) {
    if (typeof logModalEl.close === 'function') logModalEl.close();
    else logModalEl.removeAttribute('open');
  }
});


function renderRegression(checks) {
  if (!checks || !checks.length) return '';
  return `<div class="card mb-3"><div class="card-header"><h3 class="card-title">Regression checks</h3></div><div class="list-group list-group-flush">${checks.map(c => `<div class="list-group-item d-flex justify-content-between align-items-center gap-3"><span>${esc(c.name)} <span class="text-secondary small">${esc(c.detail)}</span></span>${c.pass ? '<span class="badge bg-green-lt text-green">PASS</span>' : '<span class="badge bg-red-lt text-red">FAIL</span>'}</div>`).join('')}</div></div>`;
}


function renderDiagnosis(d) {
  if (!d) return '';
  const tone = d.confidence === 'high' ? 'success' : (d.confidence === 'medium' ? 'warning' : 'secondary');
  const evidence = (d.evidence || []).map(x => '<li>' + esc(x) + '</li>').join('');
  return '<div class="card border-' + tone + ' mb-3"><div class="card-body">' +
    '<div class="d-flex flex-wrap gap-2 align-items-center mb-2"><strong>Research signal</strong><span class="badge text-bg-' + tone + '">' + esc(String(d.confidence || 'unknown').toUpperCase()) + ' CONFIDENCE</span><span class="badge text-bg-secondary">' + esc(d.kind || '') + '</span></div>' +
    '<div>' + esc(d.headline || '') + '</div>' +
    (evidence ? '<ul class="small text-secondary mt-2 mb-0">' + evidence + '</ul>' : '') +
    '</div></div>';
}

function renderResult(data) {
  if (data.status === 'queued' || data.status === 'running') {
    resultArea.innerHTML = `<div class="alert alert-info"><div class="d-flex align-items-center gap-2"><span class="spinner-border spinner-border-sm"></span><div><strong>${esc(data.status)}</strong> · cloning, analyzing and building…</div></div></div>`;
    return;
  }
  if (data.status === 'failed') {
    resultArea.innerHTML = `<div class="alert alert-danger"><h4 class="alert-title">Experiment failed</h4><pre class="log mt-3">${esc(data.error)}</pre></div>`;
    return;
  }
  const r = data.result;
  resultArea.innerHTML = `<div class="card result-summary mb-3"><div class="card-body"><div class="fw-semibold">${esc(r.summary)}</div><div class="text-secondary small mt-1">${esc(r.repo)} · ${r.repo_size_mb} MB shallow clone${r.project_path ? ' · project ' + esc(r.project_path) : ''}</div></div></div>${renderDiagnosis(r.diagnosis)}${renderStatic(r.static)}${renderMatrix(r.matrix)}<div class="card"><div class="card-body py-3"><div class="text-secondary small">Experiment ID</div><code>${esc(data.id)}</code></div></div>`;
}

window.loadScan = async function(id) {
  const r = await fetch('/api/scans/' + encodeURIComponent(id));
  const data = await r.json();
  renderResult(data);
  resultArea.scrollIntoView({behavior: 'smooth', block: 'start'});
};
async function pollScan(id) {
  for (;;) {
    const r = await fetch('/api/scans/' + encodeURIComponent(id));
    const data = await r.json();
    renderResult(data);
    if (!['queued','running'].includes(data.status)) break;
    await new Promise(resolve => setTimeout(resolve, 1800));
  }
  loadHistory();
}

$('#scanForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const raw = $('#repoInput').value.trim().replace(/^https?:\/\/github\.com\//, '').replace(/\/$/, '');
  const runtimes = [...document.querySelectorAll('.runtime-check:checked')].map(x => Number(x.value));
  const mode = document.querySelector('input[name="mode"]:checked').value;
  if (!runtimes.length) return alert('Choose at least one Node runtime.');
  resultArea.innerHTML = '<div class="alert alert-info"><span class="spinner-border spinner-border-sm me-2"></span>Queueing experiment…</div>';
  const r = await fetch('/api/scans', {method:'POST', headers:headers(), body:JSON.stringify({
    repo_url:'https://github.com/' + raw,
    branch:$('#branchInput').value.trim() || null,
    project_path:$('#projectPathInput').value.trim() || null,
    mode, runtimes
  })});
  const data = await r.json();
  if (!r.ok) {
    resultArea.innerHTML = `<div class="alert alert-danger">${esc(data.error || 'Could not start scan')}</div>`;
    return;
  }
  pollScan(data.id);
});

if ($('#selfTestBtn')) $('#selfTestBtn').addEventListener('click', async () => {
  resultArea.innerHTML = '<div class="alert alert-info"><span class="spinner-border spinner-border-sm me-2"></span>Running full-build and regression self-test…</div>';
  const r = await fetch('/api/self-test', {method:'POST', headers:headers(false)});
  const data = await r.json();
  if (!r.ok) {
    resultArea.innerHTML = `<div class="alert alert-danger">${esc(data.error || 'Self-test failed')}</div>`;
    return;
  }
  resultArea.innerHTML = `<div class="card result-summary mb-3"><div class="card-body"><strong>${esc(data.summary)}</strong></div></div>${renderRegression(data.regression_checks)}${renderStatic(data.static)}${renderMatrix(data.matrix)}`;
});

$('#refreshHistory').addEventListener('click', loadHistory);
checkHealth();
loadHistory();
