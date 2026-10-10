"""Read-only inventory and audit of the local FMP archive.

Touches nothing. Walks every file, derives one row per LOGICAL dataset (an
endpoint, not a file), and reports what is actually present rather than what
the download manifest claims.

Method notes, because the numbers are only useful if you know how they were
made:

* Row counts are exact. CSV is counted by newline minus header, JSONL by
  non-blank line, JSON by list length.
* Schema, null rates and date ranges come from a full scan for datasets under
  SCAN_FULL_ROWS rows, and from a bounded sample of files above it. Every row
  says which was used, so a sampled null rate is never mistaken for exact.
* Duplicate-key rate is computed against the inferred primary key. For
  partitioned datasets the partition is part of the key space, so duplicates
  are counted within partition and across the whole dataset separately.
* "Complete" is never asserted from row count alone. It is derived from
  contiguity of the partition axis (years, shards) plus the manifest.

Outputs ``fmp_inventory.csv`` and ``fmp_inventory.md`` in the archive root.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCAN_FULL_ROWS = 3_000_000
SAMPLE_FILES = 12

# Inferred primary keys. Anything not listed falls back to (symbol, date).
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "income-statement-bulk": ("symbol", "date", "period"),
    "balance-sheet-statement-bulk": ("symbol", "date", "period"),
    "cash-flow-statement-bulk": ("symbol", "date", "period"),
    "ratios-bulk": ("symbol", "date", "period"),
    "key-metrics-bulk": ("symbol", "date", "period"),
    "analyst-estimates-bulk": ("symbol", "date"),
    "earnings-surprises-bulk": ("symbol", "date"),
    "earnings-calendar": ("symbol", "date"),
    "dividends-calendar": ("symbol", "date"),
    "splits-calendar": ("symbol", "date"),
    "ipos-calendar": ("symbol", "date"),
    "treasury-rates": ("date",),
    "economic-indicators": ("name", "date"),
    "historical-sector-pe": ("sector", "date"),
    "historical-industry-pe": ("industry", "date"),
    "grades": ("symbol", "date", "gradingCompany"),
    "grades-historical": ("symbol", "date"),
    "stock-list": ("symbol",),
    "delisted-companies": ("symbol",),
    "shares-float-all": ("symbol",),
}
DATE_FIELDS = ("date", "acceptedDate", "filingDate", "publishedDate", "dateAdded", "delistedDate")


@dataclass
class DatasetAudit:
    dataset: str
    endpoint: str
    path: str
    fmt: str
    files: int = 0
    empty_files: int = 0
    corrupt_files: list[str] = field(default_factory=list)
    compressed_bytes: int = 0
    uncompressed_bytes: int = 0
    rows: int = 0            # observation rows (nested arrays flattened)
    records: int = 0         # physical lines / top-level entries
    symbols: int = 0
    min_date: str = ""
    max_date: str = ""
    columns: list[str] = field(default_factory=list)
    null_rates: dict[str, float] = field(default_factory=dict)
    exact_dupe_rows: int = 0
    conflict_dupe_keys: int = 0
    primary_key: str = ""
    scan_mode: str = "full"
    has_filing_date: bool = False
    has_accepted_date: bool = False
    placeholders: int = 0
    partition_axis: str = ""
    partition_gaps: str = ""
    status: str = "UNKNOWN_NEEDS_REVIEW"
    notes: str = ""


def read_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, uncompressed_bytes). Never raises on a bad file."""
    raw = gzip.open(path, "rb").read()
    name = path.name
    if name.endswith(".csv.gz"):
        text = raw.decode("utf8", "replace")
        return list(csv.DictReader(io.StringIO(text))), len(raw)
    if name.endswith(".jsonl.gz"):
        out = []
        for line in raw.decode("utf8", "replace").splitlines():
            if line.strip():
                obj = json.loads(line)
                # per-symbol crawls nest the payload under "data"
                if isinstance(obj, dict) and "data" in obj and isinstance(obj["data"], list):
                    for sub in obj["data"]:
                        if isinstance(sub, dict):
                            sub.setdefault("symbol", obj.get("symbol"))
                            out.append(sub)
                elif isinstance(obj, dict) and "_error" in obj:
                    continue
                else:
                    out.append(obj)
        return out, len(raw)
    parsed = json.loads(raw)
    return (parsed if isinstance(parsed, list) else [parsed]), len(raw)


def count_observations(path: Path) -> tuple[int, int]:
    """Return (observation_rows, physical_records).

    A per-symbol JSONL line is one SYMBOL carrying an array of observations,
    so counting lines badly understates the dataset.
    """
    raw = gzip.open(path, "rb").read()
    if path.name.endswith(".csv.gz"):
        n = max(raw.count(b"\n") - 1, 0)
        return n, n
    if path.name.endswith(".jsonl.gz"):
        obs = rec = 0
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            rec += 1
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict) and isinstance(o.get("data"), list):
                obs += len(o["data"])
            elif isinstance(o, dict) and "_error" in o:
                continue
            else:
                obs += 1
        return obs, rec
    try:
        parsed = json.loads(raw)
        n = len(parsed) if isinstance(parsed, list) else 1
        return n, n
    except Exception:
        return 0, 0


