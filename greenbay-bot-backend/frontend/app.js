/**
 * GreenBay AI Evaluator — Web Application
 * 
 * Wizard state machine, photo upload with validation,
 * chat panel mirroring, API integration, and negotiation UI.
 */

/* ============================================================
   STATE
   ============================================================ */
const API_BASE = window.location.origin;
const TOTAL_STEPS = 9;

const state = {
    currentStep: 1,
    sessionId: null,
    answers: {
        category: null,
        brand: null,
        model: '',
        age: null,
        condition: null,
        conditionGrade: null,
        ownership: null,
        issues: '',
        photos: [],         // { file, dataUrl, id }
        price: null,
    },
    evaluation: null,
    negotiation: { round: 0, offers: [], counters: [], status: 'idle' },
    chatHistory: [],
};

// Restore from localStorage
try {
    const saved = localStorage.getItem('gb_eval_state');
    if (saved) {
        const parsed = JSON.parse(saved);
        if (parsed && parsed.currentStep) {
            Object.assign(state, parsed);
            // Photos can't survive localStorage (blobs), so reset
            state.answers.photos = [];
        }
    }
} catch (_) { /* ignore */ }

function saveState() {
    try {
        const toSave = { ...state, answers: { ...state.answers, photos: [] } };
        localStorage.setItem('gb_eval_state', JSON.stringify(toSave));
    } catch (_) { /* ignore */ }
}

/* ============================================================
   STEP LABELS & CHAT MESSAGES
   ============================================================ */
const STEP_LABELS = {
    1: 'Category',
    2: 'Brand',
    3: 'Model Number',
    4: 'Product Age',
    5: 'Condition',
    6: 'Ownership',
    7: 'Issues & Damage',
    8: 'Photos',
    9: 'Your Price',
};

const CATEGORY_NAMES = {
    refrigerator: 'Refrigerator',
    washing_machine: 'Washing Machine',
    tv_monitor: 'TV / Monitor',
    cooker_oven: 'Cooker / Oven',
    microwave: 'Microwave',
    air_conditioner: 'Air Conditioner',
    water_dispenser: 'Water Dispenser',
    other: 'Other Appliance',
};

const CONDITION_LABELS = {
    new: 'New (sealed)',
    like_new: 'Like New',
    open_box: 'Open Box',
    slightly_used: 'Slightly Used',
    used: 'Used',
    working_issues: 'Working with Issues',
    partially_working: 'Partially Working',
    not_working: 'Not Working',
};

/* ============================================================
   INITIALIZATION
   ============================================================ */
