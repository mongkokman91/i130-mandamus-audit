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

SEARCH_BASE = "https://www.courtlistener.com/api/rest/v4"
REST_BASE = "https://www.courtlistener.com/api/rest/v4"
DEFAULT_CONFIG = "cases.yaml"
DEFAULT_OUTPUT = "output"
_DOCKET_DOC_CACHE = {}
_DOCUMENT_TEXT_CACHE = {}


def load_config(path):
    with Path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def session_for(key):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Token {key}",
        "Accept": "application/json",
        "User-Agent": "i130-mandamus-audit/0.6.1",
    })
    return s


def api_get(s, url, params=None):
    r = s.get(url, params=params, timeout=60)
    if not r.ok:
        body = r.text[:1500]
        raise requests.HTTPError(f"HTTP {r.status_code} for {r.url}: {body}", response=r)
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
    cache_key = int(docket_id)
    if cache_key in _DOCKET_DOC_CACHE:
        return _DOCKET_DOC_CACHE[cache_key]
    entries = paged_endpoint(s, "docket-entries", {"docket": cache_key, "order_by": "date_filed"})
    docs = []
    for e in entries:
        for d in e.get("recap_documents") or []:
            docs.append({
                "entry_id": e.get("id"),
                "entry_date": e.get("date_filed"),
                "entry_description": e.get("description"),
                **d,
            })
    _DOCKET_DOC_CACHE[cache_key] = (entries, docs)
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
        doc_id = int(doc["id"])
        if doc_id not in _DOCUMENT_TEXT_CACHE:
            try:
                detail = api_get(s, f"{REST_BASE}/recap-documents/{doc_id}/")
                _DOCUMENT_TEXT_CACHE[doc_id] = detail.get("plain_text") or ""
            except requests.RequestException:
                _DOCUMENT_TEXT_CACHE[doc_id] = ""
        parts.append(_DOCUMENT_TEXT_CACHE[doc_id])
    return "\n".join(p for p in parts if p)


def evidence_flags(text, patterns):
    lo = text.lower()
    return {k: any(term.lower() in lo for term in vals) for k, vals in patterns.items()}


def contexts(text, needle_pattern, radius=260):
    out = []
    for m in re.finditer(needle_pattern, text, re.I):
        out.append(text[max(0, m.start()-radius): min(len(text), m.end()+radius)])
    return out


def parse_date_tokens(match):
    token = match.group(0)
    dt = pd.to_datetime(token, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.date().isoformat()


def find_receipt_date(text, lawsuit_filed=None):
    date_patterns = [
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}",
        r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/20\d{2}\b",
        r"\b20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b",
    ]
    filing_words = r"filed|submitted|received|receipt date|priority date"
    candidates = []
    for ctx in contexts(text, r"\bI[-\s]?130\b|Petition for Alien Relative", radius=320):
        if not re.search(filing_words, ctx, re.I):
            continue
        for dp in date_patterns:
            for m in re.finditer(dp, ctx, re.I):
                date = parse_date_tokens(m)
                if not date:
                    continue
                before = ctx[max(0, m.start()-180):m.end()+180]
                score = 0
                if re.search(r"\bI[-\s]?130\b|Petition for Alien Relative", before, re.I): score += 4
                if re.search(filing_words, before, re.I): score += 4
                if re.search(r"receipt|priority", before, re.I): score += 1
                candidates.append((score, date, re.sub(r"\s+", " ", before).strip()))
    if lawsuit_filed:
        filed = pd.to_datetime(lawsuit_filed, errors="coerce")
        if pd.notna(filed):
            candidates = [c for c in candidates if pd.to_datetime(c[1]) <= filed]
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: (-x[0], pd.to_datetime(x[1])))
    score, date, ctx = candidates[0]
    return date, score, ctx


