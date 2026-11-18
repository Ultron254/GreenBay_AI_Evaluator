/**
 * GreenBay x Vision Plus: motion for the campaign layer (theme.css, slots/).
 * Loaded after partner.js, before lucide.createIcons().
 *
 *  - Pauses every loop while the tab is hidden (html.vp-paused) and in
 *    sections that are off screen (.vp-idle).
 *  - Pointer parallax on the hero stage and icons (fine pointers only), plus
 *    a little scroll drift on the icons. Transforms only, nothing reflows.
 *  - The product rail glides on its own, stops while someone hovers, touches,
 *    focuses or scrolls it, and loops through a hidden second copy.
 * With prefers-reduced-motion nothing moves: no parallax, no glide, and
 * theme.css switches the CSS loops off. The rail still scrolls by hand.
 */
(function () {
    'use strict';
    var root = document.documentElement;
    if (root.getAttribute('data-partner') !== 'vision-plus') return;

    var mm = function (q) { return !!(window.matchMedia && window.matchMedia(q).matches); };
    var reduce = mm('(prefers-reduced-motion: reduce)');
    var raf = window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : function (f) { return setTimeout(function () { f(Date.now()); }, 16); };
    var listeners = [];

    /* ---------- hidden tab: pause ---------- */
    function onVisibility() {
        root.classList.toggle('vp-paused', document.hidden);
        listeners.forEach(function (fn) { fn(!document.hidden); });
    }
    document.addEventListener('visibilitychange', onVisibility);
    onVisibility();

    /* ---------- off screen: pause ---------- */
    var visible = new WeakMap();
    if ('IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                visible.set(e.target, e.isIntersecting);
                e.target.classList.toggle('vp-idle', !e.isIntersecting);
                if (e.target.__vpWake) e.target.__vpWake();
            });
        }, { rootMargin: '60px 0px' });
        document.querySelectorAll('.p-hero, .vp-swap, .vp-shop, .vp-tip').forEach(function (el) { io.observe(el); });
    }
    function onScreen(el) { return visible.has(el) ? visible.get(el) : true; }

    if (reduce) return;

    /* ---------- hero parallax ---------- */
    var hero = document.querySelector('.p-hero');
    if (hero) {
        var tx = 0, ty = 0, x = 0, y = 0, sy = 0, pending = false;
        var tick = function () {
            pending = false;
            x += (tx - x) * 0.08;
            y += (ty - y) * 0.08;
            hero.style.setProperty('--mx', x.toFixed(3));
            hero.style.setProperty('--my', y.toFixed(3));
            hero.style.setProperty('--sy', sy.toFixed(3));
            if (Math.abs(tx - x) > 0.002 || Math.abs(ty - y) > 0.002) kick();
        };
        var kick = function () {
            if (pending || document.hidden) return;
            pending = true;
            raf(tick);
        };
        if (mm('(pointer: fine)')) {
            hero.addEventListener('pointermove', function (e) {
                var r = hero.getBoundingClientRect();
                tx = Math.max(-1, Math.min(1, ((e.clientX - r.left) / r.width) * 2 - 1));
                ty = Math.max(-1, Math.min(1, ((e.clientY - r.top) / r.height) * 2 - 1));
                kick();
            }, { passive: true });
            hero.addEventListener('pointerleave', function () { tx = 0; ty = 0; kick(); });
        }
        window.addEventListener('scroll', function () {
            var r = hero.getBoundingClientRect();
            if (r.bottom < 0) return;
            sy = Math.max(0, Math.min(1, -r.top / Math.max(1, r.height)));
            kick();
        }, { passive: true });
    }

    /* ---------- product rail ---------- */
    var shop = document.querySelector('.vp-shop');
    var rail = shop && shop.querySelector('.vp-rail');
    var track = rail && rail.querySelector('.vp-rail-track');
    if (!rail || !track) return;

    var SPEED = 0.032;      // px per ms, about 32 px a second
    var holdUntil = 0;      // a person is using it: wait until this time
    var hovering = false;
    var pos = rail.scrollLeft;
    var last = 0;
    var running = false;

    function loopWidth() {
        var gap = parseFloat(getComputedStyle(rail).columnGap) || 16;
        return track.offsetWidth + gap;
    }
    function hold(ms) { holdUntil = Date.now() + (ms || 2600); }
    function wrap() {
        var w = loopWidth();
        if (w > 0 && rail.scrollLeft >= w) rail.scrollLeft -= w;
    }
    function frame(t) {
        if (!running) return;
        var dt = last ? Math.min(64, t - last) : 16;
        last = t;
        if (hovering || Date.now() < holdUntil) {
            pos = rail.scrollLeft;
        } else {
            var w = loopWidth();
            pos += SPEED * dt;
            if (w > 0 && pos >= w) pos -= w;
            rail.scrollLeft = pos;
        }
        raf(frame);
    }
    function start() {
        if (running || document.hidden || !onScreen(shop)) return;
        running = true; last = 0; pos = rail.scrollLeft;
        raf(frame);
    }
    function stop() { running = false; }
    function wake() { if (!document.hidden && onScreen(shop)) start(); else stop(); }
    shop.__vpWake = wake;
    listeners.push(wake);

    rail.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') hovering = true; });
    rail.addEventListener('pointerleave', function () { hovering = false; hold(800); });
    ['pointerdown', 'touchstart', 'wheel', 'keydown'].forEach(function (ev) {
        rail.addEventListener(ev, function () { hold(); }, { passive: true });
    });
    rail.addEventListener('focusin', function () { hold(6000); });
    rail.addEventListener('scroll', function () { if (Date.now() < holdUntil || hovering) wrap(); }, { passive: true });

    shop.querySelectorAll('[data-vp-dir]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var dir = Number(btn.getAttribute('data-vp-dir')) || 1;
            var card = track.querySelector('.vp-prod');
            var step = card ? (card.offsetWidth + 16) * 2 : 400;
            hold(4000);
            if (dir < 0 && rail.scrollLeft < step) rail.scrollLeft += loopWidth();
            rail.scrollBy({ left: dir * step, behavior: 'smooth' });
        });
    });

    wake();
})();
