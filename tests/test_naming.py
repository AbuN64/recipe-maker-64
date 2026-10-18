"""recipe name: catalog and tag names need no model, a pack carries checkable evidence, submit refuses by rule."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from r1_fake_api import fake_api  # noqa: F401

from recipemaker import naming, structure

VRAM = 0x80001000


def member(name, calls=(), loads=(), strings=(), module="", kind="function", unit=None, **more):
    place = {"version": "us", "section": ".text", "rom_start": 0x1000, "rom_end": 0x1040, "vram": VRAM}
    return {
        "name": name,
        "kind": kind,
        "module": module,
        "unit": unit,
        "placements": [place],
        "calls": list(calls),
        "loads": list(loads),
        "strings": list(strings),
        **more,
    }


def module(name, folder, members):
    base = f"src/game/main/{folder}/{name}.c"
    return {
        "name": name,
        "segment": "main",
        "source": base,
        "header": base.replace("src/", "include/").replace(".c", ".h"),
        "members": members,
    }


def document():
    members = [
        # sys_A calls two input functions of three external calls: 66%, so the folder is named by its tag
        member(
            "func_80001010",
            ["func_80900000", "func_80900010", "func_80001020", "func_80700000"],
            ["D_80002000"],
            ["PAUSE"],
            "code_80001000",
            unit="src/a.c",
        ),
        member("func_80001020", ["func_80700000"], [], [], "code_80001000", unit="src/a.c"),
        member("D_80002000", kind="data", module="code_80001000"),
        # sys_B: one input call of three external ones stays for the pack
        member(
            "func_80003010",
            ["func_80900000", "func_80001020", "func_80004010"],
            [],
            ["Mix"],
            "code_80003000",
            unit="src/b.c",
        ),
        # actor is named already
        member("func_80004010", ["func_80004020"], ["D_80005000"], ["alive"], "code_80004000", unit="src/c.c"),
        member("func_80004020", [], [], [], "code_80004000", unit="src/c.c"),
        member("func_80004030", ["func_80004020"], [], [], "code_80004000", unit="src/c.c"),
        member("D_80005000", kind="data", module="code_80004000"),
        member("Actor_Update", ["func_80004020"], [], [], "code_80004000", unit="src/c.c"),
        # the SDK region
        member("func_80900000", module="code_80900000", sdk_name="osContStartReadData"),
        member("func_80900010", module="code_80900000", sdk_name="osContGetReadData"),
        member("func_80900020", module="code_80900000", sdk_name="alSeqpPlay"),
        member("func_80700000", module="code_80700000"),
    ]
    return {
        "names_from": "us",
        "versions": ["us"],
        "members": members,
        "modules": [
            module("code_80001000", "sys_80001000", ["func_80001010", "func_80001020", "D_80002000"]),
            module("code_80003000", "sys_80003000", ["func_80003010"]),
            module(
                "code_80004000",
                "actor",
                ["func_80004010", "func_80004020", "func_80004030", "D_80005000", "Actor_Update"],
            ),
            module("code_80900000", "lib_80900000", ["func_80900000", "func_80900010", "func_80900020"]),
            module("code_80700000", "math", ["func_80700000"]),
        ],
        "units": [],
        "headers": [],
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    """The moves a verb hands to the patch builder are recorded in `ops`; the builder itself is tested below."""
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: document())
    config = SimpleNamespace(project=SimpleNamespace(root=tmp_path), owners={}, ops=[], real=naming._patch)

    def spy(cfg, doc, moves):
        config.ops += moves
        return {"patch": "", "files": [], "moves": len(moves), "path": ""}

    monkeypatch.setattr(naming, "_patch", spy)
    return config


def run(project, action, **more):
    params = {"host": None, "action": action, "step": None, "count": 40, "file": None, **more}
    return naming.run(project, params)


def moves(project):
    return project.ops


def test_n0_emits_the_catalog_names_as_symbol_moves_and_writes_no_pack(project):
    got = run(project, "next", step="N0")
    assert got["pack"] is None and got["moves"] == 3 and got["next"] == "recipe apply"
    assert moves(project) == [
        {"op": "symbol", "subject": "func_80900000", "target": "osContStartReadData"},
        {"op": "symbol", "subject": "func_80900010", "target": "osContGetReadData"},
        {"op": "symbol", "subject": "func_80900020", "target": "alSeqpPlay"},
    ]
    assert not list((project.project.root / ".unbake/recipe/name").glob("pack-*"))


def test_n0_leaves_a_name_two_members_claim_neutral_on_both_and_records_it(project, monkeypatch):
    doc = document()
    doc["members"].append(member("func_80900030", module="code_80900000", sdk_name="osContStartReadData"))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    run(project, "next", step="N0")
    got = {m["subject"]: m["target"] for m in moves(project)}
    assert sorted(got.values()) == ["alSeqpPlay", "osContGetReadData"] and len(got) == 2  # one move per name
    ledger = (project.project.root / ".unbake/recipe/name/n0-ledger.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in ledger] == [
        {"name": "osContStartReadData", "members": ["func_80900000", "func_80900030"]}
    ]


def test_n2_names_a_folder_when_one_tag_holds_half_the_external_calls(project):
    got = run(project, "next", step="N2")
    assert [m["op"] for m in moves(project)] == ["source", "header"]
    assert moves(project)[0] == {
        "op": "source",
        "subject": "code_80001000",
        "target": "src/game/main/input/code_80001000.c",
    }
    assert moves(project)[1]["target"] == "include/game/main/input/code_80001000.h"
    assert got["moves"] == 2 and got["targets"] == 1  # 2 of 3 external calls are osCont: 66%
    pack = json.loads((project.project.root / got["pack"]).read_text())
    assert [t["target"] for t in pack["targets"]] == ["sys_80003000"]  # 1 of 3 is only 33%: it goes to the model
    assert pack["targets"][0]["strings"] == ["Mix"] and "answer" in pack and pack["banned"]


def test_a_pack_carries_the_evidence_of_a_public_function(project):
    got = run(project, "next", step="N3")
    pack = json.loads((project.project.root / got["pack"]).read_text())
    assert [t["target"] for t in pack["targets"]] == ["func_80700000", "func_80004010"]  # by fan-in
    card = pack["targets"][1]
    assert card["callers"] == ["func_80003010"] and card["subsystem"] == "actor" and card["fan_in"] == 1
    assert card["callees"] == ["func_80004020"] and card["strings"] == ["alive"] and card["loads"] == ["D_80005000"]
    assert pack["step"] == "N3" and got["next"].startswith("recipe name submit")


def row(target, name, evidence, kind="function", doc="Does a thing."):
    return {"target": target, "kind": kind, "name": name, "evidence": evidence, "doc": doc}


def submit(project, rows):
    path = project.project.root / "names-1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return run(project, "submit", file=str(path))


def test_submit_refuses_by_rule_with_exact_counts(project):
    rows = [
        row("func_80004010", "Actor_Spawn", ["callee func_80004020"]),
        row("func_80004030", "Menu_Spawn", ["callee func_80004020"]),  # prefix is not the folder
        row("func_80004020", "Actor_DoStuff", ["caller func_80004010"]),  # banned words
        row("func_80001020", "Actor_Spawn", ["caller func_80001010"]),  # duplicate of the first row, and unnamed folder
        row("D_80005000", "g_actorList", [], kind="global"),  # no evidence
        row("func_80001010", "Input_ReadPad", ["string 'NOPE'"]),  # the folder has no name yet
        {"target": "func_80004030"},  # malformed
    ]
    got = submit(project, rows)
    assert got["accepted"] == 1 and got["refused"] == 6
    assert got["by_rule"] == {"regex": 1, "banned": 1, "prefix": 2, "evidence": 1, "shape": 1}
    refused = [
        json.loads(x) for x in (project.project.root / ".unbake/recipe/name/refused.jsonl").read_text().splitlines()
    ]
    assert len(refused) == 6 and all(r["rule"] and r["reason"] for r in refused)


def test_submit_refuses_a_duplicate_and_unverifiable_evidence(project):
    rows = [
        row("func_80004010", "Actor_Spawn", ["callee func_80004020"]),
        row("func_80004030", "Actor_Spawn", ["callee func_80004020"]),
        row("func_80004020", "Actor_Free", ["string 'NOPE'"]),
        row("func_80004020", "Actor_Kill", ["callee osMissing"]),
        row("D_80005000", "g_actorList", ["caller func_80004010"], kind="global"),
        row("Actor_Update", "Actor_Tick", ["callee func_80004020"]),  # already has a name
        row("func_80900020", "Audio_Play", ["tag audio"]),  # an SDK function keeps its catalog name
    ]
    got = submit(project, rows)
    assert got["by_rule"] == {"duplicate": 1, "unverifiable": 2, "named": 1, "catalog": 1}
    assert got["accepted"] == 2 and [m["op"] for m in moves(project)] == ["symbol", "symbol"]


def test_accepted_rows_become_moves_in_subsystem_module_symbol_order_with_banners(project):
    rows = [
        row("func_80003010", "Audio_MixVoices", ["string 'Mix'"], doc="Mixes the voices."),
        row("code_80003000", "audio_mixer", ["callee func_80900000"], kind="module"),
        row("sys_80003000", "audio", ["string 'Mix'"], kind="subsystem"),
    ]
    got = submit(project, rows)
    assert got["accepted"] == 3 and got["refused"] == 0
    assert moves(project) == [
        {"op": "source", "subject": "code_80003000", "target": "src/game/main/audio/code_80003000.c"},
        {"op": "header", "subject": "code_80003000", "target": "include/game/main/audio/code_80003000.h"},
        {"op": "module", "subject": "code_80003000", "target": "audio_mixer"},
        {"op": "source", "subject": "audio_mixer", "target": "src/game/main/audio/audio_mixer.c"},
        {"op": "header", "subject": "audio_mixer", "target": "include/game/main/audio/audio_mixer.h"},
        {"op": "symbol", "subject": "func_80003010", "target": "Audio_MixVoices"},
    ]
    saved = json.loads((project.project.root / got["banners"]).read_text())
    assert {b["name"]: b["doc"] for b in saved}["Audio_MixVoices"] == "Mixes the voices."


def test_banners_become_candidates_with_the_address_line(project, monkeypatch):
    doc = document()
    for m in doc["members"]:
        m["name"] = {"func_80003010": "Audio_MixVoices"}.get(m["name"], m["name"])
    doc["modules"][1]["members"] = ["Audio_MixVoices"]
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    root = project.project.root
    (root / "src").mkdir()
    (root / "src/b.c").write_text('#include "x.h"\n\nvoid Audio_MixVoices(int a) {\n    a = 1;\n}\n')
    (root / ".unbake/recipe/name").mkdir(parents=True)
    (root / ".unbake/recipe/name/banners-1.json").write_text(
        json.dumps([{"name": "Audio_MixVoices", "kind": "function", "doc": "Mixes the voices."}])
    )
    got = run(project, "banners")
    text = (root / got["files"][0]).read_text()
    banner = "================\nAudio_MixVoices\n\nMixes the voices.\nus 80001000\n================\n*/\n"
    assert banner + "void Audio_MixVoices" in text
    assert got["next"] == "unbake submit --bulk " + got["files"][0]
    assert run(project, "banners")["skipped"] == 0  # the source itself is unchanged, so the same candidate again


def test_a_type_row_becomes_a_type_move_not_a_symbol_move(project, monkeypatch):
    doc = document()
    header = {"path": "include/c.h", "declares": ["Record_func_80004010"], "modules": ["code_80004000"]}
    doc["headers"] = [{**header, "included_by": []}]
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    got = submit(project, [row("Record_func_80004010", "ActorRecord", ["callee func_80004020"], kind="type")])
    assert got["accepted"] == 1, got
    assert moves(project) == [{"op": "type", "subject": "Record_func_80004010", "target": "ActorRecord"}]


def test_n0_writes_a_patch_that_renames_the_symbol_in_symbol_addrs_and_the_sources(project, monkeypatch):
    monkeypatch.setattr(naming, "_patch", project.real)
    root = project.project.root
    (root / "versions/us").mkdir(parents=True)
    (root / "versions/us/symbol_addrs.txt").write_text("func_80900000 = 0x80900000; // type:func\n")
    (root / "src").mkdir()
    (root / "src/a.c").write_text("INCLUDE_ASM(main, func_80900000);\nvoid f(void) { func_80900000(); }\n")
    got = run(project, "next", step="N0")
    out = json.loads(got["stream"])
    assert out["files"] == ["src/a.c", "versions/us/symbol_addrs.txt"] and out["moves"] == got["moves"] == 1
    assert (root / got["path"]).read_text() == out["patch"] == got["patch"]
    check = subprocess.run(["git", "-C", str(root), "apply", "--check", "-"], input=out["patch"].encode(), check=False)
    assert check.returncode == 0 and "osContStartReadData" in out["patch"]


def test_submit_prints_the_patch_of_the_accepted_renames(project, monkeypatch):
    monkeypatch.setattr(naming, "_patch", project.real)
    root = project.project.root
    (root / "src").mkdir()
    (root / "src/c.c").write_text("void func_80004030(void) {}\n")
    got = submit(project, [row("func_80004030", "Actor_Spawn", ["callee func_80004020"])])
    assert got["accepted"] == 1 and got["files"] == ["src/c.c"] and "Actor_Spawn" in json.loads(got["stream"])["patch"]
