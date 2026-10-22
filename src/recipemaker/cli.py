"""The `recipe` command line: short text ending in one `next:` line, or one JSON object with `--json`."""

import argparse
import json
import os
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal

from recipemaker import advice, brief, crack, errors, lifecycle, naming, state, structure, style, work, world

_VERBS: dict[str, Callable[[Config, Json], Json]] = {
    "next": work.next_item,
    "work": work.work,
    "crack": crack.run,
    "try": work.try_,
    "skip": work.skip,
    "look": work.look_,
    "status": work.status,
    "structure": structure.run,
    "apply": structure.apply,
    "name": naming.run,
    "style": style.run,
}


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")


def _landing(result: Json) -> list[str]:
    if result["landed"]:
        same = [f"same shape, still open: {' '.join(result['alike'])} (recipe work NAME)"] * bool(result["alike"])
        return [f"landed ({result['landed']})", *same]
    if result["saved"]:
        return [f"saved as nonmatching {brief.scores(result['score'])} ({result['saved']})"]
    return []


def _render_style(result: Json) -> list[str]:
    counts = ", ".join(f"{k} {v}" for k, v in sorted(Counter(f["rule"] for f in result["findings"]).items()))
    lines = [f"{result['files']} files, {result['parsed']} parsed: {counts or 'no findings'}"]
    lines += [f"{f['rule']} {f['path']} {f['detail']}".rstrip() for f in result["findings"][:50]]
    return lines


def _render_plan(result: Json) -> list[str]:
    lines = [f"dry: {result['why']}"]
    for number, names in enumerate(result["batches"], 1):
        lines.append(f"batch {number}: {' '.join(names)}")
    for one in result["plans"]:
        if one["skip"]:
            lines.append(f"- {one['name']}: not planned, {one['skip']}")
            continue
        extra = f", {one['casts']} casts" * bool(one["casts"]) + ", needs casts" * (
            one["cast_needed"] and not one["casts"]
        )
        left = f"; left: {', '.join(one['left'])}" * bool(one["left"])
        lines.append(
            f"- {one['name']} (critical {one['critical']}) -> {one['header']}: {one['removed']} copies deleted,"
            f" {one['includes']} includes added{extra}{left}; files: {', '.join(one['files'])}"
        )
    left = result["reported"]
    lines.append(
        f"reported, no landed definition (type unknown): {len(left)}; first: " + ", ".join(r["name"] for r in left[:12])
    )
    return lines


def _render_next(result: Json) -> list[str]:
    if not result["item"]:
        return [f"none: {result['why']}"]
    if result.get("dry"):
        return _render_plan(result)
    shape = (
        ""
        if result["kind"] in ("declaration", "type")
        else f"   size: {result['size']} bytes   regions: {result['regions']}"
    )
    brief_line = [f"brief: {result['brief']}"] if result["kind"] in ("declaration", "type") else []
    return [
        f"item: {result['item']}",
        f"kind: {result['kind']}{shape}",
        *brief_line,
        f"why: {result['why']}",
        *([f"batch: {' '.join(result['batch'])}"] if len(result.get("batch", ())) > 1 else []),
        *([f"twin: {result['twin']}"] if "twin" in result else []),
        f"lease: ends after {result['stall_tries']} tries without progress or {result['idle_minutes']:g} idle minutes",
    ]


def _render_work(result: Json) -> list[str]:
    if result.get("kind") in ("declaration", "type"):
        more = [f"record: {result['record']}", *[f"  {x}" for x in result["fields"]]] if result.get("record") else []
        more += [f"inside: {x}" for x in result.get("records", [])] + [
            f"conflict: {x}" for x in result.get("conflicts", [])
        ]
        return [*_render_next(result), *more, f"wall: {result['wall_s']} s"]
    if result["result"] == "skipped":
        return [f"skipped {result['item']} as {result['reason']}: {result['why']}"]
    lines = [*_landing(result), f"result: {result['result']}"]
    if result["score"]:
        lines.append("score: " + brief.scores(result["score"]))
    if result["refused"]:
        lines.append(f"exact but refused: {result['refused']}")
    elif result["result"] == "exact" and not result["landed"]:
        lines.append(
            "not landed: " + ("submitted, waiting for the landing" if result["submitted"] else "not submitted")
        )
    if result["seed"]:
        lines.append(result["seed"])
    if result.get("unresolved_classes"):
        rows = sorted(result["unresolved_classes"].items(), key=lambda r: (-r[1], r[0]))
        lines.append("placeholders left: " + "; ".join(f"{n} {why}" for why, n in rows))
    if result["twin"]:
        lines.append("twin: " + result["twin"])
    if result["auto_fixes"]:
        lines.append("auto-fixes: " + "; ".join(result["auto_fixes"]))
    if "restored" in result:
        lines.append("REGRESSED: " + result["restored"])
    if result["result"] != "exact":
        lines += [f"file: {result['candidate']}", f"brief: {result['brief']}"]
        budget = result["budget"]
        lines.append(
            f"budget: {budget['stall_tries'] - budget['stalled']} tries without progress left, "
            f"{budget['minutes_left']:g} idle minutes left"
        )
    return [*lines, f"wall: {result['wall_s']} s"]


