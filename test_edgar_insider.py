#!/usr/bin/env python3
"""Tests for edgar_insider.

Deliberately tests BOTH directions: that the filter catches what it should,
and that it lets through what it should. A filter tested only on the cases it
must catch will silently over-reject.
"""
import json
import sys
from pathlib import Path

from edgar_insider import (EDGAR_SUBMISSIONS, Fetcher, Throttle,
                           _financing_items, _is_offering_form,
                           add_offering_crossref,
                           assign_confidence, find_clusters, load_fixtures,
                           offering_filings_near, parse_form4)

FIX = Path(__file__).parent / "fixtures"
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --------------------------------------------------------------------------
# Financing-participation filters
# --------------------------------------------------------------------------
def _form4(ticker, icik, ocik, name, title, date, shares, price, after):
    """Minimal well-formed Form 4 for one code-P non-derivative purchase."""
    return (
        '<?xml version="1.0"?><ownershipDocument>'
        f"<issuer><issuerCik>{icik}</issuerCik><issuerName>{ticker} Inc"
        f"</issuerName><issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>"
        f"<reportingOwner><reportingOwnerId><rptOwnerCik>{ocik}</rptOwnerCik>"
        f"<rptOwnerName>{name}</rptOwnerName></reportingOwnerId>"
        f"<reportingOwnerRelationship><isOfficer>1</isOfficer>"
        f"<officerTitle>{title}</officerTitle></reportingOwnerRelationship>"
        "</reportingOwner><nonDerivativeTable><nonDerivativeTransaction>"
        f"<transactionDate><value>{date}</value></transactionDate>"
        "<transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
        f"<transactionAmounts><transactionShares><value>{shares}</value>"
        f"</transactionShares><transactionPricePerShare><value>{price}</value>"
        "</transactionPricePerShare></transactionAmounts>"
        f"<postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}"
        "</value></sharesOwnedFollowingTransaction></postTransactionAmounts>"
        "</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"
    ).encode()


