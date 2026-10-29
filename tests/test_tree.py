"""The folder tree: segment labels, module identities and paths in the project's naming style."""

import json
from pathlib import Path

import pytest
from r1_fake_api import fake_api  # noqa: F401
from test_structure import DATA, SETTINGS, W, names, of

from recipemaker import structure, tree


@pytest.fixture
def doc():
    return json.loads(Path(DATA).read_text(encoding="utf-8"))


def test_segments_with_code_are_main_and_overlays_by_their_lowest_vram(doc):
    assert tree.labels(doc) == {"span_1000": "main", "ovl_80400000": "ovl_80444D50", "data_800C": "ovl_800C209C"}
    one = {**doc, "modules": [m for m in doc["modules"] if m["segment"] == "span_1000"]}
    one["members"] = [m for m in doc["members"] if m["segment"] == "span_1000"]
    assert set(tree.labels(one).values()) == {""}  # a one-segment game has no level


def test_a_collision_suffix_is_the_second_atoms_vram_never_a_counter(doc):
    got = structure.propose(doc, SETTINGS)
    every = {a["name"]: a for a in structure.atoms(doc)}
    pair = [every["func_80214310_de"], every["func_80214624_de"]]
    assert tree.unique("code_80214310", {"code_80214310"}, pair, "us-rev1") == "code_80214310_80214624"
    assert not [n for n in names(got) if n.rsplit("_", 1)[-1].isdigit() and len(n.rsplit("_", 1)[-1]) < 4]


def test_a_name_another_file_already_has_takes_the_suffix(doc):
    module = next(m for m in doc["modules"] if m["name"] == f"{W}_3.CPP")
    module["name"] = "code_80214310"  # the name the module of 80213ED4_2 would take
    for m in doc["members"]:
        m["module"] = "code_80214310" if m["module"] == f"{W}_3.CPP" else m["module"]
    got = structure.propose(doc, SETTINGS)
    assert of(got, f"{W}_2.CPP")["name"] == "code_80214310_80214624"


@pytest.mark.parametrize(
    "naming", [tree.PLAIN, tree.Naming(case="upper", stem="{vram}", stem_max=8, source_ext=".CPP")]
)
def test_module_paths_use_the_first_atom_address_in_the_origin_folder(doc, naming):
    every = structure.atoms(doc)
    modules = [
        {"name": "code_80214310", "segment": "span_1000", "origin": f"{W}_2.CPP", "atoms": ["func_80214310_de"]},
        {
            "name": "code_8023CFC0",
            "segment": "span_1000",
            "origin": "src/ANIM/8023CFB0_2.CPP",
            "atoms": ["func_8023D010_eu_x"],
        },
    ]
    assert tree.place(doc, every, modules, naming) == ["src/ANIM", "src/WEAPONS"]
    assert [m["vram"] for m in modules] == [0x80214310, 0x8023CFC0]  # us-rev1 names: 8023D010 is eu-x
    assert [m["folder"] for m in modules] == ["src/WEAPONS", "src/ANIM"]
    assert modules[1]["header"] == f"include/ANIM/{Path(modules[1]['source']).stem}.H"
    if naming.stem == "{vram}":
        assert [Path(m["source"]).name for m in modules] == ["80214310.CPP", "8023CFC0.CPP"]


def test_two_modules_one_path_is_refused(doc):
    every = structure.atoms(doc)
    same = tree.Naming(stem="x")
    modules = [
        {"name": n, "segment": "span_1000", "origin": f"{W}_2.CPP", "atoms": ["func_80214310_de"]} for n in ("a", "b")
    ]
    with pytest.raises(ValueError, match="paths name two modules"):
        tree.place(doc, every, modules, same)


def test_a_header_follows_its_source_and_no_header_stays_none():
    assert tree.header_beside("include/IO/8020.H", "src/IO/CODE_8020.CPP") == "include/IO/CODE_8020.H"
    assert tree.header_beside("", "src/IO/CODE_8020.CPP") == ""


def _tree(tmp_path, names):
    for name in names:
        (tmp_path / "src" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / name).write_text("int x;\n")
    return tree.style(tmp_path)


