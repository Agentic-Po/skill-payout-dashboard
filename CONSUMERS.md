# Consuming this data from other systems

The three repos form ONE data bank (844,004 rows · 232 MB · 16 datasets at
last count — the LIVE numbers are always each repo's machine-generated
`DATASETS.md`; never hand-quote totals from this file). Rules for any consumer:

1. **Discover via `catalog.json`** (machine-readable; committed in every repo,
   recomputed each refresh). Public over HTTP, no auth:
   - `https://raw.githubusercontent.com/Agentic-Po/skill-payout-dashboard/main/catalog.json`
   - `https://raw.githubusercontent.com/Agentic-Po/moca-ledger/main/catalog.json`
   - moca-ledger-private: clone with repo access.
2. **Read rows ONLY through `rows.py`** (`canonical_rows(source)` — one row
   shape over every ledger layout; addresses lowercased, `value_wei` int).
   Copy the module or vendor it; the two source schemas are frozen contracts,
   the adapter is the unification.
3. **Classify ONLY through `classify.py`** (`classify_usd`, `pin_rate`) — the
   one taxonomy behind the page, Telegram, and CSV. Re-implementing it is how
   a $32K swap once became "revenue".
4. **Respect freshness**: call `rows.require_fresh(catalog, dataset, max_age_h)`
   before publishing anything derived; it raises on stale data.
5. **Snapshot feeds**: `data.json` (schema_version 2) is the dashboard's full
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
6. **PostHog warehouse path** (documented, run locally — credentials never in
   CI): export canonical rows to Parquet → R2 bucket `po-import-bucket`
   (`<dataset>/snapshot_<YYYYMMDD>.parquet`) → register via
   `POST /api/projects/459477/warehouse_tables/`. See the vault note
   "Minds Analytics Stack" for the working recipe and key locations.

Known consumers today: the dashboard page + Telegram digest (this repo),
minds-canvas-dashboard (candidate), PostHog warehouse (manual snapshots),
treasury analyses in moca-ledger-private.
