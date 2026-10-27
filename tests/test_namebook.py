"""The name ledger: rows named by stable keys, judged against the tree plus the queued rows; no tree is written."""

import json
from types import SimpleNamespace

import pytest
from r1_fake_api import fake_api  # noqa: F401

from recipemaker import keys, namebook, naming, structure

GRAPH = {
    "func_80004010": (["func_80004020"], ["D_80005000"]),
    "func_80004020": ([], []),
    "func_80004030": (["func_80004020"], []),
}


def member(name, vram, kind="function", module="code_80004000", unit="src/c.c"):
    place = {
        "version": "us",
        "section": ".text",
        "rom_start": vram & 0xFFFFFF,
        "rom_end": (vram & 0xFFFFFF) + 0x40,
        "vram": vram,
    }
    return {
        "name": name,
        "kind": kind,
        "module": module,
        "unit": unit if kind == "function" else None,
        "segment": "main",
        "placements": [place],
    }


def document():
    members = [
        member("func_80004010", 0x80004010),
        member("func_80004020", 0x80004020),
        member("func_80004030", 0x80004030),
        member("D_80005000", 0x80005000, kind="data"),
        member("Actor_Update", 0x80004040),
    ]
    base = "src/game/main/actor/code_80004000.c"
    mod = {
        "name": "code_80004000",
        "segment": "main",
        "source": base,
        "header": "include/game/main/actor/code_80004000.h",
        "members": [m["name"] for m in members],
    }
    return {"names_from": "us", "versions": ["us"], "members": members, "modules": [mod], "units": []}


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMES_STATE", str(tmp_path / "names"))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: document())
    monkeypatch.setattr(naming, "reads", lambda config, doc: dict(GRAPH))
    monkeypatch.setattr(naming, "_tables", lambda config, versions: {"us": {}})
    root = tmp_path / "tree"
    (root / "src").mkdir(parents=True)
    (root / "src/c.c").write_text("void func_80004010(void) {\n    s32 sp1C;\n    sp1C = 1;\n}\n")
    return SimpleNamespace(project=SimpleNamespace(root=root), tmp=tmp_path)


def submit(book, lane, rows):
    path = book.tmp / "rows.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return naming.run(book, {"host": None, "action": "submit", "file": str(path), "ledger": lane, "no_apply": True})


def fn(key, name, evidence=("callee func_80004020",), **more):
    return {"key": key, "name": name, "doc": "Does a thing.", "evidence": list(evidence), **more}


def test_keys_parse_canonicalize_and_resolve_the_short_form():
    assert keys.parse("fn:80123456", "us-rev1").text() == "fn:us-rev1:80123456"
    assert keys.parse("datum:de:800a1234", "us-rev1").text() == "datum:de:800A1234"
    assert keys.parse("member:CPlayerPool_t+0x1c", "us").text() == "member:CPlayerPool_t+0x1c"
    assert keys.parse("slot:80123456:sp1C", "us").text() == "slot:us:80123456:sp1C"
    assert keys.parse("func_80123456", "us") is None
    assert keys.of_target("function", "func_80123456_de", "us") == "fn:de:80123456"
    assert keys.of_target("global", "D_80123456", "us") == "datum:us:80123456"
    assert keys.of_target("function", "Actor_Update", "us") == ""
    assert keys.member_old({"unk1C": "s32", "unk20": "s32"}, 0x1C) == "unk1C"
    assert keys.member_old({"unk1C": "s32"}, 0x20) == ""


def test_a_key_row_is_queued_and_writes_nothing_else(book):
    got = submit(book, "L1", [fn("fn:us:80004010", "Actor_Spawn")])
    assert (got["accepted"], got["refused"], got["queued"], got["exit"]) == (1, 0, 1, 0)
    line = namebook.ledger()[0]
    assert (line["seq"], line["lane"], line["key"], line["name"]) == (1, "L1", "fn:us:80004010", "Actor_Spawn")
    assert line["row"]["target"] == "func_80004010" and line["confidence"] == "high"
    assert [p.name for p in book.project.root.rglob("*") if p.is_file()] == ["c.c"]  # no patch, no banners in the tree


def test_the_short_key_form_resolves_to_the_names_from_version(book):
    assert submit(book, "L1", [fn("fn:80004010", "Actor_Spawn")])["accepted"] == 1
    assert namebook.ledger()[0]["key"] == "fn:us:80004010"


