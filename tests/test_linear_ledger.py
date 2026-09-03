"""Het kostenboek: staartblok heen en weer, sommen die kloppen, eerlijke tekst."""

import unittest
from datetime import date, timedelta

from agency_os.linear import ledger
from agency_os.linear.models import Artifact
from agency_os.linear.store import Store

from tests.support_linear import T0, make_run

PRICES = (
    ledger.PriceRow("claude-opus-5", 5.0, 25.0, 0.5),
    ledger.PriceRow("claude-sonnet-5", 2.0, 10.0, 0.2),
)
FX = ledger.FxRate(0.862, "ECB", date(2026, 9, 3))
DAY = T0.date()


class TailBlockTests(unittest.TestCase):
    def test_the_block_carries_every_field_of_spec_8_3(self):
        block = ledger.render_tail_block(make_run())
        for key in ("run:", "rol:", "model:", "issue:", "gestart:", "geeindigd:", "duur_s:",
                    "kosten_usd:", "kosten_eur:", "tokens_in:", "tokens_uit:", "cache_lees:",
                    "beurten:", "dod:", "uitkomst:", "volgende_status:", "artefacten:"):
            self.assertIn(key, block)
        self.assertTrue(block.startswith("```yaml"))
        self.assertTrue(block.rstrip().endswith("```"))

    def test_the_one_extra_key_is_gemeten(self):
        self.assertIn("gemeten: true", ledger.render_tail_block(make_run()))
        self.assertIn("gemeten: false",
                      ledger.render_tail_block(make_run(metered=False, executor="native-codex")))

    def test_round_trip_is_lossless_for_everything_the_block_carries(self):
        original = make_run(issue_id="", executor="", klant=None, dienst=None)
        parsed = ledger.parse_tail_block(ledger.render_tail_block(original))
        self.assertEqual(parsed, original)

    def test_round_trip_of_the_rendering_is_stable(self):
        block = ledger.render_tail_block(make_run())
        again = ledger.render_tail_block(ledger.parse_tail_block(block))
        self.assertEqual(block, again)

    def test_the_team_key_is_derived_from_the_identifier(self):
        parsed = ledger.parse_tail_block(ledger.render_tail_block(make_run()))
        self.assertEqual(parsed.team_key, "WV")
        self.assertEqual(parsed.issue_identifier, "WV-207")

    def test_the_pr_url_is_derived_from_the_artefacts(self):
        parsed = ledger.parse_tail_block(ledger.render_tail_block(make_run()))
        self.assertEqual(parsed.pr_url,
                         "https://github.com/raderwerk/raderwerk-content/pull/7")

    def test_several_artefacts_survive_the_round_trip(self):
        run = make_run(artefacten=(
            Artifact("pr", "https://github.com/raderwerk/x/pull/7", "PR #7"),
            Artifact("test", "https://github.com/raderwerk/x/actions/runs/1", "ci groen"),
            Artifact("preview", "https://preview.example", ""),
        ), pr_url="https://github.com/raderwerk/x/pull/7", issue_id="", executor="",
            klant=None, dienst=None)
        self.assertEqual(ledger.parse_tail_block(ledger.render_tail_block(run)), run)

    def test_a_run_without_artefacts_round_trips(self):
        run = make_run(artefacten=(), pr_url=None, issue_id="", executor="", klant=None,
                       dienst=None)
        self.assertEqual(ledger.parse_tail_block(ledger.render_tail_block(run)), run)

    def test_the_block_is_found_at_the_end_of_a_full_comment(self):
        body = ("**Redacteur · Claude Sonnet 5 · run 3f9a2c · 2026-09-03 11:14**\n\n"
                "Ik heb het bouwlogboek toegevoegd.\n\n"
                "```json\n{\"iets\": \"anders\"}\n```\n\n"
                + ledger.render_tail_block(make_run()))
        parsed = ledger.parse_tail_block(body)
        self.assertEqual(parsed.run_id, "3f9a2c")

    def test_a_comment_without_a_block_returns_none(self):
        self.assertIsNone(ledger.parse_tail_block("gewoon proza"))
        self.assertIsNone(ledger.parse_tail_block(""))
        self.assertIsNone(ledger.parse_tail_block("```\nniets\n```"))


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_the_rollup_sums_equal_the_row_sums(self):
        runs = [
            make_run(run_id="aaaaaa", kosten_usd=4.21, kosten_eur=3.63, rol="redacteur"),
            make_run(run_id="bbbbbb", kosten_usd=1.10, kosten_eur=0.95, rol="reviewer",
                     issue_id="issue-9", issue_identifier="WV-9"),
            make_run(run_id="cccccc", kosten_usd=0.40, kosten_eur=0.34, rol="reviewer",
                     klant="zoutkaap"),
        ]
        for run in runs:
            ledger.record_run(self.store, run)
        day = ledger.rollup(self.store, DAY)
        self.assertEqual(day.runs, 3)
        self.assertEqual(day.issues, 2)
        self.assertAlmostEqual(day.usd, sum(r.kosten_usd for r in runs))
        self.assertAlmostEqual(day.eur, sum(r.kosten_eur for r in runs))
        self.assertAlmostEqual(sum(day.by_role.values()), day.eur)
        self.assertAlmostEqual(sum(day.by_klant.values()), day.eur)
        self.assertAlmostEqual(day.by_role["reviewer"], 0.95 + 0.34)

    def test_failed_runs_are_in_the_book_too(self):
        ledger.record_run(self.store, make_run(run_id="dddddd", uitkomst="mislukt",
                                               kosten_eur=0.12, volgende_status=None))
        self.assertEqual(ledger.rollup(self.store, DAY).runs, 1)

    def test_gate_events_feed_supervision_and_first_pass(self):
        ledger.record_run(self.store, make_run(volgende_status="Klaar"))
        event = self.store.record_gate_event(
            issue_id="issue-207", gate_state="Poort · Merge of publicatie",
            card_comment_id="c-card", card_at=T0, decided_at=T0 + timedelta(minutes=22),
            outcome="akkoord", token="AKKOORD", source="comment", source_id="c-2",
            actor_id="user-mens", actor_name="Youp", valid=True, refusal=None)
        self.store.mark_gate_applied(event, T0 + timedelta(minutes=22))
        day = ledger.rollup(self.store, DAY)
        self.assertEqual(day.gates_passed, 1)
        self.assertEqual(day.gates_rejected, 0)
        self.assertAlmostEqual(day.median_gate_wait_s, 22 * 60)
        self.assertAlmostEqual(day.supervision_minutes, 22.0)
        self.assertEqual(day.first_pass_ok, (1, 1))

    def test_a_rejected_issue_does_not_count_as_first_pass_ok(self):
        ledger.record_run(self.store, make_run(volgende_status="Klaar"))
        event = self.store.record_gate_event(
            issue_id="issue-207", gate_state="Poort · Merge of publicatie",
            card_comment_id="c", card_at=T0, decided_at=T0 + timedelta(minutes=5),
            outcome="afgekeurd", token="AFGEKEURD", source="comment", source_id="c-2",
            actor_id="user-mens", actor_name="Youp", valid=True, refusal=None)
        self.store.mark_gate_applied(event, T0 + timedelta(minutes=5))
        day = ledger.rollup(self.store, DAY)
        self.assertEqual(day.gates_rejected, 1)
        self.assertEqual(day.first_pass_ok, (0, 1))

    def test_loops_and_issue_count_come_from_the_store(self):
        self.store.set_meta("issue_count", "212")
        self.store.bump_role_run("issue-207", "redacteur", DAY)
        self.store.bump_role_run("issue-207", "redacteur", DAY)
        day = ledger.rollup(self.store, DAY)
        self.assertEqual(day.issue_count, 212)
        self.assertEqual(day.loops, 1)

    def test_an_empty_day_is_all_zeroes_and_not_a_crash(self):
        day = ledger.rollup(self.store, date(2026, 1, 1))
        self.assertEqual((day.runs, day.issues, day.eur), (0, 0, 0))
        self.assertIsNone(day.median_gate_wait_s)


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        ledger.record_run(self.store, make_run())
        ledger.record_run(self.store, make_run(run_id="eeeeee", executor="native-codex",
                                               model="codex-gpt-5.6", metered=False,
                                               kosten_usd=0.0, kosten_eur=0.0))

    def _render(self):
        return ledger.render_markdown(self.store, since=DAY, until=DAY, prices=PRICES, fx=FX)

    def test_it_has_the_three_sections(self):
        text = self._render()
        self.assertIn("## 1. Koersen en aannames", text)
        self.assertIn("## 2. Runregels", text)
        self.assertIn("## 3. Dagafsluiting", text)

    def test_it_states_its_own_incompleteness(self):
        self.assertIn(ledger.INCOMPLETENESS, self._render())
        self.assertIn(ledger.ESTIMATE_NOTICE, self._render())

    def test_the_fx_rate_comes_from_the_configuration_and_is_never_hardcoded(self):
        text = self._render()
        self.assertIn("0.862", text)
        self.assertIn("ECB", text)
        self.assertIn("2026-09-03", text)

    def test_unmetered_rows_say_so_per_row(self):
        rows = [line for line in self._render().splitlines() if "codex-gpt-5.6" in line]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].rstrip().endswith("| nee |"))

    def test_the_daily_close_has_the_two_numbers_that_matter(self):
        text = self._render()
        self.assertIn("supervisie:", text)
        self.assertIn("eerste-keer-goed:", text)
        self.assertIn("issueteller:", text)


if __name__ == "__main__":
    unittest.main()
