# Minds Treasury Wallet Dashboard

Public, facts-first dashboard for the Minds Treasury Distribution wallet
(`0xBD956171F5B50936f0Ad1C4db80c022bd2442519` on Base), live at
**https://agentic-po.github.io/skill-payout-dashboard/** with a private
Telegram digest and alert channel.

> Maintainer rule: **README changes ride with behavior changes.** If a PR
> changes what a number means, this file changes in the same PR.

## Pipeline

```
Cloudflare Worker `dashboard-cron-worker` (:07/:37, POSTs workflow_dispatch —
the real trigger; RUNBOOK §7) + GitHub Actions cron (3,18,33,48 * * * *,
best-effort backup, delivers only a few % of slots under starvation)
  └─ refresh.py
       ├─ chain fetch: Blockscout v2 → eth_getLogs fallback → 24h cross-check
       │  (monthly shards in transfers/, transfers_in/, cognition_in/)
       ├─ same fetch path, second wallet: the Coupon Distributor
       │  (coupon_out/, coupon_in/ — a wallet funded outside the treasury,
       │   in NO treasury figure; renders coupon.html + coupon_data.json)
       ├─ day-pinned rate oracle (day_rates.json — closed days never reprice)
       ├─ balance reconciliation (block-pinned, per-token drift fences)
       ├─ renders full.html (the full page — committed + tested, NOT served:
       │  _config.yml excludes it; the private edition renders the same template)
       │  and index.html (one-screen public summary = util.public_summary(data.json))
       ├─ writes data.json           ← THE versioned contract (schema_version 4)
       ├─ writes guard_private.json  ← private (gitignored, encrypted Actions cache only)
       ├─ writes exports/transfers-YYYY-MM.csv (per-tx audit, one file per UTC month,
       │  sorted; closed months byte-stable) + transfers_export.csv (trailing 7 days)
       └─ catalog.build() → catalog.json + DATASETS.md (measured, never typed)
  └─ alerts.py   reads data.json → anomaly / ≥$5k / rebate-swap / retired-payout
  │              / creator-reward cap + sybil detectors (cap_detect.py)
  └─ notify.py   reads data.json → Telegram digest (hourly gate: ≥50 min apart)
daily.yml  (01:30 UTC)  → staleness check (fails loud >3h) + daily digest
weekly.yml (Mon 01:00)  → weekly digest · health alert (always()) · heartbeat
```

## The one-classifier rule

**Every published figure comes from `classify.py`** — page, Telegram, CSV.
The classifier is **era-aware** (`classify.ERAS`; the ROW timestamp selects
the era, never the run date), so `classify_usd(usd, ts)` and `band(usd, ts)`
both take the row's timestamp:

- **v1** (until 2026-08-21T13:55Z): micro <$0.06 · invoke ≈$0.10 · equip ≈$1 (±8%)
- **pause** (to 2026-09-14T14:19Z): no reward sizes; a ≈$1 is a **system free
  top-up** (`growth`, fine `$1 system free top-up` — a growth grant, not
  creator earnings); a $0.10 is `invoke (retired)`
- **v2** (from 2026-09-14T14:19Z, Creator Rewards v2): micro <$0.003 ·
  invoke ≈$0.005 · equip ≈$0.05 (±12%); system free top-ups (≈$1) continue, rare
- $3 credit / $5 referral (±8%) in every era
- Stripe packs $10/$20/$25/$50/$100 matched on the **fee-adjusted** value
  (÷0.94, ±15%) — deliveries land ~6% short of the pack price
- everything else is **nonstandard** (swaps, treasury moves) and is *excluded*
  from economy figures, reported as the "ops" residual so totals always close
- **one grouping layer** (`classify.group_for(cat, fine)`) buckets every row
  as `skill_rewards` (to creator wallets) · `credit_grants` ($3 / $5) ·
  `system_topups` (≈$1) · `topups_delivered` (Stripe packs) · `ops`
  (nonstandard) · `micro` (dust). Page tiles, the exec summary, Telegram and
  `tests/test_parity.py` all call it — no surface hand-lists categories, and
  the six sums close on total outflow to the cent. There is deliberately **no
  paid-vs-free / backed-vs-unbacked split anywhere**: the chain cannot see the
  source of a user's credit, so no figure claims to (Po rule 5, 2026-09-15)
- pricing is **day-pinned** via `pin_rate()` (carry-forward/back), never the
  live rate — history cannot reprice with the market. Since 2026-09-14 a
  newly closed day is priced ONLY from its market close (the implied leg
  back-solved 09-14 at 2.00x from the new $0.05 cluster and is retired as a
  pricer; a refresh refuses to seal an implied-priced day after that date);
  today is provisional from the running market candle (`market-open`) or
  carried forward.