document.addEventListener('DOMContentLoaded', () => {
    // Greet in chat
    addChatMessage('bot', 'Hey! 👋 I\'m Kay, your GreenBay AI evaluator. Let\'s get your appliance valued — just follow the steps on the left, and I\'ll guide you through!');
    setTimeout(() => {
        addChatMessage('bot', 'Start by selecting what type of appliance you\'re selling. 🏷️');
    }, 800);

    // Set up photo drag & drop
    const zone = document.getElementById('photoUploadZone');
    if (zone) {
        zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
        zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
        zone.addEventListener('drop', (e) => { e.preventDefault(); zone.classList.remove('drag-over'); handlePhotoUpload(e.dataTransfer.files); });
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
}

function updateWizardUI() {
    const step = state.currentStep;
    const label = STEP_LABELS[step] || 'Results';
    const fill = step <= TOTAL_STEPS ? ((step / TOTAL_STEPS) * 100) : 100;

    document.getElementById('progressLabel').textContent = step <= TOTAL_STEPS ? `Step ${step}: ${label}` : '✅ Analysis Complete';
    document.getElementById('progressCount').textContent = step <= TOTAL_STEPS ? `${step} / ${TOTAL_STEPS}` : 'Done!';
    document.getElementById('progressFill').style.width = `${fill}%`;

    // Back button
    const prevBtn = document.getElementById('prevBtn');
    prevBtn.style.visibility = step > 1 && step <= TOTAL_STEPS ? 'visible' : 'hidden';

    // Next button
    const nextBtn = document.getElementById('nextBtn');
    if (step === TOTAL_STEPS) {
        nextBtn.textContent = '🔍 Analyze My Appliance';
        nextBtn.classList.add('btn-lg');
    } else if (step > TOTAL_STEPS) {
        nextBtn.classList.add('hidden');
    } else {
        nextBtn.textContent = 'Next →';
        nextBtn.classList.remove('btn-lg');
    }

    // Enable/disable next based on current answer
    nextBtn.disabled = !isStepValid(step);

    // Re-apply selected states for option cards (on restoration)
    restoreSelections();
}

function isStepValid(step) {
    switch (step) {
        case 1: return !!state.answers.category;
        case 2: return !!state.answers.brand;
        case 3: return state.answers.model.trim().length > 0;
        case 4: return state.answers.age !== null;
        case 5: return !!state.answers.condition;
        case 6: return !!state.answers.ownership;
        case 7: return true; // issues can be empty
        case 8: return state.answers.photos.length >= 5;
        case 9: return true; // price can be null ("make me an offer")
        default: return true;
    }
}

function nextStep() {
    if (state.currentStep === TOTAL_STEPS) {
        startAnalysis();
        return;
    }
    if (state.currentStep >= TOTAL_STEPS) return;
    if (!isStepValid(state.currentStep)) return;

    const next = state.currentStep + 1;

    // Chat mirror for previous step
    mirrorStepToChat(state.currentStep);

    goToStep(next);

    // Chat prompt for new step
    promptNextStep(next);
}

function prevStep() {
    if (state.currentStep <= 1) return;
    goToStep(state.currentStep - 1);
}

function restoreSelections() {
    // Restore option card selections
    const mappings = [
        { step: 1, field: 'category', container: 'categoryOptions' },
        { step: 2, field: 'brand', container: 'brandOptions' },
        { step: 4, field: 'age', container: 'ageOptions' },
        { step: 5, field: 'condition', container: 'conditionOptions' },
        { step: 6, field: 'ownership', container: 'ownershipOptions' },
    ];

    mappings.forEach(({ field, container }) => {
        const val = state.answers[field];
        if (!val) return;
        const el = document.getElementById(container);
        if (!el) return;
        el.querySelectorAll('.option-card').forEach(card => {
            card.classList.toggle('selected', card.dataset.value === String(val));
        });
    });
}

/* ============================================================
   OPTION SELECTION HANDLERS
   ============================================================ */
function selectOption(el, field) {
    // Deselect siblings
    el.parentElement.querySelectorAll('.option-card').forEach(c => c.classList.remove('selected'));
    el.classList.add('selected');

    const value = el.dataset.value;

    if (field === 'condition') {
        state.answers.condition = value;
        state.answers.conditionGrade = el.dataset.grade || 'B';
    } else if (field === 'age') {
        state.answers.age = parseFloat(value);
    } else {
        state.answers[field] = value;
    }

    // Enable next button
    document.getElementById('nextBtn').disabled = false;
    saveState();
}

function handleCustomBrand(value) {
    if (value.trim().length > 0) {
        // Deselect option cards
        document.querySelectorAll('#brandOptions .option-card').forEach(c => c.classList.remove('selected'));
        state.answers.brand = value.trim();
        document.getElementById('nextBtn').disabled = false;
        saveState();
    }
}

function updateAnswer(field, value) {
    state.answers[field] = value;
    document.getElementById('nextBtn').disabled = !isStepValid(state.currentStep);
    saveState();
}

function handlePriceInput(raw) {
    // Parse Kenyan price expressions: "25k" → 25000, "25,000" → 25000
    let cleaned = raw.replace(/[,\s]/g, '').toLowerCase();
    cleaned = cleaned.replace(/^kes\s*/i, '');

    if (cleaned.endsWith('k')) {
        const num = parseFloat(cleaned.slice(0, -1));
        if (!isNaN(num)) { state.answers.price = num * 1000; saveState(); return; }
    }
    const num = parseFloat(cleaned);
    state.answers.price = isNaN(num) ? null : num;
    saveState();
}

function selectCategoryFromLanding(cat) {
    state.answers.category = cat;
    // Scroll to evaluation section
    document.getElementById('evaluate').scrollIntoView({ behavior: 'smooth' });
    setTimeout(() => {
        // Select the matching option card in the wizard
        const cards = document.querySelectorAll('#categoryOptions .option-card');
        cards.forEach(c => {
            c.classList.toggle('selected', c.dataset.value === cat);
        });
        document.getElementById('nextBtn').disabled = false;
        saveState();
    }, 500);
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
            addChatMessage('bot', `That file doesn't look like an image. Please upload JPEG or PNG photos. 📸`);
            return;
        }
        if (file.size > maxSizeMB * 1024 * 1024) {
            addChatMessage('bot', `That photo is too large (${(file.size / 1024 / 1024).toFixed(1)}MB). Max 10MB please!`);
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
                document.getElementById('nextBtn').disabled = !isStepValid(8);
                saveState();

                // Chat feedback
                const count = state.answers.photos.length;
                if (count === 1) {
                    addChatMessage('bot', `Got it! 📸 That's 1/${5} photos — keep them coming!`);
                } else if (count === 3) {
                    addChatMessage('bot', `Looking good! I can already spot some details. ${count}/5 photos received. 👀`);
                } else if (count === 5) {
                    addChatMessage('bot', `All 5 photos received! ✅ Add more if you have them, or move to the next step.`);
                } else if (count > 5) {
                    addChatMessage('bot', `Nice — extra photos help me give a more accurate valuation! ${count} photos total. 📷`);
                }
            });
        };
        reader.readAsDataURL(file);
    });
}

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
       <div class="remove-photo" onclick="removePhoto('${p.id}')">✕</div>
     </div>`
    ).join('');
}

function removePhoto(id) {
    state.answers.photos = state.answers.photos.filter(p => p.id !== id);
    renderPhotoGrid();
    updatePhotoCount();
    document.getElementById('nextBtn').disabled = !isStepValid(8);
    saveState();
}

function updatePhotoCount() {
    const count = state.answers.photos.length;
    const el = document.getElementById('photoCount');
    el.innerHTML = `<span class="count-num">${count}</span> / 5 minimum photos uploaded`;
    el.classList.toggle('complete', count >= 5);
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
            addChatMessage('bot', 'Please type a price in KES (e.g. "25000" or "25k") to make a counter-offer. 💰');
        }
        return;
    }

    // Generic chat — just acknowledge
    setTimeout(() => {
        addChatMessage('bot', 'Thanks for that! Please continue filling in the form on the left — I\'m watching your progress. 😊');
    }, 600);
}

function parsePrice(text) {
    let cleaned = text.replace(/[,\s]/g, '').toLowerCase();
    cleaned = cleaned.replace(/^kes\s*/i, '');
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
    const badge = panel.querySelector('.chat-notification-badge');
    if (badge) badge.classList.remove('has-new');
}

/* ============================================================
   CHAT MIRRORING — Steps → Chat
   ============================================================ */
function mirrorStepToChat(step) {
    const a = state.answers;
    const messages = {
        1: () => a.category ? `You selected: <strong>${CATEGORY_NAMES[a.category] || a.category}</strong>` : null,
        2: () => a.brand ? `Brand: <strong>${a.brand}</strong> — nice choice! 👍` : null,
        3: () => a.model ? `Model: <strong>${a.model}</strong> — got it!` : null,
        4: () => a.age !== null ? `Age: <strong>${a.age < 1 ? 'Under 1 year' : a.age + ' years'}</strong>` : null,
        5: () => a.condition ? `Condition: <strong>${CONDITION_LABELS[a.condition] || a.condition}</strong> (Grade ${a.conditionGrade})` : null,
        6: () => a.ownership ? `Ownership: <strong>${a.ownership.replace(/_/g, ' ')}</strong>` : null,
        7: () => a.issues ? (a.issues === 'No issues'
            ? 'No issues — that\'s great! ✅'
            : `Issues noted: <em>${escapeHtml(a.issues)}</em>`) : null,
        8: () => `📸 ${a.photos.length} photos uploaded`,
        9: () => a.price ? `Your asking price: <strong>KES ${formatKES(a.price)}</strong>` : 'You\'d like us to make the first offer!',
    };

    const fn = messages[step];
    if (fn) {
        const userMsg = fn();
        if (userMsg) addChatMessage('user', userMsg);
    }
}

function promptNextStep(step) {
    const prompts = {
        2: 'Great choice! Now, what brand is your appliance? 🏷️',
        3: 'Next up — the model number. Check the sticker on the back or inside the door! 🔍',
        4: 'How old is this product? Younger appliances hold more value! 📅',
        5: 'And what condition is it in? Be honest — it helps me be accurate! 😊',
        6: 'How long have you personally owned it? This helps with provenance. 🕰️',
        7: 'Almost there! Any issues or damage I should know about? Dents, scratches, missing parts? 🔧',
        8: 'Now for the important part — photos! 📸 I need at least <strong>5 clear photos</strong>. Good lighting makes a big difference!',
        9: 'Last question — the big one! 💰 What price are you hoping for? Or let me make you an offer.',
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
    addChatMessage('bot', `Thanks for all that info! Let me analyze your <strong>${a.brand} ${a.model || CATEGORY_NAMES[a.category]}</strong>... 🔍`);

    // Animate analysis steps
    const steps = ['step-brand', 'step-condition', 'step-market', 'step-valuation'];
    const delays = [1200, 2500, 2000, 1500];

    for (let i = 0; i < steps.length; i++) {
        await sleep(delays[i]);
        const el = document.getElementById(steps[i]);
        if (el) {
            el.classList.remove('active');
            el.classList.add('complete');
            el.querySelector('.analysis-step-icon').textContent = '✅';
        }
        if (i + 1 < steps.length) {
            const next = document.getElementById(steps[i + 1]);
            if (next) next.classList.add('active');
        }

        // Chat updates
        const chatMsgs = [
            `✅ Brand verified: <strong>${a.brand}</strong>`,
            `✅ Condition: Grade <strong>${a.conditionGrade}</strong> — ${CONDITION_LABELS[a.condition]}`,
            '✅ Market data found — checking Jiji, Jumia, and Facebook Marketplace',
            '✅ Calculating your offer...',
        ];
        addChatMessage('bot', chatMsgs[i]);
    }

    // Call the backend API
    await callEvaluationAPI();
}

function buildAnalysisProgressHTML() {
    return `
    <div class="analysis-progress">
      <h3 style="margin-bottom:20px;">🔍 Analyzing Your Appliance</h3>
      <div class="analysis-step active" id="step-brand">
        <div class="analysis-step-icon">⏳</div>
        <span>Checking brand and model...</span>
      </div>
      <div class="analysis-step" id="step-condition">
        <div class="analysis-step-icon">⏳</div>
        <span>Assessing condition from photos...</span>
      </div>
      <div class="analysis-step" id="step-market">
        <div class="analysis-step-icon">⏳</div>
        <span>Searching current market prices...</span>
      </div>
      <div class="analysis-step" id="step-valuation">
        <div class="analysis-step-icon">⏳</div>
        <span>Calculating your offer...</span>
      </div>
    </div>
  `;
}

async function callEvaluationAPI() {
    const a = state.answers;

    // Map condition to score (0-100)
    const conditionScores = {
        new: 98, like_new: 90, open_box: 85,
        slightly_used: 75, used: 60,
        working_issues: 40, partially_working: 25, not_working: 10,
    };

    // Build defects from free text
    const defects = [];
    if (a.issues && a.issues !== 'No issues') {
        defects.push({ type: 'user_reported', description: a.issues, severity: 'medium' });
    }

    // Estimate retail price based on category (fallback)
    const defaultRetail = {
        refrigerator: 65000, washing_machine: 55000, tv_monitor: 45000,
        cooker_oven: 40000, microwave: 15000, air_conditioner: 50000,
        water_dispenser: 20000, other: 30000,
    };

    const payload = {
        category: a.category,
        brand: a.brand,
        model: a.model || 'Unknown',
        age_years: a.age || 0,
        condition_grade: a.conditionGrade || 'B',
        condition_score: conditionScores[a.condition] || 60,
        defects: defects,
        seller_asking_price: a.price || null,
        image_urls: [], // photos are analyzed client-side in this version
        retail_price: defaultRetail[a.category] || 35000,
        retail_price_source: 'category_default',
    };

    try {
        const resp = await fetch(`${API_BASE}/tradein/evaluate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (!resp.ok) {
            throw new Error(`API error: ${resp.status}`);
        }

        const data = await resp.json();
        state.sessionId = data.session_id;
        state.evaluation = data;
        saveState();

        await sleep(800);
        showResults(data);

    } catch (err) {
        console.error('Evaluation API error:', err);
        // Show demo results if API unavailable
        const demo = generateDemoResults(a);
        state.evaluation = demo;
        saveState();
        await sleep(800);
        showResults(demo);
        addChatMessage('bot', '<em style="opacity:.7">(Demo mode — connect to the backend API for live valuations)</em>');
    }
}

