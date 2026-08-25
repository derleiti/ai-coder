from __future__ import annotations

import unittest
from unittest.mock import patch

from aicoder.performance import RuntimePerformance, model_usage_metrics


class RuntimePerformanceTests(unittest.TestCase):
    def test_records_model_tool_and_filesystem_time(self):
        metrics = RuntimePerformance(started_at=100.0)
        metrics.record_model(12_000)
        metrics.record_model(8_000)
        metrics.record_tool("file_read", 0.250)
        metrics.record_tool("test", 1.5)
        snapshot = metrics.snapshot(now=125.0)

        self.assertEqual(snapshot["wall_ms"], 25_000)
        self.assertEqual(snapshot["model_ms"], 20_000)
        self.assertEqual(snapshot["tool_ms"], 1_750)
        self.assertEqual(snapshot["filesystem_ms"], 250)
        self.assertEqual(snapshot["build_test_ms"], 1_500)
        self.assertEqual(snapshot["average_model_ms"], 10_000)
        self.assertEqual(snapshot["bottleneck"], "model")
        self.assertEqual(snapshot["warnings"][0]["kind"], "model_latency")

    def test_filesystem_warning_requires_meaningful_absolute_and_relative_cost(self):
        metrics = RuntimePerformance(started_at=0.0)
        metrics.record_tool("file_tree", 2.5)
        snapshot = metrics.snapshot(now=10.0)
        kinds = {item["kind"] for item in snapshot["warnings"]}
        self.assertIn("filesystem_latency", kinds)

    def test_tool_error_warning_is_bounded_signal(self):
        metrics = RuntimePerformance(started_at=0.0)
        for _ in range(3):
            metrics.record_tool("file_read", 0.01, is_error=True)
        snapshot = metrics.snapshot(now=1.0)
        kinds = {item["kind"] for item in snapshot["warnings"]}
        self.assertIn("tool_errors", kinds)

    def test_openai_usage_reports_effective_output_tokens_per_second(self):
        usage = model_usage_metrics({"usage": {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250}}, 2000)
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["output_tokens"], 250)
        self.assertEqual(usage["total_tokens"], 1250)
        self.assertEqual(usage["tokens_per_second"], 125.0)

    def test_gemini_usage_is_normalized(self):
        usage = model_usage_metrics({"usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 10, "totalTokenCount": 50}}, 500)
        self.assertEqual(usage["input_tokens"], 40)
        self.assertEqual(usage["output_tokens"], 10)
        self.assertEqual(usage["tokens_per_second"], 20.0)

    def test_aggregate_throughput_uses_only_requests_with_usage(self):
        metrics = RuntimePerformance(started_at=0.0)
        metrics.record_model(2000, input_tokens=100, output_tokens=200, total_tokens=300)
        metrics.record_model(8000)
        snapshot = metrics.snapshot(now=10.0)
        self.assertEqual(snapshot["tokenized_model_requests"], 1)
        self.assertEqual(snapshot["tokenized_model_ms"], 2000)
        self.assertEqual(snapshot["output_tokens_per_second"], 100.0)


if __name__ == "__main__":
    unittest.main()
