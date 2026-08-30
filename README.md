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
       ├─ day-pinned rate oracle (day_rates.json — closed days never reprice)
       ├─ balance reconciliation (block-pinned, per-token drift fences)
       ├─ renders index.html (+ frozen legacy.html) from template.html
       ├─ writes data.json           ← THE versioned contract (schema_version 1)
       ├─ writes guard_private.json  ← private (gitignored, Actions cache only)
       ├─ writes transfers_export.csv (per-tx audit: tx_hash + log_index + class)
       └─ catalog.build() → catalog.json + DATASETS.md (measured, never typed)
  └─ alerts.py   reads data.json → anomaly / ≥$5k / rebate-swap / retired-payout
  └─ notify.py   reads data.json → Telegram digest (hourly gate: ≥50 min apart)
daily.yml  (01:30 UTC)  → staleness check (fails loud >3h) + daily digest
weekly.yml (Mon 01:00)  → weekly digest · health alert (always()) · heartbeat
```

## The one-classifier rule

**Every published figure comes from `classify.py`** — page, Telegram, CSV:

- micro <$0.06 · invoke ≈$0.10 · equip ≈$1 · $3 credit / $5 referral (all ±8%)
- Stripe packs $10/$20/$25/$50/$100 matched on the **fee-adjusted** value
  (÷0.94, ±15%) — deliveries land ~6% short of the pack price
- everything else is **nonstandard** (swaps, treasury moves) and is *excluded*
  from economy figures, reported as the "ops" residual so totals always close
- pricing is **day-pinned** via `pin_rate()` (carry-forward/back), never the
  live rate — history cannot reprice with the market

Facts (balances, flows, transfers) are Layer 1; anything inferred from size
is Layer 2 and badged "AI-inferred" on the page. Pack-sized transfers are
size-inferred and may include coupon-delivered credits — **not verified
revenue** (the Stripe ledger is not read; a one-time verified snapshot lives
in `stripe_snapshot.json`).

## State & privacy invariants

| Where | What | Why |
|---|---|---|
| committed | data.json, index.html, legacy.html, shards, CSV, day_rates.json, stats_history.json | public by design — strict subset of the page |
| Actions cache (`alert-state-*`) | alert_state.json (via `state.py`, atomic single-writer), guard_private.json | detector state & per-wallet signal rows are **never** committed — publishing them hands abusers a calibration oracle. Worst case on cache loss: one duplicate alert, never silence. |
| repo secrets | TELEGRAM_*, LEDGER_*, HEALTHCHECK_URL, POSTHOG_API_KEY | never in code or artifacts |

CI enforces this in `check_publish.py`. `--scan` fails the run if per-wallet
detector fields or identity strings reach a public artifact, if a detector
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
  (`classify.RETIRED`) after its cutoff — chain-recomputed ledger in
  guard_private.json is the record; alert state is just dedup (30d window)
- **Degradation**: data-source incomplete flips, stale-page banners
  (client-side, works when the pipeline is fully dead), daily >3h staleness
  fail-loud, weekly dead-man health check

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
- **Formula**: `refresh.py:facts_window` — `sum(r["usd"] for r in rows if r["cat"] != "nonstandard")`, where `cat` comes from `classify.py:classify_usd`.
- **Source**: same rows as `out_usd`. **Coverage**: same windows.
- **Bias**: membership is inferred from **transfer size**, not from a platform event. A swap that happens to land on $1.00 is counted as an equip.
- **Safe to say**: "Of that, $Y was payout-shaped activity — invokes, equips, incentives and top-up deliveries."

### `ops_out_usd` — the residual
- **Formula**: `refresh.py:facts_window` — `out_usd - economy_out_usd`. Computed as a residual **by design**, so the two always sum to the total exactly.
- **Source**: same rows. **Coverage**: same windows.
- **Bias**: it is a residual, not a measurement. Anything mis-sized out of the economy lands here; a negative value would mean a basis mismatch and is flagged, never printed.
- **Safe to say**: "The rest was treasury logistics — swaps and internal moves, not user activity."

### `usd_ce` — paid to creators
- **Formula**: `notify.py:win` — `sum(usd for rows classified invoke or equip)`; the page's parity check is `tests/test_parity.py`.
- **Source**: `transfers/` priced day-pinned. **Coverage**: 24h (hourly/daily digest) or 7d (weekly digest), plus all-time.
- **Bias**: invoke ≈ $0.10 and equip ≈ $1 are matched at ±8% of the day-pinned USD. Equip was **retired 2026-08-21** (`classify.RETIRED`), so all-time and window figures are not the same mix.
- **Safe to say**: "$Z went to creator wallets for skill invokes and equips in the period."

### `usd_incent` — incentive spend
- **Formula**: `notify.py:win` — `sum(usd)` over rows whose fine class is `$3 credit` or `referral $5` (`classify.INCENT`, ±8%).
- **Source**: `transfers/` priced day-pinned. **Coverage**: 24h / 7d.
- **Bias**: size-inferred. A $3 payout that was not a credit is counted; a credit paid at an unusual size is not.
- **Safe to say**: "$W of growth incentives were paid out — $3 credits and $5 referrals."

### `usd_topup` — top-ups delivered
- **Formula**: `notify.py:win` — `sum(usd)` over rows whose fine class starts `stripe $` (`classify.classify_usd`: value ÷ 0.94 within ±15% of $10/$20/$25/$50/$100).
- **Source**: `transfers/` priced day-pinned. **Coverage**: 24h / 7d. Verified counterpart: `stripe_snapshot.json` (one-time).
- **Bias**: **size-inferred, may include coupon-delivered credits — NOT verified revenue.** The Stripe ledger is not read. `data.json:server.diverge_usd` tracks the open gap against PostHog.
- **Safe to say**: "$V of flows were Stripe-pack-sized deliveries. That is a size inference, not booked revenue."

### wallet balance
- **Formula**: `refresh.py:balance_at` — `eth_call` `balanceOf` per token at latest block, USD at the live rate; block-pinned copy at `RECON_BLOCK` drives the drift fence.
- **Source**: Base RPC. **Coverage**: point-in-time, per refresh; history in `stats_history.json`.
- **Bias**: **this wallet only.** Other Minds treasury wallets (incl. the rebate sink) are out of scope. On a failed fetch the digest falls back to the last non-null snapshot and marks it stale.
- **Safe to say**: "The distribution wallet holds about $B across MOCA and MENTE — this wallet only, not all of Minds."

### subsidy ratio
- **Formula**: `refresh.py` — `unbacked_7d / ph_topup_usd`, where `unbacked_7d` is invoke/equip/growth USD excluding Stripe-sized rows over the last 7 **settled** platform days, and `ph_topup_usd` is PostHog's top-up revenue for the same days (`data.json:server.subsidy_ratio`).
- **Source**: `transfers/` + `posthog_cache.json`. **Coverage**: trailing 7 settled days; weekly trend in `server.ratio_weeks`.
- **Bias**: the denominator is client-side PostHog events, which are lossy — `server.diverge_meta` documents the open reconciliation. A lossy denominator **overstates** the ratio.
- **Safe to say**: "For every $1 of top-up revenue we saw last week, roughly $R of unbacked payouts went out — the revenue side is a lossy client-side count, so treat it as an upper bound on the subsidy."

### user-funded cognition lower bound
- **Formula**: `refresh.py` — per mind wallet, `max(0, consumed_usd - treasury_credits_usd)`, summed (`data.json:facts.cognition.funding_split.user_funded_usd`; SWARM era in `swarm_split`).
- **Source**: `cognition_in/` (MENTE into the collector) + `transfers/` credits. **Coverage**: 2026-04-12 → now.
- **Bias**: **a strict LOWER bound.** Credits are assumed spent first, so any user-brought token that a credit could have covered is attributed to the treasury.
- **Safe to say**: "At least $U of cognition was paid for with tokens users brought themselves. The real figure is higher — we cannot see how much."

### distribution float / runway
- **Formula**: `refresh.py` — `runway7 = bal_usd / burn7avg`, `runway24 = bal_usd / burn24` (`data.json:infer.guard.runway7` / `runway24`); the digest prints the lower of the two.
- **Source**: wallet balance + day-pinned outflow history. **Coverage**: trailing 24h and 7d burn.
- **Bias**: **top-up cadence, not solvency.** It measures how long this one wallet lasts before someone refills it — it says nothing about company runway, and a single large swap in the window collapses it.
- **Safe to say**: "At the current pace this wallet has about N days before it needs a top-up. That is a refill schedule, not a solvency number."

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
