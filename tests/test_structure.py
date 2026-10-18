"""Module structure: the evidence document from unbake 0.5.5's map and capture, the proposal from it, and the file
patch built from its moves. The fixture is the evidence of 48 real RageWars modules (ANIM 8023CFB0*, MENU/PAK
80444EC0*, WEAPONS 80213ED4*) as `structure.evidence` read it from the RW tree at 9e6f90430."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from r1_fake_api import fake_api  # noqa: F401
from unbake.contracts import FileFacts, Finding, Refusal

from recipemaker import clash, state, structure, tree

DATA = Path(__file__).parent / "data" / "structure.json"
SETTINGS = state.Structure()
OPS = ["member", "module", "source", "header", "file", "symbol", "type"]
MOVE = {
    "type": "object",
    "required": ["op", "subject", "target"],
    "additionalProperties": False,
    "properties": {
        "op": {"enum": OPS},
        "subject": {"type": "string", "minLength": 1},
        "target": {"type": "string", "minLength": 1},
    },
}
RW = tree.Naming(case="upper", source_ext=".CPP", evidence="upper")
W = "src/WEAPONS/80213ED4"


@pytest.fixture
def doc():
    return json.loads(DATA.read_text(encoding="utf-8"))


def names(proposal):
    return {m["name"]: m["atoms"] for m in proposal["modules"]}


def of(proposal, origin):
    return next(m for m in proposal["modules"] if m["origin"] == origin and m["keeps"])


def holding(proposal, atom):
    return next(m for m in proposal["modules"] if atom in m["atoms"])


# ---- the evidence


def _slim():
    return {
        "digest": "d1",
        "names_from": "us",
        "versions": ["eu", "us"],
        "modules": [
            {
                "path": "src/IO/80001000.CPP",
                "segment": "span_1000",
                "owns": {"us": ["f", "literals_f", "D", "g"], "eu": ["f"]},
                "fuzzy": ["g"],
                "stand_ins": {"us": ["D"]},
            },
            {"path": "src/IO/EMPTY.CPP", "segment": "span_1000", "owns": {}, "fuzzy": [], "stand_ins": {}},
        ],
    }


def _tables():
    us = {
        "f": (0x80001000, 0x1000, 0x20, "func"),
        "literals_f": (0x80003000, 0x3000, 0x10, "data"),
        "D": (0x80002000, 0x2000, None, "data"),
        "g": (0x80001020, 0x1020, 0x10, "func"),
        "after": (0x80002010, 0x2010, 4, "data"),
    }
    return {"us": us, "eu": {"f": (0x80001100, 0x1100, 0x20, "func")}}


def test_the_evidence_is_the_maps_modules_with_the_captured_sections_and_symbol_placements(tmp_path):
    (tmp_path / "src/IO").mkdir(parents=True)
    (tmp_path / "src/IO/80001000.CPP").write_text('#include "IO/80001000.H"\n')
    files = {"src/IO/": FileFacts(toolchain="gcc-b", cflags=("-O1",), omit=("-g",))}
    project = SimpleNamespace(root=tmp_path, toolchain="gcc-a", files=files)
    captured = SimpleNamespace(modules=[SimpleNamespace(path="src/IO/80001000.CPP", sections={"us": {"D": ".rodata"}})])
    got = structure.document_of(SimpleNamespace(project=project), _slim(), captured, _tables())
    assert (got["digest"], got["names_from"], got["versions"]) == ("d1", "us", ["eu", "us"])
    assert got["modules"] == [
        {
            "name": "src/IO/80001000.CPP",
            "source": "src/IO/80001000.CPP",
            "header": "include/IO/80001000.H",
            "segment": "span_1000",
            "members": ["f", "D", "g"],  # the literals fact moves with its file; an empty file is no module
        }
    ]
    assert got["units"] == [
        {
            "path": "src/IO/80001000.CPP",
            "module": "src/IO/80001000.CPP",
            "members": ["f"],
            "toolchain": "gcc-b",
            "options": {"add": ["-O1"], "omit": ["-g"]},
        }
    ]
    by = {m["name"]: m for m in got["members"]}
    assert by["f"]["unit"] == "src/IO/80001000.CPP" and [p["version"] for p in by["f"]["placements"]] == ["eu", "us"]
    assert by["D"]["unit"] is None and by["D"]["kind"] == "data"  # behind a stand-in
    assert by["D"]["placements"] == [
        {"version": "us", "section": ".rodata", "rom_start": 0x2000, "rom_end": 0x2010, "vram": 0x80002000}
    ]  # no size: it extends to the next symbol
    assert by["g"]["unit"] is None and by["g"]["fuzzy"] == "src/IO/80001000.CPP"


def test_the_evidence_refuses_a_tree_that_changed_under_the_map(tmp_path, monkeypatch):
    (tmp_path / "map.json").write_text(json.dumps(_slim()))
    monkeypatch.setattr(structure, "_unbake", lambda *a: ({"ok": True, "result": {"path": "map.json"}}, ""))
    monkeypatch.setattr(structure.api, "capture", lambda config: SimpleNamespace(digest="d2", modules=[]))
    with pytest.raises(Refusal) as caught:
        structure.evidence(SimpleNamespace(project=SimpleNamespace(root=tmp_path)))
    assert caught.value.findings[0].key == "recipe.evidence"


def test_the_fixture_is_a_real_evidence_document(doc):
    assert doc["names_from"] == "us-rev1" and doc["versions"] == ["de", "eu", "eu-x", "us", "us-rev1"]
    assert len(doc["modules"]) == 48 and {m["segment"] for m in doc["modules"]} == {
        "span_1000",
        "ovl_80400000",
        "data_800C",
    }
    assert {u["toolchain"] for u in doc["units"]} == {"gcc-2.8.1-sn64"}
    assert not [m for m in doc["members"] if m["name"].startswith(structure.LITERALS)]


# ---- the proposal


def test_every_file_is_one_object_named_by_its_address_in_its_own_folder(doc):
    got = structure.propose(doc, SETTINGS, naming=RW)
    assert sorted(m["origin"] for m in got["modules"]) == sorted(m["name"] for m in doc["modules"])
    assert all(m["keeps"] for m in got["modules"]) and got["boundaries"] == []
    module = of(got, f"{W}_2.CPP")
    assert (module["name"], module["source"]) == ("code_80214310", "src/WEAPONS/CODE_80214310.CPP")
    assert module["atoms"] == ["func_80214310_de", "func_80214624_de", "func_802149C0_de"]
    module = of(got, f"{W}_22.CPP")
    assert module["header"] == "include/WEAPONS/CODE_802171C8.H"  # the old header, renamed beside its source
    assert of(got, f"{W}_27.CPP")["name"] == "data_800C725C" and of(got, f"{W}_27.CPP")["data"]
    assert got["order"] == ["src/ANIM", "src/MENU/PAK", "src/WEAPONS"]


def test_a_file_that_keeps_its_atoms_is_renamed_never_emptied_and_every_move_is_valid(doc):
    got = structure.propose(doc, SETTINGS, naming=RW)
    flat = structure.ops(got)
    for move in flat:
        jsonschema.validate(move, MOVE)
    assert {m["op"] for m in flat} == {"module", "source", "header"}
    assert [m for m in flat if m["subject"] in (f"{W}_22.CPP", "code_802171C8")] == [
        {"op": "module", "subject": f"{W}_22.CPP", "target": "code_802171C8"},
        {"op": "source", "subject": "code_802171C8", "target": "src/WEAPONS/CODE_802171C8.CPP"},
        {"op": "header", "subject": "code_802171C8", "target": "include/WEAPONS/CODE_802171C8.H"},
    ]


def test_a_module_that_already_has_the_proposed_paths_needs_no_move(doc):
    got = structure.propose(doc, SETTINGS, naming=RW)
    for module in doc["modules"]:
        made = of(got, module["name"])
        module["source"], module["header"] = made["source"], made["header"]
    again = structure.propose(doc, SETTINGS, naming=RW)
    assert {m["op"] for m in structure.ops(again)} == {"module"}  # only the name: no file changes


def test_the_scope_is_the_segments_asked_for(doc):
    got = structure.propose(doc, SETTINGS, "data_800C")
    assert got["scope"] == ["data_800C"] and {m["origin"] for m in got["modules"]} == {
        f"{W}_{n}.CPP" for n in range(23, 28)
    }
    assert got["estimate"]["modules"] == (48, 48)


def _pad(doc):
    """A C padding unit of its own right after the last datum of WEAPONS/80213ED4_27."""
    last = next(m for m in doc["members"] if m["name"] == "D_800C2178_de")
    path = f"{W}_PAD.CPP"
    places = [
        {**p, "rom_start": p["rom_end"], "rom_end": p["rom_end"] + 4, "vram": p["vram"] + 4} for p in last["placements"]
    ]
    doc["members"].append({**last, "name": "D_padding_800C217C", "module": path, "unit": path, "placements": places})
    doc["modules"].append(
        {"name": path, "source": path, "header": "", "segment": "data_800C", "members": ["D_padding_800C217C"]}
    )
    doc["units"].append({**doc["units"][0], "path": path, "module": path, "members": ["D_padding_800C217C"]})


def test_a_file_of_padding_only_folds_into_the_file_before_it(doc):
    _pad(doc)
    got = structure.propose(doc, SETTINGS)
    host = holding(got, "D_padding_800C217C")
    assert host["origin"] == f"{W}_27.CPP" and host["atoms"][-1] == "D_padding_800C217C"
    assert {"op": "member", "subject": "D_padding_800C217C", "target": host["name"]} in structure.ops(got)
    assert got["estimate"]["padding_units"] == (1, 0) and got["gates"]["padding_zero"]


ROW = {"first": "func_80214178_de", "last": "func_80214310_de", "source": "GAME/WEAPONS/GUN.CPP"}


def test_an_evidence_row_joins_the_files_it_spans_and_cuts_them_from_the_rest(doc):
    got = structure.propose(doc, SETTINGS, evidence=[ROW])
    module = holding(got, "func_80214178_de")
    assert module["atoms"] == ["func_80214178_de", "func_80214310_de"] and module["evidence"] == ROW["source"]
    assert (module["source"], module["header"]) == ("src/game/game/weapons/gun.c", "include/game/game/weapons.h")
    assert holding(got, "func_80213ED4_de")["atoms"] == ["func_80213ED4_de"]  # the rest of each file stays
    assert holding(got, "func_80214624_de")["atoms"] == ["func_80214624_de", "func_802149C0_de"]
    assert got["boundaries"] == [ROW] and got["order"][0] == "src/game/game/weapons"
    moves = structure.ops(got)
    assert {"op": "source", "subject": module["name"], "target": "src/game/game/weapons/gun.c"} in moves
    assert {"op": "member", "subject": "func_80214310_de", "target": module["name"]} in moves


def test_tied_origin_votes_choose_the_module_by_name(doc):
    got = structure.propose(doc, SETTINGS, evidence=[ROW])
    module = holding(got, "func_80214178_de")
    assert module["origin"] == f"{W}.CPP" and not module["keeps"]  # the rest of 80213ED4.CPP keeps the file


def _spell(**by_unit):
    """`types(unit)` as the clash reader gives it, from the C text each unit defines."""
    return lambda unit: clash.definitions(by_unit.get(unit, ""))


def test_files_that_define_one_type_differently_stay_apart_inside_an_evidence_row(doc):
    clashing = _spell(**{f"{W}.CPP": "struct S {int a;};", f"{W}_2.CPP": "struct S {char b[4];};"})
    got = structure.propose(doc, SETTINGS, types=clashing, evidence=[ROW])
    assert holding(got, "func_80214178_de")["atoms"] == ["func_80214178_de"]
    assert holding(got, "func_80214310_de")["atoms"] == ["func_80214310_de"]
    same = _spell(**{f"{W}.CPP": "struct S {int a;};", f"{W}_2.CPP": "struct S {int a;}; void f(int);"})
    got = structure.propose(doc, SETTINGS, types=same, evidence=[ROW])
    assert holding(got, "func_80214178_de")["atoms"] == ["func_80214178_de", "func_80214310_de"]


def test_an_evidence_path_names_a_folder_and_a_file():
    assert structure.evidenced_path("GAME/WORLD/CAMERA.CPP") == ("game/world", "src/game/game/world/camera.c")
    assert structure.evidenced_path("MAIN.C") == ("", "src/game/main.c")


UPPER_83 = tree.Naming(
    case="upper",
    stem_max=8,
    source_ext=".CPP",
    stem="{vram}",
    evidence="keep",
    subsystem_source="src/{subsystem}",
    subsystem_header="include/{subsystem}.H",
)


def test_one_proposal_under_two_naming_styles(doc):
    plain, styled = structure.propose(doc, SETTINGS), structure.propose(doc, SETTINGS, naming=UPPER_83)
    assert names(plain) == names(styled)  # the style names files, never cuts objects
    assert of(plain, f"{W}_2.CPP")["source"] == "src/WEAPONS/code_80214310.c"
    assert of(styled, f"{W}_2.CPP")["source"] == "src/WEAPONS/80214310.CPP"


def test_evidence_casing_survives_when_the_style_keeps_it(doc):
    got = structure.propose(doc, SETTINGS, evidence=[ROW], naming=UPPER_83)
    module = holding(got, "func_80214178_de")
    assert (module["source"], module["header"]) == ("src/GAME/WEAPONS/GUN.CPP", "include/GAME/WEAPONS.H")
    lower = tree.Naming(**{**vars(UPPER_83), "evidence": "lower"})
    assert structure.evidenced_path("GAME/ACTORS/PLAYER.CPP", lower) == ("game/actors", "src/game/actors/player.CPP")


def test_a_stem_over_the_styles_limit_is_refused():
    with pytest.raises(ValueError, match="stem_max"):
        tree.Naming(stem_max=8).file("src/game/x", {"name": "code_80001000", "vram": 0x80001000})


@pytest.mark.parametrize("root", ["src/sdk/libgultra", "src/sdk/libgultra/"])
@pytest.mark.parametrize(
    "path, protected",
    [
        ("src/sdk/libgultra/a.c", True),
        ("src/sdk/libgultra/nested/a.c", True),
        ("src/sdk/libgultra_inferred/a.CPP", False),
        ("src/sdk/libgultra2/a.CPP", False),
    ],
)
def test_library_protection_is_a_directory_not_a_name_prefix(doc, root, path, protected):
    member = next(m for m in doc["members"] if m["name"] == "func_80213ED4_de")
    member["module"] = member["unit"] = path
    got = next(m for m in structure.atoms(doc, (root,)) if m["name"] == "func_80213ED4_de")
    assert got["protected"] is protected


def test_a_library_file_gets_no_module(doc):
    got = structure.propose(doc, SETTINGS, library=("src/WEAPONS/",))
    assert {m["origin"] for m in got["modules"]} == {m["name"] for m in doc["modules"] if "WEAPONS" not in m["name"]}


def test_the_gates_and_tree_numbers(doc):
    got = structure.propose(doc, SETTINGS)
    assert got["gates"] == {"dir_max_ok": True, "padding_zero": True, "func_files_zero": True}
    assert got["tree"] == {
        "c_files": 36,
        "data_files": 5,
        "dir_max": 27,
        "dir_median": 17,
        "depth": 3,
        "padding_files": 0,
        "func_files": 0,
    }
    assert not structure.propose(doc, state.Structure(dir_max=26))["gates"]["dir_max_ok"]


# ---- the patch

C_FILE = """#include "types.h"

