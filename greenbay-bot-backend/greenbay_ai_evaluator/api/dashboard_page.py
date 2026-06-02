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
<div class="wrap">
  <div id="banner"></div>

  <h2>Service Health</h2>
  <div id="health" class="grid"></div>

  <h2>Pricing Performance (last 30 days)</h2>
  <div id="kpis" class="grid"></div>

  <h2>AI vs In-House Evaluator</h2>
  <div id="accuracy" class="grid"></div>

  <h2>By Category</h2>
  <div id="catwrap"></div>

  <h2>Recent Evaluations</h2>
  <div id="recentwrap"></div>
</div>

<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
const fmt = n => (n==null? '—' : Number(n).toLocaleString());

async function getJSON(path){
  const r = await fetch(path + (path.includes('?')?'&':'?') + 'key=' + encodeURIComponent(KEY));
  if(!r.ok) throw new Error(path + ' -> ' + r.status);
  return r.json();
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

    const a = m.ai_vs_internal || {};
    document.getElementById('accuracy').innerHTML = (a.compared
      ? card('Compared vs experts', fmt(a.compared)) +
        card('Mean abs error', a.mean_abs_error_pct + '%') +
        card('Median abs error', a.median_abs_error_pct + '%') +
        card('Within ±10% / ±20%', a.within_10pct_rate + '% / ' + a.within_20pct_rate + '%')
      : `<div class="card"><div class="label">AI vs in-house</div><div class="detail">No expert-priced evaluations yet in this window.</div></div>`);

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
  } catch(e){ document.getElementById('kpis').innerHTML = `<div class="err">Metrics load failed: ${e.message}</div>`; }
}
load();
setInterval(load, 60000);
</script>
</body>
</html>"""
