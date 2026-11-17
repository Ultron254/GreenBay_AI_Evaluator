/**
 * GreenBay AI Evaluator , Web Application
 * 
 * Wizard state machine, photo upload with validation,
 * chat panel mirroring, API integration, and negotiation UI.
 */

/* ============================================================
 STATE
 ============================================================ */
const API_BASE = window.location.origin;
const TOTAL_STEPS = 4;

/* ============================================================
 MARKETS
 Each country portal (/app/ke, /app/ug, /app/ng) serves this same page. The
 market decides currency, phone rules, copy, WhatsApp line and theme
 (html[data-country]). Kenya is also the default for /app/.

 Uganda and Nigeria are priced from a local online lookup and always go to a
 person for now, so their customers never see a confidence score
 (showConfidence: false); the team sees it in Airtable and the emails.
 ============================================================ */
const COUNTRY_CONFIG = {
    KE: {
        code: 'KE', name: 'Kenya', currency: 'KES', locale: 'en-KE',
        whatsapp: '254705919099',
        // Contact details as published on greenbay.market.
        phone: '+254709155506', email: 'info@greenbay.market',
        store: 'https://greenbay.market',
        greeting: 'Karibu',
        city: 'Nairobi',
        showConfidence: true,
        outlets: [
            { name: 'Kasarani Outlet', address: 'Delta 40, Kasarani, opposite Safari Park Hotel', map: 'https://maps.app.goo.gl/kasarani-greenbay' },
            { name: 'Roysambu Outlet', address: 'Roysambu, near TRM Mall', map: 'https://maps.app.goo.gl/roysambu-greenbay' },
        ],
        copy: {
            currency: 'KES',
            heroKicker: 'Karibu! Kenya’s AI appliance evaluator',
            heroPickup: 'Free pickup anywhere in Nairobi, settled by our sourcing team.',
            pickupShort: 'We collect anywhere in Nairobi',
            pickupLine: 'Free pickup in Nairobi',
            pickupTitle: 'Free pickup in Nairobi',
            stepOfferTitle: 'Instant offer',
            stepOfferBody: 'Get a fair, data-backed offer within minutes. Not happy with it? Our team will talk it through with you.',
            pricePlaceholder: 'e.g. 25000',
            handOver: 'Free pickup anywhere in Nairobi. Our sourcing team inspects and settles with you. Done!',
            footerPlace: 'Nairobi, Kenya',
            phoneDisplay: '+254 709 155 506',
            email: 'info@greenbay.market',
            mockOffer: 'KES 28,500',
            namePlaceholder: 'e.g. Wanjiku',
            phonePlaceholder: 'e.g. 0712 345 678',
            phoneError: 'Enter a valid phone number, e.g. 0712 345 678 or +254 712 345 678.',
        },
    },
    UG: {
        code: 'UG', name: 'Uganda', currency: 'UGX', locale: 'en-UG',
        // WhatsApp, phone and email as published on uganda.greenbay.market.
        whatsapp: '256793194148',
        phone: '+256323200861', email: 'info.uganda@greenbay.market',
        store: 'https://uganda.greenbay.market',
        greeting: 'Tukusanyukidde',
        city: null, // TODO(Uganda team): pickup city/area once confirmed.
        showConfidence: false,
        outlets: [],
        copy: {
            currency: 'UGX',
            heroKicker: 'Tukusanyukidde! GreenBay trade-in, Uganda',
            heroPickup: 'Serving sellers across Uganda. Our team arranges collection with you.',
            pickupShort: 'We arrange collection across Uganda',
            pickupLine: 'Collection arranged by our Uganda team',
            pickupTitle: 'Collection arranged',
            stepOfferTitle: 'Offer confirmed by our team',
            stepOfferBody: 'You get a preliminary estimate straight away, and our Uganda team confirms the final offer with you.',
            pricePlaceholder: 'e.g. 800000',
            handOver: 'Our Uganda team inspects the item, arranges collection and settles with you.',
            footerPlace: 'Uganda',
            phoneDisplay: '+256 323 200 861',
            email: 'info.uganda@greenbay.market',
            mockOffer: 'UGX 780,000',
            namePlaceholder: 'e.g. Nakato',
            phonePlaceholder: 'e.g. 0772 123 456',
            phoneError: 'Enter a valid phone number, e.g. 0772 123 456 or +256 772 123 456.',
        },
    },
    NG: {
        code: 'NG', name: 'Nigeria', currency: 'NGN', locale: 'en-NG',
        // TODO(Nigeria team): nigeria.greenbay.market lists no WhatsApp line
        // yet, so chats go to the Kenyan CX line. Phone and email are the
        // Nigeria site's own.
        whatsapp: '254705919099',
        phone: '+2342017000170', email: 'info.nigeria@greenbay.market',
        store: 'https://nigeria.greenbay.market',
        greeting: 'Welcome',
        city: null, // TODO(Nigeria team): pickup city/area once confirmed.
        showConfidence: false,
        outlets: [],
        copy: {
            currency: 'NGN',
            heroKicker: 'Welcome! GreenBay trade-in, Nigeria',
            heroPickup: 'Serving sellers across Nigeria. Our team arranges collection with you.',
            pickupShort: 'We arrange collection across Nigeria',
            pickupLine: 'Collection arranged by our Nigeria team',
            pickupTitle: 'Collection arranged',
            stepOfferTitle: 'Offer confirmed by our team',
            stepOfferBody: 'You get a preliminary estimate straight away, and our Nigeria team confirms the final offer with you.',
            pricePlaceholder: 'e.g. 350000',
            handOver: 'Our Nigeria team inspects the item, arranges collection and settles with you.',
            footerPlace: 'Nigeria',
            phoneDisplay: '+234 201 700 0170',
            email: 'info.nigeria@greenbay.market',
            mockOffer: 'NGN 335,000',
            namePlaceholder: 'e.g. Chiamaka',
            phonePlaceholder: 'e.g. 0803 123 4567',
            phoneError: 'Enter a valid phone number, e.g. 0803 123 4567 or +234 803 123 4567.',
        },
    },
};

// Market: the portal path wins (/app/ug), then the browser timezone, then Kenya.
(function detectCountry() {
    let code = null;
    const m = /^\/app\/(ke|ug|ng)\/?$/i.exec(window.location.pathname || '');
    if (m) code = m[1].toUpperCase();
    if (!code) {
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        if (tz.startsWith('Africa/Kampala')) code = 'UG';
        else if (tz.startsWith('Africa/Lagos')) code = 'NG';
        else code = 'KE';
    }
    window.__gbCountry = code;
    try { document.documentElement.dataset.country = code; } catch (_) { /* ignore */ }
})();

const MARKET = COUNTRY_CONFIG[window.__gbCountry] || COUNTRY_CONFIG.KE;

/** Fill every [data-copy] / [data-copy-placeholder] node with the market's text. */
function applyMarketCopy() {
    document.querySelectorAll('[data-copy]').forEach(el => {
        const v = MARKET.copy[el.dataset.copy];
        if (v) el.textContent = v;
    });
    document.querySelectorAll('[data-copy-placeholder]').forEach(el => {
        const v = MARKET.copy[el.dataset.copyPlaceholder];
        if (v) el.setAttribute('placeholder', v);
    });
    document.querySelectorAll('a[data-wa]').forEach(el => {
        const text = el.dataset.wa ? '?text=' + encodeURIComponent(el.dataset.wa) : '';
        el.setAttribute('href', `https://wa.me/${MARKET.whatsapp}${text}`);
    });
    document.querySelectorAll('a[data-contact="phone"]').forEach(el => el.setAttribute('href', `tel:${MARKET.phone}`));
    document.querySelectorAll('a[data-contact="email"]').forEach(el => el.setAttribute('href', `mailto:${MARKET.email}`));
    // Store links go to this market's own GreenBay store (its home page: the
    // Kenyan collection paths are not guaranteed to exist on the others).
    if (MARKET.store !== COUNTRY_CONFIG.KE.store) {
        document.querySelectorAll('a[href^="https://greenbay.market"]').forEach(el => el.setAttribute('href', MARKET.store));
    }
    // Brands sold in one market only (Von, Scanfrost, ...).
    document.querySelectorAll('#brandSelect option[data-markets]').forEach(opt => {
        if (opt.dataset.markets.split(',').indexOf(MARKET.code) === -1) opt.remove();
    });
    document.title = `Sell Your Appliance in ${MARKET.name} | GreenBay AI Evaluator`;
}

try { applyMarketCopy(); } catch (_) { /* re-applied on DOMContentLoaded */ }

// The storage key carries a version: answers saved by the old 10-step wizard
// do not fit this one.
const GB_STATE_KEY = 'gb_eval_state_v2_' + MARKET.code;

const state = {
    currentStep: 1,
    sessionId: null,
    answers: {
        category: null,
        otherDescription: '',
        age: null,
        condition: null,
        conditionGrade: null,
        defects: [], // defect codes from the chips under "Noticeable wear or faults"
        issues: '',
        modelPhoto: null, // { dataUrl }
        plateRead: 'idle', // idle | reading | done | failed
        brand: null,
        brandSource: null, // 'label' (read off the plate) | 'manual'
        model: '',
        sellerName: '',
        sellerPhone: '',
        photos: [], // { file, dataUrl, id }
        video: null, // { s3_key, url, keyframes:[{id,dataUrl}], duration, note }
        price: null,
    },
    evaluation: null,
    negotiation: { round: 0, offers: [], counters: [], status: 'idle' },
    chatHistory: [],
};

/* ============================================================
 ANALYTICS (GA4, measurement id G-2REFLT805D, loaded in index.html)

 Funnel events sent with gtag('event', name, params). Every event carries
 the attribution captured on first load (utm_* from the query string,
 document.referrer, location.href) so campaigns can be attributed. The
 attribution lives in sessionStorage so it survives the whole wizard.

 Never send names, phones or prices to GA4. The only per-event params are
 step (integer), decision (accept | negotiate | review | reject) and
 option (A | B | C). URLs are cleaned with window.gbCleanUrl (index.html)
 before they are stored or sent: the landing URL keeps only campaign tags and
 ad-click ids, the referrer keeps no query string at all. trackEvent drops
 any parameter that is not on GB_EVENT_PARAMS, so a future call site cannot
 leak a field by accident.
 ============================================================ */
const GB_ATTRIBUTION_KEY = 'gb_attribution';
const GB_ATTRIBUTION_FIELDS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content'];

// Parameters an event may carry, and the longest value GA4 keeps (100 chars).
const GB_EVENT_PARAMS = ['step', 'decision', 'option'];

