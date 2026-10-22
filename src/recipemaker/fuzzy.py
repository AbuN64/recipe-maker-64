"""`recipe fuzzy`: m2c drafts of every function with no C, landed as NON_MATCHING bodies without the ship queue.

A body behind `#ifdef NON_MATCHING ... #else INCLUDE_ASM #endif` never changes the ROM (the build does not define
NON_MATCHING), so its only proof is that the module compiles with -DNON_MATCHING in every version that holds the
function. The land path therefore takes no ship lock and builds no ROM: per-file lock, compile gate per holding version,
source rules, one commit per file, a fast-forward push (a rebase and one retry when the branch moved). Headers and types
never ride this path; a draft that needs them stays in the failed list for the fill lanes."""

import fcntl
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal, Snapshot

from recipemaker import draft, host, judge, world

_C_SUFFIXES = (".c", ".cpp")
_STAND_IN = r'^[ \t]*INCLUDE_ASM\(\s*"[^"]+"\s*,\s*{name}\s*\)\s*;?[ \t]*$'
_DEBT = re.compile(r"\bM2C_\w+|\bunk_\w+|\bvar_\w+|\btemp_\w+|\barg\d+\b|\(\w+\s*\*\)\s*\(?\(?u8\s*\*\)|\(u8\s*\*\)")
_TYPE_DEF = re.compile(
    r"^\s*typedef\s+(?:struct|union|enum)\b[^;{]*\{.*?\}\s*(\w+)\s*;|^\s*(?:struct|union|enum)\s+(\w+)\s*\{",
    re.M | re.S,
)
_REMOTE, _BRANCH = "github", "main"
_AUTHOR = ("mo", "itsmoalawi@gmail.com")


def targets(snapshot: Snapshot, versions: Sequence[str] = (), only: Sequence[str] = ()) -> list[str]:
    """The functions no file defines as C (no body, no NON_MATCHING body), in source order of their modules."""
    layout = world.of(snapshot)
    out = []
    for name, member in layout.members.items():
        if member.kind != "function" or member.landed or name in layout.fuzzy:
            continue
        if not member.group.lower().endswith(_C_SUFFIXES):
            continue
        if versions and not set(versions) & set(member.holders()):
            continue
        if only and name not in only:
            continue
        out.append(name)
    return out


def stand_in(text: str, name: str) -> re.Match[str] | None:
    """The INCLUDE_ASM line standing in for `name`, None when the module has none (or it is already fenced)."""
    found = re.search(_STAND_IN.format(name=re.escape(name)), text, re.M)
    if found is None:
        return None
    before = text[: found.start()].rstrip().rsplit("\n", 1)[-1].strip()
    fenced = before.startswith("#else") or (before.startswith("#if") and "NON_MATCHING" in before)
    return None if fenced else found


def fence(text: str, name: str, body: str) -> str | None:
    """`text` with the stand-in of `name` replaced by `#ifdef NON_MATCHING <body> #else <stand-in> #endif`."""
    found = stand_in(text, name)
    if found is None:
        return None
    line = found.group(0).strip()
    if "M2C_" in body and "typedef int M2C_UNK;" not in text[: found.start()]:
        body = draft.with_macros(body)  # m2c's own macro names, defined once per module before the first use
    return text[: found.start()] + f"#ifdef NON_MATCHING\n{body.strip()}\n#else\n{line}\n#endif" + text[found.end() :]


def debt(body: str) -> int:
    """The raw m2c leftovers in a body: counted as fuzzy debt, never a refusal inside NON_MATCHING."""
    return len(_DEBT.findall(body))


def copies(snapshot: Snapshot, body: str) -> str:
    """The first type the body defines that a project header already defines ("" when none): a local typedef copy."""
    headers = "\n".join(draft.header_texts(snapshot).values())
    for found in _TYPE_DEF.finditer(body):
        name = found.group(1) or found.group(2)
        if re.search(rf"\}}\s*{re.escape(name)}\s*;|\b(?:struct|union|enum)\s+{re.escape(name)}\s*\{{", headers):
            return name
    return ""


