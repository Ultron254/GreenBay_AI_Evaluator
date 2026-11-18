/**
 * GreenBay x Mika: the Mika page's scene, loaded after partner.js.
 *
 *  - Step pictures: moves the four pictures from <template id="mkStepArt">
 *    (slots/after_steps.html) into the numbered steps. Space for them is
 *    already reserved in theme.css, so nothing jumps.
 *  - Credit tiles and "what we take": adds an icon to each.
 *  - Hero parallax: the floating pieces lean a little towards the pointer
 *    (fine pointers only).
 *  - Product rail: drifts on its own, stops while a person hovers, touches,
 *    focuses or scrolls it, and loops without a seam.
 *  - Pausing: every animation pauses while the tab is hidden, and the hero
 *    and rail pause while they are off screen.
 *
 * With prefers-reduced-motion nothing moves: no parallax, no drifting rail,
 * and theme.css switches the CSS animations off.
 * It runs before partner.js's own start (DOMContentLoaded), so the count-up
 * in the step pictures is seen by partner.js like any other.
 */
(function () {
    'use strict';
    var root = document.documentElement;
    if (root.getAttribute('data-partner') !== 'mika') return;

    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var finePointer = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;

    function icon(name) {
        var i = document.createElement('i');
        i.setAttribute('data-lucide', name);
        i.setAttribute('aria-hidden', 'true');
        return i;
    }

    /* ---------- step pictures ---------- */
    function stepArt() {
        var tpl = document.getElementById('mkStepArt');
        var steps = document.querySelectorAll('.p-steps-list > .p-step');
        if (!tpl || !tpl.content || !steps.length) return;
        var arts = tpl.content.querySelectorAll('.mk-sa');
        steps.forEach(function (step, n) {
            if (arts[n] && !step.querySelector('.mk-sa')) step.insertBefore(arts[n].cloneNode(true), step.firstChild);
        });
    }

    /* ---------- credit tiles and what we take ---------- */
    var TILE_ICONS = ['coins', 'store', 'badge-check', 'calendar-clock', 'split', 'ban'];
    function creditIcons() {
        document.querySelectorAll('.p-credit .p-table tr').forEach(function (tr, n) {
            if (tr.querySelector('.mk-ti')) return;
            var span = document.createElement('span');
            span.className = 'mk-ti';
            span.setAttribute('aria-hidden', 'true');
            span.appendChild(icon(TILE_ICONS[n % TILE_ICONS.length]));
            tr.insertBefore(span, tr.firstChild);
        });
        var accepts = document.querySelector('.p-accepts');
        if (accepts && !accepts.querySelector('.mk-accept-icons')) {
            var ul = document.createElement('ul');
            ul.className = 'mk-accept-icons';
            ul.setAttribute('aria-hidden', 'true');
            ['refrigerator', 'cooking-pot', 'microwave', 'washing-machine', 'blend', 'coffee'].forEach(function (name) {
                var li = document.createElement('li');
                li.appendChild(icon(name));
                ul.appendChild(li);
            });
            var p = accepts.querySelector('p');
            accepts.insertBefore(ul, p || null);
        }
    }

    /* ---------- pausing ---------- */
    function pauseWhenHidden() {
        var sync = function () { root.classList.toggle('mk-paused', document.hidden); };
        document.addEventListener('visibilitychange', sync);
        sync();
    }

    function pauseOffscreen(el, onChange) {
        if (!el || !('IntersectionObserver' in window)) return;
        new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                el.classList.toggle('mk-off', !e.isIntersecting);
                if (onChange) onChange(e.isIntersecting);
            });
        }, { rootMargin: '80px 0px' }).observe(el);
    }

    /* ---------- hero parallax ---------- */
    function parallax(hero) {
        if (!hero || reduce || !finePointer) return;
        var raf = 0, tx = 0, ty = 0;
        hero.addEventListener('pointermove', function (e) {
            var r = hero.getBoundingClientRect();
            tx = ((e.clientX - r.left) / r.width - 0.5) * 2;
            ty = ((e.clientY - r.top) / r.height - 0.5) * 2;
            if (!raf) raf = requestAnimationFrame(function () {
                raf = 0;
                root.style.setProperty('--mx', tx.toFixed(3));
                root.style.setProperty('--my', ty.toFixed(3));
            });
        });
        hero.addEventListener('pointerleave', function () {
            root.style.setProperty('--mx', '0');
            root.style.setProperty('--my', '0');
        });
    }

    /* ---------- product rail ---------- */
    function rail() {
        var el = document.querySelector('.mk-rail');
        var list = el && el.querySelector('.mk-rail-list');
        if (!el || !list) return;
        if (reduce) return; // a plain row a person can swipe or scroll

        // A second copy for the seamless loop, hidden from assistive tech
        // and from the keyboard.
        var originals = Array.prototype.slice.call(list.children);
        originals.forEach(function (li) {
            var c = li.cloneNode(true);
            c.setAttribute('aria-hidden', 'true');
            c.setAttribute('data-clone', '');
            c.querySelectorAll('img').forEach(function (img) { img.alt = ''; });
            list.appendChild(c);
        });

        var SPEED = 34; // px per second
        var pos = 0, last = 0, raf = 0;
        var hold = false, onScreen = false, resumeAt = 0;
        var setWidth = function () {
            var first = list.querySelector('[data-clone]');
            return first ? first.offsetLeft - list.firstElementChild.offsetLeft : 0;
        };
        var running = function () { return onScreen && !hold && !document.hidden && performance.now() >= resumeAt; };

        function tick(t) {
            raf = 0;
            if (!running()) { last = 0; return; }
            var dt = last ? Math.min(64, t - last) : 0;
            last = t;
            var w = setWidth();
            pos += SPEED * dt / 1000;
            if (w > 0 && pos >= w) pos -= w;
            el.scrollLeft = pos;
            raf = requestAnimationFrame(tick);
        }
        function go() { if (!raf && running()) { last = 0; raf = requestAnimationFrame(tick); } }

        function pauseFor(ms) {
            resumeAt = performance.now() + ms;
            setTimeout(function () { pos = el.scrollLeft; go(); }, ms + 20);
        }

        el.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') hold = true; });
        el.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') { hold = false; pos = el.scrollLeft; go(); } });
        el.addEventListener('focusin', function () { hold = true; });
        el.addEventListener('focusout', function () { hold = false; pos = el.scrollLeft; go(); });
        el.addEventListener('touchstart', function () { pauseFor(3500); }, { passive: true });
        el.addEventListener('wheel', function () { pauseFor(2500); }, { passive: true });
        el.addEventListener('scroll', function () {
            // A person scrolled it (not us): keep the loop seamless and follow them.
            if (raf) return;
            var w = setWidth();
            if (w > 0 && el.scrollLeft >= w) el.scrollLeft -= w;
            pos = el.scrollLeft;
        }, { passive: true });
        document.addEventListener('visibilitychange', go);
        pauseOffscreen(el, function (vis) { onScreen = vis; go(); });
    }

    function init() {
        stepArt();
        creditIcons();
        pauseWhenHidden();
        var hero = document.querySelector('.p-hero');
        pauseOffscreen(hero);
        parallax(hero);
        rail();
        if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons();
    }

    // The page's markup is all above this script, so it can run now, before
    // partner.js starts its reveal and count-up observers.
    if (document.querySelector('.p-foot')) init();
    else document.addEventListener('DOMContentLoaded', init);
})();