function cleanUrl(url, keepQuery) {
    try {
        if (typeof window.gbCleanUrl === 'function') return window.gbCleanUrl(url, keepQuery);
    } catch (_) { /* fall through */ }
    return String(url || '').split(/[?#]/)[0];   // no cleaner: keep the path only
}

function captureAttribution() {
    let stored = null;
    try {
        const raw = sessionStorage.getItem(GB_ATTRIBUTION_KEY);
        if (raw) stored = JSON.parse(raw);
    } catch (_) { stored = null; }
    if (stored) {
        // A capture made before URL cleaning shipped is cleaned on read.
        stored.referrer = cleanUrl(stored.referrer, false);
        stored.landing_url = cleanUrl(stored.landing_url, true);
    }

    let params;
    try { params = new URLSearchParams(window.location.search); } catch (_) { params = null; }
    const hasUtm = !!params && GB_ATTRIBUTION_FIELDS.some(f => params.get(f));

    // Keep the first-load capture for the whole session. Only a fresh
    // campaign link (new utm_* in the query) replaces it.
    if (stored && stored.landing_url && !hasUtm) return stored;

    const fresh = {
        utm_source: (params && params.get('utm_source')) || '',
        utm_medium: (params && params.get('utm_medium')) || '',
        utm_campaign: (params && params.get('utm_campaign')) || '',
        utm_content: (params && params.get('utm_content')) || '',
        referrer: cleanUrl(document.referrer, false).slice(0, 500),
        landing_url: cleanUrl(window.location.href, true).slice(0, 2000),
    };
    try { sessionStorage.setItem(GB_ATTRIBUTION_KEY, JSON.stringify(fresh)); } catch (_) { /* ignore */ }
    return fresh;
}

const attribution = captureAttribution();

function trackEvent(name, params) {
    try {
        if (typeof window.gtag !== 'function') return;
        const payload = Object.assign({}, attribution);
        GB_EVENT_PARAMS.forEach(k => {
            if (params && params[k] !== undefined && params[k] !== null) {
                payload[k] = typeof params[k] === 'number' ? params[k] : String(params[k]).slice(0, 100);
            }
        });
        window.gtag('event', name, payload);
    } catch (_) { /* analytics must never break the wizard */ }
}

/** Fire an event once per browser session (guarded by sessionStorage). */
function trackOnce(name, params) {
    const flag = 'gb_evt_' + name;
    try {
        if (sessionStorage.getItem(flag)) return;
        sessionStorage.setItem(flag, '1');
    } catch (_) { /* fall through and send anyway */ }
    trackEvent(name, params);
}

// Restore from localStorage — auto-reset if previous evaluation was complete
try {
    localStorage.removeItem('gb_eval_state'); // the 10-step wizard's state
    const saved = localStorage.getItem(GB_STATE_KEY);
    if (saved) {
        const parsed = JSON.parse(saved);
        if (parsed && parsed.currentStep) {
            // If the previous session was completed OR has evaluation results,
            // start fresh. Results can't survive page reload (HTML isn't persisted).
            if (parsed.currentStep > TOTAL_STEPS || parsed.evaluation || typeof parsed.currentStep !== 'number') {
                localStorage.removeItem(GB_STATE_KEY);
            } else {
                Object.assign(state, parsed);
                state.answers = Object.assign({}, state.answers, parsed.answers || {});
                // Photos can't survive localStorage (blobs), so reset them and
                // anything that depended on the label photo.
                state.answers.photos = [];
                state.answers.modelPhoto = null;
                state.answers.video = null;
                if (state.answers.brandSource === 'label' || state.answers.plateRead === 'reading') {
                    state.answers.brand = null;
                    state.answers.brandSource = null;
                    state.answers.model = '';
                }
                state.answers.plateRead = 'idle';
                if (!Array.isArray(state.answers.defects)) state.answers.defects = [];
            }
        }
    }
} catch (_) { /* ignore */ }

function saveState() {
    try {
        // Don't persist completed evaluations — they'll reset on reload
        if (state.currentStep > TOTAL_STEPS || typeof state.currentStep !== 'number') return;
        const toSave = { ...state, answers: { ...state.answers, photos: [], modelPhoto: null, video: null } };
        localStorage.setItem(GB_STATE_KEY, JSON.stringify(toSave));
    } catch (_) { /* ignore */ }
}

/**
 * Hard reset: wipe all client-side state and do a full page reload for a
 * guaranteed clean UI. Used by every "Start New Evaluation" button across
 * the result screens.
 */
function startNewEvaluation() {
    try { localStorage.removeItem(GB_STATE_KEY); } catch (_) { /* ignore */ }
    // A fresh wizard run counts as a new wizard_start; attribution is kept.
    try { sessionStorage.removeItem('gb_evt_wizard_start'); } catch (_) { /* ignore */ }
    // Keeps the market path (/app/ug) so the customer stays in their country.
    window.location.href = window.location.pathname + window.location.hash;
}
window.startNewEvaluation = startNewEvaluation;

/* ============================================================
 STEP LABELS & CHAT MESSAGES
 ============================================================ */
const STEP_LABELS = {
    1: 'Your appliance',
    2: 'Photos',
    3: 'Your price',
    4: 'Your details',
};

const CATEGORY_NAMES = {
    refrigerator: 'Refrigerator',
    washing_machine: 'Washing Machine',
    tv_monitor: 'TV / Monitor',
    cooker_oven: 'Cooker / Oven',
    microwave: 'Microwave',
    small_kitchen: 'Small Kitchen Appliance',
    other: 'Other Appliance',
};

const CONDITION_LABELS = {
    new: 'New / sealed',
    like_new: 'Like new',
    used_good: 'Used, works well',
    wear_faults: 'Noticeable wear or faults',
    not_working: 'Does not turn on',
};

// Defect chips under "Noticeable wear or faults". The code is the defect type
// the engine prices (unknown codes take the default deduction); "rust" keeps
// the existing hard-reject rule and "not_working" sends the item to the team.
const DEFECT_LABELS = {
    cosmetic_dent: 'Dents',
    cosmetic_scratch: 'Scratches',
    rust: 'Rust',
    missing_parts: 'Missing parts',
    not_cooling_heating: 'Not cooling / heating',
    noisy: 'Noisy',
    water_leak: 'Leaks',
    not_working: "Doesn't turn on",
};

/* ============================================================
 INITIALIZATION
 ============================================================ */
document.addEventListener('DOMContentLoaded', () => {
    applyMarketCopy();

    // Greet in chat
    addChatMessage('bot', `${MARKET.greeting}! I'm Kay, GreenBay's AI evaluator for ${MARKET.name}. Answer four quick steps and I'll value your appliance.`);
    setTimeout(() => {
        addChatMessage('bot', "Start with what you're selling, how old it is and its condition.");
    }, 800);

    // Set up photo drag & drop
    const zone = document.getElementById('photoUploadZone');
    if (zone) {
        zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
        zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
        zone.addEventListener('drop', (e) => { e.preventDefault(); zone.classList.remove('drag-over'); handlePhotoUpload(e.dataTransfer.files); });
    }

    // Set up model photo drag & drop
    const modelZone = document.getElementById('modelPhotoZone');
    if (modelZone) {
        modelZone.addEventListener('dragover', (e) => { e.preventDefault(); modelZone.classList.add('drag-over'); });
        modelZone.addEventListener('dragleave', () => modelZone.classList.remove('drag-over'));
        modelZone.addEventListener('drop', (e) => { e.preventDefault(); modelZone.classList.remove('drag-over'); handleModelPhotoUpload(e.dataTransfer.files); });
    }

    // body.in-eval while the evaluator is on screen: the floating WhatsApp
    // button steps aside (the evaluator has its own WhatsApp routes) and the
    // docked chat bar only shows here on phones.
    const evalSection = document.getElementById('evaluate');
    if (evalSection && 'IntersectionObserver' in window) {
        new IntersectionObserver(entries => entries.forEach(e =>
            document.body.classList.toggle('in-eval', e.isIntersecting)
        ), { rootMargin: '0px 0px -35% 0px' }).observe(evalSection);
    }

    // If we have saved state, try to restore the wizard position
    if (state.currentStep > 1) {
        goToStep(state.currentStep);
    }

    updateWizardUI();
});

/* ============================================================
 WIZARD NAVIGATION
 ============================================================ */
function goToStep(stepNum) {
    // Hide all steps
    document.querySelectorAll('.wizard-step').forEach(el => el.classList.remove('active'));
    // Show target step
    const target = document.getElementById(`step${stepNum}`) || document.querySelector(`[data-step="${stepNum}"]`);
    if (target) target.classList.add('active');

    state.currentStep = stepNum;
    updateWizardUI();
    saveState();

    // On a phone the next screen starts below the fold; bring its top into view.
    const panel = document.getElementById('wizardPanel');
    if (panel && typeof panel.getBoundingClientRect === 'function' && panel.getBoundingClientRect().top < 0) {
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function updateWizardUI() {
    const step = state.currentStep;
    const inWizard = typeof step === 'number' && step <= TOTAL_STEPS;
    const label = STEP_LABELS[step] || 'Results';
    const fill = inWizard ? ((step / TOTAL_STEPS) * 100) : 100;

    // Outside the wizard the result screens set their own header
    // (setResultHeader); until one has, the analysis is still running.
    if (inWizard || !state.evaluation) {
        document.getElementById('progressLabel').textContent = inWizard ? `Step ${step}: ${label}` : 'Analysing your appliance';
        document.getElementById('progressCount').textContent = inWizard ? `${step} / ${TOTAL_STEPS}` : '…';
    }
    document.getElementById('progressFill').style.width = `${fill}%`;

    // Back button
    const prevBtn = document.getElementById('prevBtn');
    prevBtn.style.visibility = inWizard && step > 1 ? 'visible' : 'hidden';

    // Next button
    const nextBtn = document.getElementById('nextBtn');
    if (step === TOTAL_STEPS) {
        nextBtn.textContent = 'Get my valuation';
        nextBtn.classList.add('btn-lg');
    } else if (!inWizard) {
        nextBtn.classList.add('hidden');
    } else {
        nextBtn.textContent = 'Next';
        nextBtn.classList.remove('btn-lg');
    }

    // Enable/disable next based on current answer
    nextBtn.disabled = !isStepValid(step);

    // Re-apply selected states (on restoration)
    restoreSelections();
}

function isStepValid(step) {
    const a = state.answers;
    switch (step) {
        case 1: return !!a.category
            && (a.category !== 'other' || (a.otherDescription || '').trim().length >= 2)
            && a.age !== null && a.age !== undefined
            && !!a.condition;
        case 2: return a.photos.length >= 3 && !!a.brand && a.plateRead !== 'reading';
        case 3: return true; // price can be null ("make me an offer")
        case 4: return (a.sellerName || '').trim().length >= 2 && isValidPhone(a.sellerPhone);
        default: return true;
    }
}

function refreshNext() {
    const nextBtn = document.getElementById('nextBtn');
    if (nextBtn) nextBtn.disabled = !isStepValid(state.currentStep);
}

function nextStep() {
    if (state.currentStep === TOTAL_STEPS) {
        // The offer is only shown to a customer we can call back. A session
        // restored from localStorage can hold a phone saved under older,
        // looser rules, so check again before submitting.
        if (!isStepValid(TOTAL_STEPS)) {
            returnToContactStep();
            return;
        }
        startAnalysis();
        return;
    }
    if (typeof state.currentStep !== 'number' || state.currentStep >= TOTAL_STEPS) return;
    if (!isStepValid(state.currentStep)) {
        explainWhyNotValid(state.currentStep);
        return;
    }

    const next = state.currentStep + 1;

    // Chat mirror for previous step
    mirrorStepToChat(state.currentStep);

    goToStep(next);
    trackEvent('wizard_step', { step: next });

    // Chat prompt for new step
    promptNextStep(next);
}

function explainWhyNotValid(step) {
    const a = state.answers;
    if (step === 2 && a.plateRead === 'reading') {
        addChatMessage('bot', 'Still reading your label, one moment…');
    } else if (step === 2 && !a.brand) {
        showBrandFallback();
    }
}

function prevStep() {
    if (typeof state.currentStep !== 'number' || state.currentStep <= 1) return;
    goToStep(state.currentStep - 1);
}

function restoreSelections() {
    const a = state.answers;
    const setSelect = (id, value) => {
        const el = document.getElementById(id);
        if (el && value !== null && value !== undefined) el.value = String(value);
    };
    setSelect('categorySelect', a.category);
    setSelect('ageSelect', a.age);
    const other = document.getElementById('otherDescriptionGroup');
    if (other) other.hidden = a.category !== 'other';
    const otherInput = document.getElementById('otherDescription');
    if (otherInput && !otherInput.value && a.otherDescription) otherInput.value = a.otherDescription;

    document.querySelectorAll('#conditionOptions .chip').forEach(chip => {
        const on = chip.dataset.value === a.condition;
        chip.classList.toggle('selected', on);
        chip.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    const defectGroup = document.getElementById('defectGroup');
    if (defectGroup) defectGroup.hidden = a.condition !== 'wear_faults';
    document.querySelectorAll('#defectOptions .chip').forEach(chip => {
        const on = (a.defects || []).indexOf(chip.dataset.defect) !== -1;
        chip.classList.toggle('selected', on);
        chip.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const issues = document.getElementById('issuesInput');
    if (issues && !issues.value && a.issues) issues.value = a.issues;

    if (a.brandSource === 'manual' && a.brand) {
        const sel = document.getElementById('brandSelect');
        const fallback = document.getElementById('brandFallback');
        if (fallback) fallback.hidden = false;
        if (sel) {
            const known = Array.from(sel.options).some(o => o.value === a.brand);
            sel.value = known ? a.brand : '__other';
            const custom = document.getElementById('brandCustom');
            if (custom && !known) { custom.hidden = false; if (!custom.value) custom.value = a.brand; }
        }
    }

    const nameInput = document.getElementById('sellerNameInput');
    if (nameInput && !nameInput.value && a.sellerName) nameInput.value = a.sellerName;
    const phoneInput = document.getElementById('sellerPhoneInput');
    if (phoneInput && !phoneInput.value && a.sellerPhone) phoneInput.value = a.sellerPhone;
    const priceInput = document.getElementById('priceInput');
    if (priceInput && !priceInput.value && a.price) priceInput.value = a.price;
}

/* ============================================================
 ANSWER HANDLERS (step 1)
 ============================================================ */
function selectCategory(value) {
    state.answers.category = value || null;
    // The first category choice is the start of the wizard.
    if (value) trackOnce('wizard_start');
    const other = document.getElementById('otherDescriptionGroup');
    if (other) other.hidden = value !== 'other';
    refreshNext();
    saveState();
}

function selectAge(value) {
    const n = parseFloat(value);
    state.answers.age = isNaN(n) ? null : n;
    refreshNext();
    saveState();
}

function selectCondition(el) {
    const a = state.answers;
    a.condition = el.dataset.value;
    a.conditionGrade = el.dataset.grade || 'B';
    if (a.condition !== 'wear_faults') a.defects = [];
    restoreSelections();
    refreshNext();
    saveState();
}

function toggleDefect(el) {
    const code = el.dataset.defect;
    const list = state.answers.defects || [];
    const i = list.indexOf(code);
    if (i === -1) list.push(code); else list.splice(i, 1);
    state.answers.defects = list;
    restoreSelections();
    saveState();
}

/** Grade and 0-100 score the engine receives, from the condition + defect chips. */
function conditionForEngine() {
    const a = state.answers;
    if ((a.defects || []).indexOf('not_working') !== -1) return { grade: 'D', score: 10 };
    const scores = { new: 98, like_new: 90, used_good: 70, wear_faults: 45 };
    return { grade: a.conditionGrade || 'B', score: scores[a.condition] || 60 };
}

function selectCategoryFromLanding(cat) {
    trackOnce('wizard_start');
    // Scroll to evaluation section
    document.getElementById('evaluate').scrollIntoView({ behavior: 'smooth' });
    if (typeof state.currentStep === 'number' && state.currentStep !== 1) goToStep(1);
    selectCategory(cat);
    const sel = document.getElementById('categorySelect');
    if (sel) sel.value = cat;
}

/* ============================================================
 BRAND (step 2): read from the label, or chosen by hand
 ============================================================ */
function showBrandFallback(message) {
    const fallback = document.getElementById('brandFallback');
    if (fallback) fallback.hidden = false;
    const btn = document.getElementById('noLabelBtn');
    if (btn) btn.hidden = true;
    if (message) setPlateStatus('warn', message);
    const sel = document.getElementById('brandSelect');
    if (sel && !state.answers.brand) setTimeout(() => sel.focus(), 50);
}

function selectBrand(value) {
    const custom = document.getElementById('brandCustom');
    if (value === '__other') {
        if (custom) { custom.hidden = false; custom.focus(); }
        state.answers.brand = sanitiseText(custom ? custom.value.trim() : '') || null;
    } else {
        if (custom) custom.hidden = true;
        state.answers.brand = sanitiseText(value || '') || null;
    }
    state.answers.brandSource = state.answers.brand ? 'manual' : null;
    refreshNext();
    saveState();
}

function handleCustomBrand(value) {
    state.answers.brand = sanitiseText(String(value || '').trim()) || null;
    state.answers.brandSource = state.answers.brand ? 'manual' : null;
    refreshNext();
    saveState();
}

function setPlateStatus(kind, html) {
    const el = document.getElementById('plateStatus');
    if (!el) return;
    el.className = 'plate-status' + (kind ? ` plate-status--${kind}` : '');
    el.innerHTML = html || '';
}

// Seller phone rules. THE SAME RULES LIVE IN normalise_phone() in
// greenbay_ai_evaluator/api/schemas.py, which rejects (HTTP 422) anything this
// function rejects. Change both together, or valid customers lose their
// evaluation. Returns the stored form (country code + national number, digits
// only, e.g. 254712345678) or null.
//   Kenya: 0712345678, 0112345678, 712345678, +254712345678, 254712345678,
//          00254712345678, +254 0712 345 678, with spaces / dashes / dots.
//   Uganda (256) and Nigeria (234): the same shapes; a number written locally
//          is read with the detected country (default Kenya).
//   Anywhere else: international form only, 8 to 15 digits: + or 00 prefix,
//          or 11+ digits with no leading 0.
const PHONE_RULES = {
    KE: ['254', '[17]\\d{8}'],
    UG: ['256', '[2-9]\\d{8}'],
    NG: ['234', '[789][01]\\d{8}'],
};

function normalisePhone(raw, country) {
    if (raw === null || raw === undefined) return null;
    // The API refuses more than 30 characters as typed (separators included).
    const typed = String(raw).replace(/\ufeff/g, '').trim();
    if (typed.length > 30) return null;
    let text = typed.replace(/<[^>]+>/g, '').trim().replace(/[\s\-().]/g, '');
    let international = false;
    if (text.startsWith('+')) { international = true; text = text.slice(1); }
    else if (text.startsWith('00')) { international = true; text = text.slice(2); }
    if (!/^[0-9]+$/.test(text)) return null;

    // A number that carries one of our calling codes must fit that country.
    for (const key of Object.keys(PHONE_RULES)) {
        const [code, national] = PHONE_RULES[key];
        if (text.startsWith(code) && (international || text.length > 10)) {
            const m = new RegExp(`^${code}0?(${national})$`).exec(text);
            return m ? code + m[1] : null;
        }
    }
    // 11+ digits with no leading 0 cannot be a local KE/UG/NG number: it is a
    // full international number written without the "+".
    if (international || (text.length >= 11 && !text.startsWith('0'))) {
        return (text.length >= 8 && text.length <= 15 && !text.startsWith('0')) ? text : null;
    }
    const rule = PHONE_RULES[String(country || 'KE').toUpperCase().trim()] || PHONE_RULES.KE;
    const m = new RegExp(`^0?(${rule[1]})$`).exec(text);
    return m ? rule[0] + m[1] : null;
}

function isValidPhone(raw) {
    return normalisePhone(raw, window.__gbCountry || 'KE') !== null;
}

// Send the customer back to the contact step with the inline message showing.
// Used when a restored session holds a phone the current rules reject, and
// when the backend answers 422 for the phone.
function returnToContactStep() {
    document.getElementById('wizardFooter').classList.remove('hidden');
    goToStep(TOTAL_STEPS);
    const err = document.getElementById('phoneError');
    if (err) err.style.display = 'block';
    const input = document.getElementById('sellerPhoneInput');
    if (input) {
        input.value = state.answers.sellerPhone || '';
        input.focus();
    }
}

// Free-text answers are interpolated into innerHTML in many places, so strip
// HTML-significant characters once here rather than trusting every call site.
// Mirrors the backend's _strip_tags in api/schemas.py.
function sanitiseText(value) {
    return String(value == null ? '' : value).replace(/[<>]/g, '');
}

function updateAnswer(field, value) {
    state.answers[field] = typeof value === 'string' ? sanitiseText(value) : value;
    // Inline validation feedback for the contact step.
    if (field === 'sellerPhone') {
        const err = document.getElementById('phoneError');
        if (err) err.style.display = (value && !isValidPhone(value)) ? 'block' : 'none';
    }
    if (field === 'sellerName') {
        const err = document.getElementById('nameError');
        if (err) err.style.display = (value && value.trim().length < 2) ? 'block' : 'none';
    }
    document.getElementById('nextBtn').disabled = !isStepValid(state.currentStep);
    saveState();
}

function handlePriceInput(raw) {
    state.answers.price = parsePrice(raw);
    saveState();
}

function skipPrice() {
    state.answers.price = null;
    const input = document.getElementById('priceInput');
    if (input) input.value = '';
    addChatMessage('bot', `No problem, I'll make the first offer.`);
    saveState();
    nextStep();
}

/* ============================================================
 MODEL LABEL PHOTO: the AI reads brand + model off it
 ============================================================ */
const PLATE_READ_TIMEOUT_MS = 25000;

function handleModelPhotoUpload(files) {
    const file = files[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
        addChatMessage('bot', 'That file does not look like a photo. Please upload a JPEG or PNG image of the model label.');
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        addChatMessage('bot', `That photo is too large (${(file.size / 1024 / 1024).toFixed(1)}MB). The limit is 10MB.`);
        return;
    }

    const reader = new FileReader();
    reader.onload = (e) => {
        compressImage(e.target.result, 1024, 0.85, (compressedDataUrl) => {
            state.answers.modelPhoto = { dataUrl: compressedDataUrl };

            // Show preview
            const placeholder = document.getElementById('modelPhotoPlaceholder');
            const preview = document.getElementById('modelPhotoPreview');
            const img = document.getElementById('modelPhotoImg');
            if (placeholder) placeholder.hidden = true;
            if (preview) preview.hidden = false;
            if (img) img.src = compressedDataUrl;

            lookupModelFromPhoto(compressedDataUrl);
        });
    };
    reader.readAsDataURL(file);
}

async function lookupModelFromPhoto(dataUrl) {
    const a = state.answers;
    // A brand picked by hand stays; a brand read off an earlier photo is re-read.
    if (a.brandSource !== 'manual') { a.brand = null; a.brandSource = null; }
    a.model = '';
    a.plateRead = 'reading';
    setPlateStatus('reading', '<span class="spinner-dot"></span> Reading your label…');
    refreshNext();

    let result = null;
    try {
        const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
        const timer = ctrl ? setTimeout(() => ctrl.abort(), PLATE_READ_TIMEOUT_MS) : null;
        try {
            const response = await fetch(`${API_BASE}/tradein/model-lookup`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image_data: dataUrl,
                    typed_model: '',
                    category: a.category || '',
                    brand: a.brandSource === 'manual' ? (a.brand || '') : '',
                }),
                signal: ctrl ? ctrl.signal : undefined,
            });
            if (response.ok) result = await response.json();
        } finally {
            if (timer) clearTimeout(timer);
        }
    } catch (_) {
        result = null;
    }

    // Model output read off a photo: sanitised like any other free text, and
    // only what was actually read is used. A failed read is said plainly.
    const ocrBrand = sanitiseText((result && result.brand) || '').trim();
    const ocrModel = sanitiseText((result && result.model_number) || '').trim();
    const model = ocrModel;
    if (ocrModel) state.answers.model = ocrModel;
    if (ocrBrand && a.brandSource !== 'manual') {
        state.answers.brand = ocrBrand;
        a.brandSource = 'label';
    }

    if (a.brand && a.brandSource === 'label') {
        a.plateRead = 'done';
        setPlateStatus('ok', `<span class="plate-found">Read from your label <strong>${escapeHtml(a.brand)}${model ? ' · ' + escapeHtml(model) : ''}</strong></span>
            <button type="button" class="link-button" onclick="showBrandFallback()">Not right?</button>`);
        const noLabel = document.getElementById('noLabelBtn');
        if (noLabel) noLabel.hidden = true;
        addChatMessage('bot', `I read <strong>${escapeHtml(a.brand)}</strong>${model ? ' ' + escapeHtml(model) : ''} from your label. Now add at least 3 photos of the appliance.`);
    } else if (a.brandSource === 'manual') {
        a.plateRead = 'done';
        setPlateStatus('ok', model
            ? `<span class="plate-found">Model read from your label <strong>${escapeHtml(model)}</strong></span>`
            : 'Label photo saved.');
    } else {
        a.plateRead = 'failed';
        showBrandFallback("We couldn't read the brand from that photo. Pick it below, or tap the photo to retake it.");
    }
    if (window.lucide) lucide.createIcons();
    refreshNext();
    saveState();
}

/* ============================================================
 PHOTO UPLOAD
 ============================================================ */
function handlePhotoUpload(files) {
    const maxPhotos = 10;
    const maxSizeMB = 10;
    const minDimension = 400; // relaxed from 1024 for mobile camera variance

    Array.from(files).forEach(file => {
        if (state.answers.photos.length >= maxPhotos) return;
        if (!file.type.startsWith('image/')) {
            addChatMessage('bot', `That file doesn't look like an image. Please upload JPEG or PNG photos. `);
            return;
        }
        if (file.size > maxSizeMB * 1024 * 1024) {
            addChatMessage('bot', `That photo is too large (${(file.size / 1024 / 1024).toFixed(1)}MB). The limit is 10MB.`);
            return;
        }

        const id = 'photo_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
        const reader = new FileReader();
        reader.onload = (e) => {
            // Compress via Canvas
            compressImage(e.target.result, 1024, 0.8, (compressedDataUrl) => {
                state.answers.photos.push({ file, dataUrl: compressedDataUrl, id });
                renderPhotoGrid();
                updatePhotoCount();
                refreshNext();
                saveState();

                // Chat feedback
                const count = state.answers.photos.length;
                if (count === 1) {
                    addChatMessage('bot', `1 photo added. At least 3 are needed.`);
                } else if (count === 2) {
                    addChatMessage('bot', `${count} photos added. One more and you can continue.`);
                } else if (count === 3) {
                    addChatMessage('bot', `${count} photos added, you can continue. 5 clear photos give the most accurate valuation.`);
                } else if (count === 5) {
                    addChatMessage('bot', `5 photos added. That's ideal for an accurate valuation.`);
                } else if (count > 5) {
                    addChatMessage('bot', `${count} photos added. Extra angles help the valuation.`);
                }
            });
        };
        reader.readAsDataURL(file);
    });
}

/* ============================================================
 VIDEO UPLOAD (optional)
 ============================================================ */
async function handleVideoUpload(files) {
    if (!files || files.length === 0) return;
    const file = files[0];
    const MAX_MB = 45;

    const statusEl = document.getElementById('videoStatus');
    const framesEl = document.getElementById('videoKeyframes');
    if (!statusEl || !framesEl) return;

    const show = (html) => {
        statusEl.style.display = 'block';
        statusEl.innerHTML = html;
    };

    if (!file.type.startsWith('video/') &&
        !/\.(mp4|mov|webm|mkv)$/i.test(file.name || '')) {
        show('<span style="color:var(--red);">Please choose an MP4, MOV or WebM video.</span>');
        return;
    }
    if (file.size > MAX_MB * 1024 * 1024) {
        const mb = (file.size / 1024 / 1024).toFixed(1);
        show(`<span style="color:var(--red);">Video is ${mb} MB; max ${MAX_MB} MB.</span>`);
        return;
    }

    show(`<span style="color:var(--muted);">Uploading ${(file.size/1024/1024).toFixed(1)} MB video and extracting keyframes…</span>`);
    framesEl.innerHTML = '';

    try {
        const form = new FormData();
        form.append('video', file, file.name || 'appliance.mp4');
        if (state.answers.sellerPhone) {
            form.append('user_phone', state.answers.sellerPhone);
        }

        const resp = await fetch(`${API_BASE}/tradein/upload-video`, {
            method: 'POST',
            body: form,
        });
        if (!resp.ok) {
            const txt = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${txt.slice(0, 160)}`);
        }
        const data = await resp.json();

        const frames = Array.isArray(data.keyframes_base64) ? data.keyframes_base64 : [];
        state.answers.video = {
            s3_key: data.video_s3_key || null,
            url: data.video_url || null,
            keyframes: frames.map((b64, i) => ({
                id: 'vf_' + Date.now() + '_' + i,
                dataUrl: 'data:image/jpeg;base64,' + b64,
            })),
            duration: data.duration_seconds || null,
            note: data.note || null,
        };

        renderVideoKeyframes();

        const bits = [];
        if (data.duration_seconds) bits.push(data.duration_seconds.toFixed(1) + 's');
        if (frames.length) bits.push(frames.length + ' keyframes');
        if (data.video_s3_key) bits.push('saved to S3');
        const subtitle = bits.join(' · ') || 'uploaded';
        const noteHtml = data.note
            ? `<div style="margin-top:6px;color:var(--gold);">${data.note}</div>` : '';
        show(`<span class="plate-status--ok">Video uploaded</span> <span style="color:var(--muted);">(${subtitle})</span>${noteHtml}`);
        addChatMessage(
            'bot',
            frames.length
                ? `Got your video — I pulled ${frames.length} frames from it to improve the assessment. Thanks!`
                : `Got your video — I'll include it with the submission. The team will review it personally.`
        );
        saveState();
    } catch (e) {
        console.error('Video upload failed:', e);
        show(`<span style="color:var(--red);">Video upload failed: ${e.message || e}</span>`);
        state.answers.video = null;
    }
}

function renderVideoKeyframes() {
    const el = document.getElementById('videoKeyframes');
    if (!el) return;
    const frames = (state.answers.video && state.answers.video.keyframes) || [];
    if (!frames.length) { el.innerHTML = ''; return; }
    el.innerHTML = frames.map(f => `
<div class="photo-thumb" style="position:relative;">
  <img src="${f.dataUrl}" alt="Video keyframe">
  <div style="position:absolute;top:4px;left:4px;background:rgba(13,78,59,.85);color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600;">
    VIDEO
  </div>
</div>
`).join('');
}
window.handleVideoUpload = handleVideoUpload;

function compressImage(dataUrl, maxDim, quality, callback) {
    const img = new Image();
    img.onload = () => {
        let w = img.width, h = img.height;
        if (w > maxDim || h > maxDim) {
            if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
            else { w = Math.round(w * maxDim / h); h = maxDim; }
        }
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        callback(canvas.toDataURL('image/jpeg', quality));
    };
    img.src = dataUrl;
}

function renderPhotoGrid() {
    const grid = document.getElementById('photoGrid');
    grid.innerHTML = state.answers.photos.map(p =>
        `<div class="photo-thumb" id="${p.id}">
 <img src="${p.dataUrl}" alt="Appliance photo">
 <div class="remove-photo" onclick="removePhoto('${p.id}')"></div>
 </div>`
    ).join('');
}

function removePhoto(id) {
    state.answers.photos = state.answers.photos.filter(p => p.id !== id);
    renderPhotoGrid();
    updatePhotoCount();
    refreshNext();
    saveState();
}

function updatePhotoCount() {
    const count = state.answers.photos.length;
    const el = document.getElementById('photoCount');
    el.innerHTML = `<span class="count-num">${count}</span> / 3 minimum photos (5 is ideal)`;
    el.classList.toggle('complete', count >= 3);
}

/* ============================================================
 CHAT PANEL
 ============================================================ */
function addChatMessage(sender, html, extraClass) {
    const container = document.getElementById('chatMessages');
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    const msgDiv = document.createElement('div');
    msgDiv.className = `msg ${sender} ${extraClass || ''}`;
    msgDiv.innerHTML = `
 <div class="msg-bubble">${html}</div>
 <div class="msg-time">${time}</div>
 `;
    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;

    state.chatHistory.push({ sender, html, time });

    // Mobile notification badge
    const badge = document.querySelector('.chat-notification-badge');
    if (badge && sender === 'bot' && !document.getElementById('chatPanel').classList.contains('expanded')) {
        badge.classList.add('has-new');
        badge.textContent = '!';
    }
}

function showTypingIndicator() {
    const container = document.getElementById('chatMessages');
    const el = document.createElement('div');
    el.className = 'typing-indicator';
    el.id = 'typingIndicator';
    el.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
}

function hideTypingIndicator() {
    const el = document.getElementById('typingIndicator');
    if (el) el.remove();
}

function sendChatMessage() {
    const input = document.getElementById('chatInput');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';

    addChatMessage('user', escapeHtml(text));

    // If in negotiation, handle as counter offer
    if (state.negotiation.status === 'active') {
        const parsed = parsePrice(text);
        if (parsed) {
            submitCounterOffer(parsed);
        } else {
            addChatMessage('bot', `Please type a price in ${curSym()} (e.g. "25000" or "25k") to make a counter-offer.`);
        }
        return;
    }

    // Generic chat , just acknowledge
    setTimeout(() => {
        addChatMessage('bot', 'Thanks. Carry on with the form and I\'ll follow along.');
    }, 600);
}

function parsePrice(text) {
    let cleaned = String(text || '').replace(/[,\s]/g, '').toLowerCase();
    cleaned = cleaned.replace(/^(kes|ksh|ugx|ush|ngn|₦)/i, '');
    if (cleaned.endsWith('k')) {
        const num = parseFloat(cleaned.slice(0, -1));
        return isNaN(num) ? null : num * 1000;
    }
    const num = parseFloat(cleaned);
    return isNaN(num) ? null : num;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function toggleChatMobile() {
    const panel = document.getElementById('chatPanel');
    panel.classList.toggle('expanded');
    document.body.classList.toggle('chat-open', panel.classList.contains('expanded'));
    const badge = panel.querySelector('.chat-notification-badge');
    if (badge) badge.classList.remove('has-new');
}

/* ============================================================
 CHAT MIRRORING , Steps Chat
 ============================================================ */
function mirrorStepToChat(step) {
    const a = state.answers;
    const ageText = a.age === null || a.age === undefined ? '' : (a.age < 1 ? 'under 1 year old' : `${a.age} years old`);
    const messages = {
        1: () => a.category
            ? `<strong>${CATEGORY_NAMES[a.category] || a.category}</strong>${a.category === 'other' && a.otherDescription ? ' (' + escapeHtml(a.otherDescription) + ')' : ''}, ${ageText}, ${CONDITION_LABELS[a.condition] || ''}${(a.defects || []).length ? ': ' + a.defects.map(d => DEFECT_LABELS[d] || d).join(', ') : ''}`
            : null,
        2: () => `${a.photos.length} photos`
            + (a.brand ? ` · Brand: <strong>${escapeHtml(a.brand)}</strong>` : '')
            + (a.model ? ` · Model: <strong>${escapeHtml(a.model)}</strong>` : ''),
        3: () => a.price ? `My asking price: <strong>${curSym()} ${formatKES(a.price)}</strong>` : 'You make the first offer.',
        4: () => a.sellerName ? `Contact: <strong>${escapeHtml(a.sellerName)}</strong> (${escapeHtml(a.sellerPhone)})` : null,
    };

    const fn = messages[step];
    if (fn) {
        const userMsg = fn();
        if (userMsg) addChatMessage('user', userMsg);
    }
}

function promptNextStep(step) {
    const prompts = {
        2: 'Now the photos. Start with the model label: I\'ll read the brand and model from it. Then add at least <strong>3 photos</strong> of the appliance (5 is ideal).',
        3: 'What price are you hoping for? This is optional, I can make the first offer.',
        4: 'Last step: your name and phone number, so our team can reach you with the offer.',
    };
    const msg = prompts[step];
    if (msg) {
        setTimeout(() => addChatMessage('bot', msg), 400);
    }
}

/* ============================================================
 ANALYSIS FLOW
 ============================================================ */
async function startAnalysis() {
    const a = state.answers;

    // Hide wizard footer
    document.getElementById('wizardFooter').classList.add('hidden');

    // Show analysis step in wizard
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = buildAnalysisProgressHTML();
    goToStep('results');

    // Chat
    addChatMessage('bot', `Thanks. Let me analyse your <strong>${escapeHtml(a.brand || '')} ${escapeHtml(a.model || CATEGORY_NAMES[a.category] || '')}</strong>...`);

    // Fire the real request NOW so the animation runs alongside it rather than
    // in front of it. The animation used to finish in ~7s and the customer then
    // watched a completed progress bar for the ~30s the call actually takes.
    const evaluationInFlight = callEvaluationAPI();

    // Animate analysis steps
    const steps = ['step-brand', 'step-condition', 'step-market', 'step-valuation'];
    const delays = [1200, 2500, 2000, 1500];

    for (let i = 0; i < steps.length; i++) {
        await sleep(delays[i]);
        const el = document.getElementById(steps[i]);
        if (el) {
            el.classList.remove('active');
            el.classList.add('complete');
            el.querySelector('.analysis-step-icon').textContent = '';
        }
        if (i + 1 < steps.length) {
            const next = document.getElementById(steps[i + 1]);
            if (next) next.classList.add('active');
        }

        // Progress, not findings. These fire before the backend has answered,
        // so anything stated as a result here would be invented.
        const chatMsgs = [
            `Reading the details for <strong>${escapeHtml(a.brand || '')}</strong>…`,
            'Reviewing your photos and condition report…',
            'Checking current market prices…',
            'Working out your offer…',
        ];
        addChatMessage('bot', chatMsgs[i]);
    }

    // Keep the last step spinning until the real answer lands.
    const lastStep = document.getElementById('step-valuation');
    if (lastStep) {
        lastStep.classList.remove('complete');
        lastStep.classList.add('active');
    }
    const stillWorking = setTimeout(() => {
        addChatMessage('bot', 'Still working: checking a few more sources to get this right.');
    }, 8000);

    try {
        await evaluationInFlight;
    } finally {
        clearTimeout(stillWorking);
    }
}

function buildAnalysisProgressHTML() {
    return `
<div class="analysis-progress">
  <p class="result-eyebrow">One moment</p>
  <h3 class="result-title">Analysing your appliance</h3>
  <div class="analysis-step active" id="step-brand">
    <div class="analysis-step-icon"></div>
    <span>Checking brand and model</span>
  </div>
  <div class="analysis-step" id="step-condition">
    <div class="analysis-step-icon"></div>
    <span>Assessing condition from your photos</span>
  </div>
  <div class="analysis-step" id="step-market">
    <div class="analysis-step-icon"></div>
    <span>Searching current market prices</span>
  </div>
  <div class="analysis-step" id="step-valuation">
    <div class="analysis-step-icon"></div>
    <span>Working out your offer</span>
  </div>
</div>`;
}

async function callEvaluationAPI() {
    const a = state.answers;

    // Grade + score from the condition picks; defects from the chips and note.
    const cond = conditionForEngine();
    const defects = (a.defects || []).map(code => ({
        type: code, description: DEFECT_LABELS[code] || code, severity: 'medium',
    }));
    const note = (a.issues || '').trim();
    if (note) {
        defects.push({ type: 'user_reported', description: note.slice(0, 500), severity: 'medium' });
    }

    // Per-category retail guess in KES. Declared as category_default so the
    // engine never counts it as a verified price; for Uganda/Nigeria the
    // backend carries it into the local currency.
    const defaultRetail = {
        refrigerator: 65000, washing_machine: 55000, tv_monitor: 45000,
        cooker_oven: 40000, microwave: 15000, small_kitchen: 12000,
        other: 30000,
    };

    // Combine uploaded photos with any video keyframes.
    // Priority: up to 5 user photos, then fill the remaining slots with
    // video keyframes (max 8 total because vision truncates to 8 on the backend).
    const photoUrls = a.photos.map(p => p.dataUrl);
    const videoFrameUrls = (a.video && a.video.keyframes)
        ? a.video.keyframes.map(f => f.dataUrl) : [];
    const photosTaken = photoUrls.slice(0, Math.min(5, photoUrls.length));
    const remaining = 8 - photosTaken.length;
    const framesTaken = videoFrameUrls.slice(0, Math.max(0, remaining));
    const combinedImageData = [...photosTaken, ...framesTaken];

    const payload = {
        category: a.category,
        brand: a.brand || 'Unknown',
        model: a.model || (a.category === 'other' && a.otherDescription ? a.otherDescription.slice(0, 200) : 'Unknown'),
        age_years: a.age || 0,
        condition_grade: cond.grade,
        condition_score: cond.score,
        defects: defects,
        seller_asking_price: a.price || null,
        seller_name: a.sellerName || null,
        seller_phone: a.sellerPhone || null,
        image_urls: [],
        image_data: combinedImageData,
        retail_price: defaultRetail[a.category] || 35000,
        retail_price_source: 'category_default',
        country: MARKET.code,
        // Size is no longer asked; the backend reads it from the label/photos.
        size_value: null,
        size_unit: null,
        // Campaign attribution captured on first load (see ANALYTICS above);
        // stored on the evaluation record and mirrored to Airtable.
        attribution: {
            utm_source: attribution.utm_source || null,
            utm_medium: attribution.utm_medium || null,
            utm_campaign: attribution.utm_campaign || null,
            utm_content: attribution.utm_content || null,
            referrer: attribution.referrer || null,
            landing_url: attribution.landing_url || null,
        },
    };

    trackEvent('evaluate_submit');

    try {
        const resp = await fetch(`${API_BASE}/tradein/evaluate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (resp.status === 422) {
            // The backend refused the request. If it is the phone, ask for it
            // again; never fall through to a demo offer for a real customer.
            // Look at where the error is (loc) and what it says (msg), never
            // at the echoed input: an over-long issues text that happens to
            // contain the word "phone" is not a phone error.
            let aboutPhone = false;
            try {
                const detail = (await resp.json()).detail;
                aboutPhone = Array.isArray(detail) && detail.some(e =>
                    (Array.isArray(e.loc) && e.loc.indexOf('seller_phone') !== -1) ||
                    /seller_phone/.test(String(e.msg || '')));
            } catch (_) { /* ignore */ }
            if (aboutPhone) {
                addChatMessage('bot', `I need a valid phone number before I can show your offer, ${MARKET.copy.phonePlaceholder}.`);
                returnToContactStep();
                return;
            }
        }
        if (!resp.ok) {
            let detail = '';
            try { detail = (await resp.json()).detail || ''; } catch (_) { }
            throw new Error(detail || `API error: ${resp.status}`);
        }

        const data = await resp.json();
        state.sessionId = data.session_id;
        state.evaluation = data;
        saveState();

        await sleep(800);
        showResults(data);

    } catch (err) {
        console.error('Evaluation API error:', err);
        // Never invent an offer. A fabricated price the backend never issued
        // cannot be honoured, is recorded nowhere, and nobody is notified.
        showEvaluationError(err && err.message);
    }
}

function showEvaluationError(detail) {
    const a = state.answers;
    const msg = detail && detail.length > 10
        ? detail
        : "We couldn't complete your valuation just now.";
    const wa = `https://wa.me/${MARKET.whatsapp}?text=` + encodeURIComponent(
        `Hi GreenBay, I tried to value my ${a.brand || ''} ${CATEGORY_NAMES[a.category] || 'appliance'} `
        + `but the valuation could not be completed. Name: ${a.sellerName || ''}. Phone: ${a.sellerPhone || ''}.`
    );

    setResultHeader('Valuation not completed');
    const resultsStep = document.getElementById('stepResults');
    if (resultsStep) {
        resultsStep.innerHTML = `
<div class="result result--error">
  <p class="result-eyebrow">Valuation</p>
  <h3 class="result-title">We couldn't finish your valuation</h3>
  <p class="result-note">${escapeHtml(msg)}</p>
  <p class="result-note">Talk to us and we'll value it by hand, or try again in a few minutes. Nothing you entered has been priced yet.</p>
  <div class="result-actions">
    <a href="${wa}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-block">${ICON('i-chat')} Talk to our team on WhatsApp</a>
    <button type="button" class="btn btn-ghost btn-block" onclick="startNewEvaluation()">Try again</button>
  </div>
</div>`;
    }
    addChatMessage('bot', `${escapeHtml(msg)} I haven't given you a price. Our team can value it by hand on WhatsApp.`);
}


/* ============================================================
 RESULTS DISPLAY
 Markup classes: .result (+ --offer/--review/--reject/--deal/--pickup/
 --outlets/--error), .result-eyebrow, .result-title, .result-amount,
 .result-sub, .result-rows, .result-note, .result-callout, .result-actions,
 .choice-list/.choice-card, .outlet-list/.outlet, .whatsapp-bridge.
 ============================================================ */
const ICON = (id, cls) => `<svg class="ico${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#${id}"/></svg>`;

// Sales agents in a shop open the page with ?agent=1 to see the expert
// pricing panel; customers never see it.
const AGENT_MODE = (() => {
    try { return new URLSearchParams(window.location.search).get('agent') === '1'; } catch (_) { return false; }
})();

function resultRows(rows) {
    const items = rows.filter(Boolean).map(([label, value]) =>
        `<div class="result-row"><dt>${label}</dt><dd>${value}</dd></div>`).join('');
    return items ? `<dl class="result-rows">${items}</dl>` : '';
}

/** The progress header above the result screens. */
function setResultHeader(text) {
    const label = document.getElementById('progressLabel');
    const count = document.getElementById('progressCount');
    if (label) label.textContent = text;
    if (count) count.textContent = '';
}

function newEvaluationLink() {
    return `<button type="button" class="btn-link" onclick="startNewEvaluation()">Start a new evaluation</button>`;
}

function expertPanel(currency) {
    if (!AGENT_MODE) return '';
    return `
<details class="result-sources" open>
  <summary>Expert pricing (staff only)</summary>
  <div id="expertFeedbackForm">
    <input type="text" id="expertNameInput" class="form-input" placeholder="Your name">
    <input type="number" id="expertPriceInput" class="form-input" placeholder="Your assessed price (${currency})">
    <textarea id="expertReasonInput" class="form-input" placeholder="Why this price? (optional)"></textarea>
    <button type="button" class="btn btn-secondary btn-block" onclick="submitExpertFeedback()">Submit expert price</button>
  </div>
  <p id="expertFeedbackSuccess" class="result-note" hidden>Thank you, your price has been recorded.</p>
</details>`;
}

function showResults(data) {
    const a = state.answers;
    const offer = data.decision === 'accept' && a.price ? a.price : data.opening_offer;
    const confidence = data.confidence_score || 0;
    const product = `${escapeHtml(a.brand || '')} ${CATEGORY_NAMES[a.category] || ''}`.trim();
    const conditionText = CONDITION_LABELS[a.condition] || '';
    const currency = data.currency_code || MARKET.currency;
    const resultsStep = document.getElementById('stepResults');

    // GA4 offer_shown: report the screen the customer actually sees, which is
    // decided below (reject screen, specialist routing, or the offer card).
    // Demo results (backend unreachable) are not real evaluations; skip them.
    const isDemo = String(data.session_id || '').startsWith('demo_');
    if (!isDemo) {
        let shownDecision = 'negotiate';
        if (data.decision === 'reject') shownDecision = 'reject';
        else if (confidence < 80 || data.decision === 'review' || data.decision === 'redirect_to_agents') shownDecision = 'review';
        else if (data.decision === 'accept') shownDecision = 'accept';
        trackEvent('offer_shown', { decision: shownDecision });
        if (shownDecision === 'review') trackEvent('human_review_routed');
    }

    // HARD REJECT: below our minimum condition
    if (data.decision === 'reject') {
        setResultHeader('Your result');
        resultsStep.innerHTML = `
<div class="result result--reject">
  <p class="result-eyebrow">Trade-in</p>
  <h3 class="result-title">We can't take this one in</h3>
  <p class="result-note">Your ${product} doesn't meet our minimum condition for a trade-in.</p>
  ${resultRows([['Reason', escapeHtml(data.decision_reason || 'Below our condition threshold')]])}
  <div class="result-callout">
    <p><strong>What you can do</strong></p>
    <ul><li>Get it repaired and try again</li><li>Ask our team about recycling it</li><li>Browse refurbished replacements on GreenBay</li></ul>
  </div>
  <div class="result-actions">
    <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(`Hi GreenBay, my ${a.brand || ''} ${CATEGORY_NAMES[a.category] || ''} was not eligible for trade-in. Can you help?`)}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-block">${ICON('i-chat')} Talk to our team</a>
    <a href="${MARKET.store}" target="_blank" rel="noopener" class="btn btn-ghost btn-block">Browse GreenBay</a>
  </div>
  ${newEvaluationLink()}
</div>`;
        addChatMessage('bot', `I'm sorry, your ${product} doesn't meet our minimum condition for a trade-in. ${escapeHtml(data.decision_reason || '')} Our team can talk you through other options.`);
        return;
    }

    // A person confirms the offer: low confidence, review, or sent to agents.
    if (confidence < 80 || data.decision === 'review' || data.decision === 'redirect_to_agents') {
        setResultHeader('Your result');
        const toAgents = data.decision === 'redirect_to_agents';
        const reviewMessage = escapeHtml(toAgents
            ? (data.decision_reason || 'Our team will assess this one with you directly.')
            : (data.customer_message || (MARKET.showConfidence
                ? `Our AI's confidence on this valuation is below 80%, so one of our trade-in specialists will confirm the offer for your ${a.brand || ''} ${CATEGORY_NAMES[a.category] || ''} personally.`
                : `Our ${MARKET.name} team will confirm this offer personally after a quick look at your photos.`)));
        const waText = `Hi GreenBay, I have a ${a.brand || ''} ${CATEGORY_NAMES[a.category] || ''} for trade-in.`
            + (toAgents ? '' : ` AI estimate: ${currency} ${offer}${MARKET.showConfidence ? ` (${confidence.toFixed(0)}% confidence)` : ''}.`)
            + ` Name: ${a.sellerName}. Phone: ${a.sellerPhone}`;
        resultsStep.innerHTML = `
<div class="result result--review">
  <p class="result-eyebrow">Next step</p>
  <h3 class="result-title">A specialist will confirm your offer</h3>
  <p class="result-note">${reviewMessage}</p>
  ${resultRows([
            ['Item', product],
            ['Condition', `Grade ${escapeHtml(data.condition_grade || '')}${conditionText ? ', ' + conditionText.toLowerCase() : ''}`],
            !toAgents && offer > 0 ? ['Preliminary estimate', `${currency} ${formatKES(offer)}`] : null,
            MARKET.showConfidence && !toAgents ? ['Confidence score', `${confidence.toFixed(0)}%`] : null,
        ])}
  <div class="result-actions">
    <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(waText)}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-block">${ICON('i-chat')} Speak to a trade-in specialist</a>
  </div>
  ${expertPanel(currency)}
  ${newEvaluationLink()}
</div>`;
        addChatMessage('bot', toAgents
            ? `${reviewMessage}`
            : `I'd like a specialist to confirm this one. The preliminary estimate is ${currency} ${formatKES(offer)}, and our team will confirm the final offer with you on WhatsApp.`);
        return;
    }

    // The offer card
    setResultHeader('Your valuation');
    const sources = AGENT_MODE && data.price_verification ? `
<details class="result-sources">
  <summary>How we priced it (${data.price_verification.num_sources || 0} sources)</summary>
  ${resultRows([
        ...(data.price_verification.sources || []).map(s => [escapeHtml(String(s.source || '').replace(/_/g, ' ')), `${currency} ${formatKES(s.price)}`]),
        ['Reconciled new price', `${currency} ${formatKES(data.price_verification.reconciled_price)}`],
    ])}
</details>` : '';
    resultsStep.innerHTML = `
<div class="result result--offer">
  <p class="result-eyebrow">Your GreenBay offer</p>
  <div class="result-amount">${currency} ${formatKES(offer)}</div>
  <p class="result-sub">${product} · Grade ${escapeHtml(data.condition_grade || '')}${conditionText ? ', ' + conditionText.toLowerCase() : ''} · valid for 7 days</p>
  ${resultRows([
        ['Estimated resale value', `${currency} ${formatKES(data.estimated_resale_value)}`],
        MARKET.showConfidence ? ['Confidence score', `${confidence.toFixed(0)}%`] : null,
        ['Comparable sales found', String(data.comparable_count || 0)],
    ])}
  <div class="result-actions">
    <button type="button" class="btn btn-primary btn-block btn-accept" onclick="acceptOffer(${offer})">Accept ${currency} ${formatKES(offer)}</button>
    <button type="button" class="btn btn-ghost btn-block btn-counter" onclick="showRejectionOptions()">Not happy with the price?</button>
  </div>
  ${sources}
</div>
<div class="whatsapp-bridge">
  ${ICON('i-chat', 'whatsapp-bridge-icon')}
  <div class="whatsapp-bridge-text">
    <h4>Prefer WhatsApp?</h4>
    <p>Carry on with a person from our team.</p>
  </div>
  <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(`Hi GreenBay, I have a ${a.brand || ''} ${CATEGORY_NAMES[a.category] || ''} valued at ${currency} ${offer}. Name: ${a.sellerName}. Phone: ${a.sellerPhone}`)}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>
</div>
${expertPanel(currency)}
${newEvaluationLink()}`;

    hideTypingIndicator();
    const decisionMessages = {
        accept: `Your asking price works for us: <strong>${currency} ${formatKES(offer)}</strong> for your ${product}. Tap Accept to lock it in.`,
        negotiate: `${a.price ? `Thanks for sharing your price of ${currency} ${formatKES(a.price)}. ` : ''}After checking current market prices and the condition, our offer for your ${product} is <strong>${currency} ${formatKES(data.opening_offer)}</strong>. Does that work for you?`,
        decline: `Thanks for sharing your price. Based on market prices and the condition, the most we can offer is <strong>${currency} ${formatKES(data.opening_offer)}</strong>. The offer stands for 7 days.`,
    };

    setTimeout(() => {
        addChatMessage('bot', decisionMessages[data.decision] || decisionMessages.negotiate);

        // If negotiable, enable negotiation in state
        if (data.decision === 'negotiate' || data.decision === 'decline') {
            state.negotiation = {
                round: 0,
                offers: [data.opening_offer],
                counters: [],
                status: 'active',
                ceiling: data.acquisition_ceiling,
                walkaway: data.walkaway_limit,
            };
            saveState();
        }
    }, 500);
}

/* ============================================================
 EXPERT FEEDBACK (staff only, ?agent=1)
 ============================================================ */
function submitExpertFeedback() {
    const name = document.getElementById('expertNameInput')?.value?.trim();
    const price = parseFloat(document.getElementById('expertPriceInput')?.value);
    const reasoning = document.getElementById('expertReasonInput')?.value?.trim();

    if (!name) { addChatMessage('bot', 'Please add your name to the expert price.'); return; }
    if (!price || price <= 0) { addChatMessage('bot', 'Please enter a valid expert price.'); return; }
    if (!state.sessionId) return;

    const btn = document.querySelector('#expertFeedbackForm button');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }

    fetch(`${API_BASE}/tradein/expert-feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            valuation_session_id: String(state.sessionId),
            expert_name: name,
            expert_price: price,
            expert_reasoning: reasoning || null,
        }),
    })
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
    .then(() => {
        const form = document.getElementById('expertFeedbackForm');
        const success = document.getElementById('expertFeedbackSuccess');
        if (form) form.hidden = true;
        if (success) success.hidden = false;
    })
    .catch(err => {
        console.error('Expert feedback error:', err);
        if (btn) { btn.disabled = false; btn.textContent = 'Submit expert price'; }
        addChatMessage('bot', "That expert price wasn't saved. Please try again.");
    });
}

/* ============================================================
 NOT HAPPY WITH THE PRICE: three alternatives
 ============================================================ */
function showRejectionOptions() {
    const a = state.answers;
    const data = state.evaluation || {};
    const offer = data.opening_offer || 0;
    const currency = data.currency_code || MARKET.currency;
    const upfront = Math.round(offer * 0.10 / 100) * 100;
    const balance = Math.round(offer * 0.90 / 100) * 100;

    const resultsStep = document.getElementById('stepResults');
    if (resultsStep.querySelector('.result-choices')) return;
    // GA4: the customer turned the offer down. Sent once per offer (the guard
    // above); the option they then pick is sent as rejection_option.
    trackEvent('offer_declined');

    const section = document.createElement('section');
    section.className = 'result-choices';
    section.innerHTML = `
<h4 class="result-subtitle">Not happy with the price? You have three other options.</h4>
<div class="choice-list">
  <button type="button" class="choice-card" onclick="selectRejectionOption('A')">
    ${ICON('i-tag', 'choice-icon')}
    <span class="choice-text"><strong class="choice-title">Option A: consignment</strong>
      <span class="choice-desc">We list your ${escapeHtml(a.brand || '')} ${CATEGORY_NAMES[a.category] || ''} on our marketplace at your price and handle listing, marketing and enquiries.</span></span>
  </button>
  <button type="button" class="choice-card" onclick="selectRejectionOption('B')">
    ${ICON('i-cash', 'choice-icon')}
    <span class="choice-text"><strong class="choice-title">Option B: 10/90 split</strong>
      <span class="choice-desc">${currency} ${formatKES(upfront)} when we collect it, then ${currency} ${formatKES(balance)} when it sells (within 90 days).</span></span>
  </button>
  <button type="button" class="choice-card" onclick="selectRejectionOption('C')">
    ${ICON('i-team', 'choice-icon')}
    <span class="choice-text"><strong class="choice-title">Option C: talk to our team</strong>
      <span class="choice-desc">Speak to a GreenBay specialist on WhatsApp about your options.</span></span>
  </button>
</div>`;
    resultsStep.appendChild(section);
    section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    addChatMessage('bot', 'No problem. Here are three other ways we can work with you.');
}

async function selectRejectionOption(option) {
    const data = state.evaluation || {};
    const currency = data.currency_code || MARKET.currency;
    const a = state.answers;

    addChatMessage('user', `I'd like option ${option}`);
    showTypingIndicator();
    trackEvent('rejection_option', { option: option });

    try {
        if (state.sessionId) {
            const resp = await fetch(`${API_BASE}/tradein/${state.sessionId}/rejection-choice`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ option: option }),
            });
            if (resp.ok) {
                const result = await resp.json();
                await sleep(800);
                hideTypingIndicator();
                addChatMessage('bot', escapeHtml(result.message || ''));

                if (option === 'C') {
                    const waMsg = encodeURIComponent(
                        `Hi GreenBay, I'd like to discuss the trade-in offer for my ${a.brand} ${CATEGORY_NAMES[a.category] || a.category}. AI offered ${currency} ${data.opening_offer}. Name: ${a.sellerName}. Phone: ${a.sellerPhone}`
                    );
                    addChatMessage('bot', `<a href="https://wa.me/${MARKET.whatsapp}?text=${waMsg}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>`);
                }

                const section = document.querySelector('.result-choices');
                if (section) {
                    section.innerHTML = `<div class="result-callout"><p><strong>Option ${option} recorded.</strong> ${escapeHtml(result.message || '')}</p></div>`;
                }
                saveState();
                return;
            }
        }
    } catch (err) {
        console.warn('Rejection choice API failed:', err);
    }

    // Reaching here means the choice was NOT recorded. Saying "we've noted your
    // preference" would be a promise nobody can act on: only 1 rejection exists
    // across ~380 evaluations, which is what this silent fallthrough produces.
    await sleep(500);
    hideTypingIndicator();
    addChatMessage('bot', `I couldn't save that choice just now, so I won't tell you it's been noted.<br><br>
 Please tap your option again, or reach us on
 <a href="https://wa.me/${MARKET.whatsapp}" target="_blank" rel="noopener">WhatsApp</a>
 and we'll pick it up from there.`);
    saveState();
}

/* ============================================================
 NEGOTIATION (chat counter-offers)
 ============================================================ */
function submitCounterFromInput() {
    const input = document.getElementById('counterInput');
    if (!input) return;
    const price = parsePrice(input.value);
    if (!price) {
        addChatMessage('bot', `Please enter a valid price in ${curSym()} (e.g. "25000" or "25k").`);
        return;
    }
    input.value = '';
    submitCounterOffer(price);
}

async function submitCounterOffer(sellerCounter) {
    addChatMessage('user', `I'd like ${curSym()} ${formatKES(sellerCounter)}`);
    state.negotiation.counters.push(sellerCounter);

    showTypingIndicator();
    await sleep(1200);
    hideTypingIndicator();

    // The server's counter route is paused, so the rounds are worked out here
    // from the ceiling the engine issued. Nothing is agreed in the browser: an
    // accepted counter goes through acceptOffer, which records it on the
    // server (and clamps it to that ceiling) before confirming anything.
    const neg = state.negotiation;
    const lastOffer = neg.offers[neg.offers.length - 1] || state.evaluation.opening_offer;
    const ceiling = neg.ceiling || state.evaluation.acquisition_ceiling;
    const maxRounds = 3;
    const stepPct = 0.05;

    neg.round++;

    let decision, systemOffer, reason;

    if (neg.round > maxRounds) {
        decision = 'decline';
        systemOffer = lastOffer;
        reason = 'That was our last round.';
    } else if (sellerCounter <= lastOffer) {
        decision = 'accept';
        systemOffer = sellerCounter;
        reason = 'Your price is at or below our offer.';
    } else if (sellerCounter <= ceiling) {
        decision = 'accept';
        systemOffer = sellerCounter;
        reason = 'Your price is within what we can pay.';
    } else if (sellerCounter > ceiling * 1.5) {
        decision = 'decline';
        systemOffer = lastOffer;
        reason = 'That is well above what we can pay for this unit.';
    } else {
        decision = 'counter';
        systemOffer = Math.min(ceiling, lastOffer + (ceiling - lastOffer) * stepPct);
        systemOffer = Math.round(systemOffer / 100) * 100;
        reason = 'We can move a little.';
    }

    handleNegotiationResponse({
        round_number: neg.round,
        decision,
        system_offer: systemOffer,
        ceiling,
        reason,
        rounds_remaining: Math.max(0, maxRounds - neg.round),
    });
}

function handleNegotiationResponse(data) {
    const neg = state.negotiation;
    neg.round = data.round_number;
    neg.offers.push(data.system_offer);

    if (data.decision === 'accept') {
        // Not 'accepted' yet: acceptOffer sets that only once the server has
        // recorded the deal, and says so plainly if it could not.
        neg.status = 'pending';
        addChatMessage('bot', `${curSym()} ${formatKES(data.system_offer)} works for us. Let me lock that in…`);
        acceptOffer(data.system_offer);

    } else if (data.decision === 'decline') {
        neg.status = 'declined';
        addChatMessage('bot', `We can't go above ${curSym()} ${formatKES(data.system_offer)} for this unit. ${data.reason} Our offer stands for 7 days if you change your mind.`);
        showNoDeal(data.system_offer);

    } else {
        const urgency = data.rounds_remaining === 0
            ? `<br><br>This is the most we can offer for this unit.`
            : '';
        addChatMessage('bot', `The best we can do is <strong>${curSym()} ${formatKES(data.system_offer)}</strong>. ${data.reason}${urgency}<br><br>Does that work for you?`);
        updateOfferCard(data.system_offer, data.rounds_remaining);
    }

    saveState();
}

function updateOfferCard(newOffer) {
    const amountEl = document.querySelector('.result-amount');
    if (amountEl) amountEl.textContent = `${curSym()} ${formatKES(newOffer)}`;

    const acceptBtn = document.querySelector('.btn-accept');
    if (acceptBtn) {
        acceptBtn.textContent = `Accept ${curSym()} ${formatKES(newOffer)}`;
        acceptBtn.setAttribute('onclick', `acceptOffer(${newOffer})`);
    }
}

async function acceptOffer(amount) {
    const currency = (state.evaluation && state.evaluation.currency_code) || MARKET.currency;
    addChatMessage('user', `I accept ${currency} ${formatKES(amount)}`);

    showTypingIndicator();
    trackEvent('offer_accepted');

    // The acceptance is only real once the backend has stored it. Confirming a
    // deal the server never recorded is how a customer ends up believing they
    // have a price nobody at GreenBay can see.
    let accepted = false;
    let failureReason = '';
    try {
        if (!state.sessionId) {
            failureReason = 'no session';
        } else {
            const resp = await fetch(`${API_BASE}/tradein/${state.sessionId}/accept-offer`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                // Send the amount actually shown to the customer; after a
                // negotiation it differs from the original opening offer.
                body: JSON.stringify({ accepted_amount: amount }),
            });
            if (resp.ok) {
                const data = await resp.json();
                if (typeof data.final_offer === 'number') amount = data.final_offer;
                accepted = true;
            } else {
                failureReason = `HTTP ${resp.status}`;
            }
        }
    } catch (err) {
        failureReason = err && err.message ? err.message : 'network error';
        console.warn('Accept offer API call failed:', err);
    }

    await sleep(500);
    hideTypingIndicator();

    if (!accepted) {
        console.error('Accept offer not recorded:', failureReason);
        addChatMessage('bot', `I couldn't confirm that with our system just now, so I won't promise you a deal that hasn't been recorded.<br><br>
 Nothing is lost. Please tap Accept again, or message us on
 <a href="https://wa.me/${MARKET.whatsapp}" target="_blank" rel="noopener">WhatsApp</a>
 and we'll complete it for you.`);
        state.negotiation.status = 'pending';
        saveState();
        return;
    }

    state.negotiation.status = 'accepted';
    addChatMessage('bot', `Deal confirmed at <strong>${currency} ${formatKES(amount)}</strong>. Next, choose how you'd like to hand it over.`);

    showPickupDropoffChoice(amount);
    saveState();
}

function dealSummary(amount, extraRows) {
    const a = state.answers;
    return resultRows([
        ['Appliance', `${escapeHtml(a.brand || '')} ${escapeHtml(a.model || CATEGORY_NAMES[a.category] || '')}`],
        ['Condition', `Grade ${escapeHtml((state.evaluation && state.evaluation.condition_grade) || '')}`],
        ['Seller', `${escapeHtml(a.sellerName)} (${escapeHtml(a.sellerPhone)})`],
        ...(extraRows || []),
        ['Reference', `GB-${escapeHtml(String(state.sessionId || '').slice(0, 8))}`],
        ['Agreed price', `<strong>${curSym()} ${formatKES(amount)}</strong>`],
    ]);
}

function showPickupDropoffChoice(amount) {
    const a = state.answers;
    setResultHeader('Deal confirmed');
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
<div class="result result--deal">
  <p class="result-eyebrow">Deal confirmed</p>
  <div class="result-amount">${curSym()} ${formatKES(amount)}</div>
  <p class="result-sub">Your ${escapeHtml(a.brand || '')} ${CATEGORY_NAMES[a.category] || ''} is accepted. How would you like to hand it over?</p>
  <div class="choice-list">
    <button type="button" class="choice-card" onclick="showPickupForm(${amount})">
      ${ICON('i-truck', 'choice-icon')}
      <span class="choice-text"><strong class="choice-title">Schedule a pickup</strong>
        <span class="choice-desc">We collect it from you. ${escapeHtml(MARKET.copy.pickupLine)}.</span></span>
    </button>
    ${MARKET.outlets.length ? `
    <button type="button" class="choice-card" onclick="showDropoffLocations(${amount})">
      ${ICON('i-box', 'choice-icon')}
      <span class="choice-text"><strong class="choice-title">Drop it at an outlet</strong>
        <span class="choice-desc">Bring it to ${MARKET.outlets.map(o => escapeHtml(o.name.replace(' Outlet', ''))).join(' or ')} and browse our stock while you're there.</span></span>
    </button>` : ''}
  </div>
  ${dealSummary(amount)}
</div>`;
}

function showPickupForm(amount) {
    setResultHeader('Schedule your pickup');
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
<div class="result result--pickup">
  <p class="result-eyebrow">Pickup</p>
  <h3 class="result-title">Where should we collect it?</h3>
  <p class="result-note">${escapeHtml(MARKET.copy.pickupLine)}. Our team confirms the exact time with you by phone.</p>
  <div class="form-group">
    <label class="form-label" for="pickupAddress">Pickup address or location</label>
    <textarea class="form-input" id="pickupAddress" rows="3" placeholder="Area, street and building, or a Google Maps link"></textarea>
  </div>
  <div class="form-group">
    <label class="form-label" for="pickupDay">Preferred day</label>
    <select class="form-input form-select" id="pickupDay">
      <option value="">Choose a day…</option>
      <option value="Today">Today</option>
      <option value="Tomorrow">Tomorrow</option>
      <option value="This Week">Later this week</option>
      <option value="Next Week">Next week</option>
    </select>
  </div>
  <div class="result-actions">
    <button type="button" class="btn btn-primary btn-block" onclick="submitPickupRequest(${amount})">Confirm pickup request</button>
    <button type="button" class="btn btn-ghost btn-block" onclick="showPickupDropoffChoice(${amount})">Back to options</button>
  </div>
</div>`;
    addChatMessage('bot', 'Add your location and a preferred day, and our team will arrange the pickup.');
}

async function submitPickupRequest(amount) {
    const address = document.getElementById('pickupAddress')?.value?.trim();
    const day = document.getElementById('pickupDay')?.value;
    const a = state.answers;

    if (!address) {
        addChatMessage('bot', 'Please add your address or location so we know where to collect it.');
        return;
    }

    // Save to backend and get the WhatsApp deep link for the pickup team
    let recorded = false;
    try {
        const resp = await fetch(`${API_BASE}/tradein/notify-pickup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                valuation_session_id: state.sessionId || null,
                seller_name: a.sellerName,
                seller_phone: a.sellerPhone,
                appliance_description: `${a.brand} ${a.model || ''} ${CATEGORY_NAMES[a.category]}`.trim(),
                condition_grade: state.evaluation?.condition_grade || 'B',
                agreed_price: amount,
                pickup_address: address,
                preferred_day: day || 'ASAP',
                photo_count: (a.photos || []).length,
            }),
        });
        if (resp.ok) {
            recorded = true;
            const data = await resp.json();
            if (data.whatsapp_link) window.open(data.whatsapp_link, '_blank');
        }
    } catch (_) { /* reported below */ }

    if (!recorded) {
        addChatMessage('bot', `I couldn't save your pickup request just now. Please try again, or send us your location on
 <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(`Hi GreenBay, pickup for deal GB-${String(state.sessionId || '').slice(0, 8)}. Address: ${address}. Day: ${day || 'ASAP'}.`)}" target="_blank" rel="noopener">WhatsApp</a>.`);
        return;
    }

    addChatMessage('bot', `Pickup request received for ${escapeHtml(address)}${day ? ', ' + escapeHtml(day.toLowerCase()) : ''}. Our team will call you on <strong>${escapeHtml(a.sellerPhone)}</strong> to confirm the time, and settles the ${curSym()} ${formatKES(amount)} with you on collection.`);
    showFinalConfirmation(amount, 'pickup', address, day);
}

function showDropoffLocations(amount) {
    const a = state.answers;
    setResultHeader('Drop-off');
    addChatMessage('bot', "Here are our outlets. Our sourcing team inspects it there and settles with you.");
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
<div class="result result--outlets">
  <p class="result-eyebrow">Drop-off</p>
  <h3 class="result-title">Our outlets</h3>
  <p class="result-note">Bring your ${escapeHtml(a.brand || '')} ${CATEGORY_NAMES[a.category] || ''} to either outlet. Our sourcing team inspects it and settles with you.</p>
  <div class="outlet-list">
    ${MARKET.outlets.map(o => `
    <div class="outlet">
      <h4>${escapeHtml(o.name)}</h4>
      <p>${escapeHtml(o.address)}</p>
      <a class="btn-link" href="${o.map}" target="_blank" rel="noopener">Open in Google Maps</a>
    </div>`).join('')}
  </div>
  ${dealSummary(amount)}
  <div class="result-actions">
    <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(`Hi GreenBay, I'll drop off my ${a.brand} ${CATEGORY_NAMES[a.category]} at your outlet. Agreed price: ${curSym()} ${amount}. Name: ${a.sellerName}. Ref: GB-${String(state.sessionId || '').slice(0, 8)}`)}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-block">${ICON('i-chat')} Tell us when you're coming</a>
    <button type="button" class="btn btn-ghost btn-block" onclick="showPickupDropoffChoice(${amount})">Back to options</button>
  </div>
</div>`;
}

function showFinalConfirmation(amount, method, address, day) {
    const a = state.answers;
    setResultHeader('All set');
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
<div class="result result--deal">
  <p class="result-eyebrow">All set</p>
  <h3 class="result-title">Your trade-in is confirmed</h3>
  <p class="result-note">${method === 'pickup' ? 'Our team will call you to confirm the pickup time.' : 'Drop it at your chosen outlet when it suits you.'}</p>
  ${dealSummary(amount, [
        method === 'pickup' ? ['Pickup location', escapeHtml(address || '')] : null,
        method === 'pickup' && day ? ['Preferred day', escapeHtml(day)] : null,
        ['Settlement', `By our sourcing team on ${method === 'pickup' ? 'collection' : 'drop-off'}`],
    ].filter(Boolean))}
  <div class="result-actions">
    <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(`Hi GreenBay, my deal reference is GB-${String(state.sessionId || '').slice(0, 8)}`)}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-block">${ICON('i-chat')} Track it on WhatsApp</a>
    <button type="button" class="btn btn-ghost btn-block" onclick="startNewEvaluation()">Start a new evaluation</button>
  </div>
  <div id="relatedProductsSection"></div>
</div>`;

    if (MARKET.code === 'KE') loadRelatedProducts(a.category);
}

async function loadRelatedProducts(category) {
    try {
        const resp = await fetch(`${API_BASE}/tradein/related-products?category=${encodeURIComponent(category || '')}&limit=6`);
        if (!resp.ok) return;
        const products = await resp.json();
        if (!products || products.length === 0) return;

        const container = document.getElementById('relatedProductsSection');
        if (!container) return;

        const cards = products.map(p => {
            const price = p.price ? `${curSym()} ${formatKES(p.price)}` : 'Contact us';
            const was = p.compare_at_price && p.price ? `<s>${curSym()} ${formatKES(p.compare_at_price)}</s>` : '';
            return `
<a class="related-card" href="${escapeHtml(p.product_url || MARKET.store)}" target="_blank" rel="noopener">
  ${p.image_url ? `<img src="${escapeHtml(p.image_url)}" alt="${escapeHtml(p.title || '')}" loading="lazy">` : ''}
  <span class="related-title">${escapeHtml(p.title || '')}</span>
  <span class="related-price">${price} ${was}</span>
</a>`;
        }).join('');

        container.innerHTML = `
<h4 class="result-subtitle">Put your trade-in towards something new</h4>
<div class="related-grid">${cards}</div>
<a class="btn-link" href="${MARKET.store}" target="_blank" rel="noopener">See all products on GreenBay</a>`;
    } catch (e) {
        console.log('Related products load failed (non-critical):', e);
    }
}

function showNoDeal(lastOffer) {
    const a = state.answers;
    setResultHeader('Your offer');
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
<div class="result result--offer">
  <p class="result-eyebrow">Offer stands for 7 days</p>
  <div class="result-amount">${curSym()} ${formatKES(lastOffer)}</div>
  <p class="result-sub">for your ${escapeHtml(a.brand || '')} ${CATEGORY_NAMES[a.category] || ''}</p>
  <div class="result-actions">
    <button type="button" class="btn btn-primary btn-block btn-accept" onclick="acceptOffer(${lastOffer})">Accept ${curSym()} ${formatKES(lastOffer)}</button>
    <a href="https://wa.me/${MARKET.whatsapp}?text=${encodeURIComponent(`Hi GreenBay, I got an offer of ${curSym()} ${lastOffer} for my ${a.brand} ${CATEGORY_NAMES[a.category]}`)}" target="_blank" rel="noopener" class="btn btn-whatsapp btn-block">${ICON('i-chat')} Talk to our team</a>
  </div>
  ${newEvaluationLink()}
</div>`;
}

/* ============================================================
 UTILITY FUNCTIONS
 ============================================================ */
function formatKES(num) {
    if (!num && num !== 0) return 'N/A';
    return Math.round(num).toLocaleString(MARKET.locale || 'en-KE');
}

// Current evaluation's display currency (e.g. KES, UGX, NGN). Always falls back
// to KES so existing single-market behaviour is unchanged.
function curSym() {
    return (state.evaluation && state.evaluation.currency_code) || MARKET.currency;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
