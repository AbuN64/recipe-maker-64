"""The index: its key follows every input, and a second get does no pool work."""

from dataclasses import replace
from types import SimpleNamespace

from recipemaker import headers, index, ledger


def build_world(fakes, monkeypatch, texts):
    for name, text in texts.items():
        path = fakes.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(
        headers, "sources", lambda snap, place="include", suffix=".h": [n for n in texts if n.startswith(place)]
    )


def shape_of(tmp_path, *rows):
    path = tmp_path / f"{len(list(tmp_path.iterdir()))}.s"
    path.write_text("".join(f"    /* {i * 4:X} 8000{i:04X} 00000000 */  {row}\n" for i, row in enumerate(rows)))
    return index._scan((str(path), "x", ""))[1]


def test_a_shape_needs_evidence_beyond_the_mnemonics_and_copies_on_other_data_share_it(tmp_path):
    stub, other = shape_of(tmp_path, "jr         $ra", "nop"), shape_of(tmp_path, "jr         $ra", "nop")
    assert stub == other == ""  # nothing but mnemonics: every stub would match every other
    on_a = shape_of(tmp_path, "lui        $a0, %hi(D_A)", "lw         $v0, %lo(D_A)($a0)", "jr         $ra")
    on_b = shape_of(tmp_path, "lui        $a0, %hi(D_B)", "lw         $v0, %lo(D_B)($a0)", "jr         $ra")
    split = shape_of(tmp_path, "lui        $a0, %hi(D_A)", "lw         $v0, %lo(D_B)($a0)", "jr         $ra")
    assert on_a == on_b != split != ""
    one = shape_of(tmp_path, "addiu      $v0, $a0, 0x18", "jr         $ra")
    assert one == shape_of(tmp_path, "addiu      $v0, $a0, 0x18", "jr         $ra") != ""
    assert one != shape_of(tmp_path, "addiu      $v0, $a0, 0x20", "jr         $ra")
    assert shape_of(tmp_path, "jal        a", "jr         $ra") != shape_of(tmp_path, "jal        b", "jr         $ra")


def test_next_reads_declarations_alone_and_each_part_is_built_once_per_command(fakes, monkeypatch):
    calls = []
    monkeypatch.setattr(index.headers, "catalog", lambda snap: calls.append("catalog") or {"f": ("h.h", 1, "")})
    monkeypatch.setattr(index.cparse, "load", lambda snap: calls.append("load") or [])
    monkeypatch.setattr(index, "_callers", lambda snap: calls.append("callers") or {})
    idx = fakes.real_index_get(fakes.snapshot)
    assert idx.declarations == idx.declarations == {"f": ("h.h", 1, "")}
    assert calls == ["catalog"]  # no views, identities or callers for `next`
    assert idx.callers == idx.callers == {} and calls == ["catalog", "callers"]


def test_callers_and_uses_come_from_the_assembly_scans(fakes, monkeypatch):
    rows = [
        ("f", "", ("g",), (("lw", "D_1", 4), ("lw", "D_80002004", 0), ("sw", "D_9", 0))),
        ("g", "", (), ()),
    ]
    monkeypatch.setattr(index, "_scans", lambda snap: rows)
    snapshot = replace(fakes.snapshot, versions={"a": SimpleNamespace(symbols={"D_1": 0x1000, "D_2": 0x80002000})})
    assert index._callers(snapshot) == {"g": ("f",)}
    assert index._uses(snapshot) == {"D_1": (("f", "lw", 4),), "D_2": (("f", "lw", 4),)}


def test_a_scan_reads_the_calls_and_each_lo_access_with_its_offset(tmp_path):
    path = tmp_path / "f.s"
    path.write_text(
        "    /* 0 80000000 0C000000 */  jal        g\n"
        "    /* 4 80000004 8C42372C */  lw         $v0, %lo(D_800E372C)($v0)\n"
        "    /* 8 80000008 AC22372C */  sw         $v0, %lo(D_1 + 0x8)($at)\n"
    )
    name, _, calls, accesses = index._scan((str(path), "f", ""))
    assert (name, calls) == ("f", ("g",))
    assert accesses == (("lw", "D_800E372C", 0), ("sw", "D_1", 8))


def test_scope_sees_the_globals_of_included_headers_transitively_and_other_record_globals_by_address(monkeypatch):
    table = {
        "include/a.h": ({"gA": ledger.Global("Screen", 1, False, "include/a.h")}, ("include/b.h",)),
        "include/b.h": ({"gB": ledger.Global("Game", 0, False, "include/b.h")}, ("include/a.h",)),
        "include/c.h": (
            {"gC": ledger.Global("Reply", 0, False, "include/c.h"), "gP": ledger.Global("Reply", 1, False)},
            (),
        ),
    }
    monkeypatch.setattr(index, "get", lambda snap: SimpleNamespace(globals=table, addresses={"gC": 4}))
    found = index.scope(None, '#include "a.h"\nvoid f(void) {\n}\n')
    assert set(found.globals) == {"gA", "gB"} and set(found.elsewhere) == {"gC"} and found.addresses == {"gC": 4}
