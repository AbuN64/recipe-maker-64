"""`look`: each kind of answer and what a miss says."""

from dataclasses import replace
from types import SimpleNamespace

from unbake import types

from recipemaker import cparse, index, ledger, look

VIEW = cparse.Layout(
    "Actor", "include/actor.h", (cparse.Field("x0", 0, 4, 1, "i"), cparse.Field("hp", 0x10, 4, 1, "f"))
)


def world(fakes, monkeypatch, externs=None, usage=None, conflicts=()):
    rows = {
        "function": {"func_a": {"signature": "void func_a(Actor *a)", "evidence": "landed"}},
        "global": {"gCount": {"declaration": "extern s32 gCount;", "evidence": "authored"}},
        "struct": {},
    }
    monkeypatch.setattr(types, "load", lambda snap: rows)
    found = (ledger.Identity((VIEW,), VIEW.fields),)
    idx = SimpleNamespace(
        declarations={
            "func_a": ("include/f.h", 7, "void func_a(Actor *a);"),
            "gCount": ("include/f.h", 9, "extern s32 gCount;"),
        },
        identities=found,
        callers={"func_a": ("func_b",)},
    )
    monkeypatch.setattr(index, "get", lambda snap: idx)
    monkeypatch.setattr(look.headers, "externs", lambda snap: externs or {})
    monkeypatch.setattr(look.types, "usage_at", lambda snap, name: usage or {})
    monkeypatch.setattr(look.types, "conflicts", lambda snap: list(conflicts))


def test_a_function_answers_with_prototype_where_and_callers(fakes, monkeypatch):
    world(fakes, monkeypatch)
    got = look.look(fakes.config, "func_a")
    assert got["kind"] == "function" and got["text"] == "void func_a(Actor *a);"
    assert got["where"] == "include/f.h:7" and got["seen_in"] == ["func_b"] and got["evidence"] == "landed"


def test_a_type_lists_the_merged_fields(fakes, monkeypatch):
    world(fakes, monkeypatch)
    got = look.look(fakes.config, "Actor")
    assert got["kind"] == "type" and [f["name"] for f in got["fields"]] == ["x0", "hp"]


def test_a_global_answers_with_its_declaration(fakes, monkeypatch):
    world(fakes, monkeypatch)
    got = look.look(fakes.config, "gCount")
    assert got["kind"] == "global" and got["text"] == "extern s32 gCount;"


def test_an_unknown_name_says_so(fakes, monkeypatch):
    world(fakes, monkeypatch)
    got = look.look(fakes.config, "nothing")
    assert got["kind"] == "unknown" and "nothing is not defined in the project" in got["text"]


def test_a_type_defined_only_in_a_landed_source_file_says_it_needs_a_header_change(fakes, monkeypatch):
    world(fakes, monkeypatch)
    only = cparse.Layout("Owner", "src/func_x.c", (VIEW.fields[0],))
    found = (ledger.Identity((only,), only.fields),)
    monkeypatch.setattr(
        index, "get", lambda snap: SimpleNamespace(declarations={}, identities=found, callers={}, shapes={}, extents={})
    )
    got = look.look(fakes.config, "Owner")
    assert "declared only in the source src/func_x.c and in no header" in got["text"]
    assert "needs-header-change" in got["text"] and "never redefine it locally" in got["text"]


def test_a_global_a_landed_source_declares_is_found(fakes, monkeypatch):
    world(fakes, monkeypatch, externs={"D_1": [("src/a.c", 5, "struct Cup")]})
    got = look.look(fakes.config, "D_1")
    assert got["kind"] == "global" and got["where"] == "src/a.c:5"
    assert "declared in the landed source src/a.c:5 as `struct Cup`" in got["text"]


