"""De vijf schrijfsloten, de droogloop en het handelingenlogboek.

Elk slot heeft hier zijn eigen test: verwijder het slot uit `client.py` en er
valt precies één test om.
"""

import json
import unittest

from agency_os.linear import queries
from agency_os.linear.client import LinearClient, LinearError, WriteRefused

from tests.support_linear import RecordingSink


class StubClient(LinearClient):
    """De echte client met één antwoord in plaats van een netwerkverbinding."""

    def __init__(self, response=None, **kwargs):
        kwargs.setdefault("dispatcher_user_id", "user-spil")
        super().__init__("test-key", **kwargs)
        self.posted: list[tuple[str, dict]] = []
        self.response = response or {"data": {}}

    def _post(self, document, variables):
        self.posted.append((document, dict(variables)))
        return json.dumps(self.response)

    def label_ids(self):
        return {"run/bezet": "id-bezet", "run/klaar": "id-klaar",
                "poort/akkoord": "id-akkoord", "poort/afgekeurd": "id-afgekeurd",
                "schakelaar/pauze-alles": "id-stop"}

    def state_ids(self, team_key):
        return {"Agentreview": "id-agentreview", "Na-merge controle": "id-namerge"}


class GuardOneTests(unittest.TestCase):
    """Slot 1: een agent mag geen poortopenend comment schrijven."""

    def test_akkoord_on_the_first_line_is_refused(self):
        client = StubClient()
        with self.assertRaises(WriteRefused):
            client.create_comment("issue-207", "AKKOORD\n\nSpil zet dit door.", run_id="3f9a2c")
        self.assertEqual(client.posted, [])

    def test_afgekeurd_on_the_first_line_is_refused(self):
        client = StubClient()
        with self.assertRaises(WriteRefused):
            client.create_comment("issue-207", "AFGEKEURD: te duur", run_id="3f9a2c")

    def test_a_token_further_down_the_body_is_fine(self):
        client = StubClient({"data": {"commentCreate": {"comment": {"id": "c-9"}}}})
        body = "**Poortkaart**\n\nAntwoord met:\nAKKOORD\nAFGEKEURD: <reden>"
        self.assertEqual(client.create_comment("issue-207", body, run_id="3f9a2c"), "c-9")


class GuardTwoTests(unittest.TestCase):
    """Slot 2: de Spil zet nooit zelf een poortlabel."""

    def test_adding_poort_akkoord_is_refused(self):
        client = StubClient()
        with self.assertRaises(WriteRefused):
            client.update_issue("issue-207", run_id="3f9a2c", added_labels=["poort/akkoord"])
        self.assertEqual(client.posted, [])

    def test_adding_poort_afgekeurd_is_refused(self):
        client = StubClient()
        with self.assertRaises(WriteRefused):
            client.update_issue("issue-207", run_id="3f9a2c", added_labels=["poort/afgekeurd"])

    def test_removing_a_poort_label_is_allowed(self):
        client = StubClient({"data": {"issueUpdate": {"issue": {"id": "issue-207"}}}})
        client.update_issue("issue-207", run_id="3f9a2c", removed_labels=["poort/akkoord"])
        self.assertEqual(len(client.posted), 1)


class GuardThreeTests(unittest.TestCase):
    """Slot 3: `labelIds` bestaat nergens, want het wist de hele labelset."""

    def test_the_mutation_document_has_no_label_ids_field(self):
        self.assertNotIn("labelIds", queries.ISSUE_UPDATE)
        self.assertNotIn("labelIds:", queries.ISSUE_UPDATE)

    def test_no_query_document_mentions_label_ids(self):
        for name in dir(queries):
            document = getattr(queries, name)
            if isinstance(document, str) and name.isupper():
                self.assertNotIn("labelIds", document, name)

    def test_an_update_never_builds_a_label_ids_variable(self):
        client = StubClient({"data": {"issueUpdate": {"issue": {"id": "issue-207"}}}})
        client.update_issue("issue-207", run_id="3f9a2c", added_labels=["run/klaar"],
                            removed_labels=["run/bezet"])
        _, variables = client.posted[0]
        self.assertNotIn("labelIds", variables["input"])
        self.assertEqual(variables["input"]["addedLabelIds"], ["id-klaar"])
        self.assertEqual(variables["input"]["removedLabelIds"], ["id-bezet"])


