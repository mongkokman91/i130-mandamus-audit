#!/usr/bin/env python3
"""Evidence-first I-130 mandamus audit using CourtListener RECAP."""
from __future__ import annotations
import argparse, json, os, re, sys, traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import requests
import yaml

SEARCH_BASE = "https://www.courtlistener.com/api/rest/v3"
REST_BASE = "https://www.courtlistener.com/api/rest/v4"
DEFAULT_CONFIG = "cases.yaml"
DEFAULT_OUTPUT = "output"


def load_config(path):
    with Path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def session_for(key):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Token {key}",
        "Accept": "application/json",
        "User-Agent": "i130-mandamus-audit/0.5",
    })
    return s


def api_get(s, url, params=None):
    r = s.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def search(s, params, max_pages=10):
    url = f"{SEARCH_BASE}/search/"
    params = {"type": "r", **params}
    out, pages = [], []
    for _ in range(max_pages):
        payload = api_get(s, url, params)
        pages.append(payload)
        out.extend(payload.get("results") or [])
        url = payload.get("next")
        params = None
        if not url:
            break
    return out, pages


def relevant(r):
    text = " ".join(str(r.get(k) or "") for k in ("suitNature", "cause", "caseName")).lower()
    signals = (
        "immigration", "mandamus", "administrative procedure", "judicial review",
        "mayorkas", "uscis", "edlow", "blinken", "pompeo", "wolf", "jaddou", "noem"
    )
    return any(x in text for x in signals)


def dedupe(records):
    unique = {}
    for r in records:
        key = r.get("docket_id") or (r.get("court_id"), r.get("docketNumber"), r.get("caseName"))
        unique.setdefault(str(key), r)
    return list(unique.values())


def norm(lawyer, params, r, source="named"):
    return {
        "lawyer": lawyer,
        "source": source,
        "query": json.dumps(params, sort_keys=True),
        "case_name": r.get("caseName"),
        "docket_number": r.get("docketNumber"),
        "court": r.get("court_citation_string") or r.get("court"),
        "court_id": r.get("court_id"),
        "date_filed": r.get("dateFiled"),
        "date_terminated": r.get("dateTerminated"),
        "suit_nature": r.get("suitNature"),
        "cause": r.get("cause"),
        "absolute_url": r.get("docket_absolute_url"),
        "docket_id": r.get("docket_id"),
    }


def paged_endpoint(s, endpoint, params, max_pages=25):
    url = f"{REST_BASE}/{endpoint}/"
    items = []
    for _ in range(max_pages):
        payload = api_get(s, url, params)
        items.extend(payload.get("results") or [])
        url = payload.get("next")
        params = None
        if not url:
            break
    return items


def docket_docs(s, docket_id):
    entries = paged_endpoint(s, "docket-entries", {"docket": int(docket_id), "order_by": "date_filed"})
    docs = []
    for e in entries:
        for d in e.get("recap_documents") or []:
            docs.append({
                "entry_id": e.get("id"),
                "entry_date": e.get("date_filed"),
                "entry_description": e.get("description"),
                **d,
            })
    return entries, docs


def document_text(s, doc):
    parts = [
        doc.get("entry_description") or "",
        doc.get("description") or "",
        doc.get("short_description") or "",
        doc.get("snippet") or "",
        doc.get("plain_text") or "",
    ]
    if not doc.get("plain_text") and doc.get("id"):
        try:
            detail = api_get(s, f"{REST_BASE}/recap-documents/{doc['id']}/")
            parts.append(detail.get("plain_text") or "")
        except requests.RequestException:
            pass
    return "\n".join(p for p in parts if p)


def evidence_flags(text, patterns):
    lo = text.lower()
    return {k: any(term.lower() in lo for term in vals) for k, vals in patterns.items()}


