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
            # Missing text is legitimate; a failed API fetch is not.  Let the
            # caller record the failure so coverage cannot be certified.
            detail = api_get(s, f"{REST_BASE}/recap-documents/{doc_id}/")
            _DOCUMENT_TEXT_CACHE[doc_id] = detail.get("plain_text") or ""
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


def extract_venue_context(text):
    """Extract the complaint paragraph that pleads federal venue."""
    matches = list(re.finditer(
        r"(?:28\s+U\.?S\.?C\.?\s*§?\s*1391|venue\s+is\s+proper|venue\s+lies)",
        text,
        re.I,
    ))
    if not matches:
        return None
    match = matches[0]
    return re.sub(
        r"\s+", " ",
        text[max(0, match.start()-350):min(len(text), match.end()+850)],
    ).strip()


def infer_foreign_residence(text):
    """Return a conservative foreign-residence flag, country, and context."""
    patterns = (
        r"(?:plaintiff|petitioner).{0,100}(?:resides|lives|resident).{0,60}"
        r"(Canada|China|India|Pakistan|Mexico|United Kingdom|France|Germany|"
        r"Turkey|Nigeria|Iran|Iraq|Afghanistan|United Arab Emirates)",
        r"(?:resides|lives|resident)\s+(?:in|of)\s+"
        r"(Canada|China|India|Pakistan|Mexico|United Kingdom|France|Germany|"
        r"Turkey|Nigeria|Iran|Iraq|Afghanistan|United Arab Emirates)",
        r"resides?\s+outside\s+(?:of\s+)?the\s+United\s+States",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            country = match.group(1) if match.lastindex else "Outside United States"
            context = re.sub(
                r"\s+", " ",
                text[max(0, match.start()-220):min(len(text), match.end()+220)],
            ).strip()
            return True, country, context
    return False, None, None


def is_us_citizen_petitioner(text):
    return bool(re.search(
        r"(?:plaintiff|petitioner).{0,100}(?:is|was)\s+(?:a\s+)?"
        r"(?:United States|U\.?S\.?)\s+citizen|"
        r"(?:United States|U\.?S\.?)\s+citizen.{0,100}(?:plaintiff|petitioner)",
        text,
        re.I | re.S,
    ))


def is_initiating_document(doc):
    """Return True for complaints/petitions that cannot prove a later outcome."""
    description = " ".join(str(doc.get(k) or "") for k in (
        "entry_description", "description", "short_description"
    ))
    starts_as_pleading = bool(re.search(
        r"^\s*(?:amended\s+)?(?:civil\s+)?complaint\b|"
        r"^\s*(?:amended\s+)?petition\s+for\s+(?:a\s+)?writ\b|"
        r"^\s*initiating\s+petition\b",
        description, re.I,
    ))
    first_document_pleading = (
        str(doc.get("document_number") or "").strip() == "1"
        and bool(re.search(
            r"\b(?:civil\s+)?complaint\b|\bpetition\s+for\s+(?:a\s+)?writ\b",
            description, re.I,
        ))
    )
    return starts_as_pleading or first_document_pleading


def outcome_context_is_nonhistorical(context):
    """Reject requested, conditional, and failure-to-act language."""
    return bool(re.search(
        r"\b(?:if|when|once)\s+(?:approved|adjudicated)\b|"
        r"\b(?:if|when|once)\s+(?:the\s+)?(?:I[-\s]?130|petition)?"
        r".{0,60}(?:is|were|was|has been)?\s*(?:approved|adjudicated)\b|"
        r"\b(?:should|must|may|could|would)\s+(?:be\s+)?(?:approved|adjudicated)\b|"
        r"\b(?:failure|failed|refusal|refused)\s+to\s+(?:approve|adjudicate)\b|"
        r"\bright\s+to\s+have.{0,80}(?:approved|adjudicated)\b|"
        r"\b(?:request(?:s|ed)?|seek(?:s|ing)?|pray(?:s|er)?|ask(?:s|ed)?|compel(?:ling)?)"
        r".{0,100}(?:approve|adjudicate)\b",
        context,
        re.I | re.S,
    ))


def complaint_has_pending_i130(text):
    """Require complaint-local proof that the I-130 itself was pending at filing."""
    for ctx in contexts(text, r"\bI[-\s]?130\b|Petition for Alien Relative", radius=240):
        if re.search(
            r"\b(?:remains?|is|was|has been|still)\s+pending\b|"
            r"\bpending\s+(?:with|before|at)\s+USCIS\b|"
            r"\bnot\s+(?:yet\s+)?(?:adjudicated|approved|decided)\b|"
            r"\bfail(?:ed|ure)?\s+to\s+(?:adjudicate|decide)\b",
            ctx,
            re.I,
        ):
            return True
    return False


def specific_i130_outcome(text):
    """Return mandamus outcome, benefit decision, and local evidence context.

    Mandamus seeks a decision, not a particular merits result.  A documented
    post-filing I-130 approval *or denial* therefore confirms adjudication.
    The benefit decision is retained separately so it cannot be mistaken for
    litigation success or failure.
    """
    patterns_by_decision = (
        ("APPROVED", [
            r"(?:I[-\s]?130|Petition for Alien Relative).{0,180}(?:was|has been|is|were)?\s*approved",
            r"approved.{0,180}(?:I[-\s]?130|Petition for Alien Relative)",
            r"USCIS.{0,120}approved.{0,160}(?:plaintiff(?:'s|s)?\s+)?(?:I[-\s]?130|Petition for Alien Relative)",
        ]),
        ("DENIED", [
            r"(?:I[-\s]?130|Petition for Alien Relative).{0,180}(?:was|has been|is)?\s*(?:denied|rejected)",
            r"(?:denied|rejected).{0,180}(?:I[-\s]?130|Petition for Alien Relative)",
        ]),
        ("ADJUDICATED_UNSPECIFIED", [
            r"(?:I[-\s]?130|Petition for Alien Relative).{0,180}(?:was|has been|is|were)?\s*(?:adjudicated|decided)",
            r"(?:adjudicated|decided).{0,180}(?:I[-\s]?130|Petition for Alien Relative)",
            r"USCIS.{0,120}(?:adjudicated|decided).{0,160}(?:plaintiff(?:'s|s)?\s+)?(?:I[-\s]?130|Petition for Alien Relative)",
        ]),
    )
    for benefit_decision, patterns in patterns_by_decision:
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I | re.S):
                context = re.sub(
                    r"\s+", " ",
                    text[max(0, match.start()-180):match.end()+180],
                ).strip()
                if not outcome_context_is_nonhistorical(context):
                    return "CONFIRMED_FAVORABLE", benefit_decision, context
    return None, None, None


