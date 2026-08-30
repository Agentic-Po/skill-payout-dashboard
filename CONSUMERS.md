# Consuming this data from other systems

The three repos form ONE data bank (843,975 rows · 232 MB as of 2026-08-30 —
live totals always in each repo's `DATASETS.md`). Rules for any consumer:

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
5. **Snapshot feeds**: `data.json` (schema_version 1) is the dashboard's full
   rendered dataset — same URL pattern as catalog.json. `transfers_export.csv`
   is the per-tx audit surface (tx_hash + log_index + canonical class).
6. **PostHog warehouse path** (documented, run locally — credentials never in
   CI): export canonical rows to Parquet → R2 bucket `po-import-bucket`
   (`<dataset>/snapshot_<YYYYMMDD>.parquet`) → register via
   `POST /api/projects/459477/warehouse_tables/`. See the vault note
   "Minds Analytics Stack" for the working recipe and key locations.

Known consumers today: the dashboard page + Telegram digest (this repo),
minds-canvas-dashboard (candidate), PostHog warehouse (manual snapshots),
treasury analyses in moca-ledger-private.
