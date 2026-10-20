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
