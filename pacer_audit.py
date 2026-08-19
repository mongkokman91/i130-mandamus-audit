#!/usr/bin/env python3
"""Evidence-first I-130 mandamus audit using CourtListener RECAP."""
from __future__ import annotations
import argparse, json, os, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pandas as pd, requests, yaml

API_BASE="https://www.courtlistener.com/api/rest/v3"; DEFAULT_CONFIG="cases.yaml"; DEFAULT_OUTPUT="output"

def load_config(p):
    with Path(p).open(encoding="utf-8") as f:return yaml.safe_load(f)
def session_for(key):
    s=requests.Session();s.headers.update({"Authorization":f"Token {key}","Accept":"application/json","User-Agent":"i130-mandamus-audit/0.4"});return s
def get(s,url,params=None):
    r=s.get(url,params=params,timeout=60);r.raise_for_status();return r.json()
def search(s,params,max_pages=10):
    url=f"{API_BASE}/search/"; params={"type":"r",**params}; out=[];pages=[]
    for _ in range(max_pages):
        p=get(s,url,params);pages.append(p);out.extend(p.get("results") or []);url=p.get("next");params=None
        if not url:break
    return out,pages
def relevant(r):
    t=" ".join(str(r.get(k) or "") for k in ("suitNature","cause","caseName")).lower()
    return any(x in t for x in ("immigration","mandamus","administrative procedure","judicial review","mayorkas","uscis","edlow","blinken","pompeo","wolf","jaddou","noem"))
def dedupe(rs):
    d={}
    for r in rs:
        k=r.get("docket_id") or (r.get("court_id"),r.get("docketNumber"),r.get("caseName"));d.setdefault(str(k),r)
    return list(d.values())
def norm(lawyer,params,r,source="named"):
    return {"lawyer":lawyer,"source":source,"query":json.dumps(params,sort_keys=True),"case_name":r.get("caseName"),"docket_number":r.get("docketNumber"),"court":r.get("court_citation_string") or r.get("court"),"court_id":r.get("court_id"),"date_filed":r.get("dateFiled"),"date_terminated":r.get("dateTerminated"),"suit_nature":r.get("suitNature"),"cause":r.get("cause"),"absolute_url":r.get("docket_absolute_url"),"docket_id":r.get("docket_id")}
def docket_docs(s,docket_id,max_pages=20):
    url=f"{API_BASE}/docket-entries/";params={"docket":docket_id,"order_by":"date_filed"};docs=[];entries=[]
    for _ in range(max_pages):
        p=get(s,url,params); batch=p.get("results") or []; entries.extend(batch)
        for e in batch:
            for d in e.get("recap_documents") or []:
                docs.append({"entry_id":e.get("id"),"entry_date":e.get("date_filed"),"entry_description":e.get("description"),**d})
        url=p.get("next");params=None
        if not url:break
    return entries,docs
def document_text(s,doc):
    # Prefer API plain_text; fall back to indexed description/snippet.
    did=doc.get("id"); text=""
    if did:
        try:
            p=get(s,f"{API_BASE}/recap-documents/{did}/"); text=p.get("plain_text") or ""
        except requests.HTTPError: pass
    return "\n".join(x for x in [doc.get("entry_description") or "",doc.get("description") or "",doc.get("short_description") or "",doc.get("snippet") or "",text] if x)
def evidence_flags(text,patterns):
    lo=text.lower();return {k:any(term.lower() in lo for term in vals) for k,vals in patterns.items()}
def find_receipt_date(text):
    pats=[r"(?:filed|submitted|received).{0,80}(?:I-130|petition).{0,80}(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})",r"(?:I-130|petition).{0,80}(?:filed|submitted|received).{0,80}(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})"]
    for p in pats:
        m=re.search(p,text,re.I|re.S)
        if m:
            try:return pd.to_datetime(" ".join(m.groups())).date().isoformat()
            except Exception:pass
    return None
def classify(flags,terminated):
    if flags.get("adverse"):return "CONFIRMED_ADVERSE"
    if flags.get("approval") or flags.get("adjudication"):return "CONFIRMED_FAVORABLE"
    if flags.get("voluntary_dismissal") and terminated:return "PROBABLE_FAVORABLE"
    if not terminated:return "PENDING"
    return "UNKNOWN"
