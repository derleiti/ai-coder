from __future__ import annotations

import unittest

from aicoder.executor import parse_tool_calls, strip_tool_calls


class ProtocolV2BatchTests(unittest.TestCase):
    def test_multiple_complete_blocks_are_parsed_in_order(self):
        text = (
            'TOOL_CALL file_read\n{"path":"a.txt"}\nEND_TOOL_CALL\n\n'
            'TOOL_CALL file_read\n{"path":"b.txt"}\nEND_TOOL_CALL'
        )
        self.assertEqual(
            parse_tool_calls(text),
            [
                {"name": "file_read", "arguments": {"path": "a.txt"}},
                {"name": "file_read", "arguments": {"path": "b.txt"}},
            ],
        )
        self.assertEqual(strip_tool_calls(text), "")

    def test_surrounding_prose_is_preserved_as_thought_while_calls_execute(self):
        text = (
            'I will inspect both files.\n'
            'TOOL_CALL file_read\n{"path":"a.txt"}\nEND_TOOL_CALL\n'
            'now the next one\n'
            'TOOL_CALL file_read\n{"path":"b.txt"}\nEND_TOOL_CALL\n'
            'Then I will compare the results.'
        )
        self.assertEqual(
            [call["name"] for call in parse_tool_calls(text)],
            ["file_read", "file_read"],
        )
        visible = strip_tool_calls(text)
        self.assertIn("inspect both files", visible)
        self.assertIn("now the next one", visible)
        self.assertIn("compare the results", visible)

    def test_fenced_tool_example_stays_inert(self):
        text = 'Example only:\n```\nTOOL_CALL file_read\n{"path":"secret.txt"}\nEND_TOOL_CALL\n```'
        self.assertEqual(parse_tool_calls(text), [])

    def test_malformed_second_block_rejects_whole_sequence(self):
        text = (
            'TOOL_CALL file_read\n{"path":"a.txt"}\nEND_TOOL_CALL\n'
            'TOOL_CALL file_read\n{"path":"b.txt"\nEND_TOOL_CALL'
        )
        self.assertEqual(parse_tool_calls(text), [])


if __name__ == "__main__":
    unittest.main()
