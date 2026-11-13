"""Hourly WoW Forever auction prices for Companion for WoW Forever.

Pulls every Forever auction house from Blizzard's Game Data API (US and EU), boils each one down to a few numbers per
item, and uploads one small file per region and ruleset to the app's server over FTPS.

    python3 -I prices.py             pull and upload (the GitHub Actions job)
    python3 -I prices.py --dry-run   pull and write out/ only, nothing uploaded

Secrets come only from the environment: BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET, FTP_HOST, FTP_USER, FTP_PASS.
Nothing here ever prints them, the access token Blizzard hands back is masked in the GitHub log, and any error is
printed with every secret value scrubbed out. Stdlib only, no third-party code.

Blizzard's API terms: the data is shown only to the app's own users, kept at most 30 days (each upload replaces the
last), and the app credits Blizzard as its source.
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
from datetime import datetime, timezone
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
PAUSE_BETWEEN_UPLOADS = 2.0  # seconds; the host's firewall blocks bursts of connections
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


def pull(region: str, token: str, live_rulesets: set[int]) -> dict[str, bytes]:
    """{file name: gzipped JSON} for the region's rulesets."""
    files = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for ruleset_id, name, houses in RULESETS:
        if ruleset_id not in live_rulesets:
            continue
        doc = {"v": FORMAT_VERSION, "region": region, "ruleset": name, "generated": now, "houses": {}}
        for faction, house in houses.items():
            started = time.time()
            data, modified = get(region, token, f"/data/wow/ruleset/{ruleset_id}/auctions/{house}")
            items = summarize(data.get("auctions") or [])
            doc["houses"][faction] = {"id": house, "updated": iso(modified), "items": items}
            print(f"  {region} {name} {faction}: {len(data.get('auctions') or [])} listings, {len(items)} items, "
                  f"{time.time() - started:.1f}s", flush=True)
        raw = json.dumps(doc, separators=(",", ":")).encode()
        files[f"{region}-{name}.json.gz"] = gzip.compress(raw, mtime=0)
    return files


def upload(files: dict[str, bytes], host: str, user: str, password: str) -> None:
    """One FTPS session: each file goes up under a temporary name and is renamed into place, so the app never
    downloads half a file. A pause between files keeps the host's firewall happy."""
    ftp = ftplib.FTP_TLS(host, timeout=60, context=ssl.create_default_context())
    try:
        ftp.login(user, password)
        ftp.prot_p()  # encrypt the files too, not just the login
        for d in ("public", REMOTE_DIR):
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
    args = parser.parse_args()

    client_id, client_secret = env("BLIZZARD_CLIENT_ID"), env("BLIZZARD_CLIENT_SECRET")
    ftp_login = None if args.dry_run else (env("FTP_HOST"), env("FTP_USER"), env("FTP_PASS"))

    files: dict[str, bytes] = {}
    failed = []
    token = access_token(client_id, client_secret)
    for region in REGIONS:
        try:
            index, _ = get(region, token, "/data/wow/ruleset/index")
            live = {r["id"] for r in index.get("rulesets", [])}
            print(f"{region}: rulesets {sorted(live)}", flush=True)
            files.update(pull(region, token, live))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            # one region failing leaves its old files on the server; the other region still goes up
            failed.append(region)
            print(f"{region} failed: {scrub(f'{type(e).__name__}: {e}')}", flush=True)

    if args.dry_run:
        OUT.mkdir(exist_ok=True)
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
