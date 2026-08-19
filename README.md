# I-130 Mandamus Audit

A conservative research pipeline for comparing federal immigration litigators using CourtListener/RECAP evidence for delayed Form I-130 APA/mandamus cases.

The project is designed for a mobile workflow: GitHub stores the current code, while Google Colab pulls the newest `main` branch and runs it. Secrets stay in Colab and are never committed to this repository.

## Current scope

The initial audit tracks:

- Hashim G. Jeelani
- Arif Gozel
- Benjamin G. Messer
- Carley Tatman

The target comparison is pending spousal I-130 unreasonable-delay litigation, with special attention to cases filed around 12–18 months of delay and to Potomac/YSC evidence.

## Outcome discipline

The script does **not** convert a voluntary dismissal into a proven win.

| Label | Meaning |
|---|---|
| `CONFIRMED_FAVORABLE` | Underlying filing/order expressly establishes relevant relief or adjudication. |
| `PROBABLE_FAVORABLE` | Closure is consistent with favorable agency action, but the available record does not expressly establish the result/causation. |
| `CONFIRMED_ADVERSE` | Government/court result is expressly adverse on the relevant claim. |
| `PENDING` | Case remains active or no termination is established. |
| `UNKNOWN` | Evidence is insufficient for a reliable classification. |

RECAP is incomplete. A zero-result search is never treated as proof that a lawyer has zero PACER cases.

## Files

- `pacer_audit.py` — CourtListener/RECAP search and export engine.
- `cases.yaml` — lawyer queries, methodology, and curated seed cases.
- `requirements.txt` — Python dependencies.
- `colab_runner.ipynb` — mobile runner that syncs the private repo and downloads the Excel result.
- `.gitignore` — blocks secrets, downloads, and generated audit outputs from being committed.

## One-time mobile setup

### 1. Create a fine-grained GitHub token

Create a fine-grained personal access token restricted to **this repository only**. It only needs enough repository access to clone/read the private repo. Do not give it write access unless you later have a reason to do so.

Store the token somewhere temporarily until step 3. Never paste it into this repository.

### 2. Get your CourtListener API token

Use your existing CourtListener/RECAP API token. Do not paste it into GitHub or into notebook source code.

### 3. Add both to Google Colab Secrets

Open `colab_runner.ipynb` in Google Colab. In Colab's Secrets panel add:

- `GITHUB_TOKEN`
- `COURTLISTENER_API_KEY`

Enable notebook access for both secrets.

### 4. Run the notebook

Use **Runtime → Run all**.

The notebook will:

1. securely clone or reset the local runtime to the newest GitHub `main` branch;
2. install dependencies;
3. run the CourtListener/RECAP audit;
4. create the output files; and
5. download `case_audit.xlsx` to your device.

Future code changes require no notebook replacement: open the same runner and use **Run all** again.

## Outputs

Generated under `output/` (ignored by git):

- `case_audit.xlsx`
- `search_results.csv`
- `seed_case_audit.csv`
- `raw_search_pages.json`

The workbook contains raw normalized search results, the curated seed-case audit, and the classification methodology.

## Local command-line use

```bash
export COURTLISTENER_API_KEY="..."
pip install -r requirements.txt
python pacer_audit.py
```

## Security

Never commit:

- CourtListener tokens
- PACER credentials
- GitHub personal access tokens
- `.env` files containing credentials
- downloaded court filings that should remain local

Generated output and downloads are excluded in `.gitignore` by default.

## Research roadmap

Next iterations should add document-level evidence retrieval and provenance, complaint/dismissal/order inspection, exact I-130 receipt-date extraction, service-center matching, comparable-delay scoring, and a lawyer-level denominator/statistics sheet. Any automated outcome upgrade should require explicit supporting evidence rather than inference from docket closure alone.