function generateDemoResults(answers) {
    // Deterministic demo calculation when backend is unavailable
    const retailPrices = {
        refrigerator: 65000, washing_machine: 55000, tv_monitor: 45000,
        cooker_oven: 40000, microwave: 15000, air_conditioner: 50000,
        water_dispenser: 20000, other: 30000,
    };
    const condMult = { A: 1.0, B: 0.85, C: 0.65, D: 0.45 };
    const brandPrem = { Samsung: 1.10, LG: 1.05, Sony: 1.08, Bosch: 1.12 };

    const retail = retailPrices[answers.category] || 35000;
    const age = answers.age || 2;
    const depr = Math.max(0.05, Math.pow(0.85, age));
    const condGrade = answers.conditionGrade || 'B';
    const base = retail * depr * (condMult[condGrade] || 0.75) * (brandPrem[answers.brand] || 1.0);
    const resale = Math.round(base / 100) * 100;
    const ceiling = Math.round(resale * 0.70 / 100) * 100;
    const opening = Math.round(resale * 0.55 / 100) * 100;
    const walkaway = Math.round(resale * 0.45 / 100) * 100;

    let decision = 'negotiate';
    if (answers.price && answers.price <= opening) decision = 'accept';
    else if (answers.price && answers.price > ceiling * 1.5) decision = 'decline';

    return {
        session_id: 'demo_' + Date.now(),
        estimated_resale_value: resale,
        confidence_score: 72,
        acquisition_ceiling: ceiling,
        opening_offer: opening,
        walkaway_limit: walkaway,
        decision: decision,
        decision_reason: 'Demo valuation based on category defaults',
        condition_grade: condGrade,
        risk_score: 15,
        comparable_count: 0,
    };
}

