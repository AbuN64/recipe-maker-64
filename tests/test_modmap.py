"""`recipe structure --map` (T7): the merge of units into module files, the contiguity check and the reclass."""

from pathlib import Path

from recipemaker import modcheck, modmap, structure

UNIT_A = (
    '#include "A.H"\n#include "TYPES.H"\n\nextern int D_1;\n'
    "static inline int twice(int v) { return v * 2; }\n"
    "int fa(void) { return twice(D_1); }\n"
)
UNIT_B = (
    '#include "TYPES.H"\n#include "B.H"\n\nextern int D_1;\n'
    "static inline int twice(int v) { return v * 2; }\n"
    "int fb(void) { return twice(D_1) + 1; } /* one; two */\n"
)


def tree(root: Path, files: dict[str, str]) -> structure.Files:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return structure.Files(root)


def test_split_unit_takes_the_opening_includes():
    head, rest = modmap.split_unit(UNIT_A)
    assert head == ['#include "A.H"', '#include "TYPES.H"']
    assert rest.startswith("extern int D_1;")


def test_dedupe_drops_an_identical_helper_and_keeps_comment_semicolons():
    body = modmap.split_unit(UNIT_A)[1] + modmap.split_unit(UNIT_B)[1]
    text, clashes = modmap.dedupe(body)
    assert clashes == []
    assert text.count("twice(int v)") == 1 and text.count("extern int D_1;") == 1
    assert "/* one; two */" in text


def test_dedupe_reports_two_definitions_of_one_name():
    _, clashes = modmap.dedupe("static int h(void) { return 1; }\nstatic int h(void) { return 2; }\n")
    assert clashes == ["h"]


def test_dedupe_leaves_versioned_blocks_alone():
    text, clashes = modmap.dedupe("#if X\nint a = 1;\n#endif\n#if Y\nint a = 1;\n#endif\n")
    assert clashes == [] and text.count("int a") == 2


def test_disagreements_names_a_declaration_with_two_spellings():
    src = {"a": "extern int D_1;\n", "b": "extern char D_1;\nextern int D_2;\n"}
    assert modmap.disagreements(["a", "b"], src) == ["D_1"]


def test_merge_module_merges_units_and_headers(tmp_path):
    files = tree(
        tmp_path,
        {
            "src/g/A1.CPP": UNIT_A,
            "src/g/B1.CPP": UNIT_B,
            "include/g/A1.H": "#ifndef G_A1_H\n#define G_A1_H\nextern int fa(void);\n#endif\n",
            "include/g/B1.H": '#ifndef G_B1_H\n#define G_B1_H\n#include "g/A1.H"\nextern int fb(void);\n#endif\n',
            "src/g/USER.CPP": '#include "g/B1.H"\n',
        },
    )
    src = {u: files.get(u) or "" for u in ("src/g/A1.CPP", "src/g/B1.CPP")}
    headers: dict[str, str] = {}
    text, why = modmap.merge_module(
        files, "src/g/MOD.CPP", ["src/g/A1.CPP", "src/g/B1.CPP"], "include/g/MOD.H", headers, src, []
    )
    assert why == ""
    assert text.count('#include "g/MOD.H"') == 1 and text.count("TYPES.H") == 1
    assert headers == {"g/A1.H": "g/MOD.H", "g/B1.H": "g/MOD.H"}
    header = files.get("include/g/MOD.H") or ""
    assert "extern int fa" in header and "extern int fb" in header and "g/A1.H" not in header
    modmap.rewrite_includes(files, headers)
    assert files.get("src/g/USER.CPP") == '#include "g/MOD.H"\n'
    assert files.get("include/g/A1.H") is None


def test_merge_module_refuses_assembler(tmp_path):
    files = tree(tmp_path, {"src/a.s": "nop\n", "src/b.s": "nop\n"})
    _, why = modmap.merge_module(files, "src/m.s", ["src/a.s", "src/b.s"], "-", {}, {"src/a.s": "", "src/b.s": ""}, [])
    assert "assembler" in why


def test_prepare_const_declares_definitions_const():
    src = {"u": "unsigned int D_a[2] = {1, 2};\nstatic int s = 3;\nextern int e;\n"}
    assert modmap.prepare(src, "const", "u") == ""
    assert (
        "const unsigned int D_a[2]" in src["u"] and "static const int s" in src["u"] and "const int e" not in src["u"]
    )
    assert "by hand" in modmap.prepare(src, "text", "u")


def test_chosen_skips_finished_modules_and_cuts_at_the_unit_limit():
    def row(unit, module, kind="code"):
        return {"unit": unit, "module": module, "kind": kind}

    mods = modmap.groups(
        [row("a", "M1"), row("b", "M1"), row("c", "c"), row("d", "M2"), row("e", "M2"), row("f", "M3")]
    )
    assert list(modmap.chosen(mods)) == ["M1", "M2", "M3"]
    assert list(modmap.chosen(mods, ["M"], 3)) == ["M1", "M3"]