# ------------------------------------------------------------------------------------------------ drafting
_CONFLICT = re.compile(r"(include/[\w/.+-]+\.H):\d+: conflicting types for")
_CLASH = re.compile(r"conflicting types for `(\w+)'")
_UNDECLARED = re.compile(r"`(\w+)' undeclared \(first use")
_ROUNDS = 4


def undeclare(text: str, name: str) -> str:
    """`text` without its own top-level declaration lines of `name` (prototype, extern or tentative global): the
    module and its headers already declare it, and their type is the real one."""
    gap = r"[\w \t\*?]*?[\s\*]"
    shape = rf"^(?:extern[ \t]+)?{gap}{re.escape(name)}[ \t]*(?:\[[^\]\n]*\])?[ \t]*(?:\([^;{{}}\n]*\))?[ \t]*;[ \t]*\n"
    return re.sub(shape, "", text, flags=re.M)


def amend(text: str, why: str, member: str = "") -> str:
    """The draft with what its compile error blames removed or declared: a header whose declarations clash with the
    module's own is dropped from the draft (the module keeps its declarations), a global m2c read without any
    declaration gets `extern s32 NAME;`. The same text back when nothing in the error is fixable this way."""
    out = text
    for name in dict.fromkeys(_CLASH.findall(why)):  # a clash with a declaration the module has: its type wins
        if name != member:
            out = undeclare(out, name)
    if out != text:
        return out
    for header in dict.fromkeys(_CONFLICT.findall(why)):
        out = re.sub(rf'^[ \t]*#include "{re.escape(header.removeprefix("include/"))}"[ \t]*\n', "", out, flags=re.M)
    wanted = [
        n for n in dict.fromkeys(_UNDECLARED.findall(why)) if not re.search(rf"\b(?:extern|static)\b[^;(]*\b{n}\b", out)
    ]
    if wanted:
        lines = out.split("\n")
        at = max([i + 1 for i, line in enumerate(lines) if line.startswith("#include")] or [0])
        lines[at:at] = [f"extern s32 {n};" for n in wanted]
        out = "\n".join(lines)
    return out


def _repaired(snapshot: Snapshot, member: str, out: Path, why: str) -> str:
    """Re-screen the draft after `amend` until it compiles or the error stops changing; "" when it compiles."""
    for _ in range(_ROUNDS):
        text = out.read_text(encoding="utf-8")
        fixed = amend(text, why, member)
        if fixed == text:
            return why
        host.write(out, fixed.encode())
        why = str(
            draft._checked(
                snapshot, member, [{"text": fixed}], *([p] if (p := draft.standing_version(snapshot, member)) else [])
            )[0]["reason"]
        )
        if not why:
            return ""
    return why


def _draft_job(job: tuple[Snapshot, str]) -> Json:
    snapshot, member = job
    out = snapshot.config.project.root / ".unbake/recipe/fuzzy" / f"{host.stem(member)}.c"
    try:
        why, extras = draft.drafted(snapshot, member, out)
        if why and out.is_file() and ("conflicting types" in why or "undeclared" in why):
            why = _repaired(snapshot, member, out, why)
    except Refusal as refusal:
        return {"member": member, "why": refusal.findings[0].reason, "text": ""}
    except Exception as exc:  # one function never stops the sweep; it is a failure with its reason
        return {"member": member, "why": f"internal {type(exc).__name__}: {exc}", "text": ""}
    if not why and extras:
        why = "needs header edits: " + ", ".join(sorted(extras))
    return {"member": member, "why": why, "text": "" if why else out.read_text(encoding="utf-8")}


def drafts(snapshot: Snapshot, names: Sequence[str]) -> list[Json]:
    """One row per function: {member, why, text}; why is "" for a draft that compiled in the reference version."""
    return list(api.pool_map(snapshot.config, "fuzzy.draft", _draft_job, [(snapshot, n) for n in names]))