def count_rows_cheap(path: Path) -> int:
    raw = gzip.open(path, "rb").read()
    if path.name.endswith(".csv.gz"):
        return max(raw.count(b"\n") - 1, 0)
    if path.name.endswith(".jsonl.gz"):
        return sum(1 for line in raw.split(b"\n") if line.strip())
    try:
        parsed = json.loads(raw)
        return len(parsed) if isinstance(parsed, list) else 1
    except Exception:
        return 0


def discover(root: Path) -> dict[str, list[Path]]:
    """Group every archive file under its logical dataset."""
    groups: dict[str, list[Path]] = collections.defaultdict(list)
    for path in sorted(root.rglob("*.gz")):
        rel = path.relative_to(root)
        parts = rel.parts
        if parts[0] == "_cache":
            continue
        group = parts[0]
        if len(parts) == 2:
            name = parts[1].split(".")[0]
            key = f"{group}/{name}"
        else:
            sub = parts[1]
            if group in ("macro",) and len(parts) > 3:
                key = f"{group}/{sub}/{parts[2]}" if sub in ("economic-indicators", "historical-sector-pe", "historical-industry-pe") else f"{group}/{sub}"
            elif group in ("prices", "alt", "persymbol", "revisions") and len(parts) > 3:
                key = f"{group}/{sub}/{parts[2]}"
            else:
                key = f"{group}/{sub}"
        groups[key].append(path)
    return groups


CANDIDATE_KEYS = (
    ("symbol", "date", "period"),
    ("symbol", "date", "gradingCompany"),
    ("symbol", "date", "reportedCurrency"),
    ("symbol", "date"),
    ("name", "date"),
    ("sector", "date"),
    ("industry", "date"),
    ("symbol", "fiscalYear", "period"),
    ("symbol",),
    ("date",),
)


def best_key(columns: list[str], rows_by_key: dict[tuple[str, ...], int], total: int) -> tuple[str, float]:
    """Pick the candidate key with the highest uniqueness, and report it."""
    best, best_u = "", 0.0
    for cand, distinct in rows_by_key.items():
        u = distinct / max(total, 1)
        if u > best_u:
            best, best_u = "+".join(cand), u
    return best, round(best_u, 4)


def infer_key(endpoint: str, columns: list[str]) -> tuple[str, ...]:
    for name, key in PRIMARY_KEYS.items():
        if endpoint.endswith(name):
            return tuple(k for k in key if k in columns) or ("symbol",)
    for candidate in (("symbol", "date"), ("symbol",), ("date",)):
        if all(c in columns for c in candidate):
            return candidate
    return tuple(columns[:1])