FX = {
    "us": [
        {"o": ".text", "k": "entry", "u": "a", "c": "text", "al": 4, "n": 8, "at": 0, "missing": False},
        {"o": ".text", "k": "entry", "u": "x", "c": "text", "al": 4, "n": 8, "at": 8, "missing": False},
        {"o": ".text", "k": "entry", "u": "b", "c": "text", "al": 4, "n": 8, "at": 16, "missing": False},
        {"o": ".rodata", "k": "entry", "u": "a", "c": "rodata", "al": 4, "n": 4, "at": 0, "missing": False},
        {"o": ".rodata", "k": "entry", "u": "p", "c": "data", "al": 4, "n": 4, "at": 4, "missing": False},
        {"o": ".rodata", "k": "entry", "u": "b", "c": "rodata", "al": 4, "n": 4, "at": 8, "missing": False},
    ]
}


def test_check_fails_a_module_with_a_foreign_entry_between_and_asks_a_reclass_for_padding():
    fails, _soft, found = modcheck.check(FX, [("a", "M"), ("b", "M"), ("p", "M"), ("x", "X")], lambda u: "")
    assert [(f[0], f[2]) for f in fails] == [("M", "text")]
    assert [(r[2], r[4]) for r in found if r[4] != "pad"] == [("p", "rodata")]


def test_reclassify_drops_zero_padding_and_constifies_other_data():
    found = [("M", "us", "p", "data", "rodata", ".rodata", 4, 4, 8, 4)]
    zero = {"us": (bytes(16), {".rodata": 0})}
    live = {"us": (b"\0" * 4 + b"\1\1\1\1" + bytes(8), {".rodata": 0})}
    assert modcheck.reclassify(found, zero)[0].action == "drop"
    assert modcheck.reclassify(found, live)[0].action == "const"
    # zero bytes the next entry's alignment would not pad (16 pads 12 here, not 4) stay as data
    assert modcheck.reclassify([("M", "us", "p", "data", "rodata", ".rodata", 4, 4, 16, 4)], zero)[0].action == "const"
    assert modcheck.reclassify([("M", "us", "p", "data", "text", ".text", 4, 4, 8, 4)], zero)[0].action == "text"


def test_reclassify_drops_a_rodata_unit_that_is_only_the_padding_the_merged_object_makes():
    # 3 zero bytes at 5 (object-relative) before a 4-aligned entry: gcc pads exactly that; at 6 it would pad 2
    zero = {"us": (bytes(16), {".rodata": 0})}
    pad = [("M", "us", "z", "rodata", "pad", ".rodata", 5, 3, 4, 5)]
    assert [r.action for r in modcheck.reclassify(pad, zero)] == ["drop"]
    assert modcheck.reclassify([("M", "us", "z", "rodata", "pad", ".rodata", 6, 3, 4, 6)], zero) == []
    # one version where the bytes are live keeps the unit
    live = {"us": (b"\0" * 5 + b"\1" + bytes(10), {".rodata": 0}), "eu": zero["us"]}
    assert modcheck.reclassify([*pad, ("M", "eu", "z", "rodata", "pad", ".rodata", 5, 3, 4, 5)], live) == []


def test_reserved_name_hint_spells_the_style_form():
    from recipemaker import naming

    assert naming.reserved_hint("__Cache___RecycleNode").endswith(
        "write Cache_RecycleNode (Module_VerbNoun, one underscore)"
    )
    assert naming.reserved_hint("Cache_RecycleNode") == ""


DEFINER = "unsigned char D_1[8] = { 1 };\nint fa(void) { return D_1[0]; }\n"
USER = "extern int D_1;\nint fb(void);\nint fb(void) { return D_1; }\n"


def test_settle_respells_a_declaration_as_its_defining_units_type_and_touches_no_body():
    src = {"a": DEFINER, "b": USER}
    got, done = modmap.settle(["a", "b"], src, ["D_1"])
    assert done == ["D_1"] and got["a"] == DEFINER
    assert got["b"] == "extern unsigned char D_1[8];\nint fb(void);\nint fb(void) { return D_1; }\n"


def test_settle_leaves_a_name_no_unit_defines_to_the_pack():
    src = {"a": "extern int D_9;\n", "b": "extern char D_9;\n"}
    got, done = modmap.settle(["a", "b"], src, ["D_9"])
    assert done == [] and got == src


def test_settle_follows_a_function_definition_and_leaves_ifdef_blocks_alone():
    src = {"a": "int fa(int v) { return v; }\n", "b": "int fa();\n#if X\nint fa(char);\n#endif\n"}
    got, done = modmap.settle(["a", "b"], src, ["fa"])
    assert done == ["fa"] and got["b"] == "int fa(int v);\n#if X\nint fa(char);\n#endif\n"


