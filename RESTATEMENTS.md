# Restatements

**Contract.** Closed UTC days never reprice. Every closed day's outflow
aggregates (row count, USD to the cent, economy/ops split, pinned rates) are
sealed under a sha256 in `day_digests.json` the first time the pipeline sees
the day complete. If a later run recomputes a **different** digest for a
sealed day, the build **hard-fails** — silently republishing changed history
is the one failure this repo is not allowed to have.

New corrections require a dated audit heading and a strict `digest-transition`
JSON block containing the exact prior and replacement SHA-256 values. Only
that old-to-new pair may update the seal. Once applied, any later mismatch
fails again. Missing or malformed metadata fails closed; a dated heading
alone cannot approve a correction. The historical 2026-08-22 authorization
below retains its original behavior. Remove nothing from this file.

New days are sealed only when both Treasury directions have verified coverage.
Already sealed days continue to be checked even while current coverage is
incomplete. The `digests.py --seed` entry point also requires the saved
own-Treasury coverage flag to be explicitly `true`. Offline refresh uses
the same rule: a legacy or default financial `scope.complete` value does
not permit a new seal when own-Treasury evidence is missing. The existing one-day sealing grace remains in effect.

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

## Notes

Not restatements. A note here is NOT a `## YYYY-MM-DD` heading on purpose:
such a heading is a standing approval that lets the day reseal on any future
mismatch, and none of the days below ever sealed wrong.

### Note — 2026-09-14 (never sealed wrong)

Between 2026-09-14 ~00:12 UTC and the hotfix (commit ce9d4eb, 2026-09-15),
the implied oracle priced 09-14 MOCA at 0.0185155 — 2.00x the market close
0.00924424 — because the new $0.05 creator-equip cluster (Creator Rewards v2
resumed 14:19 UTC) was read as $0.10 invokes; that day's $3 credits displayed
as "$5.93 nonstandard". The day was dropped and re-derived from the market
close while still inside the one-day seal grace, so `day_digests.json` never
carried the wrong figure. The implied leg is retired as a pricer from
2026-09-14 (market close is the only source for a newly closed day; a
refresh refuses to seal an implied-priced day after that date). Same day:
the creator-reward taxonomy became era-aware (v2 sizes $0.05 equip / $0.005
invoke from 2026-09-14T14:19Z; $1 after 2026-08-21T13:55Z is a legacy free
top-up under growth) — this changes fine classes only, every affected row
stays economy-side, and no sealed digest moved.

## 2026-10-08

Missing history was recovered after Treasury coverage catch-up. The prior
snapshot first sealed this day at 2026-10-10T00:50:55Z before full coverage
was verified: 6,697 outgoing transfers, $26,062.52. Verified canonical
history adds 4,253 transfers, giving 10,950 transfers and $37,470.74.
The MOCA daily rate remains 0.0092414; this corrects missing rows, not prices.
All 167 other previously sealed days reproduce their existing digests.
The recovered October 8 identities also match the independent MOCA ledger.

The public pending OUT proof at commit
`bf33803b54c8d00df6e598893a73601a341710ff` covers blocks 52334190–52423989,
anchored at 2026-10-10T13:02:05Z. Its chunk checksums, contiguous coverage,
and canonical cutoff header were verified before authorizing this pair.

```digest-transition
{"day":"2026-10-08","old_sha":"13fec99a13cfe801a812852472af6647069b1a72ed85cbce3cee23f0e0a6a6c6","new_sha":"0f16f71f094c95e93d0333ba01e80c39042ef5c71fff52f24a9d75a501c1ae05"}
```
