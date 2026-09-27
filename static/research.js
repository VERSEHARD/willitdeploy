(() => {
  const q = (s) => document.querySelector(s);
  const escR = (v = '') => String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));

  function setWidTab(name) {
    const scanner = q('#scannerTab');
    const research = q('#researchTab');
    if (!scanner || !research) return;
    const showResearch = name === 'research';
    scanner.classList.toggle('d-none', showResearch);
    research.classList.toggle('d-none', !showResearch);
    document.querySelectorAll('.wid-tab-btn').forEach(btn => {
      const active = btn.dataset.widTab === name;
      btn.classList.toggle('btn-primary', active);
      btn.classList.toggle('btn-outline-secondary', !active);
    });
    if (showResearch) loadResearchStatus();
  }

  document.querySelectorAll('.wid-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => setWidTab(btn.dataset.widTab));
  });

  function compactOutcome(matrix, npmVersion) {
    const rows = (matrix || []).filter(r => String(r.npm_requested || r.npm_version) === npmVersion);
    if (!rows.length) return '—';
    const pass = rows.filter(r => r.status === 'pass').length;
    if (pass === rows.length) return 'PASS';
    if (pass === 0) return 'FAIL';
    return pass + '/' + rows.length + ' PASS';
  }

  function researchClassBadge(kind) {
    const map = {
      npm11_break_candidate: ['bg-red-lt text-red', 'npm 11 BREAK'],
      npm11_fix_candidate: ['bg-green-lt text-green', 'npm 11 FIX'],
      clean: ['bg-green-lt text-green', 'CLEAN'],
      baseline_fail: ['bg-yellow-lt text-yellow', 'BASELINE FAIL'],
      mixed: ['bg-yellow-lt text-yellow', 'MIXED'],
      no_npm_lockfile: ['bg-secondary-lt', 'NO LOCK'],
      no_node_project: ['bg-secondary-lt', 'N/A'],
      probe_error: ['bg-red-lt text-red', 'ERROR']
    };
    const pair = map[kind] || ['bg-secondary-lt', String(kind || 'unknown').toUpperCase()];
    return '<span class="badge ' + pair[0] + '">' + escR(pair[1]) + '</span>';
  }

  function confirmationCell(matrix, nodeMajor) {
    const rows = (matrix || []).filter(r => Number(r.node_major) === Number(nodeMajor));
    if (!rows.length) return '—';
    const p10 = rows.find(r => r.npm_requested === '10.9.9');
    const p11 = rows.find(r => r.npm_requested === '11.19.0');
    const icon = r => !r ? '—' : (r.status === 'pass' ? '✓' : '✕');
    return '<span class="text-nowrap">10 ' + icon(p10) + ' · 11 ' + icon(p11) + '</span>';
  }

  function renderResearchState(s) {
    if (!q('#researchStatusBadge')) return;
    q('#researchBatchId').textContent = s.batch_id || 'research batch';

    const status = String(s.status || 'pending').toUpperCase();
    const statusCls = s.status === 'completed' ? 'bg-green-lt text-green' : (s.status === 'running' ? 'bg-blue-lt text-blue' : 'bg-secondary-lt');
    q('#researchStatusBadge').className = 'badge ' + statusCls;
    q('#researchStatusBadge').textContent = status;

    q('#researchScreened').textContent = String(s.probe_completed || 0) + '/' + String(s.targets_total || 0);
    q('#researchCandidates').textContent = s.candidates || 0;
    q('#researchConfirmed').textContent = s.confirmed || 0;
    if (q('#researchRepairs')) q('#researchRepairs').textContent = s.repair_verified || 0;
    q('#researchMoney').textContent = '$' + Number(s.money_earned_usd || 0).toFixed(2);
    q('#researchStage').textContent = 'Stage: ' + String(s.stage || 'queued');

    const percent = Math.max(0, Math.min(100, Number(s.progress_percent || 0)));
    q('#researchProgressBar').style.width = percent + '%';
    q('#researchProgressText').textContent = percent.toFixed(1) + '% · ' + (s.progress_completed || 0) + '/' + (s.progress_total || 0);

    if (s.current) {
      q('#researchCurrent').className = 'alert alert-info mb-0';
      q('#researchCurrent').innerHTML = '<strong>Running now:</strong> ' + escR(s.current.repo || '') +
        ' <span class="text-secondary">· ' + escR(s.current.phase || '') + ' ' + escR(s.current.index || '') + '/' + escR(s.current.total || '') + '</span>';
    } else if (s.status === 'completed') {
      q('#researchCurrent').className = 'alert alert-success mb-0';
      q('#researchCurrent').innerHTML = '<strong>Batch finished.</strong> Results below are persisted on the Railway volume.';
    } else {
      q('#researchCurrent').className = 'alert alert-secondary mb-0';
      q('#researchCurrent').textContent = 'Research worker is queued.';
    }

    const gatePassed = String(s.monetization_gate || '').startsWith('PASSED');
    q('#researchGate').innerHTML =
      '<div class="' + (gatePassed ? 'text-success' : '') + '">' + escR(s.monetization_gate || 'No gate result yet.') + '</div>' +
      '<div class="small text-secondary mt-2">Revenue stays at $0 until cash is actually received. No projected money is counted.</div>';

    const results = s.results || [];
    q('#researchResults').innerHTML = results.length ? results.map(r =>
      '<tr><td><strong>' + escR(r.repo || '') + '</strong></td>' +
      '<td class="text-secondary">' + escR(r.label || '') + '</td>' +
      '<td>' + researchClassBadge(r.classification) + '</td>' +
      '<td>' + escR(compactOutcome(r.matrix, '10.9.9')) + '</td>' +
      '<td>' + escR(compactOutcome(r.matrix, '11.19.0')) + '</td></tr>'
    ).join('') : '<tr><td colspan="5" class="text-secondary">No results yet.</td></tr>';

    const repairs = s.repairs || [];
    const repairEl = q('#repairResults');
    if (repairEl) {
      repairEl.innerHTML = repairs.length ? repairs.map(r => {
        const verified = r.verified
          ? '<span class="badge bg-green-lt text-green">VERIFIED</span>'
          : '<span class="badge bg-red-lt text-red">' + escR(String(r.status || 'FAILED').toUpperCase()) + '</span>';
        const changed = (r.changed_paths || []).length ? escR((r.changed_paths || []).join(', ')) : '—';
        const after = r.after ? 'npm10 ' + escR(r.after.npm_old || '—') + ' · npm11 ' + escR(r.after.npm_new || '—') : '—';
        return '<tr><td><strong>' + escR(r.repo || '') + '</strong></td>' +
          '<td>' + verified + '</td>' +
          '<td class="text-secondary">' + changed + '</td>' +
          '<td>' + escR(String(r.patch_bytes || 0)) + ' B</td>' +
          '<td>' + after + '</td></tr>';
      }).join('') : '<tr><td colspan="5" class="text-secondary">Repair validation has not started.</td></tr>';
    }

    const confirms = s.confirmations || [];
    q('#researchConfirmations').innerHTML = confirms.length ? confirms.map(r =>
      '<tr><td><strong>' + escR(r.repo || '') + '</strong></td>' +
      '<td>' + (r.confirmed ? '<span class="badge bg-green-lt text-green">YES</span>' : '<span class="badge bg-secondary-lt">NO</span>') + '</td>' +
      '<td>' + confirmationCell(r.matrix, 22) + '</td>' +
      '<td>' + confirmationCell(r.matrix, 24) + '</td>' +
      '<td>' + confirmationCell(r.matrix, 26) + '</td></tr>'
    ).join('') : '<tr><td colspan="5" class="text-secondary">Confirmation stage has not started.</td></tr>';
  }

  let timer = null;
  async function loadResearchStatus() {
    try {
      const r = await fetch('/api/research/status', {cache: 'no-store'});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'research status failed');
      renderResearchState(data);
      if (data.status === 'running' || data.status === 'pending') {
        if (!timer) timer = setInterval(loadResearchStatus, 2500);
      } else if (timer) {
        clearInterval(timer);
        timer = null;
      }
    } catch (err) {
      const current = q('#researchCurrent');
      if (current) {
        current.className = 'alert alert-danger mb-0';
        current.textContent = 'Could not load research status: ' + String(err);
      }
    }
  }

  window.WIDResearch = {load: loadResearchStatus, tab: setWidTab};
  loadResearchStatus();
})();