def _two_unit_tree(tmp_path, other):
    return tree(
        tmp_path,
        {
            "src/g/A1.CPP": DEFINER,
            "src/g/B1.CPP": other,
            "include/g/A1.H": "#ifndef G_A1_H\n#define G_A1_H\nextern int fa(void);\n#endif\n",
            "include/g/B1.H": "#ifndef G_B1_H\n#define G_B1_H\nextern int fb(void);\n#endif\n",
        },
    )


def test_merge_module_reports_the_externs_pack_or_settles_them(tmp_path):
    for mode in ("report", "settle"):
        files = _two_unit_tree(tmp_path / mode, USER)
        src = {u: files.get(u) or "" for u in ("src/g/A1.CPP", "src/g/B1.CPP")}
        pack, notes = [], []
        text, why = modmap.merge_module(
            files, "src/g/MOD.CPP", ["src/g/A1.CPP", "src/g/B1.CPP"], "include/g/MOD.H", {}, src, notes, pack, mode
        )
        assert why == ""
        if mode == "report":
            assert pack[0]["name"] == "D_1" and pack[0]["defined_here"] is True
            assert {d["unit"] for d in pack[0]["declarations"]} == {"src/g/B1.CPP"}
            assert "extern int D_1;" in text
        else:
            assert pack == [] and "extern unsigned char D_1[8];" in text and "extern int D_1;" not in text


HELP_A = "static int help(int v) { return v + 1; }\nint fa(int v) { return help(v); } /* help */\n"
HELP_B = 'static int help(int v) { return v * 2; }\nint fb(int v) { puts("help"); return help(v); }\n'


def test_rename_word_leaves_strings_and_comments_alone():
    text = "int help(void); /* help */ puts(\"help\"); x = help(1); y = 'h'; // help\n"
    assert modmap.rename_word(text, "help", "help_2") == (
        "int help_2(void); /* help */ puts(\"help\"); x = help_2(1); y = 'h'; // help\n"
    )


def test_static_clashes_proposes_a_new_name_only_for_a_different_later_helper():
    same = {"a": HELP_A, "b": HELP_A.replace("fa", "fb")}
    assert modmap.static_clashes(["a", "b"], same) == []
    assert modmap.static_clashes(["a", "b"], {"a": HELP_A, "b": HELP_B}) == [("b", "help", "help_2")]
    assert modmap.static_clashes(["a", "b"], {"a": HELP_A, "b": HELP_B + "int help_2;\n"}) == [("b", "help", "help_3")]


def test_merge_module_refuses_two_different_helpers_with_the_proposal_or_renames_the_later_one(tmp_path):
    for mode in ("report", "rename"):
        files = _two_unit_tree(tmp_path / mode, HELP_B)
        src = {"src/g/A1.CPP": HELP_A, "src/g/B1.CPP": HELP_B}
        notes, renames = [], []
        text, why = modmap.merge_module(
            files,
            "src/g/MOD.CPP",
            ["src/g/A1.CPP", "src/g/B1.CPP"],
            "include/g/MOD.H",
            {},
            src,
            notes,
            [],
            "report",
            mode,
            renames,
        )
        if mode == "report":
            assert "help defined twice" in why and "src/g/B1.CPP help -> help_2" in why
            assert renames == [{"module": "src/g/MOD.CPP", "unit": "src/g/B1.CPP", "name": "help", "to": "help_2"}]
        else:
            assert why == "" and "static int help_2(int v) { return v * 2; }" in text
            assert "return help_2(v);" in text and 'puts("help")' in text and "return help(v);" in text
            assert any("renamed help_2" in n for n in notes)


def test_occupied_refuses_a_target_that_exists_and_stays(tmp_path):
    files = tree(tmp_path, {"src/g/GA_2.CPP": "int fb(void);\n"})
    assert "merge that module first" in modmap.occupied(files, "src/g/GA_2.CPP", set())
    assert modmap.occupied(files, "src/g/GA_2.CPP", {"src/g/GA_2.CPP"}) == ""
    assert modmap.occupied(files, "src/g/NEW.CPP", set()) == ""


CODE_AND_JUNK = (
    "int fa(int v) {\n    int r = v;\n    return r;\n}\n"
    "#if defined(VERSION_US)\nconst unsigned int rodata_unresolved_1[1] = {\n    0x00000000,\n};\n#endif\n"
    "#if defined(VERSION_EU)\nunsigned int live_2[2] = {\n    0x00000000,\n    0x00000001,\n};\n#endif\n"
)


