"""Recipe Maker reads unbake only through its api and contracts."""

from __future__ import annotations

import ast
from pathlib import Path

ALLOWED = {"unbake.api", "unbake.contracts"}
SOURCE = Path(__file__).resolve().parent.parent / "src" / "recipemaker"


def _offences(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
            if names[0] == "unbake":
                names = [f"unbake.{alias.name}" for alias in node.names]
        else:
            continue
        found += [
            f"{path.name}:{node.lineno} {n}"
            for n in names
            if (n == "unbake" or n.startswith("unbake.")) and n not in ALLOWED
        ]
    return found


def test_only_api_and_contracts_are_imported() -> None:
    offences = [line for path in sorted(SOURCE.rglob("*.py")) for line in _offences(path)]
    assert offences == []
