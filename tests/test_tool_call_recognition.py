"""Regressions for the two defects that made on_demand mode unusable.

Both were found against real session transcripts: the classifier ignored the
most common way to ask for code, and a bare JSON tool call — the form several
providers emit — was printed as prose instead of being executed.
"""

from __future__ import annotations

import unittest

from aicoder.executor import is_tool_relevant_message, parse_tool_calls, should_load_tools


class AuthoringVerbsAreToolRelevantTests(unittest.TestCase):
    """"schreib mir X" must not start a session with an empty tool catalogue."""

    AUTHORING = (
        "schreib mir ein Textadventure",
        "baue mir einen Taschenrechner",
        "mach ein Snake in Python",
        "implementiere Tetris",
        "programmiere einen Webserver",
        "entwickle ein Plugin",
        "generiere eine Konfigurationsdatei",
        "leg das Modul an",
        "richte einen Linter ein",
        "write me a tic tac toe game",
        "build a REST API",
        "make a CLI wrapper",
        "implement the parser",
        "add a dark mode",
        "generate the migration",
    )

    def test_authoring_requests_load_tools(self):
        for prompt in self.AUTHORING:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_tool_relevant_message(prompt))
                self.assertTrue(should_load_tools("on_demand", prompt))

    def test_maintenance_verbs_still_recognized(self):
        for prompt in ("sortiere die Imports", "prüfe die Konfiguration",
                       "fix the failing test", "installiere pytest"):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_tool_relevant_message(prompt))

    def test_small_talk_and_concepts_still_skip_tools(self):
        # The whole point of on_demand: a greeting must not drag in the catalogue.
        for prompt in ("Hallo", "Hi", "danke", "guten morgen",
                       "Was ist Dependency Injection?",
                       "Erkläre mir den Unterschied zwischen Klasse und Funktion.",
                       "Why is immutability useful?"):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_tool_relevant_message(prompt))
                self.assertFalse(should_load_tools("on_demand", prompt))

    def test_tool_mode_off_still_wins(self):
        self.assertFalse(should_load_tools("off", "implementiere Tetris"))


class BareJsonToolCallTests(unittest.TestCase):
    """Providers that emit a naked JSON object must still be executable."""

    def test_bare_object_is_recognized(self):
        text = ('{"name": "file_edit", "arguments": {"path": "games/x.py", '
                '"operation": "create", "content": "print(1)"}}')
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "file_edit")

    def test_bare_object_with_id_and_raw_type(self):
        text = ('{"name": "file_tree", "arguments": {"path": "."}, '
                '"id": "call-3c2d", "raw_type": "function"}')
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "file_tree")

    def test_bare_list_is_recognized(self):
        calls = parse_tool_calls('[{"name":"file_read","arguments":{"path":"a.py"}}]')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "file_read")

    def test_existing_envelopes_still_work(self):
        for text in (
            '<tool_call>{"name":"file_edit","arguments":{"path":"a.py"}}</tool_call>',
            '```json\n{"name":"file_edit","arguments":{"path":"a.py"}}\n```',
        ):
            with self.subTest(text=text[:20]):
                self.assertEqual(len(parse_tool_calls(text)), 1)

    def test_json_quoted_inside_prose_stays_inert(self):
        # The documented guarantee: an example in an explanation is never run.
        text = ('Ein Tool-Call sieht so aus:\n'
                '{"name":"file_edit","arguments":{"path":"a.py"}}\n'
                'Alles klar?')
        self.assertEqual(parse_tool_calls(text), [])

    def test_plain_json_data_is_not_a_tool_call(self):
        for text in ('{"path": "a.py", "operation": "create"}',
                     '{"host": "localhost", "port": 9000, "debug": true}',
                     '[1, 2, 3]'):
            with self.subTest(text=text):
                self.assertEqual(parse_tool_calls(text), [])

    def test_empty_and_garbage_input(self):
        for text in ("", "   ", "kein JSON", "{kaputt"):
            with self.subTest(text=text):
                self.assertEqual(parse_tool_calls(text), [])

    def test_protocol_v2_parses_flat_argument_json(self):
        text = 'TOOL_CALL file_read\n{"path":"README.md"}\nEND_TOOL_CALL'
        self.assertEqual(parse_tool_calls(text), [{"name":"file_read","arguments":{"path":"README.md"}}])

    def test_protocol_v2_rejects_malformed_json_without_repair(self):
        text = 'TOOL_CALL file_read\n{"path":"README.md"\nEND_TOOL_CALL'
        self.assertEqual(parse_tool_calls(text), [])

    def test_retry_and_resume_are_short_confirmations(self):
        from aicoder.executor import is_short_confirmation
        for value in ("retry", "resume", "go on", "continue", "weiter"):
            with self.subTest(value=value):
                self.assertTrue(is_short_confirmation(value))


if __name__ == "__main__":
    unittest.main()
