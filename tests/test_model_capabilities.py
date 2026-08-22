"""Guards against sending tool schemas to models that cannot call tools.

A chat-only model answering a tool-carrying request returns an empty
completion on several providers, which the user experiences as an agent that
refuses to do anything. These tests pin the detection and, just as important,
the fail-open behaviour: an unknown or unreachable catalogue must never
disable a setup that works today.
"""

from __future__ import annotations

import unittest

from aicoder import model_capabilities as mc


class _FakeClient:
    def __init__(self, models):
        self._models = models
        self.calls = 0

    def list_models(self):
        self.calls += 1
        return self._models


class _BrokenClient:
    def list_models(self):
        raise RuntimeError("backend unreachable")


CATALOGUE = [
    {"id": "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "capabilities": ["chat"]},
    {"id": "openrouter/nvidia/nemotron-3-ultra-550b-a55b", "capabilities": ["chat", "function_calling"]},
    {"id": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "capabilities": ["chat", "function_calling"]},
    {"id": "mistral/mistral-code-agent-latest", "capabilities": ["code", "chat", "function_calling"]},
    {"id": "legacy/flagged-model", "tools": True},
]


class CapabilityDetectionTests(unittest.TestCase):
    def setUp(self):
        mc.reset_cache()
        self.client = _FakeClient(CATALOGUE)

    def tearDown(self):
        mc.reset_cache()

    def test_chat_only_model_is_rejected(self):
        self.assertFalse(mc.supports_tools(self.client, "nvidia/nvidia/nemotron-3-ultra-550b-a55b"))

    def test_function_calling_model_is_accepted(self):
        for model in ("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
                      "mistral/mistral-code-agent-latest"):
            with self.subTest(model=model):
                self.assertTrue(mc.supports_tools(self.client, model))

    def test_boolean_flag_variant_is_understood(self):
        # Not every backend uses the capabilities list.
        self.assertTrue(mc.supports_tools(self.client, "legacy/flagged-model"))

    def test_unknown_model_fails_open(self):
        # A missing catalogue entry must not silently degrade a working setup.
        self.assertTrue(mc.supports_tools(self.client, "some/brand-new-model"))

    def test_unreachable_catalogue_fails_open(self):
        mc.reset_cache()
        self.assertTrue(mc.supports_tools(_BrokenClient(), "anything/at-all"))

    def test_no_model_selected_fails_open(self):
        self.assertTrue(mc.supports_tools(self.client, None))

    def test_catalogue_is_cached(self):
        mc.supports_tools(self.client, "mistral/mistral-code-agent-latest")
        mc.supports_tools(self.client, "nvidia/nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(self.client.calls, 1, "agent loop must not refetch per request")


class RuntimeGateTests(unittest.TestCase):
    """The runtime must drop tools rather than send them into a dead end."""

    def setUp(self):
        mc.reset_cache()

    def tearDown(self):
        mc.reset_cache()

    def _runtime(self, model):
        from aicoder.agent_runtime import NativeLightRuntime
        return NativeLightRuntime(
            client=_FakeClient(CATALOGUE),
            initial_prompt="implementiere Tetris",
            model=model,
            fallback_model=None,
            workspace_root="/tmp",
        )

    def test_tools_are_suppressed_for_a_chat_only_model(self):
        runtime = self._runtime("nvidia/nvidia/nemotron-3-ultra-550b-a55b")
        events = []
        runtime.event_fn = lambda kind, payload: events.append((kind, payload))
        tools = [{"name": "file_edit"}, {"name": "file_read"}]
        self.assertIsNone(runtime._tools_for_request(tools, runtime.model))
        self.assertEqual([kind for kind, _ in events], ["model_without_tool_support"])
        self.assertNotIn("alternative", events[0][1])

    def test_warning_is_emitted_only_once_per_run(self):
        runtime = self._runtime("nvidia/nvidia/nemotron-3-ultra-550b-a55b")
        events = []
        runtime.event_fn = lambda kind, payload: events.append(kind)
        tools = [{"name": "file_edit"}]
        for _ in range(4):
            runtime._tools_for_request(tools, runtime.model)
        self.assertEqual(events.count("model_without_tool_support"), 1)

    def test_tools_pass_through_for_a_capable_model(self):
        runtime = self._runtime("mistral/mistral-code-agent-latest")
        tools = [{"name": "file_edit"}]
        self.assertEqual(runtime._tools_for_request(tools, runtime.model), tools)

    def test_empty_tool_list_stays_none(self):
        runtime = self._runtime("mistral/mistral-code-agent-latest")
        self.assertIsNone(runtime._tools_for_request([], runtime.model))


if __name__ == "__main__":
    unittest.main()
