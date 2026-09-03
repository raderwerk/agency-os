"""De runprompt: wat er in staat, en wat er uit het issue wordt gelezen."""

from __future__ import annotations

import unittest

from tests.fakes import make_issue

from agency_os.app.prompts import OUTPUT_CONTRACT, acceptance_criteria, build_prompt, dod_items
from agency_os.app.routing import load_table

TABLE = load_table()

DESCRIPTION = """## Opdracht

Schrijf iets.

## Acceptatiecriteria

- Eerste criterium
2. Tweede criterium

### Definition of Done

- [ ] Artefact staat in de repo
- [x] Test is groen
"""


class Cfg:
    class executors:
        repo_root = "/bestaat/niet"


class SectionTest(unittest.TestCase):
    def test_criteria_and_dod_are_read_from_the_description(self):
        self.assertEqual(["Eerste criterium", "Tweede criterium"], acceptance_criteria(DESCRIPTION))
        self.assertEqual(["Artefact staat in de repo", "Test is groen"], dod_items(DESCRIPTION))

    def test_a_missing_section_is_empty_not_an_error(self):
        self.assertEqual([], acceptance_criteria("## Iets anders\n\n- regel"))
        self.assertEqual([], dod_items(""))

    def test_items_stop_at_the_next_heading(self):
        text = "## Acceptatiecriteria\n\n- een\n\n## Kaders\n\n- niet dit\n"
        self.assertEqual(["een"], acceptance_criteria(text))


class BuildPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = make_issue()
        self.role = TABLE.roles["redacteur"]

    def test_the_prompt_has_the_rules_the_role_the_issue_and_the_contract(self):
        prompt = build_prompt(Cfg(), self.role, self.issue, run_id="a1b2c3")

        self.assertLess(prompt.index("Onwrikbare regels"), prompt.index("# Redacteur"),
                        "de regels staan vóór de rol, en de rol vóór het issue")
        self.assertLess(prompt.index("# Redacteur"), prompt.index("WV-207"))
        self.assertIn("repo: raderwerk/raderwerk-content", prompt, "het opdrachtcontract gaat mee")
        self.assertIn("Vier bestanden onder", prompt, "de acceptatiecriteria staan er los in")
        self.assertIn(OUTPUT_CONTRACT.strip(), prompt)
        self.assertIn("Volgende status bij uitkomst klaar: Agentreview", prompt)

    def test_a_missing_repo_checkout_says_so_instead_of_being_silent(self):
        prompt = build_prompt(Cfg(), self.role, self.issue, run_id="a1b2c3")
        self.assertIn("Niet gevonden op", prompt)
        self.assertIn("AGENTS.md", prompt)

    def test_extra_context_is_appended_as_its_own_block(self):
        prompt = build_prompt(
            Cfg(), self.role, self.issue, run_id="a1b2c3",
            extra_context={"agents_md": "Regels van de repo", "Afkeurreden": "te lang"},
        )
        self.assertIn("Regels van de repo", prompt)
        self.assertIn("## Afkeurreden", prompt)
        self.assertIn("te lang", prompt)

    def test_an_issue_without_criteria_gets_told_to_ask(self):
        prompt = build_prompt(Cfg(), self.role, make_issue(description="Doe iets", contract=None), run_id="a1b2c3")
        self.assertIn("Vraag ernaar", prompt)


if __name__ == "__main__":
    unittest.main()
