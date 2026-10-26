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
        {"o": ".text", "k": "entry", "u": "a", "c": "text", "n": 8, "at": 0, "missing": False},
        {"o": ".text", "k": "entry", "u": "x", "c": "text", "n": 8, "at": 8, "missing": False},
        {"o": ".text", "k": "entry", "u": "b", "c": "text", "n": 8, "at": 16, "missing": False},
        {"o": ".rodata", "k": "entry", "u": "a", "c": "rodata", "n": 4, "at": 0, "missing": False},
        {"o": ".rodata", "k": "entry", "u": "p", "c": "data", "n": 4, "at": 4, "missing": False},
        {"o": ".rodata", "k": "entry", "u": "b", "c": "rodata", "n": 4, "at": 8, "missing": False},
    ]
}


def test_check_fails_a_module_with_a_foreign_entry_between_and_asks_a_reclass_for_padding():
    fails, _soft, found = modcheck.check(FX, [("a", "M"), ("b", "M"), ("p", "M"), ("x", "X")], lambda u: "")
    assert [(f[0], f[2]) for f in fails] == [("M", "text")]
    assert [(r[2], r[4]) for r in found] == [("p", "rodata")]


def test_reclassify_drops_zero_padding_and_constifies_other_data():
    found = [("M", "us", "p", "data", "rodata", ".rodata", 4, 4)]
    zero = {"us": (bytes(16), {".rodata": 0})}
    live = {"us": (b"\0" * 4 + b"\1\1\1\1" + bytes(8), {".rodata": 0})}
    assert modcheck.reclassify(found, zero)[0].action == "drop"
    assert modcheck.reclassify(found, live)[0].action == "const"
    assert modcheck.reclassify([("M", "us", "p", "data", "text", ".text", 4, 4)], zero)[0].action == "text"


def test_reserved_name_hint_spells_the_style_form():
    from recipemaker import naming

    assert naming.reserved_hint("__Cache___RecycleNode").endswith(
        "write Cache_RecycleNode (Module_VerbNoun, one underscore)"
    )
    assert naming.reserved_hint("Cache_RecycleNode") == ""
