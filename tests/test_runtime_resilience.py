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

    def test_permanent_client_error_still_fails(self):
        client = MagicMock()
        client.timeout = 30
        client.chat.side_effect = ClientError("HTTP 401: Unauthorized")
        runtime = NativeLightRuntime(
            client=client,
            initial_prompt="Inspect workspace",
            model="test/model",
            fallback_model=None,
            workspace_root=".",
            tools=[],
            load_tools_on_start=False,
            persistent_plan=False,
        )
        result = runtime.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("HTTP 401", result.error)

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
