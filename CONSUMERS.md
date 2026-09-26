# Consuming this data from other systems

The three repos form ONE data bank (844,004 rows · 232 MB · 16 datasets at
last count — the LIVE numbers are always each repo's machine-generated
`DATASETS.md`; never hand-quote totals from this file). Rules for any consumer:

1. **Discover via `catalog.json`** (machine-readable; committed in every repo,
   recomputed each refresh). Public over HTTP, no auth:
   - `https://raw.githubusercontent.com/Agentic-Po/skill-payout-dashboard/main/catalog.json`
   - `https://raw.githubusercontent.com/Agentic-Po/moca-ledger/main/catalog.json`
   - moca-ledger-private: clone with repo access.
   - vip-metrics (private, `vip_in`: inbound MOCA+MENTE to VIP mind wallets —
     rows name VIP wallets, so the dataset is never published here; it is
     listed by name in this repo's catalog and read through
     `rows.canonical_rows("vip_in", path=<clone>/vip_in)`).
2. **Read rows ONLY through `rows.py`** (`canonical_rows(source)` — one row
   shape over every ledger layout; addresses lowercased, `value_wei` int).
   Copy the module or vendor it; the two source schemas are frozen contracts,
   the adapter is the unification.
3. **Classify ONLY through `classify.py`** (`classify_usd(usd, ts)`,
   `band(usd, ts)`, `pin_rate`) — the one taxonomy behind the page, Telegram,
   and CSV. Re-implementing it is how a $32K swap once became "revenue".
   **Signature change (2026-09-15):** `classify_usd` and `band` now REQUIRE
   the row's ISO timestamp as the second positional argument — the reward
   grid is era-aware (v1 $1 equip / $0.10 invoke until 2026-08-21T13:55Z; v2
   $0.05 equip / $0.005 invoke from 2026-09-14T14:19Z; a ≈$1 after the v1
   close is `growth` / `$1 system free top-up`). The era table is
   `classify.ERAS`; the row timestamp decides, never the run date. Callers
   that passed one argument get a `TypeError` rather than a silently wrong
   class. **Group ONLY through `classify.group_for(cat, fine)`** (2026-09-15):
   `skill_rewards` · `credit_grants` · `system_topups` · `topups_delivered` ·
   `ops` · `micro` — the six partition every row and close on `out_usd`.
4. **Respect freshness**: call `rows.require_fresh(catalog, dataset, max_age_h)`
   before publishing anything derived; it raises on stale data.
