"""Hourly WoW Forever auction prices for Companion for WoW Forever.

Pulls every Forever auction house (its own list and its commodities) from Blizzard's Game Data API (US and EU),
boils each one down to a few numbers per item, and uploads one small file per region and ruleset to the app's
server over FTPS.

    python3 -I prices.py             pull and upload (the GitHub Actions job)
    python3 -I prices.py --force     pull and upload even if the server's prices are fresh
    python3 -I prices.py --dry-run   pull and write out/ only, nothing uploaded

GitHub starts scheduled jobs when it can and drops many of them, so the job is scheduled four times an hour and a
scheduled run quits early while the server's prices are under FRESH_MINUTES old: about one upload an hour, with
three spare chances when GitHub skips one. Runs started by hand always go ahead.

Secrets come only from the environment: BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET, FTP_HOST, FTP_USER, FTP_PASS.
Nothing here ever prints them, the access token Blizzard hands back is masked in the GitHub log, and any error is
printed with every secret value scrubbed out. Stdlib only, no third-party code.

Once a day (the first run after 00:00 UTC) it also adds each item's typical price to a history file per auction
house, which keeps the last 30 days only. The history lives on the app's server, never in this repository.

Blizzard's API terms: the data is shown only to the app's own users, kept at most 30 days (each upload replaces the
last; history drops days older than 30), and the app credits Blizzard as its source.
"""
import argparse
import base64
import ftplib
import gzip
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REGIONS = ("us", "eu")
# Every region lists the same house IDs; the house ID, not the ruleset in the URL, picks the market.
# (ruleset ID, our name, {faction: house ID})
RULESETS = (
    (1, "pvp", {"alliance": 110, "horde": 113, "neutral": 116}),
    (2, "normal", {"alliance": 111, "horde": 114, "neutral": 117}),
    (3, "rp", {"alliance": 119, "horde": 120, "neutral": 121}),
    (15, "rp-pvp", {"alliance": 123, "horde": 124, "neutral": 125}),
)
REMOTE_DIR = "public/prices"
# The site sends .gz files with a one-year cache (packs never change); price files change every hour.
HTACCESS = b"""# Price files are replaced every hour: clients must check for a newer copy.
<IfModule mod_headers.c>
  <FilesMatch "\\.gz$">
    Header set Cache-Control "no-cache"
  </FilesMatch>
</IfModule>
"""
FORMAT_VERSION = 1
# The app's server, where the history so far is read back from (public files, no login needed).
SITE = os.environ.get("PRICES_SITE", "https://forever.masoombadi.top")
HISTORY_DAYS = 30
PAUSE_BETWEEN_READS = 1.0  # seconds between history downloads, gentle on the host
PAUSE_BETWEEN_UPLOADS = 2.0  # seconds; the host's firewall blocks bursts of connections
# A scheduled run skips while the server's prices are newer than this (runs come every 15 minutes).
FRESH_MINUTES = 45
# The file whose age stands for all of them (every run uploads all regions and rulesets together).
FRESHNESS_FILE = "us-normal.json.gz"
OUT = Path(__file__).resolve().parent / "out"

_SECRETS: list[str] = []


def scrub(text: str) -> str:
    """The text with every secret value (and the access token) replaced, for anything that gets printed."""
    for s in _SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


def mask(value: str) -> None:
    """Hide a value made at run time (the access token) from the GitHub Actions log, and from our own prints."""
    _SECRETS.append(value)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}", flush=True)


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing environment variable {name}")
    _SECRETS.append(value)
    return value