# ------------------------------------------------------------------------------------------------ gating
def _score_job(job: tuple[Config, str, tuple[str, ...]]) -> Json:
    """The score of a NON_MATCHING body in every version that holds it, as the tree builds it (-DNON_MATCHING)."""
    config, name, held = job
    try:
        found = dict(api.score(config, name))
    except Exception as exc:  # a module the parser or compiler cannot take is a failure, never a crash
        return {"member": name, "why": f"scoring raised {type(exc).__name__}: {exc}"[:300], "score": 0.0}
    gone = [v for v in held if found.get(v, 0.0) <= 0.0]
    if gone:
        return {
            "member": name,
            "why": f"no compile (or score 0) with -DNON_MATCHING in {', '.join(gone)}",
            "score": 0.0,
        }
    return {"member": name, "why": "", "score": min(found[v] for v in held)}


def gate(config: Config, held: dict[str, tuple[str, ...]]) -> dict[str, Json]:
    """Every function's verdict from the tree as written: {member: {why, score}}; why "" = it builds in every holder."""
    rows = api.pool_map(config, "fuzzy.score", _score_job, [(config, n, h) for n, h in held.items()])
    return {r["member"]: r for r in rows}


# ------------------------------------------------------------------------------------------------ landing
def _try(root: Path, *args: str) -> tuple[int, str]:
    who = ["-c", f"user.name={_AUTHOR[0]}", "-c", f"user.email={_AUTHOR[1]}", "-c", "core.fsmonitor=false"]
    config = SimpleNamespace(project=SimpleNamespace(root=root))
    done = host.run(["git", *who, *args], root, config)  # type: ignore[arg-type]
    return (1 if done.exit is None else done.exit), (done.stdout + done.stderr).decode("utf-8", errors="replace")


def _git(root: Path, *args: str) -> str:
    code, out = _try(root, *args)
    if code:
        raise Refusal(Finding("fuzzy.git", f"git {' '.join(args[:3])}: {out.strip()[-300:]}"))
    return out


@contextmanager
def file_lock(root: Path, path: str) -> Iterator[None]:
    """One writer per source file across every fuzzy run of this repository (not the ship lock)."""
    common = Path(_git(root, "rev-parse", "--git-common-dir").strip())
    folder = (common if common.is_absolute() else root / common) / "fuzzy-locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / path.replace("/", "%")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def private(root: Path) -> bool:
    """A linked worktree: fuzzy moves its HEAD, so it never runs in the tree the ship queue owns."""
    return (root / ".git").is_file()


def sync(root: Path, remote: str, branch: str) -> str:
    """Move the clean private tree to the branch tip; the sha."""
    _git(root, "fetch", "-q", remote, branch)
    sha = _git(root, "rev-parse", f"{remote}/{branch}").strip()
    if _git(root, "rev-parse", "HEAD").strip() != sha:
        _git(root, "checkout", "-q", "--detach", sha)
    return sha


def land_file(root: Path, remote: str, branch: str, path: str, base: str, text: str, names: Sequence[str]) -> Json:
    """Write one module, commit it alone, push fast-forward (a rebase onto the moved branch, three tries)."""
    with file_lock(root, path):
        sync(root, remote, branch)
        file = root / path
        if file.read_text(encoding="utf-8") != base:
            return {"landed": False, "why": "module changed on the branch since it was drafted"}
        host.write(file, text.encode())
        stem = Path(path).name
        subject = f"Fuzzy m2c bodies for {len(names)} functions in {stem}"[:72]
        if len(names) == 1:
            subject = f"Fuzzy m2c body for {names[0]} in {stem}"[:72]
        _git(root, "add", "--", path)
        _git(root, "commit", "-q", "-m", subject, "--", path)
        for _ in range(3):
            if _try(root, "push", "-q", remote, f"HEAD:refs/heads/{branch}")[0] == 0:
                return {
                    "landed": True,
                    "commit": _git(root, "rev-parse", "--short", "HEAD").strip(),
                    "subject": subject,
                }
            _git(root, "fetch", "-q", remote, branch)
            if _try(root, "rebase", f"{remote}/{branch}")[0]:
                _try(root, "rebase", "--abort")
                break
        _git(root, "reset", "-q", "--hard", f"{remote}/{branch}")  # our private tree: only the unpushed commit goes
        return {"landed": False, "why": "push refused and the rebase did not apply"}


