"""Hourly WoW Forever auction prices for Companion for WoW Forever.

Pulls every Forever auction house (its own list and its commodities) from Blizzard's Game Data API (US and EU),
boils each one down to a few numbers per item, and uploads one small file per region and ruleset to the app's
server, all in one HTTPS request to its api/prices-upload.php.

    python3 -I prices.py             pull and upload (the GitHub Actions job)
    python3 -I prices.py --force     pull and upload even if the server's prices are fresh
    python3 -I prices.py --dry-run   pull and write out/ only, nothing uploaded

GitHub starts scheduled jobs when it can and drops many of them, so the job is scheduled four times an hour and a
scheduled run quits early while the server's prices are under FRESH_MINUTES old: about one upload an hour, with
three spare chances when GitHub skips one. GitHub's schedule can still go hours without a run, so the app server's
cron also starts the workflow four times an hour (workflow_dispatch with from=cron, PRICES_TRIGGER=cron), and those
runs skip the same way. Runs started by hand always go ahead.

Secrets come only from the environment: BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET, PRICES_UPLOAD_KEY (the upload
page only takes price files, and only with this key). Nothing here ever prints them, the access token Blizzard hands back is masked in the GitHub log, and any error is
printed with every secret value scrubbed out. Stdlib only, no third-party code.

Once a day (the first run after 00:00 UTC) it also adds each item's typical price to a history file per auction
house, which keeps the last 30 days only. The history lives on the app's server, never in this repository.

Blizzard's API terms: the data is shown only to the app's own users, kept at most 30 days (each upload replaces the
last; history drops days older than 30), and the app credits Blizzard as its source.
"""
import argparse
import base64
import gzip
import http.client
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
FORMAT_VERSION = 1
# The app's server: the price files are read back from /prices/ (public, no login) and go up through UPLOAD_PATH.
SITE = os.environ.get("PRICES_SITE", "https://forever.masoombadi.top")
UPLOAD_PATH = "/api/prices-upload.php"
MAX_REQUEST = 6_000_000  # bytes per upload request, under the host's PHP post_max_size; a run is one request as a rule
HISTORY_DAYS = 30
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


class Site:
    """
    The app's server on one kept-open HTTPS connection: its host's firewall blocks IPs that open connections in
    bursts, so the freshness check, the history reads and the upload all share it. It is reopened only when the host
    closes it (an idle connection is closed after a few seconds, so a run takes about two).
    """

    def __init__(self, base: str):
        u = urllib.parse.urlsplit(base)
        if u.scheme == "https":
            self.conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=60, context=ssl.create_default_context())
        else:  # a local test server
            self.conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
        self.prefix = u.path.rstrip("/")

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        """(status, headers, body)"""
        h = {"User-Agent": "forever-prices-job", "Cache-Control": "no-cache", **(headers or {})}
        reused = self.conn.sock is not None
        try:
            self.conn.request(method, self.prefix + path, body=body, headers=h)
            r = self.conn.getresponse()
        except ConnectionError:
            self.conn.close()
            if not reused:
                raise
            # the host closed the kept-open connection while we were busy with Blizzard: open it once more
            self.conn.request(method, self.prefix + path, body=body, headers=h)
            r = self.conn.getresponse()
        return r.status, r.headers, r.read()

    def close(self):
        self.conn.close()


def read_site(site: Site, path: str) -> bytes | None:
    """A file from the app's server; None when it isn't there yet."""
    status, _, body = site.request("GET", f"/prices/{path}")
    if status == 404:
        return None
    if status != 200:
        raise OSError(f"/prices/{path}: HTTP {status}")
    return body


def minutes_old(site: Site, path: str) -> float | None:
    """How long ago the server's copy of a price file was replaced (one HEAD request); None when unknown."""
    try:
        status, headers, _ = site.request("HEAD", f"/prices/{path}")
        modified = headers.get("Last-Modified") if status == 200 else None
        if not modified:
            return None
        return (datetime.now(timezone.utc) - parsedate_to_datetime(modified)).total_seconds() / 60
    except (OSError, http.client.HTTPException, ValueError, TypeError):
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


def history(site: Site, typical: dict[str, dict[str, int]]) -> dict[str, bytes]:
    """Today's history files ("history/<house>.json.gz", then "history/index.json"), or none once today is done."""
    today = datetime.now(timezone.utc).date().isoformat()
    index = read_site(site, "history/index.json")
    if index and json.loads(index).get("day") == today:
        print(f"history: {today} already saved", flush=True)
        return {}
    files = {}
    for key, prices in typical.items():
        old = read_site(site, f"history/{key}.json.gz")
        doc = add_day(json.loads(gzip.decompress(old)) if old else None, today, prices)
        files[f"history/{key}.json.gz"] = gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)
    files["history/index.json"] = json.dumps({"day": today, "days": HISTORY_DAYS}).encode()  # last: marks the day done
    print(f"history: {today} added to {len(typical)} houses", flush=True)
    return files


def upload(site: Site, files: dict[str, bytes], key: str) -> None:
    """
    The files to the server's upload page, in order, as few requests as fit under MAX_REQUEST (one, as a rule).
    Body: for each file "<name>\\n<byte count>\\n<bytes>". The page checks the whole request before it writes anything
    and renames each file into place, so the app never downloads half a file; history/index.json comes last.
    """
    batches: list[list[bytes]] = [[]]
    size = 0
    for name, body in files.items():
        part = f"{name}\n{len(body)}\n".encode() + body
        if batches[-1] and size + len(part) > MAX_REQUEST:
            batches.append([])
            size = 0
        batches[-1].append(part)
        size += len(part)
    for parts in batches:
        body = b"".join(parts)
        status, _, answer = site.request("POST", UPLOAD_PATH, body, {"X-Upload-Key": key, "Content-Type": "application/octet-stream"})
        text = scrub(answer.decode("utf-8", "replace").strip())[:300]
        if status != 200:
            raise OSError(f"upload: HTTP {status}: {text}")
        print(f"  uploaded {len(parts)} files ({len(body) / 1024:.0f} KB): {text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="write out/ only, upload nothing")
    parser.add_argument("--force", action="store_true", help="pull and upload even if the server's prices are fresh")
    args = parser.parse_args()

    site = Site(SITE)
    # a scheduled or server-cron run quits early while the last upload is recent; a run started by hand always goes
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule" or os.environ.get("PRICES_TRIGGER") == "cron"
    if not args.dry_run and not args.force and scheduled:
        age = minutes_old(site, FRESHNESS_FILE)
        if age is not None and age < FRESH_MINUTES:
            print(f"prices on the server are {age:.0f} min old (under {FRESH_MINUTES}): nothing to do", flush=True)
            return 0

    client_id, client_secret = env("BLIZZARD_CLIENT_ID"), env("BLIZZARD_CLIENT_SECRET")
    upload_key = None if args.dry_run else env("PRICES_UPLOAD_KEY")

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
            files.update(history(site, typical))
        except (OSError, http.client.HTTPException, ValueError, KeyError) as e:
            print(f"history skipped: {scrub(f'{type(e).__name__}: {e}')}", flush=True)

    if args.dry_run:
        (OUT / "history").mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (OUT / name).write_bytes(body)
        print(f"dry run: wrote {len(files)} files to {OUT}", flush=True)
    elif files:
        upload(site, files, upload_key)
    site.close()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # never let a traceback print request details
        print(f"failed: {scrub(f'{type(e).__name__}: {e}')}", flush=True)
        sys.exit(1)
