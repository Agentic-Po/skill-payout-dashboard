// tests/render_harness.js — runs a page's inline scripts under tests/domshim.js
// in ONE shared node vm context per run (as a browser shares one realm
// across a page's <script> blocks), once per requested run.
//
//   node tests/render_harness.js <config.json>
//
// config: {shim, tree, scripts: [src...], data: "<json text>", runs: [run...]}
// run:    {mutation?: {path: [...], op: "delete"|"null"|"nan"|"string"},
//          now?: ms, rawData?: "<text>", dump?: "summary",
//          after?: [{advance: ms}|{setNow: ms}|{visibility: "visible"|"hidden"}],
//          probe?: "<js expression source>"}
// prints one JSON line: [{uncaught, renderErrors, dump, probe, applied}...]
//
// A script that throws is recorded as UNCAUGHT and window.onerror / "error"
// listeners are invoked (the browser behaviour), then the next script runs.
"use strict";
const fs = require("fs");
const vm = require("vm");

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const shim = fs.readFileSync(cfg.shim, "utf8");

// applies one mutation to the parsed data; returns a description of the leaf
// actually touched (the "nan"/"string" ops pick the FIRST numeric leaf, DFS)
const MUTATOR = `
globalThis.__mutate = function (D) {
  const M = globalThis.__MUTATION;
  if (!M) return D;
  let parent = null, key = null, node = D;
  for (const k of M.path) { parent = node; key = k; node = node == null ? undefined : node[k]; }
  if (M.op === "delete") { if (parent && key != null) delete parent[key]; globalThis.__APPLIED = M.path.join("."); return D; }
  if (M.op === "null") { if (parent && key != null) parent[key] = null; globalThis.__APPLIED = M.path.join("."); return D; }
  // first numeric leaf under node (DFS, arrays by index)
  const seen = [];
  function find(o, p) {
    if (typeof o === "number") return p;
    if (o && typeof o === "object") {
      for (const k of Object.keys(o)) { const r = find(o[k], p.concat([k])); if (r) return r; }
    }
    return null;
  }
  const leaf = typeof node === "number" ? [] : find(node, []);
  if (!leaf) { globalThis.__APPLIED = null; return D; }
  const full = M.path.concat(leaf);
  let pp = D; for (const k of full.slice(0, -1)) pp = pp[k];
  const lk = full[full.length - 1];
  pp[lk] = M.op === "nan" ? NaN : "n/a";
  globalThis.__APPLIED = full.join(".");
  return D;
};`;

const results = [];
for (const run of cfg.runs) {
  const logs = [];
  const sink = (...a) => { logs.push(a.map(String).join(" ")); };
  const ctx = vm.createContext({ console: { log: sink, warn: sink, error: sink, info: sink, debug: sink } });
  ctx.__DOM_TREE = cfg.tree || null;
  if (typeof run.now === "number") ctx.__CLOCK_NOW = run.now;
  vm.runInContext(shim, ctx, { filename: "domshim.js" });
  ctx.__DATA_JSON = cfg.data;
  ctx.__MUTATION = run.mutation || null;
  vm.runInContext(MUTATOR, ctx, { filename: "mutator.js" });
  cfg.scripts.forEach((src, i) => {
    let code = src;
    if (code.includes("/*__DATA__*/")) {
      const slot = run.rawData != null ? run.rawData
        : run.mutation ? "__mutate(JSON.parse(__DATA_JSON))" : cfg.data;
      code = code.split("/*__DATA__*/").join(slot);
    }
    try {
      vm.runInContext(code, ctx, { filename: "script[" + i + "]" });
    } catch (e) {
      ctx.__reportUncaught(e, "script[" + i + "]");
    }
  });
  for (const a of run.after || []) {
    try {
      if (a.advance != null) ctx.__clock.advance(a.advance);
      if (a.setNow != null) ctx.__clock.now = a.setNow;   // jump WITHOUT firing timers
      if (a.visibility) {
        ctx.document.hidden = a.visibility === "hidden";
        ctx.document.visibilityState = a.visibility;
        ctx.__fire(ctx.document, "visibilitychange", {});
      }
    } catch (e) { ctx.__reportUncaught(e, "after"); }
  }
  let probe = null;
  if (run.probe) {
    try { probe = vm.runInContext("(" + run.probe + ")", ctx, { filename: "probe.js" }); }
    catch (e) { probe = { __probe_error: String(e && e.message || e) }; }
  }
  const d = ctx.__domshim.dump();
  let re = null;
  try { re = ctx.__renderErrors ? JSON.parse(JSON.stringify(ctx.__renderErrors)) : null; } catch (e) { re = null; }
  let dumpOut = { ids: d.ids, writes: d.writes };
  if (run.dump === "summary") {
    const ids = {};
    for (const [k, v] of Object.entries(d.ids)) {
      ids[k] = { hidden: v.hidden, display: v.display, detached: v.detached, removed: v.removed,
                 textLen: (v.aggText || "").length, tag: v.tag, static: v.static,
                 placeholder: /section is unavailable right now/.test(v.aggText || ""),
                 bad: /NaN|Infinity|\[object Object\]/.test(v.aggText || "") };
    }
    dumpOut = { ids: ids };
  }
  results.push({ uncaught: d.uncaught, renderErrors: re, dump: dumpOut,
                 probe: probe == null ? null : JSON.parse(JSON.stringify(probe)),
                 applied: ctx.__APPLIED == null ? null : ctx.__APPLIED, logs: logs.slice(0, 50) });
}
process.stdout.write(JSON.stringify(results));