def test_a_drop_reclass_keeps_the_function_of_a_unit_and_strips_only_its_zero_data():
    src = {"a": CODE_AND_JUNK}
    assert modmap.holds_content(CODE_AND_JUNK) and modmap.prepare(src, "drop", "a") == ""
    assert src["a"] == (
        "int fa(int v) {\n    int r = v;\n    return r;\n}\n"
        "#if defined(VERSION_EU)\nunsigned int live_2[2] = {\n    0x00000000,\n    0x00000001,\n};\n#endif\n"
    )


def test_a_drop_reclass_leaves_a_pure_padding_unit_to_the_caller():
    junk = "#if X\nunsigned char pad[3] = {\n    0x00, 0x00, 0x00,\n};\n#endif\n"
    src = {"a": junk}
    assert not modmap.holds_content(junk) and modmap.prepare(src, "drop", "a") == "" and src["a"] == junk


def test_a_drop_reclass_keeps_a_string_unit_and_strips_only_the_zero_padding_beside_it():
    keep = '#if !(defined(VERSION_US_REV1))\nconst char str_1[13] =\n    "[ %s : %d ]\\012\\000";\n#endif\n'
    pad = "#if defined(VERSION_US)\nunsigned char rodata_unresolved_2[3] = {\n    0x00, 0x00, 0x00,\n};\n#endif\n"
    src = {"a": keep + pad}
    assert modmap.holds_content(keep + pad) and modmap.prepare(src, "drop", "a") == ""
    assert src["a"] == keep


def test_a_const_reclass_never_prefixes_a_function_definition():
    src = {"a": "int fa(void) {\n    int r = 1;\n    return r;\n}\nunsigned char t[2] = { 1, 2 };\n"}
    modmap.prepare(src, "const", "a")
    assert src["a"] == "int fa(void) {\n    int r = 1;\n    return r;\n}\nconst unsigned char t[2] = { 1, 2 };\n"


def test_a_module_part_way_merged_merges_the_units_that_are_left_and_notes_the_gone_ones(tmp_path, monkeypatch):
    from types import SimpleNamespace

    tree(
        tmp_path,
        {
            "src/g/A1.CPP": UNIT_A,
            "src/g/B1.CPP": UNIT_B,
            "include/g/A1.H": "#ifndef G_A1_H\n#define G_A1_H\nextern int fa(void);\n#endif\n",
            "include/g/B1.H": "#ifndef G_B1_H\n#define G_B1_H\nextern int fb(void);\n#endif\n",
        },
    )
    head = "unit\tmodule\theader\tstage\tconfidence\tkind\n"
    row = "{}\tsrc/g/MOD.CPP\tinclude/g/MOD.H\tnow\thigh\tcode\n"
    (tmp_path / "map.tsv").write_text(
        head + "".join(row.format(u) for u in ("src/g/A1.CPP", "src/g/GONE.CPP", "src/g/B1.CPP"))
    )
    monkeypatch.setattr(modcheck, "facts", lambda *_: {})
    monkeypatch.setattr(modcheck, "unbuilt", lambda *_: [])
    monkeypatch.setattr(modcheck, "identity", lambda *_: lambda _u: "")
    monkeypatch.setattr(modcheck, "check", lambda *_: ([], [], []))
    monkeypatch.setattr(modcheck, "roms", lambda *_: {})
    config = SimpleNamespace(project=SimpleNamespace(root=str(tmp_path), versions=["us"]))
    got = modmap.run(config, {"map": str(tmp_path / "map.tsv"), "stage": "all"})
    assert got["refused"] == [] and got["modules"] == ["src/g/MOD.CPP"] and got["units"] == 2
    assert got["notes"] == ["src/g/MOD.CPP: units gone from the tree, merged without them: src/g/GONE.CPP"]
    assert "+int fa(void)" in got["patch"] and "+int fb(void)" in got["patch"]


def test_a_const_reclass_names_the_data_it_made_const_and_the_externs_follow(tmp_path):
    from recipemaker.structure import Files

    src = {"a": 'char g_idx[40] = "x";\nstatic int s = 3;\nunsigned short D_t[2] = {1, 2};\n'}
    made: set[str] = set()
    modmap.prepare(src, "const", "a", made)
    assert made == {"g_idx", "D_t"}
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include" / "h.H").write_text(
        "extern char g_idx[40];\nextern const char other[2];\nextern int g_idx2;\n"
    )
    (tmp_path / "src" / "u.c").write_text("extern unsigned short D_t[];\nvoid f(void) { extern char g_idx[40]; }\n")
    files = Files(tmp_path)
    assert modmap.constify_declarations(files, made) == 3
    assert files.get("include/h.H") == "extern const char g_idx[40];\nextern const char other[2];\nextern int g_idx2;\n"
    assert files.get("src/u.c") == "extern const unsigned short D_t[];\nvoid f(void) { extern const char g_idx[40]; }\n"
