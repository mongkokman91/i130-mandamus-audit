#!/usr/bin/env python3
"""I-130 mandamus audit helper.

Uses the CourtListener REST API to collect RECAP search results for configured
lawyers, preserves raw API responses, and produces normalized CSV/XLSX output.

Security: provide COURTLISTENER_API_KEY through the environment. Never hard-code
credentials in this repository.
"""
from __future__ import annotations
import argparse, json, os, sys
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
    with path.open("r", encoding="utf-8") as fh: return yaml.safe_load(fh)

def make_session(api_key: str) -> requests.Session:
    s=requests.Session(); s.headers.update({"Authorization":f"Token {api_key}","Accept":"application/json","User-Agent":"i130-mandamus-audit/0.2"}); return s

def api_get(session, url, *, params=None):
    r=session.get(url,params=params,timeout=60); r.raise_for_status(); return r.json()

def iter_search(session, search_params: dict[str, Any], *, max_pages=10):
    """Search RECAP using structured CourtListener parameters.

    Important: attorney filtering belongs in the `atty_name` parameter. Putting
    `atty_name:` syntax inside q is not equivalent and previously produced false
    zero-result searches.
    """
    url=f"{API_BASE}/search/"; params={"type":"r", **search_params}; results=[]; pages=[]
    for _ in range(max_pages):
        payload=api_get(session,url,params=params); pages.append(payload)
        batch=payload.get("results") or []
        if isinstance(batch,list): results.extend(batch)
        nxt=payload.get("next")
        if not nxt: break
        url=nxt; params=None
    return results,pages

def pick(record,*keys):
    for k in keys:
        v=record.get(k)
        if v not in (None,"",[],{}): return v
    return None

def normalize_result(lawyer, search_params, record):
    return {"lawyer":lawyer,"query":json.dumps(search_params,ensure_ascii=False,sort_keys=True),"case_name":pick(record,"caseName","case_name","caption","name"),"docket_number":pick(record,"docketNumber","docket_number"),"court":pick(record,"court_citation_string","court","court_id"),"date_filed":pick(record,"dateFiled","date_filed"),"date_terminated":pick(record,"dateTerminated","date_terminated"),"absolute_url":pick(record,"docket_absolute_url","absolute_url","url"),"docket_id":pick(record,"docket_id","docketId"),"description":pick(record,"description","short_description","snippet"),"search_result_raw":json.dumps(record,ensure_ascii=False,sort_keys=True)}

def seed_case_rows(config):
    rows=[]
    for case in config.get("seed_cases",[]):
        row=dict(case); filed=pd.to_datetime(row.get("filed_date"),errors="coerce"); term=pd.to_datetime(row.get("terminated_date"),errors="coerce")
        row["days_filing_to_termination"]=int((term-filed).days) if pd.notna(filed) and pd.notna(term) else None; rows.append(row)
    return rows

def write_outputs(output_dir, search_rows, seed_rows, raw_bundle):
    output_dir.mkdir(parents=True,exist_ok=True); search_df=pd.DataFrame(search_rows); seed_df=pd.DataFrame(seed_rows)
    search_df.to_csv(output_dir/"search_results.csv",index=False); seed_df.to_csv(output_dir/"seed_case_audit.csv",index=False)
    with pd.ExcelWriter(output_dir/"case_audit.xlsx",engine="openpyxl") as writer:
        search_df.to_excel(writer,sheet_name="RECAP Search",index=False); seed_df.to_excel(writer,sheet_name="Seed Audit",index=False)
        pd.DataFrame([["CONFIRMED_FAVORABLE","Underlying filing/order expressly establishes relevant relief or adjudication."],["PROBABLE_FAVORABLE","Closure is consistent with favorable agency action but causation/result is not expressly established."],["CONFIRMED_ADVERSE","Government/court result is expressly adverse on the relevant claim."],["PENDING","Case remains active or no termination is established."],["UNKNOWN","Available evidence is insufficient for a reliable classification."]],columns=["classification","definition"]).to_excel(writer,sheet_name="Methodology",index=False)
    with (output_dir/"raw_search_pages.json").open("w",encoding="utf-8") as fh: json.dump(raw_bundle,fh,indent=2,ensure_ascii=False)

def main():
    p=argparse.ArgumentParser(); p.add_argument("--config",default=DEFAULT_CONFIG); p.add_argument("--output",default=DEFAULT_OUTPUT); p.add_argument("--max-pages",type=int,default=10); args=p.parse_args()
    key=os.environ.get("COURTLISTENER_API_KEY","").strip()
    if not key: print("ERROR: COURTLISTENER_API_KEY is not set.",file=sys.stderr); return 2
    config=load_config(Path(args.config)); session=make_session(key); rows=[]; raw={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"queries":[]}
    for lawyer in config.get("lawyers",[]):
        name=lawyer["name"]; params=lawyer.get("search_params") or {"atty_name":name,"q":"I-130"}; print(f"Searching: {name} | {params}")
        try: results,pages=iter_search(session,params,max_pages=args.max_pages)
        except requests.HTTPError as exc:
            status=exc.response.status_code if exc.response is not None else "?"; body=exc.response.text[:1000] if exc.response is not None else str(exc); print(f"WARNING: search failed for {name} (HTTP {status}): {body}",file=sys.stderr); raw["queries"].append({"lawyer":name,"search_params":params,"error":str(exc),"pages":[]}); continue
        raw["queries"].append({"lawyer":name,"search_params":params,"result_count":len(results),"pages":pages}); rows.extend(normalize_result(name,params,r) for r in results); print(f"  found {len(results)} result(s)")
    write_outputs(Path(args.output),rows,seed_case_rows(config),raw); print(f"Done. Outputs written to: {Path(args.output).resolve()}"); print("RECAP coverage is incomplete; zero hits never prove zero PACER cases."); return 0
if __name__=="__main__": raise SystemExit(main())