def test_a_queued_key_is_pending_under_another_name_and_already_under_the_same(book):
    submit(book, "L1", [fn("fn:us:80004010", "Actor_Spawn")])
    other = submit(book, "L2", [fn("fn:us:80004010", "Actor_Start")])
    assert other["by_rule"] == {"pending": 1} and "fn:us:80004010" in other["refusals"][0]
    same = submit(book, "L2", [fn("fn:us:80004010", "Actor_Spawn")])
    assert (same["accepted"], same["refused"], same["already"]) == (0, 0, 1)
    assert len(namebook.ledger()) == 1


def test_a_queued_name_is_taken_for_the_next_submit(book):
    submit(book, "L1", [fn("fn:us:80004010", "Actor_Spawn")])
    got = submit(book, "L2", [fn("fn:us:80004030", "Actor_Spawn")])
    assert got["by_rule"] == {"duplicate": 1}


def test_evidence_may_name_a_function_a_queued_row_renamed(book):
    submit(book, "L1", [fn("fn:us:80004020", "Actor_Free", ["caller func_80004010"])])
    got = submit(book, "L2", [fn("fn:us:80004010", "Actor_Spawn", ["callee Actor_Free"])])
    assert got["accepted"] == 1


def test_a_key_with_no_target_or_a_named_target_is_refused_with_the_key_in_the_line(book):
    got = submit(
        book,
        "L1",
        [fn("fn:us:80009999", "Actor_Spawn"), fn("fn:us:80004040", "Actor_Other"), fn("nonsense", "Actor_X")],
    )
    assert got["by_rule"] == {"target": 1, "named": 1, "shape": 1}
    assert any(" fn:us:80009999 -> Actor_Spawn:" in line for line in got["refusals"])


def test_a_row_for_a_function_that_already_has_the_name_is_held_not_refused(book):
    got = submit(book, "L1", [fn("fn:us:80004040", "Actor_Update")])
    assert (got["accepted"], got["refused"], got["already"]) == (0, 0, 1)


def test_a_datum_row_takes_g_or_s_by_the_name(book):
    got = submit(book, "L1", [fn("datum:us:80005000", "g_actorList", ["reads D_80005000"])])
    assert got["refused"] == 1 and got["by_rule"] == {
        "unverifiable": 1
    }  # D_80005000 is read by a caller, not by itself
    got = submit(book, "L1", [fn("datum:us:80005000", "g_actorList", ["caller func_80004010"])])
    assert got["accepted"] == 1 and namebook.ledger()[0]["row"]["kind"] == "global"


def test_a_slot_row_finds_its_line_in_the_function(book):
    row = {"key": "slot:us:80004010:sp1C", "name": "spawnCount", "doc": "Counts spawns.", "evidence": ["role loop"]}
    got = submit(book, "L1", [row])
    assert got["refused"] == 1 and got["by_rule"] == {"evidence": 1}  # the line was found; the evidence did not hold
    assert keys.declared_line(book.project.root, "src/c.c", "func_80004010", "sp1C") == 2


def test_the_owner_gate_refuses_a_row_outside_the_lane_slice_and_lets_a_suggest_row_through(book):
    (book.tmp / "names/slices").mkdir(parents=True)
    (book.tmp / "names/slices/L1.json").write_text(json.dumps({"lane": "L1", "slice": "S1", "fns": ["fn:us:80004010"]}))
    got = submit(book, "L1", [fn("fn:us:80004030", "Actor_Mine"), fn("fn:us:80004010", "Actor_Spawn")])
    assert got["accepted"] == 1 and got["by_rule"] == {"owner": 1}
    got = submit(book, "L1", [fn("fn:us:80004030", "Actor_Mine", suggest=True)])
    assert (got["accepted"], got["suggested"]) == (0, 1)
    line = json.loads((book.tmp / "names/suggest.jsonl").read_text())
    assert line["key"] == "fn:us:80004030" and line["lane"] == "L1"
    assert [r["key"] for r in namebook.ledger()] == ["fn:us:80004010"]


