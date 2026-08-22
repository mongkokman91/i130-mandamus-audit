import unittest

from pacer_audit import complaint_has_pending_i130, lawyer_stats, load_config


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
        self.assertEqual(stats["ranking_score"], 0)
        self.assertIsNone(stats["confirmed_win_rate"])

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

    def test_every_discovery_query_has_expected_date_window(self):
        queries = load_config("cases.yaml")["discovery_queries"]
        self.assertTrue(queries)
        for query in queries:
            expected = "2023-08-21" if query.get("court") == "mdd" else "2019-01-01"
            self.assertEqual(query.get("filed_after"), expected)


if __name__ == "__main__":
    unittest.main()