def classify(flags, terminated, explicit=None, context=None):
    if explicit:
        return explicit, context
    if flags.get("adverse") and terminated:
        return "ADVERSE_LITIGATION", None
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


def select_audit_documents(docs, max_documents=15):
    """Bound API work while retaining complaints and the newest later filings."""
    if not max_documents or len(docs) <= max_documents:
        return docs
    initiating = [doc for doc in docs if is_initiating_document(doc)][:3]
    initiating_ids = {str(doc.get("id") or id(doc)) for doc in initiating}
    later = [
        doc for doc in docs
        if str(doc.get("id") or id(doc)) not in initiating_ids
    ]
    later.sort(
        key=lambda doc: (
            str(doc.get("entry_date") or ""),
            sortable_document_number(doc.get("document_number")),
        ),
        reverse=True,
    )
    selected = initiating + later[:max(0, max_documents - len(initiating))]
    selected.sort(key=lambda doc: (
        str(doc.get("entry_date") or ""),
        sortable_document_number(doc.get("document_number")),
    ))
    return selected


def audit_case(s, row, patterns, max_documents=15):
    _, docs = docket_docs(s, int(row["docket_id"]))
    docs_to_audit = select_audit_documents(docs, max_documents)
    combined, initiating_texts, evidence, explicit_outcomes = [], [], [], []
    for d in docs_to_audit:
        txt = document_text(s, d)
        if not txt:
            continue
        combined.append(txt)
        initiating = is_initiating_document(d)
        if initiating:
            initiating_texts.append(txt)
        flags = evidence_flags(txt, patterns)
        outcome_eligible = not initiating
        explicit_outcome, benefit_decision, explicit_ctx = (
            specific_i130_outcome(txt) if outcome_eligible else (None, None, None)
        )
        if explicit_outcome:
            explicit_outcomes.append((
                d.get("entry_date") or "", explicit_outcome,
                benefit_decision, explicit_ctx,
            ))
        if any(flags.values()) or explicit_outcome:
            evidence.append({
                "document_id": d.get("id"),
                "document_number": d.get("document_number"),
                "entry_date": d.get("entry_date"),
                "description": d.get("description") or d.get("short_description"),
                "flags": ";".join(k for k, v in flags.items() if v),
                "explicit_i130_outcome": explicit_outcome,
                "benefit_decision": benefit_decision,
                "explicit_i130_context": explicit_ctx,
                "outcome_source_eligible": outcome_eligible,
            })
    text = "\n".join(combined)
    complaint_text = "\n".join(initiating_texts)
    i130_pending_at_filing = complaint_has_pending_i130(complaint_text)
    flags = evidence_flags(text, patterns)
    receipt, receipt_score, receipt_context = find_receipt_date(text, row.get("date_filed"))
    service, service_context = infer_service_center(text)
    filed = pd.to_datetime(row.get("date_filed"), errors="coerce", utc=True)
    rec = pd.to_datetime(receipt, errors="coerce", utc=True)
    delay = round((filed - rec).days / 30.4375, 1) if pd.notna(filed) and pd.notna(rec) else None
    terminated = pd.to_datetime(row.get("date_terminated"), errors="coerce", utc=True)
    days_to_termination = int((terminated - filed).days) if pd.notna(terminated) and pd.notna(filed) else None
    venue_context = extract_venue_context(complaint_text)
    foreign_resident, plaintiff_country, foreign_context = infer_foreign_residence(complaint_text)
    explicit_outcomes.sort(key=lambda item: item[0])
    latest_explicit = explicit_outcomes[-1] if explicit_outcomes else (None, None, None, None)
    if not i130_pending_at_filing:
        latest_explicit = (None, None, None, None)
    if i130_pending_at_filing:
        outcome, outcome_context = classify(
            flags,
            row.get("date_terminated"),
            latest_explicit[1],
            latest_explicit[3],
        )
    else:
        outcome, outcome_context = "NOT_RELEVANT", None
    result = {
        **row,
        "relationship_evidence": "spouse" if flags.get("spouse") else None,
        "i130_evidence": flags.get("i130", False),
        "pending_language": flags.get("pending", False),
        "i130_pending_at_filing": i130_pending_at_filing,
        "receipt_date_extracted": receipt,
        "receipt_date_confidence_score": receipt_score,
        "receipt_date_context": receipt_context,
        "delay_months_extracted": delay,
        "us_citizen_petitioner_evidence": is_us_citizen_petitioner(complaint_text),
        "foreign_resident_plaintiff_evidence": foreign_resident,
        "plaintiff_country": plaintiff_country,
        "foreign_residence_context": foreign_context,
        "alleged_venue_basis": venue_context,
        "service_center_evidence": service,
        "service_center_context": service_context,
        "document_count": len(docs),
        "documents_audited": len(docs_to_audit),
        "evidence_document_count": len(evidence),
        "outcome": outcome,
        "outcome_context": outcome_context,
        "benefit_decision": latest_explicit[2],
        "benefit_decision_context": latest_explicit[3],
        "explicit_post_filing_uscis_action": bool(latest_explicit[1]),
        "days_lawsuit_to_termination": days_to_termination,
        "approval_language": latest_explicit[2] == "APPROVED",
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
        if is_initiating_document(d):
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


def prioritize_discovery_records(records, max_dockets):
    """Prefer recent Maryland cases, then bounded controls."""
    def key(record):
        cohort = record.get("_discovery_cohort") or ""
        priority = 0 if cohort.startswith("md_") else 1
        filed = pd.to_datetime(record.get("dateFiled"), errors="coerce")
        filed_rank = -filed.value if pd.notna(filed) else 0
        return (priority, filed_rank, str(record.get("docket_id") or ""))
    ordered = sorted(records, key=key)
    return ordered[:max_dockets] if max_dockets else ordered


def discover(s, queries, max_pages, error_rows, max_dockets=None):
    dockets, raw = {}, []
    failed_queries = 0
    for params in queries:
        cohort = params.get("_cohort") or "control"
        search_params = {k: v for k, v in params.items() if not k.startswith("_")}
        try:
            rs, pages = search(s, search_params, max_pages)
            raw.append({
                "params": params,
                "count": len(rs),
                "page_count": len(pages),
                "truncated_at_page_limit": bool(pages and pages[-1].get("next")),
                "pages": pages,
            })
            for r in dedupe(rs):
                # Exact benefit-term searches define candidate recall.  Do not
                # discard a hit merely because optional NOS/cause metadata is
                # blank or miscoded.
                if relevant(r) and r.get("docket_id"):
                    candidate = dict(r)
                    candidate["_discovery_cohort"] = cohort
                    current = dockets.get(str(r.get("docket_id")))
                    if current is None or (
                        cohort.startswith("md_")
                        and not str(current.get("_discovery_cohort") or "").startswith("md_")
                    ):
                        dockets[str(r.get("docket_id"))] = candidate
        except Exception as exc:
            failed_queries += 1
            error_rows.append({"stage": "discovery", "target": json.dumps(params), "error": repr(exc)})
            raw.append({"params": params, "error": repr(exc), "page_count": 0,
                        "truncated_at_page_limit": False, "pages": []})
    if failed_queries:
        raise RuntimeError(
            f"{failed_queries} required discovery queries failed after retries; "
            "rankings are intentionally suppressed."
        )
    lawyer_counts = Counter()
    rows = []
    for r in prioritize_discovery_records(dockets.values(), max_dockets):
        try:
            counsel, source_desc = plaintiff_counsel_from_docket(s, r.get("docket_id"))
        except Exception as exc:
            error_rows.append({"stage": "discovery_docket", "target": str(r.get("docket_id")), "error": repr(exc)})
            counsel, source_desc = None, None
        if counsel and not looks_like_government_counsel(counsel):
            lawyer_counts[counsel] += 1
        else:
            counsel = None
        # Counsel extraction quality must never control case inclusion.
        case_row = norm(
            counsel,
            {"cohort": "focused_discovery"},
            r,
            source="focused_discovery",
        )
        case_row.update({
            "discovery_cohort": r.get("_discovery_cohort"),
            "counsel_side": "plaintiff_filing_counsel" if counsel else "unresolved",
            "counsel_source_description": source_desc,
        })
        rows.append(case_row)
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
        if r.get("lawyer") and r.get("i130_pending_at_filing"):
            by[r["lawyer"]].append(r)
    stats = []
    for lawyer, rows in by.items():
        confirmed = sum(r.get("outcome") == "CONFIRMED_FAVORABLE" for r in rows)
        adverse = sum(r.get("outcome") == "ADVERSE_LITIGATION" for r in rows)
        probable = sum(r.get("outcome") == "PROBABLE_FAVORABLE" for r in rows)
        pending = sum(r.get("outcome") == "PENDING" for r in rows)
        unknown = sum(r.get("outcome") == "UNKNOWN" for r in rows)
        tier_counts = {t: sum(r.get("tier") == t for r in rows) for t in (1, 2, 3, 4)}
        ysc = sum(bool(r.get("potomac_ysc_bonus")) for r in rows)
        reliable_delay = sum(r.get("receipt_date_confidence_score") is not None and r.get("receipt_date_confidence_score") >= 8 for r in rows)
        favorable_rows = [r for r in rows if r.get("outcome") == "CONFIRMED_FAVORABLE"]
        favorable_tier1 = sum(r.get("tier") == 1 for r in favorable_rows)
        favorable_tier2 = sum(r.get("tier") == 2 for r in favorable_rows)
        favorable_ysc = sum(bool(r.get("potomac_ysc_bonus")) for r in favorable_rows)
        approved = sum(r.get("benefit_decision") == "APPROVED" for r in rows)
        denied = sum(r.get("benefit_decision") == "DENIED" for r in rows)
        unspecified = sum(r.get("benefit_decision") == "ADJUDICATED_UNSPECIFIED" for r in rows)
        # This is an experience-evidence score, never a success rate.
        score = confirmed * 100 + favorable_tier1 * 30 + favorable_tier2 * 15 + favorable_ysc * 10 - adverse * 40
        stats.append({
            "lawyer": lawyer,
            "audited_cases": len(rows),
            "confirmed_favorable": confirmed,
            "probable_favorable_not_counted_as_win": probable,
            "adverse_litigation": adverse,
            "approved_i130": approved,
            "denied_i130": denied,
            "adjudicated_result_unspecified": unspecified,
            "pending": pending,
            "unknown": unknown,
            "tier1_cases": tier_counts[1],
            "tier2_cases": tier_counts[2],
            "tier3_cases": tier_counts[3],
            "tier4_cases": tier_counts[4],
            "potomac_ysc_cases": ysc,
            "high_confidence_receipt_dates": reliable_delay,
            "experience_evidence_score_not_success_rate": score,
        })
    return sorted(stats, key=lambda r: (-r["experience_evidence_score_not_success_rate"], -r["confirmed_favorable"], -r["tier1_cases"], -r["tier2_cases"]))


def atomic_evidence_rows(audited):
    rows = []
    flag_names = (
        "spouse", "i130", "pending", "approval", "adjudication",
        "voluntary_dismissal", "adverse", "service_centers",
    )
    for case in audited:
        try:
            documents = json.loads(case.get("evidence_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            documents = []
        for document in documents:
            flags = set(filter(None, str(document.get("flags") or "").split(";")))
            row = {
                "docket_id": case.get("docket_id"),
                "case_name": case.get("case_name"),
                "lawyer": case.get("lawyer"),
                "document_id": document.get("document_id"),
                "document_number": document.get("document_number"),
                "entry_date": document.get("entry_date"),
                "description": document.get("description"),
                "explicit_i130_outcome": document.get("explicit_i130_outcome"),
                "benefit_decision": document.get("benefit_decision"),
                "explicit_i130_context": document.get("explicit_i130_context"),
                "outcome_source_eligible": document.get("outcome_source_eligible"),
            }
            row.update({f"flag_{name}": name in flags for name in flag_names})
            rows.append(row)
    return rows


def write_outputs(out, audited, discovery_ranking, discovery_cases, seeds, errors, raw):
    out.mkdir(parents=True, exist_ok=True)
    atomic_cases = [
        {key: value for key, value in case.items() if key != "evidence_json"}
        for case in audited
    ]
    frames = {
        "Case Evidence": pd.DataFrame(atomic_cases),
        "Evidence Documents": pd.DataFrame(atomic_evidence_rows(audited)),
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
            ["CONFIRMED_FAVORABLE", "Requires explicit post-filing I-130 adjudication language. Approval and denial both satisfy the mandamus objective; the merits decision is separate."],
            ["PROBABLE_FAVORABLE", "Voluntary/stipulated dismissal only; NOT counted as a confirmed win."],
            ["ADVERSE_LITIGATION", "Court dismissal or merits loss without documented post-filing I-130 adjudication."],
            ["Benefit decision", "APPROVED, DENIED, or ADJUDICATED_UNSPECIFIED; never used as a proxy for litigation success."],
            ["Receipt date", "Extracted only from I-130-specific filing/receipt context; generic petition dates are excluded."],
            ["Service center", "Accepted only from I-130/receipt-local context, including receipt-prefix evidence."],
            ["Lawyer discovery", "Counts plaintiff filing counsel inferred from the initiating complaint/petition description; government-side indexed counsel are not counted."],
        ], columns=["item", "rule"])
        methodology.to_excel(w, sheet_name="Methodology", index=False)
    with (out / "raw_api.json").open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def checkpoint_outputs(out, audited, discovery_ranking, discovery_cases, cfg, errors, raw):
    """Persist all useful work completed so far after each expensive case audit."""
    write_outputs(
        out, audited, discovery_ranking, discovery_cases,
        seed_rows(cfg), errors, raw,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--max-dockets", type=int, default=10)
    ap.add_argument("--max-documents-per-docket", type=int, default=15)
    ap.add_argument("--cohort")
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
    discovery_queries = cfg.get("discovery_queries", [])
    if args.cohort:
        discovery_queries = [
            query for query in discovery_queries
            if query.get("_cohort") == args.cohort
        ]
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
            audited.append(audit_case(
                s, r, cfg.get("evidence_patterns", {}),
                args.max_documents_per_docket,
            ))
            print(f"audit {i}/{len(unique)} {r['case_name']}")
        except Exception as exc:
            errors.append({"stage": "case_audit", "target": f"{r.get('case_name')} | {r.get('docket_id')}", "error": repr(exc), "traceback": traceback.format_exc(limit=3)})
            audited.append({**r, "outcome": "UNKNOWN", "tier": 4, "audit_error": repr(exc)})

    # Persist the expensive named-case audit before optional discovery. If discovery later
    # fails, Actions still uploads the useful case evidence instead of an empty artifact.
    checkpoint_outputs(out, audited, [], [], cfg, errors, raw)

    try:
        discovery_cases, discovery_ranking, discovery_raw = discover(
            s, discovery_queries, args.max_pages, errors, args.max_dockets
        )
        raw["discovery"] = discovery_raw
    except Exception as exc:
        errors.append({"stage": "discovery_fatal", "target": "all", "error": repr(exc), "traceback": traceback.format_exc(limit=3)})
        discovery_cases, discovery_ranking = [], []

    known_dockets = {str(row.get("docket_id")) for row in audited}
    discovery_to_audit = [
        row for row in discovery_cases
        if str(row.get("docket_id")) not in known_dockets
    ]
    for i, row in enumerate(discovery_to_audit, 1):
        try:
            audited.append(audit_case(
                s, row, cfg.get("evidence_patterns", {}),
                args.max_documents_per_docket,
            ))
            print(f"focused audit {i}/{len(discovery_to_audit)} {row['case_name']}")
            checkpoint_outputs(
                out, audited, discovery_ranking, discovery_cases,
                cfg, errors, raw,
            )
        except Exception as exc:
            errors.append({
                "stage": "focused_case_audit",
                "target": f"{row.get('case_name')} | {row.get('docket_id')}",
                "error": repr(exc),
                "traceback": traceback.format_exc(limit=3),
            })
            audited.append({**row, "outcome": "UNKNOWN", "tier": 4, "audit_error": repr(exc)})

    checkpoint_outputs(out, audited, discovery_ranking, discovery_cases, cfg, errors, raw)
    print("Done", out.resolve())
    print("Errors captured:", len(errors))
    required_search_failed = any(
        row.get("stage") in {"discovery", "discovery_fatal"}
        for row in errors
    )
    if required_search_failed:
        print("INCOMPLETE: required discovery query failure; no ranking is decision-grade.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
