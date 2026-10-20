"""recipe name: tag names need no model, a pack carries checkable evidence, submit refuses by rule, and the calls and
loads come from the extracted assembly."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from r1_fake_api import fake_api  # noqa: F401
from unbake.contracts import Finding, Refusal

from recipemaker import naming, structure

VRAM = 0x80001000
GRAPH = {  # name -> (calls, loads), what `naming.reads` finds in the assembly
    # sys_A calls two input functions of four external calls: half, so the folder is named by its tag
    "func_80001010": (["osContStartReadData", "osContGetReadData", "func_80001020", "func_80700000"], ["D_80002000"]),
    "func_80001020": (["func_80700000"], []),
    # sys_B: one input call of three external ones stays for the pack
    "func_80003010": (["osContStartReadData", "func_80001020", "func_80004010"], []),
    # actor is named already
    "func_80004010": (["func_80004020"], ["D_80005000"]),
    "func_80004020": ([], []),
    "func_80004030": (["func_80004020"], []),
    "Actor_Update": (["func_80004020"], []),
    "osContStartReadData": ([], []),
    "osContGetReadData": ([], []),
    "alSeqpPlay": ([], []),
    "func_80700000": ([], []),
}


TABLES: dict = {"us": {}}  # per version the symbol table a spelling resolves through (`naming._tables`)


def member(name, module="", kind="function", unit=None, segment="main"):
    place = {"version": "us", "section": ".text", "rom_start": 0x1000, "rom_end": 0x1040, "vram": VRAM}
    return {"name": name, "kind": kind, "module": module, "unit": unit, "segment": segment, "placements": [place]}


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
        member("func_80001010", "code_80001000", unit="src/a.c"),
        member("func_80001020", "code_80001000", unit="src/a.c"),
        member("D_80002000", kind="data", module="code_80001000"),
        member("func_80003010", "code_80003000", unit="src/b.c"),
        member("func_80004010", "code_80004000", unit="src/c.c"),
        member("func_80004020", "code_80004000", unit="src/c.c"),
        member("func_80004030", "code_80004000", unit="src/c.c"),
        member("D_80005000", kind="data", module="code_80004000"),
        member("Actor_Update", "code_80004000", unit="src/c.c"),
        # the SDK region
        member("osContStartReadData", module="code_80900000"),
        member("osContGetReadData", module="code_80900000"),
        member("alSeqpPlay", module="code_80900000"),
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
            module("code_80900000", "lib_80900000", ["osContStartReadData", "osContGetReadData", "alSeqpPlay"]),
            module("code_80700000", "math", ["func_80700000"]),
        ],
        "units": [],
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    """The moves a verb hands to the patch builder are recorded in `ops`; the builder itself is tested below."""
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: document())
    monkeypatch.setattr(naming, "reads", lambda config, doc: dict(GRAPH))
    monkeypatch.setattr(naming, "_tables", lambda config, versions: dict(TABLES))
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


def test_n2_names_a_folder_when_one_tag_holds_half_the_external_calls(project):
    got = run(project, "next", step="N2")
    assert [m["op"] for m in moves(project)] == ["source", "header"]
    assert moves(project)[0] == {
        "op": "source",
        "subject": "code_80001000",
        "target": "src/game/main/input/code_80001000.c",
    }
    assert moves(project)[1]["target"] == "include/game/main/input/code_80001000.h"
    assert got["moves"] == 2 and got["targets"] == 1 and got["read"] == len(GRAPH)  # 2 of 4 external calls: half
    pack = json.loads((project.project.root / got["pack"]).read_text())
    assert [t["target"] for t in pack["targets"]] == ["sys_80003000"]  # 1 of 3 is only 33%: it goes to the model
    assert pack["targets"][0]["callees"][0] == "osContStartReadData" and "answer" in pack and pack["banned"]
    assert "string" not in " ".join(pack["evidence_forms"])


def test_a_pack_carries_the_evidence_of_a_public_function(project):
    got = run(project, "next", step="N3")
    pack = json.loads((project.project.root / got["pack"]).read_text())
    assert [t["target"] for t in pack["targets"]] == ["func_80700000", "func_80004010"]  # by fan-in
    card = pack["targets"][1]
    assert card["callers"] == ["func_80003010"] and card["subsystem"] == "actor" and card["fan_in"] == 1
    assert card["callees"] == ["func_80004020"] and card["loads"] == ["D_80005000"]
    assert "strings" not in card and "sdk_callees" not in card
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
        row("func_80001010", "Input_ReadPad", ["callee osMissing"]),  # the folder has no name yet
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
        row("func_80004020", "Actor_Free", ["caller func_80001010"]),
        row("func_80004020", "Actor_Kill", ["callee osMissing"]),
        row("D_80005000", "g_actorList", ["caller func_80004010"], kind="global"),
        row("Actor_Update", "Actor_Tick", ["callee func_80004020"]),  # already has a name
        row("alSeqpPlay", "Audio_Play", ["tag audio"]),  # an SDK function keeps its name
        row("func_80004030", "Actor_Spawn2", ["string 'Mix'"]),  # no string evidence exists
    ]
    got = submit(project, rows)
    assert got["by_rule"] == {"duplicate": 1, "unverifiable": 2, "evidence": 1, "named": 2}
    assert got["accepted"] == 2 and [m["op"] for m in moves(project)] == ["symbol", "symbol"]


def test_accepted_rows_become_moves_in_subsystem_module_symbol_order_with_banners(project):
    rows = [
        row("func_80003010", "Audio_MixVoices", ["callee func_80004010"], doc="Mixes the voices."),
        row("code_80003000", "audio_mixer", ["callee osContStartReadData"], kind="module"),
        row("sys_80003000", "audio", ["tag input"], kind="subsystem"),
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


def test_a_type_row_is_refused_as_malformed_for_want_of_a_header_graph(project):
    got = submit(project, [row("Record_func_80004010", "ActorRecord", ["callee func_80004020"], kind="type")])
    assert got["accepted"] == 0 and got["by_rule"] == {"shape": 1} and moves(project) == []


def test_submit_prints_the_patch_of_the_accepted_renames(project, monkeypatch):
    monkeypatch.setattr(naming, "_patch", project.real)
    root = project.project.root
    (root / "versions/us").mkdir(parents=True)
    (root / "versions/us/symbol_addrs.txt").write_text("func_80004030 = 0x80004030; // type:func\n")
    (root / "src").mkdir()
    (root / "src/c.c").write_text("void func_80004030(void) {}\n")
    got = submit(project, [row("func_80004030", "Actor_Spawn", ["callee func_80004020"])])
    out = json.loads(got["stream"])
    assert got["accepted"] == 1 and got["files"] == ["src/c.c", "versions/us/symbol_addrs.txt"]
    assert (root / got["path"]).read_text() == out["patch"] == got["patch"] and "Actor_Spawn" in out["patch"]
    check = subprocess.run(["git", "-C", str(root), "apply", "--check", "-"], input=out["patch"].encode(), check=False)
    assert check.returncode == 0


ASM = """.include "macro.inc"
glabel {name}
    /* 1 80001010 27BDFFE0 */  addiu      $sp, $sp, -0x20
    /* 2 80001014 3C02800E */  lui        $v0, %hi(D_80002000)
    /* 3 80001018 8C42E7E8 */  lw         $v0, %lo(D_80002000+0x4)($v0)
    /* 4 8000101C 0C099606 */  jal        func_80001020
    /* 5 80001020 00000000 */   nop
    /* 6 80001024 0C099606 */  jal        func_80001020
    /* 7 80001028 0040F809 */  jalr       $v0
    /* 8 8000102C 0C099606 */  jal        osMissing
    /* 9 80001030 10400005 */  beqz       $v0, .Lauto_80001040
    /* A 80001034 0C099606 */  jal        {name}
  .Lauto_80001040:
    /* C 8000103C 08084FE1 */  j          func_80700000
    /* D 80001040 00000000 */   nop