def test_returned_rows_come_back_once_in_the_lanes_next_submit(book):
    namebook.send_back("L1", {"key": "fn:us:80004010", "name": "Actor_Spawn"}, "check", "renameproof refused")
    first = submit(book, "L1", [])
    assert first["returned"] == ["returned check fn:us:80004010 -> Actor_Spawn: renameproof refused"]
    assert submit(book, "L1", [])["returned"] == []


def test_events_move_rows_between_queued_inflight_landed_and_back(book):
    submit(book, "L1", [fn("fn:us:80004010", "Actor_Spawn"), fn("fn:us:80004030", "Actor_Start")])
    assert [r["seq"] for r in namebook.queued(inflight=False)] == [1, 2]
    namebook.event({"event": "made", "batch": 1, "seqs": [1, 2]})
    assert namebook.queued(inflight=False) == [] and len(namebook.queued()) == 2
    namebook.event({"event": "requeued", "batch": 1, "seqs": [2]})
    namebook.event({"event": "landed", "batch": 1, "seqs": [1]})
    assert [r["seq"] for r in namebook.queued(inflight=False)] == [2] and namebook.status() == {
        1: "landed",
        2: "queued",
    }


def test_every_submit_leaves_a_count_line_for_watch(book):
    submit(book, "L1", [fn("fn:us:80004010", "Actor_Spawn"), fn("fn:us:80009999", "Actor_Nope")])
    line = json.loads((book.tmp / "names/submits.jsonl").read_text())
    assert (line["lane"], line["rows"], line["accepted"], line["refused"], line["by_rule"]) == (
        "L1",
        2,
        1,
        1,
        {"target": 1},
    )


def test_a_suggest_row_is_checked_for_shape_and_key_only(book):
    bad_name = fn("fn:us:80004030", "notALegalName", suggest=True)  # the owner judges the name when it applies it
    got = submit(
        book,
        "L1",
        [bad_name, fn("fn:us:80009999", "Actor_X", suggest=True), {"key": "fn:us:80004010", "suggest": True}],
    )
    assert (got["suggested"], got["refused"]) == (1, 2) and got["by_rule"] == {"target": 1, "shape": 1}


def test_an_empty_submit_leaves_no_submit_log_line(book):
    """The warm-up call after main moves (`submit --ledger warm` on an empty file) must not show as a lane in WATCH."""
    assert submit(book, "warm", [])["rows"] == 0
    assert not (book.tmp / "names/submits.jsonl").exists()


def test_a_datum_no_module_owns_resolves_through_its_versions_symbol_table():
    table = {"de": {"D_801539B0": (0x8014D720, None, 4, "data"), "D_8014D494_de": (0x8014D494, None, 1, "data")}}
    ctx = naming.Map.__new__(naming.Map)
    ctx.tables, ctx.members, ctx._by_vram = table, {}, None
    ctx.doc = {"names_from": "us-rev1", "versions": ["de", "us-rev1"]}
    ctx.canon = lambda name: name  # type: ignore[method-assign]
    row = {"key": "datum:de:8014D720", "name": "g_frontendResourceState", "doc": "d", "evidence": []}
    got, rule, _ = namebook.resolve(ctx, SimpleNamespace(), SimpleNamespace(), row)
    assert rule == "" and got["kind"] == "global" and got["target"] == "D_801539B0"
    other = {**row, "key": "datum:de:8014D494", "name": "s_x"}
    static, rule, _ = namebook.resolve(ctx, SimpleNamespace(), SimpleNamespace(), other)
    assert rule == "" and static["kind"] == "static" and static["target"] == "D_8014D494_de"
    done = {**row, "name": "D_801539B0"}
    assert namebook.resolve(ctx, SimpleNamespace(), SimpleNamespace(), done)[1] == "applied"
    _, rule, why = namebook.resolve(ctx, SimpleNamespace(), SimpleNamespace(), {**row, "key": "datum:de:80000000"})
    assert rule == "target" and "no data is placed" in why


def test_a_slot_of_a_non_matching_body_finds_its_line_in_the_fuzzy_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/f.c").write_text(
        "#ifdef NON_MATCHING\nvoid func_80004010(s32 arg0) {\n    s32 temp_a2 = arg0;\n}\n#endif\n"
    )
    ctx = SimpleNamespace(
        doc={"names_from": "us"},
        root=tmp_path,
        canon=lambda name: name,
        members={"func_80004010": {"kind": "function", "unit": None, "fuzzy": "src/f.c"}},
    )
    row = {"key": "slot:us:80004010:temp_a2", "name": "leftTab", "doc": "d", "evidence": []}
    got, rule, why = namebook.resolve(ctx, SimpleNamespace(), SimpleNamespace(), row)
    assert rule == "" and got["kind"] == "local" and (got["path"], got["line"]) == ("src/f.c", 3), why