Kerckhoffs is accepted deliberately: every `*.py` here is public, so the cap
and sybil detector thresholds are readable. They are absolute unit-based
rules — knowing the rule does not let a farmer earn more than the cap — and
the per-creator state they produce stays private (Actions cache only).

Facts (balances, flows, transfers) are Layer 1; anything inferred from size
is Layer 2 and badged "AI-inferred" on the page. Pack-sized transfers are
size-inferred and may include coupon-delivered credits — **not verified
revenue** (the Stripe ledger is not read; a one-time verified snapshot lives
in `stripe_snapshot.json`).

## State & privacy invariants

| Where | What | Why |
|---|---|---|
| committed | data.json, index.html (public summary), full.html + coupon.html (not served by Pages), coupon_data.json, shards, CSV, day_rates.json, stats_history.json | public by design — no identities; names live only in the private edition |
| Actions cache (`alert-state-enc-*`) | **encrypted only** (since 2026-09-27): alert_state.json.enc (via `state.py`, atomic single-writer), guard_private.json.enc, private_log.json.enc | detector state & per-wallet signal rows are **never** committed — publishing them hands abusers a calibration oracle. Any workflow in the repo can restore this cache, so it holds only `tools/state_crypt.py` blobs (AES-256-CBC/PBKDF2 via `openssl` + HMAC-SHA256, key = `STATE_KEY`). Worst case on cache loss: one duplicate alert, never silence. |
| public Actions logs | number-free status only ("alerts: detectors ran", "SANITY: N exact ok …", "alert liveness: ok") | anyone can read a public repo's run logs (retention **7 days** as of 2026-09-27). Inside Actions every detector/monitoring detail line goes through `privlog.private_print` — withheld from the log, banked to private_log.json (encrypted cache, last 5,000 lines); locally it prints as before |
| repo secrets | TELEGRAM_*, LEDGER_*, HEALTHCHECK_URL, POSTHOG_API_KEY, **STATE_KEY** | never in code or artifacts |

