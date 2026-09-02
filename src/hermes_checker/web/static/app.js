/* Hermes Checker — minimal SPA bootstrapper. No framework. */
(() => {
  const content = document.getElementById('content');
  const buttons = document.querySelectorAll('nav button');
  const healthEl = document.getElementById('health');

  let activeView = 'live';
  let activeSessionId = null;
  let liveTimer = null;

  function fmt(n) {
    if (n === null || n === undefined) return '—';
    if (typeof n === 'number') {
      if (n > 1000) return n.toLocaleString();
      return n.toString();
    }
    return n;
  }

  function pct(n) {
    if (n === null || n === undefined) return '—';
    return (n * 100).toFixed(1) + '%';
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  async function fetchJson(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json();
  }

  function show(templateId) {
    const tpl = document.getElementById(templateId);
    content.innerHTML = '';
    content.appendChild(tpl.content.cloneNode(true));
  }

  function activate(name) {
    activeView = name;
    buttons.forEach((b) => b.classList.toggle('active', b.dataset.view === name));
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    if (name === 'live') {
      renderLive();
      liveTimer = setInterval(renderLive, 5000);
    } else if (name === 'session') {
      renderSessionList();
    } else if (name === 'analytics') {
      renderAnalytics();
    } else if (name === 'insights') {
      renderInsights();
    }
  }

  async function renderLive() {
    show('tmpl-live');
    const data = await fetchJson('/api/live');
    const meta = document.getElementById('live-meta');
    const totals = document.getElementById('live-totals');
    const events = document.getElementById('live-events');
    if (!data.session) {
      meta.innerHTML = '<p class="hint">No sessions recorded yet.</p>';
      totals.innerHTML = '';
      events.textContent = '';
      return;
    }
    const s = data.session;
    meta.innerHTML = `
      <div class="kv">
        <span class="k">Session</span><span class="v"><code>${escapeHtml(s.session_id)}</code></span>
        <span class="k">Profile</span><span class="v">${escapeHtml(s.profile || '—')}</span>
        <span class="k">Platform</span><span class="v">${escapeHtml(s.platform || '—')}</span>
        <span class="k">Experiment</span><span class="v">${escapeHtml(s.experiment || '—')}</span>
        <span class="k">Started</span><span class="v">${escapeHtml(new Date((s.started_at || 0) * 1000).toISOString())}</span>
      </div>
    `;
    const t = data.totals || {};
    totals.innerHTML = `
      <div class="kv">
        <span class="k">API requests</span><span class="v">${fmt(t.api_requests)}</span>
        <span class="k">Provider prompt</span><span class="v">${fmt(t.prompt_tokens)}</span>
        <span class="k">Provider cached</span><span class="v">${fmt(t.cached_tokens)}</span>
        <span class="k">Provider fresh</span><span class="v">${fmt(t.fresh_tokens)}</span>
        <span class="k">Cache hit</span><span class="v">${pct(t.cache_hit_ratio)}</span>
        <span class="k">Output</span><span class="v">${fmt(t.output_tokens)}</span>
        <span class="k">Reasoning</span><span class="v">${fmt(t.reasoning_tokens)}</span>
      </div>
    `;
    events.textContent = JSON.stringify(data.events || [], null, 2);
  }

  async function renderSessionList() {
    show('tmpl-sessions');
    const data = await fetchJson('/api/sessions');
    const ul = document.getElementById('session-list');
    ul.innerHTML = '';
    if (!data.sessions || !data.sessions.length) {
      ul.innerHTML = '<li class="hint">No sessions recorded yet.</li>';
      return;
    }
    for (const s of data.sessions) {
      const li = document.createElement('li');
      li.innerHTML = `
        <div><code>${escapeHtml(s.session_id)}</code>
          ${s.experiment ? `<span class="badge info">${escapeHtml(s.experiment)}</span>` : ''}
          ${s.profile ? `<span class="badge good">${escapeHtml(s.profile)}</span>` : ''}
        </div>
        <div class="esc">started ${escapeHtml(new Date((s.started_at || 0) * 1000).toISOString())}</div>
      `;
      li.onclick = () => {
        activeSessionId = s.session_id;
        renderSessionDetail();
      };
      ul.appendChild(li);
    }
  }

  async function renderSessionDetail() {
    if (!activeSessionId) return renderSessionList();
    show('tmpl-session-detail');
    document.getElementById('session-id').textContent = activeSessionId;
    const data = await fetchJson(`/api/sessions/${encodeURIComponent(activeSessionId)}`);
    const meta = document.getElementById('session-meta');
    const totals = document.getElementById('session-totals');
    const s = data.session || {};
    meta.innerHTML = `
      <div class="kv">
        <span class="k">Profile</span><span class="v">${escapeHtml(s.profile || '—')}</span>
        <span class="k">Platform</span><span class="v">${escapeHtml(s.platform || '—')}</span>
        <span class="k">Experiment</span><span class="v">${escapeHtml(s.experiment || '—')}</span>
        <span class="k">Started</span><span class="v">${escapeHtml(new Date((s.started_at || 0) * 1000).toISOString())}</span>
        <span class="k">Ended</span><span class="v">${s.ended_at ? escapeHtml(new Date(s.ended_at * 1000).toISOString()) : '(ongoing)'}</span>
      </div>
    `;
    const t = data.totals || {};
    totals.innerHTML = `
      <div class="kv">
        <span class="k">API requests</span><span class="v">${fmt(t.api_requests)}</span>
        <span class="k">Prompt</span><span class="v">${fmt(t.prompt_tokens)}</span>
        <span class="k">Cached</span><span class="v">${fmt(t.cached_tokens)}</span>
        <span class="k">Fresh</span><span class="v">${fmt(t.fresh_tokens)}</span>
        <span class="k">Cache hit</span><span class="v">${pct(t.cache_hit_ratio)}</span>
        <span class="k">Output</span><span class="v">${fmt(t.output_tokens)}</span>
        <span class="k">Reasoning</span><span class="v">${fmt(t.reasoning_tokens)}</span>
        <span class="k">Total</span><span class="v">${fmt(t.total_tokens)}</span>
      </div>
    `;

    const reqBody = document.querySelector('#session-requests tbody');
    reqBody.innerHTML = '';
    for (const r of (data.api_requests || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(new Date((r.started_at || 0) * 1000).toISOString().slice(11, 19))}</td>
        <td>${escapeHtml(r.model || '—')}</td>
        <td>${escapeHtml(r.provider || '—')}</td>
        <td>${fmt(r.prompt_tokens)}</td>
        <td>${fmt(r.cache_read_tokens)}</td>
        <td>${fmt(r.output_tokens)}</td>
        <td>${r.ttft_s != null ? r.ttft_s.toFixed(2) + 's' : '—'}</td>
        <td>${r.tokens_per_second != null ? r.tokens_per_second.toFixed(1) : '—'}</td>
        <td>${pct(r.cache_hit_ratio)}</td>
      `;
      reqBody.appendChild(tr);
    }

    const compBody = document.querySelector('#session-components tbody');
    compBody.innerHTML = '';
    const compTotals = {};
    for (const c of (data.components || [])) {
      compTotals[c.component] = (compTotals[c.component] || 0) + c.estimated_tokens;
    }
    for (const [name, tokens] of Object.entries(compTotals).sort((a, b) => b[1] - a[1])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(name)}</td>
        <td>${fmt(tokens)}</td>
        <td class="esc">estimated</td>
        <td class="esc">—</td>
      `;
      compBody.appendChild(tr);
    }

    const toolBody = document.querySelector('#session-tools tbody');
    toolBody.innerHTML = '';
    for (const t of (data.tool_calls || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(new Date((t.started_at || 0) * 1000).toISOString().slice(11, 19))}</td>
        <td>${escapeHtml(t.tool_name)}</td>
        <td>${escapeHtml(t.category)}</td>
        <td>${t.duration_ms != null ? t.duration_ms.toFixed(1) : '—'}</td>
        <td>${escapeHtml(t.status || '—')}</td>
        <td>${fmt(t.output_chars)}</td>
      `;
      toolBody.appendChild(tr);
    }
  }

  async function renderAnalytics() {
    show('tmpl-analytics');
    const sel = document.getElementById('window-select');
    const target = document.getElementById('analytics-output');
    sel.onchange = renderAnalytics;
    const data = await fetchJson(`/api/analytics?window=${sel.value}`);
    const t = data.totals || {};
    target.innerHTML = `
      <div class="kv">
        <span class="k">Sessions</span><span class="v">${fmt(t.sessions)}</span>
        <span class="k">API requests</span><span class="v">${fmt(t.api_requests)}</span>
        <span class="k">Tool calls</span><span class="v">${fmt(t.tool_calls)}</span>
        <span class="k">Provider prompt tokens</span><span class="v">${fmt(t.provider_prompt_tokens)}</span>
        <span class="k">Provider cached</span><span class="v">${fmt(t.provider_cached_tokens)}</span>
        <span class="k">Provider output</span><span class="v">${fmt(t.provider_output_tokens)}</span>
        <span class="k">Provider reasoning</span><span class="v">${fmt(t.provider_reasoning_tokens)}</span>
        <span class="k">Cache hit (avg)</span><span class="v">${pct(t.cache_hit_ratio_avg)}</span>
        <span class="k">TPS (avg)</span><span class="v">${t.tps_avg != null ? t.tps_avg.toFixed(1) : '—'}</span>
        <span class="k">TTFT (avg)</span><span class="v">${t.ttft_avg != null ? t.ttft_avg.toFixed(2) + 's' : '—'}</span>
      </div>
    `;
  }

  async function renderInsights() {
    show('tmpl-insights');
    const data = await fetchJson('/api/insights');
    const ul = document.getElementById('insights-list');
    ul.innerHTML = '';
    if (!data.insights || !data.insights.length) {
      ul.innerHTML = '<li class="hint">No findings yet. Enable the analyzer or run a session.</li>';
      return;
    }
    for (const f of data.insights) {
      const li = document.createElement('li');
      const sev = (f.severity || 'OBSERVATION').toLowerCase();
      li.innerHTML = `
        <div>
          <span class="badge ${sev === 'potential_waste' ? 'bad' : sev === 'high_overhead' ? 'warn' : sev === 'repeated_content' ? 'info' : 'good'}">${escapeHtml(f.severity)}</span>
          <strong>${escapeHtml(f.finding_kind)}</strong>
          <span class="esc">conf ${fmt(f.confidence)}</span>
        </div>
        <div>${escapeHtml(f.message || '')}</div>
        <div class="esc">session <code>${escapeHtml(f.session_id || '—')}</code></div>
      `;
      ul.appendChild(li);
    }
  }

  async function ping() {
    try {
      const data = await fetchJson('/api/health');
      healthEl.textContent = `schema v${data.schema_version} · ${new Date(data.now * 1000).toISOString().slice(11, 19)}`;
    } catch (e) {
      healthEl.textContent = 'unhealthy';
    }
  }

  buttons.forEach((b) => b.addEventListener('click', () => activate(b.dataset.view)));

  ping();
  setInterval(ping, 10000);
  activate('live');
})();