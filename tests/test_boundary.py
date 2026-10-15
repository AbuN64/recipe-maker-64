"""The boundary with unbake: only its public API, only the runtime area, only its process and pool machinery."""

import ast
import re
from importlib import resources
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "recipemaker"
FILES = sorted(SRC.glob("*.py"))
SHAPES = {"contracts"}  # records and protocols, not functions
WRITERS = {"draft.py": 1, "crack.py": 2}  # the moved code's own writes, all under .unbake (scratch and work files)


def api() -> dict[str, set[str]]:
    """Module -> public names from the Public API table of unbake's shipped CONTRACTS.md."""
    text = resources.files("unbake.resources").joinpath("CONTRACTS.md").read_text(encoding="utf-8")
    table: dict[str, set[str]] = {}
    for line in text.splitlines():
        match = re.match(r"\| (\w+) \| (.+) \|$", line)
        if match and "`" in match[2]:
            table[match[1]] = set(re.findall(r"`(\w+)\(", match[2]))
    return table


def unbake_uses(path: Path) -> list[tuple[str, str, int]]:
    """(module, attribute, line) for every `alias.attr` where alias is an unbake module imported by this file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "unbake":
            aliases.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("unbake."):
            found += [(node.module.split(".")[1], a.name, node.lineno) for a in node.names]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            found.append((aliases[node.value.id], node.attr, node.lineno))
    return found


def test_the_api_table_was_read():
    table = api()
    assert "compare" in table and "run" in table["compare"] and len(table) > 20


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_r1_only_names_in_the_public_api_table_are_used(path):
    table = api()
    bad = []
    for module, name, line in unbake_uses(path):
        if module in SHAPES or module not in table:
            continue
        if name.startswith("_") or name not in table[module]:
            bad.append(f"{path.name}:{line} {module}.{name}")
    assert bad == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_r1_no_private_name_is_imported_from_unbake(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    private = [
        f"{path.name}:{n.lineno} {a.name}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("unbake")
        for a in n.names
        if a.name.startswith("_") and a.name != "_view"
    ]
    assert private == []


def test_r3_files_are_written_only_through_the_store_except_the_moved_scratch_writes():
    found = {}
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        count = len(re.findall(r"\.write_text\(|\.write_bytes\(|open\([^)]*[\"']w", text))
        if count:
            found[path.name] = count
    assert found == WRITERS


def test_r3_no_tracked_project_path_is_named_as_a_write_target():
    text = "".join(p.read_text(encoding="utf-8") for p in FILES if p.name != "work.py")
    assert not re.search(r"store\.write\([^)]*[\"'](?:src|include)/", text)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_r4_processes_and_workers_come_only_from_unbake(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {"subprocess", "multiprocessing", "concurrent", "os.system"}
    names = [a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    ]
    allowed = {"tui.py": {"curses", "subprocess"}}.get(path.name, set())
    assert [n for n in names if n in banned and n not in allowed] == []


def test_only_ladder_and_work_import_compare_and_land_for_verdicts():
    users = {
        p.name for p in FILES if any(m in ("compare", "land") for m, _n, _l in unbake_uses(p) if _n not in ("Finding",))
    }
    assert users <= {"ladder.py", "work.py", "crack.py", "draft.py"}  # crack and draft only bind and measure


def test_no_literal_worker_count_in_a_pool_or_slot_call():
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?:pool\.(?:map|gather)|\"-j\"|slots)\([^)]*\b\d+\b", text), path.name


def test_the_fakes_replace_only_names_the_api_table_lists():
    from fakes import SEAMS

    table = api()
    assert [f"{m.__name__}.{n}" for m, n, _ in SEAMS if n not in table[m.__name__.split(".")[-1]]] == []


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
HARD_CAP = 10420  # raw lines of recipe maker Python


def test_line_budget():
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in FILES)
    assert total <= HARD_CAP, f"src/recipemaker/*.py has {total} lines, over the hard cap of {HARD_CAP}"


def test_no_job_or_key_handed_to_the_pool_is_a_lambda():
    import ast

    def pooled(node):
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("map", "gather")
            and getattr(node.func.value, "id", "") == "pool"
        )

    for path in FILES:
        for node in filter(pooled, ast.walk(ast.parse(path.read_text(encoding="utf-8")))):
            assert not [n for n in ast.walk(node) if isinstance(n, ast.Lambda)], f"{path.name}:{node.lineno}"