def infer_service_center(text):
    center_names = {
        "potomac": "Potomac", "ysc": "YSC", "texas service center": "Texas",
        "src": "SRC", "nebraska service center": "Nebraska", "lin": "LIN",
        "vermont service center": "Vermont", "eac": "EAC",
        "california service center": "California", "wac": "WAC",
        "national benefits center": "NBC", "msc": "MSC",
    }
    for ctx in contexts(text, r"\bI[-\s]?130\b|Petition for Alien Relative|receipt(?: number)?", radius=220):
        lo = ctx.lower()
        for needle, label in center_names.items():
            if re.search(rf"\b{re.escape(needle)}\b", lo):
                return label, re.sub(r"\s+", " ", ctx).strip()
        m = re.search(r"\b(YSC|SRC|LIN|EAC|WAC|MSC)\d{8,13}\b", ctx, re.I)
        if m:
            return m.group(1).upper(), re.sub(r"\s+", " ", ctx).strip()
    return None, None


def is_initiating_document(doc):
    """Return True for complaints/petitions that cannot prove a later outcome."""
    description = " ".join(str(doc.get(k) or "") for k in (
        "entry_description", "description", "short_description"
    ))
    return bool(re.search(
        r"\\b(?:amended\\s+)?(?:civil\\s+)?complaint\\b|"
        r"\\bpetition\\s+for\\s+(?:a\\s+)?writ\\b|"
        r"\\binitiating\\s+petition\\b",
        description,
        re.I,
    ))


def outcome_context_is_nonhistorical(context):
    """Reject requested, conditional, and failure-to-act language."""
    return bool(re.search(
        r"\\b(?:if|when|once)\\s+(?:the\\s+)?(?:I[-\\s]?130|petition)?"
        r".{0,60}(?:is|were|was|has been)?\\s*(?:approved|adjudicated)\\b|"
        r"\\b(?:should|must|may|could|would)\\s+(?:be\\s+)?(?:approved|adjudicated)\\b|"
        r"\\b(?:failure|failed|refusal|refused)\\s+to\\s+(?:approve|adjudicate)\\b|"
        r"\\bright\\s+to\\s+have.{0,80}(?:approved|adjudicated)\\b|"
        r"\\b(?:request(?:s|ed)?|seek(?:s|ing)?|pray(?:s|er)?|ask(?:s|ed)?|compel(?:ling)?)"
        r".{0,100}(?:approve|adjudicate)\\b",
        context,
        re.I | re.S,
    ))


def specific_i130_outcome(text):
    favorable_patterns = [
        r"(?:I[-\\s]?130|Petition for Alien Relative).{0,180}(?:was|has been|is|were)?\\s*(?:approved|adjudicated)",
        r"(?:approved|adjudicated).{0,180}(?:I[-\\s]?130|Petition for Alien Relative)",
        r"USCIS.{0,120}(?:approved|adjudicated).{0,160}(?:plaintiff(?:'s|s)?\\s+)?(?:I[-\\s]?130|Petition for Alien Relative)",
    ]
    adverse_patterns = [
        r"(?:I[-\\s]?130|Petition for Alien Relative).{0,180}(?:was|has been|is)?\\s*(?:denied|rejected)",
        r"(?:denied|rejected).{0,180}(?:I[-\\s]?130|Petition for Alien Relative)",
    ]
    for label, patterns in (
        ("CONFIRMED_FAVORABLE", favorable_patterns),
        ("CONFIRMED_ADVERSE", adverse_patterns),
    ):
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I | re.S):
                context = re.sub(
                    r"\\s+", " ",
                    text[max(0, match.start()-180):match.end()+180],
                ).strip()
                if not outcome_context_is_nonhistorical(context):
                    return label, context
    return None, None