def _regions(found: Json) -> list[str]:
    if not found["total"]:
        return []
    gained = "" if found["gained"] is None else f" ({found['gained']:+d})"
    return [f"regions matching {found['matched']} of {found['total']}{gained}"]


def _render_try(result: Json) -> list[str]:
    word = result["result"] if result["compiled"] else "does not compile"
    budget = (
        f"{result['stalled']} of {result['stall_tries']} tries without progress, "
        f"{result['minutes_left']:g} idle minutes left"
    )
    lines = [*_landing(result), f"result: {word} ({budget})", "score: " + brief.scores(result["score"])]
    lines += _regions(result["regions"])
    lines += [f"compiler: {text}" for text in result["compiler"]]
    if "error" in result:
        error = result["error"]
        where = f" at line {error['line']}" if error["line"] else ""
        lines += [f"error: {error['class']}{where}", f"what: {error['what']}", f"fix: {error['fix']}"]
    if result["auto_fixes"]:
        lines.append("auto-fixes: " + "; ".join(result["auto_fixes"]))
    if "restored" in result:
        lines.append("REGRESSED: " + result["restored"])
    if len(result.get("batch", ())) > 1:
        lines += [
            f"- {b['item']}: {b['result']}"
            + (f" ({b['landed']})" if b["landed"] else "")
            + (f", {b['why'][0]}" if b["why"] else "")
            for b in result["batch"]
        ]
    if result["result"] not in ("exact", "would-land"):
        lines += [*(f"why: {w}" for w in result["why"]), f"file: {result['file']}", f"brief: {result['brief']}"]
    return lines


def _render_look(result: Json) -> list[str]:
    lines = [f"{result['kind']}: {result['name']}", result["text"]]
    if result["where"]:
        lines.append(f"where: {result['where']}")
    lines += [f"  {f['offset']} size {f['size']} {f['kind']} {f['name']}" for f in result["fields"]]
    if result["seen_in"]:
        lines.append("seen in: " + ", ".join(result["seen_in"][:8]))
    return [*lines, *result["facts"]]


def _render_status(result: Json) -> list[str]:
    today = result["today"]
    debt = [f"debt: {d['name']} blocks {d['blocks']} items, {d['spellings']} spellings" for d in result["debt"]]
    lines = [
        f"head: {result['head']}   open: {result['open']}   twin ready: {result['twin_ready']}",
        f"today: {today['exact']} exact, {today['fuzzy']} fuzzy, {today['skipped']} skipped, {today['tries']} tries",
    ]
    lines += [
        f"session {s['session']}: {s['item']}, {s['tries']} tries without progress, "
        f"{s['minutes_left']:g} idle minutes left"
        for s in result["sessions"]
    ]
    for label, key in (("skips", "skip_reasons"), ("errors", "error_classes")):
        if result[key]:
            lines.append(f"{label}: " + ", ".join(f"{k} {v}" for k, v in sorted(result[key].items())))
    return [*lines, *debt]


def _render_structure(result: Json) -> list[str]:
    sizes = [f"{k} {v['before']} -> {v['after']}" for k, v in result.items() if isinstance(v, dict) and "before" in v]
    return [
        f"proposed {result['moves']} moves in {result['path'] or 'no patch'}, {len(result['boundaries'])} boundaries",
        ", ".join(sizes),
        *result.get("findings", []),
    ]


def _render_name(result: Json) -> list[str]:
    if result["action"] == "next":
        pack = f"pack {result['pack']} holds {result['targets']} targets, " if result["pack"] else ""
        return [f"{result['step']}: {pack}{result['moves']} moves in patch {result['path'] or 'none'}"]
    if result["action"] == "submit":
        rules = ", ".join(f"{k} {v}" for k, v in sorted(result["by_rule"].items()))
        return [
            f"{result['accepted']} of {result['rows']} names accepted as {result['moves']} moves"
            f" in {result['path'] or 'no patch'}",
            *([f"refused {result['refused']}: {rules}"] if result["refused"] else []),
        ]
    return [f"{len(result['files'])} banner candidates, {result['skipped']} skipped"]


