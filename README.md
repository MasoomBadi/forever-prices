# forever-prices

Private. Every hour a GitHub Actions job pulls the WoW Forever auction houses (US and EU, every ruleset and
faction) from Blizzard's Game Data API, keeps a few numbers per item (lowest and median unit price, quantity,
listings), and uploads one small gzipped JSON file per region and ruleset to the app's server
(`public/prices/<region>-<ruleset>.json.gz`).

```
python3 -I prices.py --dry-run   # pull and write out/ only (needs BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET)
```

## Secrets (Settings > Secrets and variables > Actions)

`BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`, `FTP_HOST`, `FTP_USER`, `FTP_PASS`. They live only there:
never in a file, a commit, an issue, or a log. The safety rules for the job are at the top of
`.github/workflows/prices.yml`.

## Blizzard's API terms

- The prices go only to the app's own users; this is not a public dataset.
- Data is kept at most 30 days: each hourly upload replaces the last file, and the app drops prices older than that.
- The app names Blizzard as the source of the prices.
- Prices are never paid-only, and no full-screen ad stands between a player and a price screen.
