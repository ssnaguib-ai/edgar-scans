#!/usr/bin/env python3
"""
edgar_insider.py - detect insider CLUSTER BUYING from SEC EDGAR Form 4 filings.

Signal: 2+ distinct insiders making OPEN-MARKET PURCHASES (transaction code P)
in the same issuer within a rolling window.

Insiders sell for many reasons. They buy for one.

Except when they do not. Code P also covers PRIVATE purchases, so an insider
taking an allocation in their own company's placement files the same code as
someone buying on the open market -- and outscores them, because a financing
closes on one date with several officers each putting in a large slice of
their resulting stake. Every cluster is therefore cross-referenced against the
issuer's recent filings and labelled in a `confidence` column. Nothing is
dropped; suspect rows are ranked below clean ones. See README.md.

Usage
-----
  # offline: run the whole pipeline against saved fixtures, no network
  python edgar_insider.py --fixtures fixtures/ --out clusters.csv

  # live: scan a date range on EDGAR
  python edgar_insider.py --start 2026-09-01 --end 2026-09-09 \
      --user-agent "Jane Doe jane@firm.com" --out clusters.csv

SEC requires a User-Agent with a real name and email. Without it you get 403.

Note that EDGAR is S3-backed, so a path that does not exist -- a daily index
for a market holiday, say -- also answers 403, with an AccessDenied XML body.
That is not throttling and is skipped rather than retried.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Transaction codes. Only P is signal.
# --------------------------------------------------------------------------
BUY_CODE = "P"          # open-market or private purchase
EXCLUDED_CODES = {
    "A": "grant/award",
    "S": "open-market sale",
    "M": "option exercise",
    "F": "shares withheld for tax",
    "G": "gift",
    "D": "disposition to issuer",
    "C": "conversion",
    "X": "option exercise (in-the-money)",
}

SEC_RATE_LIMIT_PER_SEC = 8      # under SEC's documented 10/sec ceiling
SEC_MIN_INTERVAL_CAP = 2.0      # never crawl slower than one request / 2s
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
# SEC's WAF blocks abusive clients for ~10 minutes, so out-waiting one needs
# quiet periods measured in minutes, not seconds.
BACKOFF_SCHEDULE_SEC = (10, 30, 90, 300, 600, 900, 900)
MAX_BACKOFF_SEC = 900.0
# EDGAR's archive sits on S3, which answers a key that does not exist with
# 403 AccessDenied rather than 404. That is not throttling and must not be
# retried: it just means nothing was published at that path.
S3_MISSING_OBJECT = re.compile(rb"<Code>(?:AccessDenied|NoSuchKey)</Code>")
EDGAR_DAILY_INDEX = ("https://www.sec.gov/Archives/edgar/daily-index/"
                     "{year}/QTR{qtr}/form.{ymd}.idx")
# Submissions API — a DIFFERENT HOST from the archive. See Fetcher.headers.
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"

# --------------------------------------------------------------------------
# Financing-participation filters.
#
# Transaction code P covers open-market AND private purchases. An insider
# taking down part of their own company's placement or registered offering is
# coded P exactly like a conviction buy on the open market, and looks BETTER
# on every raw metric: several officers, one date, a large fraction of the
# resulting stake. These flags separate the two without discarding anything.
# --------------------------------------------------------------------------
# The window is deliberately ASYMMETRIC, because the paperwork lands after the
# money. An Item 3.02 8-K is due within 4 business days of the sale, but a
# Form D has up to 15 days, so a placement can close before the purchases and
# still not be on file until well after them.
OFFERING_LOOKBACK_DAYS = 5      # days before the first purchase
OFFERING_LOOKAHEAD_DAYS = 15    # days after the last purchase
SAME_DAY_MIN_BUYERS = 3         # below this, one shared date is coincidence

# 8-K is filed for everything, so the form type alone is not a financing
# signal -- the item numbers are.
#   1.01  entry into a material definitive agreement (the purchase agreement)
#   3.02  unregistered sale of equity securities (the PIPE itself)
# Notably NOT 2.02 (results of operations): directors buying the week of
# earnings is ordinary conviction, and counting 2.02 flagged DKS and MAIR.
FINANCING_8K_ITEMS = {"1.01", "3.02"}
NEW_POSITION_EPS = 0.0005       # max_pct rounding to 1.000 => held nothing before

CONFIDENCE_ORDER = {"high": 0, "review": 1, "suspect": 2}

# Explicit, so the new columns land somewhere readable and filing_urls -- the
# one column too wide to skim -- stays last.
OUTPUT_COLUMNS = [
    "ticker", "issuer", "issuer_cik", "confidence", "buyer_count",
    "total_value", "titles", "first_date", "last_date", "max_pct_of_holdings",
    "has_officer", "has_10b5_1", "same_day_cluster", "price_vs_market",
    "new_position", "offering_nearby", "offering_forms", "filing_urls",
]


# --------------------------------------------------------------------------
# Network plumbing
# --------------------------------------------------------------------------
class Throttle:
    """Sliding-window limiter that adapts downward when SEC pushes back.

    SEC documents a 10 req/sec ceiling, but a sustained scan also trips an
    undocumented volume limit that shows up as 403 after thousands of
    requests. Recover from that by permanently crawling slower, then easing
    back only after a long clean streak.
    """

    def __init__(self, per_sec: float = SEC_RATE_LIMIT_PER_SEC):
        self.min_interval = 1.0 / per_sec
        self._floor = self.min_interval
        self._last = 0.0
        self._clean = 0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()

    def penalise(self) -> None:
        self._clean = 0
        self.min_interval = min(self.min_interval * 2, SEC_MIN_INTERVAL_CAP)

    def reward(self) -> None:
        self._clean += 1
        if self._clean >= 500 and self.min_interval > self._floor:
            self._clean = 0
            self.min_interval = max(self.min_interval * 0.8, self._floor)


class Fetcher:
    """Cached, throttled HTTP. Every response is written to disk keyed by URL."""

    def __init__(self, user_agent: str, cache_dir: Path, throttle: Throttle):
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC requires a User-Agent containing a real name and email, "
                'e.g. --user-agent "Jane Doe jane@firm.com". '
                "Requests without one are rejected with HTTP 403."
            )
        # NOTE: no static Host header. The archive lives on www.sec.gov but the
        # submissions API lives on data.sec.gov, and pinning Host to www.sec.gov
        # makes data.sec.gov answer 200-shaped HTML under a 404 instead of JSON.
        # requests derives the correct Host from the URL on its own.
        self.headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.throttle = throttle
        self.hits = 0
        self.misses = 0
        self.retries = 0
        self.missing = 0
        self._session = None

    def _get_session(self):
        import requests  # imported lazily so fixture mode needs no deps

        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(self.headers)
        return self._session

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".cache")

    def get(self, url: str) -> bytes | None:
        """Return the response body, or None if EDGAR has nothing at this URL.

        Raises RuntimeError only when SEC throttling outlasts every retry —
        the cache makes a re-run resume rather than restart.
        """
        cached = self._cache_path(url)
        if cached.exists():
            self.hits += 1
            return cached.read_bytes()

        session = self._get_session()

        for attempt in range(1, len(BACKOFF_SCHEDULE_SEC) + 1):
            self.throttle.wait()
            try:
                resp = session.get(url, timeout=30)
            except Exception as exc:  # noqa: BLE001
                if attempt == len(BACKOFF_SCHEDULE_SEC):
                    print(f"  ! network error {url}: {exc}", file=sys.stderr)
                    return None
                self.retries += 1
                delay = BACKOFF_SCHEDULE_SEC[attempt - 1]
                print(f"  ! network error ({exc}) — retrying in {delay}s "
                      f"({attempt}/{len(BACKOFF_SCHEDULE_SEC)})", file=sys.stderr)
                time.sleep(delay)
                continue

            if resp.status_code == 200:
                self.throttle.reward()
                self.misses += 1
                cached.write_bytes(resp.content)
                return resp.content

            if resp.status_code == 404 or self._is_missing(resp):
                self.missing += 1
                return None

            if resp.status_code in RETRY_STATUSES:
                self.throttle.penalise()
                if attempt == len(BACKOFF_SCHEDULE_SEC):
                    raise RuntimeError(
                        f"SEC returned HTTP {resp.status_code} for {url} on every "
                        f"one of {attempt} attempts spanning "
                        f"{sum(BACKOFF_SCHEDULE_SEC) // 60} minutes. This is "
                        "throttling, not a bad User-Agent (that fails on the "
                        "first request). Wait, then re-run — cached responses "
                        "mean the scan resumes where it stopped."
                    )
                self.retries += 1
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                print(f"  ! HTTP {resp.status_code} — throttled, pausing {delay:.0f}s, "
                      f"now {1 / self.throttle.min_interval:.1f} req/s "
                      f"(attempt {attempt}/{len(BACKOFF_SCHEDULE_SEC)})",
                      file=sys.stderr)
                time.sleep(delay)
                continue

            print(f"  ! HTTP {resp.status_code} {url}", file=sys.stderr)
            return None

        return None

    @staticmethod
    def _is_missing(resp) -> bool:
        """Tell a nonexistent S3 key apart from a real SEC throttle response.

        A holiday or not-yet-published daily index 403s with S3's AccessDenied
        XML; SEC's rate limiter answers with its own HTML notice instead.
        """
        return (resp.status_code == 403
                and bool(S3_MISSING_OBJECT.search(resp.content[:2000])))

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), MAX_BACKOFF_SEC)
            except ValueError:
                pass
        delay = BACKOFF_SCHEDULE_SEC[attempt - 1]
        return delay + random.uniform(0, delay * 0.25)


# --------------------------------------------------------------------------
# Form 4 parsing
# --------------------------------------------------------------------------
def _strip_ns(xml_bytes: bytes) -> bytes:
    """Form 4s appear both with and without a default namespace. Normalise."""
    return re.sub(rb'\sxmlns(:\w+)?="[^"]*"', b"", xml_bytes, count=0)


def _text(node, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    if found is None:
        return default
    # Most Form 4 leaves wrap their content in <value>
    val = found.find("value")
    target = val if val is not None else found
    return (target.text or default).strip()


def _num(node, path: str) -> float:
    raw = _text(node, path, "")
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def parse_form4(xml_bytes: bytes, source_url: str = "") -> list[dict]:
    """Return one dict per NON-DERIVATIVE transaction. Derivatives are ignored:
    an option grant or exercise is not an open-market purchase."""
    try:
        root = ET.fromstring(_strip_ns(xml_bytes))
    except ET.ParseError as exc:
        print(f"  ! unparseable XML {source_url}: {exc}", file=sys.stderr)
        return []

    issuer = root.find("issuer")
    issuer_cik = _text(issuer, "issuerCik")
    issuer_name = _text(issuer, "issuerName")
    ticker = _text(issuer, "issuerTradingSymbol")

    # A single Form 4 can carry multiple reporting owners (joint filings).
    owners = root.findall("reportingOwner")
    if not owners:
        return []

    footnote_text = " ".join(
        (fn.text or "") for fn in root.findall(".//footnotes/footnote")
    ).lower()
    aff = _text(root, "aff10b5One", "")
    has_10b5_1 = bool(aff == "1" or "10b5-1" in footnote_text)

    # A joint Form 4 lists several reporting owners -- usually affiliated fund
    # entities -- but the transactions below are reported ONCE on their behalf,
    # not once per owner. The schema gives transactions no owner reference, so
    # credit them to the primary filer; crediting every owner would turn one
    # purchase into N "distinct buyers" and multiply its value by N.
    primary = owners[0]
    rels = [o.find("reportingOwnerRelationship") for o in owners]
    is_officer = any(_text(r, "isOfficer") in ("1", "true") for r in rels)
    is_director = any(_text(r, "isDirector") in ("1", "true") for r in rels)
    title = next((t for t in (_text(r, "officerTitle") for r in rels) if t),
                 "Director" if is_director else "")
    owner_cik = _text(primary, "reportingOwnerId/rptOwnerCik")
    owner_name = _text(primary, "reportingOwnerId/rptOwnerName")

    rows: list[dict] = []
    # NOTE: only nonDerivativeTable. Do not merge with derivativeTable.
    for txn in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(txn, "transactionCoding/transactionCode")
        if code != BUY_CODE:
            continue

        shares = _num(txn, "transactionAmounts/transactionShares")
        price = _num(txn, "transactionAmounts/transactionPricePerShare")
        held_after = _num(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction")

        rows.append({
            "issuer_cik": issuer_cik,
            "issuer_name": issuer_name,
            "ticker": ticker,
            "owner_cik": owner_cik,
            "owner_name": owner_name,
            "title": title,
            "is_officer": is_officer,
            "is_director": is_director,
            "date": _text(txn, "transactionDate"),
            "shares": shares,
            "price": price,
            "value": shares * price,
            # purchase as a share of the resulting position: a CEO adding
            # 20% to their stake is a different event from a token lot
            "pct_of_holdings": (shares / held_after) if held_after else 0.0,
            "has_10b5_1": has_10b5_1,
            "url": source_url,
        })
    return rows


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------
def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def find_clusters(txns: list[dict], window_days: int = 30,
                  min_buyers: int = 2) -> list[dict]:
    """Group purchases by issuer; flag issuers with >= min_buyers DISTINCT
    insiders buying inside any rolling window_days span."""
    by_issuer: dict[str, list[dict]] = defaultdict(list)
    for t in txns:
        if _parse_date(t["date"]):
            by_issuer[t["issuer_cik"]].append(t)

    clusters = []
    for cik, group in by_issuer.items():
        group.sort(key=lambda t: t["date"])
        best = None
        for i, anchor in enumerate(group):
            start = _parse_date(anchor["date"])
            window = [
                t for t in group[i:]
                if (_parse_date(t["date"]) - start).days <= window_days
            ]
            buyers = {t["owner_cik"] for t in window}
            if len(buyers) >= min_buyers:
                if best is None or len(buyers) > best["buyer_count"]:
                    dates = sorted(t["date"] for t in window)
                    titles = sorted({t["title"] for t in window if t["title"]})
                    max_pct = round(max(t["pct_of_holdings"] for t in window), 4)
                    best = {
                        "ticker": window[0]["ticker"],
                        "issuer": window[0]["issuer_name"],
                        "issuer_cik": cik,
                        "buyer_count": len(buyers),
                        "total_value": round(sum(t["value"] for t in window), 2),
                        "titles": "; ".join(titles),
                        "first_date": dates[0],
                        "last_date": dates[-1],
                        "max_pct_of_holdings": max_pct,
                        "has_officer": any(t["is_officer"] for t in window),
                        "has_10b5_1": any(t["has_10b5_1"] for t in window),
                        # Every purchase on one date, by enough insiders that
                        # the shared date is not coincidence. Cheap proxy for
                        # "settled together", i.e. a financing close.
                        "same_day_cluster": (dates[0] == dates[-1]
                                             and len(buyers) >= SAME_DAY_MIN_BUYERS),
                        "price_vs_market": _price_pattern(window),
                        # Rounds to 1.000: the insider held nothing before this
                        # purchase. Reads as maximum conviction; for a CEO or
                        # CFO it more often means there was no prior stake.
                        "new_position": max_pct >= 1.0 - NEW_POSITION_EPS,
                        "offering_nearby": False,
                        "offering_forms": "",
                        "confidence": "",
                        "filing_urls": " | ".join(
                            sorted({t["url"] for t in window if t["url"]})),
                    }
        if best:
            clusters.append(best)

    clusters.sort(key=lambda c: (-c["buyer_count"], -c["total_value"]))
    return clusters


def _price_pattern(window: list[dict]) -> str:
    """Describe the prices paid across a cluster.

    One identical price shared by several insiders is what a negotiated
    financing looks like: everybody takes the same deal terms on the same day.
    Independent market fills spread out, if only by pennies.

    Not a discriminator on its own -- a small same-day plan purchase also
    prints one price -- so this is reported, never scored. See
    assign_confidence().
    """
    prices = sorted({round(t["price"], 4) for t in window if t["price"] > 0})
    if not prices:
        return "unpriced"
    if len(prices) == 1:
        px = prices[0]
        buyers = len({t["owner_cik"] for t in window if t["price"] > 0})
        tag = "identical" if buyers > 1 else "single-buyer"
        # A round number is a plausible limit order; an odd one is a quoted term.
        if abs(px - round(px)) < 1e-9 or abs(px * 4 - round(px * 4)) < 1e-9:
            tag += "-round"
        return f"{tag}:{px:g}"
    return f"varied:{prices[0]:g}-{prices[-1]:g}"


# --------------------------------------------------------------------------
# Offering cross-reference
# --------------------------------------------------------------------------
def _norm_form(form: str) -> str:
    f = (form or "").strip().upper()
    return f[:-2] if f.endswith("/A") else f


def _is_offering_form(form: str) -> bool:
    """Forms an issuer files around a placement or registered offering.

    Prefix matching, so S-3 also catches S-3ASR/S-3MEF and 424B catches every
    prospectus flavour. The bare "D" (Reg D placement notice) is matched
    exactly, because a prefix would swallow DEF 14A and DEFA14A.
    """
    f = _norm_form(form)
    return (f == "D"
            or f.startswith("424B")
            or f.startswith("8-K")
            or f.startswith("S-1")
            or f.startswith("S-3"))


def _financing_items(form: str, items: str | None,
                     items_known: bool = True) -> list[str] | None:
    """Why this filing counts as a financing signal, or None if it does not.

    Returns the matched 8-K item numbers -- empty list for the forms that are
    offering documents by their very nature and carry no items.

    An unregistered sale has no 424B, because nothing was registered, and its
    Form D can trail the close by 15 days. The Item 3.02 8-K is the one filing
    that reliably lands within days of a PIPE, so 8-K has to stay in -- but
    only for the items that actually mean financing.
    """
    if not _is_offering_form(form):
        return None
    if not _norm_form(form).startswith("8-K"):
        return []                       # 424B*, S-1, S-3, D: offering by nature
    if not items_known:
        return []                       # no items column at all: cannot discriminate
    hits = sorted(FINANCING_8K_ITEMS
                  & {i.strip() for i in (items or "").split(",")})
    return hits or None


def offering_filings_near(fetcher, issuer_cik: str, first_date: str,
                          last_date: str,
                          lookback_days: int = OFFERING_LOOKBACK_DAYS,
                          lookahead_days: int = OFFERING_LOOKAHEAD_DAYS
                          ) -> tuple[bool, list[tuple[str, str]]] | None:
    """Offering-type filings by this issuer within +/- window_days of a cluster.

    Returns (found, [(form, filing_date), ...]), or None when the lookup could
    not be performed at all -- no fetcher, bad CIK, or EDGAR did not answer.
    None and (False, []) mean different things: "unknown" versus "checked,
    clean". assign_confidence() treats them differently.
    """
    if fetcher is None:
        return None
    digits = re.sub(r"\D", "", issuer_cik or "")
    if not digits:
        return None
    start, end = _parse_date(first_date), _parse_date(last_date)
    if not (start and end):
        return None
    lo = start - timedelta(days=lookback_days)
    hi = end + timedelta(days=lookahead_days)

    raw = fetcher.get(EDGAR_SUBMISSIONS.format(cik10=digits.zfill(10)))
    if not raw:
        return None
    try:
        recent = json.loads(raw)["filings"]["recent"]
        forms, filed = recent["form"], recent["filingDate"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"  ! unreadable submissions JSON for CIK {digits}: {exc}",
              file=sys.stderr)
        return None

    # Absent entirely (older shards) is not the same as present-but-blank for
    # one filing: the first means "cannot discriminate", the second means the
    # 8-K reported no items we care about.
    raw_items = recent.get("items")
    items_known = isinstance(raw_items, list) and len(raw_items) == len(forms)
    all_items = raw_items if items_known else [""] * len(forms)

    hits = []
    for form, fd, items in zip(forms, filed, all_items):
        d = _parse_date(fd)
        if not (d and lo <= d <= hi):
            continue
        matched = _financing_items(form, items, items_known)
        if matched is None:
            continue
        label = form.strip()
        if matched:
            label += "[" + ",".join(matched) + "]"
        hits.append((label, fd))
    hits.sort(key=lambda h: h[1])
    return bool(hits), hits


def add_offering_crossref(clusters: list[dict], fetcher=None) -> None:
    """Populate offering_nearby / offering_forms in place.

    Only clusters that already look like a financing get the extra request:
    everything on one date with enough buyers, or any cluster where somebody
    put more than half their resulting stake in at once.
    """
    for c in clusters:
        if not (c["same_day_cluster"] or c["max_pct_of_holdings"] > 0.5):
            c["_offering_checked"] = True   # nothing to check; treat as clean
            continue
        result = offering_filings_near(
            fetcher, c["issuer_cik"], c["first_date"], c["last_date"])
        if result is None:
            c["_offering_checked"] = False
            continue
        found, hits = result
        c["_offering_checked"] = True
        c["offering_nearby"] = found
        c["offering_forms"] = "; ".join(f"{f} {d}" for f, d in hits)


def assign_confidence(clusters: list[dict]) -> None:
    """Label each cluster and sort so clean rows come first. Nothing is dropped.

    - suspect: an offering-type filing sits next to the purchases, or the
      cluster is same-day and the cross-reference could not clear it.
    - review: somebody's whole position arrived in this cluster.
    - high:   no flag fired.

    NOTE on same_day_cluster. It is a trigger for the cross-reference, not a
    verdict by itself. WIX is why: six officers, one date, one identical price
    -- and no offering filing anywhere near it, because those were routine
    sub-$500 plan purchases. Scoring same-day alone as suspect buries exactly
    the broad-participation clusters this screen exists to surface. When the
    cross-reference cannot run, same-day falls back to suspect, since then
    there is nothing to clear it.
    """
    for c in clusters:
        checked = c.pop("_offering_checked", False)
        if c["offering_nearby"] or (c["same_day_cluster"] and not checked):
            c["confidence"] = "suspect"
        elif c["new_position"]:
            c["confidence"] = "review"
        else:
            c["confidence"] = "high"

    clusters.sort(key=lambda c: (CONFIDENCE_ORDER[c["confidence"]],
                                 -c["buyer_count"], -c["total_value"]))


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def load_fixtures(path: Path) -> list[dict]:
    txns = []
    for f in sorted(path.glob("*.xml")):
        txns.extend(parse_form4(f.read_bytes(), source_url=f.name))
    return txns


def form4_urls_for_day(fetcher: Fetcher, day: date) -> list[str]:
    url = EDGAR_DAILY_INDEX.format(
        year=day.year, qtr=(day.month - 1) // 3 + 1, ymd=day.strftime("%Y%m%d"))
    raw = fetcher.get(url)
    if not raw:
        # Weekday but no index: a market holiday, or today's index isn't out
        # yet (EDGAR publishes it after the close).
        print(f"  {day}: no daily index published")
        return []
    urls = []
    seen: set[str] = set()
    for line in raw.decode("latin-1").splitlines():
        if not line.startswith("4 "):          # form type column
            continue
        parts = line.split()
        path = parts[-1]
        if path.endswith(".txt"):
            # EDGAR indexes one line per filer, so a single filing repeats
            # under the issuer's path and each reporting owner's. Same
            # accession, byte-identical document: fetch and parse it once.
            accession = path.rsplit("/", 1)[-1]
            if accession in seen:
                continue
            seen.add(accession)
            # .txt is the full submission; the XML sits in the filing folder
            urls.append("https://www.sec.gov/Archives/" + path)
    return urls


def extract_xml_from_submission(raw: bytes) -> bytes | None:
    """A .txt submission wraps the Form 4 XML in SGML. Pull the XML out."""
    m = re.search(rb"<XML>\s*(<\?xml.*?</ownershipDocument>)", raw, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(rb"(<ownershipDocument.*?</ownershipDocument>)", raw, re.DOTALL)
    return m.group(1) if m else None


def scan_live(fetcher: Fetcher, start: date, end: date) -> list[dict]:
    txns: list[dict] = []
    day = start
    while day <= end:
        if day.weekday() < 5:  # EDGAR publishes on business days only
            urls = form4_urls_for_day(fetcher, day)
            if urls:
                print(f"  {day}: {len(urls)} Form 4 filings")
            for u in urls:
                raw = fetcher.get(u)
                if not raw:
                    continue
                xml = extract_xml_from_submission(raw)
                if xml:
                    txns.extend(parse_form4(xml, source_url=u))
        day += timedelta(days=1)
    return txns


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", type=Path,
                    help="offline mode: parse saved XML from this directory")
    ap.add_argument("--start", help="live mode start date YYYY-MM-DD")
    ap.add_argument("--end", help="live mode end date YYYY-MM-DD")
    ap.add_argument("--user-agent",
                    help='required for live mode: "Your Name you@email.com"')
    ap.add_argument("--cache", type=Path, default=Path(".edgar_cache"))
    ap.add_argument("--window", type=int, default=30, help="rolling window, days")
    ap.add_argument("--min-buyers", type=int, default=2)
    ap.add_argument("--out", type=Path, default=Path("clusters.csv"))
    args = ap.parse_args()

    fetcher = None
    if args.fixtures:
        print(f"Fixture mode: {args.fixtures}")
        txns = load_fixtures(args.fixtures)
        if args.user_agent:
            # Offline parsing, but the offering cross-reference still needs the
            # network. Without a User-Agent it is skipped and same-day clusters
            # stay "suspect" for want of anything to clear them.
            fetcher = Fetcher(args.user_agent, args.cache, Throttle())
    else:
        if not (args.start and args.end and args.user_agent):
            ap.error("live mode needs --start, --end and --user-agent")
        fetcher = Fetcher(args.user_agent, args.cache, Throttle())
        txns = scan_live(fetcher,
                         datetime.strptime(args.start, "%Y-%m-%d").date(),
                         datetime.strptime(args.end, "%Y-%m-%d").date())
        print(f"Cache: {fetcher.hits} hits, {fetcher.misses} fetched, "
              f"{fetcher.retries} retries, {fetcher.missing} not on EDGAR")

    print(f"Open-market purchases found: {len(txns)}")
    clusters = find_clusters(txns, args.window, args.min_buyers)
    print(f"Clusters ({args.min_buyers}+ buyers / {args.window}d): {len(clusters)}")

    add_offering_crossref(clusters, fetcher)
    assign_confidence(clusters)

    if clusters:
        with args.out.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
            w.writeheader()
            w.writerows(clusters)
        print(f"Wrote {args.out}")
        tally = Counter(c["confidence"] for c in clusters)
        print("  " + "  ".join(f"{k}={tally[k]}" for k in CONFIDENCE_ORDER
                               if tally[k]))
        for c in clusters:
            flags = []
            if c["has_10b5_1"]:
                flags.append("10b5-1")
            if c["same_day_cluster"]:
                flags.append("same-day")
            if c["new_position"]:
                flags.append("new-position")
            if c["offering_nearby"]:
                flags.append("offering:" + c["offering_forms"].split(";")[0].strip())
            tail = ("  [" + ", ".join(flags) + "]") if flags else ""
            print(f"  {c['confidence']:8} {c['ticker']:6} {c['buyer_count']} buyers  "
                  f"${c['total_value']:>14,.0f}  {c['titles']}{tail}")
    else:
        print("No clusters found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