_RENDER: dict[str, Callable[[Json], list[str]]] = {
    "next": _render_next,
    "work": _render_work,
    "try": _render_try,
    "crack": lambda r: [
        f"permuter {r['permuter'][0]} -> {r['permuter'][1]} score {r['score'][0]:.1%} -> {r['score'][1]:.1%}",
        *([f"budget: {r['budget']}"] if r.get("budget") else []),
        *r["diff"].splitlines(),
    ],
    "skip": lambda result: [f"skipped {result['item']} as {result['reason']}"],
    "look": _render_look,
    "status": _render_status,
    "structure": _render_structure,
    "apply": lambda r: [f"applied {r['patch']}, {r['left']} patches left, unbake sync done"],
    "name": _render_name,
    "style": _render_style,
}


def render(verb: str, result: Json) -> str:
    return "\n".join([*_RENDER[verb](result), f"next: {result['next']}"])


def _action(action: str, item: str | None) -> str:
    """An unbake action names an unbake command; the recipe verb that does it is try, or skip when only S can."""
    if "unbake " not in action or "--pad-split" in action:
        return action or "recipe status"
    if item is None:
        return "recipe status"
    return f"recipe try {item}" if action.startswith("unbake compare") else f"recipe skip {item} --reason other"


def _finding(finding: Finding, item: str | None = None) -> list[str]:
    lines = errors.render(finding)
    return [*lines[:-1], f"next: {_action(finding.action, item)}"]


_RETRY = ("internal.race", "land.rebase", "recipe.reason")  # the same command again is their one fix


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recipe", epilog=advice("sessions"), formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", type=Path, default=None, help="unbake host config (default: UNBAKE_HOST)")
    parser.add_argument("--project", type=Path, default=None, help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="verb", required=True)

    def verb(name: str, help_: str) -> argparse.ArgumentParser:
        one = sub.add_parser(name, help=help_)
        one.add_argument("--json", action="store_true", help="print one JSON object")
        return one

    one = verb("next", "lease the best open item")
    one.add_argument("--kind", choices=("function", "data", "declaration", "type"))
    one.add_argument("--twin", choices=("yes", "no"), help="only items with (yes) or without (no) a landed twin")
    one.add_argument("--max-size", type=int, help="largest item in bytes")
    one.add_argument("--order", choices=("small", "large"), help="smallest or largest first (default: largest)")
    one.add_argument("--count", type=int, default=0, help="declarations: plan this many of the best-ranked names")
    one.add_argument("--dry", action="store_true", help="declarations: show the batches and change sets, lease nothing")
    one = verb("work", "run the tool rungs on an item; prints once at exit, and a large item can take minutes")
    one.add_argument("item")
    one.add_argument("--kind", choices=("declaration", "type"), help="work the declaration or type item of this name")
    one.add_argument("--arg", type=int, help="type items: the parameter (default: the first void * the code reaches)")
    one.add_argument("--no-submit", action="store_true")
    one = verb("crack", "permuter search of the candidate's shape; writes a better candidate file if it finds one")
    one.add_argument("item")
    one.add_argument("--seconds", type=int, help="permuter run (default the recipe.toml budget.permute_seconds)")
    one = verb("try", "prove the candidate file")
    one.add_argument("item")
    one.add_argument("--dry", action="store_true", help="declarations: measure the batch, land nothing")
    one = verb("look", "show a prototype, type, field or global")
    one.add_argument("name")
    one = verb("skip", "release an item with a reason")
    one.add_argument("item")
    one.add_argument("--reason", required=True, help="one of: " + ", ".join(state.SKIP_REASONS))
    one.add_argument("--note", default="")
    one = verb("structure", "propose module structure: one JSON object {patch, files, moves} on stdout")
    one.add_argument("--segment", help="only these segments (comma list)")
    one.add_argument("--paths", nargs="+", metavar="FILE", help="only the modules holding these files and linked moves")
    one = verb("name", "name things from evidence: next (pack or tag-named subsystem moves), submit FILE, banners")
    one.add_argument("action", choices=("next", "submit", "banners"))
    one.add_argument("file", nargs="?", help="the names file for submit")
    one.add_argument(
        "--step",
        choices=("N1", "N2", "N3", "N4", "T"),
        help="the naming step (default: N3); T: original types from the debug catalog",
    )
    one.add_argument("--count", type=int, default=40, help="targets per pack")
    one.add_argument("--target", help="pack this one function, datum or folder only (the one already chosen)")
    one = verb("style", "style findings; exit 1 when one is an error")
    one.add_argument("paths", nargs="*", help="check only these paths")
    one = verb("apply", "git apply the oldest saved patch, then unbake sync")
    one.add_argument("patch", nargs="?", help="a patch file (default: the oldest saved one)")
    verb("status", "sessions, leases and counts")
    return parser