/* ============================================================
   RESULTS DISPLAY
   ============================================================ */
function showResults(data) {
    const a = state.answers;
    const offer = data.decision === 'accept' && a.price ? a.price : data.opening_offer;
    const gradeClass = `grade-${(data.condition_grade || 'b').toLowerCase()}`;

    // Update wizard panel with results
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
    <div class="offer-card">
      <div class="offer-card-header">🎯 Your GreenBay Valuation</div>
      <div class="offer-grade ${gradeClass}">
        ⭐ Grade ${data.condition_grade} — ${CONDITION_LABELS[a.condition] || a.condition}
      </div>
      <div class="offer-amount">KES ${formatKES(offer)}</div>
      <div class="offer-validity">Valid for 7 days</div>
      
      <div class="offer-breakdown">
        <div class="offer-breakdown-row">
          <span class="label">Estimated resale value</span>
          <span class="value">KES ${formatKES(data.estimated_resale_value)}</span>
        </div>
        <div class="offer-breakdown-row">
          <span class="label">Confidence score</span>
          <span class="value">${data.confidence_score?.toFixed(0) || '—'}%</span>
        </div>
        <div class="offer-breakdown-row">
          <span class="label">Comparable sales found</span>
          <span class="value">${data.comparable_count || 0}</span>
        </div>
      </div>
      
      <div class="offer-actions">
        <button class="btn btn-accept" onclick="acceptOffer(${offer})">✅ Accept KES ${formatKES(offer)}</button>
        <button class="btn btn-counter" onclick="showCounterUI()">💬 Counter</button>
      </div>
    </div>
    
    <div class="whatsapp-bridge">
      <div class="whatsapp-bridge-icon">💬</div>
      <div class="whatsapp-bridge-text">
        <h4>Prefer WhatsApp?</h4>
        <p>Continue this conversation on WhatsApp for a more personal experience.</p>
      </div>
      <a href="https://wa.me/254700000000?text=Hi%20GreenBay%2C%20I%20have%20a%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(a.category)}%20valued%20at%20KES%20${offer}" 
         target="_blank" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>
    </div>
  `;

    // Chat panel — show the offer
    hideTypingIndicator();

    const decisionMessages = {
        accept: `🎉 <strong>Your GreenBay Offer</strong><br><br>
      📋 ${a.brand} ${a.model || ''} ${CATEGORY_NAMES[a.category]}<br>
      ⭐ Condition: Grade ${data.condition_grade}<br>
      💰 <strong>KES ${formatKES(offer)}</strong><br><br>
      Your price works perfectly for us! We'd love to proceed. 🤝`,

        negotiate: `🎯 <strong>Your GreenBay Valuation</strong><br><br>
      📋 ${a.brand} ${a.model || ''} ${CATEGORY_NAMES[a.category]}<br>
      ⭐ Condition: Grade ${data.condition_grade}<br><br>
      ${a.price ? `I appreciate the KES ${formatKES(a.price)} ask. ` : ''}After checking current market prices and assessing the condition, I can offer:<br><br>
      💰 <strong>KES ${formatKES(data.opening_offer)}</strong><br><br>
      This factors in the current market and the condition I've assessed. Would this work for you?`,

        decline: `I appreciate you sharing your price. Based on current market data and the condition assessment, the most we could offer is <strong>KES ${formatKES(data.opening_offer)}</strong>.<br><br>
      I know there's a gap — if you'd like to reconsider, our offer stands for 7 days. 💚`,

        review: `Based on my analysis, I'd like a human team member to take a closer look at your ${a.brand} ${CATEGORY_NAMES[a.category]}. Our preliminary offer is <strong>KES ${formatKES(data.opening_offer)}</strong>, but we want to make sure we get this right! A specialist will be in touch shortly. 📞`,
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
   NEGOTIATION
   ============================================================ */
function showCounterUI() {
    const resultsStep = document.getElementById('stepResults');

    // Add counter input below existing content
    const existing = resultsStep.querySelector('.counter-input-area');
    if (existing) return; // already shown

    const counterDiv = document.createElement('div');
    counterDiv.className = 'counter-input-area';
    counterDiv.innerHTML = `
    <h4>💬 Make a Counter-Offer</h4>
    <div class="counter-row">
      <input type="text" class="form-input" id="counterInput" placeholder="e.g. 25000">
      <button class="btn btn-primary btn-sm" onclick="submitCounterFromInput()">Send</button>
    </div>
    <div class="round-tracker" id="roundTracker">
      Round <strong>${state.negotiation.round + 1}</strong> of 3
    </div>
  `;
    resultsStep.appendChild(counterDiv);

    addChatMessage('bot', 'What price did you have in mind? Type your counter-offer below. 💬');
    state.negotiation.status = 'active';

    // Focus the input
    setTimeout(() => document.getElementById('counterInput')?.focus(), 300);
}

function submitCounterFromInput() {
    const input = document.getElementById('counterInput');
    if (!input) return;
    const price = parsePrice(input.value);
    if (!price) {
        addChatMessage('bot', 'Please enter a valid price in KES (e.g. "25000" or "25k"). 💰');
        return;
    }
    input.value = '';
    submitCounterOffer(price);
}

async function submitCounterOffer(sellerCounter) {
    addChatMessage('user', `I'd like KES ${formatKES(sellerCounter)}`);
    state.negotiation.counters.push(sellerCounter);

    showTypingIndicator();
    await sleep(1500); // Simulate thinking
    hideTypingIndicator();

    // Try the backend API
    if (state.sessionId && !state.sessionId.startsWith('demo_')) {
        try {
            const resp = await fetch(`${API_BASE}/tradein/${state.sessionId}/counter`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ seller_counter: sellerCounter }),
            });

            if (resp.ok) {
                const data = await resp.json();
                handleNegotiationResponse(data);
                return;
            }
        } catch (err) {
            console.error('Counter API error:', err);
        }
    }

    // Fallback: local deterministic negotiation
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
        reason = 'Maximum negotiation rounds reached.';
    } else if (sellerCounter <= lastOffer) {
        decision = 'accept';
        systemOffer = sellerCounter;
        reason = 'Seller accepted at or below our offer.';
    } else if (sellerCounter <= ceiling) {
        decision = 'accept';
        systemOffer = sellerCounter;
        reason = 'Seller counter within acquisition ceiling.';
    } else if (sellerCounter > ceiling * 1.5) {
        decision = 'decline';
        systemOffer = lastOffer;
        reason = 'Seller price unrealistically above ceiling.';
    } else {
        decision = 'counter';
        systemOffer = Math.min(ceiling, lastOffer + (ceiling - lastOffer) * stepPct);
        systemOffer = Math.round(systemOffer / 100) * 100;
        reason = 'Counter-offer with incremental step.';
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
        neg.status = 'accepted';
        addChatMessage('bot', `KES ${formatKES(data.system_offer)} works! You've got a deal. 🤝<br><br>
      <strong>Next steps:</strong><br>
      📍 Free pickup anywhere in Nairobi<br>
      ⚡ Payment via M-Pesa — same day<br>
      📞 Our team will call within 24 hours`);
        showDealClosed(data.system_offer);

    } else if (data.decision === 'decline') {
        neg.status = 'declined';
        addChatMessage('bot', `Unfortunately we can't go above KES ${formatKES(data.system_offer)} for this unit at this time. Our offer stands for 7 days if you change your mind. 💚<br><br>
      Thanks for your time — you're welcome to come back anytime!`);
        showNoDeal(data.system_offer);

    } else {
        // Counter
        const incentive = data.rounds_remaining <= 1
            ? `<br><br>💡 <strong>Bonus:</strong> At this price, you'll get a <strong>15% trade-in discount</strong> on your next GreenBay purchase!`
            : '';

        const urgency = data.rounds_remaining === 0
            ? `<br><br>This is genuinely the maximum I can offer for this unit.`
            : '';

        addChatMessage('bot', `I understand you'd like more. The best I can do is <strong>KES ${formatKES(data.system_offer)}</strong>. ${data.reason}${incentive}${urgency}<br><br>
      What do you think?`);

        // Update round tracker
        const tracker = document.getElementById('roundTracker');
        if (tracker) {
            tracker.innerHTML = `Round <strong>${data.round_number + 1}</strong> of 3 &nbsp;•&nbsp; ${data.rounds_remaining} round${data.rounds_remaining !== 1 ? 's' : ''} remaining`;
        }

        // Update offer card
        updateOfferCard(data.system_offer, data.rounds_remaining);
    }

    saveState();
}

