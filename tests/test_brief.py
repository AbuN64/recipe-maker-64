"""The brief: its sections, its length cap and what it says about placeholders."""

from dataclasses import replace
from types import SimpleNamespace

from recipemaker import brief, cparse, ledger
from recipemaker.ledger import Left


def packet(fakes, diff=3, text="void f(void) {}\n", errors=()):
    body = fakes._packet(fakes.snapshot, "func_a")
    body["diff"] = "\n".join(f"d{i}" for i in range(diff))
    body["best"]["errors"] = list(errors)
    source = fakes.root / body["best"]["source"]
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text)
    return body


def test_the_brief_has_the_target_path_candidate_diff_types_and_the_next_command(fakes):
    path = brief.write(fakes.snapshot, "func_a", packet(fakes), ["resolved 1 of 2 M2C_FIELD"])
    text = path.read_text()
    assert path == fakes.root / ".unbake/recipe/work/func_a/brief.md"
    for part in (
        "## Target assembly (read only",
        "## Candidate",
        "## First differences",
        "## Fixed for you",
        "## Types",
    ):
        assert part in text
    assert f"file: {fakes.root / 'asm/a/func_a.s'}" in text and "score: a 50.0%" in text
    assert "resolved 1 of 2 M2C_FIELD" in text and text.rstrip().splitlines()[-1] == "next: recipe try func_a"


def test_a_long_diff_is_cut_and_the_whole_brief_stays_under_the_cap(fakes):
    lines = brief.write(fakes.snapshot, "func_a", packet(fakes, diff=500), []).read_text().splitlines()
    assert len(lines) <= brief.MAX_LINES and any(x.startswith("... ") and "more lines" in x for x in lines)


def test_compiler_errors_and_text_no_landing_may_hold_are_named_with_their_lines(fakes):
    body = packet(fakes, text="void f(void) {\n    x = (s32 *)y;\n    z = M2C_ERROR(1);\n}\n", errors=["bad"])
    text = brief.write(fakes.snapshot, "func_a", body, []).read_text()
    assert "## Does not compile\nbad" in text and "## Must change before try" in text
    assert "- line 2: a cast to another type or view: x = (s32 *)y;" in text and "- line 3: M2C_ text" in text


def test_placeholders_name_the_type_the_views_and_whether_another_view_has_the_field(fakes):
    left = [Left("arg0", 0x5C, "Actor", ("Actor", "ActorB"), True), Left("arg1", 0x60, "", (), False)]
    text = brief.write(fakes.snapshot, "func_a", packet(fakes), [], left).read_text()
    assert "arg0 at 0x5C: type Actor, views Actor, ActorB; another view has a field there" in text
    assert "arg1 at 0x60: the candidate declares no type for it" in text


def test_scores_are_floored_to_a_tenth_so_only_an_exact_score_reads_100_percent():
    assert brief.scores({"de": 0.9996, "us": 1.0, "eu": 0.287}) == "de 99.9%, us 100.0%, eu 28.7%"


def test_a_longer_build_says_how_many_bytes_longer_and_lists_what_the_proof_missed(fakes):
    body = packet(fakes)
    body["why"] = ["built is 16 bytes longer than the target", "size 64 != 48"]
    text = brief.write(fakes.snapshot, "func_a", body, []).read_text()
    assert "## Why it is not exact\n- built is 16 bytes longer than the target\n- size 64 != 48\n" in text


def test_the_twin_is_named_with_its_file_and_what_differs_in_its_version(fakes):
    body = packet(fakes)
    body["twin"] = {
        "function": "func_b",
        "file": "src/b.c",
        "version": "us-rev1",
        "macros": [["-DVERSION_US"], ["-DVERSION_US_REV1"]],
        "flags": {"cflags": [["-O2"], ["-O2"]], "cppflags": [["-DX"], []]},
        "diff": "-lw a\n+lw b",
    }
    text = brief.write(fakes.snapshot, "func_a", body, []).read_text()
    assert "## Twin\nfunc_b in src/b.c (us-rev1); read it: recipe look func_b\n" in text
    assert "version macros: this -DVERSION_US, twin -DVERSION_US_REV1" in text
    assert "unit cppflags: this ['-DX'], twin []" in text and "unit cflags" not in text
    assert "(+):\n-lw a\n+lw b" in text


