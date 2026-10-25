"""Slice packs (T4) and the evidence forms they add, and the mechanical auto-namers (T6)."""

import json
from pathlib import Path

import pytest
from test_naming import document, project  # noqa: F401
from test_naming import row as legacy_row

from recipemaker import autoname, naming, slicepack, slices, structure

SOURCE = """\
void func_80004010(s32 arg0, Actor *arg1) {
    s32 var_s0;
    var_s0 = arg1->unk1C;
    func_80004020(arg0);
    D_80005000 = var_s0;
}

void func_80004020(void) {
    return;
}
"""
ASM = {
    "D_80005000": ".byte 0x53, 0x63, 0x6F, 0x72, 0x65, 0x20, 0x25, 0x64, 0x20, 0x70, 0x74, 0x73, 0x00"
}  # Score %d pts


@pytest.fixture
def names(project, tmp_path, monkeypatch):  # noqa: F811
    """The naming fixture with distinct addresses, a source file, one string datum and a slice document."""
    doc = document()
    for i, m in enumerate(doc["members"]):
        m["placements"] = [{**m["placements"][0], "vram": 0x80001000 + i * 0x10}]
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    (project.project.root / "src").mkdir()
    (project.project.root / "src/c.c").write_text(SOURCE)
    monkeypatch.setattr(slicepack, "asm_text", lambda config, ctx, name: ASM.get(name, ""))
    directory = tmp_path / "names"
    got = naming.run(
        project, {"host": None, "action": "slices", "step": None, "lanes": 8, "out": str(directory), "assign": []}
    )
    assert got["slices"] >= 2
    project.dir = directory
    return project


def pack(names, **more):
    sdoc = slices.load(names.dir)
    target = next(s["id"] for s in sdoc["slices"] if any("fn:us:" in k for k in s["fns"]) and len(s["fns"]) >= 1)
    params = {
        "host": None,
        "action": "next",
        "step": "S",
        "count": 20,
        "target": target,
        "names_dir": names.dir,
        "out": str(names.dir / "packs"),
        **more,
    }
    return naming.run(names, params)


def cards(names, got):
    return {c["target"]: c for c in json.loads(Path(got["pack"]).read_text())["targets"]}


def test_a_function_card_carries_body_neighbours_strings_slots_and_keyed_row_skeletons(names):
    sdoc = slices.load(names.dir)
    owner = sdoc["owner"][slices.fn_key("us", 0x80001000 + 4 * 0x10)]  # func_80004010 is member 4
    got = pack(names, target=owner, count=40)
    card = cards(names, got)["func_80004010"]
    assert card["key"] == slices.fn_key("us", 0x80001040) and card["subsystem"] == "actor"
    assert "var_s0 = arg1->unk1C;" in card["body"] and card["line"] == 1
    assert [c["name"] for c in card["callers"]] == ["func_80003010"] and card["callers"][0]["key"].startswith("fn:us:")
    assert [c["name"] for c in card["callees"]] == ["func_80004020"]
    assert card["strings"] == [{"datum": "D_80005000", "text": "Score %d pts"}]
    slots = {s["slot"]: s for s in card["proposals"]["slots"]}
    assert set(slots) == {"arg0", "arg1", "var_s0"} and slots["arg0"]["kind"] == "param"
    assert slots["var_s0"]["row"]["key"] == slices.slot_key(card["key"], "var_s0")
    assert card["proposals"]["members"][0] | {} == {
        "member": "unk1C",
        "receiver": "arg1",
        "type": "Actor",
        "line": 3,
        "text": "var_s0 = arg1->unk1C;",
        "key": "member:Actor+0x1C",
    }
    assert card["rows"]["function"]["key"] == card["key"] and card["rows"]["function"]["name"] == ""
    assert card["rows"]["function"]["evidence"] == ["callee func_80004020"]
    assert got["slice"] == [owner] and got["next"].startswith("recipe name submit --ledger ")
    assert "body L<n>: note" in json.loads(Path(got["pack"]).read_text())["evidence_forms"]


def test_a_leaf_function_is_packed_with_its_body_and_a_caller_as_the_hint(names):
    got = pack(names, target=slices.load(names.dir)["owner"][slices.fn_key("us", 0x80001000 + 5 * 0x10)], count=40)
    leaf = cards(names, got)["func_80004020"]
    assert leaf["callees"] == [] and "return;" in leaf["body"] and leaf["body_cut"] is False
    assert leaf["rows"]["function"]["evidence"] == ["caller func_80004010"] and leaf["proposals"]["slots"] == []


def test_the_pack_skips_what_the_ledger_holds_and_shows_the_suggestions_for_its_owner(names):
    key = slices.fn_key("us", 0x80001040)
    owner = slices.load(names.dir)["owner"][key]
    (names.dir / "ledger.jsonl").write_text(
        json.dumps({"seq": 1, "key": key, "row": {}})
        + "\n"
        + "".join(
            json.dumps({"seq": 2 + i, "key": slices.slot_key(key, s)}) + "\n"
            for i, s in enumerate(("arg0", "arg1", "var_s0"))
        )
    )
    (names.dir / "suggest.jsonl").write_text(
        json.dumps({"key": "fn:80001040", "name": "Actor_Think", "lane": "L9"}) + "\n"
    )
    got = pack(names, target=owner, count=40)
    assert "func_80004010" not in cards(names, got)  # the function and every slot are in the ledger already
    (names.dir / "ledger.jsonl").write_text(json.dumps({"seq": 1, "key": key}) + "\n")
    got = pack(names, target=owner, count=40)
    card = cards(names, got)["func_80004010"]
    assert card["open"]["function"] is False and card["open"]["slots"] == ["arg0", "arg1", "var_s0"]
    assert card["suggestions"][0]["name"] == "Actor_Think"


