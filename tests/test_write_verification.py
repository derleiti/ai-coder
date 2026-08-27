"""Truncated model output must never be stored as if it were complete.

A hard-coded 4096-token reply budget cut generated files mid-line at roughly
370 lines, and file_edit wrote the fragment and reported success. These tests
pin both halves of the repair: a generous, configurable budget, and a result
check that refuses to newly break a Python file.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder import settings
from aicoder.executor import run_file_edit

VALID = "def add(a, b):\n    return a + b\n"
# The exact shape of a truncated response: cut inside a string literal.
TRUNCATED = 'def main():\n    print("Make sure you have pygame and'


class SyntaxGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patcher = patch("aicoder.executor._workspace_root", return_value=self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def _edit(self, **args):
        return run_file_edit(args)

    def test_truncated_create_is_refused_and_writes_nothing(self):
        target = self.root / "game.py"
        result, is_error = self._edit(path="game.py", operation="create", content=TRUNCATED)
        self.assertTrue(is_error)
        self.assertIn("invalid Python", result)
        self.assertFalse(target.exists(), "a refused write must not leave a partial file")

    def test_truncated_write_does_not_destroy_existing_work(self):
        target = self.root / "game.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(path="game.py", operation="write", content=TRUNCATED)
        self.assertTrue(is_error)
        self.assertEqual(target.read_text(encoding="utf-8"), VALID)

    def test_valid_python_is_written_normally(self):
        target = self.root / "ok.py"
        result, is_error = self._edit(path="ok.py", operation="create", content=VALID)
        self.assertFalse(is_error, result)
        self.assertEqual(target.read_text(encoding="utf-8"), VALID)

    def test_already_broken_file_stays_repairable(self):
        # Refusing every invalid result would make broken files unfixable.
        target = self.root / "broken.py"
        target.write_text(TRUNCATED, encoding="utf-8")
        result, is_error = self._edit(path="broken.py", operation="append", content='")\n')
        self.assertFalse(is_error, result)

    def test_repairing_a_broken_file_to_valid_python_works(self):
        target = self.root / "broken.py"
        target.write_text(TRUNCATED, encoding="utf-8")
        result, is_error = self._edit(path="broken.py", operation="write", content=VALID)
        self.assertFalse(is_error, result)
        self.assertEqual(target.read_text(encoding="utf-8"), VALID)

    def test_opt_out_allows_an_intentional_fragment(self):
        target = self.root / "fragment.py"
        result, is_error = self._edit(
            path="fragment.py", operation="create",
            content=TRUNCATED, allow_invalid_syntax=True,
        )
        self.assertFalse(is_error, result)
        self.assertTrue(target.exists())

    def test_non_python_files_are_untouched_by_the_guard(self):
        for name, content in (("notes.md", "# not python {"),
                              ("data.txt", "def broken("),
                              ("config.yml", "key: [unclosed")):
            with self.subTest(name=name):
                result, is_error = self._edit(path=name, operation="create", content=content)
                self.assertFalse(is_error, result)

    def test_append_that_breaks_valid_python_is_refused(self):
        target = self.root / "ok.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(path="ok.py", operation="append", content="def broken(")
        self.assertTrue(is_error)
        self.assertEqual(target.read_text(encoding="utf-8"), VALID)

    def test_replace_that_breaks_valid_python_is_refused(self):
        target = self.root / "ok.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(
            path="ok.py", operation="replace",
            old_text="return a + b", new_text="return a +",
        )
        self.assertTrue(is_error)
        self.assertEqual(target.read_text(encoding="utf-8"), VALID)

    def test_replace_accepts_find_replace_aliases_from_models(self):
        target = self.root / "ok.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(
            path="ok.py", operation="replace",
            find="return a + b", replace="return a - b",
        )
        self.assertFalse(is_error, result)
        self.assertIn("return a - b", target.read_text(encoding="utf-8"))

    def test_replace_accepts_content_as_unambiguous_new_text_alias(self):
        target = self.root / "ok.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(
            path="ok.py", operation="replace",
            old_text="return a + b", content="return a - b",
        )
        self.assertFalse(is_error, result)
        self.assertIn("return a - b", target.read_text(encoding="utf-8"))

    def test_replace_accepts_search_and_replacement_text_aliases(self):
        target = self.root / "ok.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(
            path="ok.py", operation="replace",
            search="return a + b", replacement_text="return a - b",
        )
        self.assertFalse(is_error, result)
        self.assertIn("return a - b", target.read_text(encoding="utf-8"))

    def test_invalid_replace_returns_actionable_contract_error(self):
        target = self.root / "ok.py"
        target.write_text(VALID, encoding="utf-8")
        result, is_error = self._edit(path="ok.py", operation="replace")
        self.assertTrue(is_error)
        self.assertIn("old_text", result)
        self.assertIn("new_text", result)
        self.assertIn("Do not repeat", result)

    def test_error_message_names_the_line(self):
        result, _ = self._edit(path="x.py", operation="create", content=TRUNCATED)
        self.assertIn("line 2", result)


class OutputBudgetSettingTests(unittest.TestCase):
    def test_max_output_tokens_is_registered_and_generous(self):
        spec = settings.REGISTRY["max_output_tokens"]
        self.assertEqual(spec.group, "runtime")
        self.assertGreaterEqual(spec.default, 8192,
                                "4096 truncated real files; the default must clear that")
        self.assertLessEqual(spec.minimum, 4096)

    def test_aliases_resolve(self):
        for alias in ("max_tokens", "output_tokens", "max-tokens"):
            with self.subTest(alias=alias):
                self.assertEqual(settings.resolve_key(alias), "max_output_tokens")

    def test_out_of_range_is_rejected(self):
        for bad in (10, 10 ** 9):
            with self.subTest(bad=bad), self.assertRaises(settings.SettingsError):
                settings.coerce("max_output_tokens", bad)

    def test_runtime_default_matches_the_registry(self):
        from aicoder.agent_runtime import NativeLightRuntime
        runtime = NativeLightRuntime(
            client=None, initial_prompt="x", model=None,
            fallback_model=None, workspace_root="/tmp",
        )
        self.assertEqual(runtime.max_output_tokens,
                         settings.REGISTRY["max_output_tokens"].default)


if __name__ == "__main__":
    unittest.main()