5. **Snapshot feeds**: `data.json` (schema_version 3) is the dashboard's full
   rendered dataset — same URL pattern as catalog.json. `transfers_export.csv`
   is the per-tx audit surface (tx_hash + log_index + canonical class).
   **schema_version 1 → 2 (2026-08-30)**: `transfers_export.csv` dropped the
   `counterparty_label` column, and no public artifact carries identity
   labels any more (owner/team/custodian names, wallet↔mind names, label
   notes) — public label fields hold structural placeholders only ("Funding
   wallet A", "creator wallet"). Consumers that joined on
   `counterparty_label` must key on the `counterparty` address instead and,
   with repo access, join identities from `moca-ledger-private:labels/`.
   **Additive (2026-08-30, still schema_version 2)**: `data.json` gained a
   top-level `exec_summary` object (`{text, degraded, data_age_hours}`) —
   the plain-English executive block the page shows above the hero strip,
   computed server-side from the same `facts` values. Consumers that
   asserted an exact top-level key set must add it; nothing else changed.
   **Additive (2026-09-15, still schema_version 2, Creator Rewards v2)**:
   `facts.rewards_v2` (`{resumed_utc, cap_on_utc, usd_24h, usd_since_resume,
   n_equip_24h, n_invoke_24h, creators_24h, creators_since_resume,
   implied_user_spend_24h}` — aggregates only, no cap figure or headroom);
   `infer.legacy_public` (`[{cat, since, n, usd, last_seen}]`, same shape
   class as `retired_public`, whose `cat` is now `invoke_v1`);
   `facts.pricing_provenance` gained `carry_forward` and `market_open`
   counts; `facts.band_keys` / `band_labels` gained `b0005` and `b005`
   (the micro label now reads `< $0.003 (< $0.06 before 14 Sep)`). The CSV
   header is unchanged; its `rate_source` vocabulary is `day-market`,
   `day-implied` (history only), `day-market (open)`, `carry-forward`,
   `carry-back`, `live`, and `class_fine` gained `invoke (retired)` and
   `$1 system free top-up`. The top-level key set is unchanged.
   **schema_version 2 → 3 (2026-09-15, "vocabulary, grouping, privacy")**.
   Removed: `infer.creators` (per-wallet all-history ranking — use
   `facts.creator_wallets.windows.all`), `infer.legacy_public` (renamed
   `infer.system_topup_public`, same shape; its `cat` and the CSV
   `class_fine` value read `$1 system free top-up`, formerly
   `$1 top-up (legacy)` — closed-day digests are unaffected, they hash the
   coarse class only), `infer.guard.{flagged_n, monitored_n, at_risk_usd,
   runway24, runway7, runway_total, burn24, burn_prev, burn7avg}` (monitoring
   status is private; the unbacked-burn runway trio is replaced by
   `facts.float`), `facts.rewards_v2.{cap_on_utc, implied_user_spend_24h}`
   (+ `creators_*` → `creator_wallets_*`; `grid` added),
   `facts.cognition.{funding_split, swarm_split}`, `stripe_snap.{period_
   subsidy_ratio, period_unbacked_dist_usd}` and `server.{subsidy_ratio,
   ratio_weeks, unbacked_7d}` — every paid-vs-free basis is gone and stays
   gone (`check_publish --scan` denies the key names). Added:
   `facts_window.groups` on every window/prev24/monthly entry
   (`{group: {n, usd, wallets}}` via `group_for`), `facts.group_keys` /
   `group_labels` / `group_to`, `facts.float` (`{basis, bal_usd, out_24h_usd,
   out_prev24_usd, out_7d_avg_usd, days_24h_pace, days_7d_pace,
   driver_24h}` — the ONE runway, total-outflow basis),
   `facts.creator_wallets` (`{default, resumed_utc, paused_utc, windows:
   {24h, 7d, since_resume, all}}`, each `{wallets, usd, equips, invokes,
   top1/top5/top10_share_pct, median_usd|null, top[≤10]{addr, usd, equips,
   invokes, first_seen(day)}, new_wallets (24h/7d only)}`), `facts.hourly[].g`
   (per-group transfer counts, additive) and `facts.hourly[].cw` (creator
   wallets paid that hour), `infer.fine_table[].group`, `gaps[].opened`.
   `stats_history.runway7` / `runway_adj` now carry `facts.float.days_7d_pace`
   (total-outflow basis) — a documented semantic change of a public series.
6. **Coupon claims feed**: `coupon_data.json` (schema_version 1) is a SEPARATE
   file, published beside `data.json` and embedded verbatim in `coupon.html`.
   It covers the Coupon Distributor wallet only — a wallet funded outside the
   treasury (its first event is a bridge mint from the zero address) whose rows
   appear in NO treasury figure, so the two feeds never double-count. Keys:
   `scope, summary, totals, buckets, daily, inflows, top, concentration,
   range`. It carries aggregates plus the top-10 claimant totals — never a
   per-claim row and never a full claimant list; read rows from the
   `coupon_out` / `coupon_in` datasets through `rows.py`
   (`canonical_rows("coupon_out")`) instead. USD in it is priced with the same
   `classify.pin_rate` against the same `day_rates.json` the treasury page
   uses; the display size buckets are MOCA-denominated and are a page-local
   presentation choice, NOT a taxonomy — do not classify against them.
   `data.json`'s top-level key set is deliberately unchanged by any of this.
7. **PostHog warehouse path** (documented, run locally — credentials never in
   CI): export canonical rows to Parquet → R2 bucket `po-import-bucket`
   (`<dataset>/snapshot_<YYYYMMDD>.parquet`) → register via
   `POST /api/projects/459477/warehouse_tables/`. See the vault note
   "Minds Analytics Stack" for the working recipe and key locations.

Known consumers today: the dashboard page + Telegram digest (this repo),
minds-canvas-dashboard (candidate), PostHog warehouse (manual snapshots),
treasury analyses in moca-ledger-private.

## v4 (2026-09-18) — `in_recycled_usd` / `in_external_usd` semantics

`in_recycled_usd` now counts every inflow that is **not new money**: collector recycling (as before) **plus returns from
the treasury-controlled reserve** `0x5edea733…d49c`. `in_external_usd` (`in_usd - in_recycled_usd`) therefore counts only
money entering from outside. Key names and shapes are unchanged; only the split moves.

Why: the treasury moved $41,313 to that reserve on 2026-08-25 and it was returned on 2026-09-04/11/18. Counting the
returns as external funding overstated lifetime money-in by ~$40K — caught while preparing the 2026-09-18 budget
request, where the public dashboard would have contradicted the filing.