def test_a_refused_landing_prints_its_reason_and_each_distinct_missing_text_once():
    from unbake.contracts import Finding

    found = Finding(
        "land.not_exact",
        "g does not match the ROM in every holding version",
        missing=(
            "version de: compile.error: x.c:3: conflicting types\ny.h:4: previous",
            "version us: compile.error: x.c:3: conflicting types\ny.h:4: previous",
        ),
    )
    assert brief.refusals([found]) == [
        "g does not match the ROM in every holding version",
        "all versions: compile.error: x.c:3: conflicting types | y.h:4: previous",
    ]


def test_a_cut_diff_names_the_file_that_holds_all_of_it(fakes):
    text = brief.write(fakes.snapshot, "func_a", packet(fakes, diff=500), []).read_text()
    full = fakes.root / ".unbake/recipe/work/func_a/diff.txt"
    assert f"full diff: {full}" in text and len(full.read_text().splitlines()) == 500


def test_an_untyped_base_lists_the_types_that_have_a_field_for_every_offset_used_on_it(fakes):
    left = [Left("arg0", 0x14, "", (), False, ("ALMainBus_s",)), Left("arg0", 0x1C, "", (), False, ("ALMainBus_s",))]
    text = brief.write(fakes.snapshot, "func_a", packet(fakes), [], left).read_text()
    assert "no type for it; types with a field for every offset used on it: ALMainBus_s" in text


def test_a_long_run_of_target_lines_is_paired_with_its_built_lines_so_a_cut_keeps_both_sides():
    diff = "--- target\n+++ built\n@@ -1,6 +1,6 @@\n same\n" + "".join(f"-t{i}\n" for i in range(5))
    diff += "".join(f"+b{i}\n" for i in range(5)) + " tail"
    assert brief._paired(diff)[:8] == [
        "--- target",
        "+++ built",
        "@@ -1,6 +1,6 @@",
        " same",
        "-t0",
        "+b0",
        "-t1",
        "+b1",
    ]
    assert brief._paired(diff)[-1] == " tail" and len(brief._paired(diff)) == len(diff.splitlines())


def test_the_cut_diff_in_the_brief_shows_a_built_line_among_its_first_lines(fakes):
    body = packet(fakes)
    body["diff"] = "".join(f"-t{i}\n" for i in range(100)) + "".join(f"+b{i}\n" for i in range(100))
    text = brief.write(fakes.snapshot, "func_a", body, []).read_text()
    assert "-t0\n+b0\n-t1\n+b1" in text


def _with_symbols(fakes, **symbols):
    record = fakes.snapshot.versions["a"]
    versions = {**fakes.snapshot.versions, "a": replace(record, symbols=symbols)}
    return replace(fakes.snapshot, versions=versions)


def test_a_neighbour_view_is_refused_in_the_brief_with_the_symbols_around_the_global(fakes):
    snapshot = _with_symbols(fakes, D_B=0x100, D_A=0xFC, D_C=0x108, D_FAR=0x900)
    body = packet(fakes, text="extern s32 D_B;\nvoid f(void) {\n    s32 *p;\n    p = &D_B;\n    p[2] = 1;\n}\n")
    text = brief.write(snapshot, "func_a", body, []).read_text()
    assert "pointer arithmetic on the address of D_B: p[2] = 1;; neighbours: D_A at -4, D_C at +8" in text
    assert "fix: the neighbouring globals are one object" in text and "D_FAR" not in text


def test_a_placeholder_offset_that_is_a_named_symbol_says_so(fakes):
    snapshot = _with_symbols(fakes, D_B=0x100, D_A=0xFC)
    left = [Left("D_B", -4, "", (), False)]
    text = brief.write(snapshot, "func_a", packet(fakes), [], left).read_text()
    assert "D_B at -4: that offset is the symbol D_A, not a field" in text


