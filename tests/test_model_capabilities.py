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

    def test_openrouter_is_pinned_to_text_tool_protocol(self):
        for model in (
            "openrouter/nvidia/nemotron-3-ultra-550b-a55b",
            "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        ):
            with self.subTest(model=model):
                self.assertFalse(mc.supports_tools(self.client, model))

    def test_other_function_calling_provider_is_accepted_by_capability_probe(self):
        self.assertTrue(mc.supports_tools(self.client, "mistral/mistral-code-agent-latest"))

    def test_openrouter_capability_probe_can_be_explicitly_enabled(self):
        self.assertTrue(mc.supports_tools(
            self.client, "openrouter/nvidia/nemotron-3-ultra-550b-a55b",
            allow_openrouter=True,
        ))

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

    def test_text_only_default_suppresses_native_tools_without_warning(self):
        runtime = self._runtime("nvidia/nvidia/nemotron-3-ultra-550b-a55b")
        events = []
        runtime.event_fn = lambda kind, payload: events.append((kind, payload))
        tools = [{"name": "file_edit"}, {"name": "file_read"}]
        self.assertIsNone(runtime._tools_for_request(tools, runtime.model))
        self.assertEqual(events, [])

    def test_opted_in_openrouter_chat_only_model_warns_only_once(self):
        client = _FakeClient(CATALOGUE + [{"id": "openrouter/chat-only", "capabilities": ["chat"]}])
        from aicoder.agent_runtime import NativeLightRuntime
        runtime = NativeLightRuntime(
            client=client, initial_prompt="implementiere Tetris", model="openrouter/chat-only",
            fallback_model=None, workspace_root="/tmp", native_openrouter_tool_calling=True,
        )
        events = []
        runtime.event_fn = lambda kind, payload: events.append(kind)
        tools = [{"name": "file_edit"}]
        for _ in range(4):
            runtime._tools_for_request(tools, runtime.model)
        self.assertEqual(events.count("model_without_tool_support"), 1)

    def test_openrouter_runtime_is_text_only_by_default(self):
        runtime = self._runtime("openrouter/nvidia/nemotron-3-ultra-550b-a55b")
        tools = [{"name": "file_edit"}, {"name": "file_read"}]
        self.assertIsNone(runtime._tools_for_request(tools, runtime.model))

    def test_other_capable_providers_are_text_only_by_default(self):
        runtime = self._runtime("mistral/mistral-code-agent-latest")
        tools = [{"name": "file_edit"}]
        self.assertIsNone(runtime._tools_for_request(tools, runtime.model))

    def test_openrouter_native_tools_require_explicit_opt_in(self):
        runtime = self._runtime("openrouter/nvidia/nemotron-3-ultra-550b-a55b")
        runtime.native_openrouter_tool_calling = True
        tools = [{"name": "file_edit"}, {"name": "file_read"}]
        self.assertEqual(runtime._tools_for_request(tools, runtime.model), tools)

    def test_opt_in_never_enables_native_tools_for_non_openrouter_provider(self):
        runtime = self._runtime("mistral/mistral-code-agent-latest")
        runtime.native_openrouter_tool_calling = True
        tools = [{"name": "file_edit"}]
        self.assertIsNone(runtime._tools_for_request(tools, runtime.model))

    def test_text_mode_ignores_unexpected_native_tool_calls(self):
        from unittest.mock import MagicMock, patch
        from aicoder.agent_runtime import NativeLightRuntime
        client = MagicMock()
        client.timeout = 30
        client.chat.side_effect = [
            {
                "response": "DONE: text path stayed authoritative",
                "tool_calls": [{"function": {"name": "file_edit", "arguments": "{}"}}],
                "model": "mistral/mistral-code-agent-latest",
            }
        ]
        runtime = NativeLightRuntime(
            client=client, initial_prompt="hello",
            model="mistral/mistral-code-agent-latest", fallback_model=None,
            workspace_root="/tmp", tools=[{"name":"file_edit","inputSchema":{"type":"object"}}],
            load_tools_on_start=True, persistent_plan=False,
        )
        with patch("aicoder.agent_runtime.run_tool") as execute:
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "DONE: text path stayed authoritative")
        execute.assert_not_called()
        self.assertIsNone(client.chat.call_args.kwargs["tools"])

    def test_native_openrouter_prompt_removes_text_tool_format(self):
        from aicoder.executor import build_system_prompt
        runtime = self._runtime("openrouter/nvidia/nemotron-3-ultra-550b-a55b")
        runtime.native_openrouter_tool_calling = True
        base = build_system_prompt([{"name":"file_read","inputSchema":{"type":"object"}}], "/tmp")
        native = runtime._system_for_tool_protocol(base, native=True)
        self.assertNotIn("TOOL_CALL tool_name", native)
        self.assertIn("Use only the provider-native function/tool calls", native)

    def test_empty_tool_list_stays_none(self):
        runtime = self._runtime("mistral/mistral-code-agent-latest")
        self.assertIsNone(runtime._tools_for_request([], runtime.model))


if __name__ == "__main__":
    unittest.main()
