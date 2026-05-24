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
const TOTAL_STEPS = 11;

// v6: Detect country via timezone heuristic (no permissions needed)
(function detectCountry() {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    if (tz.startsWith('Africa/Nairobi')) window.__gbCountry = 'KE';
    else if (tz.startsWith('Africa/Kampala')) window.__gbCountry = 'UG';
    else if (tz.startsWith('Africa/Lagos')) window.__gbCountry = 'NG';
    else window.__gbCountry = 'KE';
})();

const state = {
    currentStep: 1,
    sessionId: null,
    answers: {
        category: null,
        brand: null,
        model: '',
        modelPhoto: null, // { file, dataUrl }
        age: null,
        condition: null,
        conditionGrade: null,
        ownership: null,
        issues: '',
        otherDescription: '',
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

// Restore from localStorage — auto-reset if previous evaluation was complete
try {
    const saved = localStorage.getItem('gb_eval_state');
    if (saved) {
        const parsed = JSON.parse(saved);
        if (parsed && parsed.currentStep) {
            // If the previous session was completed OR has evaluation results,
            // start fresh. Results can't survive page reload (HTML isn't persisted).
            if (parsed.currentStep > TOTAL_STEPS || parsed.evaluation) {
                localStorage.removeItem('gb_eval_state');
            } else {
                Object.assign(state, parsed);
                // Photos can't survive localStorage (blobs), so reset
                state.answers.photos = [];
                state.answers.modelPhoto = null;
            }
        }
    }
} catch (_) { /* ignore */ }

function saveState() {
    try {
        // Don't persist completed evaluations — they'll reset on reload
        if (state.currentStep > TOTAL_STEPS) return;
        const toSave = { ...state, answers: { ...state.answers, photos: [], modelPhoto: null, video: null } };
        localStorage.setItem('gb_eval_state', JSON.stringify(toSave));
    } catch (_) { /* ignore */ }
}

/**
 * Hard reset: wipe all client-side state and do a full page reload for a
 * guaranteed clean UI. Used by every "Start New Evaluation" button across
 * the result screens.
 */
function startNewEvaluation() {
    try { localStorage.removeItem('gb_eval_state'); } catch (_) { /* ignore */ }
    // Preserve any access key or query params that should persist across reloads.
    window.location.href = window.location.pathname + window.location.hash;
}
window.startNewEvaluation = startNewEvaluation;

function resetWizard() {
    // Clear all state
    state.currentStep = 1;
    state.sessionId = null;
    state.answers = {
        category: null, brand: null, model: '', modelPhoto: null,
        age: null, condition: null, conditionGrade: null,
        ownership: null, issues: '', otherDescription: '',
        sellerName: '', sellerPhone: '',
        photos: [], price: null,
    };
    state.evaluation = null;
    state.negotiation = { round: 0, offers: [], counters: [], status: 'idle' };
    state.chatHistory = [];

    // Clear localStorage
    localStorage.removeItem('gb_eval_state');

    // Reset UI: clear selections, photos, inputs
    document.querySelectorAll('.option-card.selected').forEach(
        el => el.classList.remove('selected')
    );
    document.querySelectorAll('.wizard-step input, .wizard-step textarea').forEach(
        el => { el.value = ''; }
    );
    const gallery = document.getElementById('photoGallery');
    if (gallery) gallery.innerHTML = '';
    const modelPreview = document.getElementById('modelPhotoPreview');
    if (modelPreview) modelPreview.innerHTML = '';

    // Reset result panels
    const resultPanel = document.getElementById('resultPanel');
    if (resultPanel) resultPanel.classList.add('hidden');
    const negotiationPanel = document.getElementById('negotiationPanel');
    if (negotiationPanel) negotiationPanel.classList.add('hidden');

    // Show next button again
    const nextBtn = document.getElementById('nextBtn');
    if (nextBtn) {
        nextBtn.classList.remove('hidden');
        nextBtn.textContent = 'Next ';
        nextBtn.classList.remove('btn-lg');
    }

    // Navigate to step 1
    goToStep(1);
}

/* ============================================================
 STEP LABELS & CHAT MESSAGES
 ============================================================ */
const STEP_LABELS = {
    1: 'Category',
    2: 'Brand',
    3: 'Model Number',
    4: 'Model Label Photo',
    5: 'Product Age',
    6: 'Condition',
    7: 'Ownership',
    8: 'Issues & Damage',
    9: 'Your Details',
    10: 'Photos',
    11: 'Your Price',
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
    addChatMessage('bot', 'Hey! I\'m Kay, your GreenBay AI evaluator. Let\'s get your appliance valued , just follow the steps on the left, and I\'ll guide you through!');
    setTimeout(() => {
        addChatMessage('bot', 'Start by selecting what type of appliance you\'re selling. ');
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

    document.getElementById('progressLabel').textContent = step <= TOTAL_STEPS ? `Step ${step}: ${label}` : ' Analysis Complete';
    document.getElementById('progressCount').textContent = step <= TOTAL_STEPS ? `${step} / ${TOTAL_STEPS}` : 'Done!';
    document.getElementById('progressFill').style.width = `${fill}%`;

    // Back button
    const prevBtn = document.getElementById('prevBtn');
    prevBtn.style.visibility = step > 1 && step <= TOTAL_STEPS ? 'visible' : 'hidden';

    // Next button
    const nextBtn = document.getElementById('nextBtn');
    if (step === TOTAL_STEPS) {
        nextBtn.textContent = ' Analyze My Appliance';
        nextBtn.classList.add('btn-lg');
    } else if (step > TOTAL_STEPS) {
        nextBtn.classList.add('hidden');
    } else {
        nextBtn.textContent = 'Next ';
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
        case 3: return true; // model is now optional
        case 4: return true; // model photo is now optional
        case 5: return state.answers.age !== null;
        case 6: return !!state.answers.condition;
        case 7: return !!state.answers.ownership;
        case 8: return true; // issues can be empty
        case 9: return state.answers.sellerName.trim().length > 0 && state.answers.sellerPhone.trim().length >= 9;
        case 10: return state.answers.photos.length >= 3;
        case 11: return true; // price can be null ("make me an offer")
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
    // Parse Kenyan price expressions: "25k" 25000, "25,000" 25000
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
 MODEL LABEL PHOTO UPLOAD
 ============================================================ */
function handleModelPhotoUpload(files) {
    const file = files[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
        addChatMessage('bot', 'That file does not look like a photo. Please upload a JPEG or PNG image of the model label.');
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        addChatMessage('bot', `That photo is too large (${(file.size / 1024 / 1024).toFixed(1)}MB). Max 10MB please!`);
        return;
    }

    const reader = new FileReader();
    reader.onload = (e) => {
        compressImage(e.target.result, 1024, 0.85, (compressedDataUrl) => {
            state.answers.modelPhoto = { file, dataUrl: compressedDataUrl };

            // Show preview
            const placeholder = document.getElementById('modelPhotoPlaceholder');
            const preview = document.getElementById('modelPhotoPreview');
            const img = document.getElementById('modelPhotoImg');
            if (placeholder) placeholder.style.display = 'none';
            if (preview) { preview.style.display = 'block'; }
            if (img) img.src = compressedDataUrl;

            document.getElementById('nextBtn').disabled = false;
            saveState();

            // Chat feedback
            addChatMessage('bot', 'Model label photo received! I can see the model details. This will help me look up exact specifications and pricing data.');

            // Send to backend for OCR/verification (non-blocking)
            lookupModelFromPhoto(compressedDataUrl);
        });
    };
    reader.readAsDataURL(file);
}

async function lookupModelFromPhoto(dataUrl) {
    try {
        const response = await fetch(`${API_BASE}/api/evaluator/model-lookup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                image_data: dataUrl,
                typed_model: state.answers.model,
                category: state.answers.category,
                brand: state.answers.brand,
            }),
        });

        if (response.ok) {
            const result = await response.json();
            if (result.model_verified) {
                addChatMessage('bot', `Model <strong>${result.model_number || state.answers.model}</strong> verified! ${result.specs_summary || ''}`);
                // Update model if OCR found a better match
                if (result.model_number && result.model_number !== state.answers.model) {
                    state.answers.model = result.model_number;
                    const modelInput = document.getElementById('modelInput');
                    if (modelInput) modelInput.value = result.model_number;
                    saveState();
                }
            }
        }
    } catch (_) {
        // Non-critical: model lookup is nice-to-have
    }
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
                document.getElementById('nextBtn').disabled = !isStepValid(10);
                saveState();

                // Chat feedback
                const count = state.answers.photos.length;
                if (count === 1) {
                    addChatMessage('bot', `Got it! That's 1 photo — keep them coming! At least 3 needed.`);
                } else if (count === 2) {
                    addChatMessage('bot', `Good, ${count} photos received. One more and you can proceed!`);
                } else if (count === 3) {
                    addChatMessage('bot', `${count} photos received — you can proceed now! For the best valuation, 5 HD photos is ideal.`);
                } else if (count === 5) {
                    addChatMessage('bot', `All 5 photos received — perfect for an accurate valuation! Add more or move to the next step.`);
                } else if (count > 5) {
                    addChatMessage('bot', `Nice, extra photos help me give a more accurate valuation! ${count} photos total.`);
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
        show(`<span style="color:var(--green-primary);font-weight:600;">✓ Video uploaded</span> <span style="color:var(--muted);">(${subtitle})</span>${noteHtml}`);
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
    document.getElementById('nextBtn').disabled = !isStepValid(10);
    saveState();
}

function updatePhotoCount() {
    const count = state.answers.photos.length;
    const el = document.getElementById('photoCount');
    el.innerHTML = `<span class="count-num">${count}</span> / 3 minimum photos (5 HD ideal)`;
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
            addChatMessage('bot', 'Please type a price in KES (e.g. "25000" or "25k") to make a counter-offer. ');
        }
        return;
    }

    // Generic chat , just acknowledge
    setTimeout(() => {
        addChatMessage('bot', 'Thanks for that! Please continue filling in the form on the left , I\'m watching your progress. ');
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
 CHAT MIRRORING , Steps Chat
 ============================================================ */
function mirrorStepToChat(step) {
    const a = state.answers;
    const messages = {
        1: () => a.category ? `You selected: <strong>${CATEGORY_NAMES[a.category] || a.category}</strong>${a.category === 'other' && a.otherDescription ? ' (' + escapeHtml(a.otherDescription) + ')' : ''}` : null,
        2: () => a.brand ? `Brand: <strong>${a.brand}</strong>, nice choice!` : null,
        3: () => a.model ? `Model: <strong>${a.model}</strong>, got it!` : 'Model number skipped',
        4: () => a.modelPhoto ? 'Model label photo uploaded, verifying...' : 'Model photo skipped',
        5: () => a.age !== null ? `Age: <strong>${a.age < 1 ? 'Under 1 year' : a.age + ' years'}</strong>` : null,
        6: () => a.condition ? `Condition: <strong>${CONDITION_LABELS[a.condition] || a.condition}</strong> (Grade ${a.conditionGrade})` : null,
        7: () => a.ownership ? `Ownership: <strong>${a.ownership.replace(/_/g, ' ')}</strong>` : null,
        8: () => a.issues ? (a.issues === 'No issues'
            ? 'No issues, that\'s great!'
            : `Issues noted: <em>${escapeHtml(a.issues)}</em>`) : null,
        9: () => a.sellerName ? `Contact: <strong>${escapeHtml(a.sellerName)}</strong> (${escapeHtml(a.sellerPhone)})` : null,
        10: () => `${a.photos.length} photos uploaded`,
        11: () => a.price ? `Your asking price: <strong>KES ${formatKES(a.price)}</strong>` : 'You\'d like us to make the first offer!',
    };

    const fn = messages[step];
    if (fn) {
        const userMsg = fn();
        if (userMsg) addChatMessage('user', userMsg);
    }
}

function promptNextStep(step) {
    const prompts = {
        2: 'Great choice! Now, what brand is your appliance?',
        3: 'Next up, the model number. This is optional — skip it if you\'re not sure!',
        4: 'Upload a photo of the model label if you have it. This is optional but helps me look up exact specs!',
        5: 'How old is this product? Younger appliances hold more value!',
        6: 'And what condition is it in? Be honest, it helps me be accurate!',
        7: 'How long have you personally owned it? This helps with provenance.',
        8: 'Almost there! Any issues or damage I should know about? Dents, scratches, missing parts?',
        9: 'I need your name and phone number so we can reach you about pickup or drop-off.',
        10: 'Now for the important part — photos! I need at least <strong>3 clear photos</strong> (5 HD photos is ideal for the most accurate valuation). Good lighting makes a big difference!',
        11: 'Last question! What price are you hoping for? This is optional — you can let me make the first offer.',
    };

    // Show/hide Other description field based on category
    if (step === 3) {
        const descGroup = document.getElementById('otherDescriptionGroup');
        if (descGroup) {
            descGroup.style.display = state.answers.category === 'other' ? 'block' : 'none';
        }
    }

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
    addChatMessage('bot', `Thanks for all that info! Let me analyze your <strong>${a.brand} ${a.model || CATEGORY_NAMES[a.category]}</strong>... `);

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

        // Chat updates
        const chatMsgs = [
            ` Brand verified: <strong>${a.brand}</strong>`,
            ` Condition: Grade <strong>${a.conditionGrade}</strong> , ${CONDITION_LABELS[a.condition]}`,
            ' Finding you the best offer price based on current market data',
            ' Calculating your offer...',
        ];
        addChatMessage('bot', chatMsgs[i]);
    }

    // Call the backend API
    await callEvaluationAPI();
}

function buildAnalysisProgressHTML() {
    return `
 <div class="analysis-progress">
 <h3 style="margin-bottom:20px;"> Analyzing Your Appliance</h3>
 <div class="analysis-step active" id="step-brand">
 <div class="analysis-step-icon"></div>
 <span>Checking brand and model...</span>
 </div>
 <div class="analysis-step" id="step-condition">
 <div class="analysis-step-icon"></div>
 <span>Assessing condition from photos...</span>
 </div>
 <div class="analysis-step" id="step-market">
 <div class="analysis-step-icon"></div>
 <span>Searching current market prices...</span>
 </div>
 <div class="analysis-step" id="step-valuation">
 <div class="analysis-step-icon"></div>
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
        brand: a.brand,
        model: a.model || 'Unknown',
        age_years: a.age || 0,
        condition_grade: a.conditionGrade || 'B',
        condition_score: conditionScores[a.condition] || 60,
        defects: defects,
        seller_asking_price: a.price || null,
        image_urls: [],
        image_data: combinedImageData,
        retail_price: defaultRetail[a.category] || 35000,
        retail_price_source: 'category_default',
        country: window.__gbCountry || 'KE',
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
        addChatMessage('bot', '<em style="opacity:.7">(Demo mode , connect to the backend API for live valuations)</em>');
    }
}

function generateDemoResults(answers) {
    // Deterministic demo calculation when backend is unavailable
    const retailPrices = {
        refrigerator: 65000, washing_machine: 55000, tv_monitor: 45000,
        cooker_oven: 40000, microwave: 15000, small_kitchen: 12000,
        other: 30000,
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
    const confidence = data.confidence_score || 0;

    // HARD REJECT — product doesn't meet quality standards
    if (data.decision === 'reject') {
        const resultsStep = document.getElementById('stepResults');
        resultsStep.innerHTML = `
 <div class="offer-card">
 <div class="offer-card-header" style="background:linear-gradient(135deg,#dc3545,#c82333);">❌ Trade-In Not Eligible</div>
 <div class="offer-grade grade-d">
 Grade ${data.condition_grade} — ${CONDITION_LABELS[a.condition] || a.condition}
 </div>
 <p style="margin:16px 0;color:var(--muted);font-size:.92rem;">
 Unfortunately, your <strong>${a.brand} ${CATEGORY_NAMES[a.category]}</strong> does not meet our minimum quality standards for trade-in.
 </p>
 <div class="offer-breakdown">
 <div class="offer-breakdown-row">
 <span class="label">Reason</span>
 <span class="value" style="color:#dc3545;">${data.decision_reason || 'Below quality threshold'}</span>
 </div>
 </div>
 <div style="background:var(--warm);border-radius:var(--radius-sm);padding:16px;margin:20px 0;">
 <p style="font-size:.88rem;margin:0;"><strong>💡 What you can do:</strong><br>
 • Get the item repaired and resubmit<br>
 • Speak to our team for recycling options<br>
 • Browse our marketplace for affordable replacements</p>
 </div>
 <div style="display:grid;gap:8px;">
 <a href="https://wa.me/254705919099?text=Hi%20GreenBay%2C%20my%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(CATEGORY_NAMES[a.category])}%20was%20rejected%20for%20trade-in.%20Can%20you%20help%3F" 
 target="_blank" class="btn btn-whatsapp" style="width:100%;">
 <i data-lucide="message-circle" style="width:18px;height:18px;"></i>
 Speak to Our Team
 </a>
 <a href="https://greenbay.market" target="_blank" class="btn btn-primary" style="width:100%;">
 🛒 Browse GreenBay Marketplace
 </a>
<button class="btn btn-outline" onclick="startNewEvaluation()" style="width:100%;">
<i data-lucide="rotate-ccw" style="width:16px;height:16px;margin-right:6px;"></i>
Start New Evaluation
</button>
</div>
</div>
`;
        addChatMessage('bot', `I'm sorry, but your ${a.brand} ${CATEGORY_NAMES[a.category]} doesn't meet our minimum standards for trade-in. ${data.decision_reason || ''} You can speak to our team for other options.`);
        lucide.createIcons();
        return;
    }

    // v6: AUTO-REDIRECT to specialist if confidence < 80% OR flagged for review
    const currency = data.currency_code || 'KES';
    if (confidence < 80 || data.decision === 'review') {
        const reviewMessage = data.customer_message ||
            `Our AI's confidence on this valuation is below 80%. For the most accurate offer, we're connecting you with one of our trade-in specialists who can assess your ${a.brand} ${CATEGORY_NAMES[a.category]} personally.`;
        const resultsStep = document.getElementById('stepResults');
        resultsStep.innerHTML = `
 <div class="offer-card">
 <div class="offer-card-header">🤝 Connecting You With a Specialist</div>
 <div class="offer-grade ${gradeClass}">
 Grade ${data.condition_grade} — ${CONDITION_LABELS[a.condition] || a.condition}
 </div>
 <div class="offer-amount" style="font-size:1.2rem;color:var(--gold);">Confidence: ${confidence.toFixed(0)}%</div>
 <p style="margin:16px 0;color:var(--muted);font-size:.92rem;">
 ${reviewMessage}
 </p>
 <div class="offer-breakdown">
 <div class="offer-breakdown-row">
 <span class="label">Preliminary estimate</span>
 <span class="value">${currency} ${formatKES(offer)}</span>
 </div>
 <div class="offer-breakdown-row">
 <span class="label">Confidence score</span>
 <span class="value">${confidence.toFixed(0)}%</span>
 </div>
 </div>
 <div style="margin-top:20px;">
 <a href="https://wa.me/254705919099?text=Hi%20GreenBay%2C%20I%20have%20a%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(CATEGORY_NAMES[a.category])}%20for%20trade-in.%20AI%20estimate%3A%20${currency}%20${offer}%20(${confidence.toFixed(0)}%25%20confidence).%20Name%3A%20${encodeURIComponent(a.sellerName)}.%20Phone%3A%20${encodeURIComponent(a.sellerPhone)}" 
 target="_blank" class="btn btn-whatsapp btn-lg" style="width:100%;">
 <i data-lucide="message-circle" style="width:18px;height:18px;"></i>
 Speak to a Trade-In Specialist
 </a>
 </div>

 <div style="margin-top:1.5rem; padding:16px; background:linear-gradient(135deg, rgba(13,159,79,0.08), rgba(6,78,59,0.05)); border-radius:var(--radius-sm); border:1px solid rgba(13,159,79,0.2);">
 <div style="font-weight:700; margin-bottom:8px; color:var(--forest); font-size:1rem;">🧑‍💼 Expert Pricing</div>
 <p style="font-size:.85rem; color:var(--muted); margin-bottom:12px;">Are you an experienced sales agent? Submit your assessed price to help train the AI for better future valuations.</p>
 <div id="expertFeedbackForm">
 <input type="text" id="expertNameInput" class="form-input" placeholder="Your name" style="margin-bottom:8px; font-size:.88rem; padding:10px 12px;">
 <input type="number" id="expertPriceInput" class="form-input" placeholder="Your assessed price (KES)" style="margin-bottom:8px; font-size:.88rem; padding:10px 12px;">
 <textarea id="expertReasonInput" class="form-input" placeholder="Why this price? (optional)" style="margin-bottom:10px; font-size:.85rem; padding:10px 12px; min-height:55px;"></textarea>
 <button class="btn btn-primary btn-sm" onclick="submitExpertFeedback()" style="width:100%; padding:10px;">
 ✅ Submit Expert Price
 </button>
 </div>
 <div id="expertFeedbackSuccess" style="display:none; text-align:center; padding:12px; color:var(--green-primary); font-weight:600;">
 ✅ Thank you! Your expertise has been recorded and will improve future valuations.
 </div>
 </div>

<div style="text-align:center; margin-top:1.2rem;">
<button class="btn btn-outline" onclick="startNewEvaluation()" style="width:100%;">
<i data-lucide="rotate-ccw" style="width:16px;height:16px;margin-right:6px;"></i>
Start New Evaluation
</button>
</div>
</div>
`;
        addChatMessage('bot', `I'd like an expert to take a closer look at your ${a.brand} ${CATEGORY_NAMES[a.category]}. The preliminary estimate is KES ${formatKES(offer)}, but our team can give you a more precise valuation. Click the WhatsApp button to connect with them!`);
        lucide.createIcons();
        return;
    }

    // Normal results display (confidence >= 85%)
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
 <div class="offer-card">
 <div class="offer-card-header">✅ Your GreenBay Valuation</div>
 <div class="offer-grade ${gradeClass}">
 Grade ${data.condition_grade} — ${CONDITION_LABELS[a.condition] || a.condition}
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
 <span class="value">${confidence.toFixed(0)}%</span>
 </div>
 <div class="offer-breakdown-row">
 <span class="label">Comparable sales found</span>
 <span class="value">${data.comparable_count || 0}</span>
 </div>
 </div>
 
 <div class="offer-actions">
 <button class="btn btn-accept" onclick="acceptOffer(${offer})">✅ Accept ${currency} ${formatKES(offer)}</button>
 <button class="btn btn-counter" onclick="showRejectionOptions()">💬 Not Happy With Price?</button>
 </div>
 </div>
 
 <div class="whatsapp-bridge">
 <div class="whatsapp-bridge-icon">💬</div>
 <div class="whatsapp-bridge-text">
 <h4>Prefer WhatsApp?</h4>
 <p>Continue this conversation on WhatsApp for a more personal experience.</p>
 </div>
 <a href="https://wa.me/254705919099?text=Hi%20GreenBay%2C%20I%20have%20a%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(CATEGORY_NAMES[a.category])}%20valued%20at%20KES%20${offer}.%20Name%3A%20${encodeURIComponent(a.sellerName)}.%20Phone%3A%20${encodeURIComponent(a.sellerPhone)}" 
 target="_blank" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>
 </div>

 ${data.price_verification ? `
 <div class="offer-breakdown" style="margin-top:1rem;">
 <div style="font-weight:600; margin-bottom:8px; color:var(--forest);">📊 Price Verification (${data.price_verification.num_sources || 0} sources)</div>
 ${(data.price_verification.sources || []).map(s => `
 <div class="offer-breakdown-row">
 <span class="label">${s.source.replace(/_/g, ' ')}</span>
 <span class="value">KES ${formatKES(s.price)}</span>
 </div>
 `).join('')}
 <div class="offer-breakdown-row" style="border-top: 1px solid var(--border); padding-top: 6px; margin-top: 6px;">
 <span class="label" style="font-weight:600;">Reconciled Price</span>
 <span class="value" style="font-weight:600;">KES ${formatKES(data.price_verification.reconciled_price)}</span>
 </div>
 </div>
 ` : ''}

 <div style="margin-top:1rem; padding:12px; background:rgba(13,159,79,.06); border-radius:var(--radius-sm); border:1px solid rgba(13,159,79,.15);">
 <div style="font-weight:600; margin-bottom:6px; color:var(--forest);">🧑‍💼 Expert Pricing (Pilot)</div>
 <p style="font-size:.82rem; color:var(--muted); margin-bottom:10px;">Are you an experienced sales agent? Help train the AI by sharing what you would price this product at.</p>
 <div id="expertFeedbackForm">
 <input type="text" id="expertNameInput" class="form-input" placeholder="Your name" style="margin-bottom:8px; font-size:.85rem; padding:8px 10px;">
 <input type="number" id="expertPriceInput" class="form-input" placeholder="Your assessed price (KES)" style="margin-bottom:8px; font-size:.85rem; padding:8px 10px;">
 <textarea id="expertReasonInput" class="form-input" placeholder="Why this price? (optional)" style="margin-bottom:8px; font-size:.85rem; padding:8px 10px; min-height:50px;"></textarea>
 <button class="btn btn-secondary btn-sm" onclick="submitExpertFeedback()" style="width:100%;">
 Submit Expert Price
 </button>
 </div>
 <div id="expertFeedbackSuccess" style="display:none; text-align:center; padding:10px; color:var(--green-primary);">
 ✅ Thank you! Your expertise has been recorded.
 </div>
 </div>

<div style="text-align:center; margin-top:1.5rem; padding-top:1rem; border-top:1px solid var(--border);">
<button class="btn btn-outline" onclick="startNewEvaluation()" style="width:100%;">
<i data-lucide="rotate-ccw" style="width:16px;height:16px;margin-right:6px;"></i>
Start New Evaluation
</button>
</div>
`;

    // Chat panel , show the offer
    hideTypingIndicator();

    const decisionMessages = {
        accept: ` <strong>Your GreenBay Offer</strong><br><br>
 ${a.brand} ${a.model || ''} ${CATEGORY_NAMES[a.category]}<br>
 Condition: Grade ${data.condition_grade}<br>
 <strong>KES ${formatKES(offer)}</strong><br><br>
 Your price works perfectly for us! We'd love to proceed. `,

        negotiate: ` <strong>Your GreenBay Valuation</strong><br><br>
 ${a.brand} ${a.model || ''} ${CATEGORY_NAMES[a.category]}<br>
 Condition: Grade ${data.condition_grade}<br><br>
 ${a.price ? `I appreciate the KES ${formatKES(a.price)} ask. ` : ''}After checking current market prices and assessing the condition, I can offer:<br><br>
 <strong>KES ${formatKES(data.opening_offer)}</strong><br><br>
 This factors in the current market and the condition I've assessed. Would this work for you?`,

        decline: `I appreciate you sharing your price. Based on current market data and the condition assessment, the most we could offer is <strong>KES ${formatKES(data.opening_offer)}</strong>.<br><br>
 I know there's a gap , if you'd like to reconsider, our offer stands for 7 days. `,

        review: `Based on my analysis, I'd like a human team member to take a closer look at your ${a.brand} ${CATEGORY_NAMES[a.category]}. Our preliminary offer is <strong>KES ${formatKES(data.opening_offer)}</strong>, but we want to make sure we get this right! A specialist will be in touch shortly. `,
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
 EXPERT FEEDBACK
 ============================================================ */
function submitExpertFeedback() {
    const name = document.getElementById('expertNameInput')?.value?.trim();
    const price = parseFloat(document.getElementById('expertPriceInput')?.value);
    const reasoning = document.getElementById('expertReasonInput')?.value?.trim();

    if (!name) { alert('Please enter your name'); return; }
    if (!price || price <= 0) { alert('Please enter a valid price'); return; }
    if (!state.sessionId) { alert('No session ID available'); return; }

    const btn = document.querySelector('#expertFeedbackForm button');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting...'; }

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
    .then(r => r.json())
    .then(data => {
        const form = document.getElementById('expertFeedbackForm');
        const success = document.getElementById('expertFeedbackSuccess');
        if (form) form.style.display = 'none';
        if (success) {
            success.style.display = 'block';
            const diff = data.price_difference;
            if (diff !== null && diff !== undefined) {
                const dir = diff > 0 ? 'higher' : 'lower';
                success.innerHTML = `✅ Recorded! Your price is KES ${formatKES(Math.abs(diff))} ${dir} than the AI's offer.`;
            }
        }
        addChatMessage('bot', `Expert feedback recorded — thank you, ${name}! 🎯`);
    })
    .catch(err => {
        console.error('Expert feedback error:', err);
        if (btn) { btn.disabled = false; btn.textContent = 'Submit Expert Price'; }
        alert('Failed to submit feedback. Please try again.');
    });
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
 <h4> Make a Counter-Offer</h4>
 <div class="counter-row">
 <input type="text" class="form-input" id="counterInput" placeholder="e.g. 25000">
 <button class="btn btn-primary btn-sm" onclick="submitCounterFromInput()">Send</button>
 </div>
 <div class="round-tracker" id="roundTracker">
 Round <strong>${state.negotiation.round + 1}</strong> of 3
 </div>
 `;
    resultsStep.appendChild(counterDiv);

    addChatMessage('bot', 'What price did you have in mind? Type your counter-offer below. ');
    state.negotiation.status = 'active';

    // Focus the input
    setTimeout(() => document.getElementById('counterInput')?.focus(), 300);
}

// v6 WS8: Show three rejection option cards
function showRejectionOptions() {
    const a = state.answers;
    const data = state.evaluation || {};
    const offer = data.opening_offer || 0;
    const currency = data.currency_code || 'KES';
    const upfront = Math.round(offer * 0.10 / 100) * 100;
    const balance = Math.round(offer * 0.90 / 100) * 100;

    const resultsStep = document.getElementById('stepResults');
    const existing = resultsStep.querySelector('.rejection-options-area');
    if (existing) return;

    const optionsDiv = document.createElement('div');
    optionsDiv.className = 'rejection-options-area';
    optionsDiv.innerHTML = `
 <h4 style="margin-bottom:16px;">Not happy with the price? Here are your options:</h4>
 <div style="display:grid;gap:14px;">

 <div class="option-card" style="cursor:pointer;padding:18px;text-align:left;border:2px solid var(--border);border-radius:var(--radius-sm);transition:border-color .2s;" 
      onclick="selectRejectionOption('A')" onmouseover="this.style.borderColor='var(--green-primary)'" onmouseout="this.style.borderColor='var(--border)'">
   <div style="display:flex;align-items:flex-start;gap:12px;">
     <span style="font-size:1.8rem;">🏷️</span>
     <div>
       <strong style="display:block;font-size:1rem;">Option A — Consignment</strong>
       <span style="color:var(--muted);font-size:.85rem;">List your ${a.brand} ${CATEGORY_NAMES[a.category] || a.category} at your preferred price on our marketplace. We handle everything — listing, marketing, and customer inquiries.</span>
     </div>
   </div>
 </div>

 <div class="option-card" style="cursor:pointer;padding:18px;text-align:left;border:2px solid var(--border);border-radius:var(--radius-sm);transition:border-color .2s;"
      onclick="selectRejectionOption('B')" onmouseover="this.style.borderColor='var(--green-primary)'" onmouseout="this.style.borderColor='var(--border)'">
   <div style="display:flex;align-items:flex-start;gap:12px;">
     <span style="font-size:1.8rem;">💰</span>
     <div>
       <strong style="display:block;font-size:1rem;">Option B — 10/90 Split</strong>
       <span style="color:var(--muted);font-size:.85rem;">Get <strong>${currency} ${formatKES(upfront)}</strong> upfront today, then <strong>${currency} ${formatKES(balance)}</strong> when it sells (within 90 days).</span>
     </div>
   </div>
 </div>

 <div class="option-card" style="cursor:pointer;padding:18px;text-align:left;border:2px solid var(--border);border-radius:var(--radius-sm);transition:border-color .2s;"
      onclick="selectRejectionOption('C')" onmouseover="this.style.borderColor='var(--green-primary)'" onmouseout="this.style.borderColor='var(--border)'">
   <div style="display:flex;align-items:flex-start;gap:12px;">
     <span style="font-size:1.8rem;">🧑‍💼</span>
     <div>
       <strong style="display:block;font-size:1rem;">Option C — Talk to Our Team</strong>
       <span style="color:var(--muted);font-size:.85rem;">Speak directly with a GreenBay specialist on WhatsApp to discuss your options.</span>
     </div>
   </div>
 </div>

 </div>
 `;
    resultsStep.appendChild(optionsDiv);
    addChatMessage('bot', "We understand the price might not be what you expected. Here are three alternative options for you — pick the one that works best!");
}

async function selectRejectionOption(option) {
    const data = state.evaluation || {};
    const currency = data.currency_code || 'KES';
    const a = state.answers;

    addChatMessage('user', `I'd like Option ${option}`);
    showTypingIndicator();

    try {
        if (state.sessionId) {
            const resp = await fetch(`${API_BASE}/tradein/${state.sessionId}/rejection-choice`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ option: option }),
            });
            if (resp.ok) {
                const result = await resp.json();
                await sleep(1000);
                hideTypingIndicator();
                addChatMessage('bot', result.message);

                if (option === 'C') {
                    const waMsg = encodeURIComponent(
                        `Hi GreenBay, I'd like to discuss the trade-in offer for my ${a.brand} ${CATEGORY_NAMES[a.category] || a.category}. AI offered ${currency} ${data.opening_offer}. Name: ${a.sellerName}. Phone: ${a.sellerPhone}`
                    );
                    addChatMessage('bot', `<a href="https://wa.me/254705919099?text=${waMsg}" target="_blank" class="btn btn-whatsapp" style="display:inline-flex;margin-top:8px;">💬 Open WhatsApp</a>`);
                }

                // Remove the option cards
                const optArea = document.querySelector('.rejection-options-area');
                if (optArea) optArea.remove();

                saveState();
                return;
            }
        }
    } catch (err) {
        console.warn('Rejection choice API failed:', err);
    }

    await sleep(1000);
    hideTypingIndicator();
    addChatMessage('bot', 'We\'ve noted your preference. Our team will be in touch shortly!');
    saveState();
}

function submitCounterFromInput() {
    const input = document.getElementById('counterInput');
    if (!input) return;
    const price = parsePrice(input.value);
    if (!price) {
        addChatMessage('bot', 'Please enter a valid price in KES (e.g. "25000" or "25k"). ');
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
        addChatMessage('bot', `KES ${formatKES(data.system_offer)} works! You've got a deal!`);
        showPickupDropoffChoice(data.system_offer);

    } else if (data.decision === 'decline') {
        neg.status = 'declined';
        addChatMessage('bot', `Unfortunately we can't go above KES ${formatKES(data.system_offer)} for this unit at this time. Our offer stands for 7 days if you change your mind. <br><br>
 Thanks for your time — you're welcome to come back anytime!`);
        showNoDeal(data.system_offer);

    } else {
        // Counter
        const incentive = data.rounds_remaining <= 1
            ? `<br><br> <strong>Bonus:</strong> At this price, you'll get a <strong>15% trade-in discount</strong> on your next GreenBay purchase!`
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
        acceptBtn.textContent = ` Accept KES ${formatKES(newOffer)}`;
        acceptBtn.setAttribute('onclick', `acceptOffer(${newOffer})`);
    }
}

async function acceptOffer(amount) {
    state.negotiation.status = 'accepted';
    const currency = (state.evaluation && state.evaluation.currency_code) || 'KES';
    addChatMessage('user', `I accept ${currency} ${formatKES(amount)}`);

    showTypingIndicator();

    // v6: Call backend accept-offer endpoint
    try {
        if (state.sessionId) {
            const resp = await fetch(`${API_BASE}/tradein/${state.sessionId}/accept-offer`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            });
            if (resp.ok) {
                const data = await resp.json();
                console.log('Accept offer response:', data);
            }
        }
    } catch (err) {
        console.warn('Accept offer API call failed:', err);
    }

    await sleep(1000);
    hideTypingIndicator();

    addChatMessage('bot', `Wonderful! Deal confirmed at <strong>${currency} ${formatKES(amount)}</strong>!<br><br>
 Now, how would you like to proceed?<br><br>
 🚚 <strong>Option 1:</strong> We come to you — FREE pickup in Nairobi<br>
 🏬 <strong>Option 2:</strong> Drop it off at one of our outlets<br><br>
 Choose your preferred option below!`);

    showPickupDropoffChoice(amount);
    saveState();
}

function showPickupDropoffChoice(amount) {
    const a = state.answers;
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
 <div class="deal-result">
 <div class="result-icon">🎉</div>
 <h3>Deal Confirmed — KES ${formatKES(amount)}</h3>
 <p>Your ${a.brand} ${CATEGORY_NAMES[a.category]} has been accepted. Choose how to proceed:</p>
 
 <div style="display:grid;gap:16px;margin:24px 0;">
 <div class="option-card" style="cursor:pointer;padding:20px;text-align:left;" onclick="showPickupForm(${amount})">
 <div style="display:flex;align-items:center;gap:12px;">
 <span style="font-size:2rem;">🚚</span>
 <div>
 <strong style="display:block;font-size:1.05rem;">Schedule a Pickup</strong>
 <span style="color:var(--muted);font-size:.85rem;">We'll send someone to collect it at your location — FREE in Nairobi</span>
 </div>
 </div>
 </div>
 <div class="option-card" style="cursor:pointer;padding:20px;text-align:left;" onclick="showDropoffLocations(${amount})">
 <div style="display:flex;align-items:center;gap:12px;">
 <span style="font-size:2rem;">🏬</span>
 <div>
 <strong style="display:block;font-size:1.05rem;">Drop Off at Our Outlet</strong>
 <span style="color:var(--muted);font-size:.85rem;">Bring it to Kasarani or Roysambu — see our inventory too!</span>
 </div>
 </div>
 </div>
 </div>

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
 <span>Seller</span>
 <span>${escapeHtml(a.sellerName)} (${escapeHtml(a.sellerPhone)})</span>
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
 </div>
 `;
}

function showPickupForm(amount) {
    const a = state.answers;
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
 <div class="deal-result">
 <div class="result-icon">🚚</div>
 <h3>Schedule Your Pickup</h3>
 <p>Enter your address and we'll arrange a FREE pickup in Nairobi.</p>
 
 <div class="form-group" style="margin:20px 0;">
 <label class="form-label">Pickup Address / Location</label>
 <textarea class="form-input" id="pickupAddress" rows="3" placeholder="e.g. Kilimani, Lenana Road, Apt 4B — or drop a Google Maps link"></textarea>
 </div>
 <div class="form-group" style="margin-bottom:20px;">
 <label class="form-label">Preferred Pickup Day</label>
 <select class="form-input" id="pickupDay">
 <option value="">Select a day</option>
 <option value="Today">Today</option>
 <option value="Tomorrow">Tomorrow</option>
 <option value="This Week">Later This Week</option>
 <option value="Next Week">Next Week</option>
 </select>
 </div>
 
 <button class="btn btn-primary" style="width:100%;" onclick="submitPickupRequest(${amount})">
 ✅ Confirm Pickup Request
 </button>
 
 <button class="btn btn-ghost" style="width:100%;margin-top:8px;" onclick="showPickupDropoffChoice(${amount})">
 ← Back to Options
 </button>
 </div>
 `;
    addChatMessage('bot', 'Please enter your location details so we can schedule the pickup.');
}

async function submitPickupRequest(amount) {
    const address = document.getElementById('pickupAddress')?.value?.trim();
    const day = document.getElementById('pickupDay')?.value;
    const a = state.answers;
    
    if (!address) {
        addChatMessage('bot', 'Please enter your address or location so we know where to pick up!');
        return;
    }

    // Build notification message for Newton (via WhatsApp deep link)
    const pickupMsg = `🚚 PICKUP REQUEST\n\nSeller: ${a.sellerName}\nPhone: ${a.sellerPhone}\nAppliance: ${a.brand} ${a.model || ''} ${CATEGORY_NAMES[a.category]}\nCondition: Grade ${state.evaluation?.condition_grade || 'B'}\nAgreed Price: KES ${formatKES(amount)}\nPickup Address: ${address}\nPreferred Day: ${day || 'ASAP'}\nRef: GB-${state.sessionId || Date.now()}`;

    // Save to backend and get WhatsApp deep-link for Newton
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
            const data = await resp.json();
            // Auto-open WhatsApp to notify Newton
            if (data.whatsapp_link) {
                window.open(data.whatsapp_link, '_blank');
            }
        }
    } catch(_) { /* Non-critical */ }

    addChatMessage('bot', `Pickup confirmed! 🎉<br><br>
 <strong>PICKUP SUMMARY</strong><br>
 📍 Location: ${escapeHtml(address)}<br>
 📅 Preferred: ${day || 'ASAP'}<br>
 💰 Payment: KES ${formatKES(amount)} via M-Pesa on pickup<br><br>
 Our team (Newton) will contact you at <strong>${escapeHtml(a.sellerPhone)}</strong> to confirm the exact time. Thanks for choosing GreenBay!`);

    showFinalConfirmation(amount, 'pickup', address, day);
}

function showDropoffLocations(amount) {
    const a = state.answers;
    addChatMessage('bot', 'Here are our outlet locations. Drop off your appliance and browse our inventory while you\'re there!');
    
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
 <div class="deal-result">
 <div class="result-icon">🏬</div>
 <h3>Our Outlet Locations</h3>
 <p>Drop off your <strong>${a.brand} ${CATEGORY_NAMES[a.category]}</strong> at either location and get paid on the spot!</p>
 
 <div style="display:grid;gap:16px;margin:20px 0;">
 <div style="background:var(--green-light);border-radius:var(--radius-sm);padding:20px;">
 <h4 style="margin-bottom:8px;">📍 Kasarani Outlet</h4>
 <p style="font-size:.88rem;color:var(--slate);margin-bottom:12px;">Delta 40, Kasarani — Opposite Safari Park Hotel</p>
 <a href="https://maps.app.goo.gl/kasarani-greenbay" target="_blank" style="font-size:.85rem;font-weight:600;">Open in Google Maps →</a>
 </div>
 <div style="background:var(--green-light);border-radius:var(--radius-sm);padding:20px;">
 <h4 style="margin-bottom:8px;">📍 Roysambu Outlet</h4>
 <p style="font-size:.88rem;color:var(--slate);margin-bottom:12px;">Roysambu — Near TRM Mall</p>
 <a href="https://maps.app.goo.gl/roysambu-greenbay" target="_blank" style="font-size:.85rem;font-weight:600;">Open in Google Maps →</a>
 </div>
 </div>
 
 <div style="background:var(--warm);border-radius:var(--radius-sm);padding:16px;margin-bottom:20px;">
 <p style="font-size:.88rem;margin:0;"><strong>💡 Tip:</strong> While at the outlet, check out our inventory! You might find the perfect upgrade — and your trade-in value gives you a head start.</p>
 </div>

 <div class="deal-summary">
 <div class="deal-summary-row">
 <span>Appliance</span>
 <span>${a.brand} ${a.model || CATEGORY_NAMES[a.category]}</span>
 </div>
 <div class="deal-summary-row">
 <span>Seller</span>
 <span>${escapeHtml(a.sellerName)} (${escapeHtml(a.sellerPhone)})</span>
 </div>
 <div class="deal-summary-row total">
 <span>Amount Due on Drop-off</span>
 <span>KES ${formatKES(amount)}</span>
 </div>
 </div>

 <div class="whatsapp-bridge" style="margin-top:20px;">
 <div class="whatsapp-bridge-icon">📞</div>
 <div class="whatsapp-bridge-text">
 <h4>Confirm Your Visit</h4>
 <p>Let us know when you're coming so we can prepare!</p>
 </div>
 <a href="https://wa.me/254705919099?text=Hi%20GreenBay%2C%20I'll%20be%20dropping%20off%20my%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(CATEGORY_NAMES[a.category])}%20at%20your%20outlet.%20Agreed%20price%3A%20KES%20${amount}.%20Name%3A%20${encodeURIComponent(a.sellerName)}.%20Ref%3A%20GB-${state.sessionId || ''}" 
 target="_blank" class="btn btn-whatsapp btn-sm">Confirm on WhatsApp</a>
 </div>

 <button class="btn btn-ghost" style="width:100%;margin-top:12px;" onclick="showPickupDropoffChoice(${amount})">
 ← Back to Options
 </button>
 </div>
 `;
}

function showFinalConfirmation(amount, method, address, day) {
    const a = state.answers;
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
 <div class="deal-result">
 <div class="result-icon">🎉</div>
 <h3>All Set!</h3>
 <p>Your trade-in is confirmed. ${method === 'pickup' ? 'Our team will pick up your appliance.' : 'Drop it off at your chosen outlet.'}</p>
 
 <div class="deal-summary">
 <div class="deal-summary-row">
 <span>Appliance</span>
 <span>${a.brand} ${a.model || CATEGORY_NAMES[a.category]}</span>
 </div>
 <div class="deal-summary-row">
 <span>Seller</span>
 <span>${escapeHtml(a.sellerName)} (${escapeHtml(a.sellerPhone)})</span>
 </div>
 ${method === 'pickup' ? `<div class="deal-summary-row"><span>Pickup Location</span><span>${escapeHtml(address || '')}</span></div>` : ''}
 ${method === 'pickup' && day ? `<div class="deal-summary-row"><span>Preferred Day</span><span>${day}</span></div>` : ''}
 <div class="deal-summary-row">
 <span>Payment</span>
 <span>M-Pesa on ${method === 'pickup' ? 'pickup' : 'drop-off'}</span>
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
 
<button class="btn btn-primary" onclick="startNewEvaluation()" style="width:100%;margin-top:20px;">
<i data-lucide="rotate-ccw" style="width:16px;height:16px;margin-right:6px;"></i>
Start New Evaluation
</button>
 
 <div class="whatsapp-bridge" style="margin-top:20px;">
 <div class="whatsapp-bridge-icon">💬</div>
 <div class="whatsapp-bridge-text">
 <h4>Track on WhatsApp</h4>
 <p>Get ${method === 'pickup' ? 'pickup' : 'visit'} updates and payment confirmation.</p>
 </div>
 <a href="https://wa.me/254705919099?text=Hi%20GreenBay%2C%20my%20deal%20reference%20is%20GB-${state.sessionId || ''}" 
 target="_blank" class="btn btn-whatsapp btn-sm">Open WhatsApp</a>
 </div>
 <div id="relatedProductsSection" style="margin-top:24px;"></div>
 </div>
 `;

    // Load related product suggestions
    loadRelatedProducts(a.category);
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
            const priceStr = p.price ? `KES ${Math.round(p.price).toLocaleString('en-KE')}` : 'Contact us';
            const savingsStr = p.compare_at_price && p.price ?
                `<span style="text-decoration:line-through;color:var(--muted);font-size:.78rem;margin-left:6px;">KES ${Math.round(p.compare_at_price).toLocaleString('en-KE')}</span>` : '';
            const imgSrc = p.image_url || '';
            return `
            <a href="${p.product_url || 'https://greenbay.market'}" target="_blank" style="text-decoration:none;color:inherit;display:block;">
            <div style="background:var(--surface);border-radius:var(--radius-sm);overflow:hidden;border:1px solid var(--border);">
                ${imgSrc ? `<img src="${imgSrc}" alt="${p.title}" style="width:100%;height:140px;object-fit:cover;" loading="lazy">` : ''}
                <div style="padding:10px;">
                    <p style="font-size:.82rem;font-weight:600;margin:0 0 4px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;">${p.title}</p>
                    <p style="font-size:.85rem;margin:0;color:var(--emerald);font-weight:700;">${priceStr}${savingsStr}</p>
                    <span style="font-size:.72rem;color:var(--muted);">${p.product_type || ''}</span>
                </div>
            </div>
            </a>`;
        }).join('');

        container.innerHTML = `
            <h4 style="margin-bottom:12px;font-size:1rem;">🛍️ Use Your Trade-In Value — Browse Our Deals</h4>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;">
                ${cards}
            </div>
            <a href="https://greenbay.market" target="_blank" class="btn btn-outline" style="width:100%;margin-top:12px;">
                View All Products on GreenBay →
            </a>
        `;
    } catch(e) {
        console.log('Related products load failed (non-critical):', e);
    }
}