def test_an_address_of_placeholder_base_maps_its_offsets_to_the_neighbour_symbols(fakes):
    snapshot = _with_symbols(fakes, D_B=0x100, D_A=0xFC, D_C=0x108)
    left = [Left("&D_B", -4, "", (), False), Left("&D_B", 8, "", (), False)]
    text = brief.write(snapshot, "func_a", packet(fakes), [], left).read_text()
    assert "&D_B at -4: that offset is the symbol D_A, not a field" in text
    assert "&D_B at +8: that offset is the symbol D_C, not a field" in text


def test_a_withheld_member_names_the_versions_its_source_is_fixed_for(fakes):
    from unbake.contracts import UnitSpec

    fakes.with_units({"src/a.c": UnitSpec("src/a.c", "c", "main", ("func_a",), "gcc-test", {}, ("eu", "eu-x"))})
    text = brief.write(fakes.snapshot, "func_a", packet(fakes), []).read_text()
    assert "src/a.c is withheld in eu, eu-x" in text


def test_a_neighbour_refusal_names_the_neighbouring_globals_with_their_distance(fakes, monkeypatch):
    from unbake import types

    rows = {n: {"declaration": f"extern s32 {n};", "size": 4, "section": ".data"} for n in ("D_A", "D_B", "D_C")}
    monkeypatch.setattr(types, "load", lambda snap: {"function": {}, "global": rows, "struct": {}})
    declared = {n: ("include/d.h", i, f"extern s32 {n};") for i, n in enumerate(rows)}
    monkeypatch.setattr(brief.index, "get", lambda snap: SimpleNamespace(declarations=declared))
    snapshot = _with_symbols(fakes, D_B=0x100, D_A=0xFC, D_C=0x108)
    snapshot = replace(snapshot, overlays={"include/d.h": b"extern s32 D_A;\n"})
    body = packet(fakes, text="extern s32 D_B;\nvoid f(void) {\n    s32 *p;\n    p = &D_B;\n    p[2] = 1;\n}\n")
    text = brief.write(snapshot, "func_a", body, []).read_text()
    assert "neighbours: D_A at -4, D_C at +8" in text


def test_the_region_table_shows_when_nothing_built_every_region_not_built(fakes):
    body = packet(fakes, diff=0)
    body["diff"] = ""
    kinds = [{"start": 0, "size": 8, "kind": "not built", "anchors": "calls f", "lines": []}] * 2
    body["regions"] = kinds
    text = brief.write(fakes.snapshot, "func_a", body, []).read_text()
    assert (
        "## Regions in a, the lowest-scoring version, matching 0 of 2" in text
        and "- region 1 at +0x0, 8 bytes: not built (calls f)" in text
    )
    assert "First differing regions" not in text


def test_a_global_used_as_a_pointer_and_a_callee_with_more_arguments_than_declared_are_named(fakes, monkeypatch):
    asm = fakes.root / "asm/a/func_a.s"
    asm.parent.mkdir(parents=True, exist_ok=True)
    asm.write_text(
        "    /* 0 80000400 8DCE28BC */  lw         $t6, %lo(D_1)($t6)\n"
        "    /* 4 80000404 8DC20004 */  lw         $v0, 0x4($t6)\n"
        "    /* 8 80000408 00C02825 */  move       $a1, $a2\n"
        "    /* C 8000040C 0C000100 */  jal        func_x\n    /* 10 80000410 00E03025 */   move      $a2, $a3\n"
    )
    monkeypatch.setattr(
        brief.index, "get", lambda s: SimpleNamespace(declarations={"D_1": ("h.h", 1, "extern s32 D_1;")})
    )
    body = packet(fakes)
    body["types"]["declarations"] = ["void func_x(void *a);"]
    text = brief.write(fakes.snapshot, "func_a", body, []).read_text()
    assert (
        "- D_1 is declared s32 but used as a pointer here: this is a declaration item (recipe next --kind declaration"
        in text
    )
    assert "- func_x is declared with 1 parameters but the call sets $a2" in text
    assert "- the call to func_x passes arg0 through untouched: the function takes 3 arguments" in text