def _emit(text: str, document: Json, as_json: bool) -> None:
    sys.stdout.write((json.dumps(document, default=str) if as_json else text) + "\n")


def _where(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return f"{exc!r} at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else repr(exc)


_NO_HOST = (
    "no host config: pass --host, set UNBAKE_HOST, or create ~/.config/unbake/unbake.toml "
    "(a seat's `decomp --profile P recipe ...` passes the profile's tools.host itself)"
)


def _default_host() -> Path | None:
    """The host config a call names nowhere: the user's ~/.config/unbake/unbake.toml when it exists."""
    path = Path("~/.config/unbake/unbake.toml").expanduser()
    return path if path.is_file() else None


def _parked(root: Path) -> list[str]:
    """decomp ship's notices of set-aside changes that no longer applied, said by every command until the patch goes."""
    path = root / ".unbake/ship-parked.json"
    rows = json.loads(path.read_text()) if path.is_file() else []
    say = "ship.parked: {} no longer apply; owners: {}; git apply --3way {} then delete it"
    return [
        say.format(" ".join(r["files"]), ", ".join(r["owners"]), r["patch"]) for r in rows if Path(r["patch"]).is_file()
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    for line in _parked(args.project or Path.cwd()):
        sys.stderr.write(line + "\n")
    began = time.monotonic()
    host = args.host or (Path(os.environ["UNBAKE_HOST"]) if os.environ.get("UNBAKE_HOST") else None)
    host = host or _default_host()
    item = getattr(args, "item", None)
    result: dict[str, Any] | None = None
    code, findings, landed = 0, [], None
    loaded: Config | None = None
    try:
        with lifecycle.bound():
            if host is None:
                raise Refusal(Finding("config.missing", _NO_HOST))
            os.environ["UNBAKE_HOST"] = str(host.resolve())  # every unbake command this one runs reads the same host
            loaded = api.load(args.project or Path.cwd(), host)
            given = {k: v for k, v in vars(args).items() if k not in ("host", "project")}
            if args.verb in ("structure", "apply", "name"):  # these verbs call unbake
                given["host"] = host
            # each unbake command a verb runs takes the shared tree lock itself; a try holds none across its compiles
            # of a private candidate, which read nothing a ship rewrites. Only `apply` writes the tree.
            with (
                api.tree_lock(loaded, exclusive=True, who="apply", say=_say) if args.verb == "apply" else nullcontext()
            ):
                result = dict(_VERBS[args.verb](loaded, given))
            code = int(result.pop("exit", 0))
    except Refusal as refusal:
        code, findings = 1, list(refusal.findings)
    except work.AfterLanding as late:
        code, findings, landed = 3, [Finding("internal.error", _where(late.cause), action="recipe next")], late
    except world.AsmMissing as gone:
        retry = f"recipe {args.verb} {item}" if item else f"recipe {args.verb}"
        code, findings = 3, [Finding("internal.race", f"{gone}: retry the same command", action=retry)]
    except Exception as exc:  # an internal error is never a reason to skip an item: the item was not judged
        again = f"recipe {args.verb} {item}" if item else "recipe next"
        code, findings = (
            3,
            [
                Finding(
                    "internal.error",
                    f"{_where(exc)}: not a judgement of the item, retry the same command once",
                    action=again,
                )
            ],
        )
    if result is not None:
        result["wall_s"] = round(time.monotonic() - began)
        stream = result.pop("stream", "")
        if stream and not args.json:  # the patch owns stdout so that `recipe structure | jq -r .patch` works
            sys.stdout.write(stream)
            sys.stderr.write(render(args.verb, result) + "\n")
        else:
            _emit(render(args.verb, result), {"ok": True, **result}, args.json)
    else:
        first = []
        if landed is not None:
            first = [f"landed ({landed.landed})"] if landed.landed else []
            first += [f"saved as nonmatching ({landed.saved})"] if landed.saved else []
        lines = [*first, *[line for f in findings for line in _finding(f, item)]]
        refused = " ".join(["recipe", args.verb, item or ""]).strip()
        if lines[-1].startswith(f"next: {refused}") and findings[-1].key not in _RETRY:
            # a refusal never sends the agent back to its command, a race with an extraction does
            lines[-1] = "next: recipe status"
        document = {
            "ok": False,
            "landed": landed.landed if landed is not None else "",
            "findings": [asdict(f) for f in findings],
        }
        _emit("\n".join(lines), document, args.json)
    return code
