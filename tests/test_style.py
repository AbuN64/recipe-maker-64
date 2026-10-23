"""recipe style: one fixture per rule (hit and clean) and the parse cache by work count."""

from types import SimpleNamespace

import pytest

from recipemaker import style

CLEAN_HEADER = """#ifndef GAME_MAIN_ACTOR_H
#define GAME_MAIN_ACTOR_H
#include "types.h"
#define ACTOR_MAX 4
typedef struct ActorRecord { int x; } ActorRecord;
extern int g_actorCount;
void Actor_Spawn(int kind);
#endif
"""
CLEAN_SOURCE = """#include "actor.h"
#include "audio.h"
#include "menu.h"

/*
================
Actor_Spawn

Spawns an actor.
us 80001000
================
*/
void Actor_Spawn(int kind) {
    g_actorCount = kind;
}
"""
BAD_SOURCE = """#include "menu.h"
#include "audio.h"
#include "actor.h"
#define bad_macro 1
typedef struct { int x; } actor_rec;
int actorCount;
static int counter;
void spawn_actor(int kind) { return; }
void Actor_DoStuff(int kind) { int temp_v0 = sp20; }
/*
================
Actor_Old

Renamed.
us 80001234
================
*/
void Actor_New(void) {}
/* xx: reads the old pad table */
"""

FILES = {
    "src/game/main/actor/actor_spawn.c": CLEAN_SOURCE,
    "include/game/main/actor.h": CLEAN_HEADER,
}


def project(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path, versions=("us",)))


SYMBOLS = {
    "us": {"Actor_Spawn": (0x80001000, 0x1000, 0x40, "func")},
    "eu": {"Actor_Spawn": (0x80001200, 0x1200, 0x40, "func")},
}


@pytest.fixture(autouse=True)
def symbols(monkeypatch):
    """`api.symbols` per version, and the versions it was asked for."""
    asked = []

    def fake(config, version):
        asked.append(version)
        return SYMBOLS.get(version, {})

    monkeypatch.setattr(style.api, "symbols", fake)
    return asked


@pytest.fixture(autouse=True)
def tracked(monkeypatch):
    """`git ls-files` over a plain directory: every file but the tool's own."""

    def git(config, *args):
        root = config.project.root
        found = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".unbake" not in p.parts]
        return SimpleNamespace(stdout="\n".join(found).encode())

    monkeypatch.setattr(style.host, "git", git)


@pytest.fixture(autouse=True)
def inline_pool(monkeypatch):
    jobs = []

    def fake(config, name, function, items, key=None):
        jobs.append(len(items))
        return [function(i) for i in items]

    monkeypatch.setattr(style.api, "pool_map", fake)
    return jobs


def run(config, *paths):
    return style.run(config, {"paths": list(paths)})


def rules(result):
    return sorted((f["rule"], f["detail"]) for f in result["findings"])


def test_clean_files_have_no_finding(tmp_path):
    got = run(project(tmp_path, FILES))
    assert got["findings"] == [] and got["ok"] is True and got["exit"] == 0


def test_name_rules_hit_each_kind_and_pass_neutral_names(tmp_path):
    files = {"src/game/main/actor/actor_spawn.c": BAD_SOURCE + "void func_80001000(void) {}\nint D_80001000;\n"}
    got = {r: d for r, d in rules(run(project(tmp_path, files)))}
    seen = [(r, d) for r, d in rules(run(project(tmp_path, files)))]
    assert ("function-name", "spawn_actor") in seen
    assert ("global-name", "actorCount") in seen and ("static-name", "counter") in seen
    assert ("type-name", "actor_rec") in seen and ("macro-name", "bad_macro") in seen
    assert ("banned-word", "Actor_DoStuff (Do)") in seen
    assert not [d for r, d in seen if "func_80001000" in d or "D_80001000" in d]
    assert got


def test_a_name_the_debug_catalog_spells_so_is_the_original_and_no_finding(tmp_path, monkeypatch):
    from types import SimpleNamespace

    original = SimpleNamespace(name="actor_rec", tag="")
    catalog = SimpleNamespace(records=[original], functions=[SimpleNamespace(name="spawn_actor")], globals=[])
    monkeypatch.setattr(style.debug, "load", lambda config, source=None: catalog)
    files = {"src/game/main/actor/actor_spawn.c": BAD_SOURCE}
    seen = rules(run(project(tmp_path, files)))
    assert ("type-name", "actor_rec") not in seen and ("function-name", "spawn_actor") not in seen
    assert ("macro-name", "bad_macro") in seen and ("global-name", "actorCount") in seen


