import unittest

from agency_os.gate import (
    InvalidGateToken,
    assert_not_gate_opening,
    is_gate_opening_comment,
    parse_gate_decision,
    strip_quotes_and_code,
)


class ParseGateDecisionTests(unittest.TestCase):
    def test_plain_akkoord_is_accepted_for_normal_risk(self):
        decision = parse_gate_decision("AKKOORD\n\nGa door naar Kickoff.")
        self.assertEqual(decision.outcome, "akkoord")
        self.assertEqual(decision.token, "AKKOORD")

    def test_afgekeurd_is_accepted(self):
        decision = parse_gate_decision("AFGEKEURD\n\nPrijs klopt niet met de scorecard.")
        self.assertEqual(decision.outcome, "afgekeurd")

    def test_plain_akkoord_is_rejected_for_high_risk(self):
        with self.assertRaises(InvalidGateToken):
            parse_gate_decision("AKKOORD\n\nGa door.", high_risk=True)

    def test_exact_high_risk_token_is_accepted(self):
        decision = parse_gate_decision("AKKOORD RISICO-GEZIEN\n\nGa door.", high_risk=True)
        self.assertEqual(decision.outcome, "akkoord")

    def test_unknown_akkoord_variant_is_rejected(self):
        with self.assertRaises(InvalidGateToken):
            parse_gate_decision("AKKOORD, ziet er goed uit")

    def test_non_gate_comment_returns_none(self):
        self.assertIsNone(parse_gate_decision("Kun je dit nog toelichten?"))

    def test_empty_comment_returns_none(self):
        self.assertIsNone(parse_gate_decision(""))
        self.assertIsNone(parse_gate_decision(None))


class GateOpeningGuardTests(unittest.TestCase):
    def test_detects_akkoord_and_afgekeurd_openers(self):
        self.assertTrue(is_gate_opening_comment("AKKOORD\n\nverder"))
        self.assertTrue(is_gate_opening_comment("AFGEKEURD, reden: te duur"))
        self.assertFalse(is_gate_opening_comment("Dit lijkt me akkoord."))

    def test_agent_may_not_write_a_gate_opening_comment(self):
        with self.assertRaises(InvalidGateToken):
            assert_not_gate_opening("AKKOORD\n\nSpil zet dit door.", author_is_agent=True)

    def test_human_may_write_a_gate_opening_comment(self):
        assert_not_gate_opening("AKKOORD\n\nMens zet dit door.", author_is_agent=False)

    def test_agent_may_write_anything_else(self):
        assert_not_gate_opening("Poortkaart staat klaar, wacht op een mens.", author_is_agent=True)


class StripQuotesAndCodeTests(unittest.TestCase):
    """Spec 7.8: een token binnen een citaat of codeblok telt nooit."""

    def test_a_quoted_token_is_removed(self):
        self.assertEqual(strip_quotes_and_code("> AKKOORD\n\nzei de klant"), "\nzei de klant")

    def test_a_fenced_token_is_removed_with_its_block(self):
        text = "Ik citeer:\n\n```\nAKKOORD\n```\n\nmaar ik beslis nog niet."
        cleaned = strip_quotes_and_code(text)
        self.assertNotIn("AKKOORD", cleaned)
        self.assertIn("maar ik beslis nog niet.", cleaned)

    def test_a_tilde_fence_counts_too(self):
        self.assertNotIn("AKKOORD", strip_quotes_and_code("~~~\nAKKOORD\n~~~"))

    def test_a_language_tagged_fence_counts_too(self):
        self.assertNotIn("AKKOORD", strip_quotes_and_code("```markdown\nAKKOORD\n```"))

    def test_an_unclosed_fence_swallows_the_rest(self):
        self.assertEqual(strip_quotes_and_code("```\nAKKOORD\nnog meer"), "")

    def test_plain_text_is_untouched(self):
        self.assertEqual(strip_quotes_and_code("AKKOORD\n\nGa door."), "AKKOORD\n\nGa door.")

    def test_empty_input_is_empty_output(self):
        self.assertEqual(strip_quotes_and_code(""), "")
        self.assertEqual(strip_quotes_and_code(None), "")

    def test_a_quoted_token_does_not_parse_as_a_decision(self):
        self.assertIsNone(parse_gate_decision(strip_quotes_and_code("> AKKOORD")))

    def test_a_real_decision_still_parses_after_stripping(self):
        text = strip_quotes_and_code("AKKOORD\n\n> eerder zei ik: AFGEKEURD")
        decision = parse_gate_decision(text)
        self.assertEqual(decision.outcome, "akkoord")


if __name__ == "__main__":
    unittest.main()
