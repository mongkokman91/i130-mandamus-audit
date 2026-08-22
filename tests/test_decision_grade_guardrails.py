import tempfile
import unittest
from pathlib import Path

import pandas as pd

from pacer_audit import (
    checkpoint_outputs,
    complaint_has_pending_i130,
    lawyer_stats,
    load_config,
    prioritize_discovery_records,
    select_audit_documents,
)


class DecisionGradeGuardrailTests(unittest.TestCase):
    def test_pending_i130_is_not_favorable_and_scores_zero(self):
        rows = [{
            "lawyer": "Pending Counsel",
            "outcome": "PENDING",
            "tier": 1,
            "potomac_ysc_bonus": True,
            "receipt_date_confidence_score": 9,
        }]
        stats = lawyer_stats(rows)[0]
        self.assertEqual(stats["confirmed_favorable"], 0)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["experience_evidence_score_not_success_rate"], 0)

    def test_already_approved_i130_is_not_pending_at_filing(self):
        text = (
            "USCIS approved the Form I-130 in 2018. "
            "The plaintiff now seeks adjudication of a pending Form I-601A."
        )
        self.assertFalse(complaint_has_pending_i130(text))

    def test_complaint_local_pending_i130_is_recognized(self):
        text = (
            "Plaintiff's Form I-130, Petition for Alien Relative, "
            "has been pending with USCIS since 2024."
        )
        self.assertTrue(complaint_has_pending_i130(text))

    def test_maryland_census_spans_a_decade_without_nos_or_cause_filters(self):
        queries = load_config("maryland_census.yaml")["discovery_queries"]
        md = [query for query in queries if query.get("court") == "mdd"]
        self.assertEqual(min(q["filed_after"] for q in md), "2016-08-01")
        self.assertTrue(all("nature_of_suit" not in q and "cause" not in q for q in md))

    def test_discovery_is_bounded_and_maryland_first(self):
        records = [
            {"docket_id": "c1", "dateFiled": "2026-08-01", "_discovery_cohort": "canada_control"},
            {"docket_id": "m1", "dateFiled": "2025-01-01", "_discovery_cohort": "md_2024_2025"},
            {"docket_id": "m2", "dateFiled": "2026-01-01", "_discovery_cohort": "md_2026"},
            {"docket_id": "c2", "dateFiled": "2026-07-01", "_discovery_cohort": "canada_control"},
        ]
        selected = prioritize_discovery_records(records, 3)
        self.assertEqual([r["docket_id"] for r in selected], ["m2", "m1", "c1"])

    def test_queries_are_split_into_explicit_cohorts(self):
        queries = load_config("maryland_census.yaml")["discovery_queries"]
        cohorts = [q.get("_cohort") for q in queries]
        self.assertEqual(sum(str(c).startswith("md_") for c in cohorts), 20)
        self.assertEqual(cohorts.count("canada_control"), 0)
        self.assertNotIn(None, cohorts)

    def test_checkpoint_writes_case_evidence_immediately(self):
        case = {
            "docket_id": "123", "case_name": "Checkpoint v. USCIS",
            "lawyer": "Test Counsel", "outcome": "PENDING", "tier": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_outputs(Path(tmp), [case], [], [], {"seed_cases": []}, [], {})
            saved = pd.read_csv(Path(tmp) / "case_evidence.csv")
            self.assertEqual(saved.loc[0, "docket_id"], 123)
            self.assertEqual(saved.loc[0, "outcome"], "PENDING")

    def test_document_selection_keeps_complaint_and_newest_filings(self):
        docs = [{
            "id": 1, "entry_date": "2024-01-01", "document_number": "1",
            "entry_description": "COMPLAINT for Writ of Mandamus",
        }]
        docs.extend({
            "id": i, "entry_date": f"2024-02-{i:02d}",
            "document_number": str(i), "entry_description": "Later filing",
        } for i in range(2, 12))
        selected = select_audit_documents(docs, 5)
        self.assertEqual(len(selected), 5)
        self.assertIn(1, [doc["id"] for doc in selected])
        self.assertEqual(
            sorted(doc["id"] for doc in selected if doc["id"] != 1),
            [8, 9, 10, 11],
        )


if __name__ == "__main__":
    unittest.main()
