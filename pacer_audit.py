#!/usr/bin/env python3
"""I-130 mandamus audit helper.

Uses the CourtListener REST API to collect RECAP search results for configured
lawyers, preserves raw API responses, and produces normalized CSV/XLSX output.

Security: provide COURTLISTENER_API_KEY through the environment. Never hard-code
credentials in this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

API_BASE = "https://www.courtlistener.com/api/rest/v3"
DEFAULT_CONFIG = "cases.yaml"
DEFAULT_OUTPUT = "output"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def make_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Token {api_key}",
            "Accept": "application/json",
            "User-Agent": "i130-mandamus-audit/0.1",
        }
    )
    return session


def api_get(session: requests.Session, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def iter_search(
    session: requests.Session,
    query: str,
    *,
    max_pages: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return flattened RECAP search results plus raw pages.

    CourtListener search uses the REST search endpoint. `type=r` requests RECAP
    search results. Pagination follows the API-provided `next` URL rather than
    constructing page numbers ourselves.
    """
    url = f"{API_BASE}/search/"
    params: dict[str, Any] | None = {"q": query, "type": "r"}
    results: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []

    for _ in range(max_pages):
        payload = api_get(session, url, params=params)
        raw_pages.append(payload)
        batch = payload.get("results") or []
        if isinstance(batch, list):
            results.extend(batch)
        next_url = payload.get("next")
        if not next_url:
            break
        url = next_url
        params = None

    return results, raw_pages


def pick(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def normalize_result(lawyer: str, query: str, record: dict[str, Any]) -> dict[str, Any]:
    """Normalize fields defensively because RECAP search schemas can vary."""
    return {
        "lawyer": lawyer,
        "query": query,
        "case_name": pick(record, "caseName", "case_name", "caption", "name"),
        "docket_number": pick(record, "docketNumber", "docket_number"),
        "court": pick(record, "court_citation_string", "court", "court_id"),
        "date_filed": pick(record, "dateFiled", "date_filed"),
        "date_terminated": pick(record, "dateTerminated", "date_terminated"),
        "absolute_url": pick(record, "absolute_url", "url"),
        "docket_id": pick(record, "docket_id", "docketId"),
        "document_id": pick(record, "document_id", "documentId", "id"),
        "description": pick(record, "description", "short_description", "snippet"),
        "search_result_raw": json.dumps(record, ensure_ascii=False, sort_keys=True),
    }


def seed_case_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in config.get("seed_cases", []):
        row = dict(case)
        filed = pd.to_datetime(row.get("filed_date"), errors="coerce")
        terminated = pd.to_datetime(row.get("terminated_date"), errors="coerce")
        if pd.notna(filed) and pd.notna(terminated):
            row["days_filing_to_termination"] = int((terminated - filed).days)
        else:
            row["days_filing_to_termination"] = None
        rows.append(row)
    return rows


def write_outputs(
    output_dir: Path,
    search_rows: list[dict[str, Any]],
    seed_rows: list[dict[str, Any]],
    raw_bundle: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    search_df = pd.DataFrame(search_rows)
    seed_df = pd.DataFrame(seed_rows)

    search_csv = output_dir / "search_results.csv"
    seed_csv = output_dir / "seed_case_audit.csv"
    workbook = output_dir / "case_audit.xlsx"
    raw_json = output_dir / "raw_search_pages.json"

    search_df.to_csv(search_csv, index=False)
    seed_df.to_csv(seed_csv, index=False)

    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        search_df.to_excel(writer, sheet_name="RECAP Search", index=False)
        seed_df.to_excel(writer, sheet_name="Seed Audit", index=False)

        # Small methodology sheet so classifications remain interpretable.
        methodology = pd.DataFrame(
            [
                ["CONFIRMED_FAVORABLE", "Underlying filing/order expressly establishes relevant relief or adjudication."],
                ["PROBABLE_FAVORABLE", "Closure is consistent with favorable agency action but causation/result is not expressly established."],
                ["CONFIRMED_ADVERSE", "Government/court result is expressly adverse on the relevant claim."],
                ["PENDING", "Case remains active or no termination is established."],
                ["UNKNOWN", "Available evidence is insufficient for a reliable classification."],
            ],
            columns=["classification", "definition"],
        )
        methodology.to_excel(writer, sheet_name="Methodology", index=False)

    with raw_json.open("w", encoding="utf-8") as fh:
        json.dump(raw_bundle, fh, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit I-130 mandamus cases using CourtListener RECAP search.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--max-pages", type=int, default=10)
    args = parser.parse_args()

    api_key = os.environ.get("COURTLISTENER_API_KEY", "").strip()
    if not api_key:
        print("ERROR: COURTLISTENER_API_KEY is not set.", file=sys.stderr)
        print("Store it in Colab Secrets or another environment variable; do not commit it to GitHub.", file=sys.stderr)
        return 2

    config = load_config(Path(args.config))
    session = make_session(api_key)

    search_rows: list[dict[str, Any]] = []
    raw_bundle: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "queries": [],
    }

    for lawyer in config.get("lawyers", []):
        name = lawyer["name"]
        query = lawyer["search_query"]
        print(f"Searching: {name} | {query}")
        try:
            results, raw_pages = iter_search(session, query, max_pages=args.max_pages)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = exc.response.text[:1000] if exc.response is not None else str(exc)
            print(f"WARNING: search failed for {name} (HTTP {status}): {body}", file=sys.stderr)
            raw_bundle["queries"].append(
                {"lawyer": name, "query": query, "error": str(exc), "pages": []}
            )
            continue

        raw_bundle["queries"].append(
            {"lawyer": name, "query": query, "result_count": len(results), "pages": raw_pages}
        )
        search_rows.extend(normalize_result(name, query, record) for record in results)
        print(f"  found {len(results)} result(s)")

    seed_rows = seed_case_rows(config)
    write_outputs(Path(args.output), search_rows, seed_rows, raw_bundle)

    print(f"\nDone. Outputs written to: {Path(args.output).resolve()}")
    print("Important: RECAP coverage is incomplete. Zero search hits do not prove zero PACER cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
