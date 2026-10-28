"""Settle member renames by compiling: the typed pass of step M cannot see an access whose record it cannot
infer or one a macro spells, but the compiler names each (`structure has no member named M`). `settle` compiles
what reads the touched files, fixes every reported use from its source line and reports what no line spells (a
macro expansion)."""

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from unbake.contracts import Config, Json

from recipemaker import access, host, members

_NO_MEMBER = re.compile(
    r"^(\S+\.i):(\d+): (?:error: )?(?:structure|union|[\w ]+?) has no member named [`'\"]?(\w+)[`'\"]?", re.M
)
_ACCESS = r"(->|\.)\s*{}\b"
SETTLE_ROUNDS = 12
BATCH = 40


def _sources(path: str) -> bool:
    return path.lower().endswith((".c", ".cpp", ".cc"))


def objects_for(root: Path, files: Iterable[str], version: str) -> list[str]:
    """The object targets that read FILES: a source's own object, and for a header every object whose `.d` names it."""
    files = sorted(set(files))
    sources = [f for f in files if _sources(f)]
    headers = [f for f in files if not _sources(f)]
    objs = {f"build/{version}/{s}.o" for s in sources}
    if headers:
        for dep in (root / "build" / version).rglob("*.d"):
            text = dep.read_text(encoding="utf-8", errors="replace")
            if any(h in text for h in headers):
                objs.add(dep.relative_to(root).with_suffix(".o").as_posix())
    return sorted(objs)


_ERROR = re.compile(r"^(?:\S+:\d+:(?:\d+:)? )?(?:fatal )?error:? .*$", re.M)
CHECK_LIMIT = 24  # objects one compile check builds: a header reaches thousands, a sample of its readers judges it


def compile_check(
    config: Config,
    files: Iterable[str],
    *,
    run: Callable[[list[str]], tuple[str, str]],
    version: str = "",
    limit: int = CHECK_LIMIT,
) -> Json:
    """Compile what reads FILES (every source itself, at most LIMIT objects in all, sources first) and return
    `{"compiled", "sampled", "errors"}`: the compiler's own error lines. An edit outside the target is judged by this,
    not by the evidence that proposed it."""
    root = config.project.root
    version = version or (config.project.versions[0] if config.project.versions else "")
    objs = objects_for(root, files, version) if version else []
    own = {f"build/{version}/{f}.o" for f in files if _sources(f)}
    chosen = [*sorted(own & set(objs)), *[o for o in objs if o not in own]][:limit]
    errors: list[str] = []
    for i in range(0, len(chosen), BATCH):
        so, se = run(chosen[i : i + BATCH])
        errors += [m.group(0) for m in _ERROR.finditer(so + "\n" + se)]
    return {"compiled": len(chosen), "sampled": len(objs) > len(chosen), "errors": errors[:12]}


def fix_errors(
    root: Path, errors: Iterable[tuple[str, str, str]], rename: Mapping[str, str], headers: Sequence[str]
) -> tuple[int, list[str]]:
    """Fix each `no member named M` the compiler reported: its preprocessed line is found again in the unit's source,
    then in the headers, and the one access of M on it is spelled with the other name of the pair (an old name that was
    missed becomes the new one, a new name a typed rename put on another record's member goes back). A line that no
    source spells is the expansion of a macro: it comes back as `path:line member` for a human."""
    inverse = {new: old for old, new in rename.items()}
    counts: dict[tuple[str, str, str], int] = {}
    for item in errors:
        counts[item] = counts.get(item, 0) + 1
    cache: dict[str, list[str]] = {}
    held: dict[str, list[str]] = {}
    dirty: set[str] = set()
    fixed, left = 0, []
    for (ifile, line, member), times in sorted(counts.items()):
        if ifile not in cache:
            cache[ifile] = (root / ifile).read_text(encoding="utf-8", errors="replace").split("\n")
        i = int(line) - 1
        shown = cache[ifile][i].strip() if i < len(cache[ifile]) else ""
        other = rename.get(member) or inverse.get(member)
        if not other or not shown:
            left.append(f"{ifile}:{line} {member}")
            continue
        unit = ifile.removesuffix(".i").split("/", 2)[-1]
        todo = times
        for path in [unit, *headers]:
            if todo <= 0:
                break
            if path not in held:
                p = root / path
                held[path] = p.read_text(encoding="utf-8", errors="replace").split("\n") if p.is_file() else []
            text = held[path]
            for j, source in enumerate(text):
                if todo <= 0:
                    break
                if source.strip() != shown:
                    continue
                while todo > 0:
                    new = re.sub(_ACCESS.format(re.escape(member)), r"\g<1>" + other, text[j], count=1)
                    if new == text[j]:
                        break
                    text[j], todo, fixed = new, todo - 1, fixed + 1
                    dirty.add(path)
        if todo > 0:
            left.append(f"{ifile}:{line} {member}")
    for path in dirty:
        host.write(root / path, "\n".join(held[path]).encode())
    return fixed, left


def lines(found: Json | None) -> list[str]:
    """The blocking lines of a settle result: each use the compiler named that no source line spells."""
    return [f"unfixed member use (macro expansion? edit its definition): {u}" for u in (found or {}).get("unfixed", [])]


def doubtful(root: Path, rename: Mapping[str, str], ours: Collection[str]) -> set[str]:
    """The files the compiler has to judge: those holding an access of an old name the types cannot place (a macro body
    or an expression with no known record) or still place on the renamed record, and one of a new name on any other
    record. An access placed on another record is certain, so a file with only those is not compiled."""
    texts = members.texts_of(root)
    types = access.Types(texts)
    types.learn(texts)
    found = {s.path for s in types.sites(texts, list(rename)) if s.owner == "" or s.owner in ours}
    return found | {s.path for s in types.sites(texts, list(rename.values())) if s.owner not in ours}


def settle(
    config: Config,
    rename: Mapping[str, str],
    ours: Collection[str],
    *,
    run: Callable[[list[str]], tuple[str, str]],
    version: str = "",
    rounds: int = SETTLE_ROUNDS,
) -> Json:
    """After member renames landed: compile what reads the files `doubtful` names and fix every use the compiler
    reports as `has no member named M` (the uses the typed pass could not see), until the objects build or no error is
    fixable. RUN(argv) -> (stdout, stderr) is the make runner. Returns `{"settled", "compiled", "fixed",
    "unfixed"}`; `unfixed` lists `path:line member` of the errors no source line spells (a macro expansion: edit
    its definition)."""
    root = config.project.root
    version = version or (config.project.versions[0] if config.project.versions else "")
    candidates = doubtful(root, rename, ours)
    headers = sorted(p for p in candidates if not _sources(p))
    objs = objects_for(root, candidates, version) if version else []
    out: dict[str, Any] = {"settled": not objs, "compiled": len(objs), "fixed": 0, "unfixed": []}
    for _ in range(rounds):
        errors: list[tuple[str, str, str]] = []
        for i in range(0, len(objs), BATCH):
            so, se = run(objs[i : i + BATCH])
            errors += _NO_MEMBER.findall(so + "\n" + se)
        if not errors:
            out.update(settled=True, unfixed=[])
            return out
        fixed, left = fix_errors(root, errors, rename, headers)
        out["fixed"] += fixed
        out["unfixed"] = left
        if not fixed:
            break
    out["settled"] = False
    return out