def test_a_slot_of_a_body_written_under_a_define_alias_finds_its_line(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/f.c").write_text(
        "#ifdef NON_MATCHING\n#define Frontend_Open func_80426FA0_eu_x\ns32 Frontend_Open(s32 arg0) {\n"
        "    s32 temp_a2 = arg0;\n    return temp_a2;\n}\n#endif\n"
    )
    assert keys.declared_line(tmp_path, "src/f.c", "func_80426FA0_eu_x", "temp_a2") == 4
    assert keys.declared_line(tmp_path, "src/f.c", "func_80426FA0_eu_x", "nothere") == 0


def test_a_register_split_placeholder_is_a_decompiler_slot():
    from recipemaker import locals_

    assert locals_.placeholder("regpart_temp_v0_2") and not locals_.placeholder("priorDefaultFocus")


def test_a_suggestion_may_correct_a_member_that_already_has_a_name():
    types = SimpleNamespace(key={"CGameStatus_t": "CGameStatus"}, member={"CGameStatus": {"mode": "s32"}})
    fields = SimpleNamespace(
        types=types, unknown_at=lambda tag, off: ("", "CGameStatus has no unknown member at +0x18")
    )
    ctx = SimpleNamespace(doc={"names_from": "us-rev1"})
    row = {"key": "member:CGameStatus_t+0x18", "name": "scoreLimit", "doc": "d", "evidence": []}
    _, rule, why = namebook.resolve(ctx, fields, SimpleNamespace(), row)
    assert rule == "target" and "no unknown member" in why  # an owned row may not rename a named member
    got, rule, _ = namebook.resolve(ctx, fields, SimpleNamespace(), {**row, "suggest": True})
    assert rule == "" and got["_key"] == "member:CGameStatus_t+0x18"


TWO_COPIES = (
    "#if defined(VERSION_EU)\n"
    "int pick(int arg0, int *pThis) {\n"
    "    int selection = 1;\n"
    "    return selection;\n"
    "}\n"
    "#endif\n"
    "#if defined(VERSION_DE) || defined(VERSION_US_REV1)\n"
    "int pick(int arg0, int *arg1) {\n"
    "    int var_a0 = 2;\n"
    "    return var_a0;\n"
    "}\n"
    "#endif\n"
)


