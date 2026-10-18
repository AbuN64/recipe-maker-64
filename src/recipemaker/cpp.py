"""Preprocess context through the same toolchain contract as unbake's build."""

from __future__ import annotations

from unbake import api
from unbake.contracts import Config


def run(config: Config, path: str, text: str, version: str) -> str:
    """`text`, as the project file `path`, with line markers for header provenance."""
    return api.preprocess(config, path, version, text.encode(), markers=True)
