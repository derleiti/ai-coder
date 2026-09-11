"""Local GGUF model discovery, installation and Ollama hosting.

The local runtime is intentionally loopback-only. Publishing a model to remote
TriForce/Federation is a separate opt-in boundary; this module never opens a
listener to the LAN or Internet.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

OLLAMA_BASE_URL = os.environ.get("AICODER_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
LOCAL_MODEL_ROOT = Path(os.environ.get("AICODER_LOCAL_MODEL_DIR", "~/.local/share/aicoder/models")).expanduser()
_MODEL_NAME_RE = re.compile(r"[^a-zA-Z0-9._:-]+")
_QUANT_RE = re.compile(r"(?:^|[._-])(IQ\d(?:_[A-Z0-9]+)?|Q\d(?:_[A-Z0-9]+)+)(?:[._-]|$)", re.I)


class LocalModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalHostStatus:
    ollama_installed: bool
    ollama_executable: str
    ollama_version: str
    api_online: bool
    service_state: str
    base_url: str = OLLAMA_BASE_URL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HFModel:
    repo_id: str
    downloads: int = 0
    likes: int = 0
    pipeline_tag: str = ""
    gated: bool = False
    updated_at: str = ""


@dataclass(frozen=True)
class HFFile:
    repo_id: str
    filename: str
    size: int = 0
    quantization: str = ""


def _run(argv: list[str], *, timeout: int = 30, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalModelError(f"Command failed to start: {argv[0]}") from exc


def _api_json(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
              timeout: int = 5) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(OLLAMA_BASE_URL + path, data=body, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:  # noqa: S310 - loopback URL is fixed/configured locally
            raw = response.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise LocalModelError(f"Local Ollama API unavailable at {OLLAMA_BASE_URL}") from exc
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise LocalModelError("Local Ollama returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise LocalModelError("Local Ollama returned an invalid response")
    return data


def host_status() -> LocalHostStatus:
    executable = shutil.which("ollama") or ""
    version = ""
    if executable:
        result = _run([executable, "--version"], timeout=8)
        version = (result.stdout or result.stderr or "").strip().splitlines()[0] if (result.stdout or result.stderr) else ""
    api_online = False
    try:
        _api_json("/api/tags", timeout=3)
        api_online = True
    except LocalModelError:
        pass
    service_state = "unknown"
    systemctl = shutil.which("systemctl")
    if systemctl:
        result = _run([systemctl, "is-active", "ollama.service"], timeout=5)
        service_state = (result.stdout or "inactive").strip() or "inactive"
    return LocalHostStatus(bool(executable), executable, version, api_online, service_state)


def installed_models() -> list[dict[str, Any]]:
    data = _api_json("/api/tags")
    result: list[dict[str, Any]] = []
    for row in data.get("models", []) or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("model") or "").strip()
        if not name:
            continue
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        result.append({
            "name": name,
            "id": f"ollama/{name}",
            "size": int(row.get("size") or 0),
            "modified_at": str(row.get("modified_at") or ""),
            "quantization": str(details.get("quantization_level") or ""),
            "family": str(details.get("family") or ""),
        })
    return sorted(result, key=lambda item: item["name"].lower())


def _hf_api():
    try:
        from huggingface_hub import HfApi  # type: ignore
    except ImportError as exc:
        raise LocalModelError("Python dependency 'huggingface_hub' is missing") from exc
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY") or None
    return HfApi(token=token)


def search_huggingface(query: str = "", *, limit: int = 40) -> list[HFModel]:
    """Search GGUF repositories on Hugging Face, ordered by downloads."""
    api = _hf_api()
    try:
        rows: Iterable[Any] = api.list_models(
            search=(query.strip() or None), filter="gguf", sort="downloads",
            limit=max(1, min(100, int(limit))), full=False,
        )
        result: list[HFModel] = []
        for row in rows:
            repo_id = str(getattr(row, "id", "") or "").strip()
            if not repo_id:
                continue
            result.append(HFModel(
                repo_id=repo_id,
                downloads=int(getattr(row, "downloads", 0) or 0),
                likes=int(getattr(row, "likes", 0) or 0),
                pipeline_tag=str(getattr(row, "pipeline_tag", "") or ""),
                gated=bool(getattr(row, "gated", False)),
                updated_at=str(getattr(row, "last_modified", "") or ""),
            ))
        return result
    except Exception as exc:
        raise LocalModelError(f"Hugging Face search failed: {exc}") from exc


def huggingface_gguf_files(repo_id: str) -> list[HFFile]:
    api = _hf_api()
    try:
        info = api.model_info(repo_id, files_metadata=True)
    except Exception as exc:
        raise LocalModelError(f"Could not read Hugging Face model files: {exc}") from exc
    files: list[HFFile] = []
    for sibling in getattr(info, "siblings", []) or []:
        filename = str(getattr(sibling, "rfilename", "") or "")
        if not filename.lower().endswith(".gguf"):
            continue
        # Ollama can import one complete GGUF directly. Split repositories also
        # publish consolidated files in common cases; hide shards here because
        # selecting a single -00001-of-00002 file would create a broken model.
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", filename, re.IGNORECASE):
            continue
        size = int(getattr(sibling, "size", 0) or 0)
        match = _QUANT_RE.search(Path(filename).name)
        files.append(HFFile(repo_id=repo_id, filename=filename, size=size,
                            quantization=(match.group(1).upper() if match else "")))
    return sorted(files, key=lambda item: (item.size <= 0, item.size, item.filename.lower()))


def download_gguf(repo_id: str, filename: str) -> Path:
    if not filename.lower().endswith(".gguf"):
        raise LocalModelError("Only GGUF model files can be imported into this local hosting workflow")
    try:
        from huggingface_hub import hf_hub_download  # type: ignore
    except ImportError as exc:
        raise LocalModelError("Python dependency 'huggingface_hub' is missing") from exc
    target = LOCAL_MODEL_ROOT / repo_id.replace("/", "__")
    target.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY") or None
    try:
        path = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(target), token=token)
    except Exception as exc:
        raise LocalModelError(f"Hugging Face download failed: {exc}") from exc
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise LocalModelError("Downloaded GGUF file is missing")
    return resolved


def suggested_host_name(repo_id: str, filename: str) -> str:
    repo = repo_id.split("/")[-1]
    repo = re.sub(r"(?i)-gguf$", "", repo)
    quant = ""
    match = _QUANT_RE.search(Path(filename).name)
    if match:
        quant = match.group(1).lower()
    raw = f"hf-{repo}:{quant}" if quant else f"hf-{repo}:latest"
    return sanitize_host_name(raw)


def sanitize_host_name(name: str) -> str:
    value = _MODEL_NAME_RE.sub("-", str(name or "").strip()).strip("-./")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        raise LocalModelError("A local Ollama model name is required")
    if len(value) > 120:
        value = value[:120].rstrip("-")
    return value.lower()


def import_gguf(gguf_path: str | Path, model_name: str) -> str:
    executable = shutil.which("ollama")
    if not executable:
        raise LocalModelError("Ollama is not installed")
    if not host_status().api_online:
        raise LocalModelError("Ollama service is not running")
    path = Path(gguf_path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".gguf":
        raise LocalModelError("Selected GGUF file does not exist")
    name = sanitize_host_name(model_name)
    with tempfile.TemporaryDirectory(prefix="aicoder-ollama-import-") as tmp:
        modelfile = Path(tmp) / "Modelfile"
        # Ollama's Modelfile parser supports quoted paths; escape only the two
        # characters that can terminate/alter a quoted FROM value.
        safe_path = str(path).replace("\\", "\\\\").replace('"', '\\"')
        modelfile.write_text(f'FROM "{safe_path}"\n', encoding="utf-8")
        result = _run([executable, "create", name, "-f", str(modelfile)], timeout=1800)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Ollama import failed").strip().splitlines()[-1]
        raise LocalModelError(detail[:500])
    return f"ollama/{name}"


def test_model(model_name: str, *, timeout: int = 120) -> dict[str, Any]:
    name = str(model_name or "").removeprefix("ollama/").strip()
    if not name:
        raise LocalModelError("Model name is required")
    data = _api_json("/api/generate", method="POST", payload={
        "model": name,
        "prompt": "Reply with exactly: LOCAL_MODEL_OK",
        "stream": False,
        "options": {"temperature": 0},
    }, timeout=timeout)
    text = str(data.get("response") or "").strip()
    if not text:
        raise LocalModelError("Hosted model returned an empty response")
    return {"ok": True, "model": f"ollama/{name}", "response": text[:300]}


def delete_model(model_name: str) -> None:
    executable = shutil.which("ollama")
    if not executable:
        raise LocalModelError("Ollama is not installed")
    name = str(model_name or "").removeprefix("ollama/").strip()
    result = _run([executable, "rm", name], timeout=120)
    if result.returncode != 0:
        raise LocalModelError((result.stderr or result.stdout or "Ollama remove failed").strip()[:500])


def ollama_install_command() -> str:
    """Visible interactive installer command; intentionally never run silently."""
    return (
        "tmp=$(mktemp); "
        "curl -fL https://ollama.com/install.sh -o \"$tmp\" && "
        "sudo sh \"$tmp\"; rc=$?; rm -f \"$tmp\"; "
        "if [ $rc -eq 0 ]; then sudo systemctl enable --now ollama.service; fi; "
        "echo; echo 'Ollama setup exit code:' $rc; read -r -p 'Press Enter to close...' _; exit $rc"
    )


def launch_ollama_installer() -> None:
    """Open the official Ollama installer in a visible terminal for sudo consent."""
    script = ollama_install_command()
    title = "AICoder - Ollama Setup"
    candidates: list[list[str]] = []
    if shutil.which("konsole"):
        candidates.append(["konsole", "--new-tab", "-p", f"tabtitle={title}", "-e", "bash", "-lc", script])
    if shutil.which("gnome-terminal"):
        candidates.append(["gnome-terminal", "--title", title, "--", "bash", "-lc", script])
    if shutil.which("xterm"):
        candidates.append(["xterm", "-T", title, "-e", "bash", "-lc", script])
    for argv in candidates:
        try:
            subprocess.Popen(argv, start_new_session=True)
            return
        except OSError:
            continue
    raise LocalModelError("No graphical terminal found for Ollama setup")
