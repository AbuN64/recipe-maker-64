"""world: the members, modules and units Recipe Maker reads from unbake's captured tree."""

from pathlib import Path
from types import SimpleNamespace

from recipemaker import world


def module(path, owns, fuzzy=(), stand_ins=None, sections=None, segment="main"):
    return SimpleNamespace(
        path=path,
        segment=segment,
        owns=owns,
        fuzzy=tuple(fuzzy),
        stand_ins=stand_ins or {},
        sections=sections or {},
    )


def snapshot(tmp_path: Path, modules, symbols, files=None):
    for name, text in (files or {}).items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(text)
    project = SimpleNamespace(root=tmp_path, names_from="a", libraries=("src/lib/",), toolchain="gcc-test")
    tree = SimpleNamespace(modules=tuple(modules), symbols=symbols, digest="t")
    peek = lambda p: (tmp_path / p).read_bytes() if (tmp_path / p).is_file() else None  # noqa: E731
    return SimpleNamespace(files=tree, config=SimpleNamespace(project=project), digest="s", peek=peek)


SYMBOLS = {
    "a": {
        "f": (0x80000400, 0x1000, 8, "func"),
        "g": (0x80000408, 0x1008, 8, "func"),
        "d": (0x80001000, 0x2000, 4, "u32"),
    },
    "b": {"f": (0x80000500, 0x1100, 8, "func")},
}


def test_members_carry_placements_per_version_kind_and_whether_they_landed(tmp_path):
    files = {"src/m.c": b'#include "m.h"\n', "src/lib/l.c": b""}
    mods = [
        module(
            "src/m.c", {"a": ("f", "g", "d"), "b": ("f",)}, stand_ins={"a": ("g",)}, sections={"a": {"d": ".rodata"}}
        ),
        module("src/lib/l.c", {}),
    ]
    layout = world._build(snapshot(tmp_path, mods, SYMBOLS, files))
    f, g, d = (layout.members[n] for n in ("f", "g", "d"))
    assert f.kind == "function" and f.landed and f.holders() == ("a", "b") and f.reference("b") == "b"
    assert f.placements[0] == world.Placement("a", ".text", 0x1000, 0x1008, 0x80000400)
    assert not g.landed and d.kind == "data" and d.placements[0].section == ".rodata"
    group = layout.groups["src/m.c"]
    assert group.members == ("f", "g", "d") and group.header == "include/m.h" and not group.sdk
    assert set(layout.units) == {"src/m.c"} and "src/lib/l.c" not in layout.groups  # a module that owns nothing


def test_a_fuzzy_body_is_not_landed_and_names_its_module(tmp_path):
    mods = [module("src/m.c", {"a": ("f",), "b": ("f",)}, fuzzy=("f",))]
    layout = world._build(snapshot(tmp_path, mods, SYMBOLS))
    assert layout.fuzzy == {"f": "src/m.c"} and not layout.members["f"].landed and layout.units == {}


def test_a_bss_placement_has_no_rom_bytes_and_keeps_its_size(tmp_path):
    symbols = {"a": {"b": (0x80002000, 0x3000, 0x40, "u8")}}
    mods = [module("src/m.c", {"a": ("b",)}, sections={"a": {"b": ".bss"}})]
    place = world._build(snapshot(tmp_path, mods, symbols)).members["b"].placements[0]
    assert (place.rom_start, place.rom_end, place.size) == (0x3000, 0x3000, 0x40)


def test_rom_bytes_reads_a_slice_of_the_versions_image(tmp_path):
    rom = tmp_path / "rom.z64"
    rom.write_bytes(bytes(range(16)))
    version = SimpleNamespace(rom=rom, rom_sha256="x")
    assert world.rom_bytes(version, 4, 8) == bytes([4, 5, 6, 7])