function showNoDeal(lastOffer) {
    const a = state.answers;
    const resultsStep = document.getElementById('stepResults');
    resultsStep.innerHTML = `
 <div class="deal-result">
 <div class="result-icon"></div>
 <h3>Offer Stands for 7 Days</h3>
 <p>Our offer of <strong>KES ${formatKES(lastOffer)}</strong> for your ${a.brand} ${CATEGORY_NAMES[a.category]} is still available.</p>
 
 <div style="display:flex;gap:12px;justify-content:center;margin-top:20px;">
<button class="btn btn-primary" onclick="acceptOffer(${lastOffer})">Accept KES ${formatKES(lastOffer)}</button>
<button class="btn btn-secondary" onclick="startNewEvaluation()">
  <i data-lucide="rotate-ccw" style="width:14px;height:14px;margin-right:4px;"></i>
  Start New Evaluation
</button>
 </div>
 
 <div class="whatsapp-bridge" style="margin-top:20px;">
 <div class="whatsapp-bridge-icon"></div>
 <div class="whatsapp-bridge-text">
 <h4>Continue on WhatsApp</h4>
 <p>Chat with our team for a more personal experience.</p>
 </div>
 <a href="https://wa.me/254705919099?text=Hi%20GreenBay%2C%20I%20got%20an%20offer%20of%20KES%20${lastOffer}%20for%20my%20${encodeURIComponent(a.brand)}%20${encodeURIComponent(CATEGORY_NAMES[a.category])}" 
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
        category: null, brand: null, model: '', modelPhoto: null,
        age: null, condition: null, conditionGrade: null, ownership: null,
        issues: '', otherDescription: '', sellerName: '', sellerPhone: '',
        photos: [], price: null,
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
    const sellerName = document.getElementById('sellerNameInput');
    if (sellerName) sellerName.value = '';
    const sellerPhone = document.getElementById('sellerPhoneInput');
    if (sellerPhone) sellerPhone.value = '';
    document.getElementById('photoGrid').innerHTML = '';
    updatePhotoCount();

    // Reset chat
    document.getElementById('chatMessages').innerHTML = '';

    // Show wizard footer
    document.getElementById('wizardFooter').classList.remove('hidden');

    goToStep(1);
    addChatMessage('bot', 'Let\'s start fresh! What type of appliance are you selling?');
}

/* ============================================================
 UTILITY FUNCTIONS
 ============================================================ */
function formatKES(num) {
    if (!num && num !== 0) return 'N/A';
    return Math.round(num).toLocaleString('en-KE');
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
