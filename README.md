# forever-prices

About every hour a GitHub Actions job pulls the WoW Forever auction houses (US and EU, every ruleset and
faction) from Blizzard's Game Data API, keeps a few numbers per item (lowest and median unit price, quantity,
listings), and uploads one small gzipped JSON file per region and ruleset to the app's server
(`public/prices/<region>-<ruleset>.json.gz`). Everything goes up in one HTTPS request to the server's
`api/prices-upload.php`, which only takes price files and only with the upload key; there is no FTP login here.

```
python3 -I prices.py --dry-run   # pull and write out/ only (needs BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET)
```

## Secrets (Settings > Environments > prices)

`BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`, `PRICES_UPLOAD_KEY` (the server keeps only its SHA-256, as
`prices_upload_key_sha256` in its `secrets.php`). They live only in the
`prices` environment, which only the main branch can use: never in a file, a commit, an issue, a log, or the
repository-level secrets. The safety rules for the job are at the top of `.github/workflows/prices.yml`.

Pull requests from outside are not accepted.

## Blizzard's API terms

- The prices go only to the app's own users; this is not a public dataset.
- Data is kept at most 30 days: each hourly upload replaces the last file, and the app drops prices older than that.
- The app names Blizzard as the source of the prices.
- Prices are never paid-only, and no full-screen ad stands between a player and a price screen.
