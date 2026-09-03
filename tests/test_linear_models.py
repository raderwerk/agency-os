"""Canonieke labelnamen en het lezen van het opdrachtcontract."""

import unittest
from datetime import datetime, timezone

from agency_os.linear.models import Contract, canonical_label_name, issue_from_node

from tests.support_linear import make_issue, raw_issue



class CanonicalLabelTests(unittest.TestCase):
    def test_leaf_plus_parent_becomes_the_canonical_name(self):
        self.assertEqual(canonical_label_name("contentstuk", "soort"), "soort/contentstuk")

    def test_ungrouped_label_keeps_its_bare_name(self):
        self.assertEqual(canonical_label_name("risico-publiek", None), "risico-publiek")

    def test_repo_label_has_two_slashes(self):
        self.assertEqual(
            canonical_label_name("raderwerk/raderwerk-content", "repo"),
            "repo/raderwerk/raderwerk-content",
        )

    def test_issue_view_builds_canonical_names_from_leaf_and_parent(self):
        issue = issue_from_node(raw_issue())
        self.assertEqual(issue.labels, (
            "agent/sonnet", "dienst/content", "klant/raderwerk",
            "repo/raderwerk/raderwerk-content", "risico-publiek", "soort/contentstuk",
        ))
        self.assertEqual(issue.label_ids["soort/contentstuk"], "l2")

    def test_label_in_group_splits_only_on_the_first_slash(self):
        issue = issue_from_node(raw_issue())
        self.assertEqual(issue.label_in_group("repo"), "raderwerk/raderwerk-content")
        self.assertEqual(issue.repo, "raderwerk/raderwerk-content")
        self.assertIsNone(issue.label_in_group("poort"))


class DerivedPropertyTests(unittest.TestCase):
    def test_the_wv_207_shape(self):
        issue = issue_from_node(raw_issue())
        self.assertEqual(issue.dienst, "content")
        self.assertEqual(issue.soort, "contentstuk")
        self.assertEqual(issue.klant, "raderwerk")
        self.assertEqual(issue.agent_hint, "sonnet")
        self.assertIsNone(issue.run_state)
        self.assertEqual(issue.flags, frozenset({"risico-publiek"}))
        self.assertFalse(issue.is_gate_state)

    def test_risico_defaults_to_laag_when_the_label_is_absent(self):
        issue = issue_from_node(raw_issue())
        self.assertEqual(issue.risico, "laag")
        self.assertFalse(issue.high_risk)

    def test_high_risk_reads_the_label(self):
        issue = make_issue(labels=("risico/hoog",))
        self.assertEqual(issue.risico, "hoog")
        self.assertTrue(issue.high_risk)

    def test_gate_state_is_any_state_starting_with_poort(self):
        for name in ("Poort · Merge of publicatie", "Poort 1 · Voorstel akkoord",
                     "Poort 3 · Factuur akkoord"):
            self.assertTrue(make_issue(state_name=name).is_gate_state, name)
        self.assertFalse(make_issue(state_name="Ingepland").is_gate_state)

    def test_repo_falls_back_to_the_contract(self):
        issue = make_issue(labels=("soort/contentstuk",))
        self.assertEqual(issue.repo, "raderwerk/raderwerk-content")

    def test_klant_falls_back_to_the_contract_but_never_to_geen(self):
        issue = make_issue(labels=("soort/bureau",))
        self.assertEqual(issue.klant, "raderwerk")
        bureau = Contract.parse("## Opdrachtcontract\n```yaml\ncontract: v1\nklant: geen\n```")
        self.assertIsNone(make_issue(labels=(), contract=bureau).klant)

    def test_updated_at_is_timezone_aware_utc(self):
        issue = issue_from_node(raw_issue())
        self.assertEqual(issue.updated_at.tzinfo, timezone.utc)
        self.assertEqual(issue.updated_at, datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc))


class ContractParseTests(unittest.TestCase):
    def test_missing_block_returns_none(self):
        self.assertIsNone(Contract.parse("Gewoon een omschrijving zonder contract."))
        self.assertIsNone(Contract.parse(""))
        self.assertIsNone(Contract.parse(None))

    def test_heading_without_a_fenced_block_returns_none(self):
        self.assertIsNone(Contract.parse("## Opdrachtcontract\n\nnog niet ingevuld"))

    def test_feature_template(self):
        contract = Contract.parse(
            "## Opdrachtcontract\n```yaml\ncontract: v1\nklant:\nrepo:\n"
            "basisbranch: main\nomgeving: preview\npubliek: false\n```")
        self.assertEqual(contract.version, "v1")
        self.assertIsNone(contract.klant)
        self.assertIsNone(contract.repo)
        self.assertEqual(contract.basisbranch, "main")
        self.assertEqual(contract.omgeving, "preview")
        self.assertFalse(contract.publiek)
        self.assertEqual(contract.bronnen, ())

    def test_contentstuk_template_keeps_its_unknown_key(self):
        contract = Contract.parse(
            "## Opdrachtcontract\n```yaml\ncontract: v1\nklant:\nrepo:\npubliek: true\n"
            "eindredacteur:\n```")
        self.assertTrue(contract.publiek)
        self.assertIn("eindredacteur", contract.unknown_keys)

    def test_campagne_template_keeps_two_unknown_keys(self):
        contract = Contract.parse(
            "## Opdrachtcontract\n```yaml\ncontract: v1\nklant:\nkanaal:\n"
            "budget_kader_eur:\npubliek: true\n```")
        self.assertEqual(contract.unknown_keys, ("budget_kader_eur", "kanaal"))

    def test_incident_template_reads_the_verboden_list(self):
        contract = Contract.parse(
            "## Opdrachtcontract\n```yaml\ncontract: v1\nklant:\nrepo:\npubliek: false\n"
            "verboden:\n  - terugrollen\n  - deployen\n  - klant informeren\n```")
        self.assertEqual(contract.verboden, ("terugrollen", "deployen", "klant informeren"))

    def test_bureau_template(self):
        contract = Contract.parse(
            "## Opdrachtcontract\n```yaml\ncontract: v1\nklant: geen\n"
            "repo: raderwerk/agency-os\nbasisbranch: main\nomgeving: geen\npubliek: false\n```")
        self.assertEqual(contract.klant, "geen")
        self.assertEqual(contract.repo, "raderwerk/agency-os")
        self.assertEqual(contract.omgeving, "geen")

    def test_full_wv_207_template_with_bronnen_and_verboden(self):
        contract = Contract.parse(
            "## Doel\ntekst\n\n## Opdrachtcontract\n```yaml\ncontract: v1\nklant: zoutkaap\n"
            "repo: raderwerk/zoutkaap-shop\nbasisbranch: main\nomgeving: preview\n"
            "publiek: false\nbronnen:\n  - [Klantdossier](https://linear.app/x)\n"
            "verboden:\n  - mergen\n  - deployen naar productie\n```")
        self.assertEqual(contract.bronnen, ("[Klantdossier](https://linear.app/x)",))
        self.assertEqual(contract.verboden, ("mergen", "deployen naar productie"))
        self.assertEqual(contract.unknown_keys, ())
        self.assertIn("klant: zoutkaap", contract.raw)

    def test_defaults_when_keys_are_absent(self):
        contract = Contract.parse("## Opdrachtcontract\n```yaml\ncontract: v1\n```")
        self.assertEqual(contract.basisbranch, "main")
        self.assertEqual(contract.omgeving, "geen")
        self.assertFalse(contract.publiek)


if __name__ == "__main__":
    unittest.main()
