"""Regions: the target cut into basic-block runs, a diff read per region, and what the brief and try say of them."""

import struct

from unbake import symptoms

from recipemaker import brief, regions

ASM = """
glabel f
    /* 0 80000400 27BDFFE8 */  addiu      $sp, $sp, -0x18
    /* 4 80000404 AFBF0014 */  sw         $ra, 0x14($sp)
    /* 8 80000408 10800003 */  beqz       $a0, .Lauto_80000418
    /* C 8000040C 00000000 */   nop
    /* 10 80000410 0C000100 */  jal        func_80000400_other
    /* 14 80000414 3C018000 */   lui       $at, %hi(D_80001234)
  .Lauto_80000418:
    /* 18 80000418 8FBF0014 */  lw         $ra, 0x14($sp)
    /* 1C 8000041C 03E00008 */  jr         $ra
    /* 20 80000420 27BD0018 */   addiu     $sp, $sp, 0x18
endlabel f
"""


def code(*words):
    return b"".join(struct.pack(">I", w) for w in words)


def test_regions_are_cut_at_labels_and_after_a_branch_with_its_delay_slot_and_carry_what_they_read():
    found = regions.split(ASM)
    assert [(r.start, r.size) for r in found] == [(0, 0x10), (0x10, 0x8), (0x18, 0xC)]
    assert found[1].calls == ("func_80000400_other",) and found[1].reads == ("D_80001234",)
    assert found[1].anchors() == "calls func_80000400_other, reads D_80001234"


def test_one_inserted_instruction_marks_one_region_not_every_later_one():
    target = code(0x27BDFFE8, 0xAFBF0014, 0x10800003, 0, 0x0C000100, 0x3C018000, 0x8FBF0014, 0x03E00008, 0x27BD0018)
    built = code(
        0x27BDFFE8, 0xAFBF0014, 0x10800003, 0, 0x0C000100, 0x3C018000, 0x8FBF0014, 0x3C050001, 0x03E00008, 0x27BD0018
    )
    diff = symptoms.diff(built, target, 0x80000400)
    found = regions.classify(regions.split(ASM), diff, 0x80000400)
    assert [r.kind for r in found] == ["match", "match", "extra"]


def test_kinds_of_difference():
    assert regions.kind(["lw $a, 0($b)", "nop"], ["nop", "lw $a, 0($b)"]) == "order"
    assert regions.kind(["addu $v0, $a0, $a1"], ["addu $v1, $a0, $a1"]) == "register"
    assert regions.kind(["nop"], []) == "missing" and regions.kind([], ["nop"]) == "extra"
    assert regions.kind(["nop"], ["lw $a, 0($b)"]) == "other" and regions.kind([], []) == "match"


def rows(n):
    return [
        {
            "start": 4 * i,
            "size": 4,
            "kind": "other" if i % 2 else "match",
            "anchors": "",
            "lines": [f"-{i}: x", f"+{i}: y"],
        }
        for i in range(n)
    ]


def test_the_brief_is_the_same_size_for_a_thousand_instruction_function():
    small, large = brief.regions(rows(10), "eu"), brief.regions(rows(2000), "eu")
    assert small[0] == "## Regions in eu, the lowest-scoring version, matching 5 of 10"
    assert large[0].endswith("matching 1000 of 2000")
    assert len(large) <= brief._REGIONS + 1 + 2 + brief._DIFF + 2 and any("more differing regions" in x for x in large)


CALLS = """
    /* 0 80000400 3C0E800E */  lui        $t6, %hi(D_800E28BC)
    /* 4 80000404 8DCE28BC */  lw         $t6, %lo(D_800E28BC)($t6)
    /* 8 80000408 8DC20004 */  lw         $v0, 0x4($t6)
    /* C 8000040C 00C02825 */  move       $a1, $a2
    /* 10 80000410 0C000100 */  jal        func_x
    /* 14 80000414 00E03025 */   move      $a2, $a3
"""


def test_the_argument_registers_set_before_a_call_and_the_globals_used_as_pointers_are_read_from_the_asm():
    calls, pointers, through = regions.uses(CALLS)
    assert calls == {"func_x": (1, 2)} and pointers == ["D_800E28BC"] and through == {"func_x": (0,)}


def test_a_jal_the_target_has_and_the_build_lacks_names_its_callee():
    here = regions.Region(0, 8, ("func_x",), (), "missing", ("-80000400: jal 0x100", "-80000404: nop"))
    kept = regions.Region(8, 8, ("func_y",), (), "other", ("-1: jal 0x1", "+1: jal 0x2"))
    assert regions.missing_calls([here, kept]) == ["target calls func_x here; your candidate does not"]


def test_only_a_global_declared_as_a_plain_value_is_called_misdeclared():
    declared = {"D_800E28BC": ("h.h", 1, "extern s32 D_800E28BC;")}
    assert regions.misdeclared(CALLS, declared) == {"D_800E28BC": "s32"}
    assert regions.misdeclared(CALLS, {"D_800E28BC": ("h.h", 1, "extern s32 *D_800E28BC;")}) == {}
