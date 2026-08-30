# Runbook — external dead-man's switch

Every in-repo health check shares one failure mode: it needs the pipeline to
run in order to notice the pipeline stopped. On 2026-08-26 GitHub's scheduler
stalled account-wide for 8 hours and nothing fired, because nothing ran. An
external dead-man is the only leg that catches that: healthchecks.io expects a
ping, and it is the SILENCE that alerts.

Two checks, one per repo. Both halves — creating the checks and setting the
secrets — are **Po's**, because both need account access this repo does not
have. Nothing below contains a secret value.

## 1. Create the checks (healthchecks.io)

| Check name | Repo | Period | Grace | Why these numbers |
|---|---|---|---|---|
| `skill-payout-dashboard` | Agentic-Po/skill-payout-dashboard | **15 min** | **75 min** | cron fires 4x/hour but GitHub delivers best-effort (8–85% observed); 75 min tolerates four consecutive misses before crying wolf |
| `moca-ledger crawl` | Agentic-Po/moca-ledger | **10 min** | **40 min** | 10-min cron, tighter tolerance — this one watches money moving, and three missed runs is already a real gap |

Set both to notify the same channel Po already watches. Do **not** point them
at the public Telegram group: a dead-man that is loud in a shared channel gets
muted, and a muted dead-man is worse than none.

## 2. Where each URL goes

Each check gives a ping URL. It is a bearer credential — anyone holding it can
fake a healthy pipeline — so it goes in repo secrets, never in code, never in
a workflow file, never in an artifact.

| Secret | Repo | Consumed by |
|---|---|---|
| `HEALTHCHECK_URL` | skill-payout-dashboard | `.github/workflows/refresh.yml` → "Dead-man ping"; `weekly.yml` nags while it is unset |
| `HEALTHCHECK_URL` | moca-ledger | `.github/workflows/crawl.yml` → "Dead-man ping" |

```bash
gh secret set HEALTHCHECK_URL -R Agentic-Po/skill-payout-dashboard -b "<dashboard ping url>"
gh secret set HEALTHCHECK_URL -R Agentic-Po/moca-ledger          -b "<crawl ping url>"
```

## 3. The step each workflow carries

Both repos carry the identical guarded no-op. Unset secret = an empty string =
a printed skip, so the step is safe to merge before the secret exists and
starts working the moment it is set — no second deploy.

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

moca-ledger already pings a *separate* `HC_PING_URL` check (start / success /
fail) from its crawl workflow. That one is richer — it measures run duration —
but it is a different check and a different secret. The `HEALTHCHECK_URL` step
added here is the plain heartbeat this runbook describes, so both repos have
the same one-line contract and one runbook covers both.

## 4. Verifying it works

1. Run the workflow manually (`workflow_dispatch`) and confirm the step logs
   `ping ok`, and the check on healthchecks.io flips to "up".
2. Pause the check for longer than period + grace and confirm the alert
   arrives. **Test the alarm, not just the ping** — an unverified dead-man is
   an assumption, not a control.
3. Resume it.

## 5. When it fires

A dead-man alert means *no runs are landing*, which is different from *runs
are failing* (those already alert on their own).

1. Open the repo's Actions tab. If the last run is old, the scheduler stalled —
   trigger `workflow_dispatch` manually; that alone usually unwedges the cron.
2. If runs ARE landing but the ping is silent, the secret is wrong or
   healthchecks.io is down. Check the step log for `HEALTHCHECK_URL not set`.
3. Data does not need repair after a stall. Both crawlers are resumable and
   catch up from their own state on the next successful run; the day-pinned
   rates for closed days are immutable, so nothing reprices.