# ------------------------------------------------------------------------------------------------ rename proof
def renamed(config: Config, against: str) -> str:
    """The second proof kind of the lock-free land path: unbake's renameproof says the working tree differs from
    `against` only by identifier renames (no ROM build). "" when proven, else why not."""
    try:
        judge.call(config, "renameproof", "--against", against)
    except Refusal as refusal:
        return refusal.findings[0].reason if refusal.findings else "renameproof refused"
    return ""


def land_renames(config: Config, patch: Path, subject: str, remote: str = _REMOTE, branch: str = _BRANCH) -> Json:
    """Apply a rename-only patch in the private worktree at the branch tip, prove it with renameproof, commit it as one
    commit and push fast-forward; a patch that does not prove is taken back out and nothing lands."""
    root = config.project.root
    if not private(root):
        raise Refusal(Finding("fuzzy.tree", f"{root} is not a linked worktree"))
    if len(subject) > 72:
        raise Refusal(Finding("fuzzy.subject", "the subject is over 72 characters"))
    sync(root, remote, branch)
    head = _git(root, "rev-parse", "HEAD").strip()
    code, said = _try(root, "apply", str(patch))
    if code:
        return {"landed": False, "why": f"patch does not apply: {said.strip()[-200:]}"}
    why = renamed(config, head)
    files = [f for f in _git(root, "diff", "--name-only").splitlines() if f]
    if why or not files:
        _git(root, "checkout", "-q", "--", *files) if files else None
        return {"landed": False, "why": why or "the patch changes nothing"}
    with ExitStack() as stack:
        for f in files:
            stack.enter_context(file_lock(root, f))
        _git(root, "add", "--", *files)
        code, said = _try(root, "commit", "-q", "-m", subject)
        if code:
            _git(root, "reset", "-q", "--hard", head)
            return {"landed": False, "why": f"commit refused: {said.strip()[-120:]}"}
        for _ in range(3):
            if _try(root, "push", "-q", remote, f"HEAD:refs/heads/{branch}")[0] == 0:
                return {
                    "landed": True,
                    "commit": _git(root, "rev-parse", "--short", "HEAD").strip(),
                    "subject": subject,
                }
            _git(root, "fetch", "-q", remote, branch)
            if _try(root, "rebase", f"{remote}/{branch}")[0]:
                _try(root, "rebase", "--abort")
                break
        _git(root, "reset", "-q", "--hard", f"{remote}/{branch}")
    return {"landed": False, "why": "push refused and the rebase did not apply"}


# ------------------------------------------------------------------------------------------------ metrics
def metrics(config: Config, layout: world.Layout, scores: dict[str, float]) -> Json:  # scores: the bodies just landed
    """Functions with C / all per version, and the size-weighted score from docs/map-*.json (new bodies by their
    gate score). A function has C when it is landed or has a NON_MATCHING body."""
    root = config.project.root
    out: Json = {}
    for version in config.project.versions:
        funcs = [m for m in layout.members.values() if m.kind == "function" and version in m.holders()]
        withc = [m for m in funcs if m.landed or m.name in layout.fuzzy or m.name in scores]
        row: Json = {
            "with_c": len(withc),
            "functions": len(funcs),
            "pct": round(100 * len(withc) / max(1, len(funcs)), 2),
        }
        try:
            fns = json.loads((root / f"docs/map-{version}.json").read_text())["fns"]
            total = sum(f[2] for f in fns) or 1

            def part(f: list[Any]) -> float:
                base = re.sub(r"_(us_rev1|us|de|eu_x|eu)$", "", f[0])
                return max(f[3] / 100, scores.get(f[0], 0.0), scores.get(base, 0.0)) if f[3] < 100 else 1.0

            row["weighted_pct"] = round(100 * sum(f[2] * part(f) for f in fns) / total, 2)
        except (OSError, KeyError, ValueError):
            row["weighted_pct"] = None
        out[version] = row
    return out


