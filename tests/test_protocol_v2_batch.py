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

    def test_prose_between_blocks_rejects_whole_sequence(self):
        text = (
            'TOOL_CALL file_read\n{"path":"a.txt"}\nEND_TOOL_CALL\n'
            'now the next one\n'
            'TOOL_CALL file_read\n{"path":"b.txt"}\nEND_TOOL_CALL'
        )
        self.assertEqual(parse_tool_calls(text), [])

    def test_malformed_second_block_rejects_whole_sequence(self):
        text = (
            'TOOL_CALL file_read\n{"path":"a.txt"}\nEND_TOOL_CALL\n'
            'TOOL_CALL file_read\n{"path":"b.txt"\nEND_TOOL_CALL'
        )
        self.assertEqual(parse_tool_calls(text), [])


if __name__ == "__main__":
    unittest.main()
