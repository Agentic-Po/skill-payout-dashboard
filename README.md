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
       └─ writes transfers_export.csv (per-tx audit: tx_hash + log_index + class)
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

CI enforces this: a leak-tripwire step fails the run if per-wallet detector
fields ever appear in a public artifact, and `alert_state.json` /
`guard_private.json` / `*.tmp` are gitignored so `git add -A` cannot stage
them.

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

## Open items owned by Po

1. `HEALTHCHECK_URL` secret (external dead-man; weekly digest nags until set)
2. Whether to rewrite public git history (pre-2026-08-29 commits contain old
   state files; content is stale but recoverable)
3. transfers_export.csv monthly sharding before it nears the 50 MB tripwire
