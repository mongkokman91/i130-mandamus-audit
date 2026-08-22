import unittest

from pacer_audit import (
    classify,
    is_initiating_document,
    looks_like_government_counsel,
    specific_i130_outcome,
)


class OutcomeClassificationTests(unittest.TestCase):
    def test_complaint_is_ineligible_outcome_source(self):
        doc = {
            "document_number": "1",
            "entry_description": "COMPLAINT for Writ of Mandamus",
        }
        self.assertTrue(is_initiating_document(doc))

    def test_amended_complaint_is_ineligible(self):
        doc = {"entry_description": "AMENDED COMPLAINT"}
        self.assertTrue(is_initiating_document(doc))

    def test_hypothetical_approval_is_not_confirmed(self):
        text = "If approved, Plaintiff's I-130 will permit the next immigration step."
        self.assertEqual(specific_i130_outcome(text), (None, None))

    def test_failure_to_adjudicate_is_not_confirmed(self):
        text = "USCIS's failure to adjudicate Plaintiff's I-130 continues."
        self.assertEqual(specific_i130_outcome(text), (None, None))

    def test_requested_relief_is_not_confirmed(self):
        text = "Plaintiff asks the Court to compel USCIS to adjudicate the I-130."
        self.assertEqual(specific_i130_outcome(text), (None, None))

    def test_later_explicit_approval_is_confirmed(self):
        label, context = specific_i130_outcome(
            "On June 4, 2026, USCIS approved Plaintiff's I-130."
        )
        self.assertEqual(label, "CONFIRMED_FAVORABLE")
        self.assertIn("approved", context)

    def test_voluntary_dismissal_remains_probable(self):
        outcome, _ = classify({"voluntary_dismissal": True}, "2026-06-05")
        self.assertEqual(outcome, "PROBABLE_FAVORABLE")

    def test_court_staff_names_are_excluded(self):
        for name in ("KNS, Deputy Clerk", "Court Staff", "Courtroom Deputy", "bas"):
            with self.subTest(name=name):
                self.assertTrue(looks_like_government_counsel(name))


if __name__ == "__main__":
    unittest.main()
