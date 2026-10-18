"""recipe flags: the lattice, the staged search, and the judgement of a set against the neighbours of its function."""

import json
from pathlib import Path

from recipemaker import flags

DATA = Path(__file__).parent / "data" / "structure.json"
X = ("-x",)


def _doc():
    return json.loads(DATA.read_text(encoding="utf-8"))


def test_the_lattice_is_default_singles_and_pairs_inside_one_family():
    sets = flags.lattice(["-O1", "-O2", "-ffast-math", "-fno-delayed-branch", "-mfp32", "-mfp64"])
    assert sets[0] == () and ("-O1",) in sets and ("-mfp32",) in sets
    assert ("-ffast-math", "-fno-delayed-branch") in sets and ("-mfp32", "-mfp64") in sets
    assert ("-O1", "-O2") not in sets and ("-O2", "-ffast-math") not in sets and len(sets) == 1 + 6 + 2


def test_the_search_tries_pairs_only_with_an_option_that_helped(monkeypatch):
    calls = []

    def score(config, host, rows, clock, tag):
        calls.append((tag, [tuple(r["flags"]) for r in rows]))
        table = {(): -1.0, ("-ffast-math",): 0.333, ("-fno-delayed-branch",): -1.0, ("-mfp32",): -1.0}
        table[("-ffast-math", "-fno-delayed-branch")] = 1.0
        return [
            {
                "ok": table[tuple(r["flags"])] >= 0,
                "result": {"exact": table[tuple(r["flags"])] == 1.0, "score": table[tuple(r["flags"])]},
            }
            for r in rows
        ]

    monkeypatch.setattr(flags, "_score", score)
    sets = flags.lattice(["-ffast-math", "-fno-delayed-branch", "-mfp32"])
    values = flags.search(None, None, [("sqrtf", "src/fuzzy/sqrtf.c")], sets, [])
    assert [t for t, _ in calls] == ["singles", "pairs"]
    assert calls[1][1] == [("-ffast-math", "-fno-delayed-branch")]  # -mfp32 helped nothing: no pair with it
    assert flags.best(values["sqrtf"]) == ("-ffast-math", "-fno-delayed-branch")


def test_best_prefers_default_then_the_smallest_set():
    assert flags.best({(): 1.0, ("-a",): 1.0}) == ()
    assert flags.best({(): 0.5, ("-b", "-c"): 1.0, ("-d",): 1.0}) == ("-d",)
    assert flags.best({(): 0.5}) is None


def test_neighbours_are_the_landed_c_functions_beside_a_run_and_skip_asm():
    got = flags.neighbours(_doc(), ["func_80214310_de"])
    assert got == [
        ("func_80214178_de", "func_80214310_de", "left"),
        ("func_80213ED4_de", "func_80214178_de", "left"),
        ("func_80214624_de", "func_80214310_de", "right"),
        ("func_80214DD4_de", "func_802149C0_de", "right"),  # asm func_802149C0_de is passed over
    ]


def test_a_set_a_neighbour_loses_bytes_under_decides_a_cut_and_splits_the_function():
    judged = flags.judge(_doc(), {"func_80214310_de": X}, {("func_80214178_de", X): 0.5})
    edge = {"left": "func_80214178_de", "right": "func_80214310_de", "by": ["-x"], "start": "flags"}
    assert judged["decided_cuts"] == [edge] and [s["function"] for s in judged["splits"]] == ["func_80214310_de"]
    assert flags.moves(judged) == [
        {"op": "member", "subject": "func_80214310_de", "target": "obj_func_80214310_de"},
        {"op": "options", "subject": "obj_func_80214310_de", "target": '{"add": ["-x"], "omit": []}'},
    ]


def test_a_set_every_landed_member_keeps_becomes_an_options_move():
    judged = flags.judge(_doc(), {"func_80214310_de": X}, {})
    assert judged["options"] == [{"module": "src/WEAPONS/80213ED4_2.CPP", "flags": ["-x"], "for": "func_80214310_de"}]
    assert flags.moves(judged)[0]["op"] == "options"