void keep(void) {
    int a = 1;
}

#ifdef NON_MATCHING
void fuzzy(int x) {
    if (x) { x = 2; }
}
#else
INCLUDE_ASM(main, fuzzy);
#endif

INCLUDE_ASM(main, func_8000);

void tail(void) {
}
"""


def _repo(tmp_path, files):
    """A git repository holding `files` (path -> text), committed."""
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    for argv in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(tmp_path), *argv], check=True, capture_output=True)
    return tmp_path


def _check(root, patch, *flags):
    return subprocess.run(
        ["git", "-C", str(root), "apply", *flags, "-"], input=patch.encode(), capture_output=True, check=False
    )


MODULES = {"old": {"source": "src/a.c", "header": "include/a.h"}}


def test_one_member_move_is_a_diff_git_apply_accepts_that_makes_the_target_file(tmp_path):
    root = _repo(tmp_path, {"src/a.c": C_FILE, "include/a.h": ""})
    moves = [
        {"op": "member", "subject": "func_8000", "target": "new"},
        {"op": "source", "subject": "new", "target": "src/game/new.c"},
    ]
    got = structure.build(root, moves, {"func_8000": "src/a.c"}.get, MODULES)
    assert got["moves"] == 1 and got["files"] == ["src/a.c", "src/game/new.c"]
    assert _check(root, got["patch"], "--check").returncode == 0
    _check(root, got["patch"])
    assert "func_8000" not in (root / "src/a.c").read_text()
    made = (root / "src/game/new.c").read_text()
    assert made == '#include "types.h"\n\nINCLUDE_ASM(main, func_8000);\n'


def test_a_fuzzy_function_moves_with_its_whole_non_matching_block_and_a_c_body_with_its_braces(tmp_path):
    root = _repo(tmp_path, {"src/a.c": C_FILE, "src/b.c": "int b;\n"})
    modules = {"old": {"source": "src/a.c", "header": ""}, "other": {"source": "src/b.c", "header": ""}}
    moves = [{"op": "member", "subject": n, "target": "other"} for n in ("fuzzy", "keep")]
    got = structure.build(root, moves, {"fuzzy": "src/a.c", "keep": "src/a.c"}.get, modules)
    assert _check(root, got["patch"], "--check").returncode == 0
    _check(root, got["patch"])
    left, took = (root / "src/a.c").read_text(), (root / "src/b.c").read_text()
    assert "fuzzy" not in left and "#else" not in left and "keep" not in left and "tail" in left
    assert took.startswith("int b;\n") and "#ifdef NON_MATCHING" in took and "int a = 1;" in took


def test_a_symbol_rename_edits_symbol_addrs_and_every_word_bounded_use(tmp_path):
    root = _repo(
        tmp_path,
        {
            "versions/us/symbol_addrs.txt": "func_8000 = 0x8000; // type:func\nfunc_80001 = 0x9000;\n",
            "src/a.c": "INCLUDE_ASM(main, func_8000);\nvoid g(void) { func_8000(); func_80001(); }\n",
            "include/a.h": "void func_8000(void);\n",
        },
    )
    got = structure.build(root, [{"op": "symbol", "subject": "func_8000", "target": "osYield"}], {}.get, {})
    assert got["files"] == ["include/a.h", "src/a.c", "versions/us/symbol_addrs.txt"] and got["moves"] == 1
    assert _check(root, got["patch"], "--check").returncode == 0
    _check(root, got["patch"])
    assert (root / "versions/us/symbol_addrs.txt").read_text().startswith("osYield = 0x8000;")
    assert "func_80001()" in (root / "src/a.c").read_text() and "osYield();" in (root / "src/a.c").read_text()


def test_a_source_and_a_header_rename_are_rename_diffs_and_the_includes_follow(tmp_path):
    root = _repo(
        tmp_path,
        {"src/a.c": '#include "x/a.h"\n', "include/x/a.h": "int a;\n", "src/c.c": "#include <x/a.h>\n"},
    )
    modules = {"m": {"source": "src/a.c", "header": "include/x/a.h"}}
    moves = [
        {"op": "source", "subject": "m", "target": "src/game/m.c"},
        {"op": "header", "subject": "m", "target": "include/game/m.h"},
    ]
    got = structure.build(root, modules=modules, moves=moves, owner={}.get)
    assert "rename from src/a.c" in got["patch"] and "rename to include/game/m.h" in got["patch"]
    assert _check(root, got["patch"], "--check").returncode == 0
    _check(root, got["patch"])
    assert (root / "src/game/m.c").read_text() == '#include "game/m.h"\n'
    assert (root / "src/c.c").read_text() == "#include <game/m.h>\n"
    assert not (root / "src/a.c").exists() and (root / "include/game/m.h").exists()


def test_a_header_move_edits_the_include_lines(tmp_path):
    root = _repo(tmp_path, {"src/a.c": '#include "old.h"\n#include "other.h"\n', "include/old.h": "int a;\n"})
    got = structure.build(root, [{"op": "file", "subject": "include/old.h", "target": "include/types.h"}], {}.get, {})
    _check(root, got["patch"])
    assert (root / "src/a.c").read_text() == '#include "types.h"\n#include "other.h"\n'


def _fake_unbake(monkeypatch, answers):
    calls = []

    def run(config, host, *args):
        calls.append(args)
        return answers.pop(0), ""

    monkeypatch.setattr(structure, "_unbake", run)
    return calls


def _project(tmp_path, monkeypatch, doc):
    """The fixture's files on disk, its evidence and a tree that knows each atom's file."""
    for m in doc["modules"]:
        (tmp_path / m["source"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / m["source"]).write_text("int x;\n")
    monkeypatch.setattr(structure, "evidence", lambda config, host=None: doc)
    owners = {a["name"]: a["module"] for a in doc["members"]}
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path, libraries=()), owners=owners)


def test_structure_prints_the_patch_files_and_moves_and_saves_the_patch(tmp_path, monkeypatch, doc):
    config = _project(tmp_path, monkeypatch, doc)
    got = structure.run(config, {"host": None, "segment": None})
    out = json.loads(got["stream"])
    assert set(out) == {"patch", "files", "moves"} and out["moves"] == got["moves"] == 48
    assert (tmp_path / got["path"]).read_text() == got["patch"] and got["path"] == ".unbake/recipe/patches/patch-1.diff"
    assert got["modules"] == {"before": 48, "after": 48} and "src/WEAPONS/CODE_80214310.CPP" in got["files"]


def _scope(doc, *files):
    got = structure.propose(doc, SETTINGS)
    closure = structure.scope_modules(doc, got, files)
    return got, closure, structure.scoped(got, closure)


def test_a_scope_is_the_modules_holding_the_files_and_the_modules_linked_to_them(doc):
    _, closure, _ = _scope(doc, f"{W}_2.CPP")
    assert closure == {f"{W}_2.CPP", "code_80214310"}
    _, closure, _ = _scope(doc, "include/WEAPONS/80213ED4_22.H")  # a module's header holds it too
    assert closure == {f"{W}_22.CPP", "code_802171C8"}
    got = structure.propose(doc, SETTINGS, evidence=[ROW])
    closure = structure.scope_modules(doc, got, [f"{W}.CPP"])  # the row ties the two files together
    assert closure >= {f"{W}.CPP", f"{W}_2.CPP"}


def test_a_scoped_proposal_holds_only_the_closure(doc):
    got, closure, narrow = _scope(doc, f"{W}_2.CPP")
    assert [m["origin"] for m in narrow["modules"]] == [f"{W}_2.CPP"] and len(got["modules"]) == 48
    assert {m["op"] for m in structure.ops(narrow)} == {"module", "source"}
    structure.confine(narrow, closure)


def test_a_move_outside_the_closure_is_refused_naming_its_file(doc):
    got, closure, _ = _scope(doc, f"{W}_2.CPP")
    with pytest.raises(Refusal) as caught:
        structure.confine(got, closure)
    assert "outside the scope" in str(caught.value.findings[0])


def test_a_scope_no_module_holds_is_refused(doc):
    with pytest.raises(Refusal):
        structure.scope_modules(doc, structure.propose(doc, SETTINGS), ["src/nothing.c"])


def test_structure_without_paths_is_unchanged_and_with_paths_is_the_narrow_patch(tmp_path, monkeypatch, doc):
    config = _project(tmp_path, monkeypatch, doc)
    whole = structure.run(config, {"host": None, "segment": None})
    same = structure.run(config, {"host": None, "segment": None, "paths": None})
    assert same["patch"] == whole["patch"] and same["moves"] == whole["moves"] and "suggestions" not in whole
    narrow = structure.run(config, {"host": None, "segment": None, "paths": [f"./{W}_2.CPP"]})
    assert narrow["files"] == [f"{W}_2.CPP", "src/WEAPONS/CODE_80214310.CPP"] and narrow["moves"] == 1
    assert narrow["findings"] == [] and [m["op"] for m in narrow["suggestions"]] == ["module", "source"]


def test_a_scope_no_module_holds_is_a_finding_with_an_empty_proposal_not_an_error(tmp_path, monkeypatch, doc):
    config = _project(tmp_path, monkeypatch, doc)
    got = structure.run(config, {"host": None, "segment": None, "paths": ["src/nothing.c"]})
    assert got["moves"] == 0 and got["path"] == "" and got["suggestions"] == []
    assert "src/nothing.c" in got["findings"][0]
    assert not (tmp_path / ".unbake/recipe/patches").exists()


def test_apply_runs_git_apply_then_unbake_sync_and_returns_syncs_result(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"src/a.c": "INCLUDE_ASM(main, func_8000);\n"})
    built = structure.build(root, [{"op": "symbol", "subject": "func_8000", "target": "osYield"}], {}.get, {})
    saved = root / ".unbake/recipe/patches"
    structure.write(saved / "patch-1.diff", built["patch"])
    calls = _fake_unbake(monkeypatch, [{"ok": True, "result": {"changed": ["Makefile"]}, "findings": []}])
    got = structure.apply(SimpleNamespace(project=SimpleNamespace(root=root)), {"host": None})
    assert calls == [("sync",)] and got["changed"] == ["Makefile"] and got["left"] == 0
    assert "osYield" in (root / "src/a.c").read_text() and (saved / "patch-1.applied").exists()
    with pytest.raises(Refusal) as caught:
        structure.apply(SimpleNamespace(project=SimpleNamespace(root=root)), {"host": None})
    assert caught.value.findings[0].action == "recipe structure"


def test_a_patch_git_refuses_stays_saved_and_sync_is_not_run(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"src/a.c": "x\n"})
    structure.write(root / ".unbake/recipe/patches/patch-1.diff", "garbage\n")
    calls = _fake_unbake(monkeypatch, [])
    with pytest.raises(Refusal) as caught:
        structure.apply(SimpleNamespace(project=SimpleNamespace(root=root)), {"host": None})
    assert "git apply refused" in caught.value.findings[0].reason and calls == []
    assert (root / ".unbake/recipe/patches/patch-1.diff").exists()


def test_a_failing_sync_is_refused_with_unbakes_findings(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"src/a.c": "x\n"})
    patch = structure.build(root, [{"op": "symbol", "subject": "x", "target": "y"}], {}.get, {})["patch"]
    structure.write(root / ".unbake/recipe/patches/patch-1.diff", patch)
    bad = {"ok": False, "result": None, "findings": [{"key": "config.schema", "reason": "schema 3"}]}
    monkeypatch.setattr(structure, "_unbake", lambda *a: (bad, ""))
    monkeypatch.setattr(structure, "_refusal", lambda document: Refusal(Finding("config.schema", "schema 3")))
    with pytest.raises(Refusal) as caught:
        structure.apply(SimpleNamespace(project=SimpleNamespace(root=root)), {"host": None})
    assert caught.value.findings[0].key == "config.schema"