def access_token(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    _SECRETS.append(basic)
    req = urllib.request.Request(
        "https://oauth.battle.net/token",  # one token serves every region
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        token = json.load(r)["access_token"]
    mask(token)
    return token


def get(region: str, token: str, path: str) -> tuple[dict, str | None]:
    q = urllib.parse.urlencode({"namespace": f"dynamic-forever-{region}", "locale": "en_US"})
    req = urllib.request.Request(
        f"https://{region}.api.blizzard.com{path}?{q}",
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body), r.headers.get("Last-Modified")


def get_or_empty(region: str, token: str, path: str) -> dict:
    """An auction list, or none when the house has no such list (404)."""
    try:
        return get(region, token, path)[0]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def summarize(auctions: list[dict]) -> dict[str, list[int]]:
    """Per item: [lowest unit price, quantity-weighted median unit price, total quantity, listings], prices in copper."""
    by_item: dict[int, list[tuple[float, int]]] = {}
    for a in auctions:
        qty = a.get("quantity") or 1
        unit = a.get("unit_price") or (a.get("buyout") or 0) / qty
        if unit > 0:
            by_item.setdefault(a["item"]["id"], []).append((unit, qty))
    out = {}
    for item, listings in by_item.items():
        listings.sort()
        total = sum(q for _, q in listings)
        seen, median = 0, listings[-1][0]
        for unit, qty in listings:
            seen += qty
            if seen * 2 >= total:
                median = unit
                break
        out[str(item)] = [round(listings[0][0]), round(median), total, len(listings)]
    return out


def iso(last_modified: str | None) -> str | None:
    if not last_modified:
        return None
    return parsedate_to_datetime(last_modified).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(region: str, token: str, live_rulesets: set[int]) -> tuple[dict[str, bytes], dict[str, dict[str, int]]]:
    """{file name: gzipped JSON} for the region's rulesets, and {"<region>-<ruleset>-<faction>": {item: typical}}."""
    files, typical = {}, {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for ruleset_id, name, houses in RULESETS:
        if ruleset_id not in live_rulesets:
            continue
        doc = {"v": FORMAT_VERSION, "region": region, "ruleset": name, "generated": now, "houses": {}}
        for faction, house in houses.items():
            started = time.time()
            base = f"/data/wow/ruleset/{ruleset_id}/auctions/{house}"
            # Gear and other single items are the house's own list; stackable goods (cloth, herbs, potions) are its
            # separate commodities list. No item is in both.
            data, modified = get(region, token, base)
            commodities = get_or_empty(region, token, f"{base}/commodities")
            listings = (data.get("auctions") or []) + (commodities.get("auctions") or [])
            items = summarize(listings)
            doc["houses"][faction] = {"id": house, "updated": iso(modified), "items": items}
            typical[f"{region}-{name}-{faction}"] = {item: v[1] for item, v in items.items()}
            print(f"  {region} {name} {faction}: {len(listings)} listings, {len(items)} items, "
                  f"{time.time() - started:.1f}s", flush=True)
        raw = json.dumps(doc, separators=(",", ":")).encode()
        files[f"{region}-{name}.json.gz"] = gzip.compress(raw, mtime=0)
    return files, typical


def read_site(path: str) -> bytes | None:
    """A file from the app's server; None when it isn't there yet."""
    req = urllib.request.Request(f"{SITE}/prices/{path}", headers={"User-Agent": "forever-prices-job", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def minutes_old(path: str) -> float | None:
    """How long ago the server's copy of a price file was replaced (one HEAD request); None when unknown."""
    req = urllib.request.Request(f"{SITE}/prices/{path}", method="HEAD", headers={"User-Agent": "forever-prices-job", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            modified = r.headers.get("Last-Modified")
        if not modified:
            return None
        return (datetime.now(timezone.utc) - parsedate_to_datetime(modified)).total_seconds() / 60
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return None  # can't tell: go ahead and pull


def add_day(old: dict | None, today: str, prices: dict[str, int]) -> dict:
    """The history with [prices] as [today]'s column: days older than HISTORY_DAYS go, items with no price left go."""
    days = list(old["days"]) if old else []
    items = {k: list(v) for k, v in (old or {}).get("items", {}).items()}
    if days and days[-1] == today:  # a re-run on the same day replaces the day
        days.pop()
        for v in items.values():
            v.pop()
    first = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    keep = [i for i, d in enumerate(days) if d >= first]
    days = [days[i] for i in keep] + [today]
    out = {}
    for item in set(items) | set(prices):
        old_row = items.get(item)
        row = [old_row[i] for i in keep] if old_row else [0] * (len(days) - 1)
        row.append(prices.get(item, 0))
        if any(row):
            out[item] = row
    return {"v": 1, "days": days, "items": out}


def history(typical: dict[str, dict[str, int]]) -> dict[str, bytes]:
    """Today's history files ("history/<house>.json.gz", then "history/index.json"), or none once today is done."""
    today = datetime.now(timezone.utc).date().isoformat()
    index = read_site("history/index.json")
    if index and json.loads(index).get("day") == today:
        print(f"history: {today} already saved", flush=True)
        return {}
    files = {}
    for key, prices in typical.items():
        old = read_site(f"history/{key}.json.gz")
        doc = add_day(json.loads(gzip.decompress(old)) if old else None, today, prices)
        files[f"history/{key}.json.gz"] = gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)
        time.sleep(PAUSE_BETWEEN_READS)
    files["history/index.json"] = json.dumps({"day": today, "days": HISTORY_DAYS}).encode()  # last: marks the day done
    print(f"history: {today} added to {len(typical)} houses", flush=True)
    return files


def upload(files: dict[str, bytes], host: str, user: str, password: str) -> None:
    """One FTPS session: each file goes up under a temporary name and is renamed into place, so the app never
    downloads half a file. A pause between files keeps the host's firewall happy."""
    ftp = ftplib.FTP_TLS(host, timeout=60, context=ssl.create_default_context())
    try:
        ftp.login(user, password)
        ftp.prot_p()  # encrypt the files too, not just the login
        for d in ("public", REMOTE_DIR, f"{REMOTE_DIR}/history"):
            try:
                ftp.mkd(d)
            except ftplib.error_perm:
                pass  # already there
        ftp.voidcmd("TYPE I")
        try:
            have_htaccess = ftp.size(f"{REMOTE_DIR}/.htaccess") == len(HTACCESS)
        except ftplib.error_perm:
            have_htaccess = False
        if not have_htaccess:
            files = {".htaccess": HTACCESS, **files}
        for name, body in files.items():
            tmp = f"{REMOTE_DIR}/{name}.part"
            ftp.storbinary(f"STOR {tmp}", io.BytesIO(body), blocksize=1 << 16)
            ftp.rename(tmp, f"{REMOTE_DIR}/{name}")
            print(f"  uploaded {name} ({len(body) / 1024:.0f} KB)", flush=True)
            time.sleep(PAUSE_BETWEEN_UPLOADS)
    finally:
        try:
            ftp.quit()
        except (ftplib.all_errors, OSError):
            ftp.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="write out/ only, upload nothing")
    parser.add_argument("--force", action="store_true", help="pull and upload even if the server's prices are fresh")
    args = parser.parse_args()

    # a scheduled run quits early while the last upload is recent; a run started by hand always goes ahead
    if not args.dry_run and not args.force and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        age = minutes_old(FRESHNESS_FILE)
        if age is not None and age < FRESH_MINUTES:
            print(f"prices on the server are {age:.0f} min old (under {FRESH_MINUTES}): nothing to do", flush=True)
            return 0

    client_id, client_secret = env("BLIZZARD_CLIENT_ID"), env("BLIZZARD_CLIENT_SECRET")
    ftp_login = None if args.dry_run else (env("FTP_HOST"), env("FTP_USER"), env("FTP_PASS"))

    files: dict[str, bytes] = {}
    typical: dict[str, dict[str, int]] = {}
    failed = []
    token = access_token(client_id, client_secret)
    for region in REGIONS:
        try:
            index, _ = get(region, token, "/data/wow/ruleset/index")
            live = {r["id"] for r in index.get("rulesets", [])}
            print(f"{region}: rulesets {sorted(live)}", flush=True)
            region_files, region_typical = pull(region, token, live)
            files.update(region_files)
            typical.update(region_typical)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            # one region failing leaves its old files on the server; the other region still goes up
            failed.append(region)
            print(f"{region} failed: {scrub(f'{type(e).__name__}: {e}')}", flush=True)

    if typical and not failed:  # a partial day would leave holes, so history waits for a full pull
        try:
            files.update(history(typical))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            print(f"history skipped: {scrub(f'{type(e).__name__}: {e}')}", flush=True)

    if args.dry_run:
        (OUT / "history").mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (OUT / name).write_bytes(body)
        print(f"dry run: wrote {len(files)} files to {OUT}", flush=True)
    elif files:
        upload(files, *ftp_login)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # never let a traceback print request details
        print(f"failed: {scrub(f'{type(e).__name__}: {e}')}", flush=True)
        sys.exit(1)
