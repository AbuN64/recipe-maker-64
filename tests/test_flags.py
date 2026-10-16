"""recipe flags: the lattice, the staged search, and the judgement of a set against the neighbours of its function."""

import json
from pathlib import Path

from recipemaker import flags

DATA = Path(__file__).parent / "data" / "map.json"


def _doc(classes):
    """The structure fixture map with edge classes: `classes` per function, unal (a join) for the rest."""
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    for m in doc["members"]:
        if m["kind"] == "function":
            held = [p["version"] for p in m["placements"] if p["section"] == ".text"]
            m["edge"] = {v: classes.get(m["name"], "unal") for v in held}
    return doc


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


def test_neighbours_stop_at_a_proven_cut_and_skip_asm():
    doc = _doc({"f4": "pad"})
    got = flags.neighbours(doc, ["f2"])
    assert got == [("f1", "f2", "left"), ("f3", "f2", "right")]  # f4 sits past the proven cut f3|f4
    assert flags.neighbours(doc, ["f4"]) == [("f5", "f4", "right"), ("f6", "f5", "right")]


def test_a_set_its_neighbours_lose_bytes_under_is_a_conflict_at_a_join_and_a_cut_at_an_open_edge():
    doc = _doc({"f3": "al0", "f4": "pad"})
    values = {("f1", ("-x",)): 0.5, ("f3", ("-x",)): 0.6}
    judged = flags.judge(doc, {"f2": ("-x",)}, values)
    assert [c["function"] for c in judged["conflicts"]] == ["f2"]  # f1|f2 is unal: one object, two sets
    assert judged["decided_cuts"] == [{"left": "f2", "right": "f3", "by": ["-x"], "start": "flags"}]
    judged = flags.judge(_doc({"f2": "al0", "f3": "al0", "f4": "pad"}), {"f2": ("-x",)}, values)
    assert not judged["conflicts"] and [s["function"] for s in judged["splits"]] == ["f2"]
    assert flags.moves(judged) == [
        {"op": "member", "subject": "f2", "target": "obj_f2"},
        {"op": "options", "subject": "obj_f2", "target": '{"add": ["-x"], "omit": []}'},
    ]


def test_a_set_every_landed_member_keeps_becomes_an_options_move():
    judged = flags.judge(_doc({"f4": "pad"}), {"f2": ("-x",)}, {})
    assert judged["options"] == [{"module": "code_old1", "flags": ["-x"], "for": "f2"}]
    assert flags.moves(judged)[0]["op"] == "options"