# ------------------------------------------------------------------------------------------------ the verb
def _at(text: str, name: str) -> int:
    found = stand_in(text, name)
    return found.start() if found else len(text)


def _write(root: Path, texts: dict[str, str]) -> None:
    for path, text in texts.items():
        host.write(root / path, text.encode())


def _chunk(
    config: Config, snapshot: Snapshot, layout: world.Layout, chunk: list[str], remote: str, branch: str, dry: bool
) -> tuple[dict[str, str], dict[str, float], list[Json], dict[str, str]]:
    """One batch: draft, gate and land; (failed, scores, landed files, bodies)."""
    root = config.project.root
    sync(root, remote, branch)
    rows = drafts(snapshot, chunk)
    failed: dict[str, str] = {r["member"]: r["why"] for r in rows if r["why"]}
    bodies = {r["member"]: r["text"] for r in rows if not r["why"]}
    by_module: dict[str, list[str]] = {}
    for member, body in bodies.items():
        if name := copies(snapshot, body):
            failed[member] = f"draft copies the type {name} a header defines"
        else:
            by_module.setdefault(layout.members[member].group, []).append(member)
    base = {p: (root / p).read_text(encoding="utf-8") for p in by_module}
    held = {m: layout.members[m].standing or layout.members[m].holders() for ms in by_module.values() for m in ms}
    text = dict(base)
    live = {p: sorted(ms, key=lambda m, p=p: _at(base[p], m)) for p, ms in by_module.items()}
    scores: dict[str, float] = {}
    for _ in range(3):  # a body that sinks its module's build is dropped; the rest are measured again
        for path, ms in live.items():
            text[path] = base[path]
            for member in ms:
                again = fence(text[path], member, bodies[member])
                if again is None:
                    failed[member] = "no INCLUDE_ASM stand-in line in its module"
                else:
                    text[path] = again
            live[path] = [m for m in ms if m not in failed]
        _write(root, text)
        verdict = gate(config, {m: held[m] for ms in live.values() for m in ms})
        dropped = False
        for member, row in verdict.items():
            if row["why"]:
                failed[member], dropped = row["why"], True
            else:
                scores[member] = row["score"]
        if not dropped:
            break
    _git(root, "checkout", "-q", "--", ".")  # the private tree is clean again: each module lands from the branch tip
    landed: list[Json] = []
    for path, ms in live.items():
        ms = [m for m in ms if m not in failed]
        if not ms:
            continue
        final = base[path]
        for member in ms:
            final = fence(final, member, bodies[member]) or final
        if dry:
            landed.append({"path": path, "functions": ms, "landed": False})
            continue
        one = land_file(root, remote, branch, path, base[path], final, sorted(ms))
        landed.append({"path": path, "functions": sorted(ms), **one})
        if not one["landed"]:
            failed.update({m: one["why"] for m in ms})
    return failed, scores, landed, bodies


