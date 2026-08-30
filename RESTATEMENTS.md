# Restatements

**Contract.** Closed UTC days never reprice. Every closed day's outflow
aggregates (row count, USD to the cent, economy/ops split, pinned rates) are
sealed under a sha256 in `day_digests.json` the first time the pipeline sees
the day complete. If a later run recomputes a **different** digest for a
sealed day, the build **hard-fails** — silently republishing changed history
is the one failure this repo is not allowed to have.

The only way past that failure is this file: add a `## YYYY-MM-DD` heading
for the affected day with a short explanation of what changed and why. The
next run then updates the seal, prints a loud `RESTATED` line in the build
log, and the change ships with its documentation already public. An
undocumented restatement is treated as corruption; a documented one is an
audit event. Remove nothing from this file — it is the append-only public
record of every time history moved.

Digests are aggregates only (counts, dollar totals, rates). No transfer
rows, no counterparty addresses.

## 2026-08-22

Repriced when the day-rate oracle gained its market-close leg (2026-08-30).
Invokes stopped on 2026-08-21, so from 08-22 onward no day had a $0.10
invoke cluster to imply a rate from; those closed days had been priced by
carrying the 08-21 implied rate forward while MOCA kept moving. The oracle
now prices such days from that day's market close (see the day-pricing
provenance note on the dashboard), which restated 2026-08-22's USD
aggregates once. Already public on the page via `pricing_provenance`
(implied vs market-filled day counts).
