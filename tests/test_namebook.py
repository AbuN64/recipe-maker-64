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
