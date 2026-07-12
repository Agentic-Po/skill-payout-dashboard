# Minds Bank Wallet — Creator Payout Dashboard

Auto-updating dashboard of creator MOCA payouts from the Minds bank wallet
(`0xBD956171F5B50936f0Ad1C4db80c022bd2442519` on Base).

- **Live dashboard:** GitHub Pages serves `index.html` from `main`.
- **Refresh:** GitHub Actions runs `refresh.py` hourly (cron `7 * * * *` UTC),
  pulling new outgoing transfers from the Base Blockscout API and the live
  MOCA/USD rate, then commits the regenerated page.
- **Data cache:** `transfers.json` (incremental — only new transfers are fetched).
- Payout classification is inferred from transfer size:
  invoke ≈ $0.10, equip ≈ $1 in MOCA at the live rate; larger/micro transfers
  are listed separately as unclassified.
