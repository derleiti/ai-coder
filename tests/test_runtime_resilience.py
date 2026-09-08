from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_journal import ContinuationJournalStore
from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.client import ClientError, TriForceClient


class RuntimeResilienceTests(unittest.TestCase):
    def test_transient_model_failure_pauses_and_persists_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            config = Path(temp) / "config"
            store = PlanStore(config / "plans")
            journal_store = ContinuationJournalStore(config / "journals")
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = ClientError("HTTP 503: Service Unavailable")
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Inspect and debug failing tests",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                journal_store=journal_store,
                persistent_plan=True,
            )
            result = runtime.run()
            self.assertEqual(result.status, "paused")
            self.assertIn("Transient model/backend failure", result.response)
            plan = store.load(str(workspace), result.plan_id)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.status, "paused")
            journal = journal_store.load(str(workspace), result.plan_id)
            self.assertIsNotNone(journal)
            self.assertIn("Inspect and debug failing tests", journal.pending_input)


    def test_transient_pause_preserves_retry_metadata_for_team_orchestrator(self):
        client = MagicMock()
        client.timeout = 30
        client.chat.side_effect = ClientError(
            "HTTP 503: overloaded", status_code=503, retryable=True, retry_after=120
        )
        runtime = NativeLightRuntime(
            client=client, initial_prompt="inspect", model="test/model", fallback_model=None,
            workspace_root=".", tools=[], load_tools_on_start=False, persistent_plan=False,
        )
        result = runtime.run()
        self.assertEqual(result.status, "paused")
        self.assertEqual(result.failure_category, "transient")
        self.assertEqual(result.retry_after, 120)

    def test_permanent_client_error_still_fails(self):
        client = MagicMock()
        client.timeout = 30
        client.chat.side_effect = ClientError("HTTP 401: Unauthorized")
        events = []
        runtime = NativeLightRuntime(
            client=client,
            initial_prompt="Inspect workspace",
            model="test/model",
            fallback_model=None,
            workspace_root=".",
            tools=[],
            load_tools_on_start=False,
            persistent_plan=False,
            event_fn=lambda kind, payload: events.append((kind, payload)),
        )
        result = runtime.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("HTTP 401", result.error)
        self.assertEqual(events[-1][0], "run_terminal")
        self.assertEqual(events[-1][1]["status"], "failed")
        self.assertTrue(events[-1][1]["run_id"].startswith("run-"))

    def test_success_emits_final_then_completed_terminal_event(self):
        client = MagicMock()
        client.timeout = 30
        client.chat.return_value = {"response": "DONE: complete", "model": "test/model"}
        events = []
        runtime = NativeLightRuntime(
            client=client, initial_prompt="Summarize the workspace", model="test/model",
            fallback_model=None, workspace_root=".", tools=[], load_tools_on_start=False,
            persistent_plan=False,
            event_fn=lambda kind, payload: events.append((kind, payload)),
        )

        result = runtime.run()

        self.assertEqual(result.status, "completed")
        self.assertEqual(events[-2][0], "final")
        self.assertEqual(events[-1][0], "run_terminal")
        self.assertEqual(events[-1][1]["status"], "completed")
        self.assertEqual(events[-1][1]["progress"], 100)
        self.assertFalse(events[-1][1]["resumable"])

    def test_client_retries_429_once_before_returning_success(self):
        client = TriForceClient("http://example.invalid", timeout=1)
        with patch.object(
            client,
            "_do_request",
            side_effect=[ClientError("HTTP 429: Too Many Requests"), {"ok": True}],
        ) as request, patch("aicoder.client.time.sleep"):
            result = client._request("GET", "/x", _retries=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)

    def test_client_does_not_retry_401(self):
        client = TriForceClient("http://example.invalid", timeout=1)
        with patch.object(
            client,
            "_do_request",
            side_effect=ClientError("HTTP 401: Unauthorized"),
        ) as request, patch("aicoder.client.time.sleep"):
            with self.assertRaises(ClientError):
                client._request("GET", "/x", _retries=1)
        self.assertEqual(request.call_count, 1)

    def test_retry_resumes_existing_paused_plan_instead_of_creating_new_task(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        from aicoder.agent_plan import PlanStore
        from aicoder.agent_runtime import NativeLightRuntime

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            plan = store.create("Review the real project", str(workspace), "test/model")
            plan.status = "paused"
            plan.pause_reason = "transient failure"
            store.save(plan)

            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {"response": "DONE: resumed", "model": "test/model"}
            runtime = NativeLightRuntime(
                client=client, initial_prompt="retry", model="test/model", fallback_model=None,
                workspace_root=str(workspace), tools=[], load_tools_on_start=False,
                plan_store=store, resume=True, base_timeout=30,
            )
            result = runtime.run()
            resumed = store.load(str(workspace), plan.id)
            self.assertEqual(result.plan_id, plan.id)
            self.assertEqual(resumed.task, "Review the real project")
            self.assertEqual(resumed.resume_count, 1)
            self.assertEqual(resumed.status, "completed")


    def test_interruptible_runtime_cancels_active_transport_on_stop(self):
        import threading
        from aicoder.agent_runtime import NativeLightRuntime

        class CancellableTransport:
            timeout = 30
            def __init__(self):
                self.started = threading.Event()
                self.cancelled = threading.Event()
                self.finished = threading.Event()
            def list_models(self):
                return []
            def chat(self, **_kwargs):
                self.started.set()
                self.cancelled.wait(2)
                self.finished.set()
                raise RuntimeError("cancelled")
            def cancel_current_request(self):
                self.cancelled.set()
                return True

        transport = CancellableTransport()
        runtime = NativeLightRuntime(
            client=transport, model_client=transport, initial_prompt="inspect", model="test/model",
            fallback_model=None, workspace_root=".", tools=[], load_tools_on_start=False,
            persistent_plan=False, stop_requested=lambda: transport.started.is_set(),
        )
        result = runtime.run()
        self.assertEqual(result.status, "paused")
        self.assertTrue(transport.cancelled.is_set())
        self.assertTrue(transport.finished.wait(1))

    def test_triforce_cancel_closes_active_response(self):
        client = TriForceClient("http://example.invalid", token="token", timeout=1)
        response = MagicMock()
        client._set_active_response(response)
        self.assertTrue(client.cancel_current_request())
        response.close.assert_called_once()
        response.release_conn.assert_called_once()
        self.assertFalse(client.cancel_current_request())

    def test_triforce_cancel_targets_named_parallel_request(self):
        client = TriForceClient("http://example.invalid", token="token", timeout=1)
        first, second = MagicMock(), MagicMock()
        client._set_active_response(first, "req-a")
        client._set_active_response(second, "req-b")
        self.assertTrue(client.cancel_current_request("req-a"))
        first.close.assert_called_once()
        second.close.assert_not_called()
        self.assertIn("req-b", client._active_responses)

    def test_chat_sets_keepalive_header(self):
        client = TriForceClient("http://example.invalid", token="token", timeout=1)
        with patch.object(client, "_request", return_value={"response": "ok", "model": "m"}) as request:
            result = client.chat(message="hi", model="m")
        self.assertEqual(result["response"], "ok")
        self.assertEqual(request.call_args.kwargs["_extra_headers"], {"X-AICoder-Keepalive": "json"})

    def test_chat_propagates_request_id_to_transport_header_and_telemetry(self):
        client = TriForceClient("http://example.invalid", token="token", timeout=1)
        with patch.object(client, "_request", return_value={
            "response": "ok", "model": "m", "_transport_telemetry": {"elapsed_s": 1.0}
        }) as request:
            result = client.chat(message="hi", model="m", request_id="req-123")
        self.assertEqual(request.call_args.kwargs["_extra_headers"]["X-AICoder-Request-ID"], "req-123")
        self.assertEqual(result["_transport_telemetry"]["request_id"], "req-123")

    def test_keepalive_chunks_may_extend_total_turn_duration(self):
        class FakeResponse:
            status = 200
            def __init__(self):
                self.parts = [b"   \n", b'{"response":"OK","model":"m"}', b""]
                self.released = False
            def read(self, _size=None):
                return self.parts.pop(0)
            def release_conn(self):
                self.released = True

        response = FakeResponse()
        pool = MagicMock()
        pool.request.return_value = response
        client = TriForceClient("http://example.invalid", timeout=1)
        with patch("aicoder.client._get_pool", return_value=pool), patch(
            "aicoder.client.time.monotonic", side_effect=[0.0, 0.5, 120.0, 120.0, 120.0]
        ):
            result = client._do_request(
                "POST", "http://example.invalid/chat",
                {"X-AICoder-Keepalive": "json"}, b"{}", "chat/test",
            )
        self.assertEqual(result["response"], "OK")
        self.assertTrue(response.released)
        telemetry = result["_transport_telemetry"]
        self.assertGreaterEqual(telemetry["elapsed_s"], 120.0)
        self.assertEqual(telemetry["keepalive_chunks"], 1)
        self.assertEqual(telemetry["payload_chunks"], 1)
        self.assertEqual(telemetry["keepalive_times_s"], [0.5])

    def test_structured_stream_error_preserves_retry_after(self):
        from aicoder.client import _normalize_chat_response
        with self.assertRaises(ClientError) as caught:
            _normalize_chat_response({
                "error": {"status": 524, "detail": "origin timeout", "retryable": True, "retry_after": 120}
            })
        self.assertEqual(caught.exception.status_code, 524)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 120)

    def test_runtime_pause_surfaces_retry_after(self):
        client = MagicMock()
        client.timeout = 30
        client.chat.side_effect = ClientError(
            "HTTP 524: timeout", status_code=524, retryable=True, retry_after=120
        )
        runtime = NativeLightRuntime(
            client=client, initial_prompt="Inspect workspace", model="test/model",
            fallback_model=None, workspace_root=".", tools=[], load_tools_on_start=False,
            persistent_plan=False,
        )
        result = runtime.run()
        self.assertEqual(result.status, "paused")
        self.assertIn("Recommended retry delay: 120s", result.response)