def classify(flags, terminated, explicit=None, context=None):
    if explicit:
        return explicit, context
    if flags.get("adverse"):
        return "UNKNOWN", None
    if flags.get("voluntary_dismissal") and terminated:
        return "PROBABLE_FAVORABLE", None
    if not terminated:
        return "PENDING", None
    return "UNKNOWN", None


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
    _, docs = docket_docs(s, int(row["docket_id"]))
    combined, evidence, explicit_outcomes = [], [], []
    for d in docs:
        txt = document_text(s, d)
        if not txt:
            continue
        combined.append(txt)
        flags = evidence_flags(txt, patterns)
        outcome_eligible = not is_initiating_document(d)
        explicit_outcome, explicit_ctx = specific_i130_outcome(txt) if outcome_eligible else (None, None)
        if explicit_outcome:
            explicit_outcomes.append((d.get("entry_date") or "", explicit_outcome, explicit_ctx))
        if any(flags.values()) or explicit_outcome:
            evidence.append({
                "document_id": d.get("id"),
                "document_number": d.get("document_number"),
                "entry_date": d.get("entry_date"),
                "description": d.get("description") or d.get("short_description"),
                "flags": ";".join(k for k, v in flags.items() if v),
                "explicit_i130_outcome": explicit_outcome,
                "explicit_i130_context": explicit_ctx,
                "outcome_source_eligible": outcome_eligible,
            })
    text = "\n".join(combined)
    flags = evidence_flags(text, patterns)
    receipt, receipt_score, receipt_context = find_receipt_date(text, row.get("date_filed"))
    service, service_context = infer_service_center(text)
    filed = pd.to_datetime(row.get("date_filed"), errors="coerce", utc=True)
    rec = pd.to_datetime(receipt, errors="coerce", utc=True)
    delay = round((filed - rec).days / 30.4375, 1) if pd.notna(filed) and pd.notna(rec) else None
    explicit_outcomes.sort(key=lambda item: item[0])
    latest_explicit = explicit_outcomes[-1] if explicit_outcomes else (None, None, None)
    outcome, outcome_context = classify(
        flags,
        row.get("date_terminated"),
        latest_explicit[1],
        latest_explicit[2],
    )
    result = {
        **row,
        "relationship_evidence": "spouse" if flags.get("spouse") else None,
        "i130_evidence": flags.get("i130", False),
        "pending_language": flags.get("pending", False),
        "receipt_date_extracted": receipt,
        "receipt_date_confidence_score": receipt_score,
        "receipt_date_context": receipt_context,
        "delay_months_extracted": delay,
        "service_center_evidence": service,
        "service_center_context": service_context,
        "document_count": len(docs),
        "evidence_document_count": len(evidence),
        "outcome": outcome,
        "outcome_context": outcome_context,
        "approval_language": outcome == "CONFIRMED_FAVORABLE",
        "adjudication_language": outcome == "CONFIRMED_FAVORABLE",
        "voluntary_dismissal_language": flags.get("voluntary_dismissal", False),
        "adverse_language": flags.get("adverse", False),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
    }
    result["tier"] = tier_case(result)
    result["potomac_ysc_bonus"] = bool((service or "").lower() in {"potomac", "ysc"})
    return result


def looks_like_government_counsel(name):
    lo = (name or "").lower()
    blocked = (
        "ausa", "assistant united states attorney", "united states attorney", "u.s. attorney",
        "department of justice", "civil division", "office of immigration litigation",
        "trial attorney", "government counsel", "us attorney", "doj",
        "deputy clerk", "court staff", "law clerk", "case manager",
        "courtroom deputy", "clerk of court"
    )
    if any(x in lo for x in blocked):
        return True
    normalized = re.sub(r"[^a-z ]", " ", lo)
    tokens = [token for token in normalized.split() if token]
    return bool(tokens) and all(len(token) <= 3 for token in tokens)


def sortable_document_number(value):
    """Normalize CourtListener document numbers so mixed strings/ints sort safely."""
    if value in (None, ""):
        return (1, 10**9, "")
    text = str(value).strip()
    m = re.match(r"^(\d+)", text)
    if m:
        return (0, int(m.group(1)), text)
    return (0, 10**9 - 1, text)


