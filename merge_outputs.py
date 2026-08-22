#!/usr/bin/env python3
"""Merge independent Maryland audit shards without hiding coverage failures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from pacer_audit import lawyer_stats


CSV_FILES = (
    "case_evidence.csv", "evidence_documents.csv", "discovery_cases.csv",
    "lawyer_discovery.csv", "errors.csv", "seed_audit.csv",
)


def read_csvs(root: Path, filename: str) -> pd.DataFrame:
    frames = []
    for path in sorted(root.rglob(filename)):
        try:
            frames.append(pd.read_csv(path))
        except pd.errors.EmptyDataError:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def records(df: pd.DataFrame):
    if df.empty:
        return []
    return df.where(pd.notna(df), None).to_dict("records")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-shards", nargs="+", required=True)
    args = parser.parse_args()
    root, out = Path(args.input_root), Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    found = {p.parent.name.removeprefix("md-shard-") for p in root.rglob("case_evidence.csv")}
    missing = sorted(set(args.expected_shards) - found)

    frames = {name: read_csvs(root, name) for name in CSV_FILES}
    cases = frames["case_evidence.csv"]
    if not cases.empty and "docket_id" in cases:
        cases = cases.drop_duplicates(subset=["docket_id"], keep="last")
    docs = frames["evidence_documents.csv"]
    if not docs.empty:
        keys = [k for k in ("docket_id", "document_id", "document_number") if k in docs]
        if keys:
            docs = docs.drop_duplicates(subset=keys, keep="last")
    discovered = frames["discovery_cases.csv"]
    if not discovered.empty and "docket_id" in discovered:
        discovered = discovered.drop_duplicates(subset=["docket_id"], keep="last")

    raw_files = sorted(root.rglob("raw_api.json"))
    coverage, raw_payloads = [], []
    for path in raw_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_payloads.append({"source": str(path), "payload": payload})
        for query in payload.get("discovery", []):
            coverage.append({
                "source": str(path),
                "cohort": (query.get("params") or {}).get("_cohort"),
                "query": (query.get("params") or {}).get("q"),
                "filed_after": (query.get("params") or {}).get("filed_after"),
                "filed_before": (query.get("params") or {}).get("filed_before"),
                "result_count": query.get("count"),
                "page_count": query.get("page_count"),
                "truncated_at_page_limit": query.get("truncated_at_page_limit"),
                "error": query.get("error"),
            })
    coverage_df = pd.DataFrame(coverage)
    errors = frames["errors.csv"]
    fatal = bool(missing)
    if not coverage_df.empty:
        fatal = fatal or bool(coverage_df["truncated_at_page_limit"].fillna(False).any())
        fatal = fatal or bool(coverage_df["error"].notna().any())
    fatal = fatal or (not errors.empty)

    stats = pd.DataFrame(lawyer_stats(records(cases)))
    outputs = {
        "case_evidence.csv": cases,
        "evidence_documents.csv": docs,
        "discovery_cases.csv": discovered,
        "lawyer_stats.csv": stats,
        "lawyer_discovery.csv": frames["lawyer_discovery.csv"],
        "errors.csv": errors,
        "query_coverage.csv": coverage_df,
    }
    for name, frame in outputs.items():
        frame.to_csv(out / name, index=False)
    with pd.ExcelWriter(out / "maryland_decade_census.xlsx", engine="openpyxl") as writer:
        for name, frame in outputs.items():
            frame.to_excel(writer, sheet_name=name.removesuffix(".csv")[:31], index=False)
    (out / "raw_api_all_shards.json").write_text(
        json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "expected_shards": args.expected_shards,
        "found_shards": sorted(found),
        "missing_shards": missing,
        "case_count": len(cases),
        "evidence_document_count": len(docs),
        "query_count": len(coverage_df),
        "errors": len(errors),
        "coverage_complete": not fatal,
        "limitations": [
            "CourtListener/RECAP contains only publicly available indexed docket material.",
            "PACER-only or sealed documents cannot be classified without separate access.",
        ],
    }
    (out / "coverage_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if fatal:
        print(json.dumps(manifest, indent=2))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
