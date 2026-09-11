"""Secure browser login for native AILinux applications.

The browser authenticates against login.ailinux.me. The app receives only a
short-lived one-time code on a random loopback port and exchanges it with a
PKCE verifier for its own TriForce session.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BROKER_URL = "https://login.ailinux.me/"


class BrowserLoginError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def browser_login(base_url: str, app_id: str, timeout: float = 180.0) -> dict[str, Any]:
    """Authenticate via the system browser and return a normal TriForce login payload."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}
    event = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            params = urllib.parse.parse_qs(parsed.query)
            result["code"] = (params.get("code") or [""])[0]
            result["state"] = (params.get("state") or [""])[0]
            result["error"] = (params.get("error") or [""])[0]
            body = (
                b"<!doctype html><meta charset=utf-8><title>AILinux Login</title>"
                b"<style>body{font-family:system-ui;background:#0b0f14;color:#eaf0f6;"
                b"display:grid;place-items:center;min-height:90vh}main{max-width:520px;"
                b"padding:32px;border:1px solid #283545;border-radius:18px;background:#111923}</style>"
                b"<main><h2>AILinux login received</h2><p>You can return to the application.</p></main>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            event.set()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    server.timeout = 0.5
    port = int(server.server_address[1])
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    query = urllib.parse.urlencode(
        {
            "google": "1",
            "app_login": "1",
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    login_url = BROKER_URL + "?" + query

    opened = webbrowser.open(login_url, new=1, autoraise=True)
    if not opened:
        server.server_close()
        raise BrowserLoginError(f"Could not open the system browser. Open this URL manually: {login_url}")

    deadline = time.monotonic() + max(10.0, timeout)
    try:
        while not event.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not event.is_set():
        raise BrowserLoginError("Browser login timed out")
    if result.get("error"):
        raise BrowserLoginError("Browser login failed: " + result["error"])
    if not result.get("code") or not secrets.compare_digest(result.get("state", ""), state):
        raise BrowserLoginError("Browser login callback failed state/code validation")

    endpoint = base_url.rstrip("/") + "/v1/auth/browser/exchange"
    payload = json.dumps(
        {
            "code": result["code"],
            "purpose": "app",
            "app_id": app_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail") or body
        except Exception:
            detail = body
        raise BrowserLoginError(f"TriForce browser exchange failed (HTTP {exc.code}): {detail}") from exc
    except Exception as exc:
        raise BrowserLoginError(f"TriForce browser exchange failed: {exc}") from exc

    if not data.get("token"):
        raise BrowserLoginError("TriForce browser exchange returned no session token")
    return data