def test_a_global_with_a_types_row_also_lists_where_landed_source_declares_it(fakes, monkeypatch):
    world(fakes, monkeypatch, externs={"gCount": [("src/a.c", 1, "s32")]})
    text = look.look(fakes.config, "gCount")["text"]
    assert text.startswith("extern s32 gCount;") and "declared in the landed source src/a.c:1" in text


def test_a_function_no_header_declares_says_to_declare_its_prototype_in_the_file(fakes, monkeypatch):
    world(fakes, monkeypatch)
    got = look.look(fakes.config, "func_b")
    assert got["kind"] == "unknown" and "no header declares it; declare its prototype in your file" in got["text"]


def test_a_global_only_a_header_declares_is_a_global_not_a_function(fakes, monkeypatch):
    world(fakes, monkeypatch)
    index.get(None).declarations["gDev"] = ("include/d.h", 3, "extern DeviceState gDev;")
    got = look.look(fakes.config, "gDev")
    assert got["kind"] == "global" and got["where"] == "include/d.h:3"


def test_a_global_answer_shows_the_largest_offset_the_leased_item_reaches(fakes, monkeypatch):
    world(fakes, monkeypatch)
    monkeypatch.setattr(
        look.index, "usage", lambda snap, item: {"addresses": [{"address": 0x1074, "base": ("gCount", 0x74)}]}
    )
    member = fakes.snapshot.layout.members["func_a"]
    member = replace(member, placements=tuple(p for p in member.placements if p.version == "b"))
    fakes.snapshot = replace(
        fakes.snapshot,
        layout=replace(fakes.snapshot.layout, members={"func_a": member}),
        versions={"a": SimpleNamespace(symbols={"gCount": 0x2000}), "b": SimpleNamespace(symbols={"gCount": 0x1000})},
    )
    monkeypatch.setattr(look.layout, "capture", lambda config: fakes.snapshot)
    got = look.look(fakes.config, "gCount", "func_a")
    assert "gCount the leased function reaches +0x74" in got["text"]
    assert got["where"] == "types.toml (authored)"


def test_a_global_lists_its_versions_conflicts_and_usage_rows(fakes, monkeypatch):
    row = {
        "address": 0x1004,
        "how": "abs",
        "width": 4,
        "readers": [("func_a", True)],
        "spellings": [("src/b.c", 2, "s32")],
        "base": None,
        "span": {"writer": "func_w", "stores": 5, "fills": 1, "size": 0x40},
    }
    bad = SimpleNamespace(unit="gCount", missing=("gCount: a.h:1 s32", "gCount: b.c:2 f32"), symptoms={})
    world(fakes, monkeypatch, usage={"a": [row]}, conflicts=[bad], externs={"gCount": [("src/b.c", 2, "f32")]})
    fakes.snapshot = replace(fakes.snapshot, versions={"a": SimpleNamespace(symbols={"gCount": 0x1000})})
    monkeypatch.setattr(look.layout, "capture", lambda config: fakes.snapshot)
    monkeypatch.setattr(look.types, "usage_at", lambda snap, name: {"a": [row]})
    got = look.look(fakes.config, "gCount")["facts"]
    assert "conflict: gCount: b.c:2 f32" in got and "authoritative: none: no landed source defines it" in got
    assert any(x.startswith("named in: a") for x in got)
    assert any("(landed)" in x and "written together by func_w: 5 stores, 1 fills" in x for x in got)


def test_look_ignores_nonmatching_saves_under_src_fuzzy(fakes, monkeypatch):
    world(fakes, monkeypatch, externs={"gFuzz": [("src/fuzzy/f.c", 1, "s32")], "gReal": [("src/real.c", 1, "s32")]})
    assert look.look(fakes.config, "gFuzz")["kind"] == "unknown"
    assert look.look(fakes.config, "gReal")["kind"] == "global"


def test_an_undefined_name_gets_the_nearest_known_names(fakes, monkeypatch):
    world(fakes, monkeypatch)
    assert "nearest known: Actor" in look.look(fakes.config, "Acter")["text"]