def test_the_style_is_read_from_the_existing_files_by_majority(tmp_path):
    got = _tree(tmp_path, ["IO/80200500.CPP", "IO/80200600.CPP", "GAME/PLAYER.CPP", "lower.c"])
    assert (got.case, got.source_ext, got.stem_max, got.evidence) == ("upper", ".CPP", 8, "upper")


def test_lower_case_long_stems_get_no_length_limit(tmp_path):
    got = _tree(tmp_path, ["game/player_update.c", "game/actor_list.c", "game/Mixed.c"])
    assert (got.case, got.source_ext, got.stem_max) == ("lower", ".c", 0)


def test_mixed_case_stems_keep_the_spelling_and_a_project_with_no_sources_gets_the_default(tmp_path):
    assert _tree(tmp_path, ["a/Player.c", "a/ActorList.c"]).case == "as-is"
    assert tree.style(tmp_path / "missing") == tree.PLAIN and tree.style(None) == tree.PLAIN


def test_config_toml_is_not_read_for_the_style(tmp_path):
    (tmp_path / "config.toml").write_text('[naming]\ncase = "upper"\nstem_max = 3\n')
    assert _tree(tmp_path, ["a/player_update.c"]).case == "lower"


def test_evidence_paths_follow_the_projects_own_source_root(tmp_path):
    # RECIPE-TYPES-1: RW keeps src/AI/..., so src/game/AI/ENEMIES/TURRETAI.CPP was a path no merge could reach
    (tmp_path / "include" / "AI").mkdir(parents=True)
    (tmp_path / "include" / "AI" / "TURRET.H").write_text("")
    flat = _tree(tmp_path, ["AI/ENEMIES/TURRETAI_2.CPP", "IO/80200500.CPP"])
    assert flat.evidenced("AI/ENEMIES/TURRETAI.CPP") == ("AI/ENEMIES", "src/AI/ENEMIES/TURRETAI.CPP")
    assert flat.header("AI/ENEMIES") == "include/AI/ENEMIES.H"
    nested = _tree(tmp_path / "bt", ["game/sys_80076000/code_80079A5C.c"])
    assert nested.evidenced("GAME/ACTORS/PLAYER.CPP")[1].startswith("src/game/GAME/ACTORS/")  # BT: under src/game


MOVED = (
    '[layout]\nsource_roots = ["src", "sdk"]\nheader_roots = ["include", "sdk/include"]\n'
    'library_source = "sdk/{library}"\nlibrary_header = "sdk/include/{library}.h"\n'
    'subsystem_source = "src/{subsystem}"\nsubsystem_header = "src/{subsystem}.h"\n'
)


def test_the_default_layout_is_the_packaged_one(tmp_path):
    got = _tree(tmp_path, ["game/AI/ENEMY.CPP", "IO/80200500.CPP"])
    assert (got.library_source, got.source_roots, got.header_roots) == ("src/sdk/{library}", ("src",), ("include",))
    assert (got.subsystem_source, got.subsystem_header) == ("src/game/{subsystem}", "include/game/{subsystem}.h")


def test_config_toml_layout_rows_move_the_roots_and_the_library_folder(tmp_path):
    (tmp_path / "config.toml").write_text(MOVED)
    (tmp_path / "sdk" / "libultra").mkdir(parents=True)
    (tmp_path / "sdk" / "libultra" / "OS.C").write_text("int x;\n")
    got = _tree(tmp_path, ["AI/ENEMY.C", "IO/80200500.C"])
    assert (got.library_source, got.source_roots, got.header_roots) == (
        "sdk/{library}",
        ("src", "sdk"),
        ("include", "sdk/include"),
    )
    assert (got.subsystem_source, got.subsystem_header) == ("src/{subsystem}", "src/{subsystem}.h")


def test_a_project_with_no_sources_gets_its_layout_in_the_default(tmp_path):
    (tmp_path / "config.toml").write_text(MOVED)
    assert tree.style(tmp_path).library_source == "sdk/{library}"
    assert tree.style(tmp_path).subsystem_source == "src/{subsystem}"
    assert tree.style(tmp_path / "missing") == tree.PLAIN
