// tests/domshim.js — hand-written DOM shim for the execution-level render
// gate (tests/test_render_exec.py, Cycle-3 Loop 2 item 1).
//
// Deliberately NOT jsdom and NOT an npm dependency: a single committed file,
// implementing exactly the browser surface the page scripts in template.html
// / template_legacy.html actually touch (grep before extending it). Elements
// are dumb plain objects that RECORD what the script did — children arrays,
// innerHTML/textContent strings, attributes, listeners — so the test can
// assert on the recorded structure. Nothing here paints; the point is that
// the scripts RUN to completion on real data without an uncaught error, and
// produce non-empty structure where the page's tables and strips live.
//
// APIs implemented (from grepping the page scripts):
//   document: getElementById, createElement, createTextNode, body,
//             documentElement, addEventListener, querySelector(All)
//   element:  appendChild, insertBefore, removeChild, children/childNodes,
//             firstChild/lastChild, innerHTML, textContent, innerText,
//             addEventListener, set/get/removeAttribute, dataset, style,
//             classList, querySelector(All), contains, closest, matches,
//             getBoundingClientRect, focus/blur/click/remove, cloneNode,
//             onclick (plain property), open (plain property)
//   window:   innerWidth/innerHeight, matchMedia, addEventListener,
//             requestAnimationFrame
//   misc:     localStorage (inert, try/catch-safe), navigator.clipboard,
//             location
(function (g) {
  "use strict";

  function makeClassList() {
    var set = {};
    return {
      add: function (c) { set[c] = 1; },
      remove: function (c) { delete set[c]; },
      toggle: function (c) { if (set[c]) { delete set[c]; } else { set[c] = 1; } },
      contains: function (c) { return !!set[c]; }
    };
  }

  function El(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.childNodes = this.children;
    this.style = {};              // style.cssText / .left / .opacity etc: plain props
    this.dataset = {};
    this.classList = makeClassList();
    this._attrs = {};
    this._listeners = {};
    this.innerHTML = "";
    this.textContent = "";
    this.innerText = "";
    this.value = "";
    this.parentNode = null;
    this.onclick = null;
    this.open = undefined;        // <details> — read before ever being set
  }
  El.prototype.appendChild = function (c) {
    this.children.push(c);
    if (c && typeof c === "object") { c.parentNode = this; }
    return c;
  };
  El.prototype.insertBefore = function (c, ref) {
    var i = ref ? this.children.indexOf(ref) : -1;
    if (i >= 0) { this.children.splice(i, 0, c); } else { this.children.push(c); }
    if (c && typeof c === "object") { c.parentNode = this; }
    return c;
  };
  El.prototype.removeChild = function (c) {
    var i = this.children.indexOf(c);
    if (i >= 0) { this.children.splice(i, 1); }
    return c;
  };
  El.prototype.addEventListener = function (t, fn) {
    (this._listeners[t] = this._listeners[t] || []).push(fn);
  };
  El.prototype.removeEventListener = function () {};
  El.prototype.setAttribute = function (k, v) {
    this._attrs[k] = String(v);
    if (k === "id") { this.id = String(v); }
  };
  El.prototype.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
  };
  El.prototype.removeAttribute = function (k) { delete this._attrs[k]; };
  El.prototype.querySelectorAll = function () { return []; };
  El.prototype.querySelector = function () { return null; };
  El.prototype.getElementsByTagName = function () { return []; };
  El.prototype.contains = function () { return false; };
  El.prototype.closest = function () { return null; };
  El.prototype.matches = function () { return false; };
  El.prototype.getBoundingClientRect = function () {
    return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
  };
  El.prototype.focus = function () {};
  El.prototype.blur = function () {};
  El.prototype.click = function () {};
  El.prototype.remove = function () {};
  El.prototype.scrollIntoView = function () {};
  El.prototype.cloneNode = function () { return new El(this.tagName); };
  Object.defineProperty(El.prototype, "firstChild", {
    get: function () { return this.children[0] || null; }
  });
  Object.defineProperty(El.prototype, "lastChild", {
    get: function () { return this.children[this.children.length - 1] || null; }
  });

  var elements = new Map();
  var documentShim = {
    body: new El("body"),
    documentElement: new El("html"),
    createElement: function (t) { return new El(t); },
    createTextNode: function (t) { return { textContent: String(t) }; },
    getElementById: function (id) {
      if (!elements.has(id)) {
        var e = new El("div");
        e.id = String(id);
        elements.set(id, e);
      }
      return elements.get(id);
    },
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    addEventListener: function () {},
    removeEventListener: function () {}
  };

  g.document = documentShim;
  g.window = g;
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
  if (!g.location) {
    try { g.location = { href: "https://localhost/", search: "", hash: "", pathname: "/" }; }
    catch (e) { /* ignore */ }
  }
  if (!g.requestAnimationFrame) {
    g.requestAnimationFrame = function (fn) { return setTimeout(fn, 0); };
  }
  if (!g.addEventListener) { g.addEventListener = function () {}; }

  // the probe appended by test_render_exec.py reads the recorded structure here
  g.__domshim = { elements: elements, El: El, document: documentShim };
})(globalThis);
