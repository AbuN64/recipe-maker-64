"""knowledge: provenance, shape and size of the cards, tables and rows. Pure data checks, no project."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent / "src" / "recipemaker"
KNOWLEDGE = ROOT / "knowledge"
TOOLCHAINS = ("ido-5.3", "ido-7.1", "gcc-2.7.2-kmc", "gcc-2.8.1-sn64")
AUTHORED_CAP = 3000  # lines of cards, and errors.toml together (owner decision)
FIXES = {"", "drop-local-definition"}  # the ids errors.autofix knows


def _toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _authored() -> list[Path]:
    return [*sorted(KNOWLEDGE.glob("**/*.toml"))]


def test_every_authored_file_says_it_is_original() -> None:
    for path in _authored():
        if path.suffix == ".toml":
            assert _toml(path)["source"] == "original", path.name
        else:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert rows and all(row["source"] == "original" for row in rows)


def test_no_code_block_over_three_lines_and_no_embedded_bytes() -> None:
    for path in _authored():
        text = path.read_text()
        values = _strings([json.loads(x) for x in text.splitlines()] if path.suffix == ".jsonl" else _toml(path))
        for value in values:
            assert value.count("\n") < 3, (path.name, value[:40])
        assert not re.search(r"\b[0-9A-Fa-f]{16,}\b", text), path.name  # no ROM or table bytes


@pytest.mark.parametrize("pattern", [r"/home/", r"/mnt/", r"\.z64|\.n64|\.v64"])
def test_no_machine_paths_or_rom_names(pattern: str) -> None:
    for path in [*KNOWLEDGE.glob("**/*"), *(ROOT / "tables").glob("*")]:
        if path.is_file():
            assert not re.search(pattern, path.read_text()), path.name


def test_authored_size_is_under_the_cap_and_tables_are_not_counted() -> None:
    assert sum(len(p.read_text().splitlines()) for p in _authored()) < AUTHORED_CAP


def test_compiler_cards_cover_every_toolchain_with_one_target_each() -> None:
    cards = [_toml(p) for p in sorted((KNOWLEDGE / "compilers").glob("*.toml"))]
    assert len(cards) == 3
    claimed = [t for card in cards for t in card["toolchains"]]
    assert sorted(claimed) == sorted(TOOLCHAINS)  # each toolchain is on exactly one card
    for card in cards:
        assert card["m2c_target"].startswith("mips-") and card["tells"] and card["notes"]


def test_error_classes_have_a_pattern_or_a_key_and_known_fixes() -> None:
    classes = _toml(KNOWLEDGE / "errors.toml")["class"]
    assert {"decl-conflict", "header-disagreement", "undeclared", "not-exact"} == set(classes)
    for name, row in classes.items():
        assert "match" in row or "key" in row, name
        if "match" in row:
            re.compile(row["match"])
        assert row["what"] and "fix" in row and row.get("autofix", "") in FIXES, name


def test_display_command_table_is_plain_data() -> None:
    table = _toml(ROOT / "tables" / "gbi.toml")
    commands = table["command"]
    assert len(commands) > 50
    assert all(isinstance(c["macro"], str) and 0 <= c["opcode"] <= 255 for c in commands)
