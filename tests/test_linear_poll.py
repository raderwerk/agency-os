"""De pollronde: één leesronde, en een indeling waarin niets stil verdwijnt."""

import unittest

from agency_os.linear.poll import PollConfig, poll

from tests.support_linear import FakeLinearClient, raw_issue

CFG = PollConfig(
    team_keys=("WV", "KR"),
    panel_identifier="WV-156",
    in_scope_states={
        "WV": ("Ingepland", "In uitvoering", "Agentreview", "QA op preview",
               "Na-merge controle"),
        "KR": ("Lead", "Discovery", "Voorstel"),
    },
    max_claims=4,
)


def node(identifier, state, *, id=None, labels=(), priority=0, updated="2026-09-03T08:00:00Z",
         team="WV", state_type="unstarted"):
    return raw_issue(
        id=id or f"issue-{identifier}", identifier=identifier, priority=priority,
        updatedAt=updated, team={"key": team},
        state={"id": f"s-{state}", "name": state, "type": state_type},
        labels={"nodes": [
            {"id": f"l-{name}", "name": name.split("/", 1)[-1] if "/" in name else name,
             "parent": {"name": name.split("/", 1)[0]} if "/" in name else None}
            for name in labels]},
    )


def run_poll(nodes, **kwargs):
    client = FakeLinearClient(raw_nodes=nodes, **kwargs)
    return client, poll(client, CFG)


class ClassificationTests(unittest.TestCase):
    def test_an_in_scope_issue_is_ready(self):
        _, result = run_poll([node("WV-207", "Ingepland")])
        self.assertEqual([i.identifier for i in result.ready], ["WV-207"])
        self.assertEqual(result.gates, ())
        self.assertEqual(result.skipped, ())

    def test_a_gate_state_goes_to_gates_and_never_to_ready(self):
        _, result = run_poll([node("WV-207", "Poort · Merge of publicatie",
                                   state_type="started")])
        self.assertEqual([i.identifier for i in result.gates], ["WV-207"])
        self.assertEqual(result.ready, ())

    def test_a_busy_issue_is_watched_not_claimed(self):
        _, result = run_poll([node("WV-207", "In uitvoering", labels=("run/bezet",))])
        self.assertEqual([i.identifier for i in result.watching], ["WV-207"])
        self.assertEqual(result.ready, ())

    def test_completed_and_cancelled_issues_are_ignored(self):
        _, result = run_poll([
            node("WV-1", "Klaar", state_type="completed"),
            node("WV-2", "Geannuleerd", state_type="canceled"),
        ])
        self.assertEqual(result.ready, ())
        self.assertEqual(result.skipped, ())

    def test_every_blocking_label_has_its_own_reason(self):
        nodes = [
            node("WV-1", "Ingepland", labels=("schakelaar/pauze",)),
            node("WV-2", "Ingepland", labels=("run/vastgelopen",)),
            node("WV-3", "Ingepland", labels=("run/onbevestigd",)),
            node("WV-4", "Ingepland", labels=("schakelaar/mens-vereist",)),
            node("WV-5", "Ingepland", labels=("agent/mens",)),
        ]
        _, result = run_poll(nodes)
        self.assertEqual(dict(result.skipped), {
            "WV-1": "gepauzeerd", "WV-2": "vastgelopen", "WV-3": "onbevestigd",
            "WV-4": "mens-vereist", "WV-5": "mensenwerk"})
        self.assertEqual(result.ready, ())

    def test_an_out_of_scope_state_is_skipped_as_buiten_mvp(self):
        _, result = run_poll([node("WV-9", "Binnen", state_type="triage"),
                              node("KR-3", "Kickoff", team="KR")])
        self.assertEqual(dict(result.skipped),
                         {"WV-9": "buiten-mvp", "KR-3": "buiten-mvp"})

    def test_the_panel_is_read_but_never_claimed(self):
        _, result = run_poll([node("WV-156", "Ingepland", id="issue-156"),
                              node("WV-207", "Ingepland")])
        self.assertIsNotNone(result.panel)
        self.assertEqual(result.panel.identifier, "WV-156")
        self.assertEqual([i.identifier for i in result.ready], ["WV-207"])

    def test_a_missing_panel_is_not_a_crash(self):
        _, result = run_poll([node("WV-207", "Ingepland")])
        self.assertIsNone(result.panel)


class OrderTests(unittest.TestCase):
    def test_priority_first_then_the_oldest_update(self):
        nodes = [
            node("WV-1", "Ingepland", priority=0, updated="2026-09-01T08:00:00Z"),
            node("WV-2", "Ingepland", priority=2, updated="2026-09-02T08:00:00Z"),
            node("WV-3", "Ingepland", priority=1, updated="2026-09-03T08:00:00Z"),
            node("WV-4", "Ingepland", priority=2, updated="2026-09-01T08:00:00Z"),
        ]
        _, result = run_poll(nodes)
        self.assertEqual([i.identifier for i in result.ready],
                         ["WV-3", "WV-4", "WV-2", "WV-1"])


class SwitchTests(unittest.TestCase):
    def test_the_switch_state_travels_with_the_poll_result(self):
        nodes = [node("WV-156", "Ingepland", id="issue-156",
                      labels=("schakelaar/pauze-alles",))]
        _, result = run_poll(nodes)
        self.assertTrue(result.switches.global_pause)

    def test_restrict_leaves_only_incidents_claimable(self):
        nodes = [node("WV-1", "Ingepland", labels=("soort/incident",)),
                 node("WV-2", "Ingepland", labels=("soort/feature",))]
        _, result = run_poll(nodes, issue_count=221)
        self.assertEqual([i.identifier for i in result.ready], ["WV-1"])
        self.assertEqual(dict(result.skipped), {"WV-2": "issuebudget-restrict"})


if __name__ == "__main__":
    unittest.main()
