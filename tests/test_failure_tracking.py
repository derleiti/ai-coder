from __future__ import annotations

import unittest

from aicoder.failure_tracking import FailureTracker


class FailureTrackerTests(unittest.TestCase):
    def test_same_import_error_across_different_traceback_lines_has_same_signature(self):
        tracker = FailureTracker()
        first = tracker.observe(
            "Traceback:\n  File a.py, line 10\nImportError: cannot import name Font from partially initialized module pygame.font",
            True,
        )
        second = tracker.observe(
            "wrapper output\n  File b.py, line 99\nImportError: cannot import name Font from partially initialized module pygame.font",
            True,
        )
        self.assertEqual(first.category, "environment")
        self.assertEqual(first.signature, second.signature)
        self.assertEqual(second.count, 2)
        self.assertFalse(second.retryable)

    def test_transient_provider_failure_is_retryable(self):
        failure = FailureTracker().observe("HTTP 503 temporarily unavailable", True)
        self.assertEqual(failure.category, "transient")
        self.assertTrue(failure.retryable)


    def test_transient_retry_budget_opens_circuit_after_two_failures(self):
        tracker = FailureTracker(transient_retry_budget=2)
        first = tracker.observe("HTTP 503 temporarily unavailable", True)
        second = tracker.observe("HTTP 503 temporarily unavailable", True)
        third = tracker.observe("HTTP 503 temporarily unavailable", True)
        self.assertEqual(first.category, "transient")
        self.assertTrue(first.retryable)
        self.assertEqual(second.category, "transient")
        self.assertTrue(second.retryable)
        self.assertEqual(third.category, "persistent_dependency")
        self.assertFalse(third.retryable)
        self.assertEqual(third.count, 3)
        self.assertEqual(first.signature, third.signature)

    def test_permission_failure_is_not_retryable(self):
        failure = FailureTracker().observe("file_edit: aborted by user", True)
        self.assertEqual(failure.category, "permission")
        self.assertFalse(failure.retryable)

    def test_success_is_not_failure(self):
        self.assertIsNone(FailureTracker().observe("ok", False))


class NetworkFailureClassificationTests(unittest.TestCase):
    def test_common_transport_disconnects_are_transient(self):
        for message in (
            "connection refused",
            "connection aborted",
            "remote end closed connection",
            "network is unreachable",
            "temporary failure in name resolution",
        ):
            category, _signature, retryable = FailureTracker.classify(message)
            self.assertEqual(category, "transient", message)
            self.assertTrue(retryable, message)


if __name__ == "__main__":
    unittest.main()