function updateOfferCard(newOffer, roundsRemaining) {
    const amountEl = document.querySelector('.offer-amount');
    if (amountEl) amountEl.textContent = `KES ${formatKES(newOffer)}`;

    const acceptBtn = document.querySelector('.btn-accept');
    if (acceptBtn) {
        acceptBtn.textContent = `✅ Accept KES ${formatKES(newOffer)}`;
        acceptBtn.setAttribute('onclick', `acceptOffer(${newOffer})`);
    }
}

async function acceptOffer(amount) {
    state.negotiation.status = 'accepted';
    addChatMessage('user', `I accept KES ${formatKES(amount)} ✅`);

    showTypingIndicator();
    await sleep(1000);
    hideTypingIndicator();

    addChatMessage('bot', `Wonderful! 🎉 Deal confirmed!<br><br>
    ━━━━━━━━━━━━━━━━━━━━━━<br>
    📋 <strong>ACQUISITION SUMMARY</strong><br>
    ━━━━━━━━━━━━━━━━━━━━━━<br><br>
    Appliance: ${state.answers.brand} ${state.answers.model || ''} ${CATEGORY_NAMES[state.answers.category]}<br>
    Condition: Grade ${state.evaluation.condition_grade}<br>
    Agreed Price: <strong>KES ${formatKES(amount)}</strong><br>
    Reference: GB-${state.sessionId || Date.now()}<br><br>
    📍 <strong>What happens next:</strong><br>
    1. Our team calls you within 24 hours<br>
    2. We schedule a FREE pickup at your location<br>
    3. Quick 5-minute verification at pickup<br>
    4. <strong>Same-day payment via M-Pesa</strong><br><br>
    Thanks for choosing GreenBay! 💚`);

    showDealClosed(amount);
    saveState();
}