def test_file_dir_and_path_rules(tmp_path):
    files = {
        "src/game/main/actor/menu_draw.c": "int g_a;\n",  # the stem does not start with the folder
        "src/game/main/actor/code_80001000_592.c": "int g_a;\n",  # a collision counter
        "src/game/main/Bad-Dir/bad.c": "int g_a;\n",
        "src/game/main/actor/func_80001000.c": "int g_a;\n",  # named after a function
        "src/fuzzy/func_80001000.c": "int g_a;\n",  # the fuzzy mirror is exempt
        "src/game/main/audio/audio_mix.c": "int g_a;\n",
        "src/game/main/menu/audio_mix.c": "int g_a;\n",  # basename repeated
    }
    got = run(project(tmp_path, files))
    seen = {(f["rule"], f["path"]) for f in got["findings"]}
    assert ("file-name", "src/game/main/actor/menu_draw.c") in seen
    assert ("collision-suffix", "src/game/main/actor/code_80001000_592.c") in seen
    assert ("dir-name", "src/game/main/Bad-Dir") in seen
    assert ("tool-named-path", "src/game/main/actor/func_80001000.c") in seen
    assert not [p for r, p in seen if p.startswith("src/fuzzy")]
    assert ("file-name", "src/game/main/menu/audio_mix.c") in seen


def test_more_than_the_cap_of_files_in_a_folder_fails(tmp_path):
    many = {f"src/game/main/actor/actor_x{i:02d}.c": "int g_a;\n" for i in range(33)}
    assert [f["rule"] for f in run(project(tmp_path, many))["findings"]] == ["files-per-dir"]
    few = {f"src/game/main/actor/actor_x{i:02d}.c": "int g_a;\n" for i in range(32)}
    assert run(project(tmp_path / "b", few))["findings"] == []


def test_header_order_guard_include_order_and_banner_rules(tmp_path):
    header = CLEAN_HEADER.replace("GAME_MAIN_ACTOR_H", "UNBAKE_ACTOR_H").replace("extern int g_actorCount;\n", "")
    header = header.replace("void Actor_Spawn(int kind);\n", "void Actor_Spawn(int kind);\nextern int g_actorCount;\n")
    files = {"include/game/main/actor.h": header, "src/game/main/actor/actor_spawn.c": BAD_SOURCE}
    seen = {(f["rule"], f["path"]) for f in run(project(tmp_path, files))["findings"]}
    assert ("guard-tool", "include/game/main/actor.h") in seen
    assert ("header-order", "include/game/main/actor.h") in seen  # an extern after a prototype
    assert ("include-order", "src/game/main/actor/actor_spawn.c") in seen
    details = {f["detail"] for f in run(project(tmp_path, files))["findings"] if f["rule"].startswith("banner")}
    assert details == {"Actor_Old above Actor_New"}  # the address has no placement, so it is not judged


def test_a_banner_address_must_equal_the_symbol_table_of_its_version(tmp_path, symbols):
    files = {"src/game/main/actor/actor_spawn.c": CLEAN_SOURCE.replace("80001000", "80001004")}
    got = run(project(tmp_path, files))
    assert [(f["rule"], f["detail"]) for f in got["findings"]] == [("banner-address", "Actor_Spawn us 80001004")]
    assert symbols == ["us"]  # the tables of the configured versions, read once


def test_a_banner_of_another_version_is_judged_by_that_versions_table(tmp_path):
    source = CLEAN_SOURCE.replace("us 80001000", "eu 80001200")
    config = project(tmp_path, {"src/game/main/actor/actor_spawn.c": source})
    config.project.versions = ("us", "eu")
    assert run(config)["findings"] == []
    wrong = CLEAN_SOURCE.replace("us 80001000", "eu 80001000")
    (tmp_path / "src/game/main/actor/actor_spawn.c").write_text(wrong)
    assert [f["detail"] for f in run(config)["findings"]] == ["Actor_Spawn eu 80001000"]


def test_a_version_word_must_be_a_configured_version(tmp_path):
    files = {"src/game/main/actor/actor_spawn.c": "/* us: fine */\n/* xx: not a version */\nint g_a;\n"}
    assert [(f["rule"], f["detail"]) for f in run(project(tmp_path, files))["findings"]] == [("version-word", "xx")]


def test_m2c_locals_are_counted_as_a_warning_that_never_fails_a_form_check(tmp_path):
    files = {"src/game/main/actor/actor_spawn.c": "void Actor_Spawn(void) { int temp_v0 = sp20; }\n"}
    got = run(project(tmp_path, files))
    assert [f["rule"] for f in got["findings"]] == ["m2c-local"] and got["ok"] is True and got["exit"] == 0


def test_paths_check_only_those_paths(tmp_path):
    config = project(tmp_path, {**FILES, "src/game/main/actor/actor_bad.c": BAD_SOURCE})
    assert run(config, "src/game/main/actor/actor_spawn.c")["findings"] == []
    got = run(config, "src/game/main/actor/actor_bad.c")
    assert got["ok"] is False and got["exit"] == 1 and got["files"] == 1


