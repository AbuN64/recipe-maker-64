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


# Soft cap on the size of the Python source; raise it with the change that needs the room.
HARD_CAP = 14000  # raw lines of recipe maker Python (debug catalog; KUSEG code words; split rules; declaration batches)


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
