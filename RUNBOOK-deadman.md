# Runbook — external dead-man's switch

Every in-repo health check shares one failure mode: it needs the pipeline to
run in order to notice the pipeline stopped. On 2026-08-26 GitHub's scheduler
stalled account-wide for 8 hours and nothing fired, because nothing ran. An
external dead-man is the only leg that catches that: healthchecks.io expects a
ping, and it is the SILENCE that alerts.

Two checks, one per repo — **never one shared check**. A shared check would
destroy attributability (a 3am alert could not tell you which pipeline died)
and would pollute the moca-ledger check's richer /start-/fail semantics. Both
halves — creating the checks and setting the secrets — are **Po's**, because
both need account access this repo does not have. Nothing below contains a
secret value.

> **The healthchecks.io UI is the source of truth** for check names, periods,
> grace windows and ping URLs. Everything in this runbook (and the dotfile
> cache described in §2) is a copy that can drift. When in doubt, read the UI.

## 1. The two checks (healthchecks.io)

| Check name | Repo / pipeline | Period | Grace | Why these numbers |
|---|---|---|---|---|
| `moca-ledger detection floor` | Agentic-Po/moca-ledger — `crawl.yml` /start, `selftest.yml` /fail | 1 h | **2 h — INTERIM** | This is the detection floor for a live-exploit detector. 2h grace is a concession to GitHub cron starvation (a 5h-old last ping was observed), **not** a considered latency target — during an active exploit a 2h blind window is real money. See §1.1. |
| `skill-payout-dashboard refresh` | Agentic-Po/skill-payout-dashboard — `refresh.yml` | 1 h | 3 h | Measured GitHub delivery gaps run ~2.4 h (historically 8 h+). Period 1h + grace 3h alerts only after ~4 h of silence: above worst recent normal, far below the pathological case, and zero spam. Deliberately **not** period 15 min to match the 4x/hour cron — that would page on ordinary scheduler jitter. |

The check formerly called **"My First Check"** is the `moca-ledger detection
floor` row above; it was renamed so a 3am alert email is self-explanatory. Its
period and ping URL are unchanged.

> ⚠️ **The dashboard numbers are dashboard-only.** Period 1h / grace 3h means a
> real hard failure (bad deploy, revoked secret) can be silent for ~4 h. That
> is acceptable for a *refresh* pipeline and **must never be copied to the
> detection floor check**, which guards exploit detection latency. If you find
> yourself reaching for these numbers on the detector, stop.

### 1.1 The 2h detection-floor grace is INTERIM — revisit 2026-09-13

Raising the detection floor's grace from 30 min to 2 h mutes the alarm instead
of fixing the delivery. The real fix is to make cron delivery reliable — an
external trigger hitting the `workflow_dispatch` endpoint, or a second cheap
runner — after which grace should come back down to ~45 min.

**Revisit date: 2026-09-13.** If the external trigger is not in place by then,
that is a finding to raise, not a deadline to quietly extend. The same revisit
date is recorded in the README next to the check mapping, so the "temporary"
2 h does not become permanent by forgetting.

Also note: a `/fail` ping bypasses grace and alerts immediately — that is the
documented healthchecks.io behaviour, which is exactly why `selftest.yml`
pings `/fail`. **Verify this in the healthchecks UI rather than assuming it**;
the whole point of the fail path is that it does not wait out the grace window.

Set both checks to notify the same channel Po already watches
(poc@animocabrands.com). Do **not** point them at the public Telegram group: a
dead-man that is loud in a shared channel gets muted, and a muted dead-man is
worse than none. After creating the new check, send a manual test ping and
confirm the email arrives and is distinguishable from the other check's before
trusting the wiring.

## 2. Where each URL goes

Each check gives a ping URL. It is a bearer credential — anyone holding it can
fake a healthy pipeline — so it goes in repo secrets, never in code, never in
a workflow file, never in an artifact.

| Check | Secret | Repo | Consumed by |
|---|---|---|---|
| `moca-ledger detection floor` | `HC_PING_URL` | moca-ledger | `crawl.yml` (`/start`, success, `/fail`); `selftest.yml` (`/fail`) |
| `skill-payout-dashboard refresh` | `HEALTHCHECK_URL` | skill-payout-dashboard | `refresh.yml` → "Dead-man ping"; `weekly.yml` nags while it is unset |

moca-ledger no longer carries a `HEALTHCHECK_URL` step — the redundant no-op
dead-man was removed from `crawl.yml` on 2026-08-30. **Do not backfill that
secret in moca-ledger**: two half-wired ping paths in one workflow is precisely
how a silent no-op secret survives for weeks. After any change there, confirm
with `grep -rn 'HEALTHCHECK\|HC_PING' .github/workflows/` that exactly one
ping path remains.

