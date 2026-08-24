from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aicoder.executor as executor

from aicoder.privileges import PrivilegeBroker, assess_execution


class PrivilegeBrokerPolicyTests(unittest.TestCase):
    def test_safe_read_is_automatic_everywhere(self):
        risk=assess_execution("file_read", {"command":"cat README.md"})
        decision=PrivilegeBroker.evaluate("ask", risk, headless=True)
        self.assertTrue(decision.automatic)
        self.assertFalse(decision.denied)

    def test_autopilot_write_is_automatic(self):
        risk=assess_execution("future_tool", {"_mutating":True})
        decision=PrivilegeBroker.evaluate("autopilot", risk, headless=True)
        self.assertTrue(decision.automatic)
        self.assertFalse(decision.denied)

    def test_destructive_headless_fails_closed(self):
        risk=assess_execution("future_tool", {"_mutating":True,"_destructive":True})
        decision=PrivilegeBroker.evaluate("all", risk, headless=True)
        self.assertTrue(decision.denied)
        self.assertFalse(decision.automatic)

    def test_elevation_headless_fails_closed(self):
        risk=assess_execution("future_tool", {"sudo":True})
        decision=PrivilegeBroker.evaluate("all", risk, headless=True)
        self.assertTrue(decision.denied)
        self.assertIn("elevation", decision.reason)

    def test_workspace_escape_requires_interactive_confirmation(self):
        risk=assess_execution("file_read", {"path":"/tmp/x"})
        decision=PrivilegeBroker.evaluate("all", risk, workspace_escape=True, headless=False)
        self.assertTrue(decision.requires_confirmation)
        self.assertFalse(decision.automatic)

    def test_security_change_never_auto_approves(self):
        risk=assess_execution("settings_apply_patch", {"_mutating":True,"_security_change":True})
        decision=PrivilegeBroker.evaluate("all", risk, headless=False)
        self.assertTrue(decision.requires_confirmation)
        self.assertFalse(decision.automatic)

    def test_terminal_auth_fails_without_tty(self):
        with patch("sys.stdin.isatty", return_value=False):
            ok, message=PrivilegeBroker.authenticate_terminal()
        self.assertFalse(ok)
        self.assertIn("TTY", message)

    def test_terminal_auth_uses_sudo_validation_without_capturing_password(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("shutil.which", return_value="/usr/bin/sudo"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            ok, _=PrivilegeBroker.authenticate_terminal()
        self.assertTrue(ok)
        run.assert_called_once_with(["sudo", "-v"], timeout=120, check=False)

    def test_terminal_auth_rejects_failed_validation(self):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("shutil.which", return_value="/usr/bin/sudo"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        ):
            ok, message=PrivilegeBroker.authenticate_terminal()
        self.assertFalse(ok)
        self.assertIn("exit code 1", message)

    def test_gui_elevation_preflight_requires_display_and_pkexec(self):
        with patch("shutil.which", return_value=None):
            ok, _=PrivilegeBroker.gui_elevation_available()
        self.assertFalse(ok)
        with (
            patch("shutil.which", return_value="/usr/bin/pkexec"),
            patch.dict("os.environ", {}, clear=True),
        ):
            ok, message=PrivilegeBroker.gui_elevation_available()
        self.assertFalse(ok)
        self.assertIn("graphical session", message)

    def test_elevated_shell_wrapper_is_noninteractive_after_auth(self):
        wrapped=executor._elevated_shell_command("printf hello", "sudo")
        self.assertTrue(wrapped.startswith("sudo -n -- /bin/bash -lc "))
        self.assertIn("printf hello", wrapped)
        gui=executor._elevated_shell_command("printf hello", "pkexec")
        self.assertTrue(gui.startswith("pkexec /bin/bash -lc "))

    def test_approval_callback_strategy_reaches_local_executor(self):
        client=MagicMock()
        def approve(_name, args):
            args["_elevation_strategy"]="sudo"
            return True
        with (
            patch.object(executor, "run_local_shell", return_value=("ok", False)) as local,
            patch.object(executor.audit, "log_tool"),
        ):
            result, is_error=executor.run_tool(
                client, "shell", {"command":"printf hello", "sudo":True}, approval_fn=approve
            )
        self.assertFalse(is_error)
        self.assertEqual(result, "ok")
        self.assertEqual(local.call_args.args[0]["_elevation_strategy"], "sudo")

    def test_known_read_only_binary_exec_does_not_require_approval(self):
        for program, arguments in (("uname", ["-a"]), ("cat", ["/proc/cpuinfo"]), ("git", ["--version"]), ("df", ["-h"])):
            with self.subTest(program=program):
                risk = assess_execution("binary_exec", {"program": program, "arguments": arguments})
                self.assertFalse(risk.needs_approval)
                self.assertFalse(risk.mutation)

    def test_unknown_or_mutating_binary_exec_remains_approval_gated(self):
        self.assertTrue(assess_execution("binary_exec", {"program": "python3", "arguments": ["-c", "open('x','w').write('y')"]}).needs_approval)
        self.assertTrue(assess_execution("binary_exec", {"program": "mv", "arguments": ["a", "b"]}).needs_approval)


if __name__ == "__main__":
    unittest.main()
