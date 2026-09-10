# EDGAR Insider Cluster-Buy Screen

Detects companies where **2 or more distinct insiders made open-market purchases**
within a rolling 30-day window, from SEC Form 4 filings.

Insiders sell for many reasons — diversification, tax, planned programs. They buy
for one. The cluster requirement filters out the lone token purchase.

## Setup

```bash
pip install requests
```

That's the only dependency, and it's only needed for live mode. Fixture mode runs
on the standard library alone.

## Run

**Offline — verify it works before touching the network:**

```bash
python edgar_insider.py --fixtures fixtures/ --out clusters.csv
```

Expected: 3 clusters — EXIN `high`, NUPO `review`, PLCM `suspect`.

Fixture mode does no networking, so the offering cross-reference is skipped
and same-day clusters stay `suspect` for want of anything to clear them.
Pass `--user-agent` alongside `--fixtures` to run the cross-reference anyway.

**Live:**

```bash
python edgar_insider.py \
    --start 2026-09-01 --end 2026-09-09 \
    --user-agent "Your Name you@yourfirm.com" \
    --out clusters.csv
```

### The User-Agent is not optional

SEC rejects requests without a User-Agent containing a real name and email,
returning **HTTP 403**. If you see a 403, that is the cause — not rate limiting.
The script raises a specific error for this so you don't debug the wrong layer.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--window` | 30 | Rolling window in days |
| `--min-buyers` | 2 | Distinct insiders required |
| `--cache` | `.edgar_cache` | Every response cached by URL; re-runs don't re-fetch |
| `--fixtures` | — | Offline mode, no network |

## What counts

Only `transactionCode = P` in the **non-derivative** table.

Excluded: `A` grant, `S` sale, `M` option exercise, `F` tax withholding, `G` gift,
`C` conversion. The derivative table is never merged in — an option transaction
is not an open-market purchase.

## Output columns

`ticker, issuer, issuer_cik, confidence, buyer_count, total_value, titles,
first_date, last_date, max_pct_of_holdings, has_officer, has_10b5_1,
same_day_cluster, price_vs_market, new_position, offering_nearby,
offering_forms, filing_urls`

Beyond the raw count:

- **`max_pct_of_holdings`** — the largest purchase as a fraction of that insider's
  resulting position. An officer adding 40% to their stake is a different event
  from a director buying a round lot.
- **`has_10b5_1`** — purchase made under a pre-arranged plan. Weaker signal;
  the decision was made months earlier.

## Financing participation — the big false positive

Transaction code `P` covers open-market **and private** purchases. An insider
taking down part of their own company's private placement or registered offering
files a `P` exactly like a conviction buy, and it scores *better* on every raw
metric: several officers at once, one date, a large fraction of the resulting
stake. Before these filters, the top of the output was reliably financing
participation at a negotiated price with warrants attached.

Four columns separate the two. **Nothing is excluded** — flagged rows are ranked
below clean ones so you still see them.

- **`same_day_cluster`** — every purchase on one date, from 3+ distinct buyers.
  A cheap proxy for "settled together". This is a *trigger* for the check below,
  not a verdict on its own.
- **`offering_nearby` / `offering_forms`** — an offering-type filing by the
  issuer near the cluster. This is the real evidence. Only clusters that are
  same-day, or where somebody put more than half their resulting stake in at
  once, cost the extra request. See below for what counts and when.
- **`price_vs_market`** — `identical:4.88`, `varied:0.19-0.25`, `unpriced`. One
  identical odd price shared by several insiders is what negotiated deal terms
  look like. Reported, never scored — see below.
- **`new_position`** — `max_pct_of_holdings` rounds to 1.000, so the insider held
  nothing beforehand. This reads as maximum conviction and usually is not: for a
  CEO or CFO it more often means there was no prior skin in the game.

### `confidence`

| Value | Meaning |
|---|---|
| `high` | No flag fired. Sorted first. |
| `review` | `new_position` — somebody's entire stake arrived in this cluster. |
| `suspect` | `offering_nearby`, or same-day with the cross-reference unavailable. |

**Same-day alone is not enough, and identical price alone is not enough.** WIX is
the reason: six officers, one date, one identical price of $59.89 — and no
offering filing anywhere near it, because those were routine sub-$500 plan
purchases. QNRX looks nearly identical on those two metrics (four officers, one
date, $4.88) but has an 8-K filed three days earlier announcing the ~$50M
placement that closed that day. The offering cross-reference is what tells them
apart, so same-day only stands on its own when the cross-reference could not run.

### What counts as an offering filing

`424B*`, `S-1`, `S-3` and `D` are offering documents by their nature and count
on sight. `8-K` does not — issuers file one for everything — so the **item
numbers** decide:

| Item | | |
|---|---|---|
| `1.01` | entry into a material definitive agreement | counts (the purchase agreement) |
| `3.02` | unregistered sale of equity securities | counts (the PIPE itself) |
| everything else | `2.02` earnings, `5.02` officer changes, `7.01` Reg FD, `9.01` exhibits | ignored |

8-K cannot simply be dropped in favour of the other four. An unregistered sale
has **no `424B`**, because nothing was registered, and its **Form D can trail
the close by 15 days**. The Item 3.02 8-K, due within 4 business days, is the
only filing that reliably lands near a PIPE.

`offering_forms` names the matched items inline — `8-K[1.01,3.02] 2026-08-28` —
so you can see exactly what fired.

### The window is asymmetric

**−5 days before the first purchase, +15 days after the last.** The paperwork
lands after the money: an Item 3.02 8-K is due within 4 business days, but a
Form D has up to 15, so a placement can close before the purchases and not be
on file until well after them. A tight ±5 window misses those.

### Two hosts

The archive is `www.sec.gov`; the submissions API is `data.sec.gov`. A `Host`
header pinned to the former makes the latter answer **404 with an HTML page**
rather than JSON, which the fetcher reads as "nothing published here" and passes
over silently. The fetcher no longer sets `Host` at all — `requests` derives it
per URL.

## Tests

```bash
python test_edgar_insider.py
```

101 assertions covering both directions — that clusters are caught, and that
grants, sales, single buyers and derivative transactions are *not* flagged.
A filter tested only on what it must catch will silently over-reject.

The financing filters get the same treatment. Fixtures cover a same-day
3-buyer identical-price cluster (must be `suspect`), a multi-day varied-price
cluster (must stay `high`), and a zero-prior-stake buy (must be `review`);
live-shaped cases replay QNRX and WIX against canned submissions JSON to prove
the cross-reference separates them, and an earnings-only issuer proves a `2.02`
8-K next to a same-day cluster is cleared rather than flagged. The window
boundaries are pinned at 5 days back and 14/15/16 days forward.

## Rate limiting

Throttled to 10 requests/second, SEC's published cap. Responses are cached to
disk, so a re-run over the same dates makes zero network calls.

## Known limits

- Daily index covers business days only; weekends are skipped automatically.
- A high `buyer_count` at a large company can reflect a routine event rather
  than conviction. Read `titles` and `max_pct_of_holdings`, not just the count.
- The offering cross-reference reads only the `recent` block of the submissions
  API — roughly the last 1,000 filings. Ample for a scan of recent dates; a scan
  of a range years back would need the older `filings.files` shards.
- Purchases below roughly $50k are usually noise. Filter in the CSV.