function showDealClosed(amount) {
    const a = state.answers;
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
    <div class="deal-result">
      <div class="result-icon">🎉</div>
      <h3>Deal Confirmed!</h3>
      <p>Your ${a.brand} ${CATEGORY_NAMES[a.category]} has been accepted.</p>
      
      <div class="deal-summary">
        <div class="deal-summary-row">
          <span>Appliance</span>
          <span>${a.brand} ${a.model || CATEGORY_NAMES[a.category]}</span>
        </div>
        <div class="deal-summary-row">
          <span>Condition</span>
          <span>Grade ${state.evaluation.condition_grade}</span>
        </div>
        <div class="deal-summary-row">
          <span>Reference</span>
          <span>GB-${state.sessionId || Date.now()}</span>
        </div>
        <div class="deal-summary-row total">
          <span>Agreed Price</span>
          <span>KES ${formatKES(amount)}</span>
        </div>
      </div>
      
      <button class="btn btn-primary" onclick="resetEvaluator()">
        🔄 Evaluate Another Appliance
      </button>
      
      <div class="whatsapp-bridge" style="margin-top:20px;">
        <div class="whatsapp-bridge-icon">💬</div>
        <div class="whatsapp-bridge-text">
          <h4>Track on WhatsApp</h4>
          <p>Get pickup updates and payment confirmation via WhatsApp.</p>
        </div>
        <a href="https://wa.me/254700000000?text=Hi%20GreenBay%2C%20my%20deal%20reference%20is%20GB-${state.sessionId || ''}" 
           target="_blank" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>
      </div>
    </div>
  `;
}

function showNoDeal(lastOffer) {
    const a = state.answers;
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
    <div class="deal-result">
      <div class="result-icon">💚</div>
      <h3>Offer Stands for 7 Days</h3>
      <p>Our offer of <strong>KES ${formatKES(lastOffer)}</strong> for your ${a.brand} ${CATEGORY_NAMES[a.category]} is still available.</p>
      
      <div style="display:flex;gap:12px;justify-content:center;margin-top:20px;">
        <button class="btn btn-primary" onclick="acceptOffer(${lastOffer})">Accept KES ${formatKES(lastOffer)}</button>
        <button class="btn btn-secondary" onclick="resetEvaluator()">New Valuation</button>
      </div>
      
      <div class="whatsapp-bridge" style="margin-top:20px;">
        <div class="whatsapp-bridge-icon">💬</div>
        <div class="whatsapp-bridge-text">
          <h4>Continue on WhatsApp</h4>
          <p>Chat with our team for a more personal experience.</p>
        </div>
        <a href="https://wa.me/254700000000?text=Hi%20GreenBay%2C%20I%20got%20an%20offer%20of%20KES%20${lastOffer}%20for%20my%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(a.category)}" 
           target="_blank" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>
      </div>
    </div>
  `;
}