def test_data_fills_the_pack_when_the_slice_has_no_open_function_left(names):
    sdoc = slices.load(names.dir)
    key = slices.datum_key("us", 0x80001000 + 7 * 0x10)  # D_80005000
    owner = sdoc["owner"][key]
    fns = [k for k in sdoc["slices"][int(owner[1:]) - 1]["fns"]] if owner.startswith("S") else []
    (names.dir / "ledger.jsonl").write_text("".join(json.dumps({"key": k}) + "\n" for k in fns))
    got = pack(names, target=owner, count=40)
    datum = cards(names, got).get("D_80005000")
    assert datum and datum["kind"] == "datum" and datum["scope"] == "static" and datum["prefix"] == "s_"
    assert datum["strings"][0]["text"] == "Score %d pts" and datum["rows"]["datum"]["key"] == key


def test_an_unknown_slice_or_a_missing_document_is_refused(names, tmp_path):
    with pytest.raises(Exception, match="no slice or lane"):
        pack(names, target="S999")
    with pytest.raises(Exception, match=r"no slices\.json"):
        pack(names, names_dir=tmp_path / "nowhere")


def test_decode_reads_byte_runs_and_ascii_lines_and_ignores_binary():
    assert slicepack.decode(".byte 0x41, 0x42, 0x43, 0x44, 0x00, 0x45, 0x46, 0x47, 0x48, 0x00") == ["ABCD", "EFGH"]
    assert slicepack.decode('.asciz "hello %s"') == ["hello %s"]
    assert slicepack.decode(".byte 0x00, 0x01, 0x02, 0x03, 0x04") == []
    assert slicepack.decode(".word 0x80001000") == []


def test_body_and_string_evidence_are_checked_against_the_source_and_the_data(names):
    ctx = naming.Map(names, structure.evidence(names), naming.reads(names, structure.evidence(names)))
    users = ["func_80004010"]
    assert ctx.verifies('body L3: "var_s0 = arg1->unk1C"', users)
    assert ctx.verifies("body L3: reads a member into a local", users)
    assert not ctx.verifies('body L3: "nothing like this"', users)
    assert not ctx.verifies("body L9: outside the function", users)  # line 9 is the second function
    assert ctx.verifies("string Score", users)  # it loads D_80005000
    assert not ctx.verifies("string Missing", users)
    assert ctx.verifies("string pts", ["D_80005000"])
    assert slicepack.extra_form("body L1: x") and not slicepack.extra_form("callee x")


def test_a_row_citing_body_evidence_names_a_function_with_no_callee_evidence(names):
    rows = [legacy_row("func_80004010", "Actor_Spawn", ["body L3: a member into a local"])]
    path = names.project.root / "names-1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    got = naming.run(
        names, {"host": None, "action": "submit", "step": None, "count": 40, "file": str(path), "no_apply": True}
    )
    assert got["accepted"] == 1 and got["refused"] == 0


def test_string_names_come_from_the_words_and_say_format_for_printf_strings():
    assert autoname.string_name("Score %d points", "static") == "s_scorePointsFormat"
    assert autoname.string_name("GAME OVER", "global") == "g_gameOverText"
    assert autoname.string_name("%d.%s.%s", "static") == ""  # nothing a name can hold
    assert autoname.string_name('say "hi there"', "static") == ""
    assert autoname.string_name("This is a very long sentence about nothing at all", "static") == ""
    assert autoname.words("%5.2f seconds of play") == ["seconds", "play"]
    for noise in ("?fff", "NGequR", "ENT ON T", "Nnk(", "a1b2c3d4"):  # printable bytes that are no language
        assert not autoname.plausible(noise), noise
    assert autoname.plausible("Fly Mode Off") and autoname.plausible("Players")


def test_auto_emits_judged_rows_for_sdk_names_and_one_string_data(names, tmp_path):
    table = names.project.root / "versions/us/libultra_names.tsv"
    table.parent.mkdir(parents=True)
    table.write_text("vram\tname\tbasis\n0x%08X\tosYield\tx\n" % (0x80001000 + 12 * 0x10))  # func_80700000
    out = tmp_path / "auto.jsonl"
    got = naming.run(names, {"host": None, "action": "auto", "step": None, "out": str(out), "kinds": None})
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    by = {r["name"]: r for r in rows}
    assert set(by) == {"osYield", "s_scorePtsFormat"} or "s_scoreFormat" in by or "s_scorePtsFormat" in by
    sdk = by["osYield"]
    assert sdk["key"] == "fn:us:%08X" % (0x80001000 + 12 * 0x10) and sdk["evidence"] == ["sdk osYield"]
    text = next(r for r in rows if r["name"].startswith("s_"))
    assert text["key"] == slices.datum_key("us", 0x80001000 + 7 * 0x10) and text["confidence"] == "low"
    assert text["evidence"] == ["string Score"] and got["rows"] == len(rows)
    assert got["next"].startswith("recipe name submit --ledger auto")
    with pytest.raises(Exception, match="auto takes kinds"):
        naming.run(names, {"host": None, "action": "auto", "step": None, "out": None, "kinds": "nope"})
