/**
 * Partner pages (GreenBay x Mika, GreenBay x Vision Plus): everything on top
 * of the shared wizard in app.js. Loaded after app.js on /mika, /vision-plus.
 *
 *  - Motion: reveal on scroll, a count-up on the example credit, a hairline
 *    that draws itself under the numbered steps. None of it runs when the
 *    visitor prefers reduced motion, and the page reads fine without it.
 *  - Wizard: only the partner's categories, a note that the brand is fixed,
 *    a "what would you like from <partner>?" block in step 3.
 *  - Results: the credit offer, the review card and the "you're in" card,
 *    rendered here through GBPartnerUI.render(kind, data, extra), which
 *    app.js calls (see partnerRender in app.js).
 *
 * All text comes from the partner config (GB_PARTNER.config.copy) and every
 * value is escaped before it touches innerHTML.
 */
(function () {
    'use strict';
    if (typeof GB_PARTNER === 'undefined' || !GB_PARTNER) return;

    const cfg = GB_PARTNER.config || {};
    const copy = cfg.copy || {};
    const theme = cfg.theme || {};
    const esc = (v) => (typeof escapeHtml === 'function' ? escapeHtml(String(v == null ? '' : v))
        : String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
    const fill = (text, vars) => String(text || '').replace(/\{(\w+)\}/g, (_, k) => (vars && vars[k] != null ? String(vars[k]) : ''));
    const money = (n) => (typeof formatKES === 'function' ? formatKES(n) : Math.round(Number(n) || 0).toLocaleString('en-KE'));
    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    /* ---------------- motion ---------------- */
    function countUp(el) {
        const target = Number(el.dataset.countup) || 0;
        // A background tab gets no animation frames: show the figure, do not animate.
        if (reduceMotion || document.hidden || !target) { el.textContent = money(target); return; }
        const start = performance.now();
        const dur = 1400;
        const step = (t) => {
            const p = Math.min(1, (t - start) / dur);
            const eased = 1 - Math.pow(1 - p, 3);
            el.textContent = money(Math.round(target * eased / 50) * 50);
            if (p < 1) requestAnimationFrame(step); else el.textContent = money(target);
        };
        requestAnimationFrame(step);
    }

    function initMotion() {
        const countups = document.querySelectorAll('[data-countup]');
        const track = document.querySelector('.p-steps-track');
        if (reduceMotion || !('IntersectionObserver' in window)) {
            countups.forEach(el => { el.textContent = money(el.dataset.countup); });
            if (track) track.classList.add('is-drawn');
            return;
        }
        document.documentElement.classList.add('p-motion');
        const io = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;
                const el = entry.target;
                el.classList.add('is-in');
                if (el.matches('.p-steps-track')) el.classList.add('is-drawn');
                el.querySelectorAll('[data-countup]').forEach(countUp);
                io.unobserve(el);
            });
        }, { threshold: 0.18, rootMargin: '0px 0px -6% 0px' });
        document.querySelectorAll('[data-reveal], .p-steps-track').forEach(el => io.observe(el));
    }

    /* ---------------- wizard ---------------- */
    function limitCategories() {
        const allowed = cfg.categories || [];
        const sel = document.getElementById('categorySelect');
        if (!sel || !allowed.length) return;
        Array.from(sel.options).forEach(opt => {
            if (opt.value && allowed.indexOf(opt.value) === -1) opt.remove();
        });
    }

    function brandNote() {
        const step2 = document.getElementById('step2');
        if (!step2 || step2.querySelector('.p-brand-lock')) return;
        const note = document.createElement('div');
        note.className = 'p-brand-lock';
        note.innerHTML = `<i data-lucide="badge-check" aria-hidden="true" style="width:18px;height:18px;flex:none;margin-top:2px"></i>
            <span>This swap is for <strong>${esc(cfg.name)}</strong> appliances.</span>`;
        const anchor = step2.querySelector('.plate-block, .form-group, .photo-upload-zone');
        if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(note, anchor);
        else step2.prepend(note);
        // The brand is fixed on a partner site: no brand picker.
        const fallback = document.getElementById('brandFallback');
        if (fallback) fallback.remove();
        const noLabel = document.getElementById('noLabelBtn');
        if (noLabel) noLabel.remove();
    }

    function buyIntentBlock() {
        const step3 = document.getElementById('step3');
        const options = cfg.buy_categories || [];
        if (!step3 || !options.length || step3.querySelector('.p-buy')) return;
        const a = state.answers;

        // Step 3 leads with what the customer wants from the partner; the
        // price for the old item stays, marked optional, underneath.
        const h3 = step3.querySelector('h3');
        const desc = step3.querySelector('.step-description');
        if (h3 && copy.buy_title) h3.textContent = copy.buy_title;
        if (desc) desc.textContent = copy.buy_hint || '';

        const block = document.createElement('div');
        block.className = 'form-group p-buy';
        block.innerHTML = `
            <div class="chip-group" role="radiogroup" aria-label="${esc(copy.buy_title || 'What would you like to buy?')}">
                ${options.map(o => `<button type="button" class="chip" role="radio" data-buy="${esc(o.id)}"
                    aria-checked="false">${esc(o.label)}</button>`).join('')}
            </div>
            <label class="sr-only" for="pBuyModel">${esc(copy.buy_model_placeholder || 'Model or budget')}</label>
            <input type="text" class="form-input" id="pBuyModel" maxlength="200"
                placeholder="${esc(copy.buy_model_placeholder || 'Model or budget (optional)')}">`;
        if (desc) desc.insertAdjacentElement('afterend', block);
        else step3.prepend(block);
        const model = block.querySelector('#pBuyModel');
        model.value = a.buyModel || '';

        if (copy.price_hint) {
            const priceGroup = step3.querySelector('.price-input-wrapper');
            if (priceGroup && priceGroup.parentNode) {
                const lbl = document.createElement('label');
                lbl.className = 'form-label';
                lbl.htmlFor = 'priceInput';
                lbl.textContent = copy.price_hint;
                priceGroup.parentNode.insertBefore(lbl, priceGroup);
            }
        }

        const paint = () => block.querySelectorAll('[data-buy]').forEach(b => {
            const on = b.dataset.buy === state.answers.buyCategory;
            b.classList.toggle('selected', on);
            b.setAttribute('aria-checked', on ? 'true' : 'false');
        });
        block.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-buy]');
            if (!btn) return;
            state.answers.buyCategory = state.answers.buyCategory === btn.dataset.buy ? null : btn.dataset.buy;
            paint();
            saveState();
        });
        model.addEventListener('input', () => {
            state.answers.buyModel = sanitiseText(model.value).slice(0, 200) || null;
            saveState();
        });
        paint();
    }

    /* Partner wording for steps 1 and 2, where the config has it. */
    function relabelCopy() {
        const set = (sel, key) => {
            const el = document.querySelector(sel);
            if (el && copy[key]) el.textContent = copy[key];
        };
        set('#step1 h3', 'step1_title');
        set('#step1 .step-description', 'step1_hint');
        set('label[for="categorySelect"]', 'step1_category_label');
        set('#step2 .step-description', 'step2_hint');
    }

    function relabelSteps() {
        if (typeof STEP_LABELS === 'object' && copy.step3_label) STEP_LABELS[3] = copy.step3_label;
        if (typeof updateWizardUI === 'function') { try { updateWizardUI(); } catch (_) { /* ignore */ } }
    }

    /* ---------------- results ---------------- */
    const gbFoot = `<div class="p-offer-foot"><img src="/app/partners/greenbay/greenbay-badge.png" alt="" width="16" height="16">
        ${esc(copy.run_by || 'Valued and bought by GreenBay')}</div>`;

    function itemLine() {
        const a = state.answers;
        const cat = (typeof CATEGORY_NAMES === 'object' && CATEGORY_NAMES[a.category]) || '';
        return [cfg.name, a.model || '', cat].filter(Boolean).join(' ');
    }

    function offerCard(data, extra) {
        const amount = Number(extra && extra.offer) || Number(data.opening_offer) || 0;
        const cur = (extra && extra.currency) || data.currency_code || cfg.currency || 'KES';
        const label = fill(copy.accept_cta || 'Accept {amount}', { amount: `${cur} ${money(amount)}` });
        return `
        <div class="p-offer">
            <div class="p-offer-head">
                <img src="${esc(theme.logo_on_dark || theme.logo || '')}" alt="${esc(theme.logo_alt || cfg.name)}">
                <span>${esc(copy.offer_header || 'Your credit offer')}</span>
            </div>
            <div class="p-offer-body">
                <p class="p-offer-label">${esc(copy.card_label || 'Credit')}</p>
                <p class="p-offer-amount"><small>${esc(cur)}</small><span data-countup="${amount}">${money(amount)}</span></p>
                <p class="p-offer-item">${esc(itemLine())}</p>
                <p class="p-offer-note">${esc(copy.offer_note || '')}</p>
                <div class="p-offer-actions">
                    <button type="button" class="p-btn p-btn--ink" onclick="acceptOffer(${amount})">${esc(label)}</button>
                    <a class="p-btn p-btn--line" href="#" data-wa="${esc(`Hi GreenBay, I have a question about my ${cfg.name} swap offer (${cur} ${money(amount)}).`)}"
                       target="_blank" rel="noopener">${esc(copy.talk_cta || 'Talk to our team')}</a>
                </div>
            </div>
            ${gbFoot}
        </div>`;
    }

    function reviewCard() {
        return `
        <div class="p-offer">
            <div class="p-offer-head">
                <img src="${esc(theme.logo_on_dark || theme.logo || '')}" alt="${esc(theme.logo_alt || cfg.name)}">
                <span>Thanks, we have your details</span>
            </div>
            <div class="p-offer-body">
                <p class="p-offer-item">A person on our team will look at your ${esc(itemLine())} before we put a figure on it.
                We will call you on ${esc(state.answers.sellerPhone || 'the number you gave')}.</p>
                <p class="p-offer-note">${esc(copy.offer_note || '')}</p>
                <div class="p-offer-actions">
                    <a class="p-btn p-btn--ink" href="#" data-wa="${esc(`Hi GreenBay, I sent my ${cfg.name} for the swap and would like to talk it through.`)}"
                       target="_blank" rel="noopener">${esc(copy.talk_cta || 'Talk to our team')}</a>
                    <button type="button" class="p-btn p-btn--line" onclick="startNewEvaluation()">Value another appliance</button>
                </div>
            </div>
            ${gbFoot}
        </div>`;
    }

    function acceptedCard(extra) {
        const ref = (extra && extra.reference) || '';
        const phone = state.answers.sellerPhone || '';
        const steps = Array.isArray(copy.accepted_next) ? copy.accepted_next : [];
        return `
        <div class="p-offer p-done">
            <div class="p-offer-head">
                <img src="${esc(theme.logo_on_dark || theme.logo || '')}" alt="${esc(theme.logo_alt || cfg.name)}">
                <span>Swap accepted</span>
            </div>
            <div class="p-offer-body">
                <h3>${esc(copy.accepted_title || 'You are in.')}</h3>
                ${ref ? `<p class="p-offer-label">${esc(copy.accepted_ref_label || 'Your reference')}</p>
                <div class="p-done-ref">${esc(ref)}</div>` : ''}
                <p>${esc(fill(copy.accepted_body || '', { phone }))}</p>
                ${steps.length ? `<ol>${steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
                <div class="p-offer-actions">
                    <button type="button" class="p-btn p-btn--line" onclick="startNewEvaluation()">Value another appliance</button>
                </div>
            </div>
            ${gbFoot}
        </div>`;
    }

    function show(html) {
        const el = document.getElementById('stepResults');
        if (!el) return false;
        if (typeof setResultHeader === 'function') setResultHeader(copy.results_label || 'Your offer');
        el.innerHTML = html;
        if (typeof applyMarketCopy === 'function') { try { applyMarketCopy(); } catch (_) { /* ignore */ } }
        el.querySelectorAll('[data-countup]').forEach(countUp);
        if (window.lucide) lucide.createIcons();
        return true;
    }

    window.GBPartnerUI = {
        render(kind, data, extra) {
            if (kind === 'offer') {
                const ok = show(offerCard(data || {}, extra || {}));
                if (ok && typeof addChatMessage === 'function') {
                    addChatMessage('bot', `Here is your offer: ${esc((extra && extra.currency) || 'KES')} ${money(extra && extra.offer)} in ${esc(cfg.name)} credit. It is ready to spend once our team has checked the item.`);
                }
                return ok;
            }
            if (kind === 'review') {
                const ok = show(reviewCard());
                if (ok && typeof addChatMessage === 'function') {
                    addChatMessage('bot', 'I would like a person on our team to look at this one before we give you a figure. They will call you.');
                }
                return ok;
            }
            if (kind === 'accepted') {
                const ok = show(acceptedCard(extra || {}));
                if (ok && typeof addChatMessage === 'function' && extra && extra.reference) {
                    addChatMessage('bot', `Done. Your reference is <strong>${esc(extra.reference)}</strong>. Our team will call you to arrange the check.`);
                }
                return ok;
            }
            return false;
        },
    };

    function init() {
        limitCategories();
        brandNote();
        relabelCopy();
        buyIntentBlock();
        relabelSteps();
        initMotion();
        if (window.lucide) lucide.createIcons();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
