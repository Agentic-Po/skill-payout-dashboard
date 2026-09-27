// tests/domshim.js — hand-written DOM shim for the execution-level render
// gates (tests/test_render_exec.py, tests/test_figure_parity.py,
// tests/test_render_mutation.py — driven by tests/render_harness.js).
//
// Deliberately NOT jsdom and NOT an npm dependency: a single committed file,
// implementing exactly the browser surface the page scripts in template.html
// / template_coupon.html actually touch (grep before extending it). Elements
// are dumb plain objects that RECORD what the script did — innerHTML /
// textContent strings, attributes, listeners — so a test can assert on the
// recorded structure. Nothing here paints.
//
// Loop 2 (2026-09-27) additions:
//   * STATIC TREE. When globalThis.__DOM_TREE is set (tests/pagehost.py parses
//     the page's own markup into {id: {parent, tag, attrs}}), getElementById
//     returns null for an id the page does not have (as a browser does), and
//     writing innerHTML/textContent on an element DETACHES every static
//     descendant — so "the section was wiped to its placeholder" is
//     observable, and a stale reference cannot be mistaken for a live one.
//     textContent reads aggregate the element's own script-written text plus
//     its attached descendants' (static prose is not modelled).
//   * FAKE CLOCK + TIMERS. Date.now() reads globalThis.__clock.now (defaults
//     to the real time at load); setTimeout / setInterval / clearInterval
//     register with the shim and fire only from __clock.advance(ms), so an
//     interval-driven check (the stale banner) is testable deterministically.
//   * EVENTS. document / window / elements keep listeners; __fire(target,
//     type, ev) dispatches. document.hidden / visibilityState are settable.
//     window.onerror + window "error" listeners are invoked by the harness for
//     any uncaught error (__reportUncaught).
//   * createDocumentFragment, replaceChildren, remove(), hidden.
//
// APIs implemented:
//   document: getElementById, createElement, createTextNode,
//             createDocumentFragment, body, documentElement, add/remove
//             EventListener, querySelector(All) (inert), hidden,
//             visibilityState
//   element:  appendChild, insertBefore, removeChild, replaceChildren,
//             children/childNodes, firstChild/lastChild, innerHTML,
//             textContent, innerText, add/removeEventListener,
//             set/get/removeAttribute, dataset, style, classList,
//             querySelector(All) (inert), contains, closest, matches,
//             getBoundingClientRect, focus/blur/click/remove, cloneNode,
//             onclick, open, hidden, id
//   window:   innerWidth/innerHeight, matchMedia, add/removeEventListener,
//             onerror, requestAnimationFrame, setTimeout/setInterval/
//             clearTimeout/clearInterval (fake clock)
//   misc:     localStorage (inert, try/catch-safe), navigator.clipboard,
//             location
(function (g) {
  "use strict";

  var TREE = g.__DOM_TREE || null;
  var KIDS = {};                       // static id -> [child ids]
  if (TREE) {
    Object.keys(TREE).forEach(function (id) {
      var p = TREE[id].parent;
      if (p != null) { (KIDS[p] = KIDS[p] || []).push(id); }
    });
  }
  var ALL = [];                        // every element ever created (dump)
  var elements = new Map();            // id -> El (static or registered)
  var detached = {};                   // static ids wiped by an ancestor write
  var uncaught = [];

  function makeClassList() {
    var set = {};
    return {
      add: function (c) { set[c] = 1; },
      remove: function (c) { delete set[c]; },
      toggle: function (c, f) {
        var on = f === undefined ? !set[c] : !!f;
        if (on) { set[c] = 1; } else { delete set[c]; }
        return on;
      },
      contains: function (c) { return !!set[c]; }
    };
  }

  function stripTags(h) {
    return String(h).replace(/<[^>]*>/g, "").replace(/&nbsp;/g, " ")
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'").replace(/&amp;/g, "&");
  }
  function esc(t) {
    return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function detachStaticBelow(id) {
    (KIDS[id] || []).forEach(function (k) {
      detached[k] = true;
      var e = elements.get(k);
      if (e) { e._detached = true; }
      detachStaticBelow(k);
    });
  }

  function El(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.childNodes = this.children;
    this.style = {};
    this.dataset = {};
    this.classList = makeClassList();
    this._attrs = {};
    this._listeners = {};
    this._html = "";
    this._text = null;
    this._id = "";
    this._static = false;
    this._detached = false;
    this._removed = false;
    this.innerText = "";
    this.value = "";
    this.parentNode = null;
    this.onclick = null;
    this.open = undefined;        // <details> — read before ever being set
    this.hidden = false;
    ALL.push(this);
  }
  Object.defineProperty(El.prototype, "id", {
    get: function () { return this._id; },
    set: function (v) {
      this._id = String(v);
      if (v && !this._static) { elements.set(String(v), this); }
    }
  });
  Object.defineProperty(El.prototype, "innerHTML", {
    get: function () { return this._text != null ? esc(this._text) : this._html; },
    set: function (v) {
      this._html = String(v); this._text = null;
      this.children.length = 0;
      if (this._static) { detachStaticBelow(this._id); }
    }
  });
  Object.defineProperty(El.prototype, "textContent", {
    get: function () {
      var s = this._text != null ? this._text : stripTags(this._html);
      this.children.forEach(function (c) { if (c && !c._removed) { s += c.textContent || ""; } });
      if (this._static) {
        (KIDS[this._id] || []).forEach(function (k) {
          var e = elements.get(k);
          if (e && !e._detached && !e._removed) { s += e.textContent; }
        });
      }
      return s;
    },
    set: function (v) {
      this._text = String(v); this._html = "";
      this.children.length = 0;
      if (this._static) { detachStaticBelow(this._id); }
    }
  });
  El.prototype.appendChild = function (c) {
    if (c && c.tagName === "#FRAGMENT") {
      var self = this;
      c.children.slice().forEach(function (k) { self.appendChild(k); });
      c.children.length = 0;
      return c;
    }
    this.children.push(c);
    if (c && typeof c === "object") { c.parentNode = this; c._removed = false; }
    return c;
  };
  El.prototype.insertBefore = function (c, ref) {
    var i = ref ? this.children.indexOf(ref) : -1;
    if (i >= 0) { this.children.splice(i, 0, c); } else { this.children.push(c); }
    if (c && typeof c === "object") { c.parentNode = this; c._removed = false; }
    return c;
  };
  El.prototype.removeChild = function (c) {
    var i = this.children.indexOf(c);
    if (i >= 0) { this.children.splice(i, 1); }
    if (c && typeof c === "object") { c.parentNode = null; c._removed = true; }
    return c;
  };
  El.prototype.replaceChildren = function () {
    this._html = ""; this._text = null; this.children.length = 0;
    if (this._static) { detachStaticBelow(this._id); }
    for (var i = 0; i < arguments.length; i++) { this.appendChild(arguments[i]); }
  };
  El.prototype.addEventListener = function (t, fn) {
    (this._listeners[t] = this._listeners[t] || []).push(fn);
  };
  El.prototype.removeEventListener = function (t, fn) {
    var l = this._listeners[t] || [];
    var i = l.indexOf(fn);
    if (i >= 0) { l.splice(i, 1); }
  };
  El.prototype.setAttribute = function (k, v) {
    this._attrs[k] = String(v);
    if (k === "id") { this.id = String(v); }
    if (k === "hidden") { this.hidden = true; }
  };
  El.prototype.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
  };
  El.prototype.hasAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this._attrs, k);
  };
  El.prototype.removeAttribute = function (k) {
    delete this._attrs[k];
    if (k === "hidden") { this.hidden = false; }
  };
  El.prototype.querySelectorAll = function () { return []; };
  El.prototype.querySelector = function () { return null; };
  El.prototype.getElementsByTagName = function () { return []; };
  El.prototype.contains = function (o) { return o === this; };
  El.prototype.closest = function () { return null; };
  El.prototype.matches = function () { return false; };
  El.prototype.getBoundingClientRect = function () {
    return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
  };
  El.prototype.focus = function () {};
  El.prototype.blur = function () {};
  El.prototype.click = function () {};
  El.prototype.remove = function () {
    if (this.parentNode) { this.parentNode.removeChild(this); }
    this._removed = true;
    if (this._static) { detached[this._id] = true; this._detached = true; detachStaticBelow(this._id); }
  };
  El.prototype.scrollIntoView = function () {};
  El.prototype.cloneNode = function () { return new El(this.tagName); };
  Object.defineProperty(El.prototype, "firstChild", {
    get: function () { return this.children[0] || null; }
  });
  Object.defineProperty(El.prototype, "lastChild", {
    get: function () { return this.children[this.children.length - 1] || null; }
  });

  function staticEl(id) {
    var spec = TREE[id];
    var e = new El(spec.tag);
    e._static = true;
    e._id = String(id);
    var a = spec.attrs || {};
    Object.keys(a).forEach(function (k) { e._attrs[k] = a[k]; });
    if ("hidden" in a) { e.hidden = true; }
    if (spec.tag === "details") { e.open = "open" in a; }
    var m = /display\s*:\s*none/.test(a.style || "");
    if (m) { e.style.display = "none"; }
    elements.set(id, e);
    if (spec.parent != null && !detached[spec.parent]) {
      var p = getById(spec.parent);
      if (p) { e.parentNode = p; }
    }
    return e;
  }

  function getById(id) {
    id = String(id);
    if (TREE) {
      if (Object.prototype.hasOwnProperty.call(TREE, id)) {
        if (detached[id]) { return null; }
        return elements.get(id) || staticEl(id);
      }
      var d = elements.get(id);
      return d && !d._removed ? d : null;
    }
    // legacy mode (no tree): every id exists
    if (!elements.has(id)) {
      var e = new El("div");
      e.id = id;
      elements.set(id, e);
    }
    return elements.get(id);
  }

  var docListeners = {};
  var documentShim = {
    body: new El("body"),
    documentElement: new El("html"),
    hidden: false,
    visibilityState: "visible",
    createElement: function (t) { return new El(t); },
    createTextNode: function (t) { return { textContent: String(t) }; },
    createDocumentFragment: function () { return new El("#fragment"); },
    getElementById: getById,
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    addEventListener: function (t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener: function (t, fn) {
      var l = docListeners[t] || []; var i = l.indexOf(fn); if (i >= 0) { l.splice(i, 1); }
    },
    _listeners: docListeners
  };

  g.document = documentShim;
  g.window = g;
  g.self = g;
  g.innerWidth = 1280;
  g.innerHeight = 800;
  g.matchMedia = function (q) {
    return { matches: false, media: String(q),
             addEventListener: function () {}, removeEventListener: function () {},
             addListener: function () {}, removeListener: function () {} };
  };
  // inert but never-throwing (page call sites wrap in try/catch anyway)
  g.localStorage = {
    getItem: function () { return null; },
    setItem: function () {},
    removeItem: function () {},
    clear: function () {}
  };
  try {
    if (!g.navigator) { g.navigator = {}; }
    if (!g.navigator.clipboard) {
      g.navigator.clipboard = { writeText: function () { return Promise.resolve(); } };
    }
  } catch (e) { /* node >= 21 exposes a locked-down navigator — fine, the
                   clipboard is only touched inside click handlers */ }
  try {
    if (!g.location) { g.location = { href: "https://localhost/", search: "", hash: "", pathname: "/" }; }
  } catch (e) { /* ignore */ }

  // ---- window listeners + uncaught-error reporting ----
  var winListeners = {};
  g.addEventListener = function (t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); };
  g.removeEventListener = function (t, fn) {
    var l = winListeners[t] || []; var i = l.indexOf(fn); if (i >= 0) { l.splice(i, 1); }
  };
  function fire(target, type, ev) {
    ev = ev || {};
    ev.type = type;
    if (!ev.target) { ev.target = target; }
    var l = target === g ? winListeners[type]
          : target === documentShim ? docListeners[type]
          : (target && target._listeners ? target._listeners[type] : null);
    (l || []).slice().forEach(function (fn) { fn.call(target, ev); });
  }
  g.__fire = fire;
  function reportUncaught(err, where) {
    var msg = (err && err.message) ? err.message : String(err);
    uncaught.push({ where: where || "", msg: msg, name: err && err.name });
    try {
      if (typeof g.onerror === "function") { g.onerror(msg, where || "", 0, 0, err); }
      fire(g, "error", { message: msg, error: err });
    } catch (e2) {
      uncaught.push({ where: "onerror handler", msg: String(e2 && e2.message || e2) });
    }
  }
  g.__reportUncaught = reportUncaught;

  // ---- fake clock + timers ----
  var RealNow = Date.now;
  var clock = { now: (typeof g.__CLOCK_NOW === "number") ? g.__CLOCK_NOW : RealNow() };
  Date.now = function () { return clock.now; };
  var timers = {}; var nextId = 1;
  function addTimer(fn, ms, repeat) {
    var id = nextId++;
    ms = Math.max(0, +ms || 0);
    timers[id] = { fn: fn, ms: ms, at: clock.now + ms, repeat: repeat };
    return id;
  }
  g.setTimeout = function (fn, ms) { return addTimer(fn, ms, false); };
  g.setInterval = function (fn, ms) { return addTimer(fn, ms, true); };
  g.clearTimeout = g.clearInterval = function (id) { delete timers[id]; };
  g.requestAnimationFrame = function (fn) { return addTimer(fn, 16, false); };
  clock.advance = function (ms) {
    var end = clock.now + ms;
    for (;;) {
      var due = null;
      Object.keys(timers).forEach(function (k) {
        var t = timers[k];
        if (t.at <= end && (due === null || t.at < timers[due].at)) { due = k; }
      });
      if (due === null) { break; }
      var t = timers[due];
      clock.now = Math.max(clock.now, t.at);
      if (t.repeat) { t.at += Math.max(t.ms, 1); } else { delete timers[due]; }
      try { t.fn(); } catch (e) { reportUncaught(e, "timer"); }
    }
    clock.now = end;
  };
  clock.timers = function () { return Object.keys(timers).map(function (k) { return timers[k]; }); };
  g.__clock = clock;

  // ---- dump for the harness: every attached element's recorded writes ----
  function attached(e) {
    if (e._removed || e._detached) { return false; }
    var p = e.parentNode, n = 0;
    while (p && n++ < 200) { if (p._removed || p._detached) { return false; } p = p.parentNode; }
    return true;
  }
  function dump() {
    var ids = {}, writes = [];
    elements.forEach(function (e, id) {
      ids[id] = { html: e._html, text: e._text, hidden: !!e.hidden,
                  display: e.style.display == null ? null : String(e.style.display),
                  detached: !!(e._detached || detached[id]), removed: !!e._removed,
                  attrs: e._attrs, tag: e.tagName.toLowerCase(), static: e._static,
                  aggText: e.textContent, listeners: Object.keys(e._listeners) };
    });
    ALL.forEach(function (e) {
      if (!attached(e)) { return; }
      if (e._text != null && e._text !== "") { writes.push({ text: e._text }); }
      else if (e._html) { writes.push({ html: e._html }); }
    });
    return { ids: ids, writes: writes, uncaught: uncaught };
  }

  // the probe / harness reads the recorded structure here
  g.__domshim = { elements: elements, El: El, document: documentShim,
                  dump: dump, uncaught: uncaught, fire: fire, clock: clock };
})(globalThis);