def find_receipt_date(text):
    month = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    patterns = [
        rf"(?:filed|submitted|received).{{0,120}}(?:I-130|petition).{{0,120}}{month}\s+(\d{{1,2}}),\s+(20\d{{2}})",
        rf"(?:I-130|petition).{{0,120}}(?:filed|submitted|received).{{0,120}}{month}\s+(\d{{1,2}}),\s+(20\d{{2}})",
        rf"On\s+{month}\s+(\d{{1,2}}),\s+(20\d{{2}}).{{0,100}}(?:filed|submitted).{{0,80}}(?:I-130|Petition for Alien Relative)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            try:
                return pd.to_datetime(" ".join(m.groups())).date().isoformat()
            except Exception:
                pass
    return None


def infer_service_center(text, terms):
    lo = text.lower()
    for term in terms:
        if term.lower() in lo:
            return term
    receipt = re.search(r"\b(YSC|SRC|LIN|EAC|WAC|MSC)[-\s]?\d", text, re.I)
    return receipt.group(1).upper() if receipt else None


def classify(flags, terminated):
    if flags.get("adverse"):
        return "CONFIRMED_ADVERSE"
    if flags.get("approval") or flags.get("adjudication"):
        return "CONFIRMED_FAVORABLE"
    if flags.get("voluntary_dismissal") and terminated:
        return "PROBABLE_FAVORABLE"
    if not terminated:
        return "PENDING"
    return "UNKNOWN"


def tier_case(row):
    spouse = bool(row.get("relationship_evidence") == "spouse")
    i130 = bool(row.get("i130_evidence"))
    delay = row.get("delay_months_extracted")
    pending = bool(row.get("pending_language"))
    if spouse and i130 and pending and delay is not None and 12 <= float(delay) <= 18:
        return 1
    if spouse and i130 and pending:
        return 2
    if i130 and pending:
        return 3
    return 4


def audit_case(s, row, patterns):
    entries, docs = docket_docs(s, int(row["docket_id"]))
    combined, evidence = [], []
    for d in docs:
        txt = document_text(s, d)
        if not txt:
            continue
        combined.append(txt)
        flags = evidence_flags(txt, patterns)
        if any(flags.values()):
            evidence.append({
                "document_id": d.get("id"),
                "document_number": d.get("document_number"),
                "entry_date": d.get("entry_date"),
                "description": d.get("description") or d.get("short_description"),
                "flags": ";".join(k for k, v in flags.items() if v),
            })
    text = "\n".join(combined)
    flags = evidence_flags(text, patterns)
    receipt = find_receipt_date(text)
    service = infer_service_center(text, patterns.get("service_centers", []))
    filed = pd.to_datetime(row.get("date_filed"), errors="coerce", utc=True)
    rec = pd.to_datetime(receipt, errors="coerce", utc=True)
    delay = round((filed - rec).days / 30.4375, 1) if pd.notna(filed) and pd.notna(rec) else None
    result = {
        **row,
        "relationship_evidence": "spouse" if flags.get("spouse") else None,
        "i130_evidence": flags.get("i130", False),
        "pending_language": flags.get("pending", False),
        "receipt_date_extracted": receipt,
        "delay_months_extracted": delay,
        "service_center_evidence": service,
        "document_count": len(docs),
        "evidence_document_count": len(evidence),
        "outcome": classify(flags, row.get("date_terminated")),
        "approval_language": flags.get("approval", False),
        "adjudication_language": flags.get("adjudication", False),
        "voluntary_dismissal_language": flags.get("voluntary_dismissal", False),
        "adverse_language": flags.get("adverse", False),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
    }
    result["tier"] = tier_case(result)
    result["potomac_ysc_bonus"] = bool((service or "").lower() in {"potomac", "ysc"})
    return result


def discover(s, queries, max_pages, error_rows):
    dockets, raw = {}, []
    for params in queries:
        try:
            rs, pages = search(s, params, max_pages)
            raw.append({"params": params, "count": len(rs), "pages": pages})
            for r in dedupe(rs):
                if relevant(r) and r.get("docket_id"):
                    dockets.setdefault(str(r.get("docket_id")), r)
        except Exception as exc:
            error_rows.append({"stage": "discovery", "target": json.dumps(params), "error": repr(exc)})
            raw.append({"params": params, "error": repr(exc), "pages": []})
    lawyer_counts = Counter()
    rows = []
    for r in dockets.values():
        for attorney in r.get("attorney") or []:
            lawyer_counts[attorney] += 1
            rows.append({
                "lawyer": attorney,
                "case_name": r.get("caseName"),
                "docket_number": r.get("docketNumber"),
                "court": r.get("court_citation_string"),
                "docket_id": r.get("docket_id"),
            })
    ranking = [{"lawyer": k, "discovered_relevant_dockets": v} for k, v in lawyer_counts.most_common()]
    return rows, ranking, raw


def seed_rows(cfg):
    rows = []
    for c in cfg.get("seed_cases", []):
        r = dict(c)
        a = pd.to_datetime(r.get("filed_date"), errors="coerce")
        b = pd.to_datetime(r.get("terminated_date"), errors="coerce")
        r["days_filing_to_termination"] = int((b - a).days) if pd.notna(a) and pd.notna(b) else None
        rows.append(r)
    return rows


def lawyer_stats(audited):
    by = defaultdict(list)
    for r in audited:
        if r.get("lawyer"):
            by[r["lawyer"]].append(r)
    stats = []
    for lawyer, rows in by.items():
        confirmed = sum(r.get("outcome") == "CONFIRMED_FAVORABLE" for r in rows)
        adverse = sum(r.get("outcome") == "CONFIRMED_ADVERSE" for r in rows)
        probable = sum(r.get("outcome") == "PROBABLE_FAVORABLE" for r in rows)
        pending = sum(r.get("outcome") == "PENDING" for r in rows)
        tier_counts = {t: sum(r.get("tier") == t for r in rows) for t in (1, 2, 3, 4)}
        ysc = sum(bool(r.get("potomac_ysc_bonus")) for r in rows)
        score = confirmed * 100 + tier_counts[1] * 30 + tier_counts[2] * 15 + tier_counts[3] * 5 + ysc * 10 - adverse * 40
        stats.append({
            "lawyer": lawyer,
            "audited_cases": len(rows),
            "confirmed_favorable": confirmed,
            "probable_favorable_not_counted_as_win": probable,
            "confirmed_adverse": adverse,
            "pending": pending,
            "tier1_cases": tier_counts[1],
            "tier2_cases": tier_counts[2],
            "tier3_cases": tier_counts[3],
            "tier4_cases": tier_counts[4],
            "potomac_ysc_cases": ysc,
            "confirmed_win_rate": round(confirmed / (confirmed + adverse), 3) if confirmed + adverse else None,
            "ranking_score": score,
        })
    return sorted(stats, key=lambda r: (-r["ranking_score"], -r["confirmed_favorable"], -r["tier1_cases"], -r["tier2_cases"]))


def write_outputs(out, audited, discovery_ranking, discovery_cases, seeds, errors, raw):
    out.mkdir(parents=True, exist_ok=True)
    frames = {
        "Case Evidence": pd.DataFrame(audited),
        "Lawyer Stats": pd.DataFrame(lawyer_stats(audited)),
        "Lawyer Discovery": pd.DataFrame(discovery_ranking),
        "Discovery Cases": pd.DataFrame(discovery_cases),
        "Seed Audit": pd.DataFrame(seeds),
        "Errors": pd.DataFrame(errors),
    }
    for name, df in frames.items():
        df.to_csv(out / (name.lower().replace(" ", "_") + ".csv"), index=False)
    with pd.ExcelWriter(out / "case_audit.xlsx", engine="openpyxl") as w:
        for name, df in frames.items():
            df.to_excel(w, sheet_name=name[:31], index=False)
        methodology = pd.DataFrame([
            ["Tier 1", "US-citizen/spousal pending I-130 with extracted 12-18 month delay."],
            ["Tier 2", "Spousal pending I-130 at any delay length."],
            ["Tier 3", "Other pending family I-130."],
            ["Tier 4", "Broader immigration/USCIS delay litigation."],
            ["CONFIRMED_FAVORABLE", "Explicit approval/adjudication language found."],
            ["PROBABLE_FAVORABLE", "Voluntary/stipulated dismissal only; NOT counted as a confirmed win."],
            ["CONFIRMED_ADVERSE", "Explicit adverse disposition language found."],
        ], columns=["item", "rule"])
        methodology.to_excel(w, sheet_name="Methodology", index=False)
    with (out / "raw_api.json").open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--max-pages", type=int, default=10)
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    raw = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "named": [], "discovery": []}
    key = os.environ.get("COURTLISTENER_API_KEY", "").strip()
    if not key:
        (out / "fatal_error.txt").write_text("COURTLISTENER_API_KEY is not set\n")
        return 2
    cfg = load_config(args.config)
    s = session_for(key)
    named = []
    for x in cfg.get("lawyers", []):
        try:
            rs, pages = search(s, x["search_params"], args.max_pages)
            clean = [r for r in dedupe(rs) if relevant(r)]
            raw["named"].append({"lawyer": x["name"], "raw": len(rs), "unique": len(dedupe(rs)), "filtered": len(clean), "pages": pages})
            named.extend(norm(x["name"], x["search_params"], r) for r in clean)
            print(x["name"], len(clean))
        except Exception as exc:
            errors.append({"stage": "named_search", "target": x.get("name"), "error": repr(exc)})
            raw["named"].append({"lawyer": x.get("name"), "error": repr(exc), "pages": []})
    unique = {str(r["docket_id"]): r for r in named if r.get("docket_id")}
    audited = []
    for i, r in enumerate(unique.values(), 1):
        try:
            audited.append(audit_case(s, r, cfg.get("evidence_patterns", {})))
            print(f"audit {i}/{len(unique)} {r['case_name']}")
        except Exception as exc:
            errors.append({"stage": "case_audit", "target": f"{r.get('case_name')} | {r.get('docket_id')}", "error": repr(exc), "traceback": traceback.format_exc(limit=3)})
            fallback = {**r, "outcome": "UNKNOWN", "tier": 4, "audit_error": repr(exc)}
            audited.append(fallback)
    discovery_cases, discovery_ranking, discovery_raw = discover(s, cfg.get("discovery_queries", []), args.max_pages, errors)
    raw["discovery"] = discovery_raw
    write_outputs(out, audited, discovery_ranking, discovery_cases, seed_rows(cfg), errors, raw)
    print("Done", out.resolve())
    print("Errors captured:", len(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