class GuardFourTests(unittest.TestCase):
    """Slot 4: een poortstatus verlaten kan alleen met gate_ok=True."""

    def _client(self):
        return StubClient({"data": {
            "issue": {"id": "issue-207", "team": {"key": "WV"},
                      "state": {"name": "Poort · Merge of publicatie"}},
            "issueUpdate": {"issue": {"id": "issue-207"}},
        }})

    def test_leaving_a_gate_state_without_permission_is_refused(self):
        client = self._client()
        with self.assertRaises(WriteRefused) as caught:
            client.update_issue("issue-207", run_id="3f9a2c", state="Na-merge controle")
        self.assertIn("Poort", str(caught.exception))

    def test_leaving_a_gate_state_with_permission_is_allowed(self):
        client = self._client()
        client.update_issue("issue-207", run_id="3f9a2c", state="Na-merge controle",
                            gate_ok=True)
        self.assertTrue(any("IssueUpdate" in doc for doc, _ in client.posted))

    def test_a_non_gate_state_needs_no_permission(self):
        client = StubClient({"data": {
            "issue": {"id": "issue-207", "team": {"key": "WV"},
                      "state": {"name": "In uitvoering"}},
            "issueUpdate": {"issue": {"id": "issue-207"}},
        }})
        client.update_issue("issue-207", run_id="3f9a2c", state="Agentreview")
        self.assertTrue(any("IssueUpdate" in doc for doc, _ in client.posted))

    def test_a_label_only_update_inside_a_gate_needs_no_read(self):
        client = StubClient({"data": {"issueUpdate": {"issue": {"id": "issue-207"}}}})
        client.update_issue("issue-207", run_id="3f9a2c", added_labels=["run/klaar"])
        self.assertEqual(len(client.posted), 1)


class GuardFiveTests(unittest.TestCase):
    """Slot 5: de noodstop mag aan, nooit uit."""

    def test_removing_the_emergency_stop_is_refused(self):
        client = StubClient()
        with self.assertRaises(WriteRefused) as caught:
            client.update_issue("issue-156", run_id="3f9a2c",
                                removed_labels=["schakelaar/pauze-alles"])
        self.assertIn("pauze-alles", str(caught.exception))
        self.assertEqual(client.posted, [])

    def test_setting_the_emergency_stop_is_allowed(self):
        client = StubClient({"data": {"issueUpdate": {"issue": {"id": "issue-156"}}}})
        client.update_issue("issue-156", run_id="3f9a2c",
                            added_labels=["schakelaar/pauze-alles"])
        self.assertEqual(len(client.posted), 1)


class UnknownLabelTests(unittest.TestCase):
    def test_an_unknown_label_name_is_refused_instead_of_guessed(self):
        client = StubClient()
        with self.assertRaises(WriteRefused) as caught:
            client.update_issue("issue-207", run_id="3f9a2c", added_labels=["soort/verzonnen"])
        self.assertIn("canoniek", str(caught.exception))


class MutationLogTests(unittest.TestCase):
    def test_every_write_reaches_every_sink(self):
        sink = RecordingSink()
        client = StubClient({"data": {"issueUpdate": {"issue": {"id": "issue-207"}}}},
                            sinks=[sink])
        client.update_issue("issue-207", run_id="3f9a2c", added_labels=["run/klaar"],
                            removed_labels=["run/bezet"])
        self.assertEqual(len(sink.records), 1)
        record = sink.records[0]
        self.assertEqual(record.mutation, "issueUpdate")
        self.assertEqual(record.run_id, "3f9a2c")
        self.assertTrue(record.ok)
        self.assertFalse(record.dry_run)
        self.assertEqual(record.variables_summary["addedLabelIds"], ["run/klaar"])
        self.assertEqual(len(record.variables_digest), 64)

    def test_a_failed_write_is_logged_too(self):
        sink = RecordingSink()
        client = StubClient({"errors": [{"message": "boem"}]}, sinks=[sink])
        with self.assertRaises(LinearError):
            client.update_issue("issue-207", run_id="3f9a2c", added_labels=["run/klaar"])
        self.assertEqual(len(sink.records), 1)
        self.assertFalse(sink.records[0].ok)
        self.assertEqual(sink.records[0].error, "boem")

    def test_the_summary_is_an_allowlist_and_never_carries_a_body(self):
        sink = RecordingSink()
        client = StubClient({"data": {"commentCreate": {"comment": {"id": "c-1"}}}}, sinks=[sink])
        client.create_comment("issue-207", "geheime tekst", run_id="3f9a2c")
        summary = sink.records[0].variables_summary
        self.assertNotIn("body", summary)
        self.assertEqual(summary["body_len"], len("geheime tekst"))

    def test_a_description_is_summarised_by_length_only(self):
        sink = RecordingSink()
        client = StubClient({"data": {"issueUpdate": {"issue": {"id": "x"}}}}, sinks=[sink])
        client.update_issue("issue-207", run_id="3f9a2c", description="lange tekst")
        summary = sink.records[0].variables_summary
        self.assertNotIn("description", summary)
        self.assertEqual(summary["description_len"], len("lange tekst"))


