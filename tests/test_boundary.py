"""The boundary with unbake: only `unbake.api` and the records, only the runtime area, one process starter, and the
one module that runs unbake's commands."""

import ast
import re
from pathlib import Path

import pytest
from unbake import api as unbake_api

SRC = Path(__file__).resolve().parent.parent / "src" / "recipemaker"
FILES = sorted(SRC.glob("*.py"))
WRITERS = {"draft.py": 1, "host.py": 1, "structure.py": 1}  # the candidate file, the atomic write, the saved patch


def api_uses(path: Path) -> list[tuple[str, int]]:
    """(attribute, line) for every `api.attr` of a file that imports `unbake.api`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {
        a.asname or a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "unbake"
        for a in n.names
        if a.name == "api"
    }
    return [
        (n.attr, n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in aliases
    ]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_r1_only_names_unbake_api_exports_are_used(path):
    exported = set(unbake_api.__all__)
    assert [f"{path.name}:{line} api.{name}" for name, line in api_uses(path) if name not in exported] == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_r1_no_private_name_is_imported_from_unbake(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    private = [
        f"{path.name}:{n.lineno} {a.name}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("unbake")
        for a in n.names
        if a.name.startswith("_")
    ]
    assert private == []


def test_r3_files_are_written_only_through_host_write_except_the_named_writes():
    found = {}
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        count = len(re.findall(r"\.write_text\(|\.write_bytes\(|open\([^)]*[\"']w", text))
        if count:
            found[path.name] = count
    assert found == WRITERS


def test_r3_no_tracked_project_path_is_named_as_a_write_target():
    text = "".join(p.read_text(encoding="utf-8") for p in FILES)
    assert not re.search(r"host\.write\([^)]*[\"'](?:src|include)/", text)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_r4_processes_start_only_in_host(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {"subprocess", "multiprocessing", "concurrent", "os.system"}
    names = [a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    ]
    allowed = {"tui.py": {"curses", "subprocess"}, "host.py": {"subprocess"}}.get(path.name, set())
    assert [n for n in names if n in banned and n not in allowed] == []


def test_only_judge_and_structure_run_unbake_commands():
    users = {p.name for p in FILES if re.search(r'"-m", "unbake"|with_name\("unbake"\)', p.read_text(encoding="utf-8"))}
    assert users <= {"judge.py", "structure.py"}


def test_no_literal_worker_count_in_a_pool_or_slot_call():
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?:pool_map|\"-j\"|slots)\([^)]*\b\d+\b", text), path.name


def test_the_fakes_replace_recipemaker_seams_or_api_exports():
    from fakes import SEAMS

    bad = [
        f"{m.__name__}.{n}"
        for m, n, _ in SEAMS
        if not m.__name__.startswith("recipemaker.") and not (m is unbake_api and n in unbake_api.__all__)
    ]
    assert bad == []


@pytest.mark.parametrize(
    "pattern",
    [
        r"^\s*(?:import|from) (?:threading|concurrent|contextvars)\b",
        r"^\s*(?:import|from) hashlib\b",
        r"\b_CODE\b",
        r"^\s*(?:import|from) abucache\b",
        r"^\s*from functools import .*\bcache\b|^\s*@(?:functools\.)?cache\b",
    ],
)
def test_recipemaker_has_no_threads_hashing_code_tags_process_caches_or_abucache(pattern):
    found = [p.name for p in FILES if re.search(pattern, p.read_text(encoding="utf-8"), re.M)]
    assert found == []


# Raised from 5200 by the owner's soft-cap rule: structure, GBI recovery, jump tables and permuter gating landed since,
# and after idiom64 (-235) no other dead code was found. Raised to 6300 for naming.py and style.py (RWSTRAT-5): the two
# verbs are new surface (evidence packs, name validation, banners, style census) with their rules held in style.toml.
# Raised to 6900 for tree.py (RWSTRAT-4): library regions, call-graph communities, data ownership, folder paths,
# and apply that retries a busy lock and defers moves on dirty files. Raised to 7100 for the draft probe in next, the
# persisted memos and lifecycle.py (a try ends with its caller and holds a slot per permuter worker).
# Raised to 7800 for idiom64.py (B3c: pair locals, differences, u64 relations, float casts; ~690 lines), on trial:
# it stays only if it turns at least 10 drafts into compiling ones (RESULT counts).
# Raised to 8050 for clash.py (CUT-R): units that define one type differently stay apart as files of one subsystem,
# plus `apply --all` and the chain-free header moves of the RW cutover.
# Raised to 8250 for repair.py (RW-P1-TOOLR): the text repairs of m2c drafts (stack locals, small-number field
# offsets, dereferenced scalars) and the jump table words of overlays, so the no-C set drafts into code that builds.
# Raised to 8500 for records.py (TOOL-T-NOCRATE): m2c's re-printed project structs fold into their header (9 of 19
# no-C drafts failed on a redefinition), macro-spelled externs and struct-word reads, on trial: it stays only if
# the no-C drafts that compile rise (RESULT counts, 0 of 19 before).
# Raised to 8800 for rungs.split/retype_from_callers/classify and repair.unfake/unknown (TOOL-T-NOCRATE2): exact
# drafts held by M2C_FIELD, M2C_UNK and per-function callee records, on trial: it stays only if lanes land more
# exact and fuzzy no-C functions with them (RESULT counts).
# Raised to 9150 for the M2C_FIELD resolver (TOOL-T-M2CFIELD): bases typed by the globals the draft sees, symbols
# inside record globals, members of nested records and arrays, nested M2C_FIELD and void locals typed by the member
# stored in them, on trial: it stays only if RW drafts resolve more placeholders (RESULT counts). Then to 9260 for
# parameter evidence from callers still in assembly (what reaches a0-a3 at the call) and from typed callees, and
# to 9360 for function tables (sibling entries in a column, the indirect calls of their readers) and a resolve rung.
# Raised to 9580 for TOOL-T-DRAFTCC: an m2c context that agrees with the headers a draft includes (no jump table
# declarations), m2c's argument notes, folds into header bodies with nested records, and unions in the type reader,
# on trial: it stays only if more RW no-C drafts compile (RESULT counts, 0 of 20 before).
# Raised to 10060 for dispatch.py (TOOL-T-DISPATCHREC): one shared record per dispatch group (table columns, objects
# passed on), built from every group function's accesses, on trial: it stays only if held exact drafts land with it.
# Raised to 10250 for one record per global object or pointer global, and pointee records one hop from a dispatch group
# record (TOOL-T-DISPATCHREC), on trial: it stays only if held exact drafts land with them.
# Raised to 10400 for owner-local records (TOOL-RECIPE-OWNREC, owner 2026-10-15): a data symbol with no type evidence
# gets one record named from it in its owner unit's header, shared by every user (locals it feeds too), extended when
# another user shows more members, on trial: it stays only if held no-C drafts land with it (RESULT counts).
# Then to 10420 for later elements of a record pointer (p[1].f) when an offset passes the record's end.
# Then to 10440 for every display-list run of a function (m2c numbered pointer copies, a run that does not decode
# left as it was) and GBI macros on the words the resolver names.
# Raised to 10840 for perfect-decomp (owner 2026-10-15): structure on the map's object-edge verdicts and
# evidence rows, and flags.py (one flag set per object: the lattice through one score --variants, options, splits).
# Then to 10920 for the project naming style (config.toml [naming]): the folders and file names recipe assigns.
# Raised to 11700 for the cutover to unbake's public api (2026-10-17): world.py (the tree as members and modules),
# judge.py (verdicts through unbake's commands), headers.py and cpp.py (the header reads unbake no longer offers)
# replace what Recipe Maker imported from unbake's internals; the permuter, partial landings, cross-version twins and
# types.toml reads left with the old layout.
HARD_CAP = 11700  # raw lines of recipe maker Python


def test_line_budget():
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in FILES)
    assert total <= HARD_CAP, f"src/recipemaker/*.py has {total} lines, over the hard cap of {HARD_CAP}"


def test_no_job_or_key_handed_to_the_pool_is_a_lambda():
    import ast

    def pooled(node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pool_map"
            and getattr(node.func.value, "id", "") == "api"
        )

    for path in FILES:
        for node in filter(pooled, ast.walk(ast.parse(path.read_text(encoding="utf-8")))):
            assert not [n for n in ast.walk(node) if isinstance(n, ast.Lambda)], f"{path.name}:{node.lineno}"
