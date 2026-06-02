"""Self-contained HTML for the ops dashboard (served by GET /tradein/dashboard)."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>GreenBay Evaluator — Ops Dashboard</title>
<style>
  :root { --forest:#14532d; --ok:#16a34a; --bad:#dc2626; --warn:#d97706; --bg:#f8fafc; --card:#fff; --line:#e2e8f0; --muted:#64748b; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:var(--bg); color:#0f172a; }
  header { background:var(--forest); color:#fff; padding:16px 24px; display:flex; justify-content:space-between; align-items:center; }
  header h1 { font-size:18px; margin:0; }
  header .meta { font-size:12px; opacity:.85; }
  .wrap { padding:24px; max-width:1200px; margin:0 auto; }
  h2 { font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:28px 0 12px; }
  .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card .label { font-size:12px; color:var(--muted); }
  .card .value { font-size:24px; font-weight:700; margin-top:4px; }
  .svc { display:flex; align-items:center; gap:10px; }
  .dot { width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
  .dot.ok { background:var(--ok); } .dot.bad { background:var(--bad); }
  .svc .name { font-weight:600; font-size:14px; }
  .svc .detail { font-size:12px; color:var(--muted); }
  .svc .lat { margin-left:auto; font-size:12px; color:var(--muted); }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; font-size:13px; }
  th,td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); }
  th { background:#f1f5f9; font-size:11px; text-transform:uppercase; color:var(--muted); }
  tr:last-child td { border-bottom:none; }
  .pill { padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; }
  .pill.ok { background:#dcfce7; color:#166534; } .pill.bad { background:#fee2e2; color:#991b1b; }
  .pill.mid { background:#fef9c3; color:#854d0e; }
  .err { color:var(--bad); padding:12px; }
  .refresh { font-size:12px; color:#fff; opacity:.85; }
</style>
</head>
<body>
<header>
  <h1>GreenBay Evaluator — Ops Dashboard</h1>
  <div class="refresh">auto-refresh 60s · <span id="ts">—</span></div>
</header>
<div id="gate" style="display:none; max-width:360px; margin:80px auto; text-align:center;">
  <div class="card">
    <h2 style="margin-top:0">Enter access key</h2>
    <input id="keyInput" type="password" class="" placeholder="DASHBOARD_KEY"
       style="width:100%; padding:10px; border:1px solid var(--line); border-radius:8px; font-size:14px;"/>
    <button id="keyBtn" style="margin-top:10px; width:100%; padding:10px; background:var(--forest); color:#fff; border:none; border-radius:8px; font-weight:600; cursor:pointer;">Open dashboard</button>
    <p id="keyErr" class="err" style="display:none;">Wrong key — try again.</p>
  </div>
</div>

<div id="wrap" class="wrap" style="display:none;">
  <div id="banner"></div>

  <h2>Service Health</h2>
  <div id="health" class="grid"></div>

  <h2>Pricing Performance (last 30 days)</h2>
  <div id="kpis" class="grid"></div>

  <h2>Trends Over Time</h2>
  <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(320px,1fr));">
    <div class="card"><div class="label">Evaluations per day</div><div id="chartVolume"></div></div>
    <div class="card"><div class="label">Avg confidence per day (is it improving?)</div><div id="chartConf"></div></div>
  </div>

  <h2>AI vs In-House Evaluator</h2>
  <div id="accuracy" class="grid"></div>

  <h2>Calibration vs Real Sold Prices (back-test)</h2>
  <div id="calibwrap"></div>

  <h2>By Category</h2>
  <div id="catwrap"></div>

  <h2>Recent Evaluations</h2>
  <div id="recentwrap"></div>
</div>

<script>
let KEY = new URLSearchParams(location.search).get('key') || sessionStorage.getItem('gb_key') || '';

async function getJSON(path){
  const r = await fetch(path + (path.includes('?')?'&':'?') + 'key=' + encodeURIComponent(KEY));
  if(r.status === 403) { const e = new Error('forbidden'); e.forbidden = true; throw e; }
  if(!r.ok) throw new Error(path + ' -> ' + r.status);
  return r.json();
}
const fmt = n => (n==null? '—' : Number(n).toLocaleString());

// Lightweight inline SVG charts (no external libs).
function lineChart(points, valueKey, color){
  if(!points || !points.length) return '<div class="detail">No data yet.</div>';
  const W=300,H=90,P=6;
  const vals=points.map(p=>p[valueKey]||0);
  const max=Math.max(...vals,1), min=Math.min(...vals,0);
  const x=i=>P+(i*(W-2*P)/Math.max(points.length-1,1));
  const y=v=>H-P-((v-min)/Math.max(max-min,1))*(H-2*P);
  const d=points.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(p[valueKey]||0).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2"/>
    <text x="${P}" y="12" font-size="10" fill="#94a3b8">${max.toFixed(0)}</text>
    <text x="${P}" y="${H-2}" font-size="10" fill="#94a3b8">${min.toFixed(0)}</text>
  </svg>`;
}
function barChart(points, valueKey, color){
  if(!points || !points.length) return '<div class="detail">No data yet.</div>';
  const W=300,H=90,P=6, n=points.length;
  const vals=points.map(p=>p[valueKey]||0); const max=Math.max(...vals,1);
  const bw=(W-2*P)/n;
  const bars=points.map((p,i)=>{const h=((p[valueKey]||0)/max)*(H-2*P);
    return `<rect x="${(P+i*bw).toFixed(1)}" y="${(H-P-h).toFixed(1)}" width="${Math.max(bw-1,1).toFixed(1)}" height="${h.toFixed(1)}" fill="${color}"/>`;}).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">${bars}
    <text x="${P}" y="12" font-size="10" fill="#94a3b8">${max.toFixed(0)}</text></svg>`;
}

function card(label, value){
  return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`;
}

async function load(){
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  // Health
  try {
    const h = await getJSON('/tradein/health/services');
    const s = h.summary || {};
    document.getElementById('banner').innerHTML = s.all_ok
      ? `<div class="card" style="border-left:4px solid var(--ok)">All ${s.total} services healthy ✅</div>`
      : `<div class="card" style="border-left:4px solid var(--bad)">${s.healthy}/${s.total} healthy — degraded: ${(s.degraded||[]).join(', ')}</div>`;
    const hd = Object.entries(h.services||{}).map(([k,v])=>`
      <div class="card svc">
        <span class="dot ${v.ok?'ok':'bad'}"></span>
        <div><div class="name">${k}</div><div class="detail">${v.detail||''}</div></div>
        <span class="lat">${v.latency_ms!=null? v.latency_ms+'ms':''}</span>
      </div>`).join('');
    document.getElementById('health').innerHTML = hd;
  } catch(e){ document.getElementById('health').innerHTML = `<div class="err">Health load failed: ${e.message}</div>`; }

  // Metrics
  try {
    const m = await getJSON('/tradein/dashboard/metrics');
    document.getElementById('kpis').innerHTML =
      card('Total evaluations', fmt(m.total_evaluations)) +
      card('Acceptance rate', m.acceptance_rate_pct + '%') +
      card('Avg confidence', m.avg_confidence + '%') +
      card('Confidence &lt; 80%', m.confidence_below_80_pct + '%');

    // Trend charts
    const ts = m.timeseries || [];
    document.getElementById('chartVolume').innerHTML = barChart(ts, 'count', '#14532d');
    document.getElementById('chartConf').innerHTML = lineChart(ts, 'avg_confidence', '#2563eb');

    const a = m.ai_vs_internal || {};
    document.getElementById('accuracy').innerHTML = (a.compared
      ? card('Compared vs experts', fmt(a.compared)) +
        card('Mean abs error', a.mean_abs_error_pct + '%') +
        card('Median abs error', a.median_abs_error_pct + '%') +
        card('Within ±10% / ±20%', a.within_10pct_rate + '% / ' + a.within_20pct_rate + '%')
      : `<div class="card"><div class="label">AI vs in-house</div><div class="detail">No expert-priced evaluations yet in this window.</div></div>`);

    // Calibration back-test (vs real sold prices)
    try {
      const cal = await getJSON('/tradein/dashboard/calibration');
      const rows = (cal.by_category||[]);
      document.getElementById('calibwrap').innerHTML = rows.length
        ? `<div class="detail" style="margin-bottom:8px;color:var(--muted)">Engine offer vs actual acquisition cost on ${fmt(cal.compared)} historical units (lower error = more accurate).</div>
           <table><tr><th>Category</th><th>Units</th><th>Mean abs error</th><th>Median</th><th>Within ±20%</th></tr>` +
          rows.map(r=>`<tr><td>${r.category}</td><td>${fmt(r.count)}</td><td>${r.mean_abs_error_pct}%</td><td>${r.median_abs_error_pct}%</td><td>${r.within_20pct_rate}%</td></tr>`).join('') + `</table>`
        : `<div class="card detail">No calibration data computed yet.</div>`;
    } catch(e){ document.getElementById('calibwrap').innerHTML = `<div class="card detail">Calibration not available: ${e.message}</div>`; }

    const cats = (m.by_category||[]);
    document.getElementById('catwrap').innerHTML = cats.length
      ? `<table><tr><th>Category</th><th>Count</th><th>Avg offer</th><th>Acceptance</th></tr>` +
        cats.map(c=>`<tr><td>${c.category}</td><td>${fmt(c.count)}</td><td>${fmt(c.avg_offer)}</td><td>${c.acceptance_rate_pct}%</td></tr>`).join('') + `</table>`
      : `<div class="card detail">No evaluations in window.</div>`;

    const rec = (m.recent||[]);
    document.getElementById('recentwrap').innerHTML = rec.length
      ? `<table><tr><th>When</th><th>Item</th><th>Size</th><th>AI offer</th><th>Asking</th><th>Conf</th><th>Decision</th></tr>` +
        rec.map(r=>{
          const cls = r.confidence==null?'mid':(r.confidence>=80?'ok':'bad');
          return `<tr><td>${(r.when||'').replace('T',' ').slice(0,16)}</td><td>${r.item||''}</td><td>${r.size||''}</td>
            <td>${r.currency} ${fmt(r.ai_offer)}</td><td>${r.asking?fmt(r.asking):'—'}</td>
            <td><span class="pill ${cls}">${r.confidence==null?'—':r.confidence+'%'}</span></td><td>${r.decision||''}</td></tr>`;
        }).join('') + `</table>`
      : `<div class="card detail">No recent evaluations.</div>`;
  } catch(e){
    if(e.forbidden){ showGate(true); throw e; }
    document.getElementById('kpis').innerHTML = `<div class="err">Metrics load failed: ${e.message}</div>`;
  }
}

function showGate(err){
  document.getElementById('gate').style.display = 'block';
  document.getElementById('wrap').style.display = 'none';
  document.getElementById('keyErr').style.display = err ? 'block' : 'none';
}
function showDash(){
  document.getElementById('gate').style.display = 'none';
  document.getElementById('wrap').style.display = 'block';
}

document.getElementById('keyBtn').onclick = () => {
  KEY = document.getElementById('keyInput').value.trim();
  sessionStorage.setItem('gb_key', KEY);
  start();
};
document.getElementById('keyInput').addEventListener('keydown', e => { if(e.key==='Enter') document.getElementById('keyBtn').click(); });

async function start(){
  if(!KEY){ showGate(false); return; }
  try {
    // Validate the key with one gated call before showing the dashboard.
    await getJSON('/tradein/dashboard/metrics');
    sessionStorage.setItem('gb_key', KEY);
    showDash();
    load();
    if(!window._gbTimer) window._gbTimer = setInterval(load, 60000);
  } catch(e){
    if(e.forbidden){ showGate(true); } else { showDash(); load(); }
  }
}
start();
</script>
</body>
</html>"""
