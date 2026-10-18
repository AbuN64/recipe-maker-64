"""Declaration items: a name declared more than one way. The fix is one change set (a header and the files that
spell the name differently) that unbake lands as one commit, every touched unit proved exact."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from unbake.contracts import Config, Json, Snapshot

from recipemaker import errors, headers, host, index, ladder, state, twin, world

_BLOCKED = 10  # blocked items named in the brief
_SPELLING = re.compile(r"^[^:]*: (\S+?):(\d+) (.*)$")


def spellings(rows: tuple[str, ...]) -> list[tuple[str, int, str]]:
    """(file, line, text) of each declaration a conflict names."""
    return [(m[1], int(m[2]), m[3]) for row in rows if (m := _SPELLING.match(row))]


def conflicts(config: Config, snapshot: Snapshot) -> list[Json]:
    """Per conflicting name: its spellings, its landed definition, the files that use it, the items waiting on it."""
    waiting: dict[str, list[str]] = {}
    for item, symbols in sorted(state.blocked(config).items()):
        for name in symbols:
            waiting.setdefault(name, []).append(item)
    rows = []
    for f in headers.disagreements(snapshot)[1]:
        found = f.symptoms.get("definition")
        rows.append(
            {
                "name": f.unit,
                "spellings": spellings(f.missing),
                "definition": (found["path"], found["line"], found["signature"]) if found else None,
                "consumers": f.symptoms.get("consumers", 0),
                "items": waiting.get(f.unit, []),
            }
        )
    return sorted(rows, key=lambda row: (-len(row["items"]), -row["consumers"], row["name"]))


def folder(config: Config, name: str) -> Path:
    return config.project.root / ".unbake/recipe/work" / f"{host.stem(name)}.decl"


def is_item(config: Config, name: str) -> bool:
    return folder(config, name).is_dir()


def _evidence(snapshot: Snapshot, name: str) -> list[str]:
    unit = world.unit_of(snapshot, name)
    source = index.source(snapshot, unit.path) if unit else ""
    head = twin.function(source, name) if source else None
    out = [f"defined in {unit.path}: {head.split('{')[0].strip()}"] if head and unit else []
    return out or ["no landed definition: choose the type from how the callers use it"]


def _headers(snapshot: Snapshot, row: Json) -> tuple[list[str], str]:
    """The headers the single declaration may go in, and the one that gets it: the header that declares the name
    today, else the first header the defining unit includes."""
    spelled = sorted({p for p, _, _ in row["spellings"]})
    known = set(headers.sources(snapshot))
    unit = world.unit_of(snapshot, row["name"])
    texts = [index.source(snapshot, p) for p in ([unit.path] if unit else spelled) if p.endswith(".c")]
    names = (n for t in texts for n in re.findall(r'^#include "([^"]+)"', t, re.M))
    included = [p for p in dict.fromkeys(f"include/{n}" for n in names) if p in known]
    declaring = [p for p in spelled if p.endswith(".h")]
    return sorted({*declaring, *included}), next(iter([*declaring, *included]), "")


def _brief(snapshot: Snapshot, row: Json, where: Path) -> str:
    files = sorted({path for path, _, _ in row["spellings"]})
    lines = [f"# {row['name']} (declaration)", "", f"declared {len(row['spellings'])} ways in {len(files)} files", ""]
    if row.get("definition"):
        path, number, text = row["definition"]
        lines += [
            f"canonical: the landed definition {path}:{number} `{text}`; declare exactly that, once",
            "The copies already spell it so: check them, then run try.",
            "",
        ]
    lines += ["## Spellings", *[f"- {p}:{n}  {t}" for p, n, t in row["spellings"]], "", "## Evidence"]
    lines += [f"- {line}" for line in _evidence(snapshot, row["name"])]
    items = row["items"]
    lines += ["", f"## Blocks {len(items)} items", *[f"- {i}" for i in items[:_BLOCKED]]]
    lines += [f"... {len(items) - _BLOCKED} more"] * (len(items) > _BLOCKED)
    candidates, chosen = _headers(snapshot, row)
    lines += ["", "## Headers", *([f"- {p}" for p in candidates] or ["- none: create one and include it"])]
    target = f"Put it in {chosen}." if chosen else "No candidate header: create include/<name>.h and include it."
    lines += [
        "",
        "## Do",
        f"Edit the copies under {where} (same paths as the project). Declare the name once, with one",
        f"type, in a header. {target} Delete every other declaration; keep everything else as it is.",
        "",
    ]
    lines += [f"next: recipe try {row['name']}"]
    return "\n".join(lines) + "\n"


def next_item(config: Config, snapshot: Snapshot, now: float) -> Json:
    taken = {lease.item for lease in state.leases(config, now)}
    hidden = state.skipped(config, now) | taken
    rows = [r for r in conflicts(config, snapshot) if r["name"] not in hidden]
    if not rows:
        return {"item": "", "why": "no declaration conflict is open", "next": "recipe status"}
    return take(config, snapshot, rows[0], now)


def _canonical(path: str, text: bytes, row: Json) -> bytes:
    """The copy of `path` with every spelling that differs from the landed definition rewritten to it."""
    if not row.get("definition"):
        return text
    lines = text.decode(errors="replace").splitlines(keepends=True)
    want = row["definition"][2].strip().rstrip(";") + ";"
    for file, number, spelled in row["spellings"]:
        if file == path and (file, number, spelled) != row["definition"] and 0 < number <= len(lines):
            lines[number - 1] = lines[number - 1].replace(spelled, "extern " * spelled.startswith("extern") + want, 1)
    return "".join(lines).encode()


def take(config: Config, snapshot: Snapshot, row: Json, now: float) -> Json:
    """Lease the declaration item `row` names and write its work folder."""
    where = folder(config, row["name"])
    for path in sorted({p for p, _, _ in row["spellings"]} | set(_headers(snapshot, row)[0])):
        host.write(where / path, _canonical(path, snapshot.peek(path) or b"", row))
    host.write(where / "brief.md", _brief(snapshot, row, where).encode())
    lease = state.take(config, row["name"], now)
    files = len({p for p, _, _ in row["spellings"]})
    return {
        "item": row["name"],
        "kind": "declaration",
        "size": 0,
        "regions": 0,
        "why": f"declared {len(row['spellings'])} ways in {files} files; blocks {len(row['items'])} items",
        "aims": [],
        "evidence": "",
        "brief": str(where / "brief.md"),
        "lease_until": state.stamp(lease.expires),
        "idle_minutes": state.settings().idle_minutes,
        "stall_tries": state.settings().stall_tries,
        "next": f"read the brief, edit the copies, then run: recipe try {row['name']}",
    }


def _edits(config: Config, snapshot: Snapshot, name: str) -> dict[str, bytes]:
    where = folder(config, name)
    found = {p.relative_to(where).as_posix(): p.read_bytes() for p in sorted(where.rglob("*")) if p.is_file()}
    found.pop("brief.md", None)
    return {path: text for path, text in found.items() if text != snapshot.peek(path)}


def _primary(snapshot: Snapshot, edits: dict[str, bytes]) -> str:
    """The landed unit the change set is submitted under: an edited one, else one that includes an edited header."""
    found = ladder.primary(snapshot, edits)
    if found is None:
        raise state.stop(
            "recipe.no-unit", "no landed unit includes the edited files", action="recipe skip NAME --reason other"
        )
    return found


def _changed(old: bytes | None, new: bytes) -> set[int]:
    """The 1-based numbers of the lines of `new` that `old` does not have."""
    before, after = (old or b"").decode(errors="replace").splitlines(), new.decode(errors="replace").splitlines()
    ops = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    return {n for tag, _, _, j1, j2 in ops if tag != "equal" for n in range(j1 + 1, j2 + 1)}


_STATEMENT = re.compile(r"^[^\n#{};]*;[ \t]*$", re.M)


def _normal(line: str) -> str:
    return " ".join(line.split()).removeprefix("extern ")


def _drop_repeats(snapshot: Snapshot, edits: dict[str, bytes]) -> tuple[dict[str, bytes], list[str]]:
    """Every edited source without the file-local declarations repeating word for word one in an included header."""
    out, notes = dict(edits), []
    for path, data in edits.items():
        text = data.decode(errors="replace")
        if not path.endswith(".c"):
            continue
        heads = (
            out.get(h := f"include/{n}") or snapshot.peek(h) or b""
            for n in re.findall(r'^#include "([^"]+)"', text, re.M)
        )
        given = {_normal(m[0]) for h in heads for m in _STATEMENT.finditer(h.decode(errors="replace"))}
        kept = [x for x in text.splitlines(keepends=True) if not (_STATEMENT.match(x) and _normal(x) in given)]
        if (dropped := len(text.splitlines(keepends=True)) - len(kept)) > 0:
            out[path] = "".join(kept).encode()
            notes.append(f"dropped {dropped} repeated externs from {path}")
    return out, notes


def _score(config: Config, name: str) -> float:
    return max(
        (state.worst(r["score"]) for r in host.rows(config, "recipe/tries") if r["item"] == name),
        default=0.0,
    )


def run(config: Config, name: str, now: float) -> Json:
    """Prove the edited copies as one change set and land it when every touched unit is exact."""
    snapshot = world.capture(config)
    edits = _edits(config, snapshot, name)
    if not edits:
        raise state.stop(
            "recipe.no-change",
            f"the copies of {name} are unchanged",
            unit=name,
            action=f"edit the copies, then run: recipe try {name}",
        )
    declared = index.get(snapshot).declarations
    for path, text in edits.items():
        touched = _changed(snapshot.peek(path), text)
        hits = [h for h in errors.banned(text.decode(errors="replace"), declared) if h[3] in touched]
        if hits and path.endswith(".c"):
            reason, action = f"{path} holds {hits[0][1]} on line {hits[0][3]}", f"fix it, then run: recipe try {name}"
            raise state.stop(hits[0][0], reason, unit=name, action=action)
    edits, fixed = _drop_repeats(snapshot, edits)
    unit = world.of(snapshot).units[_primary(snapshot, edits)]
    proofs, drain = ladder.land_set(config, snapshot, unit, edits)
    score = {v: round(min(p.score for p in proofs if p.version == v), 4) for v in sorted({p.version for p in proofs})}
    landed = ladder.commits(drain).get("publish", "")
    refused = [f for body in drain.get("refused", []) for f in body["findings"]]
    progressed = state.worst(score) > _score(config, name)
    lease = state.renew(config, name, now) if refused else state.count_try(config, name, now, progressed)
    word = "exact" if landed else "refused" if refused else "fuzzy" if any(score.values()) else "failed"
    why = [x for f in refused for x in (f["reason"], *f["missing"])] or [m for p in proofs for m in p.missing][:3]
    state.record(config, name, now, word, score)
    if landed:
        state.release(config, name)
    rules = state.settings()
    return {
        "item": name,
        "result": word,
        "compiled": not any("compile.error" in m for p in proofs for m in p.missing),
        "score": score,
        "stalled": lease.tries,
        "stall_tries": rules.stall_tries,
        "minutes_left": state.minutes_left(lease, now),
        "auto_fixes": fixed,
        "landed": landed,
        "saved": "",
        "alike": [],
        "regions": {"matched": 0, "total": 0, "gained": None},
        "file": str(folder(config, name)),
        "why": why,
        "compiler": [m.partition(": ")[2] for p in proofs for m in p.missing if "compile.error" in m][:3],
        "brief": str(folder(config, name) / "brief.md"),
        "next": "recipe next" if landed else f"edit the copies, then run: recipe try {name}",
    }