### Ingesting the dashboard ping URL (never echo the value)

Po pastes the new check's ping URL **once** into a dotfile; it is then piped
into `gh` from that file so the value is never a shell argument and never
lands in shell history:

```bash
chmod 600 ~/.moca-ledger/healthchecks_dashboard_ping_url
gh secret set HEALTHCHECK_URL -R Agentic-Po/skill-payout-dashboard \
  < ~/.moca-ledger/healthchecks_dashboard_ping_url
```

**Authority:** the healthchecks.io UI is the source of truth for this URL; the
dotfile `~/.moca-ledger/healthchecks_dashboard_ping_url` is only a **cache**
and can go stale. If the URL is ever rotated in the UI, rewrite the dotfile and
re-run the `gh secret set` line above — otherwise the dotfile and the repo
secret drift silently. The dotfile is recorded (path and purpose only, never
its value) in the vault's Local Credentials Map.

## 3. The step each workflow carries

This repo carries a guarded no-op. Unset secret = an empty string = a printed
skip, so the step is safe to merge before the secret exists and starts working
the moment it is set — no second deploy.

```yaml
      - name: Dead-man ping
        continue-on-error: true
        env:
          HC_URL: ${{ secrets.HEALTHCHECK_URL }}
        run: |
          if [ -n "$HC_URL" ]; then curl -fsS -m 10 --retry 3 "$HC_URL" >/dev/null && echo "ping ok"; else echo "HEALTHCHECK_URL not set — skipping"; fi
```

`continue-on-error: true` is deliberate: healthchecks.io being down must not
fail a refresh that otherwise succeeded. The ping is placed **after** the
commit/push step, so it reports success only once the run actually published.

moca-ledger's check is driven by a *different*, richer contract — `HC_PING_URL`
with `/start`, success and `/fail` pings, so it measures run duration and can
signal an explicit failure. That is intentional asymmetry, not drift: the
detection floor earns the richer instrumentation; the dashboard refresh does
not need it.

## 4. Verifying it works

1. Run the workflow manually (`workflow_dispatch`) and confirm the step logs
   `ping ok`, and the check on healthchecks.io flips to "up".
2. Pause the check for longer than period + grace and confirm the alert
   arrives. **Test the alarm, not just the ping** — an unverified dead-man is
   an assumption, not a control.
3. Resume it.
4. Confirm in the healthchecks UI that a `/fail` ping alerts immediately and
   does **not** wait out the grace window (moca-ledger's `selftest.yml` relies
   on this). Verify it; do not assume it.
5. Confirm the two checks' alert emails are distinguishable by name —
   `moca-ledger detection floor` vs `skill-payout-dashboard refresh`. 3am
   triage depends entirely on the check name.

## 5. When it fires

A dead-man alert means *no runs are landing*, which is different from *runs
are failing* (those already alert on their own).

1. Open the repo's Actions tab. If the last run is old, the scheduler stalled —
   trigger `workflow_dispatch` manually; that alone usually unwedges the cron.
2. If runs ARE landing but the ping is silent, the secret is wrong or
   healthchecks.io is down. Check the step log for `HEALTHCHECK_URL not set`,
   and re-check the ping URL in the healthchecks UI (source of truth) against
   the dotfile cache — they drift after a rotation.
3. Data does not need repair after a stall. Both crawlers are resumable and
   catch up from their own state on the next successful run; the day-pinned
   rates for closed days are immutable, so nothing reprices.

## 6. Send-path liveness (alive_check.py) — and its cache caveat

The dead-man above answers "are runs landing?". A separate gate answers
"can the runs still *reach Telegram*?": `state.record_send()` keeps a
consecutive-failure counter per channel (`alerts`, `digest`) in
`alert_state.json`, and refresh.yml's blocking **Alert liveness check** step
(`python3 alive_check.py`) turns 3+ consecutive failed send attempts into a
red workflow. The sends themselves stay `continue-on-error` — a Telegram
outage must never kill a refresh that otherwise succeeded; only the
*pattern* of failures blocks.

**Documented caveat: cache eviction resets the counter.** `alert_state.json`
lives only in the Actions cache (deliberately — committing it published
armed/cooling detector state). GitHub evicts cache entries after ~7 days
unused or under the 10 GB repo cap, and a fresh runner with no cache hit
starts every counter at zero. After an eviction, a dead send path needs 3
NEW consecutive failures before the gate fires again — so treat a green
liveness check right after a cache miss ("no send attempts recorded yet" in
the step log) as unproven, not as healthy. The healthchecks.io dead-man and
the daily digest's `alerts: N sent / M failed (24h)` line are the
cross-checks that do not share this reset.