CI enforces this in `check_publish.py`. `--scan` fails the run if per-wallet
detector fields, **monitoring-status counts** (`flagged_n`, `monitored_n`,
`at_risk_usd` — private since 2026-09-15, digest-only), the cap switch-on
instant, or identity strings reach a public artifact, if a detector
field or a review/flagged status ever appears next to an address in one, or if
a monitored address turns up in a **curated** surface (`DATASETS.md`,
`README.md`, `catalog.json`, `data.json`'s registry) without being on the
hand-maintained `publish_allow_addrs.txt`. The invariant is *no public
artifact may reveal an address's monitoring status* — deliberately not "no
monitored address may appear": `facts.top_recipients` ranks counterparties by
USD received, which anyone can recompute from the shards this repo publishes,
so redacting a wallet from it would hide nothing while breaking the page. `--stage` replaced `git add -A` with an **explicit
allowlist**: gitignoring a private file is no longer the only thing standing
between it and publication, and any working-tree file that is neither ignored
nor listed gets a loud warning in the log — so a new state file is *noticed*
rather than silently published (the `add -A` risk) or silently dropped (the
2026-07-19 outage class).

**Encrypted cache (2026-09-27).** refresh/daily/weekly each run
`actions/cache/restore` (only the `.enc` files) → `state_crypt.py decrypt` →
the job → `state_crypt.py encrypt` → `actions/cache/save` (only the `.enc`
files). Encrypt and save run `always()` but only after a SUCCESSFUL decrypt —
the old implicit `actions/cache` post-step saved on job success only, so a
failed gate used to discard that run's state; now it is kept, and a missing
or wrong `STATE_KEY` fails the job loudly (`::error::state_crypt …`) without
writing or saving anything (all-or-nothing: state is never replaced by empty
data, never saved in plaintext). A one-time migration step restores the old
plaintext `alert-state-*` entry only while no encrypted entry exists; the
first save re-stores it encrypted. `.enc` files and private_log.json are
gitignored and asserted never staged (`tests/test_state_crypt.py`, which
also holds all three workflows' cache path lists to `state_crypt.FILES`).
Rotating `STATE_KEY`: FIRST delete every `alert-state-enc-*` cache entry
(`gh cache list --key alert-state-enc-` then `gh cache delete <id>`), THEN set
the new secret — an old entry under a new key fails its HMAC and BLOCKS every
refresh until deleted. Result: one cold start (at most one duplicate alert).
Generate with `openssl rand -base64 48`; the owner's copy lives in
`~/.moca-ledger/state_key` (mode 600).

**Quiet public logs (2026-09-27).** `privlog.private_print` is the ONE switch
(no other module reads `GITHUB_ACTIONS`): fence/runaway metrics, first-ever
wallet counts, creator-reward share, cap probe, WARN-queue keys, sanity's
grant-bleed / anomaly-pass lines, liveness counters and heartbeat age, ledger
peer age, digest cadence and dry-run bodies, run-duration banking. The
digest / `state.warn` / guard_private.json still carry what they always
carried; lines that used to live only in the log go to private_log.json. The
C1–C6 promotion `::warning::` (it printed the share) now queues a WARN for the
digest instead. `::error::` annotations carry no monitoring numbers.
`tests/test_quiet_logs.py` (CI) runs sanity / alerts / notify / alive_check
with `GITHUB_ACTIONS=true` against a deny list, and again without it to prove
the detail still prints locally. Run logs from before 2026-09-27 age out
under the 7-day retention.

Residual exposure, stated honestly: (a) pre-2026-08-29 git history still
contains old committed state files (stale, but retrievable) — removing them
needs a history rewrite, open item 2 below; (b) the Actions cache is
branch-scoped and not downloadable by outsiders, and its contents are now
ciphertext — the key is a repo secret, so the residual surface is whoever can
read secrets (a workflow change that echoes `STATE_KEY` would still expose it;
review workflow diffs accordingly); (c) the legacy plaintext cache entries
remain until evicted (7 days unused) unless deleted by hand
(`gh cache list --key alert-state-` / `gh cache delete`).

## Alerting

- **Anomaly**: trailing-1h outflow > median + 3×IQR of the full day-pinned
  hourly history (edge-triggered, 6h cooldown)
- **Large transfers** ≥$5k (live-priced deliberately), deduped 48h
- **Rebate wallet**: weekly MENTE→MOCA swap overdue (>8d, ≥$500 unswapped)
- **Retired payouts**: any transfer matching a retired category
  (`classify.RETIRED`, today the v1 $0.10 invoke after 2026-08-21T13:55Z) —
  chain-recomputed ledger in guard_private.json is the record; alert state is
  just dedup (30d window). The system free top-up (`classify.SYSTEM_TOPUP`,
  ≈$1) is legitimate but shape-constrained — one per wallet; a wallet
  receiving a second one fires "Repeat system free top-up to one wallet"
- **Edge alerts (2026-09-15, `alerts.py`, edge-triggered with a 24h
  cooldown)**: *first-ever creator-wallet surge* — ≥ 10 wallets paid their
  first-ever skill reward in the trailing 24h AND > 50% of wallets paid in
  that window (replay max: 4 / 52); WARN tier since loop 2 (a creator-reward
  rule). The $20,000 / 3× credit-grant spike rule was retired in loop 2 —
  lifetime grant spend is ~$105K, so it could never fire
- **Per-category outflow fences + runaway rule (loop 2, 2026-09-21)**:
  `fences.py` — the single treasury Tukey fence split by `classify.group_for`
  (skill rewards / credit grants / top-ups delivered / ops), each median +
  3×IQR over its own trailing 30 d with `fences.INCIDENT_WINDOWS` (August
  farm, 15 Sep oracle, 18–20 Sep rounding) EXCLUDED from every baseline; and
  a *runaway payouts* rate rule on economy payouts (≥ 3× the 14-day median,
  last 12 h ≥ 2× the prior 12 h, spread across the day, ≥ $2,000) — 26 h
  lead on the August farm in replay, silent on 15 Sep and 18–20 Sep. Every
  tier is assigned from the MEASURED false-fire count of a full-history
  replay (`tools/replay_detectors.py`): all WARN today
- **Creator-reward cap (v2)**: `cap_detect.py` — per-WALLET clock-hour and
  rolling-60-min unit counters (equip 1, invoke 0.1) on each row's OWN era
  grid (era-aware since loop 2: a v1 $1 equip counts exactly as a v2 $0.05
  equip; a farm in any era is visible) — a per-wallet check is a lower
  bound: the chain shows wallets, not accounts; 🟠 breach / straddle /
  saturated / fan-out, 📈 pool fence, 🟠 oracle disagreement (grid agreement
  < 0.5) — all edge-triggered with cooldowns, WARN tier while creator
  rewards are < 2% of outflow (0.4% today; `cap_detect.CREATOR_REWARD_TIER`
  is the deliberate switch), private state only; a heartbeat older than 2h
  turns the workflow red via `alive_check.py`
- **Degradation**: data-source incomplete flips, stale-page banners (90 min, see "Page render safety")
  (client-side, works when the pipeline is fully dead), daily >3h staleness
  fail-loud, weekly dead-man health check

## Sanity gate — monitor-of-the-monitor (`sanity.py`, 2026-09-21)

Council verdict after three weeks live: six detectors never fired while four
real incidents shipped (blank charts under green checks, a shadowed-import
crash, a day rate at exactly 2x its market close, a rounding nudge that paged
Po on 7 of ~22 runs). The pipeline could not tell "the treasury is fine" from
"the monitor is wrong". `sanity.py` recomputes the headline facts from the RAW
shards + `day_rates.json` by a path that never imports `refresh.py`, diffs them
against the published `data.json`, and runs as a BLOCKING step immediately
before the commit in `refresh.yml` (offline in `ci.yml`). Runtime ~1 s offline,
~5 s with the live legs.

| Tier | Check | Bound | On failure |
|---|---|---|---|
| EXACT | out row count · out wallet count · raw MOCA/MENTE totals per window (24h/7d/30d/all) · in row count · group `n` per window · closed-day count (`day_digests.json`) · `schema_version` present · every window's group keys | any difference | BLOCK — exit 1, nothing publishes, 🔴 Telegram names the gate |
| BOUNDED | USD totals and group USD per window | max(0.5 %, $1) | BLOCK over the bound, LOG under it |
| BOUNDED | `balance_usd` vs an independent `eth_call` × published live rate (online only) | 1.5 % | BLOCK / LOG |
| BOUNDED | `float.days_7d_pace` (runway) | 5 % | BLOCK / LOG |
| BOUNDED | day rate vs the day's market close, trailing 30 days; open-day rate vs the running candle (`MARKET_AGREE`) | 6 % | BLOCK / LOG |
| BOUNDED | published live rate vs an independent DexScreener quote (online only) | 6 % | BLOCK / LOG |
| LOG | any difference under its bound — including the cent-scale rounding drift of Sep 18-20 | — | one line in the run log, never pages |
| WARN | a bounded check at ≥ 50 % of its bound; a live leg that could not run; a degraded peer catalog | — | queued via `state.warn()`, carried by the next digest |

Every run prints `SANITY: N exact ok, M bounded ok, K logged drift` plus one
detail line per non-clean check. `tests/test_sanity.py` proves the tiers on
seeded copies of the real tree: clean passes, a 2x price blocks, a dropped
row blocks, a one-cent drift only logs.

### Severity tiers across the whole alert surface

| Tier | Meaning | Delivery |
|---|---|---|
| 🔴 BLOCK / PAGE | publish blocked, or an **event notice** fired — a specific actionable event rather than an anomaly guess (≥ $5k transfers, retired category, repeat system top-up, **float below 7 d / 3 d** at the 7d pace). Event notices are expected on ordinary days (the replay shows ≥ $5k inflow on 12 of them, each a real funding arrival). An **anomaly detector** reaches this tier only with zero ordinary-day fires in the full-history replay; today none qualify, so all of them sit at WARN | its own Telegram message, immediately; the workflow failure notice names the gate and the tier; `RUNBOOK-deadman.md` §8 says what the recipient does and who can pause |
| 🟠 WARN | a detector that fires on ordinary days in the replay, or watches < 2 % of outflow (cap C1–C6, the four per-category outflow fences, the runaway payouts rule, first-ever surge), plus degraded-but-published notices (rebate swap overdue, ledger/state mismatch, data source degraded/recovered, oracle agreement restored, coupon leg > 120 s, sanity drift near a bound, peer catalog not fetched, a new 30-day high of the private grant-bleed share) | `state.warn(key, text)` → `alert_state.json` (private cache); the next hourly/daily digest carries it, at most one per key per 6 h, dropped after 24 h unsent |
| LOG | everything else | run log locally; inside Actions detector/monitoring LOG lines go to private_log.json (encrypted cache) and the public log gets a number-free status line (`privlog.private_print`) |

The private anomaly pass (repeat credit grants bucketed 1 / 2–5 / 6–10 /
11–50 / >50 with wallet and USD counts, the top-20 wallets by grant count,
and the 60-day daily distinct-grant-wallet series) is banked by `sanity.py`
into `guard_private.json` only; the daily digest carries ONE private line
("Repeat grants: …"). No account identifiers anywhere — the chain shows
wallets. Loop 2 adds the slow-bleed measurement (in `fences.py`: the
rolling 7-day share of grant $ going to wallets that already held a grant,
against its own trailing 30-day history, and the wallets crossing 5 / 20
lifetime grants this week) — one more private daily line, never a page, not
a sybil claim. `tools/replay_detectors.py` replays every current rule over
ALL history, counts fires per day (a fire outside a known-incident window
is a false fire, and those counts assign the tiers) and prints a T-minus
table per incident with the ~30 min refresh-cadence bound.

## The first screen (council loop 1, 2026-09-27)

Built for one decision — how much to refill this wallet, and when. In order:
7-day exec summary → lifetime sentence → tiles (each with a one-line plain
definition, no new numbers) → **Month by month**: payouts out (excl. swaps) ·
swaps & ops out · refills in · returns in · biggest item, all straight from
`facts.monthly`. Rules the card keeps: swaps are their own column so a
swap-heavy month never reads as spend; partial months carry "N of M days" (the open month: "to <day> <time> UTC") and
no ratio compares a partial month with a full one; the biggest item carries
its size-inferred band **in the cell**; no forecast, runway date, scenario or
recommended amount is published (council ruling — those are the owner's
analysis, not page facts). Top creator wallets and the address registry are
collapsed below the fold. `tests/test_page.py` holds the card to the cent
(months close per column, sum to all-history, closed months = their sealed
days), pins the first-screen order, and lints public surfaces for ruled-out
inflow wording ("new/external funding", "top-ups in" — inflow is "refills" and
"returns"; "top-ups" names Stripe packs delivered OUT).

## Page render safety (council loop 2, 2026-09-27)

The page is one inline script; before loop 2 a single missing data key threw
at top level and blanked every section below it with nothing on screen saying
so (reproduced: deleting or nulling `scope`, `facts`, `infer`,
`facts.windows/monthly/daily` or `infer.guard` blanked 4–31 of 31 content
blocks). Now:

- **Section isolation.** Every section runs in `renderSection(name, cardId,
  fn)`. A throw — or a rendered `NaN` / `Infinity` / `[object Object]` —
  wipes that card to "This section is unavailable right now — the rest of
  the page is unaffected." (never half-written; no stack trace), records
  `{name, msg}` in `window.__renderErrors`, and raises the page banner "Part
  of this page could not be displayed — do not rely on it for decisions
  until it recovers." `window.onerror` is the last resort for anything
  outside a section. A missing optional block (no `sink`, no `stripe_snap`)
  still just hides its card — that is design, not an error.
- **Strict formatters.** `fmt` / `fmt0` / `usd` / `usd2` / `num` (and every
  direct `.toLocaleString()` / `.toFixed()` on a data field) throw on null,
  undefined, NaN or a non-number; a real 0 renders. Audited on the live
  data.json before switching: zero non-finite inputs.
- **Fail closed.** "This page is still loading or failed to load …" is static
  markup, visible by default; the script's LAST statement removes it only if
  `window.__renderErrors` is empty. A syntax error, invalid inlined JSON or an
  uncaught throw leaves it up.
- **Stale banner** at **90 min** past `scope.generated_iso` (was 2.5 h; 75 min
  would flash ~1x/day on healthy pages per a 976-run replay),
  re-checked every 60 s and on `visibilitychange`; a page with no readable
  build time says its freshness cannot be checked.
- **Accessibility.** Creator-window tabs and the USD/count toggle are
  `<button aria-pressed>`; one `<main>`; `th scope="col"` everywhere; chart
  SVGs are `role="img"` with a label; Escape closes tooltips; daily rows are
  no longer tab stops; `:focus-visible` outline; `prefers-reduced-motion`
  honoured. Band chips carry their hue as a left stripe with body-ink text;
  the old `#d33` is the `--bad` token. Contrast ≥ 4.5:1 in both themes.

Gates (all in `ci.yml`, all driven by `tests/render_harness.js` +
`tests/domshim.js`, which runs a page's scripts in one shared realm with a
fake clock, timers, events and the page's own id tree):
`tests/test_render_exec.py` (real data: zero `__renderErrors`, loading banner
removed, stale-banner clock test, fail-closed cases) ·
`tests/test_render_mutation.py` (every top-level key plus `facts.windows /
monthly / daily / balance_series` and `infer.guard`, each deleted, nulled,
first numeric leaf → NaN and → string: only the sections that read the key may
degrade — declared in its `DEPENDS` map — no uncaught error, banner iff a
failure) · `tests/test_figure_parity.py` (every digit-bearing rendered text
node, ages masked, identical between `origin/main`'s template and the working
one on the same data.json; an intended figure change runs with
`FIGURE_PARITY_ALLOW=1` and says so in the PR) · `tests/test_a11y.py`
(controls, landmarks, scopes, SVG labels, contrast computed from the CSS
tokens). A new section needs a `renderSection` wrapper and a `DEPENDS` /
`SECTIONS` entry, or the mutation gate fails.

## Changing code safely — the golden diff

`refresh.py --offline` is byte-deterministic run-to-run, so a code change can be
tested for exactly which published figures it moves:

```
python3 tools/golden.py origin/main
```

builds two throwaway copies of the working tree over the SAME data, puts
`origin/main`'s code in one, runs `--offline` in both and diffs every artifact
(JSON as key paths). A behaviour-preserving change must print `IDENTICAL`; an
intended change must show only the keys it meant to touch. It cannot see the
fetch legs (balances, rates, sink and PostHog are reused from the previous
data.json offline) — those need a live run.

## Changing the pipeline safely (council loop 3, 2026-09-27)

What each gate can and cannot prove, and where a figure lives.

| Change touches | Proven by | Blind spot |
|---|---|---|
| post-fetch code (facts, layer 2, render, CSV) | `tools/golden.py` — IDENTICAL or a key-path diff | nothing offline |
| order of the two STATE writes | `tests/test_state_order.py` + `tests/state_write_order.json` (trace via `REFRESH_TRACE_STATE=path`; golden holds head's trace to it) | the coupon write only runs online — pinned by static checks |
| pricing in the five loaders | `tests/test_pricing_parity.py` (all five on the real tree + fixtures) | refresh.py's row quantity is rebuilt by the test (only `day_rate` is extracted), and inflows are compared for `alerts` only |
| data.json shape, or a CLOSED day's figure | `tests/test_contract.py` + an entry in CONSUMERS.md (and a `schema_version` bump if a field changes meaning); `digests.enforce` hard-fails a repriced/vanished closed day unless `RESTATEMENTS.md` documents it | — |
| fetch code (crawl, rates, balances, sink, PostHog) | **nothing offline** — offline reuses the previous run's fetched values; needs a pure-move review + a watched live run (a live run cannot prove equality: the chain moves). Record/replay is the next cycle's first item | |

**Where a headline figure lives** (quick map — the glossary below has formulas):
windows → `refresh.py:facts_window` → `data.json facts.windows[]` → `test_parity.py`, `sanity.py` (EXACT);
month-by-month card → `facts_window` per month → `facts.monthly[]` → `test_page.py` (per-column closure, months = all-history, closed months = their days), `test_contract.py` (key set), `sanity.py` (section present);
runway/float → `facts.float` → `test_parity.py`, `sanity.py` (BOUNDED 5%);
day rates → `refresh.py` oracle + `day_rates.json` → `test_rate_stale.py`, `test_digests.py`, `sanity.py`;
exec summary → `refresh.py` (`exec_summary`) → `test_parity.py` (to the cent).
**Do not touch without a council:** `classify.py`, digest sealing (`digests.enforce`), the oracle backward walk, the STATE write order, the legacy taxonomy, `runway_adj` (public field in `stats_history`).

**One freshness threshold** — `freshness.py` `STALE_MINUTES = 90`: the page stale banner and coupon banner (`__STALE_MIN__` substituted at render), the exec-summary / coupon "data is N hours old" prefix, and the weekly health check (now in minutes; was 48 h). The daily staleness fail-loud (3 h, `daily.yml`) is deliberately looser — a coarse backstop, not a banner. `daily.yml` scheduled 01:30 UTC actually starts ~06:14 UTC (GitHub cron delay; only `refresh.yml` is Worker-triggered).

**Pricing parity — owner decisions pinned in the allow-list** (the test fails if one stops diverging, so the list cannot rot): (1) `notify`/`alerts` divide token quantities by `1e18` rather than `10**18` — ≤ 2 ulp (~1e-16 relative) on ~18.6K rows, no visible effect; (2) rows dated AFTER the open day: four loaders carry the open-day rate forward, `refresh.py` carries the last closed day (cannot occur while the open day is today); (3) a token with no closed rates yet: four loaders apply the open-day rate, `refresh.py` the live rate (bootstrap only).

**Other:** `util.py` holds the shared pure helpers (`hkt`, `iso_min`, `parse_dt`, `cooled`; no I/O, no STATE — `test_util.py`); `notify.hkt` stays separate because it also accepts strings. `rpc_batch` no longer drops a batch item silently: skips are counted by reason, the first five logged, and `rpc_batch_skips=N` rides the PHASE TIMING line.

## Figures glossary

Every exec-facing number, with the formula that produces it, what it is
measured from, and the bias it carries. `tests/test_parity.py` fails the build
if any figure here disagrees with the page, or if a declared figure loses its
entry. **If you are about to quote a number in a deck, quote the safe sentence.**

### `out_usd` — total outflow in the window
- **Formula**: `refresh.py:facts_window` — `sum(r["usd"] for r in rows)`, each row priced at its day-pinned rate (`refresh.py:day_rate`).
- **Source**: `transfers/` (outbound shards). **Coverage**: 2026-04-24 → now, 24h · 7d · 30d · all-history windows.
- **Bias**: MOCA + MENTE only. ETH, gas and any untracked token are invisible here.
- **Safe to say**: "The treasury sent $X of MOCA and MENTE out of this one wallet in the last 24 hours."

### `economy_out_usd` — the part of outflow that is the economy
- **Formula**: `refresh.py:facts_window` — `sum(r["usd"] for r in rows if r["cat"] != "nonstandard")`, where `cat` comes from `classify.py:classify_usd`. Equals every group below except `ops`.
- **Source**: same rows as `out_usd`. **Coverage**: same windows.
- **Bias**: membership is inferred from **transfer size**, not from a platform event. A swap that happens to land on $1.00 is counted as a payout.
- **Safe to say**: "Of that, $Y was payout-shaped activity — skill rewards, credit grants, system free top-ups and top-up deliveries."

### `ops_out_usd` — the residual
- **Formula**: `refresh.py:facts_window` — `out_usd - economy_out_usd`. Computed as a residual **by design**, so the two always sum to the total exactly.
- **Source**: same rows. **Coverage**: same windows.
- **Bias**: it is a residual, not a measurement. Anything mis-sized out of the economy lands here; a negative value would mean a basis mismatch and is flagged, never printed.
- **Safe to say**: "The rest was treasury logistics — swaps and internal moves, not user activity."

### `groups` — where the outflow went (the one grouping layer)
- **Formula**: `refresh.py:facts_window` — `data.json:facts.windows[].groups[g] = {n, usd, wallets}` for `g` in `skill_rewards` · `credit_grants` · `system_topups` · `topups_delivered` · `ops` · `micro`, each row assigned by `classify.group_for(cat, fine)`. The six sums close on `out_usd` to the cent; the non-ops five close on `economy_out_usd` (`tests/test_parity.py`).
- **Source**: `transfers/` priced day-pinned. **Coverage**: every window, every month.
- **Bias**: size-inferred on the row's own era grid (±8% v1 / ±12% v2). `skill_rewards` = invoke/equip-sized rows **to creator wallets** (a creator may hold several wallets); `credit_grants` = `$3 credit` + `referral $5`; `system_topups` = the ≈$1 `$1 system free top-up` after 2026-08-21T13:55Z (a growth grant, not creator earnings); `topups_delivered` = Stripe-pack-sized deliveries (size-inferred, may include coupon-delivered credits — **not** verified revenue); `micro` = sub-floor dust, shown only when non-zero.
- **Safe to say**: "$A went to creator wallets as skill rewards, $B was credit grants to users, $C system free top-ups, $D top-ups delivered, $E ops."

### `rewards_v2` — Creator Rewards v2 (from 2026-09-14T14:19Z)
- **Formula**: `refresh.py` — `data.json:facts.rewards_v2`: `usd_24h` / `usd_since_resume` = `sum(usd)` over rows since the resume whose fine class is `equip` or `invoke`; `n_equip_24h`, `n_invoke_24h`, `creator_wallets_24h`, `creator_wallets_since_resume` (distinct recipient wallets); `grid` = the era's reward sizes from `classify.ERAS`.
- **Source**: `transfers/` priced day-pinned. **Coverage**: 24h and since the resume.
- **Bias**: size-inferred at ±12%; per **wallet**, not per creator. Carries no cap figure, no cap instant, no headroom and no implied user spend (the chain cannot see how a user's credit was funded, so no figure reasons about it).
- **Safe to say**: "Creator wallets earned $Z at half the user price since the 14 Sep resume." Never quote a cap figure from the page.

### `creator_wallets` — top creator wallets, four windows
- **Formula**: `refresh.py` — `data.json:facts.creator_wallets.windows[w]` for `w` in `24h` · `7d` · `since_resume` (page default) · `all`: skill-reward rows grouped per recipient wallet → `wallets`, `usd`, `equips`, `invokes`, `top1/top5/top10_share_pct`, `median_usd` (null when fewer than 10 wallets), `new_wallets` (24h/7d only: first-ever reward inside the window) and a `top` list of 10 `{addr, usd, equips, invokes, first_seen}` (first_seen at **day** grain).
- **Source**: `transfers/` priced day-pinned. **Coverage**: the four windows; `all` spans **both** reward eras (v1 $1 equip / $0.10 invoke to 2026-08-21T13:55Z, so wallets paid at those larger sizes rank high there).
- **Bias**: per wallet, never per creator; never appended to `stats_history.json`; no hourly figure of any kind.
- **Safe to say**: "N creator wallets were paid in the window; the top wallet took X% of it."

### `in_usd` split — refills vs returns
- **Formula**: `refresh.py:facts_window` — `in_recycled_usd` = inflows from the collector (`in_collector_usd`) or the treasury reserve (`in_reserve_usd`); `in_external_usd = in_usd - in_recycled_usd`. Both identities hold to the cent (`tests/test_contract.py`).
- **Source**: `transfers_in/`, priced day-pinned. **Coverage**: every window, `prev24`, every month.
- **Bias**: classified by SOURCE ADDRESS only. Collector recycling stopped 2026-06-18, so recent `in_collector_usd` is 0; the reserve returned funds the treasury itself had parked there (2026-08-25 out, 09-04/11/18 back).
- **Naming**: `in_external_usd` is a legacy key name — it is **treasury refills from Minds/Animoca's own wallets**, never external money (Po, 2026-09-27: all inbound refills are ours). The page says "refills" and "returns" — never "top-ups" for inflow, which the page reserves for Stripe packs delivered OUT to users.
- **Safe to say**: "Of $X lifetime inflow — all our own treasury money — $Y was deliberate refills and the rest came back: $C recycled from the collector before June and $R returned from the reserve." Never "usage fees recycling back" for the whole returns figure, and never "new" or "external" funding.

### wallet balance
- **Formula**: `refresh.py:balance_at` — `eth_call` `balanceOf` per token at latest block, USD at the live rate; block-pinned copy at `RECON_BLOCK` drives the drift fence.
- **Source**: Base RPC. **Coverage**: point-in-time, per refresh; history in `stats_history.json`.
- **Bias**: **this wallet only.** Other Minds treasury wallets (incl. the rebate sink) are out of scope. On a failed fetch the digest falls back to the last non-null snapshot and marks it stale.
- **Safe to say**: "The distribution wallet holds about $B across MOCA and MENTE — this wallet only, not all of Minds."

### distribution float / runway — ONE number, total-outflow basis
- **Formula**: `refresh.py` — `data.json:facts.float`: `days_7d_pace = bal_usd / out_7d_avg_usd` (the headline everywhere: exec summary, hero strip, tile, Telegram) and `days_24h_pace = bal_usd / out_24h_usd` (yesterday's pace); `driver_24h` names the group that dominated the last 24h. `out_*` are **total** outflow (every category), never an "unbacked" subset. `stats_history.runway7` carries `days_7d_pace` from 2026-09-15.
- **Source**: wallet balance + day-pinned outflow history. **Coverage**: trailing 24h and 7d.
- **Bias**: **top-up cadence, not solvency.** It measures how long this one wallet lasts before someone refills it — it says nothing about company runway, and a single large swap in the window collapses it.
- **Safe to say**: "At the 7-day pace this wallet has about N days before it needs a top-up. That is a refill schedule, not a solvency number."

## Data catalog

`catalog.py` measures every dataset in this repo — rows, bytes and coverage
are computed off the files on every refresh, never hand-typed — and writes
`catalog.json` (machine) and [`DATASETS.md`](DATASETS.md) (human). Private
datasets appear there by name only — no path, no schema, no coverage and no
size (a byte count tracks how many wallets are flagged) — so their absence is
visible without their shape being published. `python3 catalog.py --check` fails CI if the committed
catalog disagrees with the data. `DATASETS.md` also aggregates the peer ledger
at [Agentic-Po/moca-ledger](https://github.com/Agentic-Po/moca-ledger); that
fetch is best-effort and degrades to a one-line note, so neither repo's CI can
break the other's.

Cross-crawler agreement is tested weekly by
`.github/workflows/reconcile.yml` → `tests/test_reconcile.py`, which compares
this repo's Blockscout-sourced outbound rows against moca-ledger's
`eth_getLogs`-sourced rows for the last three closed days.

## Dead-man checks — which check watches which pipeline

Two healthchecks.io checks, one per pipeline. Never one shared check: a shared
check cannot tell you which pipeline died. Full detail in
[`RUNBOOK-deadman.md`](RUNBOOK-deadman.md).

| Check name | Pipeline | Period | Grace | Secret |
|---|---|---|---|---|
| `moca-ledger detection floor` | moca-ledger `crawl.yml` / `selftest.yml` | 1 h | 2 h | `HC_PING_URL` (moca-ledger) |
| `skill-payout-dashboard refresh` | this repo's `refresh.yml` | 1 h | 3 h | `HEALTHCHECK_URL` (this repo) |

- `moca-ledger detection floor` was previously named "My First Check"; period
  and ping URL are unchanged.
- The moca-ledger check's 2 h grace is that repo's setting (its own runbook
  owns it); this table only records it.
- **The dashboard's 1 h/3 h numbers are dashboard-only.** They tolerate ~4 h of
  silence, which is fine for a refresh pipeline and must **never** be copied to
  the detection floor check.
- A `/fail` ping bypasses grace and alerts immediately — verify this in the
  healthchecks UI rather than assuming it; `selftest.yml` depends on it.
- **The healthchecks.io UI is the source of truth** for names, schedules and
  ping URLs. The dotfile `~/.moca-ledger/healthchecks_dashboard_ping_url`
  (mode 0600) is only a **cache** of the dashboard ping URL — after any
  rotation in the UI, rewrite the dotfile and re-run
  `gh secret set HEALTHCHECK_URL -R Agentic-Po/skill-payout-dashboard < ~/.moca-ledger/healthchecks_dashboard_ping_url`.
- moca-ledger's redundant no-op `HEALTHCHECK_URL` dead-man step was removed
  from its `crawl.yml` on 2026-08-30; that secret is deliberately **not**
  backfilled there.

## Open items owned by Po

Resolved 2026-09-27: `HEALTHCHECK_URL` set (dead-man live); CSV sharded
monthly; the five July page open items answered by the owner (no MENTE burn;
PostHog is the payments source of truth; funder identified; swept MENTE goes
Rebate wallet -> treasury reserve; refills are a manual owner budget request).

1. Public git history rewrite — approved 2026-09-27, being prepared (purges
   pre-2026-08-29 state files and identity strings; needs a one-off pause of
   the refresh bot and of the force-push rule).
2. Pricing unification across the five loaders — approved; next cycle, behind
   `tests/test_pricing_parity.py`.
3. Unexplained ~88K MENTE balance-vs-transfers gap (~$1,250) — not a burn.