def test_a_slot_of_a_function_with_a_copy_per_version_reads_the_copy_that_version_compiles(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/f.c").write_text(TWO_COPIES)
    assert keys.declared_line(tmp_path, "src/f.c", "pick", "var_a0", "us-rev1") == 9
    assert keys.declared_line(tmp_path, "src/f.c", "pick", "arg0", "us-rev1") == 8
    assert keys.declared_line(tmp_path, "src/f.c", "pick", "arg0", "eu") == 2
    assert keys.declared_line(tmp_path, "src/f.c", "pick", "var_a0", "eu") == 0  # the eu copy has no such slot
    assert keys.declared_line(tmp_path, "src/f.c", "pick", "nothere", "us-rev1") == 0


def test_the_copy_holding_the_evidence_line_is_the_one_a_local_row_renames(tmp_path):
    from recipemaker import repair

    spans = repair.local_spans(TWO_COPIES, "pick")
    assert len(spans) == 2 and repair.local_span(TWO_COPIES, "pick") is None
    assert repair.span_at(TWO_COPIES, "pick", 9) == spans[1] and repair.span_at(TWO_COPIES, "pick", 3) == spans[0]
    assert repair.span_at(TWO_COPIES, "pick", 6) is None


def land(book, lane, key, name):
    """A row of `lane` that landed: the tree holds `name` for the key's function (Actor_Update in the fixture)."""
    namebook.append(lane, [{"_key": key, "name": name, "doc": "d", "evidence": []}])
    namebook.event({"event": "landed", "batch": 1, "seqs": [max(r["seq"] for r in namebook.ledger())]})


def test_a_lane_corrects_the_name_its_own_landed_row_gave_under_the_same_key(book, monkeypatch):
    monkeypatch.setitem(GRAPH, "Actor_Update", (["func_80004020"], []))
    land(book, "L1", "fn:us:80004040", "Actor_Update")
    got = submit(book, "L1", [fn("fn:us:80004040", "Actor_Tick")])
    assert (got["accepted"], got["refused"]) == (1, 0)
    assert namebook.ledger()[-1]["key"] == "fn:us:80004040" and namebook.ledger()[-1]["name"] == "Actor_Tick"
    assert namebook.ledger()[-1]["row"]["target"] == "Actor_Update"  # the rename moves the tree's current name


def test_a_correction_still_needs_evidence_and_a_name_the_rules_accept(book, monkeypatch):
    monkeypatch.setitem(GRAPH, "Actor_Update", (["func_80004020"], []))
    land(book, "L1", "fn:us:80004040", "Actor_Update")
    assert submit(book, "L1", [fn("fn:us:80004040", "Actor_DoStuff")])["by_rule"] == {"banned": 1}
    got = submit(book, "L1", [fn("fn:us:80004040", "Actor_Tick", evidence=["callee osMissing"])])
    assert got["by_rule"] == {"unverifiable": 1}


def test_another_lane_or_an_unlanded_or_foreign_name_is_still_named(book):
    land(book, "L1", "fn:us:80004040", "Actor_Update")
    assert submit(book, "L2", [fn("fn:us:80004040", "Actor_Tick")])["by_rule"] == {"named": 1}
    land(book, "L1", "fn:us:80004010", "Actor_Spawn")  # the tree holds func_80004010, not the landed name
    assert submit(book, "L1", [fn("fn:us:80004030", "Actor_Start")])["accepted"] == 1
    namebook.append("L1", [{"_key": "fn:us:80004040", "name": "Actor_Update", "doc": "d", "evidence": []}])  # queued
    assert submit(book, "L1", [fn("fn:us:80004040", "Actor_Tock")])["by_rule"] == {"pending": 1}


VOICE = "typedef struct ALVoice_s_tag {\n    int next;\n    int prev;\n} ALVoice_s_func_802B3000_de;\n"


def test_a_type_key_parses_and_names_a_pure_type_rename():
    assert keys.parse("type:ALVoice_s_func_802B3000_de", "us").text() == "type:ALVoice_s_func_802B3000_de"
    assert keys.parse("type:", "us") is None


def test_a_type_row_renames_a_record_by_its_key_and_refuses_a_second_taker(book):
    (book.project.root / "src/v.h").write_text(VOICE)
    row = {"key": "type:ALVoice_s_func_802B3000_de", "name": "ALVoice", "doc": "A synth voice.", "evidence": []}
    got = submit(book, "L1", [row, {**row, "key": "type:ALVoice_s_tag", "name": "ALVoice"}])
    assert (got["accepted"], got["refused"], got["queued"]) == (1, 1, 1)
    line = namebook.ledger()[0]
    assert line["key"] == "type:ALVoice_s_func_802B3000_de" and line["row"]["kind"] == "type"
    assert line["row"]["target"] == "ALVoice_s_func_802B3000_de"


def test_a_type_row_names_no_record_the_tree_lacks_and_knows_one_already_renamed(book):
    (book.project.root / "src/v.h").write_text(VOICE.replace("ALVoice_s_func_802B3000_de", "ALVoice"))
    gone = {"key": "type:NoSuchRecord", "name": "Other", "doc": "d", "evidence": []}
    done = {"key": "type:ALVoice_s_func_802B3000_de", "name": "ALVoice", "doc": "d", "evidence": []}
    got = submit(book, "L1", [gone, done])
    assert got["refused"] == 1 and got["already"] == 1


def test_a_type_row_patch_renames_the_type_in_every_file(book):
    root = book.project.root
    (root / "src/v.h").write_text(VOICE)
    (root / "src/u.c").write_text('#include "v.h"\nvoid use(ALVoice_s_func_802B3000_de *v) {\n    v->next = 0;\n}\n')
    row = {"key": "type:ALVoice_s_func_802B3000_de", "name": "ALVoice", "doc": "A synth voice.", "evidence": []}
    path = book.tmp / "rows.jsonl"
    path.write_text(json.dumps(row) + "\n")
    got = naming.run(book, {"host": None, "action": "submit", "file": str(path), "no_apply": True})
    assert (got["accepted"], got["refused"]) == (1, 0)
    patch = (root / got["path"]).read_text()
    assert "+} ALVoice;" in patch and "+void use(ALVoice *v) {" in patch


BOXES = {
    "src/a.h": "struct Box { int w; };\n",
    "src/b.h": "struct Box { float w; float h; };\n",
    "src/a.c": '#include "a.h"\nstruct Box one;\n',
    "src/b.c": '#include "b.h"\nstruct Box two;\n',
}


def boxes(book, extra=None):
    for name, text in {**BOXES, **(extra or {})}.items():
        (book.project.root / name).write_text(text)


def scoped(path, line=1, name="WideBox"):
    return {
        "key": "type:Box",
        "name": name,
        "doc": "One of the Box records.",
        "evidence": [],
        "path": path,
        "line": line,
    }


def test_a_type_row_naming_a_definition_site_keeps_it_and_binds_one_record(book):
    boxes(book)
    got = submit(book, "L1", [scoped("src/a.h")])
    assert (got["accepted"], got["refused"]) == (1, 0)
    assert (namebook.ledger()[0]["row"]["path"], namebook.ledger()[0]["row"]["line"]) == ("src/a.h", 1)
    path = book.tmp / "rows.jsonl"
    path.write_text(json.dumps(scoped("src/a.h")) + "\n")
    patch = naming.run(book, {"host": None, "action": "submit", "file": str(path), "no_apply": True})["path"]
    text = (book.project.root / patch).read_text()
    assert "+struct WideBox { int w; };" in text and "+struct WideBox one;" in text
    assert "WideBox two" not in text and "b.h" not in text


def test_a_type_row_whose_use_binds_to_two_definitions_is_refused_as_binding(book):
    boxes(book, {"src/both.c": '#include "a.h"\n#include "b.h"\nstruct Box both;\n'})
    got = submit(book, "L1", [scoped("src/a.h")])
    assert (got["accepted"], got["by_rule"]) == (0, {"binding": 1}) and "binds to 2" in got["refusals"][0]
    assert namebook.ledger() == []


def test_a_type_row_naming_no_definition_there_is_refused_as_binding(book):
    boxes(book)
    assert submit(book, "L1", [scoped("src/a.h", line=9)])["by_rule"] == {"binding": 1}


def test_the_ledger_lists_each_seq_with_its_state_and_reason(book):
    submit(book, "L1", [fn("fn:us:80004010", "Actor_Spawn"), fn("fn:us:80004030", "Actor_Start")])
    namebook.append("L2", [{"_key": "fn:us:80004040", "name": "Actor_Tick", "doc": "d", "evidence": []}])
    namebook.event({"event": "made", "batch": 1, "seqs": [1, 2]})
    namebook.event({"event": "returned", "batch": 1, "seqs": [2], "reason": "renameproof refused"})
    got = naming.run(book, {"host": None, "action": "status", "seqs": "2-3"})
    assert [(r["seq"], r["lane"], r["state"], r["reason"]) for r in got["rows"]] == [
        (2, "L1", "returned", "renameproof refused"),
        (3, "L2", "queued", ""),
    ]
    assert got["counts"] == {"returned": 1, "queued": 1}
    assert [r["seq"] for r in naming.run(book, {"host": None, "action": "status", "ledger": "L1"})["rows"]] == [1, 2]


def test_keyless_rows_returned_by_the_applier_each_show_their_own_reason(book):
    namebook.append("L1", [{"name": "Alpha", "doc": "d", "evidence": []}, {"name": "Beta", "doc": "d", "evidence": []}])
    namebook.event({"event": "made", "batch": 1, "seqs": [1, 2]})
    namebook.event({"event": "returned", "batch": 1, "seqs": [1, 2]})
    namebook.send_back("L1", {"name": "Alpha"}, "proof", "token kept")
    namebook.send_back("L1", {"name": "Beta"}, "build", "warn")
    got = naming.run(book, {"host": None, "action": "status", "seqs": "1-2"})
    assert [r["reason"] for r in got["rows"]] == ["proof: token kept", "build: warn"]
