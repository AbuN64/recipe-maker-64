from __future__ import annotations

from recipemaker import cparse as structs

HEADER = """
typedef struct {
    char pad0[0x10];
    f32 x10;
    char pad14[0x20 - 0x14];
    s32 x20;
    s32 sp24;
} Pair;
typedef struct {
    s32 a;
    f32 b;
} Other;
typedef struct {
    s32 a;
    f32 b;
} Twin;
"""


def layouts() -> list[structs.Layout]:
    return structs._layouts({"include/a.h": structs.scan(HEADER)})


def test_layouts_follow_size_expressions_and_leave_out_padding_and_stack_slots() -> None:
    pair = next(layout for layout in layouts() if layout.name == "Pair")
    assert [(f.name, f.offset, f.kind) for f in pair.fields] == [("x10", 0x10, "f"), ("x20", 0x20, "i")]


def test_load_reads_each_file_once_through_the_pool_and_buckets_its_jobs(fakes, monkeypatch):
    files = {"include/a.h": HEADER, "src/b.c": "typedef struct { s32 q; } Q;\n"}
    (fakes.root / "include").mkdir()
    (fakes.root / "src").mkdir()
    for name, text in files.items():
        (fakes.root / name).write_text(text)
    monkeypatch.setattr(
        structs.headers, "sources", lambda snap, place="include", suffix=".h": [n for n in files if n.startswith(place)]
    )
    got = structs.load(fakes.snapshot)
    assert {layout.name for layout in got} == {"Pair", "Other", "Twin", "Q"}
    assert [(n, j) for n, j in fakes.pool_jobs if n == "recipe.cparse"] == [("recipe.cparse", 2)]
    assert [n for n, _ in fakes.pool_jobs] == ["recipe.cparse"]  # the layouts are computed once, not per struct


UNIT = """# 1 "a.c"
#pragma pack(push, 1)
typedef int s32;
typedef struct Cup { s32 a; s32 b; } Cup;
typedef struct Unused { s32 z; } Unused;
extern Cup gCup;
extern s32 gOther;
static s32 helper(s32 x) { return x + gOther; }
s32 other(void) { return gOther; }
s32 target(Cup *c) {
    char *s = "{;";
    return helper(c->a) + gCup.b;
}
s32 later(void) { return 0; }
"""


def test_a_zero_length_pad_and_a_pointer_sizeof_do_not_end_the_struct() -> None:
    pads = "char pad4[0x8 - 0x4 - sizeof(char*)];\n  s32 b;\n  char pad8[0x10 - 0x8 - sizeof(s32)];"
    header = f"struct S {{\n  char pad0[0x4];\n  char *a;\n  {pads}\n  s32 c;\n}};\n"
    (found,) = structs._layouts({"include/s.h": structs.scan(header)})
    assert [(f.name, f.offset) for f in found.fields] == [("a", 4), ("b", 8), ("c", 0x10)]


def test_layouts_keep_each_padding_array_with_its_offset_as_room_to_split() -> None:
    pair = next(layout for layout in layouts() if layout.name == "Pair")
    assert [(f.name, f.offset, f.size * f.count) for f in pair.pads] == [("pad0", 0, 0x10), ("pad14", 0x14, 0xC)]


def test_layouts_keep_record_members_their_types_the_size_and_the_typedef_names_of_a_tag() -> None:
    header = (
        "struct Slot_s {\n    s32 a;\n    struct Target *t;\n    u8 b[2];\n};\ntypedef struct Slot_s Slot;\n"
        "struct Game {\n    u32 flags;\n    Slot slots[3];\n    struct Slot_s last;\n};\n"
    )
    found = {f.name: f for f in structs._layouts({"include/g.h": structs.scan(header)})}
    slot, game = found["struct Slot_s"], found["struct Game"]
    assert (slot.size, slot.aliases) == (0xC, ("Slot",))
    assert [(f.name, f.type) for f in slot.fields] == [("a", "s32"), ("t", "Target *"), ("b", "u8")]
    assert [(f.name, f.offset, f.size, f.count, f.type) for f in game.records] == [
        ("slots", 4, 0xC, 3, "struct Slot_s"),
        ("last", 0x28, 0xC, 1, "struct Slot_s"),
    ]
    assert [f.name for f in game.fields] == ["flags"] and game.size == 0x34


UNIONS = """
union Preview {
    struct { char pad0[0x20]; void *window; s32 item24; };
    struct { char padC8[0xC8]; char workspace; };
};
typedef union Preview Preview;
typedef union { s32 word; f32 real; } Either;
typedef struct State {
    union { s32 views[4]; Either raw; } ring;
    char pad10[0x10];
    s32 enabled;
} State;
"""


def test_a_union_holds_every_member_at_offset_zero_and_an_anonymous_member_is_flattened_into_it() -> None:
    found = {f.name: f for f in structs._layouts({"include/u.h": structs.scan(UNIONS)})}
    preview = found["union Preview"]
    assert preview.union and preview.aliases == ("Preview",) and preview.size == 0xCC and preview.pads == ()
    assert [(f.name, f.offset) for f in preview.fields] == [("window", 0x20), ("item24", 0x24), ("workspace", 0xC8)]
    either = found["Either"]
    assert [(f.name, f.offset, f.kind) for f in either.fields] == [("word", 0, "i"), ("real", 0, "f")]
    assert either.size == 4 and not either.inner


def test_a_named_union_member_is_a_record_of_an_inner_type_and_the_members_after_it_keep_their_offsets() -> None:
    found = {f.name: f for f in structs._layouts({"include/u.h": structs.scan(UNIONS)})}
    state, ring = found["State"], found["State.ring"]
    assert [(f.name, f.offset, f.size, f.type) for f in state.records] == [("ring", 0, 0x10, "State.ring")]
    assert [(f.name, f.offset) for f in state.fields] == [("enabled", 0x20)] and state.size == 0x24
    assert ring.inner and ring.union and [(f.name, f.count) for f in ring.fields] == [("views", 4)]
    assert [(f.name, f.type) for f in ring.records] == [("raw", "Either")]
