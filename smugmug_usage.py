#!/usr/bin/env python3
"""
smugmug_usage.py - Total up how much space your SmugMug account uses.

READ-ONLY: every request this script sends is an HTTP GET, and the
authorization it asks for is Permissions=Read, so it cannot change,
move, or delete anything in your SmugMug account.

It walks every album you own via the SmugMug API v2, sums each image's
ArchivedSize (the size of the original file you uploaded), and writes a
per-album CSV breakdown.

Local files it writes (nothing else):
  - .smugmug_tokens.json in your home folder (your saved login)
  - smugmug_usage_by_album.csv in the current folder
    (if that file is open/locked, a timestamped copy is written instead)

Credentials come from two environment variables (never put them in code):
  SMUGMUG_API_KEY, SMUGMUG_API_SECRET
"""

import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import requests
from requests_oauthlib import OAuth1Session

API_ORIGIN = "https://api.smugmug.com"
API_BASE = API_ORIGIN + "/api/v2"
OAUTH_BASE = API_ORIGIN + "/services/oauth/1.0a"
TOKEN_FILE = Path.home() / ".smugmug_tokens.json"
PAGE_SIZE = 100
CSV_OUT = "smugmug_usage_by_album.csv"
MAX_RETRIES = 6


class AuthError(Exception):
    pass


def human(n: float) -> str:
    """Format a byte count as a readable size (1024-based)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.2f} {unit}"
        n /= 1024
    return f"{n:,.2f} TB"


def get_credentials(env=None):
    """Return (key, secret) from the environment, or None if either is missing."""
    env = os.environ if env is None else env
    key = (env.get("SMUGMUG_API_KEY") or "").strip().strip('"')
    secret = (env.get("SMUGMUG_API_SECRET") or "").strip().strip('"')
    if not key or not secret:
        return None
    return key, secret


# ---------------------------------------------------------------- auth
def _token_get(sess: OAuth1Session, url: str) -> dict:
    """Fetch an OAuth token with a signed GET; return the decoded fields."""
    r = sess.get(url, timeout=60)
    if r.status_code >= 400:
        raise AuthError(f"SmugMug rejected the request ({r.status_code}): {r.text[:200]}")
    tok = dict(parse_qsl(r.text.strip()))
    if "oauth_token" not in tok or "oauth_token_secret" not in tok:
        raise AuthError(f"Unexpected token response: {r.text[:200]}")
    return tok


def _session(key, secret, token, token_secret) -> OAuth1Session:
    return OAuth1Session(key, client_secret=secret,
                         resource_owner_key=token,
                         resource_owner_secret=token_secret)


def load_saved_tokens():
    if not TOKEN_FILE.exists():
        return None
    try:
        tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return tok["token"], tok["secret"]
    except (ValueError, KeyError, TypeError, OSError):
        print(f"Saved login at {TOKEN_FILE} is unreadable; signing in again.")
        return None


def authorize(key: str, secret: str) -> OAuth1Session:
    """Out-of-band (PIN) OAuth 1.0a flow. Requests READ-only access."""
    oauth = OAuth1Session(key, client_secret=secret, callback_uri="oob")
    req = _token_get(oauth, OAUTH_BASE + "/getRequestToken")
    auth_url = OAUTH_BASE + "/authorize?" + urlencode(
        {"oauth_token": req["oauth_token"], "Access": "Full", "Permissions": "Read"})
    print("\nOpen this URL, approve access, then paste the PIN shown:\n")
    print("  " + auth_url + "\n")
    pin = input("PIN: ").strip()
    if not pin:
        raise AuthError("No PIN entered.")

    oauth = OAuth1Session(key, client_secret=secret,
                          resource_owner_key=req["oauth_token"],
                          resource_owner_secret=req["oauth_token_secret"],
                          verifier=pin)
    acc = _token_get(oauth, OAUTH_BASE + "/getAccessToken")
    TOKEN_FILE.write_text(json.dumps({"token": acc["oauth_token"],
                                      "secret": acc["oauth_token_secret"]}),
                          encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    print(f"Saved login to {TOKEN_FILE}\n")
    return _session(key, secret, acc["oauth_token"], acc["oauth_token_secret"])


# ---------------------------------------------------------------- API
def _retry_wait(resp, attempt: int) -> float:
    """Seconds to wait before a retry: honor Retry-After (capped at 120s),
    otherwise exponential backoff capped at 60s."""
    ra = resp.headers.get("Retry-After") if resp is not None else None
    if ra:
        try:
            return max(0.0, min(float(ra), 120.0))
        except ValueError:
            try:
                when = parsedate_to_datetime(ra)
                delta = (when - datetime.now(when.tzinfo)).total_seconds()
                return max(0.0, min(delta, 120.0))
            except (TypeError, ValueError):
                pass
    return float(min(2 ** attempt, 60))


def api_get(sess, url: str, params=None) -> dict:
    """Signed GET returning the JSON 'Response' object. Retries on
    429 / 5xx / network errors. Raises AuthError on 401."""
    if url.startswith("/"):
        url = API_ORIGIN + url
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = sess.get(url, params=params,
                         headers={"Accept": "application/json"}, timeout=60)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            wait = _retry_wait(None, attempt)
            print(f"  Network error ({type(e).__name__}), retrying in {wait:.0f}s...",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            wait = _retry_wait(r, attempt)
            print(f"  HTTP {r.status_code}, retrying in {wait:.0f}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 401:
            raise AuthError("401 Unauthorized")
        r.raise_for_status()
        try:
            return r.json()["Response"]
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"Unexpected response from {url}: {r.text[:200]}") from e
    raise RuntimeError(f"Gave up on {url} after {MAX_RETRIES} tries ({last})")


def paged(sess, url: str, key: str, params: dict):
    """Yield items from a paged collection, following Pages.NextPage.
    Stops if SmugMug ever repeats a page URL (prevents infinite loops)."""
    params = dict(params, start=1, count=PAGE_SIZE)
    seen_urls = set()
    while url:
        resp = api_get(sess, url, params)
        yield from resp.get(key) or []
        nxt = (resp.get("Pages") or {}).get("NextPage")
        if not nxt or nxt in seen_urls:
            break
        seen_urls.add(nxt)
        url, params = nxt, None  # NextPage already carries start/count/filters


def video_size(sess, img):
    """For videos, ArchivedSize is the poster JPG, not the video. The
    stored video's size comes from the LargestVideo endpoint (MP4s).
    Returns None if SmugMug doesn't report one (e.g. some MPEG uploads)."""
    uri = ((img.get("Uris") or {}).get("LargestVideo") or {}).get("Uri")
    if not uri and img.get("ImageKey"):
        uri = f"/api/v2/image/{img['ImageKey']}-{img.get('Serial', 0)}!largestvideo"
    if not uri:
        return None
    try:
        lv = api_get(sess, uri).get("LargestVideo") or {}
    except requests.HTTPError:
        return None
    s = lv.get("Size")
    return s if isinstance(s, (int, float)) and s > 0 else None


