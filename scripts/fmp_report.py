"""Turn the read-only audit into fmp_inventory.md plus a classified gap list."""
from __future__ import annotations
import json, pathlib, collections

ROOT = pathlib.Path("fmp_data")
A = json.load((ROOT / "fmp_inventory.json").open())
by_ds = {d["dataset"]: d for d in A}
have = {d["dataset"].split("/", 1)[-1] for d in A}

# endpoint -> (priority, verified name, retrieval shape, est requests, status)
CATALOG = [
 ("A1","income-statement-as-reported","per-symbol",68207,"MISSING_GET","raw XBRL line items, restatement + PIT auditing"),
 ("A1","balance-sheet-statement-as-reported","per-symbol",68207,"MISSING_GET",""),
 ("A1","cash-flow-statement-as-reported","per-symbol",68207,"MISSING_GET",""),
 ("A1","financial-statement-full-as-reported","per-symbol",68207,"MISSING_GET","superset of the three above"),
 ("A1","financial-reports-dates","per-symbol",68207,"MISSING_GET","65 rows for AAPL; drives PIT checks"),
 ("A2","earning-call-transcript","per transcript",None,"MISSING_GET","NOTE: correct name is singular 'earning'"),
 ("A2","earnings-transcript-list","one call",1,"HAVE_COMPLETE","index only, 11,213 rows"),
 ("A3","price-target-news","per-symbol",26172,"MISSING_GET","individual analyst target actions"),
 ("A3","price-target-consensus","per-symbol",26172,"MISSING_GET",""),
 ("A3","price-target-summary","per-symbol",26172,"MISSING_GET","bulk snapshot already held"),
 ("A4","employee-count","per-symbol",68207,"HAVE_PARTIAL","FALSE-NEGATIVE STOP: 29/137 shards, 1 symbol"),
 ("A5","sec-filings-search/symbol","per-symbol",26172,"MISSING_GET","filing metadata + EDGAR links"),
 ("A5","sec-filings-8k","date sweep",400,"MISSING_GET",""),
 ("A5","sec-filings-financials","date sweep",400,"MISSING_GET",""),
 ("B1","etf/holdings","per-ETF",None,"MISSING_GET","505 rows for SPY"),
 ("B1","etf/sector-weightings","per-ETF",None,"MISSING_GET",""),
 ("B1","etf/country-weightings","per-ETF",None,"MISSING_GET",""),
 ("B1","etf-holder-bulk","bulk parts",None,"HAVE_COMPLETE","10.5M rows already held"),
 ("B2","news/stock","date sweep",None,"MISSING_GET","historical stock news, raw text"),
 ("B3","news/press-releases","date sweep",None,"MISSING_GET",""),
 ("B4","governance-executive-compensation","per-symbol",26172,"MISSING_GET","339 rows for AAPL"),
 ("B4","executive-compensation-benchmark","per-year",30,"MISSING_GET","377 rows/yr"),
 ("B5","ipos-prospectus","date sweep",400,"MISSING_GET",""),
 ("B5","ipos-disclosure","date sweep",400,"MISSING_GET",""),
 ("B6","fundraising-latest","paged",200,"MISSING_GET","symbol form returns HTTP 400"),
 ("B7","scores-bulk","bulk",1,"HAVE_COMPLETE","vendor scores, QA only"),
 ("C1","commitment-of-traders-report","date sweep",300,"MISSING_GET",""),
 ("C1","commitment-of-traders-analysis","date sweep",300,"MISSING_GET",""),
 ("C2","historical-price-eod/full (FX)","per-pair",200,"MISSING_GET","EURUSD etc. verified working"),
 ("C3","historical-price-eod/full (commodities)","per-symbol",40,"MISSING_GET","GCUSD verified working"),
 ("C4","historical-price-eod/full (indices)","per-index",100,"MISSING_GET","^GSPC verified working"),
 ("SKIP","technical indicators (SMA/EMA/RSI/ADX)","-",0,"SKIP_RECONSTRUCTABLE","derive locally from OHLCV"),
 ("SKIP","custom-dcf / dcf-bulk","-",0,"SKIP_RECONSTRUCTABLE","vendor-computed, already held as snapshot"),
 ("SKIP","quotes / gainers / losers / peers","-",0,"SKIP_RECONSTRUCTABLE","current-state snapshots, no research value"),
]

lines = ["# FMP archive inventory", "",
         f"Audited read-only. {len(A)} logical datasets, "
         f"{sum(d['rows'] for d in A):,} rows, "
         f"{sum(d['compressed_bytes'] for d in A)/1e9:.2f} GB compressed, "
         f"{sum(d['files'] for d in A):,} files.", "",
         "Nothing was modified. Row counts are exact; schema, null rates and date",
         "ranges are full-scan under 3M rows and sampled above it (sample always",
         "includes the first and last partition, so date ranges are real).", "",
         "## Holdings by group", "",
         "| group | datasets | rows | MB | symbols | date range | exact dupes | key conflicts | placeholders |",
         "|---|---|---|---|---|---|---|---|---|"]
agg = collections.defaultdict(lambda: dict(n=0, rows=0, cb=0, syms=0, mn="", mx="", ex=0, cf=0, pl=0))
for d in A:
    g = d["dataset"].split("/")[0]; r = agg[g]
    r["n"] += 1; r["rows"] += d["rows"]; r["cb"] += d["compressed_bytes"]
    r["syms"] = max(r["syms"], d["symbols"]); r["ex"] += d["exact_dupe_rows"]
    r["pl"] += d["placeholders"]
    if "uniqueness" in d["primary_key"]:
        u = float(d["primary_key"].split("uniqueness ")[1].rstrip(")"))
        if u > 0.90: r["cf"] += d["conflict_dupe_keys"]
    if d["min_date"]: r["mn"] = min(r["mn"], d["min_date"]) if r["mn"] else d["min_date"]
    if d["max_date"]: r["mx"] = max(r["mx"], d["max_date"]) if r["mx"] else d["max_date"]
for g, r in sorted(agg.items()):
    lines.append(f"| {g} | {r['n']} | {r['rows']:,} | {r['cb']/1e6:.0f} | {r['syms']:,} | "
                 f"{r['mn']}..{r['mx']} | {r['ex']:,} | {r['cf']:,} | {r['pl']:,} |")
lines += ["", "Key conflicts are counted ONLY where the inferred key has >0.90 uniqueness.",
          "Below that the key is wrong, not the data: `etf-holder-bulk` legitimately",
          "holds many rows per symbol, so its apparent 10.5M 'conflicts' are an",
          "artefact of having no holding-level identifier among the candidate keys.", "",
          "## Endpoint classification", "",
          "| pri | endpoint | shape | est. requests | status | note |", "|---|---|---|---|---|---|"]
for pri, ep, shape, req, st, note in CATALOG:
    lines.append(f"| {pri} | `{ep}` | {shape} | {req if req is not None else '?'} | **{st}** | {note} |")
counts = collections.Counter(c[4] for c in CATALOG)
lines += ["", "## Status tally", ""] + [f"- **{k}**: {v}" for k, v in counts.most_common()]
(ROOT / "fmp_inventory.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines[:0]))
print(f"wrote {ROOT/'fmp_inventory.md'}")
for k, v in counts.most_common(): print(f"  {k:24s} {v}")