def plaintiff_counsel_from_docket(s, docket_id):
    try:
        _, docs = docket_docs(s, int(docket_id))
    except Exception:
        return None, None
    starters = []
    for d in docs:
        desc = " ".join(str(d.get(k) or "") for k in ("entry_description", "description", "short_description"))
        if re.search(r"\b(complaint|petition for writ|petition|civil complaint)\b", desc, re.I):
            starters.append((d, desc))
    starters.sort(key=lambda x: (sortable_document_number(x[0].get("document_number")), str(x[0].get("entry_date") or "9999")))
    for d, desc in starters[:3]:
        parens = re.findall(r"\(([^()]{3,100})\)", desc)
        for candidate in reversed(parens):
            candidate = candidate.strip()
            if "," in candidate and not looks_like_government_counsel(candidate):
                if not re.search(r"attachments?|entered|transferred|filing fee|receipt", candidate, re.I):
                    return candidate, desc
    return None, starters[0][1] if starters else None


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
        try:
            counsel, source_desc = plaintiff_counsel_from_docket(s, r.get("docket_id"))
        except Exception as exc:
            error_rows.append({"stage": "discovery_docket", "target": str(r.get("docket_id")), "error": repr(exc)})
            continue
        if counsel and not looks_like_government_counsel(counsel):
            lawyer_counts[counsel] += 1
            rows.append({
                "lawyer": counsel,
                "case_name": r.get("caseName"),
                "docket_number": r.get("docketNumber"),
                "court": r.get("court_citation_string"),
                "docket_id": r.get("docket_id"),
                "counsel_side": "plaintiff_filing_counsel",
                "counsel_source_description": source_desc,
            })
    ranking = [{"lawyer": k, "verified_plaintiff_filing_dockets": v} for k, v in lawyer_counts.most_common()]
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
        unknown = sum(r.get("outcome") == "UNKNOWN" for r in rows)
        tier_counts = {t: sum(r.get("tier") == t for r in rows) for t in (1, 2, 3, 4)}
        ysc = sum(bool(r.get("potomac_ysc_bonus")) for r in rows)
        reliable_delay = sum(r.get("receipt_date_confidence_score") is not None and r.get("receipt_date_confidence_score") >= 8 for r in rows)
        score = confirmed * 100 + tier_counts[1] * 30 + tier_counts[2] * 15 + tier_counts[3] * 5 + ysc * 10 - adverse * 40
        stats.append({
            "lawyer": lawyer,
            "audited_cases": len(rows),
            "confirmed_favorable": confirmed,
            "probable_favorable_not_counted_as_win": probable,
            "confirmed_adverse": adverse,
            "pending": pending,
            "unknown": unknown,
            "tier1_cases": tier_counts[1],
            "tier2_cases": tier_counts[2],
            "tier3_cases": tier_counts[3],
            "tier4_cases": tier_counts[4],
            "potomac_ysc_cases": ysc,
            "high_confidence_receipt_dates": reliable_delay,
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
            ["CONFIRMED_FAVORABLE", "Requires explicit I-130/Petition for Alien Relative approval or adjudication language in local context."],
            ["PROBABLE_FAVORABLE", "Voluntary/stipulated dismissal only; NOT counted as a confirmed win."],
            ["CONFIRMED_ADVERSE", "Requires explicit I-130/Petition for Alien Relative denial/rejection language."],
            ["Receipt date", "Extracted only from I-130-specific filing/receipt context; generic petition dates are excluded."],
            ["Service center", "Accepted only from I-130/receipt-local context, including receipt-prefix evidence."],
            ["Lawyer discovery", "Counts plaintiff filing counsel inferred from the initiating complaint/petition description; government-side indexed counsel are not counted."],
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
            audited.append({**r, "outcome": "UNKNOWN", "tier": 4, "audit_error": repr(exc)})

    # Persist the expensive named-case audit before optional discovery. If discovery later
    # fails, Actions still uploads the useful case evidence instead of an empty artifact.
    write_outputs(out, audited, [], [], seed_rows(cfg), errors, raw)

    try:
        discovery_cases, discovery_ranking, discovery_raw = discover(s, cfg.get("discovery_queries", []), args.max_pages, errors)
        raw["discovery"] = discovery_raw
    except Exception as exc:
        errors.append({"stage": "discovery_fatal", "target": "all", "error": repr(exc), "traceback": traceback.format_exc(limit=3)})
        discovery_cases, discovery_ranking = [], []

    write_outputs(out, audited, discovery_ranking, discovery_cases, seed_rows(cfg), errors, raw)
    print("Done", out.resolve())
    print("Errors captured:", len(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