if __name__ == "__main__":
    unittest.main()


def test_observational_runtime_allows_tool_protocol_example_as_final_text():
    from unittest.mock import MagicMock
    from aicoder.agent_runtime import NativeLightRuntime

    class Transport:
        timeout = 300
        def chat(self, **kwargs):
            return {
                "response": "# STAGE SUMMARY\nExample only:\nTOOL_CALL file_read\n{not-json}\nEND_TOOL_CALL\n\n# NEXT STAGE INSTRUCTIONS\nContinue.",
                "model": "openrouter/test/model",
            }

    runtime = NativeLightRuntime(
        client=MagicMock(), model_client=Transport(), initial_prompt="produce handoff",
        model="openrouter/test/model", fallback_model=None, workspace_root=".",
        tools=[], load_tools_on_start=False, persistent_plan=False, max_iterations=1,
        allow_mixed_tool_protocol_final=True,
    )
    with patch("aicoder.agent_runtime.is_action_request", return_value=True):
        result = runtime.run()
    assert result.status == "completed"
    assert "TOOL_CALL file_read" in result.response


def test_default_runtime_still_rejects_mixed_tool_protocol_final():
    from unittest.mock import MagicMock
    from aicoder.agent_runtime import NativeLightRuntime

    class Transport:
        timeout = 300
        def __init__(self): self.calls = 0
        def chat(self, **kwargs):
            self.calls += 1
            return {
                "response": "analysis\nTOOL_CALL file_read\n{not-json}\nEND_TOOL_CALL",
                "model": "openrouter/test/model",
            }

    transport=Transport()
    runtime = NativeLightRuntime(
        client=MagicMock(), model_client=transport, initial_prompt="Read x.txt using the available file_read tool and report the result.",
        model="openrouter/test/model", fallback_model=None, workspace_root=".",
        tools=[{"name":"file_read","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}],
        load_tools_on_start=False, persistent_plan=False, max_iterations=2,
    )
    with patch("aicoder.agent_runtime.is_action_request", return_value=True):
        result = runtime.run()
    assert result.status == "paused"
    assert result.failure_category == "transient"


def test_carried_tool_history_prevents_redundant_tool_nudge_after_provider_resume():
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import NativeLightRuntime

    class Transport:
        timeout = 300
        def __init__(self): self.calls = []
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return {"response": "DONE: preserved evidence is sufficient", "model": "openrouter/test/model"}

    transport = Transport()
    runtime = NativeLightRuntime(
        client=MagicMock(), model_client=transport,
        initial_prompt="Continue the same stage and finish the contract.",
        model="openrouter/test/model", fallback_model=None, workspace_root=".",
        tools=[{"name":"file_tree","inputSchema":{"type":"object","properties":{}}}],
        load_tools_on_start=False, persistent_plan=False, max_iterations=2,
        conversation=[
            {"role":"assistant","content":"", "tool_calls":[{"id":"c1","type":"function","function":{"name":"file_tree","arguments":"{}"}}]},
            {"role":"tool","tool_call_id":"c1","name":"file_tree","content":"(empty directory)"},
        ],
    )
    with patch("aicoder.agent_runtime.is_action_request", return_value=True):
        result = runtime.run()
    assert result.status == "completed"
    assert len(transport.calls) == 1
    assert result.iterations == 1


def test_native_tool_diagnostics_identifies_malformed_arguments_without_values():
    from aicoder.agent_runtime import _native_tool_call_diagnostics

    result = {
        "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "file_tree", "arguments": "{not-json}"},
        }]
    }
    rows = _native_tool_call_diagnostics(result)
    assert rows == [{
        "name": "file_tree",
        "argument_type": "str",
        "arguments_json_object": False,
        "has_id": True,
        "raw_type": "function",
    }]
    assert "not-json" not in str(rows)