"""


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    """Extracted assembly of `main` under asm/us, and an `api.asm_path` that places what has a ROM address."""
    folder = tmp_path / "asm/us/nonmatchings/main"
    folder.mkdir(parents=True)
    (folder / "func_80001010.s").write_text(ASM.format(name="func_80001010"))
    (folder / "func_80001020.s").write_text('.include "macro.inc"\n    /* 1 2 3 */  jal        func_80700000\n')
    unplaced = {"func_80004020"}

    def asm_path(config, version, name):
        if name in unplaced:
            raise Refusal(Finding("symbols.unknown", f"{name} has no rom address in {version}"))
        return f"asm/{version}/nonmatchings/main/{name}.s"

    monkeypatch.setattr(naming.api, "asm_path", asm_path)
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path))


def test_reads_takes_calls_and_loads_of_known_members_from_the_assembly_of_their_own_version(assembly):
    got = naming.reads(assembly, document())
    # self and labels out; a callee no member is named as stays for `Map.canon` (a renamed function's stale name)
    assert got["func_80001010"] == (["func_80001020", "osMissing", "func_80700000"], ["D_80002000"])
    assert got["func_80001020"] == (["func_80700000"], [])
    assert sorted(got) == ["func_80001010", "func_80001020"]  # no file, no entry; data members have none


def test_reads_follows_a_member_to_the_assembly_of_the_version_that_names_it(assembly):
    folder = assembly.project.root / "asm/eu/nonmatchings/main"
    folder.mkdir(parents=True)
    (folder / "func_80001010_eu.s").write_text("    /* 1 2 3 */  jal        func_80001020_eu\n")
    doc = document()
    for name in ("func_80001010_eu", "func_80001020_eu"):
        other = member(name, "code_80001000", unit="src/a.c")
        other["placements"][0]["version"] = "eu"
        doc["members"].append(other)
    got = naming.reads(assembly, doc)
    assert got["func_80001010_eu"] == (["func_80001020_eu"], []) and "func_80001020_eu" not in got


def test_reads_takes_a_call_only_another_version_of_the_function_makes(assembly):
    # func_80001010 is placed in us and eu; only its eu assembly (func_<its eu VRAM>_eu there) calls func_80004010
    folder = assembly.project.root / "asm/eu/nonmatchings/main"
    folder.mkdir(parents=True)
    (folder / f"func_{VRAM:08X}_eu.s").write_text("    /* 1 2 3 */  jal        func_80004010\n")
    doc = document()
    first = next(m for m in doc["members"] if m["name"] == "func_80001010")
    first["placements"].append({**first["placements"][0], "version": "eu"})
    got = naming.reads(assembly, doc)
    assert got["func_80001010"][0] == ["func_80001020", "osMissing", "func_80700000", "func_80004010"]


def test_reads_places_a_segment_by_the_first_member_unbake_can_place(assembly):
    doc = document()
    doc["members"].insert(0, member("func_80004020", "code_80004000", unit="src/c.c"))  # unplaced and first
    assert "func_80001010" in naming.reads(assembly, doc)


def test_reads_refuses_when_no_assembly_is_extracted(assembly, monkeypatch):
    def none(config, version, name):
        raise Refusal(Finding("symbols.unknown", name))

    monkeypatch.setattr(naming.api, "asm_path", none)
    with pytest.raises(Refusal) as refused:
        naming.reads(assembly, document())
    assert refused.value.findings[0].key == "recipe.name"


@pytest.mark.parametrize("folder", ["sched", "SCHED", "Sched"])
def test_a_subsystem_folder_word_is_its_name_folded_to_the_convention_case(folder):
    from recipemaker import style

    assert style.subsystem_word(folder) == "sched"
    assert style.pattern("function", style.subsystem_word(folder)).startswith("^Sched_")
    assert style.legal("function", "Sched_ReadResourcePages", style.subsystem_word(folder))
    assert style.legal("module", "sched_pager", style.subsystem_word(folder))


@pytest.mark.parametrize("folder", ["", "src", "SRC", "sys_80003000", "SYS_80003000", "func_80001000", "AUDIO_3"])
def test_a_folder_that_is_no_subsystem_word_gives_no_word(folder):
    from recipemaker import style

    assert style.subsystem_word(folder) == ""


def test_map_word_agrees_for_upper_and_lower_case_folders():
    ctx = naming.Map.__new__(naming.Map)
    assert ctx.word("SCHED") == ctx.word("sched") == "sched"
    assert ctx.word("sys_80003000") == ""


@pytest.mark.parametrize("folder", ["actor", "ACTOR"])
def test_submit_takes_the_same_prefix_in_a_lower_or_upper_case_folder(project, monkeypatch, folder):
    doc = document()
    for m in doc["modules"]:
        if m["name"] == "code_80004000":
            m["source"] = m["source"].replace("/actor/", f"/{folder}/")
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    rows = [
        row("func_80004010", "Actor_Spawn", ["callee func_80004020"]),
        row("func_80004030", "Menu_Spawn", ["callee func_80004020"]),
        row("code_80004000", "actor_spawner", ["callee func_80004020"], kind="module"),
        row("func_80004020", "actor_Free", ["caller func_80004010"]),
    ]
    got = submit(project, rows)
    assert got["accepted"] == 2 and got["by_rule"] == {"regex": 2}
    named = submit(project, [row(folder, "actor", ["tag actor"], kind="subsystem")])
    assert named["by_rule"] == {"named": 1}  # an already-named folder is still recognised


def test_a_collision_counter_folder_is_unnamed_so_it_can_be_named_and_then_hold_names(project, monkeypatch):
    doc = document()
    doc["members"].append(member("func_80006010", "code_80006000", unit="src/d.c"))
    doc["modules"].append(module("code_80006000", "FX_2", ["func_80006010"]))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    monkeypatch.setitem(GRAPH, "func_80006010", (["func_80700000"], []))
    rows = [
        row("func_80006010", "Particle_Build", ["callee func_80700000"]),
        row("FX_2", "particle", ["callee func_80700000"], kind="subsystem"),
        row("actor", "creature", ["callee func_80004020"], kind="subsystem"),  # a named folder stays named
    ]
    got = submit(project, rows)
    assert got["accepted"] == 2 and got["by_rule"] == {"named": 1}
    assert {"op": "symbol", "subject": "func_80006010", "target": "Particle_Build"} in moves(project)
    assert any(m["target"] == "src/game/main/particle/code_80006000.c" for m in moves(project))


def _placeholder(monkeypatch):
    """A module in a collision-counter folder (SYSTEM_2): a subsystem with no name yet."""
    doc = document()
    doc["members"].append(member("func_80006010", "code_80006000", unit="src/d.c"))
    doc["modules"].append(module("code_80006000", "SYSTEM_2", ["func_80006010"]))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    monkeypatch.setitem(GRAPH, "func_80006010", (["func_80700000"], []))


def test_n2_offers_every_unnamed_subsystem_folder_a_placeholder_too_but_no_library(project, monkeypatch):
    _placeholder(monkeypatch)
    got = run(project, "next", step="N2")
    pack = json.loads((project.project.root / got["pack"]).read_text())
    assert [t["target"] for t in pack["targets"]] == ["SYSTEM_2", "sys_80003000"]  # lib_80900000 is no subsystem


def test_a_function_in_a_neutral_folder_is_refused_with_the_way_to_name_the_folder(project):
    got = submit(project, [row("func_80003010", "Input_ReadPad", ["callee func_80001020"])])
    assert got["by_rule"] == {"prefix": 1}
    refused = json.loads((project.project.root / ".unbake/recipe/name/refused.jsonl").read_text().splitlines()[-1])
    assert "subsystem row for sys_80003000" in refused["reason"] and "--step N2" in refused["reason"]


def test_a_function_in_a_placeholder_folder_takes_the_word_before_its_counter(project, monkeypatch):
    _placeholder(monkeypatch)  # SYSTEM_2: structure gave it the word system, so System_ is true of its code
    rows = [
        row("func_80006010", "Combat_ApplyAreaDamage", ["callee func_80700000"]),
        row("func_80006010", "System_ApplyAreaDamage", ["callee func_80700000"]),
    ]
    got = submit(project, rows)
    assert got["accepted"] == 1 and got["by_rule"] == {"regex": 1}
    assert {"op": "symbol", "subject": "func_80006010", "target": "System_ApplyAreaDamage"} in moves(project)
    rows = [
        row("func_80006010", "Combat_ApplyAreaDamage", ["callee func_80700000"]),
        row("SYSTEM_2", "combat", ["callee func_80700000"], kind="subsystem"),
    ]
    assert submit(project, rows)["accepted"] == 2  # named together in one file, the folder first


def test_naming_a_placeholder_folder_carries_its_prefixed_names_to_the_new_word(project, monkeypatch):
    doc = document()
    doc["members"] += [
        member("System_ApplyAreaDamage", "system_damage", unit="src/d.c"),
        member("func_80006020", "system_damage"),
    ]
    doc["modules"].append(module("system_damage", "SYSTEM_2", ["System_ApplyAreaDamage", "func_80006020"]))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    monkeypatch.setitem(GRAPH, "System_ApplyAreaDamage", (["func_80700000"], []))
    got = submit(project, [row("SYSTEM_2", "combat", ["callee func_80700000"], kind="subsystem")])
    assert got["accepted"] == 1
    assert moves(project) == [
        {"op": "symbol", "subject": "System_ApplyAreaDamage", "target": "Combat_ApplyAreaDamage"},
        {"op": "module", "subject": "system_damage", "target": "combat_damage"},
        {"op": "source", "subject": "combat_damage", "target": "src/game/main/combat/combat_damage.c"},
        {"op": "header", "subject": "combat_damage", "target": "include/game/main/combat/combat_damage.h"},
    ]


def test_a_carried_name_that_is_taken_refuses_the_folder_name(project, monkeypatch):
    doc = document()
    doc["members"] += [member("System_Spawn", "code_80006000", unit="src/d.c"), member("Actor_Spawn", "code_80004000")]
    doc["modules"].append(module("code_80006000", "SYSTEM_2", ["System_Spawn"]))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    monkeypatch.setitem(GRAPH, "System_Spawn", (["func_80700000"], []))
    got = submit(project, [row("SYSTEM_2", "actor", ["callee func_80700000"], kind="subsystem")])
    assert got["by_rule"] == {"duplicate": 1}  # the folder word actor is taken first
    got = submit(project, [row("SYSTEM_2", "spawner", ["callee func_80700000"], kind="subsystem")])
    assert got["accepted"] == 1


def test_a_function_in_a_folder_that_takes_no_prefix_has_the_unprefixed_form(project, monkeypatch):
    doc = document()
    doc["members"].append(member("func_80006010", "code_80006000", unit="src/d.c"))
    doc["modules"].append(module("code_80006000", "GAME", ["func_80006010"]))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    monkeypatch.setitem(GRAPH, "func_80006010", (["func_80700000"], []))
    rows = [
        row("func_80006010", "Game_StartRound", ["callee func_80700000"]),
        row("func_80006010", "StartRound", ["callee func_80700000"]),
        row("code_80006000", "round_start", ["callee func_80700000"], kind="module"),
    ]
    got = submit(project, rows)
    assert got["accepted"] == 2 and got["by_rule"] == {"regex": 1}
    refused = (naming._root(project) / "refused.jsonl").read_text()
    assert "GAME takes no prefix: the unprefixed form, such as StartRound" in refused  # THINK-802946A4


def test_a_named_subsystem_folder_is_spelled_as_the_projects_folders_are(project, monkeypatch):
    _placeholder(monkeypatch)
    for path in ("src/AI/CODE_80001000.CPP", "src/FRONTEND/CODE_80002000.CPP"):
        (project.project.root / path).parent.mkdir(parents=True, exist_ok=True)
        (project.project.root / path).write_text("")
    rows = [
        row("SYSTEM_2", "combat", ["callee func_80700000"], kind="subsystem"),
        row("func_80006010", "Combat_ApplyAreaDamage", ["callee func_80700000"]),
    ]
    got = submit(project, rows)
    assert got["accepted"] == 2
    assert {"op": "source", "subject": "code_80006000", "target": "src/game/main/COMBAT/code_80006000.c"} in moves(
        project
    )


def test_a_subsystem_name_is_taken_when_a_folder_spells_it_in_another_case(project, monkeypatch):
    doc = document()
    doc["members"].append(member("func_80006010", "code_80006000", unit="src/d.c"))
    doc["modules"].append(module("code_80006000", "COMBAT", ["func_80006010"]))
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    taken = submit(project, [row("sys_80003000", "combat", ["callee osContStartReadData"], kind="subsystem")])
    assert taken["by_rule"] == {"duplicate": 1}


def test_submit_checks_reads_evidence_against_the_loads_and_names_an_item_of_no_form(project):
    # SPEED-4: `reads X` (the card's own `loads`) was refused as unverifiable, and so was an item of no known form
    rows = [
        row("func_80004010", "Actor_Spawn", ["reads D_80005000"]),  # the card's loads hold it
        row("func_80004020", "Actor_Free", ["reads D_80005000"]),  # this one loads nothing
        row("func_80004030", "Actor_Kill", ["callee func_80004020", "uses D_80005000"]),
    ]
    got = submit(project, rows)
    assert got["accepted"] == 1 and got["by_rule"] == {"unverifiable": 1, "evidence": 1}
    refused = [
        json.loads(x) for x in (project.project.root / ".unbake/recipe/name/refused.jsonl").read_text().splitlines()
    ]
    reasons = {r["rule"]: r["reason"] for r in refused}
    assert reasons["unverifiable"] == "the assembly does not show: reads D_80005000"
    assert "'uses D_80005000'" in reasons["evidence"] and "reads NAME" in reasons["evidence"]


def test_an_evidence_item_may_end_in_a_note_that_is_not_checked(project):
    rows = [
        row("func_80004010", "Actor_Spawn", ["callee func_80004020 (frees the slot)"]),
        row("func_80004030", "Actor_Despawn", ["callee func_80004020 (frees (twice))"]),  # a note holds no parentheses
    ]
    got = submit(project, rows)
    assert got["accepted"] == 1 and got["by_rule"] == {"evidence": 1}
    refused = json.loads((project.project.root / ".unbake/recipe/name/refused.jsonl").read_text().splitlines()[-1])
    assert "a note in parentheses" in refused["reason"]


def _aliased(monkeypatch):
    """D_80005000 is named D_blob_1 in the names_from table (an alias), D_80005000 in another version's; the tree's
    C spells D_80005000. func_80004020 was renamed Actor_Free while the assembly still says func_80004020."""
    doc = document()
    for m in doc["members"]:
        if m["name"] == "D_80005000":
            m["name"] = "D_blob_1"
            m["placements"] = [{**m["placements"][0], "vram": 0x80005000, "rom_start": 0x5000}]
        if m["name"] == "func_80004020":
            m["name"] = "Actor_Free"
            m["placements"] = [{**m["placements"][0], "vram": 0x80004020, "rom_start": 0x4020}]
    for mod in doc["modules"]:
        mod["members"] = [
            "D_blob_1" if n == "D_80005000" else "Actor_Free" if n == "func_80004020" else n for n in mod["members"]
        ]
        mod["source"] = mod["source"].replace("code_80004000.c", "CODE_80004000.CPP")
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    tables = {
        "us": {"D_80005000": (0x80005000, 0x5000, 4, "data"), "func_80004020": (0x80004020, 0x4020, 0x40, "func")}
    }
    monkeypatch.setattr(naming, "_tables", lambda config, versions: tables)
    return doc


def test_a_data_target_resolves_through_the_spelling_a_version_table_gives_it(project, monkeypatch):
    _aliased(monkeypatch)
    got = submit(project, [row("D_80005000", "g_actorList", ["caller func_80004010"], kind="global")])
    assert got["accepted"] == 1, got["by_rule"]
    assert {"op": "symbol", "subject": "D_80005000", "target": "g_actorList"} in moves(project)


def test_evidence_naming_a_renamed_function_by_its_stale_assembly_name_or_its_new_name_is_verified(
    project, monkeypatch
):
    _aliased(monkeypatch)
    rows = [
        row("func_80004010", "Actor_Spawn", ["callee Actor_Free"]),
        row("func_80004030", "Actor_Kill", ["callee func_80004020"]),
    ]
    assert submit(project, rows)["accepted"] == 2


def test_a_module_row_names_its_target_by_stem_or_path_and_a_neutral_upper_case_stem_is_unnamed(project, monkeypatch):
    _aliased(monkeypatch)
    for target in ("CODE_80004000", "src/game/main/actor/CODE_80004000.CPP", "game/main/actor/CODE_80004000"):
        got = submit(project, [row(target, "actor_spawner", ["callee func_80004020"], kind="module")])
        assert got["accepted"] == 1, (target, got["by_rule"])


def test_an_sdk_function_takes_the_name_the_projects_libultra_table_gives_its_address(project):
    table = project.project.root / "versions/us/libultra_names.tsv"
    table.parent.mkdir(parents=True)
    table.write_text(f"vram\tname\tevidence_class\tevidence\n0x{VRAM:08X}\t__osDisableInt\tEXACT\tsame words\n")
    rows = [
        row("func_80700000", "__osDisableInt", ["caller func_80001010"]),
        row("func_80700000", "__osRestoreInt", ["caller func_80001010"]),  # no table names it so
    ]
    got = submit(project, rows)
    assert got["accepted"] == 1 and got["by_rule"] == {"regex": 1}
    assert {"op": "symbol", "subject": "func_80700000", "target": "__osDisableInt"} in moves(project)


def test_n4_calls_a_datum_static_when_one_file_reads_it_whatever_the_folder_holds(project):
    doc = document()
    doc["members"] += [member("func_80001030", "code_80001100", unit="src/a2.c"), member("D_80002100", kind="data")]
    doc["modules"].append(module("code_80001100", "sys_80001000", ["func_80001030"]))  # same folder, another file
    graph = {**GRAPH, "func_80001030": ([], ["D_80002000"]), "func_80001020": (["func_80700000"], ["D_80002100"])}
    ctx = naming.Map(project, doc, graph)
    kinds = {n: k for k, n in naming._targets(ctx, "N4")}
    assert kinds["D_80002000"] == "global"  # read by two files of one folder: a folder is no C scope
    assert kinds["D_80002100"] == "static"  # read by one file only, though it sits in no module of its own
    assert kinds["D_80005000"] == "static"


def test_a_module_with_no_header_of_its_own_is_named_by_its_source_alone(project, monkeypatch):
    """A module whose source has no same-stem header (header "") is renamed by its source only (10-20: the empty
    header path crashed the submit with PurePosixPath('.') has an empty name)."""
    doc = document()
    for m in doc["modules"]:
        if m["name"] == "code_80004000":
            m["header"] = ""
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    got = submit(project, [row("code_80004000", "actor_spawner", ["callee func_80004020"], kind="module")])
    assert got["accepted"] == 1
    assert [m["op"] for m in moves(project)] == ["module", "source"]
    submit(project, [row("actor", "creature", ["callee func_80004020"], kind="subsystem")])  # its folder moves
    assert "header" not in [m["op"] for m in moves(project)]