# ---------------------------------------------------------------- tally
@dataclass
class Totals:
    photo_bytes: int = 0
    video_bytes: int = 0
    photos: int = 0
    videos: int = 0
    unknown: int = 0
    dupes: int = 0
    seen: set = field(default_factory=set)        # ImageKeys already counted
    no_size_videos: list = field(default_factory=list)


def tally_album(images, album_name: str, totals: Totals, get_video_size):
    """Add one album's images to the running totals.

    Collected images appear in more than one album; each ImageKey is
    counted once. Videos are sized with get_video_size(img); files with
    no usable size are counted as unknown. Returns (bytes, files) for
    this album.
    """
    a_bytes = a_count = 0
    for img in images:
        ik = img.get("ImageKey")
        if ik is not None:
            if ik in totals.seen:
                totals.dupes += 1
                continue
            totals.seen.add(ik)
        if img.get("IsVideo"):
            size = get_video_size(img)
            if size is None:
                totals.unknown += 1
                totals.no_size_videos.append(f"{album_name}/{img.get('FileName', '?')}")
                continue
            a_bytes += size
            a_count += 1
            totals.video_bytes += size
            totals.videos += 1
            continue
        size = img.get("ArchivedSize")
        if not isinstance(size, (int, float)) or size <= 0:
            totals.unknown += 1
            continue
        a_bytes += size
        a_count += 1
        totals.photo_bytes += size
        totals.photos += 1
    return a_bytes, a_count


