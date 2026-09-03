"""Commentvormen. De belangrijkste eis: geen enkele opent per ongeluk een poort."""

import unittest
from datetime import datetime, timezone

from agency_os.gate import assert_not_gate_opening, parse_gate_decision
from agency_os.linear import comments

from tests.support_linear import T0, make_issue, make_run

WHEN = datetime(2026, 9, 3, 9, 14, tzinfo=timezone.utc)


def a_gate_card(high_risk=False, risk_flags=()):
    return comments.gate_card(
        gate_no="merge", issue=make_issue(), what="De PR mag gemerged worden.",
        evidence=(), criteria="6 van 6 gehaald", reviewers="Reviewer 1: goedkeuren",
        disagreement="geen", risk="risico/midden", cost_so_far="€ 4,12 over 3 runs",
        high_risk=high_risk, run_id="3f9a2c", duration_s=12, cost_eur=0.02,
        risk_flags=risk_flags)


class TimeTests(unittest.TestCase):
    def test_comments_show_amsterdam_time(self):
        self.assertEqual(comments.local_time(WHEN), "2026-09-03 11:14")

    def test_a_naive_datetime_is_read_as_utc(self):
        self.assertEqual(comments.local_time(datetime(2026, 9, 3, 9, 14)), "2026-09-03 11:14")


class SignatureTests(unittest.TestCase):
    def test_the_signature_matches_spec_8_3(self):
        self.assertEqual(
            comments.signature("Ontwikkelaar", "Claude Opus 5", "3f9a2c", WHEN),
            "**Ontwikkelaar · Claude Opus 5 · run 3f9a2c · 2026-09-03 11:14**")

    def test_the_claim_line_is_one_line_and_carries_the_run_id(self):
        line = comments.claim_comment("3f9a2c", WHEN)
        self.assertEqual(line, "**Spil** claim 3f9a2c op 2026-09-03 11:14")
        self.assertNotIn("\n", line)


class RunCommentTests(unittest.TestCase):
    def test_it_has_every_part_of_the_output_contract(self):
        body = comments.run_comment(
            role_title="Redacteur", model_display="Claude Sonnet 5", run=make_run(),
            body_md="Ik heb vier weken bouwlogboek toegevoegd.",
            evidence=make_run().artefacten, dod="6/6", next_state="Agentreview")
        self.assertTrue(body.startswith("**Redacteur · Claude Sonnet 5 · run 3f9a2c"))
        self.assertIn("**Bewijs**", body)
        self.assertIn("**Definition of Done** 6/6", body)
        self.assertIn("**Volgende status** Agentreview", body)
        self.assertIn("```yaml", body)
        self.assertIn("run: 3f9a2c", body)

    def test_it_is_honest_when_there_is_no_evidence(self):
        body = comments.run_comment(
            role_title="Redacteur", model_display="Claude Sonnet 5", run=make_run(),
            body_md="Niets opgeleverd.", evidence=(), dod="-", next_state="Wacht op input")
        self.assertIn("geen — er is niets opgeleverd", body)


class GateCardTests(unittest.TestCase):
    def test_the_card_never_opens_a_gate_itself(self):
        card = a_gate_card()
        assert_not_gate_opening(card, author_is_agent=True)
        self.assertIsNone(parse_gate_decision(card))
        self.assertTrue(card.startswith("**Poortkaart merge · WV-207**"))

    def test_the_card_carries_the_two_answer_tokens_mid_body(self):
        card = a_gate_card()
        self.assertIn("\nAKKOORD\nAFGEKEURD: <reden>", card)
        self.assertIn("poort/akkoord", card)

    def test_a_high_risk_card_asks_for_the_longer_token_and_repeats_the_risk(self):
        card = a_gate_card(high_risk=True)
        self.assertIn("AKKOORD RISICO-GEZIEN", card)
        self.assertNotIn("\nAKKOORD\n", card)
        self.assertIn("Een kaal AKKOORD wordt geweigerd", card)
        assert_not_gate_opening(card, author_is_agent=True)

    def test_the_loose_risk_flags_stand_next_to_the_severity(self):
        card = a_gate_card(risk_flags=("risico-juridisch", "risico-publiek"))
        self.assertIn("**Risico** risico/midden · `risico-juridisch`, `risico-publiek`", card)
        assert_not_gate_opening(card, author_is_agent=True)

    def test_without_flags_the_risk_line_stays_as_it_was(self):
        self.assertIn("**Risico** risico/midden\n", a_gate_card())

    def test_the_card_has_the_sections_of_spec_7_3(self):
        card = a_gate_card()
        for heading in ("**Waar je ja tegen zegt**", "**Bewijs**", "**Acceptatiecriteria**",
                        "**Reviewers**", "**Oneens**", "**Risico**", "**Kosten tot nu**",
                        "**Hoe je antwoordt**"):
            self.assertIn(heading, card)
        self.assertIn("— Raderwerk · Spil (dispatcher) · run 3f9a2c", card)


