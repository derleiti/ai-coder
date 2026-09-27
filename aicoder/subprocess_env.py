"""Environment helpers for subprocesses launched from standalone AICoder builds."""
from __future__ import annotations

import os
import sys
from typing import Mapping


def external_system_env(*, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment safe for native system executables.

    PyInstaller one-file builds prepend their temporary extraction directory
    to ``LD_LIBRARY_PATH`` and preserve the previous value in
    ``LD_LIBRARY_PATH_ORIG``. Native host tools such as ``journalctl`` must
    not inherit the bundle-private loader path or they may load AICoder's
    bundled OpenSSL/system libraries instead of the host versions.
    """
    env = dict(os.environ if base is None else base)
    had_original = "LD_LIBRARY_PATH_ORIG" in env
    original_ld = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if getattr(sys, "frozen", False) or had_original:
        if original_ld:
            env["LD_LIBRARY_PATH"] = original_ld
        else:
            env.pop("LD_LIBRARY_PATH", None)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
