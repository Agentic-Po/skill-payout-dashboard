# Minds Treasury Wallet Dashboard

Public, facts-first dashboard for the Minds Treasury Distribution wallet
(`0xBD956171F5B50936f0Ad1C4db80c022bd2442519` on Base), live at
**https://agentic-po.github.io/skill-payout-dashboard/** with a private
Telegram digest and alert channel.

> Maintainer rule: **README changes ride with behavior changes.** If a PR
> changes what a number means, this file changes in the same PR.

## Pipeline

```
GitHub Actions cron (3,18,33,48 * * * *  — 4x/hour, best-effort)
  └─ refresh.py
       ├─ chain fetch: Blockscout v2 → eth_getLogs fallback → 24h cross-check
       │  (monthly shards in transfers/, transfers_in/, cognition_in/)
       ├─ same fetch path, second wallet: the Coupon Distributor
       │  (coupon_out/, coupon_in/ — a wallet funded outside the treasury,
       │   in NO treasury figure; renders coupon.html + coupon_data.json)
       ├─ day-pinned rate oracle (day_rates.json — closed days never reprice)
       ├─ balance reconciliation (block-pinned, per-token drift fences)
       ├─ renders index.html (+ frozen legacy.html) from template.html
       ├─ writes data.json           ← THE versioned contract (schema_version 3)
       ├─ writes guard_private.json  ← private (gitignored, Actions cache only)
       ├─ writes transfers_export.csv (per-tx audit: tx_hash + log_index + class)
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
| committed | data.json, index.html, legacy.html, coupon.html, coupon_data.json, shards, CSV, day_rates.json, stats_history.json | public by design — strict subset of the page |
| Actions cache (`alert-state-*`) | alert_state.json (via `state.py`, atomic single-writer), guard_private.json | detector state & per-wallet signal rows are **never** committed — publishing them hands abusers a calibration oracle. Worst case on cache loss: one duplicate alert, never silence. |
| repo secrets | TELEGRAM_*, LEDGER_*, HEALTHCHECK_URL, POSTHOG_API_KEY | never in code or artifacts |

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

Residual exposure, stated honestly: (a) pre-2026-08-29 git history still
contains old committed state files (stale, but retrievable) — removing them
needs a history rewrite, open item 2 below; (b) the Actions cache holding
guard_private.json is branch-scoped and not downloadable by outsiders, but it
is a broader surface than repo secrets — treat its contents accordingly.

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
  cooldown)**: *credit-grant spike* — trailing-24h credit grants ≥ $20,000
  AND ≥ 3× the prior 24h (the 1–15 Sep replay peaked at $13,483 / 33× on the
  11 Sep batch, so routine $3-credit batches stay silent and show in the
  daily instead; only a runaway job fires); *first-ever creator-wallet
  surge* — ≥ 10 wallets paid their first-ever skill reward in the trailing
  24h AND > 50% of wallets paid in that window (replay max: 4 / 52)
- **Creator-reward cap (v2)**: `cap_detect.py` — per-WALLET clock-hour and
  rolling-60-min unit counters (equip 1, invoke 0.1) since the 14 Sep resume
  (a per-wallet check is a lower bound: the chain shows wallets, not accounts);
  🔴 breach, 🟠 straddle / saturated / fan-out, 📈 pool fence, 🟠 oracle
  disagreement (grid agreement < 0.5) — all edge-triggered with cooldowns,
  Telegram + private state only; a heartbeat older than 2h turns the
  workflow red via `alive_check.py`
- **Degradation**: data-source incomplete flips, stale-page banners
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
| 🔴 BLOCK / PAGE | publish blocked, or a money-relevant detector fired (cap C1–C6, Tukey outflow, ≥ $5k transfers, retired category, repeat system top-up, credit-grant spike, first-ever surge, **float below 7 d / 3 d** at the 7d pace) | its own Telegram message, immediately; the workflow failure notice names the gate and the tier |
| 🟠 WARN | degraded but published (rebate swap overdue, ledger/state mismatch, data source degraded/recovered, oracle agreement restored, coupon leg > 120 s, sanity drift near a bound, peer catalog not fetched) | `state.warn(key, text)` → `alert_state.json` (private cache); the next hourly/daily digest carries it, at most one per key per 6 h, dropped after 24 h unsent |
| LOG | everything else | run log only |

The private anomaly pass (repeat credit grants bucketed 1 / 2–5 / 6–10 /
11–50 / >50 with wallet and USD counts, the top-20 wallets by grant count,
and the 60-day daily distinct-grant-wallet series) is banked by `sanity.py`
into `guard_private.json` only; the daily digest carries ONE private line
("Repeat grants: …"). No account identifiers anywhere — the chain shows
wallets. `tools/replay_detectors.py` replays the current detector rules over
the August farm and prints, plainly, whether they would have fired.

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
| `moca-ledger detection floor` | moca-ledger `crawl.yml` / `selftest.yml` | 1 h | **2 h (INTERIM — revisit 2026-09-13)** | `HC_PING_URL` (moca-ledger) |
| `skill-payout-dashboard refresh` | this repo's `refresh.yml` | 1 h | 3 h | `HEALTHCHECK_URL` (this repo) |

- `moca-ledger detection floor` was previously named "My First Check"; period
  and ping URL are unchanged.
- **The 2 h detection-floor grace is interim, not a target.** It accommodates
  GitHub cron starvation rather than fixing it; the real fix is an external
  trigger on the `workflow_dispatch` endpoint, after which grace returns to
  ~45 min. **Revisit 2026-09-13** — recorded here, not only in the decision
  notes, so "temporary" does not become permanent.
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

1. `HEALTHCHECK_URL` secret for the `skill-payout-dashboard refresh` check
   (external dead-man; weekly digest nags until set) — ingest via the dotfile,
   see the table above
2. Whether to rewrite public git history (pre-2026-08-29 commits contain old
   state files; content is stale but recoverable)
3. transfers_export.csv monthly sharding before it nears the 50 MB tripwire
