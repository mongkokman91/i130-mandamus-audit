# I-130 Mandamus Audit

A conservative research pipeline for comparing federal immigration litigators using CourtListener/RECAP evidence for delayed Form I-130 APA/mandamus cases.

The project is designed for a mobile workflow: GitHub Actions runs the audit directly from this private repository. No Colab notebook is required for routine use.

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
- `.github/workflows/audit.yml` — manual GitHub Actions runner.
- `colab_runner.ipynb` — optional fallback runner; not needed for normal use.
- `.gitignore` — blocks secrets, downloads, and generated audit outputs from being committed.

## One-time setup

### Add the CourtListener API key as a GitHub Actions secret

In this repository, open:

`Settings → Secrets and variables → Actions → New repository secret`

Create exactly:

- Name: `COURTLISTENER_API_KEY`
- Secret: your CourtListener/RECAP API token

Do not commit the token to the repository and do not put it in workflow source.

## Run the audit from mobile

Open this repository on GitHub, then:

1. Tap **Actions**.
2. Select **I-130 Mandamus Audit**.
3. Tap **Run workflow**.
4. Leave branch as `main` and tap **Run workflow** again.
5. Open the completed workflow run.
6. Under **Artifacts**, download `i130-mandamus-audit-results`.

The artifact is retained for 30 days and contains the generated audit outputs.

## Outputs

Generated under `output/` during the workflow:

- `case_audit.xlsx`
- `search_results.csv`
- `seed_case_audit.csv`
- `raw_search_pages.json`

The workbook contains raw normalized search results, the curated seed-case audit, and the classification methodology.

## Local / Codespaces use

```bash
export COURTLISTENER_API_KEY="..."
pip install -r requirements.txt
python pacer_audit.py
```

Codespaces is optional and mainly useful for debugging or development. Routine runs should use the GitHub Actions button.

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