def test_a_data_brief_shows_the_symbol_home_bytes_with_ascii_and_each_reference(fakes, monkeypatch):
    from recipemaker import draft, index

    place = fakes.snapshot.layout.members["data_d"].placements[0]
    entry = fakes.snapshot.layout.members["data_d"]
    monkeypatch.setattr(draft, "data_image", lambda s, m: (entry, "a", (place,), b"Hi!\x00\x00\x00\x00\x01"))
    version = replace(fakes.snapshot.versions["a"], symbols={"data_d": place.vram})
    fakes.snapshot = replace(fakes.snapshot, versions={**fakes.snapshot.versions, "a": version})
    uses = {"data_d": (("func_a", "lbu", 0), ("func_a", "lw", 4), ("func_b", "lh", 2))}
    monkeypatch.setattr(index, "get", lambda s: SimpleNamespace(declarations={}, uses=uses))
    text = "\n".join(brief.data_facts(fakes.snapshot, "data_d"))
    assert "symbol: data_d (a); .data" in text and "definition goes in src/data_d.c" in text
    assert "+0x0000  48692100 00000001" in text and "|Hi!.....|" in text
    assert "- func_a: lbu +0x0 (u8), lw +0x4 (32)" in text and "- func_b: lh +0x2 (s16)" in text


def test_an_entry_in_front_of_the_frame_in_one_version_only_is_named_up_front():
    def asm(extra):
        body = "    /* 0 80000000 27BDFFE0 */  addiu      $sp, $sp, -0x20\n"
        body += "    /* 4 80000004 AFBF001C */  sw         $ra, 0x1c($sp)\n"
        return (extra + body).encode()

    entry = (
        "    /* 0 80000000 00E83821 */  addu       $t0, $a3\n    /* 4 80000004 27A60018 */  addiu      $a2, $sp, 0x18\n"
    )
    files = {"asm/eu/f.s": asm(entry), "asm/de/f.s": asm(""), "asm/us/f.s": asm("")}
    packet = {"target": {v: {"asm": f"asm/{v}/f.s"} for v in ("eu", "de", "us")}}
    snapshot = SimpleNamespace(peek=files.get)
    got = brief.entry_artifacts(snapshot, packet)
    assert got == [
        "eu: extra instructions at entry not present in other versions (addu $t0, $a3; addiu $a2, $sp, 0x18); "
        "land the others with eu withheld"
    ]


def test_the_brief_says_what_landed_code_knows_of_the_addresses_and_arguments(fakes, monkeypatch):
    row = {
        "address": 0x1000,
        "names": ["D_1"],
        "readers": [("func_w", True)],
        "landed_readers": 1,
        "spellings": [("src/a.c", 3, "s32"), ("src/b.c", 4, "f32"), ("src/c.c", 5, "s32")],
        "span": {"start": 0x1000, "size": 0x40, "writer": "func_w", "stores": 5, "fills": 1},
    }
    use = {"addresses": [row], "args": {"a0": {"offsets": [4, 8], "landed_callers": ["func_c"]}}, "calls": []}
    monkeypatch.setattr(brief.index, "usage", lambda snapshot, member: use)
    big = ledger.Identity(
        (cparse.Layout("Big", "h.h", (cparse.Field("a", 4, 4, 1, "i"), cparse.Field("b", 8, 0x18, 1, "i"))),), ()
    )
    big = ledger.Identity(big.views, big.views[0].fields)
    monkeypatch.setattr(brief.index, "get", lambda snapshot: SimpleNamespace(declarations={}, identities=(big,)))
    monkeypatch.setattr(brief.layout, "unit_of", lambda snapshot, name: SimpleNamespace(path="src/w.c"))
    snapshot = replace(fakes.snapshot, versions={"a": SimpleNamespace(symbols={"D_1": 0x1000})})
    got = brief.landed_facts(snapshot, "func_a")
    assert got[0] == "D_1..+0x40 is written together by func_w (src/w.c): 5 stores, 1 fills"
    assert "D_1: no header type; spelled `s32`, `f32`; landed readers: func_w" in got
    assert "D_1 is spelled 2 ways: declaration item (recipe next --kind declaration)" in got
    assert "a0: offsets 0x4, 0x8; landed callers: func_c; types: Big (2 of 2 offsets, 0x20 bytes)" in got
