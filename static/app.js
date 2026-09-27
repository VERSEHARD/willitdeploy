const $ = (s) => document.querySelector(s);
const resultArea = $('#resultArea');
const historyEl = $('#history');

function token() {
  const input = $('#tokenInput');
  if (!input) return '';
  if (input.value) localStorage.setItem('wid_scan_token', input.value);
  return input.value || localStorage.getItem('wid_scan_token') || '';
}

if ($('#tokenInput')) $('#tokenInput').value = localStorage.getItem('wid_scan_token') || '';

function headers(json=true) {
  const h = {};
  if (json) h['Content-Type'] = 'application/json';
  const t = token();
  if (t) h['X-Scan-Token'] = t;
  return h;
}

function esc(v='') {
  return String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));
}

function badgeFor(status) {
  if (status === 'pass') return '<span class="badge badge-soft-success">PASS</span>';
  if (status === 'completed') return '<span class="badge badge-soft-success">DONE</span>';
  if (status === 'unsupported_package_manager') return '<span class="badge text-bg-secondary">SKIP</span>';
  if (status === 'queued' || status === 'running') return '<span class="badge badge-soft-warning">RUNNING</span>';
  return '<span class="badge badge-soft-danger">FAIL</span>';
}

async function checkHealth() {
  try {
    const r = await fetch('/api/health');
    const h = await r.json();
    $('#healthDot').className = 'status-dot bg-success';
    $('#healthText').textContent = `online · v${h.version} · Python ${h.python}`;
  } catch {
    $('#healthDot').className = 'status-dot bg-danger';
    $('#healthText').textContent = 'scanner unavailable';
  }
}

async function loadHistory() {
  try {
    const r = await fetch('/api/scans?limit=12');
    const rows = await r.json();
    historyEl.innerHTML = rows.length ? rows.map(row => `
      <button class="list-group-item list-group-item-action text-start" onclick="loadScan('${row.id}')">
        <div class="d-flex justify-content-between gap-2">
          <strong class="small repo-link">${esc(row.repo_url.replace('https://github.com/',''))}</strong>
          ${badgeFor(row.status)}
        </div>
        <div class="small text-secondary mt-1">${esc(row.mode)} · Node ${row.runtimes.join('/')} ${row.project_path ? '· ' + esc(row.project_path) : ''}</div>
      </button>`).join('') : '<div class="p-3 text-secondary small">No experiments yet.</div>';
  } catch {
    historyEl.innerHTML = '<div class="p-3 text-danger small">Could not load history.</div>';
  }
}

function renderStatic(s) {
  if (!s.node_project_found) {
    return `<div class="card border-secondary-subtle mb-3"><div class="card-body p-4">
      <div class="fw-semibold">No Node project found</div>
      <div class="text-secondary small mt-2">${esc((s.risk_reasons || [])[0] || '')}</div>
    </div></div>`;
  }

  const risky = (s.native_or_risky_dependencies || []).map(x => {
    const versions = (x.versions || []).length ? ` <span class="text-secondary">(${esc(x.versions.join(', '))})</span>` : '';
    const paths = (x.paths || []).slice(0, 3).map(p => `<div class="text-secondary mt-1">↳ ${esc(Array.isArray(p) ? p.join(' → ') : p)}</div>`).join('');
    const info = x.informational ? ' <span class="badge text-bg-secondary">informational</span>' : '';
    return `<li><code>${esc(x.package)}</code>${versions}${info} — ${esc(x.why)}${paths}</li>`;
  }).join('');
  const reasons = (s.risk_reasons || []).map(x => `<li>${esc(x)}</li>`).join('');
  const pins = (s.runtime_pins || []).map(x => `<li><code>${esc(x.value)}</code> <span class="text-secondary">from ${esc(x.source)}</span></li>`).join('');
  const registry = s.registry_resolution ? `<div class="small text-secondary mt-2">npm metadata resolver: ${s.registry_resolution.visited_packages} packages inspected${s.registry_resolution.truncated ? ' · capped' : ''}${s.registry_resolution.lookup_errors ? ' · ' + s.registry_resolution.lookup_errors + ' lookup errors' : ''}</div>` : '';
  const projects = (s.discovered_projects || []).length > 1 ? `<div class="small text-secondary mt-2">Detected projects: ${esc(s.discovered_projects.join(', '))}</div>` : '';

  return `
    <div class="card border-secondary-subtle mb-3">
      <div class="card-body p-4">
        <div class="d-flex justify-content-between align-items-start gap-3">
          <div><div class="text-secondary small">STATIC RISK</div><div class="risk-score">${s.risk_score}/100</div></div>
          <div class="text-end small text-secondary">${s.dependency_count} declared dependencies<br>${esc(s.lockfile || 'no npm lockfile')}<br>${esc((s.package_manager || {}).name || 'unknown')} · ${esc(s.selected_project || '.')}</div>
        </div>
        ${reasons ? `<ul class="mt-3 mb-0">${reasons}</ul>` : ''}
        ${pins ? `<hr><div class="fw-semibold mb-2">Runtime pins</div><ul class="small mb-0">${pins}</ul>` : ''}
        ${risky ? `<hr><div class="fw-semibold mb-2">Runtime-sensitive dependency evidence</div><ul class="small mb-0">${risky}</ul>` : ''}
        ${registry}${projects}
      </div>
    </div>`;
}

