"""retext: integer-array definitions holding text become char arrays with the same bytes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_draft import USES, _snapshot, references  # noqa: F401  (references: the autouse fixture that serves USES)

from recipemaker import retext, world

TEXT = b"controller pak\0\0"
WORDS = b"anim object\0"


def _source(raw: bytes, name: str, const: bool) -> str:
    cells = ", ".join(f"0x{int.from_bytes(raw[i : i + 4], 'big'):08X}" for i in range(0, len(raw), 4))
    return f"{'const ' * const}unsigned int {name}[] = {{\n    {cells},\n}};\n"


def _snap(tmp: Path) -> Any:
    data = {"d": {"a": (0x1200, TEXT)}, "e": {"a": (0x1210, WORDS)}}
    source = _source(TEXT, "d", True) + _source(WORDS, "e", False)
    snapshot = _snapshot(tmp, {"d": "rodata"}, data=data, versions=("a",), sources={"code_80000400.c": source})
    layout = snapshot.layout
    members = {n: replace(m, landed=True) for n, m in layout.members.items()}
    unit = world.Unit("src/code_80000400.c", "c", "src/code_80000400.c", tuple(members), "gcc-test", {})
    return replace(snapshot, layout=replace(layout, members=members, units={unit.path: unit}))


def test_retext_types_text_as_char_array_and_keeps_words_read_as_words(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snap(tmp_path)
    USES["d"] = (("f", "addiu", 0), ("f", "lbu", 0))
    USES["e"] = (("g", "lw", 0),)  # the bytes are text but read as words: the array stays
    monkeypatch.setattr(world, "capture", lambda config: snapshot)
    result = retext.run(snapshot.config, {"write": True})
    assert [i["symbol"] for i in result["items"]] == ["d"]
    written = (snapshot.config.project.root / "src/code_80000400.c").read_text()
    assert 'const char d[16] = "controller pak";' in written  # the same 16 bytes: NULs as zero fill, const kept
    assert "unsigned int e[] = {\n    0x616E696D," in written
    assert "unsigned int d[]" not in written


def test_retext_points_kuseg_code_words_at_alias_rows_in_the_versions_the_block_serves(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    words = b"".join(v.to_bytes(4, "big") for v in (0x80000700, 0x00000400, 0x00000400))
    data = {"p": {"a": (0x1200, words)}, "q": {"a": (0x1300, b"\0" * 4)}}
    source = "#if defined(VERSION_A)\n" + _source(words, "p", False) + "#endif\n"
    snapshot = _snapshot(tmp_path, None, data=data, versions=("a", "b"), sources={"code_80000400.c": source})
    layout = snapshot.layout
    members = {n: replace(m, landed=True) for n, m in layout.members.items()}
    unit = world.Unit("src/code_80000400.c", "c", "src/code_80000400.c", tuple(members), "gcc-test", {})
    snapshot = replace(snapshot, layout=replace(layout, members=members, units={unit.path: unit}))
    monkeypatch.setattr(world, "capture", lambda config: snapshot)
    result = retext.run(snapshot.config, {"write": True})
    assert [(p["word"], p["alias"], p["function"]) for p in result["pointers"]] == [
        (1, "D_00000400", "func_80000400"),
        (2, "D_00000400", "func_80000400"),
    ]
    root = snapshot.config.project.root
    written = (root / "src/code_80000400.c").read_text()
    assert written.count("extern char D_00000400[];\n") == 1
    assert "0x80000700, (unsigned int)D_00000400, (unsigned int)D_00000400," in written
    assert "D_00000400 = 0x00000400; //\n" in (root / "versions/a/symbol_addrs.txt").read_text()
    assert "D_00000400" not in (root / "versions/b/symbol_addrs.txt").read_text()  # the block serves a only
    again = retext.run(snapshot.config, {})
    assert again["pointers"] == [] or all(p["alias"] == "D_00000400" for p in again["pointers"])