def write_csv(rows, base=CSV_OUT) -> str:
    """Write per-album CSV. UTF-8 with BOM so Excel shows accents/emoji.
    If the file is locked (open in Excel), write a timestamped copy."""
    stem = base[:-4] if base.endswith(".csv") else base
    targets = [base, f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.csv"]
    for path in targets:
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Album", "Files counted", "Album ImageCount",
                            "Bytes", "Size", "Note"])
                w.writerows(rows)
            return path
        except PermissionError:
            print(f"  {path} is open/locked; trying another name...", file=sys.stderr)
    raise PermissionError("Could not write CSV output.")


# ---------------------------------------------------------------- main
def run() -> int:
    creds = get_credentials()
    if creds is None:
        print("No API key/secret. Set the SMUGMUG_API_KEY and SMUGMUG_API_SECRET "
              "environment variables (see README).")
        return 2
    key, secret = creds

    saved = load_saved_tokens()
    sess = _session(key, secret, *saved) if saved else authorize(key, secret)
    try:
        user = api_get(sess, API_BASE + "!authuser")["User"]
    except AuthError:
        if not saved:
            raise
        print("Saved login was rejected; signing in again.")
        sess = authorize(key, secret)
        user = api_get(sess, API_BASE + "!authuser")["User"]

    nick = user["NickName"]
    print(f"Signed in as {nick}. Listing albums...")
    albums = list(paged(sess, f"{API_BASE}/user/{nick}!albums", "Album",
                        {"_filter": "AlbumKey,Name,UrlPath,ImageCount"}))
    print(f"{len(albums)} albums found.\n")

    totals = Totals()
    rows, failed = [], []

    for i, alb in enumerate(albums, 1):
        name = alb.get("Name") or alb.get("AlbumKey") or "(unnamed)"
        note = ""
        a_bytes = a_count = 0
        url = f"{API_BASE}/album/{alb['AlbumKey']}!images"
        try:
            images = paged(sess, url, "AlbumImage",
                           {"_filter": "ImageKey,Serial,FileName,ArchivedSize,IsVideo",
                            "_filteruri": "LargestVideo"})
            a_bytes, a_count = tally_album(images, name, totals,
                                           lambda img: video_size(sess, img))
        except AuthError:
            raise
        except (requests.HTTPError, RuntimeError) as e:
            note = f"ERROR: {e}"[:200]
            failed.append(name)
            print(f"[{i}/{len(albums)}] {name}: skipped ({note})", file=sys.stderr)
        else:
            print(f"[{i}/{len(albums)}] {name}: {a_count} files, {human(a_bytes)}")
        rows.append([alb.get("UrlPath") or name, a_count, alb.get("ImageCount", ""),
                     a_bytes, human(a_bytes), note])

    rows.sort(key=lambda r: r[3], reverse=True)
    out = write_csv(rows)

    t = totals
    print("\n" + "=" * 50)
    print(f"Photos : {t.photos:>8,}  {human(t.photo_bytes)}")
    print(f"Videos : {t.videos:>8,}  {human(t.video_bytes)}")
    print(f"TOTAL  : {t.photos + t.videos:>8,}  {human(t.photo_bytes + t.video_bytes)}")
    if t.unknown:
        vids = f", incl. {len(t.no_size_videos)} video(s)" if t.no_size_videos else ""
        print(f"({t.unknown} files had no size reported and weren't counted{vids})")
        for v in t.no_size_videos[:10]:
            print(f"   no video size: {v}")
    if t.dupes:
        print(f"({t.dupes} collected/duplicate references skipped)")
    if failed:
        print(f"WARNING: {len(failed)} album(s) could not be read and are NOT in the "
              f"total: {', '.join(failed[:10])}{' ...' if len(failed) > 10 else ''}")
    print(f"\nPer-album breakdown (largest first): {out}")
    return 1 if failed else 0


def main():
    # Never let an odd character in an album name crash console output.
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        print("\nStopped. Nothing in your SmugMug account was changed.")
        sys.exit(130)
    except AuthError as e:
        print(f"\nSign-in failed: {e}\nCheck your key/secret, then delete "
              f"{TOKEN_FILE} and run again.")
        sys.exit(3)
    except (RuntimeError, requests.RequestException, OSError) as e:
        print(f"\nStopped: {e}\nSmugMug may be down or your connection dropped. "
              f"Nothing in your SmugMug account was changed; just run it again.")
        sys.exit(4)


if __name__ == "__main__":
    main()
