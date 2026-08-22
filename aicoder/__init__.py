"""AICoder package metadata.

``pyproject.toml`` is the single source version in a source checkout. Installed
artifacts use distribution metadata generated from the same project version.
"""
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path


def _source_version() -> str | None:
    project = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not project.is_file():
        return None
    try:
        import tomllib
        with project.open("rb") as handle:
            value = tomllib.load(handle).get("project", {}).get("version")
        return str(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


__version__ = _source_version()
if __version__ is None:
    try:
        __version__ = distribution_version("aicoder")
    except PackageNotFoundError:
        __version__ = "0+unknown"