class FakeFetcher:
    """Serves canned submissions JSON. Records what was requested."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        body = self.payloads.get(url)
        return json.dumps(body).encode() if body is not None else None


def _subs(rows, with_items=True):
    """rows: (form, filingDate) or (form, filingDate, items)."""
    rows = [r if len(r) == 3 else (r[0], r[1], "") for r in rows]
    recent = {"form": [r[0] for r in rows], "filingDate": [r[1] for r in rows]}
    if with_items:
        recent["items"] = [r[2] for r in rows]
    return {"filings": {"recent": recent}}


def _screen(txns, fetcher=None):
    clusters = find_clusters(txns, window_days=30, min_buyers=2)
    add_offering_crossref(clusters, fetcher)
    assign_confidence(clusters)
    return {c["ticker"]: c for c in clusters}


def offering_tests():
    print("\nFIXTURES -- financing participation")
    fx = _screen(load_fixtures(FIX))

    check("same-day 3-buyer identical-price cluster is suspect",
          fx.get("PLCM", {}).get("confidence") == "suspect",
          str(fx.get("PLCM", {}).get("confidence")))
    if "PLCM" in fx:
        check("PLCM same_day_cluster set", fx["PLCM"]["same_day_cluster"] is True)
        check("PLCM price pattern reports one identical odd price",
              fx["PLCM"]["price_vs_market"] == "identical:4.88",
              fx["PLCM"]["price_vs_market"])

    check("multi-day varied-price cluster stays high",
          fx.get("EXIN", {}).get("confidence") == "high",
          str(fx.get("EXIN", {}).get("confidence")))
    if "EXIN" in fx:
        check("EXIN not same_day_cluster", fx["EXIN"]["same_day_cluster"] is False)
        check("EXIN price pattern reports a range",
              fx["EXIN"]["price_vs_market"] == "varied:42.5-44",
              fx["EXIN"]["price_vs_market"])
        check("EXIN not a new position", fx["EXIN"]["new_position"] is False)

    check("zero-prior-stake buy is review",
          fx.get("NUPO", {}).get("confidence") == "review",
          str(fx.get("NUPO", {}).get("confidence")))
    if "NUPO" in fx:
        check("NUPO new_position set", fx["NUPO"]["new_position"] is True)
        check("NUPO max_pct rounds to 1.000",
              round(fx["NUPO"]["max_pct_of_holdings"], 3) == 1.0,
              str(fx["NUPO"]["max_pct_of_holdings"]))
        check("NUPO not marked suspect", fx["NUPO"]["confidence"] != "suspect")

    print("\nOFFERING CROSS-REFERENCE -- form matching")
    for form in ("8-K", "8-K/A", "424B5", "424B3", "S-1", "S-1/A", "S-3",
                 "S-3ASR", "D", "D/A"):
        check(f"{form} counts as an offering form", _is_offering_form(form))
    for form in ("4", "10-Q", "10-K", "DEF 14A", "DEFA14A", "SCHEDULE 13G",
                 "SC 13D/A", "144", "3", "ARS", "SD"):
        check(f"{form} does NOT count as an offering form",
              not _is_offering_form(form))

    print("\nOFFERING CROSS-REFERENCE -- live-shaped cases")

    # QNRX: four officers, one date, one identical placement price, and an 8-K
    # three days earlier announcing the ~$50M placement that closed that day.
    qnrx_cik = "0001671502"
    qnrx = []
    for ocik, name, title, sh, after in [
        ("3100001", "Myers Michael", "Chief Executive Officer", 20490, 37710),
        ("3100002", "Lawlor Sally", "Chief Financial Officer", 10244, 10684),
        ("3100003", "Carter Denise", "Chief Operating Officer", 20490, 37708),
        ("3100004", "Culverwell Anthony", "Director", 6146, 40357),
    ]:
        qnrx += parse_form4(_form4("QNRX", qnrx_cik, ocik, name, title,
                                   "2026-08-31", sh, "4.88", after))
    # Real item numbers, as data.sec.gov reports them. The 08-28 8-K carries
    # 1.01 + 3.02 (the placement); 08-20 is 5.02/5.07 (governance); 08-14 is
    # 2.02 (earnings). Only the first is financing.
    qnrx_ff = FakeFetcher({EDGAR_SUBMISSIONS.format(cik10=qnrx_cik): _subs([
        ("4", "2026-09-01"), ("4", "2026-09-01"), ("4", "2026-08-28"),
        ("8-K", "2026-08-28", "1.01,3.02,7.01,8.01,9.01"),
        ("8-K", "2026-08-20", "5.02,5.07,8.01"),
        ("10-Q", "2026-08-14", ""),
    ])})
    q = _screen(qnrx, qnrx_ff)["QNRX"]
    check("QNRX is suspect", q["confidence"] == "suspect", q["confidence"])
    check("QNRX offering_nearby true", q["offering_nearby"] is True)
    check("QNRX names the 8-K, its financing items and its date",
          q["offering_forms"] == "8-K[1.01,3.02] 2026-08-28", q["offering_forms"])
    check("QNRX governance 8-K outside the lookback is not listed",
          "2026-08-20" not in q["offering_forms"], q["offering_forms"])
    check("QNRX identical placement price reported",
          q["price_vs_market"] == "identical:4.88", q["price_vs_market"])

    # WIX: six officers, one date, one identical price -- but sub-$500 routine
    # purchases with no offering filing anywhere near them. The same-day
    # heuristic fires; the cross-reference must clear it.
    wix_cik = "0001576789"
    wix = []
    for ocik, name, title, sh, after in [
        ("1826257", "Abrahami Avishai", "Chief Executive Officer", 654, 503000),
        ("1969467", "Shemesh Lior", "CFO", 456, 217000),
        ("1969583", "Zohar Nir", "President", 376, 537000),
        ("1969635", "Even-Haim Yaniv", "CTO", 320, 139000),
        ("1976036", "Meyer Shelly B", "Chief People Officer", 479, 184000),
        ("1576789", "Shai Omer", "CMO", 490, 544000),
    ]:
        wix += parse_form4(_form4("WIX", wix_cik, ocik, name, title,
                                  "2026-08-31", sh, "59.89", after))
    wix_ff = FakeFetcher({EDGAR_SUBMISSIONS.format(cik10=wix_cik): _subs([
        ("4", "2026-09-02"), ("144", "2026-09-02"), ("4", "2026-08-31"),
        ("144", "2026-08-27"), ("4", "2026-08-26"), ("144", "2026-08-25"),
    ])})
    w = _screen(wix, wix_ff)["WIX"]
    check("WIX same_day heuristic fires", w["same_day_cluster"] is True)
    check("WIX cross-reference finds no offering",
          w["offering_nearby"] is False and w["offering_forms"] == "",
          w["offering_forms"])
    check("WIX is NOT flagged suspect despite being single-day",
          w["confidence"] == "high", w["confidence"])
    check("WIX identical price alone does not condemn it",
          w["price_vs_market"] == "identical:59.89", w["price_vs_market"])

    print("\n8-K ITEM NUMBERS -- form type alone is not a financing signal")
    check("8-K Item 3.02 (unregistered sale) is financing",
          _financing_items("8-K", "3.02,9.01") == ["3.02"])
    check("8-K Item 1.01 (material agreement) is financing",
          _financing_items("8-K", "1.01,9.01") == ["1.01"])
    check("8-K carrying both reports both",
          _financing_items("8-K", "1.01,3.02,7.01,8.01,9.01") == ["1.01", "3.02"])
    check("8-K Item 2.02 (earnings) is NOT financing",
          _financing_items("8-K", "2.02,8.01,9.01") is None)
    check("8-K Item 7.01/9.01 (Reg FD, exhibits) is NOT financing",
          _financing_items("8-K", "7.01,9.01") is None)
    check("8-K Item 5.02 (officer change) is NOT financing",
          _financing_items("8-K", "5.02,5.07,8.01") is None)
    check("8-K/A inherits the item test",
          _financing_items("8-K/A", "3.02") == ["3.02"])
    check("8-K with blank items is NOT financing",
          _financing_items("8-K", "") is None)
    check("2.02 is not matched as a substring of another item",
          _financing_items("8-K", "12.02,3.021") is None)
    check("Form D needs no items to count", _financing_items("D", "") == [])
    check("424B5 needs no items to count", _financing_items("424B5", "") == [])
    check("S-3 needs no items to count", _financing_items("S-3", "") == [])
    check("10-Q is not an offering form at all",
          _financing_items("10-Q", "") is None)
    check("no items column at all keeps every 8-K, rather than clearing them",
          _financing_items("8-K", None, items_known=False) == [])

    # An issuer whose ONLY nearby 8-K is earnings must come back clean. This is
    # DKS and MAIR: directors buying the week of results, previously suspect.
    earnings_cik = "0001089063"
    earnings = []
    for ocik, name in [("7000001", "Director One"), ("7000002", "Director Two"),
                       ("7000003", "Director Three")]:
        earnings += parse_form4(_form4("ERNX", earnings_cik, ocik, name,
                                       "Director", "2026-08-27", 100, "130.00",
                                       5000))
    e_ff = FakeFetcher({EDGAR_SUBMISSIONS.format(cik10=earnings_cik): _subs([
        ("8-K", "2026-08-25", "2.02,8.01,9.01"),
        ("10-Q", "2026-08-25", ""),
    ])})
    e = _screen(earnings, e_ff)["ERNX"]
    check("same-day cluster next to an EARNINGS 8-K is cleared to high",
          e["confidence"] == "high", e["confidence"])
    check("earnings 8-K is not listed in offering_forms",
          e["offering_forms"] == "", e["offering_forms"])

    print("\nASYMMETRIC WINDOW -- paperwork lands after the money")
    late_cik = "0009900001"
    late = parse_form4(_form4("LATE", late_cik, "9900010", "Buyer A", "CEO",
                              "2026-08-31", 1000, "5.00", 1000))
    late += parse_form4(_form4("LATE", late_cik, "9900011", "Buyer B", "CFO",
                               "2026-08-31", 1000, "5.00", 1000))
    late += parse_form4(_form4("LATE", late_cik, "9900012", "Buyer C", "COO",
                               "2026-08-31", 1000, "5.00", 1000))

    def _late(rows):
        return _screen(late, FakeFetcher(
            {EDGAR_SUBMISSIONS.format(cik10=late_cik): _subs(rows)}))["LATE"]

    check("Form D filed 14 days after the purchases is caught",
          _late([("D", "2026-09-14")])["offering_nearby"] is True)
    check("Form D filed 15 days after the purchases is caught (the deadline)",
          _late([("D", "2026-09-15")])["offering_nearby"] is True)
    check("Form D filed 16 days after is outside the lookahead",
          _late([("D", "2026-09-16")])["offering_nearby"] is False)
    check("lookback stays tight at 5 days",
          _late([("8-K", "2026-08-26", "3.02")])["offering_nearby"] is True)
    check("an 8-K 6 days BEFORE is outside the lookback",
          _late([("8-K", "2026-08-25", "3.02")])["offering_nearby"] is False)
    check("a late Form D still lands the cluster in suspect",
          _late([("D", "2026-09-12")])["confidence"] == "suspect")

    print("\nOFFERING CROSS-REFERENCE -- plumbing")
    check("submissions URL zero-pads the CIK to 10 digits",
          qnrx_ff.urls == ["https://data.sec.gov/submissions/CIK0001671502.json"],
          str(qnrx_ff.urls))

    short = FakeFetcher({EDGAR_SUBMISSIONS.format(cik10="0000000042"): _subs([])})
    offering_filings_near(short, "42", "2026-08-31", "2026-08-31")
    check("unpadded CIK is normalised before the request",
          short.urls == ["https://data.sec.gov/submissions/CIK0000000042.json"],
          str(short.urls))

    check("no fetcher means 'unknown', not 'clean'",
          offering_filings_near(None, "42", "2026-08-31", "2026-08-31") is None)
    check("unreachable EDGAR means 'unknown', not 'clean'",
          offering_filings_near(FakeFetcher({}), "42",
                                "2026-08-31", "2026-08-31") is None)
    check("malformed submissions JSON means 'unknown'",
          offering_filings_near(
              FakeFetcher({EDGAR_SUBMISSIONS.format(cik10="0000000042"):
                           {"filings": {}}}),
              "42", "2026-08-31", "2026-08-31") is None)
    check("blank issuer CIK means 'unknown'",
          offering_filings_near(FakeFetcher({}), "", "2026-08-31",
                                "2026-08-31") is None)

    # An unresolvable same-day cluster has nothing to clear it, so it stays
    # suspect rather than being promoted to high by a failed lookup.
    check("same-day cluster with a failed lookup falls back to suspect",
          _screen(wix, FakeFetcher({}))["WIX"]["confidence"] == "suspect")

    print("\nCROSS-REFERENCE IS NOT SPENT ON EVERY CLUSTER")
    quiet = FakeFetcher({})
    _screen(load_fixtures(FIX), quiet)
    # PLCM (same-day, 3 buyers) and NUPO (max_pct 1.0 > 0.5) qualify.
    # EXIN -- multi-day, 40% of a stake -- must not cost a request.
    check("only financing-shaped clusters trigger a request",
          len(quiet.urls) == 2, f"{len(quiet.urls)} requests: {quiet.urls}")
    check("an ordinary multi-day cluster costs no request",
          "CIK0000320193.json" not in " ".join(quiet.urls), str(quiet.urls))

    print("\nRANKING")
    ranked = find_clusters(load_fixtures(FIX), window_days=30, min_buyers=2)
    add_offering_crossref(ranked, None)
    assign_confidence(ranked)
    order = [c["confidence"] for c in ranked]
    check("high-confidence rows sort first",
          order == sorted(order, key=lambda c: {"high": 0, "review": 1,
                                                "suspect": 2}[c]), str(order))
    check("nothing is auto-excluded",
          len(ranked) == 3, f"{len(ranked)} clusters survived")

    print("\nHOST HEADER")
    f = Fetcher("Tester tester@example.com", Path(".edgar_cache"), Throttle())
    check("no static Host header pinned to www.sec.gov",
          "Host" not in f.headers,
          "a Host of www.sec.gov makes data.sec.gov answer 404 HTML")


def main():
    txns = load_fixtures(FIX)
    clusters = find_clusters(txns, window_days=30, min_buyers=2)
    by_ticker = {c["ticker"]: c for c in clusters}

    print("\nPOSITIVE -- must be detected")
    check("EXIN cluster detected", "EXIN" in by_ticker)
    if "EXIN" in by_ticker:
        c = by_ticker["EXIN"]
        check("EXIN counts 3 distinct buyers", c["buyer_count"] == 3,
              f"got {c['buyer_count']}")
        check("EXIN total value correct",
              abs(c["total_value"] - (25000 * 42.50 + 8000 * 43.10 + 3000 * 44.00)) < 0.01,
              f"got {c['total_value']}")
        check("EXIN flags officer involvement", c["has_officer"] is True)
        check("EXIN surfaces CEO and CFO titles",
              "Chief Executive Officer" in c["titles"] and "Chief Financial Officer" in c["titles"],
              c["titles"])
        check("EXIN flags 10b5-1 presence", c["has_10b5_1"] is True)
        check("EXIN date span correct",
              c["first_date"] == "2026-08-14" and c["last_date"] == "2026-08-25",
              f"{c['first_date']}..{c['last_date']}")
        check("EXIN max pct_of_holdings = CFO 8000/20000 = 0.4",
              abs(c["max_pct_of_holdings"] - 0.4) < 0.001,
              str(c["max_pct_of_holdings"]))

    print("\nNEGATIVE -- must NOT be flagged")
    check("grants (code A) excluded", "GRNT" not in by_ticker)
    check("sales (code S) excluded",
          not any(t["issuer_cik"] == "0000777777" for t in txns))
    check("single buyer not a cluster", "LONE" not in by_ticker)
    check("derivative-table purchase ignored",
          not any(t["owner_cik"] == "0006666666" for t in txns),
          "derivative option 'purchase' must not count")

    print("\nPARSING")
    ceo = parse_form4((FIX / "cluster_ceo_buy.xml").read_bytes())
    check("one txn parsed from CEO filing", len(ceo) == 1, f"got {len(ceo)}")
    if ceo:
        check("ticker parsed", ceo[0]["ticker"] == "EXIN")
        check("shares parsed", ceo[0]["shares"] == 25000)
        check("price parsed", ceo[0]["price"] == 42.50)
        check("officer flag parsed", ceo[0]["is_officer"] is True)

    print("\nTHRESHOLDS")
    strict = find_clusters(txns, window_days=30, min_buyers=4)
    check("min_buyers=4 finds nothing", len(strict) == 0, f"got {len(strict)}")
    narrow = find_clusters(txns, window_days=3, min_buyers=2)
    check("3-day window breaks the cluster", "EXIN" not in {c['ticker'] for c in narrow})

    ns = parse_form4(
        b'<?xml version="1.0"?><ownershipDocument xmlns="http://www.sec.gov/edgar/ownership">'
        b"<issuer><issuerCik>1</issuerCik><issuerName>NS</issuerName>"
        b"<issuerTradingSymbol>NS</issuerTradingSymbol></issuer>"
        b"<reportingOwner><reportingOwnerId><rptOwnerCik>9</rptOwnerCik>"
        b"<rptOwnerName>X</rptOwnerName></reportingOwnerId>"
        b"<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>"
        b"</reportingOwner><nonDerivativeTable><nonDerivativeTransaction>"
        b"<transactionDate><value>2026-08-01</value></transactionDate>"
        b"<transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
        b"<transactionAmounts><transactionShares><value>10</value></transactionShares>"
        b"<transactionPricePerShare><value>5</value></transactionPricePerShare>"
        b"</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable>"
        b"</ownershipDocument>")
    check("namespaced XML parses", len(ns) == 1, f"got {len(ns)}")

    print("\nJOINT FILINGS")
    joint = parse_form4(
        b'<?xml version="1.0"?><ownershipDocument>'
        b"<issuer><issuerCik>2</issuerCik><issuerName>JNT</issuerName>"
        b"<issuerTradingSymbol>JNT</issuerTradingSymbol></issuer>"
        + b"".join(
            b"<reportingOwner><reportingOwnerId><rptOwnerCik>%d</rptOwnerCik>"
            b"<rptOwnerName>Fund %d</rptOwnerName></reportingOwnerId>"
            b"<reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner>"
            b"</reportingOwnerRelationship></reportingOwner>" % (n, n)
            for n in (11, 12, 13)
        )
        + b"<nonDerivativeTable><nonDerivativeTransaction>"
        b"<transactionDate><value>2026-08-01</value></transactionDate>"
        b"<transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
        b"<transactionAmounts><transactionShares><value>100</value></transactionShares>"
        b"<transactionPricePerShare><value>10</value></transactionPricePerShare>"
        b"</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable>"
        b"</ownershipDocument>")
    check("joint filing credits txn once, not per owner", len(joint) == 1,
          f"got {len(joint)} rows for 1 transaction / 3 reporting owners")
    if joint:
        check("joint filing credits the primary filer",
              joint[0]["owner_cik"] == "11", joint[0]["owner_cik"])
        check("joint filing value not multiplied", joint[0]["value"] == 1000,
              str(joint[0]["value"]))
    solo = find_clusters(joint, window_days=30, min_buyers=2)
    check("joint filing alone is not a cluster", len(solo) == 0, f"got {len(solo)}")

    offering_tests()

    print("\nMALFORMED INPUT")
    check("garbage XML returns empty, no crash", parse_form4(b"<not valid") == [])
    check("empty input returns empty", parse_form4(b"") == [])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
