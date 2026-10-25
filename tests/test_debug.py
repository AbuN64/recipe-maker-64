"""The debug catalog as evidence: original records read with their member sizes, project records matched by size and
member offsets, type moves only for invented records with one strong match, `debug NAME` naming evidence placed by
address, and source files as module boundaries."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from r1_fake_api import fake_api  # noqa: F401

from recipemaker import debug, naming, state, structure
from recipemaker.cparse import Field, Layout


def field(name, offset, type="int", **more):
    return {"name": name, "offset": offset, "size": 0, "type": type, **more}


def record(name, size, fields, kind="struct", where=None):
    return [{"def": {"name": name, "kind": kind, "size": size, "fields": fields}, "where": where or {"us": [64, 1]}}]


def typedef(name, spelled):
    return [{"def": {"name": name, "kind": "typedef", "size": 0, "type": spelled}, "where": {"us": [8, 1]}}]


def catalog_document():
    types = {
        "CVector3_t": record(
            "CVector3_t", 12, [field("x", 0, "float"), field("y", 4, "float"), field("z", 8, "float")]
        ),
        "CVector3": typedef("CVector3", "struct CVector3_t"),
        "CWarp_t": record(
            "CWarp_t",
            20,
            [
                {"name": "m_vPos", "offset": 0, "size": 12, "type": "struct CVector3_t"},
                field("m_RotY", 12, "float"),
                field("m_nLevel", 16, "short"),
                field("m_nRegion", 18, "short"),
            ],
        ),
        "CWarp": typedef("CWarp", "struct CWarp_t"),
        "CLink_t": record("CLink_t", 12, [field("m_pNext", 0, "struct CLink_t *"), field("m_Name", 4, "char [8]")]),
        "CLink": typedef("CLink", "struct CLink_t"),
        ".7fake": record(".7fake", 8, [field("cmd", 0, "int", bit=0, bits=8), field("addr", 4, "unsigned int")]),
        "GfxCmd": typedef("GfxCmd", "struct .7fake"),
        "COther_t": record(
            "COther_t", 20, [field("a", 0), field("b", 4), field("c", 8), field("d", 12), field("e", 16)]
        ),
        "COther": typedef("COther", "struct COther_t"),
        "CTwin_t": record("CTwin_t", 20, [field("v", 0), field("w", 4), field("x", 8), field("y", 12), field("z", 16)]),
        "CTwin": typedef("CTwin", "struct CTwin_t"),
    }
    functions = [
        {"name": "Warp_Init", "address": 0x1000, "file": "C:\\GAME\\SOURCE\\WORLD\\WARP.CPP", "line": 10, "frame": 24,
         "params": [{"name": "pThis", "type": "struct CWarp_t *"}]},
        {"name": "Warp_Exit", "address": 0x1040, "file": "C:\\GAME\\SOURCE\\WORLD\\WARP.CPP", "line": 40, "frame": 0,
         "params": []},
        {"name": "Ai_Think", "address": 0x2000, "file": "C:\\GAME\\SOURCE\\AI\\THINK.CPP", "line": 5, "frame": 8,
         "params": []},
    ]  # fmt: skip
    streams = [
        {"dialect": "sym", "functions": functions, "globals": [
            {"global": "Warp_Init", "address": 0x1000, "type": "void ()"},
            {"global": "WarpTable", "address": 0x80005000, "type": "struct CWarp_t [4]"},
        ]},
        {"dialect": "obj", "functions": [{"name": "Pak_Open", "address": 0, "file": "", "frame": 8, "params": []}],
         "globals": []},
    ]  # fmt: skip
    return {"schema": 1, "types": types, "versions": {"us": {"rom_sha256": "aa" * 32, "streams": streams}}}


@pytest.fixture
def catalog():
    return debug.read(catalog_document(), "debug-types.json")


def f(name, offset, size=4, kind="i", count=1, type=""):
    return Field(name, offset, size, count, kind, type)


def layout(name, size, fields, records=(), pads=(), path="include/a.h"):
    return Layout(name, path, tuple(fields), tuple(pads), tuple(records), size)


WARP_VIEW = layout(
    "func_80001000_S1",
    20,
    [f("x", 0, kind="f"), f("y", 4, kind="f"), f("z", 8, kind="f"), f("rot", 12, kind="f"), f("unk10", 16, 2)],
    pads=[f("pad12", 18, 1, count=2)],
)


def test_read_spells_records_by_their_typedef_and_sizes_members(catalog):
    by = {r.name: r for r in catalog.records}
    assert set(by) == {"CVector3", "CWarp", "CLink", "GfxCmd", "COther", "CTwin"}
    warp = {s.name: (s.size, s.kind) for s in by["CWarp"].slots}
    assert warp == {"m_vPos": (12, "s"), "m_RotY": (4, "f"), "m_nLevel": (2, "i"), "m_nRegion": (2, "i")}
    link = {s.name: (s.size, s.kind) for s in by["CLink"].slots}
    assert link == {"m_pNext": (4, "p"), "m_Name": (8, "i")}
    assert by["GfxCmd"].tag == ".7fake" and by["GfxCmd"].slots[0].size == 1  # a whole-byte bit field
    assert by["CWarp"].where == "us rom 0x40"
    funcs = {fn.name: fn for fn in catalog.functions}
    assert funcs["Warp_Init"].vram == 0x80001000 and funcs["Pak_Open"].vram is None  # an object value is an offset
    assert [g.name for g in catalog.globals] == ["WarpTable"]  # a function's own global row is not a datum


def test_read_refuses_a_document_of_another_schema():
    with pytest.raises(Exception, match="schema-1"):
        debug.read({"schema": 2, "types": {}})


def test_a_project_record_agrees_member_by_member_including_elements_of_a_record_member(catalog):
    found = debug.matches([WARP_VIEW], catalog)
    one = debug.unique(found["func_80001000_S1"])
    assert one is not None and one.record.name == "CWarp"
    assert one.fields == (("x", "m_vPos.x", 0), ("y", "m_vPos.y", 4), ("z", "m_vPos.z", 8), ("rot", "m_RotY", 12),
                          ("unk10", "m_nLevel", 16))  # fmt: skip
    assert one.agreed == 2 and one.total == 5 and not debug.strong(one)  # vector parts are not whole members
    assert "debug-type CWarp: size 0x14" in one.evidence()


def test_a_float_never_stands_for_an_integer_and_an_ambiguous_match_proposes_nothing(catalog):
    ints = layout("func_80002000_S1", 20, [f("a", 0), f("b", 4), f("c", 8), f("d", 12), f("e", 16)])
    found = debug.matches([ints], catalog)["func_80002000_S1"]
    assert {m.record.name for m in found} == {"COther", "CTwin"}  # CWarp starts with floats
    assert debug.unique(found) is None
    floats = layout("V", 20, [f("a", 0, kind="f"), f("b", 4, kind="i"), f("c", 8, kind="f"), f("d", 12, kind="i")])
    assert "V" not in debug.matches([floats], catalog)  # m_vPos.y is a float: no record agrees


def plan(catalog, layouts, taken=(), texts=None):
    return debug.plan(layouts, catalog, taken, lambda p: (texts or {}).get(p, ""))


WHOLE = layout(
    "Rec14",
    20,
    [f("pos", 0, 12, "s", type="CVector3"), f("rot", 12, kind="f"), f("level", 16, 2), f("region", 18, 2)],
    records=(),
)


def test_an_invented_record_with_one_strong_match_takes_the_original_type_and_tag(catalog):
    text = "typedef struct Rec14 Rec14;\nstruct Rec14 {\n    CVector3 pos;\n};\n"
    got = plan(catalog, [WHOLE], texts={"include/a.h": text})
    member = {"op": "field", "path": "include/a.h", "tag": "CWarp_t", "alias": "CWarp"}
    assert got.moves == (
        {"op": "type", "subject": "Rec14", "target": "CWarp"},
        {"op": "type", "subject": "struct CWarp", "target": "struct CWarp_t"},
        {**member, "subject": "pos", "target": "m_vPos"},
        {**member, "subject": "rot", "target": "m_RotY"},
        {**member, "subject": "level", "target": "m_nLevel"},
        {**member, "subject": "region", "target": "m_nRegion"},
    )
    row = got.proposals[0]
    assert row["moved"] and row["original"] == "CWarp" and row["tag"] == "CWarp_t"
    assert [m["original"] for m in row["members"]] == ["m_vPos", "m_RotY", "m_nLevel", "m_nRegion"]


def test_a_typedef_with_its_own_tag_moves_the_tag_too(catalog):
    text = "typedef struct Rec14_s {\n    int a;\n} Rec14;\n"
    got = plan(catalog, [WHOLE], texts={"include/a.h": text})
    assert got.moves[1] == {"op": "type", "subject": "Rec14_s", "target": "CWarp_t"}


def test_a_member_spelling_another_record_has_too_is_still_a_field_move_for_the_typed_build(catalog):
    other = layout("Other", 8, [f("level", 0), f("b", 4)], path="include/b.h")
    got = plan(catalog, [WHOLE, other])
    assert [m["subject"] for m in got.moves if m["op"] == "field"] == ["pos", "rot", "level", "region"]


def test_an_original_record_with_placeholder_members_takes_only_the_member_names(catalog):
    already = replace(WHOLE, name="struct CWarp_t", fields=(f("unk0", 0, 12, "s"), *WHOLE.fields[1:]))
    got = plan(catalog, [already])
    assert [(m["op"], m["subject"], m["target"], m["tag"], m["alias"]) for m in got.moves] == [
        ("field", "unk0", "m_vPos", "CWarp_t", ""),
        ("field", "rot", "m_RotY", "CWarp_t", ""),
        ("field", "level", "m_nLevel", "CWarp_t", ""),
        ("field", "region", "m_nRegion", "CWarp_t", ""),
    ]


def test_the_field_move_renames_the_declarator_and_every_access_but_no_other_word(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include/a.h").write_text(
        "typedef struct {\n    s32 level;\n    s32 b;\n} CWarp;\nstruct Keep {\n    s32 b;\n};\nextern s32 level;\n"
    )
    (tmp_path / "src/a.c").write_text("void f(CWarp *w, CWarp v) { w->level = v.level + level; w -> level++; }\n")
    move = {"op": "field", "subject": "level", "target": "m_nLevel", "path": "include/a.h", "tag": "", "alias": "CWarp"}
    built = structure.build(tmp_path, [move], lambda name: None, {})
    assert built["moves"] == 1 and built["files"] == ["include/a.h", "src/a.c"]
    assert "s32 level;" in (tmp_path / "include/a.h").read_text()  # the build reads, never writes
    assert (
        "s32 m_nLevel;" in built["patch"]
        and "+void f(CWarp *w, CWarp v) { w->m_nLevel = v.m_nLevel + level;" in built["patch"]
    )
    assert "w -> m_nLevel++" in built["patch"] and "extern s32 level;" not in built["patch"]


@pytest.mark.parametrize(
    ("case", "why"),
    [("named", "named here"), ("taken", "already a name in the project")],
)
def test_no_move_without_an_invented_name_a_free_original_name_and_one_claimant(catalog, case, why):
    layouts = {
        "named": [replace(WHOLE, name="WarpPoint")],
        "taken": [WHOLE],
    }[case]
    got = plan(catalog, layouts, ("CWarp",) if case == "taken" else ())
    assert got.moves == () and got.proposals and all(why in p["why"] and not p["moved"] for p in got.proposals)


def test_two_invented_records_matching_one_original_merge_the_second_into_the_first(catalog):
    twin = replace(WHOLE, name="Rec14B", path="include/b.h")
    got = plan(catalog, [WHOLE, twin])
    assert [m["op"] for m in got.moves].count("type") == 2 and got.moves[-1]["op"] == "merge"
    assert got.moves[-1] | {"subject": "Rec14B", "target": "CWarp", "into_tag": "CWarp_t"} == got.moves[-1]
    assert [p["why"] for p in got.proposals] == ["", "merged into Rec14"]


def test_a_named_record_with_a_whole_heavy_match_follows_it_but_a_light_one_does_not(catalog):
    heavy = replace(WHOLE, name="WarpPoint", fields=(f("a", 0, 12, "s"), f("b", 12, kind="f"), *WHOLE.fields[2:]))
    assert not debug.whole(debug.matches([heavy], catalog)["WarpPoint"][0])


def test_a_record_already_spelled_as_the_original_is_left_alone(catalog):
    got = plan(catalog, [replace(WHOLE, name="struct CWarp_t")])
    assert {m["op"] for m in got.moves} == {"field"} and got.proposals[0]["why"] == "already the original name"


def test_invented_names_are_numbered_padding_or_placeholder_records():
    assert debug.invented(layout("Block40", 8, [f("a", 0), f("b", 4)]))
    assert debug.invented(layout("Thing", 8, [f("unk0", 0), f("field_4", 4)]))
    assert debug.invented(layout("Thing", 16, [f("a", 0)], pads=[f("pad4", 4, 1, count=12)]))
    assert not debug.invented(layout("Thing", 8, [f("count", 0), f("next", 4)]))


def test_boundaries_give_one_row_per_source_file_below_the_shared_folder(catalog):
    def fn(name, vram):
        place = {"version": "us", "section": ".text", "vram": vram, "rom_start": vram - 0x80000000}
        return {"name": name, "kind": "function", "placements": [place]}

    doc = {
        "members": [fn("func_80001000", 0x80001000), fn("func_80001040", 0x80001040), fn("func_80002000", 0x80002000)]
    }
    rows = debug.boundaries(catalog, doc)
    assert rows == [
        {"first": "func_80002000", "last": "func_80002000", "source": "AI/THINK.CPP", "functions": 1},
        {"first": "func_80001000", "last": "func_80001040", "source": "WORLD/WARP.CPP", "functions": 2},
    ]
    assert debug.root_of(["C:\\GAME\\SOURCE\\A\\X.CPP", "C:\\GAME\\SOURCE\\B.CPP"]) == "C:/GAME/SOURCE/"


def test_the_catalog_path_comes_from_the_inputs_table_relative_to_the_project(tmp_path):
    conf = tmp_path / "recipe.toml"
    conf.write_text('[inputs]\ndebug_types = "ev/debug-types.json"\n')
    config = SimpleNamespace(project=SimpleNamespace(root=tmp_path / "game"))
    assert debug.path(config, conf) == tmp_path / "game/ev/debug-types.json"
    assert state.inputs(tmp_path / "missing.toml") == {}
    with pytest.raises(Exception, match="does not exist"):
        debug.load(config, conf)
    (tmp_path / "game/ev").mkdir(parents=True)
    (tmp_path / "game/ev/debug-types.json").write_text(json.dumps(catalog_document()))
    assert {r.name for r in debug.load(config, conf, {"us": "aa" * 32}).records} >= {"CWarp"}


def test_a_catalog_of_another_rom_is_ignored_and_only_the_projects_versions_name_by_address(tmp_path, capsys):
    # BT has a `us` too: RW's catalog from the rig-global recipe.toml must never name BT functions by address
    conf = tmp_path / "recipe.toml"
    conf.write_text('[inputs]\ndebug_types = "debug-types.json"\n')
    config = SimpleNamespace(project=SimpleNamespace(root=tmp_path))
    document = catalog_document()
    document["versions"]["de"] = {"rom_sha256": "dd" * 32, "streams": document["versions"]["us"]["streams"]}
    (tmp_path / "debug-types.json").write_text(json.dumps(document))
    assert debug.load(config, conf, {"us": "bb" * 32}) is None
    assert "is ignored: no version's ROM matches" in capsys.readouterr().err
    both = debug.load(config, conf, {"us": "aa" * 32, "de": "ee" * 32})
    assert both is not None and {f.version for f in both.functions} == {"us"}  # de's ROM is another: its rows go
    assert debug.own({"versions": {"us": {"streams": []}}}, {"us": "aa" * 32}) is None  # no sha recorded: untrusted


def doc_with(name, vram, kind="function", folder="world"):
    place = {"version": "us", "section": ".text" if kind == "function" else ".data", "rom_start": vram & 0xFFFFFF,
             "rom_end": (vram & 0xFFFFFF) + 0x40, "vram": vram}  # fmt: skip
    source = f"src/game/main/{folder}/code_{vram:08X}.c"
    member = {"name": name, "kind": kind, "module": f"code_{vram:08X}", "unit": None, "segment": "main",
              "placements": [place]}  # fmt: skip
    module = {"name": f"code_{vram:08X}", "segment": "main", "source": source,
              "header": source.replace("src/", "include/").replace(".c", ".h"), "members": [name]}  # fmt: skip
    return member, module


@pytest.fixture
def named(tmp_path, monkeypatch, catalog):
    members, modules = zip(
        doc_with("func_80001000", 0x80001000),
        doc_with("Warp_Begin", 0x80001040),
        doc_with("D_80005000", 0x80005000, kind="data"),
        doc_with("func_80003000", 0x80003000),
        strict=True,
    )
    doc = {"names_from": "us", "versions": ["us"], "members": list(members), "modules": list(modules), "units": []}
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    monkeypatch.setattr(naming, "reads", lambda config, d: {})
    monkeypatch.setattr(naming, "_tables", lambda config, versions: {"us": {}})
    monkeypatch.setattr(debug, "load", lambda config, source=None: catalog)
    config = SimpleNamespace(project=SimpleNamespace(root=tmp_path), ops=[])

    def spy(cfg, d, moves):
        config.ops += moves
        return {"patch": "", "files": [], "moves": len(moves), "path": ""}

    monkeypatch.setattr(naming, "_patch", spy)
    return config


def submit(project, rows):
    path = project.project.root / "names-1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    params = {"host": None, "action": "submit", "step": None, "count": 40, "file": str(path)}
    return naming.run(project, params)


def test_debug_evidence_names_by_address_in_the_original_spelling_over_a_name_given_here(named):
    rows = [
        {"target": "func_80001000", "kind": "function", "name": "Warp_Init", "evidence": ["debug Warp_Init"],
         "doc": "Sets up a warp."},
        {"target": "Warp_Begin", "kind": "function", "name": "Warp_Exit", "evidence": ["debug Warp_Exit (WARP.CPP:40)"],
         "doc": "Leaves a warp."},
        {"target": "D_80005000", "kind": "global", "name": "WarpTable", "evidence": ["debug WarpTable"],
         "doc": "The warps."},
        {"target": "func_80003000", "kind": "function", "name": "World_Think", "evidence": ["debug Ai_Think"],
         "doc": "Thinks."},
        {"target": "func_80003000", "kind": "function", "name": "World_Open", "evidence": ["debug Pak_Open"],
         "doc": "Opens."},
    ]  # fmt: skip
    got = submit(named, rows)
    assert got["accepted"] == 3 and got["by_rule"] == {"unverifiable": 2}  # Ai_Think sits elsewhere, Pak_Open nowhere
    assert named.ops == [
        {"op": "symbol", "subject": "func_80001000", "target": "Warp_Init"},
        {"op": "symbol", "subject": "Warp_Begin", "target": "Warp_Exit"},  # the original replaces a given name
        {"op": "symbol", "subject": "D_80005000", "target": "WarpTable"},  # not g_ style: the original's spelling
    ]


def test_step_t_writes_the_report_and_one_patch_of_type_member_and_original_names(named, monkeypatch):
    from recipemaker import cparse

    monkeypatch.setattr(naming.api, "snapshot", lambda config: None)
    monkeypatch.setattr(cparse, "load", lambda snapshot: [WHOLE])
    (named.project.root / "include").mkdir()
    (named.project.root / "include/a.h").write_text("typedef struct Rec14 Rec14;\n")
    params = {"host": None, "action": "next", "step": "T", "count": 40, "file": None}
    got = naming.run(named, params)
    report = json.loads((named.project.root / got["pack"]).read_text())
    assert got["proposed"] == 1 and got["names"] == 3 and report["proposals"][0]["original"] == "CWarp"
    assert [m["op"] for m in named.ops] == ["type", "type", "field", "field", "field", "field", "symbol", "symbol",
                                            "symbol"]  # fmt: skip
    assert {m["target"] for m in named.ops if m["op"] == "symbol"} == {"Warp_Init", "Warp_Exit", "WarpTable"}


def test_step_t_without_a_catalog_says_how_to_configure_one(named, monkeypatch):
    monkeypatch.setattr(debug, "load", lambda config, source=None: None)
    params = {"host": None, "action": "next", "step": "T", "count": 40, "file": None}
    with pytest.raises(Exception, match="no debug catalog"):
        naming.run(named, params)


SHARED = "struct A {\n    s32 level;\n};\nstruct B {\n    s32 level;\n};\n"
MOVE_A = {"op": "field", "subject": "level", "target": "m_nLevel", "path": "include/a.h", "tag": "A", "alias": ""}


def _shared(tmp_path, code):
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include/a.h").write_text(SHARED)
    (tmp_path / "src/a.c").write_text(code)
    return structure.build(tmp_path, [MOVE_A], lambda name: None, {})


def test_the_field_move_of_a_shared_spelling_renames_only_the_accesses_typed_as_its_record(tmp_path):
    built = _shared(
        tmp_path,
        "void f(struct A *a, struct B *b) { a->level = 1; b->level = 2; g(a)->x; }\n"
        "void g(void) { struct A v; v.level = 3; }\n",
    )
    assert built["moves"] == 1 and built["skipped"] == []
    assert "a->m_nLevel = 1; b->level = 2;" in built["patch"] and "v.m_nLevel = 3" in built["patch"]
    assert "struct B {\n-    s32 level" not in built["patch"]


def test_the_field_move_of_a_shared_spelling_stops_at_an_access_with_an_untyped_base(tmp_path):
    built = _shared(tmp_path, "void f(void) { get()->level = 1; }\n")
    assert built["moves"] == 0 and "no known record type" in built["skipped"][0]["why"]


def test_a_merge_deletes_the_duplicate_and_spells_the_survivor_everywhere(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include/a.h").write_text("typedef struct A {\n    s32 x;\n} A;\n")
    (tmp_path / "include/b.h").write_text(
        "struct Dup;\ntypedef struct Dup Dup;\nstruct Dup {\n    s32 x;\n};\nextern Dup g;\n"
    )
    (tmp_path / "src/a.c").write_text("void f(struct Dup *d, Dup e) {}\n")
    move = {"op": "merge", "subject": "Dup", "target": "A", "path": "include/b.h", "tag": "Dup", "alias": "Dup"}
    move |= {"into_path": "include/a.h", "into_tag": "A", "into_alias": "A"}
    built = structure.build(tmp_path, [move], lambda name: None, {})
    assert built["moves"] == 1
    assert '+#include "a.h"' in built["patch"] and "+extern A g;" in built["patch"]
    assert "+void f(A *d, A e) {}" in built["patch"] and "-struct Dup {" in built["patch"]


def test_a_merge_after_a_refused_member_rename_does_nothing(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include/a.h").write_text(
        "struct A {\n    s32 level;\n};\nstruct B {\n    s32 level;\n};\nstruct Dup {\n    s32 b;\n};\n"
    )
    (tmp_path / "include/c.h").write_text("void f(void) { get()->level; }\n")
    rename = MOVE_A | {"tag": "Dup"}
    merge = {"op": "merge", "subject": "Dup", "target": "struct A", "path": "include/a.h", "tag": "Dup", "alias": ""}
    built = structure.build(
        tmp_path, [rename, merge | {"into_path": "include/a.h", "into_tag": "A", "into_alias": ""}], lambda n: None, {}
    )
    assert built["moves"] == 0 and "original name" in built["skipped"][1]["why"]


def test_a_loose_field_move_renames_the_typed_accesses_and_leaves_the_untyped_base_to_the_compiler(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include/a.h").write_text(SHARED)
    (tmp_path / "src/a.c").write_text("void f(struct A *a) { a->level = 1; get()->level = 2; }\n")
    built = structure.build(tmp_path, [{**MOVE_A, "loose": True}], lambda name: None, {})
    assert built["moves"] == 1 and "a->m_nLevel = 1; get()->level = 2;" in built["patch"]