def run(config: Config, params: Json) -> Json:
    """`recipe fuzzy`: draft, gate and land NON_MATCHING bodies for every function with no C."""
    root = config.project.root
    remote, branch = params.get("remote") or _REMOTE, params.get("branch") or _BRANCH
    dry = bool(params.get("dry"))
    if params.get("rename_patch"):
        one = land_renames(config, Path(params["rename_patch"]), params.get("subject") or "", remote, branch)
        return {"rename": one, "next": "recipe status"}
    if not private(root):
        raise Refusal(
            Finding("fuzzy.tree", f"{root} is not a linked worktree: run `recipe fuzzy --project <your worktree>`")
        )
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise Refusal(Finding("fuzzy.dirty", f"{root} has tracked changes; fuzzy needs a clean private worktree"))
    sync(root, remote, branch)
    snapshot = api.snapshot(config)
    layout = world.of(snapshot)
    names = targets(snapshot, params.get("version") or (), params.get("only") or ())
    if params.get("limit"):
        names = names[: int(params["limit"])]
    failed: dict[str, str] = {}
    scores: dict[str, float] = {}
    bodies: dict[str, str] = {}
    landed: list[Json] = []
    size = int(params.get("chunk") or 16)
    for at in range(0, len(names), size):
        f, sc, ld, bd = _chunk(config, snapshot, layout, names[at : at + size], remote, branch, dry)
        failed.update(f)
        scores.update(sc)
        landed += ld
        bodies.update(bd)
        sys.stderr.write(f"fuzzy: {min(at + size, len(names))}/{len(names)} drafted, {len(landed)} files\n")
    refused: Counter[str] = Counter(
        r["why"] for r in landed if r.get("why") and not r.get("landed") and r["why"] != "dry"
    )
    done = {n for r in landed if r.get("landed") for n in r["functions"]}
    path = Path(params["failed"]) if params.get("failed") else root / ".unbake/recipe/fuzzy-failed.json"
    listing = [
        {"member": n, "why": w, "holders": list(layout.members[n].holders()), "size": _size(layout, n)}
        for n, w in sorted(failed.items())
    ]
    host.write(path, json.dumps(listing, indent=1).encode())
    why = Counter(_class(w) for w in failed.values())
    return {
        "attempted": len(names),
        "compiled": len(scores),
        "landed": len(done),
        "failed": len(failed),
        "debt_bodies": sum(debt(bodies[n]) > 0 for n in done),
        "commits": [r for r in landed if r.get("landed")],
        "refused": dict(refused),
        "failed_path": str(path),
        "failed_by_reason": why.most_common(12),
        "fuzzy": metrics(config, layout, {n: scores[n] for n in done}),
        "next": "recipe fuzzy --version V for the next set, or hand the failed list to the fill lanes",
    }


def _class(why: str) -> str:
    """A failure reason reduced to its kind for the count: the compiler's own words without names and numbers."""
    head = re.sub(r"compile\.error: version \S+: ", "", why)
    found = re.search(
        r"(conflicting types|undeclared|void value not ignored|parse error|syntax error|redefinition|"
        r"incompatible|invalid|too many|too few|storage size|has no member|no compile|needs header|"
        r"empty body|m2c|jump table|no INCLUDE_ASM|copies the type|push refused|changed on)",
        head,
    )
    return found.group(1) if found else re.sub(r"[\w/.]*\d[\w/.]*|\W+", " ", head)[:60].strip()


def _size(layout: world.Layout, name: str) -> int:
    member = layout.members[name]
    return sum(p.size for p in member.placements if p.version == member.holders()[0])


def render(result: Json) -> list[str]:
    if "rename" in result:
        one = result["rename"]
        return [
            f"rename-only patch landed {one['commit']}: {one['subject']}"
            if one["landed"]
            else f"not landed: {one['why']}"
        ]
    lines = [
        f"fuzzy: {result['attempted']} attempted, {result['compiled']} compiled in every holding version, "
        f"{result['landed']} landed, {result['failed']} failed; {result['debt_bodies']} landed bodies carry m2c debt"
    ]
    lines += [f"  {c['commit']} {c['subject']}" for c in result["commits"]]
    lines += [f"  refused: {k} x{v}" for k, v in result["refused"].items()]
    lines += [f"  failed x{n}: {w}" for w, n in result["failed_by_reason"]]
    lines.append(f"failed list: {result['failed_path']}")
    for version, row in result["fuzzy"].items():
        lines.append(
            f"fuzzy {version}: {row['with_c']}/{row['functions']} functions with C ({row['pct']}%), "
            f"size-weighted {row['weighted_pct']}%"
        )
    return lines