function renderMatrix(matrix) {
  if (!matrix) return '';
  if (!matrix.length) return '<div class="alert alert-secondary">No build matrix was run for this repository.</div>';
  return `<div class="row g-3 mb-3">${matrix.map(r => {
    const logs = r.status === 'fail_install' ? r.install.output : (r.status === 'fail_build' && r.build ? r.build.output : (r.status === 'unsupported_package_manager' ? r.install.output : ''));
    const sigs = (r.failure_signatures || []).map(s => `<li>${esc(s.explanation)}</li>`).join('');
    return `<div class="col-md-4">
      <div class="card matrix-card border-secondary-subtle h-100">
        <div class="card-body">
          <div class="d-flex justify-content-between align-items-center"><strong>Node ${r.node_major}</strong>${badgeFor(r.status)}</div>
          <div class="small text-secondary mt-1">${esc(r.node_version)}${r.npm_version ? ' · npm ' + esc(r.npm_version) : ''}</div>
          <div class="small mt-3">install: ${r.install.code === 0 ? '✓' : '✕'} · ${r.install.duration_seconds}s</div>
          ${r.build ? `<div class="small">build: ${r.build.code === 0 ? '✓' : '✕'} · ${r.build.duration_seconds}s</div>` : '<div class="small text-secondary">build script: none/skip</div>'}
          ${sigs ? `<ul class="small mt-3 mb-0 ps-3">${sigs}</ul>` : ''}
          ${logs ? `<button class="btn btn-sm btn-outline-secondary mt-3" data-bs-toggle="collapse" data-bs-target="#log${r.node_major}">Show log</button>` : ''}
        </div>
      </div>
      ${logs ? `<div class="collapse mt-2" id="log${r.node_major}"><pre class="log">${esc(logs)}</pre></div>` : ''}
    </div>`;
  }).join('')}</div>`;
}

function renderRegression(checks) {
  if (!checks || !checks.length) return '';
  return `<div class="card border-secondary-subtle mb-3"><div class="card-body p-4"><div class="fw-semibold mb-2">Regression checks</div>${checks.map(c => `<div class="d-flex justify-content-between border-top py-2"><span>${esc(c.name)} <span class="text-secondary small">${esc(c.detail)}</span></span>${c.pass ? '<span class="badge badge-soft-success">PASS</span>' : '<span class="badge badge-soft-danger">FAIL</span>'}</div>`).join('')}</div></div>`;
}

function renderResult(data) {
  if (data.status === 'queued' || data.status === 'running') {
    resultArea.innerHTML = `<div class="alert alert-secondary"><span class="spinner-border spinner-border-sm me-2"></span>${esc(data.status)}… cloning/analyzing/building.</div>`;
    return;
  }
  if (data.status === 'failed') {
    resultArea.innerHTML = `<div class="alert alert-danger"><strong>Experiment failed.</strong><pre class="log mt-3 mb-0">${esc(data.error)}</pre></div>`;
    return;
  }
  const r = data.result;
  resultArea.innerHTML = `
    <div class="alert alert-primary"><strong>${esc(r.summary)}</strong><div class="small mt-1">${esc(r.repo)} · ${r.repo_size_mb} MB shallow clone${r.project_path ? ' · project ' + esc(r.project_path) : ''}</div></div>
    ${renderStatic(r.static)}
    ${renderMatrix(r.matrix)}
    <div class="card border-secondary-subtle"><div class="card-body"><div class="small text-secondary">Experiment ID</div><code>${esc(data.id)}</code></div></div>`;
}

window.loadScan = async function(id) {
  const r = await fetch('/api/scans/' + encodeURIComponent(id));
  const data = await r.json();
  renderResult(data);
};

async function pollScan(id) {
  for (;;) {
    const r = await fetch('/api/scans/' + encodeURIComponent(id));
    const data = await r.json();
    renderResult(data);
    if (!['queued','running'].includes(data.status)) break;
    await new Promise(resolve => setTimeout(resolve, 2200));
  }
  loadHistory();
}

$('#scanForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const raw = $('#repoInput').value.trim().replace(/^https?:\/\/github\.com\//, '').replace(/\/$/, '');
  const runtimes = [...document.querySelectorAll('.runtime-check:checked')].map(x => Number(x.value));
  const mode = document.querySelector('input[name="mode"]:checked').value;
  if (!runtimes.length) return alert('Choose at least one Node runtime.');
  resultArea.innerHTML = '<div class="alert alert-secondary"><span class="spinner-border spinner-border-sm me-2"></span>Queueing experiment…</div>';

  const r = await fetch('/api/scans', {
    method: 'POST', headers: headers(), body: JSON.stringify({
      repo_url: 'https://github.com/' + raw,
      branch: $('#branchInput').value.trim() || null,
      project_path: $('#projectPathInput').value.trim() || null,
      mode, runtimes
    })
  });
  const data = await r.json();
  if (!r.ok) {
    resultArea.innerHTML = `<div class="alert alert-danger">${esc(data.error || 'Could not start scan')}</div>`;
    return;
  }
  pollScan(data.id);
});

if ($('#selfTestBtn')) $('#selfTestBtn').addEventListener('click', async () => {
  resultArea.innerHTML = '<div class="alert alert-secondary"><span class="spinner-border spinner-border-sm me-2"></span>Running full-build + regression self-test… first run may download Node runtimes.</div>';
  const r = await fetch('/api/self-test', {method:'POST', headers:headers(false)});
  const data = await r.json();
  if (!r.ok) return resultArea.innerHTML = `<div class="alert alert-danger">${esc(data.error || 'Self-test failed')}</div>`;
  resultArea.innerHTML = `<div class="alert alert-primary"><strong>${esc(data.summary)}</strong></div>${renderRegression(data.regression_checks)}${renderStatic(data.static)}${renderMatrix(data.matrix)}`;
});

$('#refreshHistory').addEventListener('click', loadHistory);
checkHealth();
loadHistory();
