/**
 * GreenBay ops: partner swaps (Mika, Vision Plus, ...).
 *
 * Talks to the admin-keyed /tradein/partners/admin/* routes. The key is asked
 * for once, kept in sessionStorage for this tab only, and sent in the
 * X-Admin-Key header: never in a URL. Every action needs the operator's name
 * ("You are"), which the ledger writes into the credit's audit trail.
 */
(function () {
    'use strict';

    const KEY = 'gb_ops_admin_key';
    const ACTOR = 'gb_ops_actor';
    const $ = (s, el) => (el || document).querySelector(s);
    const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
    const store = {
        get(k, s) { try { return (s ? sessionStorage : localStorage).getItem(k) || ''; } catch (_) { return ''; } },
        set(k, v, s) { try { (s ? sessionStorage : localStorage).setItem(k, v); } catch (_) { /* private mode */ } },
        del(k, s) { try { (s ? sessionStorage : localStorage).removeItem(k); } catch (_) { /* ignore */ } },
    };
    const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const money = (n, cur) => (n == null ? '' : `${cur || 'KES'} ${Math.round(Number(n) || 0).toLocaleString('en-KE')}`);
    const when = (iso) => { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); };
    /** 254712345678 -> '0712 345 678' for reading out; the tel: link keeps the full number. */
    const phone = (p) => { const d = String(p || '').replace(/\D/g, ''); return d.length === 12 && d.startsWith('254') ? `0${d.slice(3, 6)} ${d.slice(6, 9)} ${d.slice(9)}` : d; };
    const STATUS = {
        offered: 'Offered', accepted: 'Accepted', pending_validation: 'Awaiting inspection', validated: 'Inspected',
        issued: 'Credit live', partially_redeemed: 'Partly used', redeemed: 'Fully used', expired: 'Expired', cancelled: 'Cancelled',
    };
    const METHOD = { technician: 'Technician visit', drop_greenbay: 'Drop-off at a GreenBay shop', drop_partner: 'Drop-off at a partner shop' };
    const CATS = { refrigerator: 'Fridge or freezer', washing_machine: 'Washing machine', tv_monitor: 'TV', cooker_oven: 'Cooker or oven', microwave: 'Microwave', small_kitchen: 'Small kitchen appliance' };
    let partners = [];
    let days = 30;

    /* ---------------- plumbing ---------------- */
    function toast(msg, bad) {
        const t = $('#oToast');
        t.textContent = msg;
        t.classList.toggle('bad', !!bad);
        t.hidden = false;
        clearTimeout(toast._t);
        toast._t = setTimeout(() => { t.hidden = true; }, 4200);
    }

    function actor() {
        const v = $('#oActor').value.trim();
        if (v.length < 2) {
            toast('Add your name at the top first. Every action is recorded with it.', true);
            $('#oActor').focus();
            return null;
        }
        return v;
    }

    async function api(path, opts) {
        const key = store.get(KEY, true);
        if (!key) { gate(); throw new Error('locked'); }
        const o = Object.assign({ headers: {} }, opts || {});
        o.headers = Object.assign({ 'X-Admin-Key': key }, o.headers);
        if (o.json !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(o.json); o.method = o.method || 'POST'; }
        const r = await fetch('/tradein' + path, o);
        if (r.status === 403) { store.del(KEY, true); gate('That key was not accepted.'); throw new Error('forbidden'); }
        if (r.status === 503) { gate('The admin key is not set on the server yet.'); throw new Error('unavailable'); }
        if (!r.ok) {
            let msg = `Something went wrong (${r.status}).`;
            try { const d = (await r.json()).detail; msg = (d && (d.message || d)) || msg; } catch (_) { /* keep */ }
            if (typeof msg !== 'string') msg = 'That was refused. Check the values and try again.';
            throw Object.assign(new Error(msg), { shown: true });
        }
        return o.raw ? r : r.json();
    }

    function fail(e) {
        if (e && ['locked', 'forbidden', 'unavailable'].includes(e.message)) return;
        toast((e && e.message) || 'Something went wrong.', true);
    }

    function gate(err) {
        $('#oGate').hidden = false;
        $$('.o-panel').forEach(p => { p.hidden = true; });
        $('.o-tabs').hidden = true;
        $('#oSignOut').hidden = true;
        const e = $('#oGateErr');
        e.hidden = !err;
        e.textContent = err || '';
        setTimeout(() => $('#oKey').focus(), 30);
    }

    function badge(status) { return `<span class="o-badge s-${esc(status)}">${esc(STATUS[status] || status)}</span>`; }
    function pname(slug) { const p = partners.find(x => x.slug === slug); return p ? p.name : slug; }
    function item(c) { return [c.brand, c.model, CATS[c.category] || c.category].filter(Boolean).join(' '); }

    /* ---------------- tabs ---------------- */
    function show(tab) {
        $$('.o-tabs [role=tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
        $$('.o-panel').forEach(p => { p.hidden = p.dataset.panel !== tab; });
        store.set('gb_ops_tab', tab, true);
        ({ overview, inspect, review, credits: loadCredits, redeem: () => {}, settle: () => {}, qr: loadQr }[tab] || overview)();
    }

    /* ---------------- overview ---------------- */
    async function overview() {
        const el = $('#pOverview');
        el.innerHTML = '<p class="o-lede">Loading…</p>';
        try {
            const d = await api(`/partners/admin/overview?days=${days}`);
            const steps = ['scan', 'start', 'complete', 'offer', 'accept', 'validated', 'issued', 'redeemed'];
            const label = { scan: 'Scans', start: 'Started', complete: 'Finished', offer: 'Offers', accept: 'Accepted', validated: 'Inspected', issued: 'Issued', redeemed: 'Used' };
            el.innerHTML = `
                <h2 class="o-h2">Overview</h2>
                <div class="o-period" role="group" aria-label="Period">
                    ${[7, 30, 90, 365].map(n => `<button class="o-btn ${n === days ? '' : 'o-btn--line'}" data-days="${n}">${n === 365 ? 'Year' : n + ' days'}</button>`).join('')}
                </div>
                <div class="o-partners">${d.partners.map(p => {
                    const max = Math.max(1, ...steps.map(s => p.funnel[s] || 0));
                    const c = p.credits;
                    return `<article class="o-card">
                        <h3>${esc(p.name)} <a href="${esc(p.path)}" target="_blank" rel="noopener">Open page</a></h3>
                        <div class="o-stats">
                            <div class="o-stat"><b>${c.awaiting_inspection}</b><span>Waiting on us</span></div>
                            <div class="o-stat"><b>${p.review_leads}</b><span>Need a price</span></div>
                            <div class="o-stat"><b>${esc(money(c.outstanding, p.currency))}</b><span>Credit customers hold</span></div>
                            <div class="o-stat"><b>${esc(money(c.redeemed_total, p.currency))}</b><span>Used so far</span></div>
                        </div>
                        <div class="o-funnel" aria-label="Funnel, last ${d.days} days">
                            ${steps.map(s => `<div class="o-funnel-row"><span>${label[s]}</span><i style="width:${Math.round(100 * (p.funnel[s] || 0) / max)}%"></i><em>${p.funnel[s] || 0}</em></div>`).join('')}
                        </div>
                    </article>`;
                }).join('')}</div>`;
            $$('[data-days]', el).forEach(b => b.addEventListener('click', () => { days = Number(b.dataset.days); overview(); }));
            counts();
        } catch (e) { fail(e); el.innerHTML = ''; }
    }

    async function counts() {
        try {
            const [a, r] = await Promise.all([
                api('/partners/admin/credits?status=accepted,pending_validation,validated&limit=500'),
                api('/partners/admin/review-leads?days=90'),
            ]);
            $('#cInspect').textContent = a.items.length || '';
            $('#cReview').textContent = r.items.length || '';
        } catch (_) { /* counts are a nicety */ }
    }

    /* ---------------- inspections ---------------- */
    async function inspect() {
        const el = $('#pInspect');
        el.innerHTML = '<p class="o-lede">Loading…</p>';
        try {
            const d = await api('/partners/admin/credits?status=accepted,pending_validation,validated&limit=500');
            const intro = `<h2 class="o-h2">Inspections</h2><p class="o-lede">Customers who accepted a swap. Book the check, record what the technician or shop found, then issue the credit. Fridges, washing machines and big TVs get a technician visit.</p>`;
            if (!d.items.length) { el.innerHTML = intro + '<div class="o-empty">Nothing waiting. New accepted swaps appear here.</div>'; counts(); return; }
            el.innerHTML = intro + `<div class="o-list">${d.items.map(c => `
                <article class="o-item" data-id="${esc(c.id)}">
                    <div>
                        <div class="o-item-title">${esc(item(c))} ${c.is_nominal ? '<span class="o-badge">Nominal</span>' : ''}</div>
                        <div class="o-item-sub"><span class="o-badge p">${esc(pname(c.partner))}</span> <span class="o-mono">${esc(c.reference)}</span> ${badge(c.status)}</div>
                        <div class="o-item-sub">${esc(c.customer_name || '')} · <a href="tel:+${esc(c.customer_phone || '')}">${esc(phone(c.customer_phone))}</a> · ${esc(c.source || '')}</div>
                    </div>
                    <div>
                        <div class="o-amount">${esc(money(c.agreed_amount != null ? c.agreed_amount : c.offered_amount, c.currency))}</div>
                        <div class="o-item-sub">${c.validation_method ? esc(METHOD[c.validation_method]) : 'Suggested: ' + esc(METHOD[c.suggested_method])}</div>
                    </div>
                    <div class="o-item-actions">${actionsFor(c)}</div>
                </article>`).join('')}</div>`;
            wireActions(el, inspect);
            counts();
        } catch (e) { fail(e); el.innerHTML = ''; }
    }

    function actionsFor(c) {
        if (c.status === 'accepted') {
            return `<select data-method aria-label="How it will be checked">${Object.entries(METHOD).map(([k, v]) => `<option value="${k}" ${k === c.suggested_method ? 'selected' : ''}>${v}</option>`).join('')}</select>
                <button class="o-btn" data-act="start">Book the check</button>
                <button class="o-btn o-btn--warn" data-act="cancel">Customer pulled out</button>`;
        }
        if (c.status === 'pending_validation') {
            return `<button class="o-btn" data-act="pass">It passed</button>
                <button class="o-btn o-btn--line" data-act="lower">Passed at a lower value</button>
                <button class="o-btn o-btn--warn" data-act="fail">It failed</button>`;
        }
        if (c.status === 'validated') return `<button class="o-btn" data-act="issue">Issue credit</button>`;
        return `<button class="o-btn o-btn--line" data-act="open">Details</button>`;
    }

    function wireActions(root, reload) {
        $$('[data-act]', root).forEach(btn => btn.addEventListener('click', async () => {
            const card = btn.closest('[data-id]');
            const id = card.dataset.id;
            const act = btn.dataset.act;
            if (act === 'open') return openCredit(id);
            const who = actor();
            if (!who) return;
            let path = '', json = { actor: who };
            if (act === 'start') { path = 'start-validation'; json.method = $('[data-method]', card).value; }
            if (act === 'pass') { path = 'validate'; }
            if (act === 'lower') {
                const v = prompt('Value agreed with the customer after the check (KES). It cannot be more than the accepted offer.');
                if (!v) return;
                path = 'validate'; json.agreed_amount = Number(String(v).replace(/[^0-9.]/g, ''));
                json.note = prompt('What did the check find? (optional)') || null;
            }
            if (act === 'fail') {
                const note = prompt('What did the check find? This ends the swap.');
                if (note === null) return;
                path = 'fail-inspection'; json.note = note || null;
            }
            if (act === 'cancel') {
                if (!confirm('Cancel this swap? The customer gets no credit.')) return;
                path = 'cancel'; json.note = prompt('Why? (optional)') || null;
            }
            if (act === 'issue') path = 'issue';
            btn.disabled = true;
            try {
                const c = await api(`/partners/admin/credits/${encodeURIComponent(id)}/${path}`, { json });
                if (act === 'issue') {
                    toast(`Credit issued: ${c.voucher_code}. Call the customer with this code.`);
                    openCredit(c.id);
                } else {
                    toast(`${c.reference}: ${STATUS[c.status] || c.status}.`);
                }
                reload();
            } catch (e) { fail(e); btn.disabled = false; }
        }));
    }

    /* ---------------- needs a price ---------------- */
    async function review() {
        const el = $('#pReview');
        el.innerHTML = '<p class="o-lede">Loading…</p>';
        try {
            const d = await api('/partners/admin/review-leads?days=90');
            const intro = `<h2 class="o-h2">Needs a price</h2><p class="o-lede">Partner swaps the engine passed to a person: the evidence was thin or something needs a closer look. Call the customer, agree a figure, and open the credit here. It starts as accepted.</p>`;
            if (!d.items.length) { el.innerHTML = intro + '<div class="o-empty">Nothing waiting for a price.</div>'; return; }
            el.innerHTML = intro + `<div class="o-list">${d.items.map(v => `
                <article class="o-item" data-session="${esc(v.session_id)}">
                    <div>
                        <div class="o-item-title">${esc(item(v))}, ${esc(v.age_years)} years, grade ${esc(v.condition_grade || '?')}</div>
                        <div class="o-item-sub"><span class="o-badge p">${esc(pname(v.partner))}</span> <span class="o-mono">${esc(v.session_ref)}</span> · ${esc(when(v.created_at))}</div>
                        <div class="o-item-sub">${esc(v.customer_name || '')} · <a href="tel:+${esc(v.customer_phone || '')}">${esc(phone(v.customer_phone))}</a></div>
                    </div>
                    <div class="o-item-sub">Engine figure ${esc(money(v.engine_offer))}<br>confidence ${Math.round(v.confidence || 0)}, risk ${Math.round(v.risk || 0)}</div>
                    <div class="o-item-actions o-inline">
                        <label class="sr" for="amt-${esc(v.session_ref)}">Agreed credit (KES)</label>
                        <input id="amt-${esc(v.session_ref)}" type="number" min="1" step="50" placeholder="Agreed KES" data-amount>
                        <button class="o-btn" data-offer>Open credit</button>
                    </div>
                </article>`).join('')}</div>`;
            $$('[data-offer]', el).forEach(btn => btn.addEventListener('click', async () => {
                const card = btn.closest('[data-session]');
                const amount = Number($('[data-amount]', card).value);
                const who = actor();
                if (!who) return;
                if (!(amount > 0)) { toast('Type the agreed amount first.', true); return; }
                btn.disabled = true;
                try {
                    const c = await api(`/partners/admin/review-leads/${encodeURIComponent(card.dataset.session)}/offer`, { json: { actor: who, amount } });
                    toast(`${c.reference} opened at ${money(c.offered_amount, c.currency)}. It is now in Inspections.`);
                    review(); counts();
                } catch (e) { fail(e); btn.disabled = false; }
            }));
        } catch (e) { fail(e); el.innerHTML = ''; }
    }

    /* ---------------- credits ---------------- */
    async function loadCredits() {
        const el = $('#oCreditList');
        const p = $('#fPartner').value, s = $('#fStatus').value, q = $('#fQuery').value.trim();
        el.innerHTML = '<p class="o-lede">Loading…</p>';
        try {
            const qs = new URLSearchParams({ limit: '300' });
            if (p) qs.set('partner', p);
            if (s) qs.set('status', s);
            // The search (it may be a phone number) goes in a header, never the URL.
            const d = await api(`/partners/admin/credits?${qs}`, q ? { headers: { 'X-Lookup-Query': q } } : undefined);
            if (!d.items.length) { el.innerHTML = '<div class="o-empty">No credits match.</div>'; return; }
            el.innerHTML = `<table class="o-table"><thead><tr><th>Reference</th><th>Partner</th><th>Item</th><th>Status</th><th>Balance</th><th>Expires</th></tr></thead><tbody>
                ${d.items.map(c => `<tr data-id="${esc(c.id)}" tabindex="0" style="cursor:pointer">
                    <td class="o-mono">${esc(c.voucher_code || c.reference)}</td><td>${esc(pname(c.partner))}</td><td>${esc(item(c))}</td>
                    <td>${badge(c.status)}</td><td class="o-amount">${c.issued_amount != null ? esc(money(c.balance, c.currency)) : esc(money(c.offered_amount, c.currency))}</td>
                    <td>${esc(when(c.expires_at))}</td></tr>`).join('')}</tbody></table>`;
            $$('tr[data-id]', el).forEach(tr => {
                tr.addEventListener('click', () => openCredit(tr.dataset.id));
                tr.addEventListener('keydown', e => { if (e.key === 'Enter') openCredit(tr.dataset.id); });
            });
        } catch (e) { fail(e); el.innerHTML = ''; }
    }

    async function openCredit(id) {
        const body = $('#dBody');
        $('#oDrawer').hidden = false;
        body.innerHTML = '<p>Loading…</p>';
        try {
            const c = await api(`/partners/admin/credits/${encodeURIComponent(id)}`);
            body.innerHTML = `
                <h3 id="dTitle">${esc(item(c))}</h3>
                <p>${badge(c.status)} <span class="o-badge p">${esc(pname(c.partner))}</span></p>
                ${c.voucher_code ? `<div class="o-code" aria-label="Voucher code">${esc(c.voucher_code)}</div>` : ''}
                <dl class="o-kv">
                    <dt>Reference</dt><dd class="o-mono">${esc(c.reference)}</dd>
                    <dt>Customer</dt><dd>${esc(c.customer_name || '')}, <a href="tel:+${esc(c.customer_phone || '')}">${esc(phone(c.customer_phone))}</a></dd>
                    <dt>Accepted offer</dt><dd>${esc(money(c.offered_amount, c.currency))}</dd>
                    ${c.agreed_amount != null ? `<dt>After inspection</dt><dd>${esc(money(c.agreed_amount, c.currency))}</dd>` : ''}
                    ${c.issued_amount != null ? `<dt>Balance</dt><dd class="o-amount">${esc(money(c.balance, c.currency))} of ${esc(money(c.issued_amount, c.currency))}</dd>` : ''}
                    ${c.expires_at ? `<dt>Expires</dt><dd>${esc(when(c.expires_at))}</dd>` : ''}
                    <dt>Came from</dt><dd>${esc(c.source || '')}${c.qr_code ? ` (<span class="o-mono">${esc(c.qr_code)}</span>)` : ''}</dd>
                    <dt>Wants to buy</dt><dd>${esc([c.buy_intent.category, c.buy_intent.model].filter(Boolean).join(', ') || 'Not said')}</dd>
                    <dt>Inspection</dt><dd>${esc(METHOD[c.validation_method] || 'Not booked')}</dd>
                    <dt>Evaluation</dt><dd class="o-mono">${esc(c.session_ref)}</dd>
                </dl>
                <h4>Audit trail</h4>
                <ol class="o-trail">${(c.history || []).map(h => `<li><b>${esc((h.event || '').replace(/_/g, ' '))}</b>${h.amount != null ? ' · ' + esc(money(h.amount, c.currency)) : ''}
                    <small>${esc(h.actor)} · ${esc(when(h.at))} ${esc(new Date(h.at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }))}${h.note ? ' · ' + esc(h.note) : ''}</small></li>`).join('')}</ol>
                <div class="o-item-actions" style="margin-top:18px" data-id="${esc(c.id)}">${['accepted', 'pending_validation', 'validated'].includes(c.status) ? actionsFor(c) : ''}
                    ${['issued'].includes(c.status) ? '<button class="o-btn o-btn--warn" data-act="cancel">Cancel credit</button>' : ''}</div>`;
            wireActions(body, () => { openCredit(id); loadCredits(); });
            $('#dClose').focus();
        } catch (e) { fail(e); $('#oDrawer').hidden = true; }
    }

    /* ---------------- redeem ---------------- */
    async function lookupCode() {
        const code = $('#rCode').value.trim().toUpperCase();
        const out = $('#rLookup');
        if (code.length < 6) { out.textContent = ''; return; }
        try {
            const d = await api(`/partners/admin/credits?partner=${encodeURIComponent($('#rPartner').value)}&limit=5`,
                { headers: { 'X-Lookup-Query': code } });
            const c = d.items.find(x => x.voucher_code === code);
            out.innerHTML = c
                ? `${esc(c.customer_name || '')}: ${badge(c.status)} balance <b>${esc(money(c.balance, c.currency))}</b>, expires ${esc(when(c.expires_at))}`
                : 'No credit with that code for this partner.';
        } catch (e) { fail(e); }
    }

    async function redeem(ev) {
        ev.preventDefault();
        const who = actor();
        if (!who) return;
        const json = {
            actor: who, partner: $('#rPartner').value, voucher_code: $('#rCode').value.trim().toUpperCase(),
            amount: Number($('#rAmount').value), note: $('#rNote').value.trim() || null,
        };
        try {
            const c = await api('/partners/admin/redeem', { json });
            $('#oRedeemResult').innerHTML = `<div class="o-card" style="margin-top:16px"><b>Recorded.</b> ${esc(c.voucher_code)} has ${esc(money(c.balance, c.currency))} left. ${badge(c.status)}</div>`;
            toast('Redemption recorded.');
            $('#rAmount').value = ''; $('#rNote').value = '';
            lookupCode();
        } catch (e) { fail(e); }
    }

    /* ---------------- settlement ---------------- */
    async function settle(ev) {
        if (ev) ev.preventDefault();
        const el = $('#oSettle');
        const partner = $('#sPartner').value, month = $('#sMonth').value;
        if (!partner || !month) return;
        el.innerHTML = '<p class="o-lede">Loading…</p>';
        try {
            const d = await api(`/partners/admin/settlement?partner=${encodeURIComponent(partner)}&month=${encodeURIComponent(month)}`);
            el.innerHTML = `
                <div class="o-sum">
                    <div class="o-stat due"><b>${esc(money(d.amount_due, d.currency))}</b><span>Due to ${esc(pname(partner))} (credit used)</span></div>
                    <div class="o-stat"><b>${esc(money(d.issued, d.currency))}</b><span>Credit issued</span></div>
                    <div class="o-stat"><b>${esc(money(d.lapsed, d.currency))}</b><span>Lapsed unused</span></div>
                    <div class="o-stat"><b>${esc(money(d.outstanding_at_end, d.currency))}</b><span>Held by customers at month end</span></div>
                </div>
                ${d.redemptions.length ? `<table class="o-table"><thead><tr><th>Date</th><th>Voucher</th><th>Amount</th><th>Partner receipt</th><th>Recorded by</th></tr></thead><tbody>
                    ${d.redemptions.map(r => `<tr><td>${esc(when(r.date))}</td><td class="o-mono">${esc(r.voucher_code)}</td><td class="o-amount">${esc(money(r.amount, d.currency))}</td><td>${esc(r.note)}</td><td>${esc(r.recorded_by)}</td></tr>`).join('')}
                </tbody></table>` : '<div class="o-empty">No credit was used this month.</div>'}`;
        } catch (e) { fail(e); el.innerHTML = ''; }
    }

    async function settleCsv() {
        const partner = $('#sPartner').value, month = $('#sMonth').value;
        if (!partner || !month) return;
        try {
            const r = await api(`/partners/admin/settlement?partner=${encodeURIComponent(partner)}&month=${encodeURIComponent(month)}&format=csv`, { raw: true });
            const url = URL.createObjectURL(await r.blob());
            const a = document.createElement('a');
            a.href = url; a.download = `settlement-${partner}-${month}.csv`;
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 2000);
        } catch (e) { fail(e); }
    }

    /* ---------------- QR codes ---------------- */
    const CHANNEL = { shop: 'Shop', website: 'Website', social: 'Social', print: 'Print', staff: 'Staff', event: 'Event', direct: 'Direct' };
    const bare = (url) => String(url || '').replace(/^https?:\/\//, '');

    async function loadQr() {
        const el = $('#oQrList');
        el.innerHTML = '<p class="o-lede">Loading…</p>';
        try {
            const d = await api(`/partners/admin/qr?partner=${encodeURIComponent($('#qPartner').value)}&days=90`);
            if (!d.items.length) { el.innerHTML = '<div class="o-empty">No codes yet.</div>'; return; }
            el.innerHTML = `<table class="o-table"><thead><tr><th></th><th>Code</th><th>Where</th><th>Scans</th><th>Finished</th><th>Accepted</th><th>Credit used</th><th>Link</th><th></th></tr></thead><tbody>
                ${d.items.map(q => `<tr data-code="${esc(q.code)}">
                    <td><input type="checkbox" aria-label="Include ${esc(q.code)} on the sheet" ${q.active ? '' : 'disabled'}></td>
                    <td class="o-mono">${esc(q.code)}${q.active ? '' : ' <span class="o-badge s-cancelled">Off</span>'}</td>
                    <td>${esc(q.label || '')}<div class="o-item-sub">${esc(CHANNEL[q.channel] || q.channel)}${q.shop ? ' · ' + esc(q.shop) : ''} · ${esc(q.placement || '')}</div></td>
                    <td class="o-amount">${q.funnel.scan}</td><td>${q.funnel.complete}</td><td>${q.funnel.accept}</td>
                    <td>${esc(money(q.redeemed_amount))}</td>
                    <td><a href="${esc(q.url)}" target="_blank" rel="noopener" class="o-mono">${esc(bare(q.url))}</a></td>
                    <td class="o-inline">${q.active ? '<button class="o-btn o-btn--line" data-qsvg>QR SVG</button>' : ''}
                        ${q.active && q.source === 'admin' ? '<button class="o-btn o-btn--warn" data-qoff>Turn off</button>' : ''}</td></tr>`).join('')}
            </tbody></table>`;
            $$('[data-qsvg]', el).forEach(b => b.addEventListener('click', () => {
                const code = b.closest('tr').dataset.code;
                download(`/partners/admin/qr/${encodeURIComponent(code)}/svg`, `qr-${code}.svg`);
            }));
            $$('[data-qoff]', el).forEach(b => b.addEventListener('click', async () => {
                const who = actor();
                if (!who) return;
                const code = b.closest('tr').dataset.code;
                if (!confirm(`Turn off ${code}? Scans will land on the main evaluator page instead.`)) return;
                try {
                    await api(`/partners/admin/qr/${encodeURIComponent(code)}/deactivate`, { json: { actor: who } });
                    toast(`${code} turned off.`);
                    loadQr();
                } catch (e) { fail(e); }
            }));
            $$('input[type=checkbox]', el).forEach(cb => cb.addEventListener('change', () => {
                const n = $$('input[type=checkbox]:checked', el).length;
                $('#qPicked').textContent = n ? `${n} ticked.` : 'Tick codes below, or leave all unticked for every active code.';
            }));
        } catch (e) { fail(e); el.innerHTML = ''; }
    }

    async function download(path, name) {
        try {
            const r = await api(path, { raw: true });
            const url = URL.createObjectURL(await r.blob());
            const link = document.createElement('a');
            link.href = url;
            link.download = name;
            document.body.appendChild(link);
            link.click();
            link.parentNode.removeChild(link);
            setTimeout(() => URL.revokeObjectURL(url), 2000);
        } catch (e) { fail(e); }
    }

    function sheet(format) {
        const partner = $('#qPartner').value;
        const codes = $$('#oQrList tr[data-code]').filter(tr => $('input:checked', tr)).map(tr => tr.dataset.code);
        const pick = codes.length ? '&codes=' + encodeURIComponent(codes.join(',')) : '';
        download(`/partners/admin/qr-sheet?partner=${encodeURIComponent(partner)}&format=${format}${pick}`, `stickers-${partner}.${format}`);
    }

    async function newQr(ev) {
        ev.preventDefault();
        const who = actor();
        if (!who) return;
        const json = { actor: who, partner: $('#qPartner').value, channel: $('#qChannel').value,
            shop: $('#qShop').value.trim() || null, placement: $('#qPlacement').value.trim() };
        try {
            const q = await api('/partners/admin/qr', { json });
            toast(`${q.code} made: ${bare(q.url)}`);
            $('#qShop').value = '';
            $('#qPlacement').value = '';
            loadQr();
        } catch (e) { fail(e); }
    }

    /* ---------------- start ---------------- */
    async function start() {
        $('#oGate').hidden = true;
        $('.o-tabs').hidden = false;
        $('#oSignOut').hidden = false;
        try {
            partners = (await api('/partners/admin/partners')).items;
        } catch (e) { fail(e); return; }
        const opts = partners.map(p => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('');
        $('#fPartner').innerHTML = '<option value="">Every partner</option>' + opts;
        $('#rPartner').innerHTML = opts;
        $('#sPartner').innerHTML = opts;
        $('#qPartner').innerHTML = opts;
        const now = new Date();
        $('#sMonth').value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
        show(store.get('gb_ops_tab', true) || 'overview');
    }

    document.addEventListener('DOMContentLoaded', () => {
        $('#oActor').value = store.get(ACTOR);
        $('#oActor').addEventListener('input', () => store.set(ACTOR, $('#oActor').value.trim()));
        $('#oGateForm').addEventListener('submit', (e) => {
            e.preventDefault();
            store.set(KEY, $('#oKey').value.trim(), true);
            $('#oKey').value = '';
            start();
        });
        $('#oSignOut').addEventListener('click', () => { store.del(KEY, true); gate(); });
        $$('.o-tabs [role=tab]').forEach(b => b.addEventListener('click', () => show(b.dataset.tab)));
        $('#oCreditFilter').addEventListener('submit', (e) => { e.preventDefault(); loadCredits(); });
        $('#rCode').addEventListener('change', lookupCode);
        $('#rPartner').addEventListener('change', lookupCode);
        $('#oRedeemForm').addEventListener('submit', redeem);
        $('#oSettleForm').addEventListener('submit', settle);
        $('#sCsv').addEventListener('click', settleCsv);
        $('#qPartner').addEventListener('change', loadQr);
        $('#oQrNew').addEventListener('submit', newQr);
        $('#qPdf').addEventListener('click', () => sheet('pdf'));
        $('#qSvg').addEventListener('click', () => sheet('svg'));
        $('#dClose').addEventListener('click', () => { $('#oDrawer').hidden = true; });
        $('#oDrawer').addEventListener('click', (e) => { if (e.target.id === 'oDrawer') $('#oDrawer').hidden = true; });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#oDrawer').hidden = true; });
        if (store.get(KEY, true)) start(); else gate();
    });
})();