def test_malformed_native_tool_call_is_not_reported_as_empty_response():
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import NativeLightRuntime

    class Transport:
        timeout = 300
        def chat(self, **kwargs):
            return {
                "response": "", "model": "openrouter/test/model",
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "file_tree", "arguments": "{not-json}"},
                }],
                "finish_reason": "error",
            }

    events = []
    runtime = NativeLightRuntime(
        client=MagicMock(), model_client=Transport(), initial_prompt="Inspect using file_tree.",
        model="openrouter/test/model", fallback_model=None, workspace_root=".",
        tools=[{"name":"file_tree","inputSchema":{"type":"object","properties":{}}}],
        load_tools_on_start=False, persistent_plan=False, max_iterations=2,
        native_openrouter_tool_calling=True,
        event_fn=lambda kind, payload: events.append((kind, payload)),
    )
    with patch("aicoder.agent_runtime.is_action_request", return_value=True):
        result = runtime.run()
    assert result.status == "paused"
    repairs = [p for k,p in events if k == "final_response_repair"]
    assert repairs and repairs[0]["reason"] == "malformed_native_tool_call"
    assert repairs[0]["diagnostics"]["native_tool_calls"][0]["arguments_json_object"] is False


def test_observational_runtime_does_not_require_verification_after_disposable_mutation(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import NativeLightRuntime

    class Transport:
        timeout = 300
        def __init__(self): self.calls = 0
        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "response": "", "model": "openrouter/test/model",
                    "tool_calls": [{
                        "id": "call-1", "type": "function",
                        "function": {"name": "directory_create", "arguments": '{"path":"scratch"}'},
                    }],
                    "finish_reason": "tool_calls",
                }
            return {"response": "SECTION:\nobservational handoff complete", "model": "openrouter/test/model", "finish_reason": "stop"}

    transport = Transport()
    runtime = NativeLightRuntime(
        client=MagicMock(), model_client=transport, initial_prompt="Inspect and produce SECTION handoff.",
        model="openrouter/test/model", fallback_model=None, workspace_root=str(tmp_path),
        tools=[{"name":"directory_create","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}],
        load_tools_on_start=False, persistent_plan=False, max_iterations=3,
        native_openrouter_tool_calling=True,
        enforce_post_mutation_verification=False,
    )
    with patch("aicoder.agent_runtime.is_action_request", return_value=True):
        result = runtime.run()
    assert result.status == "completed"
    assert result.response == "SECTION:\nobservational handoff complete"
    assert transport.calls == 2


def test_git_status_on_new_workspace_is_not_an_error(tmp_path):
    from unittest.mock import patch
    from aicoder.executor import run_git_read

    with patch("aicoder.executor._workspace_root", return_value=tmp_path.resolve()):
        result, is_error = run_git_read({"action": "status", "cwd": str(tmp_path)})
    assert is_error is False
    assert '"status": "not_git_repository"' in result
    assert "no Git repository yet" in result


def test_runtime_limits_tool_calls_per_turn(tmp_path):
    from aicoder.agent_runtime import NativeLightRuntime

    class Model:
        def __init__(self): self.calls = 0
        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                blocks = "\n".join(
                    f'TOOL_CALL file_tree\n{{"path":".","max_depth":{i+1}}}\nEND_TOOL_CALL'
                    for i in range(6)
                )
                return {"response": blocks, "tool_calls": [], "finish_reason": "stop"}
            return {"response": "DONE", "tool_calls": [], "finish_reason": "stop"}

    events = []
    runtime = NativeLightRuntime(
        client=object(), model_client=Model(), initial_prompt="Inspect the workspace and report.",
        model="test/model", fallback_model=None, workspace_root=str(tmp_path),
        plan_workspace_root=str(tmp_path), protected_workspace_root=None,
        tools=[{"name":"file_tree","description":"tree","input_schema":{"type":"object","properties":{}}}],
        persistent_plan=False, progressive_tool_disclosure=False, max_iterations=3,
        max_tool_calls_per_turn=2, event_fn=lambda kind, payload: events.append((kind,payload)),
    )
    result = runtime.run()
    assert result.status == "completed"
    tool_calls = [payload for kind,payload in events if kind == "tool_call"]
    assert len(tool_calls) == 2
    limited = [payload for kind,payload in events if kind == "tool_batch_limited"]
    assert limited and limited[0]["requested"] == 6 and limited[0]["executed"] == 2
