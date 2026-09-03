"""De noodrem en de budgetwacht: binnen één cyclus stil, en nooit zelf weer aan."""

import unittest

from agency_os.linear import killswitch
from agency_os.linear.client import WriteRefused
from agency_os.linear.store import Store

from tests.support_linear import T0, FakeLinearClient, make_issue

BUDGET = (200, 220, 225)


def panel(**overrides):
    base = dict(id="issue-156", identifier="WV-156", title="Bedieningspaneel",
                state_name="Ingepland", labels=())
    base.update(overrides)
    return make_issue(**base)


class ReadSwitchesTests(unittest.TestCase):
    def test_a_clean_panel_is_all_clear(self):
        state = killswitch.read_switches(None, panel(), [], issue_count=84, thresholds=BUDGET)
        self.assertFalse(state.global_pause)
        self.assertFalse(state.engine_dead)
        self.assertEqual(state.budget_level, "ok")
        self.assertIsNone(state.reason)
        self.assertEqual(state.paused_issue_ids, frozenset())

    def test_pauze_alles_on_the_panel_is_a_global_pause(self):
        state = killswitch.read_switches(
            None, panel(labels=("schakelaar/pauze-alles",)), [], issue_count=84,
            thresholds=BUDGET)
        self.assertTrue(state.global_pause)
        self.assertIn("pauze-alles", state.reason)

    def test_pauze_on_an_issue_pauses_only_that_issue(self):
        paused = make_issue(id="issue-9", labels=("schakelaar/pauze",))
        other = make_issue(id="issue-8", labels=("soort/feature",))
        state = killswitch.read_switches(None, panel(), [paused, other], issue_count=84,
                                         thresholds=BUDGET)
        self.assertEqual(state.paused_issue_ids, frozenset({"issue-9"}))
        self.assertFalse(state.global_pause)

    def test_motor_dood_is_informational_and_not_a_pause(self):
        state = killswitch.read_switches(None, panel(labels=("schakelaar/motor-dood",)), [],
                                         issue_count=84, thresholds=BUDGET)
        self.assertTrue(state.engine_dead)
        self.assertFalse(state.global_pause)

    def test_the_three_budget_thresholds(self):
        for count, level in ((199, "ok"), (200, "warn"), (219, "warn"), (220, "restrict"),
                             (224, "restrict"), (225, "stop"), (400, "stop")):
            state = killswitch.read_switches(None, panel(), [], issue_count=count,
                                             thresholds=BUDGET)
            self.assertEqual(state.budget_level, level, count)
            self.assertEqual(state.issue_count, count)

    def test_a_budget_level_names_its_reason(self):
        state = killswitch.read_switches(None, panel(), [], issue_count=226, thresholds=BUDGET)
        self.assertIn("226", state.reason)


class HaltTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.issue = make_issue(labels=("run/bezet",))
        self.client = FakeLinearClient([self.issue, panel(labels=("schakelaar/pauze-alles",))])
        self.store.insert_claim("issue-207", "3f9a2c", "WV-207", T0)

    def _switches(self):
        return killswitch.read_switches(
            self.client, panel(labels=("schakelaar/pauze-alles",)), [], issue_count=84,
            thresholds=BUDGET)

    def test_every_open_claim_goes_back_to_the_queue_in_one_pass(self):
        aborted = killswitch.halt_everything(self.client, self.store, self._switches(),
                                             run_id="3f9a2c")
        self.assertEqual(aborted, 1)
        self.assertEqual(self.store.open_claims(), [])
        update = [m for m in self.client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(update.variables_summary["addedLabelIds"], ["run/wachtrij"])
        self.assertEqual(update.variables_summary["removedLabelIds"], ["run/bezet"])

    def test_one_abort_comment_per_touched_issue(self):
        killswitch.halt_everything(self.client, self.store, self._switches(), run_id="3f9a2c")
        comments = [m for m in self.client.mutations if m.mutation == "commentCreate"]
        self.assertEqual(len(comments), 1)
        self.assertIn("afgebroken", self.client.comments("issue-207")[-1].body)

    def test_without_a_global_pause_nothing_happens(self):
        state = killswitch.read_switches(self.client, panel(), [], issue_count=84,
                                         thresholds=BUDGET)
        self.assertEqual(killswitch.halt_everything(self.client, self.store, state,
                                                    run_id="3f9a2c"), 0)
        self.assertEqual(self.client.mutations, [])

    def test_the_halt_comment_reports_what_it_stopped(self):
        from agency_os.linear import comments as rendering
        body = rendering.halt_comment("3f9a2c", T0, aborted=3, elapsed_s=120, cost_eur=1.5)
        self.assertIn("3 lopende run", body)
        self.assertIn("€ 1,50", body)


class EmergencyStopTests(unittest.TestCase):
    def test_tripping_the_stop_sets_the_label_and_writes_one_comment(self):
        client = FakeLinearClient([panel()])
        killswitch.trip_emergency_stop(client, panel(), "vijf mislukte runs", run_id="3f9a2c")
        update = [m for m in client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(update.variables_summary["addedLabelIds"], ["schakelaar/pauze-alles"])
        self.assertEqual(len([m for m in client.mutations if m.mutation == "commentCreate"]), 1)
        self.assertIn("vijf mislukte runs", client.comments("issue-156")[-1].body)

    def test_tripping_twice_does_not_set_the_label_again(self):
        already = panel(labels=("schakelaar/pauze-alles",))
        client = FakeLinearClient([already])
        killswitch.trip_emergency_stop(client, already, "nog een keer", run_id="3f9a2c")
        self.assertEqual([m for m in client.mutations if m.mutation == "issueUpdate"], [])

    def test_the_stop_can_never_be_removed_by_the_machine(self):
        client = FakeLinearClient([panel(labels=("schakelaar/pauze-alles",))])
        with self.assertRaises(WriteRefused):
            client.update_issue("issue-156", run_id="3f9a2c",
                                removed_labels=["schakelaar/pauze-alles"])
        self.assertEqual(client.mutations, [])

    def test_no_function_in_the_module_removes_the_stop(self):
        import inspect
        source = inspect.getsource(killswitch)
        self.assertNotIn("removed_labels=[GLOBAL_PAUSE_LABEL]", source)
        self.assertNotIn('removed_labels=["schakelaar/pauze-alles"]', source)


if __name__ == "__main__":
    unittest.main()
