"""Typed local operating-system tools used natively and through MCP adapters."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .plugins import ToolDefinition, ToolSecurity

_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _schema(name: str, description: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


def _run(argv: list[str], *, timeout: int = 10, stdin: str | None = None) -> tuple[str, bool]:
    try:
        completed = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return json.dumps({"error": f"command not found: {argv[0]}"}), True
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"command timed out after {timeout}s", "program": argv[0]}), True
    except OSError as exc:
        return json.dumps({"error": str(exc), "program": argv[0]}), True
    payload = {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[:20000],
        "stderr": (completed.stderr or "")[:8000],
    }
    return json.dumps(payload, ensure_ascii=False), completed.returncode != 0


def _elevated_argv(argv: list[str], args: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    strategy = str(args.get("_elevation_strategy") or "")
    if strategy == "sudo":
        return ["sudo", "-n", "--", *argv], None
    if strategy == "pkexec":
        return ["pkexec", *argv], None
    return None, "elevated Local OS action reached provider without an approved elevation strategy"


def _is_termux() -> bool:
    return bool(os.environ.get("TERMUX_VERSION") or os.environ.get("TERMUX__PREFIX")) or platform.system().lower() == "android"


def _manager() -> str | None:
    for name in ("apt-get", "dnf", "yum", "pacman", "zypper"):
        if shutil.which(name):
            return name
    return None


def _system_overview() -> dict[str, Any]:
    data: dict[str, Any] = {
        "hostname": platform.node(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "package_manager": _manager(),
        "environment": "termux" if _is_termux() else "linux",
    }
    try:
        data["loadavg"] = list(os.getloadavg())
    except (AttributeError, OSError):
        pass
    try:
        data["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                meminfo[key] = value.strip()
        data["memory"] = meminfo
    except OSError:
        pass
    return data


class LocalOSToolProvider:
    provider_id = "local-os"

    def tools(self) -> tuple[ToolDefinition, ...]:
        ro = ToolSecurity(read_only=True, mutating=False, external_side_effect=False)
        mutate = ToolSecurity(read_only=False, mutating=True, external_side_effect=True)
        root = ToolSecurity(read_only=False, mutating=True, requires_elevation=True, external_side_effect=True)
        return (
            ToolDefinition(_schema("os_system_overview", "Read local OS, kernel, CPU/load, memory and package-manager overview."), ("system_diagnostics",), ro),
            ToolDefinition(_schema("os_kernel_info", "Read local kernel and operating-system release information."), ("system_diagnostics",), ro),
            ToolDefinition(_schema("os_process_list", "List local processes with pid, user, CPU/memory and command.", {"limit": {"type":"integer","minimum":1,"maximum":200}}), ("system_diagnostics",), ro),
            ToolDefinition(_schema("os_network_routes", "Read local IP addresses and routing table."), ("network","system_diagnostics"), ro),
            ToolDefinition(_schema("os_network_ports", "Read listening TCP/UDP sockets."), ("network","system_diagnostics"), ro),
            ToolDefinition(_schema("os_storage_overview", "Read mounted filesystem capacity and block-device overview."), ("storage","system_diagnostics"), ro),
            ToolDefinition(_schema("os_package_list_upgradable", "List locally known package upgrades without installing them."), ("packages","system_diagnostics"), ro),
            ToolDefinition(_schema("os_service_status", "Read status of one systemd service.", {"service":{"type":"string"}}, ["service"]), ("services","system_diagnostics"), ro),
            ToolDefinition(_schema("os_service_logs", "Read recent journal entries for one systemd service.", {"service":{"type":"string"},"lines":{"type":"integer","minimum":1,"maximum":500}}, ["service"]), ("services","system_diagnostics"), ro),
            ToolDefinition(_schema("os_container_list", "List local Docker or Podman containers.", {"runtime":{"type":"string","enum":["auto","docker","podman"]}}), ("containers","system_diagnostics"), ro),
            ToolDefinition(_schema("os_container_logs", "Read recent logs of one local container.", {"container":{"type":"string"},"runtime":{"type":"string","enum":["auto","docker","podman"]},"lines":{"type":"integer","minimum":1,"maximum":500}}, ["container"]), ("containers","system_diagnostics"), ro),
            ToolDefinition(_schema("os_package_install", "Install explicitly named packages using the detected local package manager.", {"packages":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":50}}, ["packages"]), ("packages",), root),
            ToolDefinition(_schema("os_service_action", "Start, stop, restart, enable or disable one systemd service.", {"service":{"type":"string"},"action":{"type":"string","enum":["start","stop","restart","enable","disable"]}}, ["service","action"]), ("services",), root),
            ToolDefinition(_schema("os_container_action", "Start, stop or restart one local container.", {"container":{"type":"string"},"runtime":{"type":"string","enum":["auto","docker","podman"]},"action":{"type":"string","enum":["start","stop","restart"]}}, ["container","action"]), ("containers",), mutate),
        )

    def security_for(self, name: str, args: dict[str, Any]) -> ToolSecurity:
        for tool in self.tools():
            if tool.name == name:
                return tool.security
        return ToolSecurity()

    def _runtime(self, requested: str) -> str | None:
        if requested in {"docker", "podman"}:
            return requested if shutil.which(requested) else None
        for candidate in ("docker", "podman"):
            if shutil.which(candidate):
                return candidate
        return None

    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        known = {tool.name for tool in self.tools()}
        if name not in known:
            return json.dumps({"error": f"unknown local OS tool: {name}"}), True
        if name == "os_system_overview":
            return json.dumps(_system_overview(), ensure_ascii=False, indent=2), False
        if name == "os_kernel_info":
            payload = {"uname": platform.uname()._asdict()}
            try:
                payload["os_release"] = dict(
                    line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                    if "=" in line
                )
            except OSError:
                pass
            return json.dumps(payload, ensure_ascii=False, indent=2), False
        if name == "os_process_list":
            limit = max(1, min(200, int(args.get("limit") or 50)))
            text, err = _run(["ps", "-eo", "pid,user,pcpu,pmem,stat,comm,args", "--sort=-pcpu"], timeout=8)
            if err: return text, True
            data=json.loads(text); lines=data.get("stdout", "").splitlines()[:limit+1]; data["stdout"]="\n".join(lines)+("\n" if lines else "")
            return json.dumps(data, ensure_ascii=False), False
        if name == "os_network_routes":
            addr_argv = ["ip", "address", "show"] if _is_termux() else ["ip", "-brief", "address"]
            addr, e1=_run(addr_argv, timeout=5); routes, e2=_run(["ip", "route", "show"], timeout=5)
            payload={"addresses": json.loads(addr), "routes": json.loads(routes)}
            if _is_termux() and (e1 or e2):
                payload["limited"] = "Android may deny netlink route/address details to unprivileged Termux apps"
            return json.dumps(payload, ensure_ascii=False), e1 and e2
        if name == "os_network_ports":
            return _run(["ss", "-lntup"], timeout=8)
        if name == "os_storage_overview":
            df_argv = ["df", "-h"] if _is_termux() else ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"]
            df, e1=_run(df_argv, timeout=8)
            if shutil.which("lsblk"):
                lsblk, e2=_run(["lsblk", "-o", "NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS"], timeout=8)
                block_devices=json.loads(lsblk)
            else:
                e2=True
                block_devices={"available": False, "reason": "lsblk not installed; common on Termux/Android"}
            payload={"filesystems": json.loads(df), "block_devices": block_devices}
            if _is_termux():
                payload["environment"]="termux"
                payload["limited"]="Android does not expose a traditional Linux block-device view to unprivileged Termux apps"
            return json.dumps(payload, ensure_ascii=False), e1
        if name == "os_package_list_upgradable":
            manager=_manager()
            if manager == "apt-get": argv=["apt", "list", "--upgradable"]
            elif manager in {"dnf","yum"}: argv=[manager, "check-update", "--cacheonly"]
            elif manager == "pacman": argv=["pacman", "-Qu"]
            elif manager == "zypper": argv=["zypper", "--non-interactive", "list-updates"]
            else: return json.dumps({"error":"no supported package manager found"}), True
            text, err=_run(argv, timeout=30)
            if manager in {"dnf","yum"} and err and json.loads(text).get("exit_code") == 100: err=False
            return text, err
        if name in {"os_service_status", "os_service_logs", "os_service_action"}:
            service=str(args.get("service") or "")
            if not _NAME_RE.fullmatch(service): return json.dumps({"error":"invalid service name"}), True
            if not shutil.which("systemctl"): return json.dumps({"error":"systemd/systemctl not available"}), True
            if name == "os_service_status": return _run(["systemctl","status","--no-pager","--full",service], timeout=10)
            if name == "os_service_logs":
                lines=max(1,min(500,int(args.get("lines") or 100)))
                return _run(["journalctl","-u",service,"-n",str(lines),"--no-pager"], timeout=12)
            action=str(args.get("action") or "")
            if action not in {"start","stop","restart","enable","disable"}: return json.dumps({"error":"invalid service action"}), True
            argv, why=_elevated_argv(["systemctl",action,service],args)
            if argv is None: return json.dumps({"error":why}), True
            return _run(argv, timeout=60)
        if name in {"os_container_list", "os_container_logs", "os_container_action"}:
            runtime=self._runtime(str(args.get("runtime") or "auto"))
            if runtime is None: return json.dumps({"error":"docker/podman runtime not available"}), True
            if name == "os_container_list": return _run([runtime,"ps","-a","--no-trunc"], timeout=12)
            container=str(args.get("container") or "")
            if not _CONTAINER_RE.fullmatch(container): return json.dumps({"error":"invalid container name/id"}), True
            if name == "os_container_logs":
                lines=max(1,min(500,int(args.get("lines") or 100)))
                return _run([runtime,"logs","--tail",str(lines),container], timeout=15)
            action=str(args.get("action") or "")
            if action not in {"start","stop","restart"}: return json.dumps({"error":"invalid container action"}), True
            return _run([runtime,action,container], timeout=60)
        if name == "os_package_install":
            packages=args.get("packages")
            if not isinstance(packages,list) or not packages or len(packages)>50 or not all(_NAME_RE.fullmatch(str(p)) for p in packages):
                return json.dumps({"error":"packages must be 1..50 explicit package names"}), True
            manager=_manager()
            if manager == "apt-get": base=["apt-get","install","-y","--",*map(str,packages)]
            elif manager in {"dnf","yum"}: base=[manager,"install","-y",*map(str,packages)]
            elif manager == "pacman": base=["pacman","-S","--noconfirm",*map(str,packages)]
            elif manager == "zypper": base=["zypper","--non-interactive","install",*map(str,packages)]
            else: return json.dumps({"error":"no supported package manager found"}), True
            argv, why=_elevated_argv(base,args)
            if argv is None: return json.dumps({"error":why}), True
            return _run(argv, timeout=300)
        return json.dumps({"error":f"unhandled local OS tool: {name}"}), True