function resetEvaluator() {
    // Clear state
    state.currentStep = 1;
    state.sessionId = null;
    state.answers = {
        category: null, brand: null, model: '', age: null,
        condition: null, conditionGrade: null, ownership: null,
        issues: '', photos: [], price: null,
    };
    state.evaluation = null;
    state.negotiation = { round: 0, offers: [], counters: [], status: 'idle' };
    state.chatHistory = [];

    localStorage.removeItem('gb_eval_state');

    // Reset UI
    document.querySelectorAll('.option-card.selected').forEach(c => c.classList.remove('selected'));
    document.getElementById('modelInput').value = '';
    document.getElementById('issuesInput').value = '';
    document.getElementById('priceInput').value = '';
    document.getElementById('brandCustom').value = '';
    document.getElementById('photoGrid').innerHTML = '';
    updatePhotoCount();

    // Reset chat
    document.getElementById('chatMessages').innerHTML = '';

    // Show wizard footer
    document.getElementById('wizardFooter').classList.remove('hidden');

    goToStep(1);
    addChatMessage('bot', 'Let\'s start fresh! 🌟 What type of appliance are you selling?');
}

/* ============================================================
   UTILITY FUNCTIONS
   ============================================================ */
function formatKES(num) {
    if (!num && num !== 0) return '—';
    return Math.round(num).toLocaleString('en-KE');
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