def audit_case(s,row,patterns):
    entries,docs=docket_docs(s,int(row["docket_id"])); combined=[]; evidence=[]
    for d in docs:
        txt=document_text(s,d)
        if not txt:continue
        combined.append(txt); f=evidence_flags(txt,patterns)
        if any(f.values()): evidence.append({"document_id":d.get("id"),"document_number":d.get("document_number"),"entry_date":d.get("entry_date"),"short_description":d.get("short_description"),"flags":";".join(k for k,v in f.items() if v)})
    text="\n".join(combined); flags=evidence_flags(text,patterns); receipt=find_receipt_date(text)
    service=""
    lo=text.lower()
    for term in patterns.get("service_centers",[]):
        if term.lower() in lo: service=term;break
    filed=pd.to_datetime(row.get("date_filed"),errors="coerce"); rec=pd.to_datetime(receipt,errors="coerce")
    delay=round((filed-rec).days/30.4375,1) if pd.notna(filed) and pd.notna(rec) else None
    return {**row,"relationship_evidence":"spouse" if flags.get("spouse") else None,"i130_evidence":flags.get("i130",False),"pending_language":flags.get("pending",False),"receipt_date_extracted":receipt,"delay_months_extracted":delay,"service_center_evidence":service or None,"document_count":len(docs),"evidence_document_count":len(evidence),"outcome":classify(flags,row.get("date_terminated")),"approval_language":flags.get("approval",False),"adjudication_language":flags.get("adjudication",False),"voluntary_dismissal_language":flags.get("voluntary_dismissal",False),"adverse_language":flags.get("adverse",False),"evidence_json":json.dumps(evidence,ensure_ascii=False)}
def discover(s,queries,max_pages):
    dockets={}; raw=[]
    for params in queries:
        rs,pages=search(s,params,max_pages);raw.append({"params":params,"count":len(rs),"pages":pages})
        for r in dedupe(rs):
            if relevant(r):dockets.setdefault(str(r.get("docket_id")),r)
    lawyer_counts=Counter(); rows=[]
    for r in dockets.values():
        for a in r.get("attorney") or []:
            lawyer_counts[a]+=1;rows.append({"lawyer":a,"case_name":r.get("caseName"),"docket_number":r.get("docketNumber"),"court":r.get("court_citation_string"),"docket_id":r.get("docket_id")})
    ranking=[{"lawyer":k,"discovered_relevant_dockets":v} for k,v in lawyer_counts.most_common()]
    return rows,ranking,raw

def seed_rows(cfg):
    out=[]
    for c in cfg.get("seed_cases",[]):
        r=dict(c);a=pd.to_datetime(r.get("filed_date"),errors="coerce");b=pd.to_datetime(r.get("terminated_date"),errors="coerce");r["days_filing_to_termination"]=int((b-a).days) if pd.notna(a) and pd.notna(b) else None;out.append(r)
    return out
def main():
    ap=argparse.ArgumentParser();ap.add_argument("--config",default=DEFAULT_CONFIG);ap.add_argument("--output",default=DEFAULT_OUTPUT);ap.add_argument("--max-pages",type=int,default=10);args=ap.parse_args();key=os.environ.get("COURTLISTENER_API_KEY","").strip()
    if not key:print("ERROR: COURTLISTENER_API_KEY is not set",file=sys.stderr);return 2
    cfg=load_config(args.config);s=session_for(key);named=[];raw={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"named":[],"discovery":[]}
    for x in cfg.get("lawyers",[]):
        rs,pages=search(s,x["search_params"],args.max_pages);clean=[r for r in dedupe(rs) if relevant(r)];raw["named"].append({"lawyer":x["name"],"raw":len(rs),"unique":len(dedupe(rs)),"filtered":len(clean),"pages":pages});named.extend(norm(x["name"],x["search_params"],r) for r in clean);print(x["name"],len(clean))
    # Audit every unique named candidate docket once.
    unique={str(r["docket_id"]):r for r in named if r.get("docket_id")}; audited=[]
    for i,r in enumerate(unique.values(),1):
        try:audited.append(audit_case(s,r,cfg.get("evidence_patterns",{})));print(f"audit {i}/{len(unique)} {r['case_name']}")
        except Exception as e: audited.append({**r,"outcome":"UNKNOWN","audit_error":str(e)})
    disc_rows,ranking,disc_raw=discover(s,cfg.get("discovery_queries",[]),args.max_pages);raw["discovery"]=disc_raw
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    dfs={"Case Evidence":pd.DataFrame(audited),"Lawyer Discovery":pd.DataFrame(ranking),"Discovery Cases":pd.DataFrame(disc_rows),"Seed Audit":pd.DataFrame(seed_rows(cfg))}
    for name,df in dfs.items():df.to_csv(out/(name.lower().replace(" ","_")+".csv"),index=False)
    with pd.ExcelWriter(out/"case_audit.xlsx",engine="openpyxl") as w:
        for name,df in dfs.items():df.to_excel(w,sheet_name=name[:31],index=False)
        pd.DataFrame([["CONFIRMED_FAVORABLE","Explicit approval/adjudication language found."],["PROBABLE_FAVORABLE","Voluntary/stipulated dismissal language plus terminated docket; explicit adjudication not found."],["CONFIRMED_ADVERSE","Explicit adverse disposition language found."],["PENDING","Docket not terminated and no explicit result found."],["UNKNOWN","Insufficient deterministic evidence."]],columns=["classification","rule"]).to_excel(w,sheet_name="Methodology",index=False)
    with (out/"raw_api.json").open("w",encoding="utf-8") as f:json.dump(raw,f,ensure_ascii=False,indent=2)
    print("Done",out.resolve());return 0
if __name__=="__main__":raise SystemExit(main())