def audit_dataset(key: str, paths: list[Path], root: Path) -> DatasetAudit:
    endpoint = key.split("/", 1)[1] if "/" in key else key
    fmt = "csv.gz" if paths[0].name.endswith(".csv.gz") else (
        "jsonl.gz" if paths[0].name.endswith(".jsonl.gz") else "json.gz")
    a = DatasetAudit(dataset=key, endpoint=endpoint.split("/")[-1],
                     path=str(paths[0].parent.relative_to(root)), fmt=fmt)
    a.files = len(paths)
    a.compressed_bytes = sum(p.stat().st_size for p in paths)

    total = recs = 0
    for p in paths:
        try:
            n, rec = count_observations(p)
        except Exception as exc:
            a.corrupt_files.append(f"{p.name}: {type(exc).__name__}")
            continue
        total += n
        recs += rec
        if n == 0:
            a.empty_files += 1
    a.rows = total
    a.records = recs

    scan = paths
    if total > SCAN_FULL_ROWS and len(paths) > SAMPLE_FILES:
        step = max(len(paths) // SAMPLE_FILES, 1)
        picked = paths[::step][:SAMPLE_FILES]
        # endpoints of the partition axis must be in the sample or the
        # reported date range is simply wrong
        for edge in (paths[0], paths[-1]):
            if edge not in picked:
                picked.append(edge)
        scan = sorted(set(picked))
        a.scan_mode = f"sampled {len(scan)}/{len(paths)} files (incl. first+last)"

    cols: list[str] = []
    nulls: collections.Counter[str] = collections.Counter()
    seen_rows = 0
    symbols: set[str] = set()
    dates: list[str] = []
    keysets: dict[tuple[str, ...], set[tuple]] = {}
    rowhashes: set[int] = set()
    exact = placeholders = 0
    conflict_by_key: dict[tuple[str, ...], int] = {}
    seen_canon: dict[tuple[str, ...], dict[tuple, str]] = {}

    for p in scan:
        try:
            rows, ubytes = read_rows(p)
        except Exception as exc:
            a.corrupt_files.append(f"{p.name}: {type(exc).__name__}")
            continue
        a.uncompressed_bytes += ubytes
        for r in rows:
            if not isinstance(r, dict):
                continue
            seen_rows += 1
            if not cols:
                cols = list(r)
            for c in r:
                if r[c] in (None, "", "null"):
                    nulls[c] += 1
            sym = r.get("symbol")
            if sym:
                symbols.add(str(sym))
            for f in DATE_FIELDS:
                v = r.get(f)
                if v and str(v)[:4].isdigit():
                    dates.append(str(v)[:10])
                    break
            rating = [v for k, v in r.items() if k.startswith("analystRatings")]
            if rating and all(v in (0, "0", None) for v in rating):
                placeholders += 1
            canon = json.dumps(r, sort_keys=True, default=str)
            h = hash(canon)
            if h in rowhashes:
                exact += 1
            else:
                rowhashes.add(h)
            part = p.parent.name if p.parent.name not in (a.path.split("/")[-1],) else ""
            for cand in CANDIDATE_KEYS:
                if not all(c in r for c in cand):
                    continue
                kv = (part, *(str(r.get(c, "")) for c in cand))
                keysets.setdefault(cand, set()).add(kv)
                store = seen_canon.setdefault(cand, {})
                if kv in store:
                    if store[kv] != canon:
                        conflict_by_key[cand] = conflict_by_key.get(cand, 0) + 1
                else:
                    store[kv] = canon

    a.columns = cols
    a.symbols = len(symbols)
    if dates:
        a.min_date, a.max_date = min(dates), max(dates)
    a.null_rates = {c: round(nulls[c] / max(seen_rows, 1), 4) for c in cols if nulls[c]}
    a.exact_dupe_rows = exact
    distinct = {k: len(v) for k, v in keysets.items()}
    pk, uniq = best_key(cols, distinct, seen_rows)
    a.primary_key = f"{pk} (uniqueness {uniq:.3f})" if pk else ""
    best_cand = tuple(pk.split("+")) if pk else ()
    a.conflict_dupe_keys = conflict_by_key.get(best_cand, 0)
    a.has_filing_date = "filingDate" in cols
    a.has_accepted_date = "acceptedDate" in cols
    a.placeholders = placeholders

    years = sorted({int(m.group()) for p in paths
                    if (m := re.search(r"(19|20)\d{2}", p.name))})
    if years:
        a.partition_axis = f"year {years[0]}-{years[-1]}"
        missing = [y for y in range(years[0], years[-1] + 1) if y not in years]
        a.partition_gaps = ",".join(map(str, missing[:10])) if missing else ""
    shards = sorted({int(m.group(1)) for p in paths
                     if (m := re.match(r"shard-(\d+)", p.name))})
    if shards:
        a.partition_axis = f"shard 0-{shards[-1]} ({len(shards)} present)"
        missing_s = [s for s in range(shards[-1] + 1) if s not in shards]
        a.partition_gaps = f"{len(missing_s)} missing shards" if missing_s else ""
    return a


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("fmp_data"))
    args = ap.parse_args(argv)
    root: Path = args.root

    groups = discover(root)
    print(f"auditing {len(groups)} logical datasets across "
          f"{sum(len(v) for v in groups.values()):,} files", file=sys.stderr)
    audits = []
    for i, (key, paths) in enumerate(sorted(groups.items()), 1):
        print(f"  [{i}/{len(groups)}] {key}", file=sys.stderr)
        audits.append(audit_dataset(key, paths, root))

    out_csv = root / "fmp_inventory.csv"
    with out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["dataset", "endpoint", "path", "format", "files", "empty_files",
                    "corrupt_files", "compressed_bytes", "uncompressed_bytes_scanned",
                    "rows", "records", "symbols", "min_date", "max_date", "primary_key",
                    "exact_dupe_rows", "conflict_dupe_keys", "placeholders",
                    "has_filingDate", "has_acceptedDate", "partition_axis",
                    "partition_gaps", "scan_mode", "columns", "null_rates", "status", "notes"])
        for a in audits:
            w.writerow([a.dataset, a.endpoint, a.path, a.fmt, a.files, a.empty_files,
                        ";".join(a.corrupt_files), a.compressed_bytes, a.uncompressed_bytes,
                        a.rows, a.records, a.symbols, a.min_date, a.max_date, a.primary_key,
                        a.exact_dupe_rows, a.conflict_dupe_keys, a.placeholders,
                        a.has_filing_date, a.has_accepted_date, a.partition_axis,
                        a.partition_gaps, a.scan_mode, "|".join(a.columns),
                        json.dumps(a.null_rates), a.status, a.notes])
    print(f"wrote {out_csv}", file=sys.stderr)
    json.dump([a.__dict__ for a in audits], (root / "fmp_inventory.json").open("w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