class QaReportTests(unittest.TestCase):
    def test_it_follows_the_template_of_spec_5_9(self):
        report = comments.qa_report(
            model_display="Claude Opus 5", run_id="3f9a2c", when=WHEN, verdict="afkeuren",
            tested="WV-207 op de preview", criteria_rows=[("1", "Vier weken", "gehaald", "PR #7")],
            suite_ran=False, suite_output="npm run ci: niet gedraaid",
            findings_rows=[("blokkerend", "Testsuite niet gedraaid", "CI", "opnieuw draaien")],
            edge_cases="leeg: ok", not_verified="de gerenderde pagina",
            regression_risk="laag")
        self.assertIn("**Oordeel** afkeuren", report)
        self.assertIn("| # | Criterium | Uitkomst | Bewijs |", report)
        self.assertIn("volledig gedraaid: nee", report)
        self.assertIn("| Ernst | Bevinding | Waar | Voorstel |", report)
        self.assertIn("**Wat ik niet heb kunnen controleren**", report)
        assert_not_gate_opening(report, author_is_agent=True)


class OtherCommentTests(unittest.TestCase):
    def test_the_rejection_comment_quotes_the_reason(self):
        body = comments.rejection_comment(
            run_id="3f9a2c", when=WHEN, reason="AFGEKEURD\nGeen idempotentie.",
            actor_name="Youp", back_to="In uitvoering", attempt=1)
        self.assertIn("> AFGEKEURD", body)
        self.assertIn("> Geen idempotentie.", body)
        assert_not_gate_opening(body, author_is_agent=True)

    def test_the_stuck_comment_offers_exactly_three_choices(self):
        body = comments.stuck_comment(run_id="3f9a2c", when=WHEN, first_reason="a",
                                      second_reason="b")
        self.assertIn("geen derde poging", body)
        self.assertIn("1. de opdracht herschrijven", body)
        self.assertIn("3. het issue annuleren", body)

    def test_the_unconfirmed_comment_names_what_it_saw_and_why_it_refuses(self):
        body = comments.unconfirmed_comment(
            run_id="3f9a2c", when=WHEN, refusal="de actor is een app-account",
            source="comment", source_id="c-2", actor_name=None)
        self.assertIn("kanaal: comment", body)
        self.assertIn("registratie: `c-2`", body)
        self.assertIn("niet vast te stellen", body)
        self.assertIn("app-account", body)

    def test_every_rendered_comment_passes_the_agent_write_guard(self):
        bodies = [
            a_gate_card(), a_gate_card(high_risk=True),
            comments.claim_comment("3f9a2c", WHEN),
            comments.halt_comment("3f9a2c", WHEN, 2, 90, 1.25),
            comments.confirmation_comment(
                run_id="3f9a2c", when=WHEN, actor_name="Youp", decided_at=WHEN,
                source="comment", source_id="c-2", outcome="akkoord",
                next_state="Na-merge controle"),
        ]
        for body in bodies:
            assert_not_gate_opening(body, author_is_agent=True)


if __name__ == "__main__":
    unittest.main()
