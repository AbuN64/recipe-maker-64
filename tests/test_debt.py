"""Declaration conflicts and their ranking by the skipped items they block."""

from unbake.contracts import Finding

from recipemaker import declare, state

NOW = 1_000_000.0


def conflict(name, *spellings):
    return Finding("types.conflict", name, unit=name, missing=tuple(f"{name}: {s}" for s in spellings))


def test_spellings_are_read_as_file_line_and_text():
    got = declare.spellings(("gX: src/a.c:3 s32 gX;", "gX: include/a.h:9 extern f32 gX;", "not a spelling"))
    assert got == [("src/a.c", 3, "s32 gX;"), ("include/a.h", 9, "extern f32 gX;")]


def test_conflicts_are_ranked_by_the_skipped_items_they_wait_on(fakes, monkeypatch):
    for item, names in (("f1", ["D_A", "D_B"]), ("f2", ["D_B"]), ("f3", ["gX"])):
        state.skip(fakes.config, item, "needs-header-change", "", 0.9, NOW, "", {n: "" for n in names})
    found = [conflict("gX", "src/a.c:3 s32 gX;"), conflict("D_B", "a.h:1 s32 D_B;"), conflict("D_Z", "b.h:2 u8 D_Z;")]
    monkeypatch.setattr(declare.headers, "disagreements", lambda snap: ({}, found))
    got = declare.conflicts(fakes.config, fakes.snapshot)
    assert [(r["name"], r["items"]) for r in got] == [("D_B", ["f1", "f2"]), ("gX", ["f3"]), ("D_Z", [])]
    assert got[1]["spellings"] == [("src/a.c", 3, "s32 gX;")]


def test_a_definition_conflict_carries_the_definition_and_ranks_by_consumers_after_blocked_items(fakes, monkeypatch):
    fakes_found = Finding(
        "types.conflict",
        "func_a",
        unit="func_a",
        missing=("func_a: src/f.c:1 s32 func_a(s32 a)", "func_a: include/f.h:2 s32 func_a(void);"),
        symptoms={"definition": {"path": "src/f.c", "line": 1, "signature": "s32 func_a(s32 a)"}, "consumers": 5},
    )
    busy = Finding("types.conflict", "gX", unit="gX", missing=("gX: a.h:1 s32 gX;",), symptoms={"consumers": 1})
    monkeypatch.setattr(declare.headers, "disagreements", lambda snap: ({}, [busy, fakes_found]))
    got = declare.conflicts(fakes.config, fakes.snapshot)
    assert [r["name"] for r in got] == ["func_a", "gX"]
    assert got[0]["definition"] == ("src/f.c", 1, "s32 func_a(s32 a)") and got[0]["consumers"] == 5
    assert got[1]["definition"] is None