def test_the_parse_cache_parses_a_file_content_once(tmp_path, inline_pool):
    config = project(tmp_path, FILES)
    first = run(config)
    assert first["parsed"] == 2 and inline_pool == [2]
    second = run(config)
    assert second["parsed"] == 0 and inline_pool == [2]  # the pool got no job
    (tmp_path / "src/game/main/actor/actor_spawn.c").write_text(CLEAN_SOURCE + "\n")
    assert run(config)["parsed"] == 1 and inline_pool == [2, 1]  # only the changed content


def test_a_listing_without_paths_comes_from_git(tmp_path, monkeypatch):
    config = project(tmp_path, {**FILES, "notes.txt": "x"})
    ran = SimpleNamespace(stdout=b"include/game/main/actor.h\nsrc/game/main/actor/actor_spawn.c\n")
    monkeypatch.setattr(style.host, "git", lambda *a: ran)
    assert run(config)["files"] == 2


def test_macro_bodies_and_neutral_hex_names_are_no_findings(tmp_path):
    macro = "#define aSet(pkt, f) \\\n    { (pkt)->w0 = f; (pkt)->w1 = 2; }\nint g_a;\n"
    files = {"include/game/main/actor.h": macro, "src/game/main/actor/code_80001000.c": "int g_a;\n"}
    files["include/game/main/code_80001000.h"] = "int g_a;\n"
    got = run(project(tmp_path, files))
    assert [(f["rule"], f["detail"]) for f in got["findings"]] == [("macro-name", "aSet")]
    assert style.collision("code_80406A88_592") and not style.collision("code_80406A88")


def test_an_anonymous_enum_is_no_global(tmp_path):
    files = {"include/game/main/actor.h": "enum { ACTOR_A, ACTOR_B };\nint g_a;\n"}
    assert run(project(tmp_path, files))["findings"] == []


def test_uppercase_cpp_and_h_are_audited_and_an_audit_of_nothing_fails(tmp_path):
    config = project(tmp_path, {"src/game/MENU/80424E30.CPP": "int D_80100000;\n", "src/game/MENU/ITEMS.H": "int x;\n"})
    got = run(config)
    assert (got["audited"], got["parsed"]) == (2, 2) and "nothing-audited" not in rules(got)[0]
    bare = run(project(tmp_path / "bare", {"README.md": "no source\n"}))
    assert bare["audited"] == 0 and bare["ok"] is False and bare["exit"] == 1
    assert [f["rule"] for f in bare["findings"]] == ["nothing-audited"]


def _against_base(monkeypatch, root, touched, before):
    """A project whose base commit held `before` (path -> text) and whose change touched `touched`."""
    listing = tracked_listing = lambda config: [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]

    def git(config, *args):
        if args[0] == "ls-files" and "--others" not in args:
            return SimpleNamespace(stdout="\n".join(tracked_listing(config)).encode())
        out = {"diff": touched, "ls-files": [], "ls-tree": [*before]}
        return SimpleNamespace(stdout="\0".join(out[args[0]]).encode())

    def show(argv, cwd, config, **_):
        path = argv[-1].split(":", 1)[1]
        return SimpleNamespace(exit=0, stdout=before[path].encode())

    monkeypatch.setattr(style.host, "git", git)
    monkeypatch.setattr(style.host, "run", show)
    return listing


def test_only_the_findings_a_change_introduces_gate_and_the_base_debt_is_reported(tmp_path, monkeypatch):
    old, mine = "src/game/main/actor/actor_old.c", "src/game/main/actor/actor_spawn.c"
    debt = "int actorCount;\n"  # a bad global name the base already had
    config = project(tmp_path, {old: debt, mine: CLEAN_SOURCE + debt})
    _against_base(monkeypatch, tmp_path, [mine], {old: debt, mine: CLEAN_SOURCE + debt})
    got = style.run(config, {"paths": [], "base": "HEAD"})
    assert got["ok"] is True and got["findings"] == []
    assert sorted(f["path"] for f in got["debt"]) == [old, mine]
    (tmp_path / mine).write_text(CLEAN_SOURCE + debt + "int otherCount;\n")
    got = style.run(config, {"paths": [], "base": "HEAD"})
    assert [(f["rule"], f["detail"]) for f in got["findings"]] == [("global-name", "otherCount")]
    assert got["ok"] is False and len(got["debt"]) == 2
    assert style.run(config, {"paths": [], "base": "HEAD", "full": True})["debt"] == []


def test_generated_placeholders_are_neutral_names_of_every_kind():
    for name in (
        "D_auto_data_unresolved_800E42D8",
        "D_blob_e67cb8f9f48e076798ff",
        "D_800E42D8",
        "unresolved_x",
        "func_800E42D8_us",
    ):
        assert style.neutral(name), name
    assert not style.neutral("g_selectionLayoutTail") and not style.neutral("Menu_Draw")