class DryRunTests(unittest.TestCase):
    def test_no_http_happens_and_the_mutation_is_still_logged(self):
        sink = RecordingSink()
        client = StubClient(sinks=[sink], dry_run=True)
        result = client.update_issue("issue-207", run_id="3f9a2c", added_labels=["run/klaar"])
        self.assertIsNone(result)
        self.assertEqual(client.posted, [])
        self.assertEqual(len(sink.records), 1)
        self.assertTrue(sink.records[0].dry_run)
        self.assertTrue(sink.records[0].ok)

    def test_a_dry_run_comment_returns_a_synthetic_id(self):
        client = StubClient(dry_run=True)
        comment_id = client.create_comment("issue-207", "hallo", run_id="3f9a2c")
        self.assertTrue(comment_id.startswith("dry-3f9a2c-"))
        self.assertEqual(client.posted, [])

    def test_the_guards_still_fire_in_dry_run(self):
        client = StubClient(dry_run=True)
        with self.assertRaises(WriteRefused):
            client.create_comment("issue-207", "AKKOORD", run_id="3f9a2c")


class TransportTests(unittest.TestCase):
    def test_the_auth_header_carries_the_key_without_bearer(self):
        import inspect
        source = inspect.getsource(LinearClient._post)
        self.assertIn('"Authorization": self._api_key', source)
        self.assertNotIn("Bearer", source)

    def test_paginate_walks_every_page(self):
        pages = [
            {"data": {"issueLabels": {"nodes": [{"id": "1"}],
                                      "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}},
            {"data": {"issueLabels": {"nodes": [{"id": "2"}],
                                      "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ]

        class Paged(StubClient):
            def _post(self, document, variables):
                self.posted.append((document, dict(variables)))
                return json.dumps(pages[len(self.posted) - 1])

        client = Paged()
        nodes = client.paginate(queries.LABELS, "issueLabels")
        self.assertEqual([n["id"] for n in nodes], ["1", "2"])
        self.assertEqual(client.posted[1][1]["after"], "c1")

    def test_graphql_errors_become_a_linear_error_that_can_be_matched(self):
        client = StubClient({"errors": [{"message": "Entity not found",
                                         "extensions": {"code": "NOT_FOUND"}}]})
        with self.assertRaises(LinearError) as caught:
            client.query("query { viewer { id } }")
        self.assertTrue(caught.exception.matches("not_found"))
        self.assertFalse(caught.exception.matches("rate limit"))


class AgentSessionQueryTests(unittest.TestCase):
    """De vorm die de live API op 2026-09-03 werkelijk teruggeeft.

    `AgentActivity.content` is een union en `AgentSessionToPullRequest` draagt
    geen `url`. Een kale selectie op een van beide laat Linear het hele document
    weigeren, dus elke poll op een Codex- of Cursorsessie viel om met
    "must have a selection of subfields" en "Cannot query field url".
    """

    def test_the_query_selects_subfields_on_the_activity_union(self):
        self.assertIn("... on AgentActivityResponseContent", queries.AGENT_SESSIONS)
        self.assertIn("... on AgentActivityActionContent", queries.AGENT_SESSIONS)
        self.assertNotIn("nodes { id createdAt content }", queries.AGENT_SESSIONS)

    def test_the_query_reaches_the_pull_request_one_level_deeper(self):
        self.assertIn("pullRequests { nodes { pullRequest { url } } }", queries.AGENT_SESSIONS)

    def test_a_live_shaped_response_parses_into_a_session_view(self):
        node = {
            "id": "sess-9",
            "status": "complete",
            "summary": "Verwerk QA-bevindingen",
            "createdAt": "2026-09-03T11:10:40.623Z",
            "updatedAt": "2026-09-03T11:21:29.560Z",
            "appUser": {"id": "app-codex", "name": "Codex", "app": True},
            "activities": {"nodes": [
                {"id": "a-1", "createdAt": "2026-09-03T11:21:29.433Z",
                 "content": {"__typename": "AgentActivityResponseContent",
                             "type": "response", "body": "### Summary"}},
                {"id": "a-2", "createdAt": "2026-09-03T11:21:29.433Z",
                 "content": {"__typename": "AgentActivityActionContent",
                             "type": "action", "action": "open_pr",
                             "result": "https://github.com/raderwerk/raderwerk-content/pull/2"}},
            ]},
            "pullRequests": {"nodes": [
                {"pullRequest": {"url": "https://github.com/raderwerk/raderwerk-content/pull/2"}}]},
        }
        view = StubClient().to_session_view(node)
        self.assertEqual(view.app_user_name, "Codex")
        self.assertEqual(view.pull_request_url,
                         "https://github.com/raderwerk/raderwerk-content/pull/2")
        self.assertEqual([a.body for a in view.activities],
                         ["### Summary",
                          "https://github.com/raderwerk/raderwerk-content/pull/2"])

    def test_a_session_without_a_pull_request_stays_none(self):
        node = {"id": "s", "status": "active", "summary": None,
                "createdAt": "2026-09-03T11:10:40.623Z",
                "updatedAt": "2026-09-03T11:10:40.623Z",
                "appUser": {"id": "app-codex", "name": "Codex", "app": True},
                "activities": {"nodes": []}, "pullRequests": {"nodes": []}}
        self.assertIsNone(StubClient().to_session_view(node).pull_request_url)


if __name__ == "__main__":
    unittest.main()
