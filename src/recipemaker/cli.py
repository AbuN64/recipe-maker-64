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

from recipemaker import errors, lifecycle, look, naming, structure, style, world

_VERBS: dict[str, Callable[[Config, Json], Json]] = {
    "look": lambda config, params: {**look.look(config, params["name"]), "next": "recipe name next"},
    "structure": structure.run,
    "apply": structure.apply,
    "name": naming.run,
    "style": style.run,
}


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")


def _render_style(result: Json) -> list[str]:
    counts = ", ".join(f"{k} {v}" for k, v in sorted(Counter(f["rule"] for f in result["findings"]).items()))
    lines = [f"{result['files']} files, {result['parsed']} parsed: {counts or 'no findings'}"]
    if result.get("debt"):
        debt = Counter(f["rule"] for f in result["debt"])
        lines.append("debt (info, the base had it): " + ", ".join(f"{k} {v}" for k, v in sorted(debt.items())))
    lines += [f"{f['rule']} {f['path']} {f['detail']}".rstrip() for f in result["findings"][:50]]
    return lines


def _render_look(result: Json) -> list[str]:
    lines = [f"{result['kind']}: {result['name']}", result["text"]]
    if result["where"]:
        lines.append(f"where: {result['where']}")
    lines += [f"  {f['offset']} size {f['size']} {f['kind']} {f['name']}" for f in result["fields"]]
    if result["seen_in"]:
        lines.append("seen in: " + ", ".join(result["seen_in"][:8]))
    return [*lines, *result["facts"]]


def _render_structure(result: Json) -> list[str]:
    sizes = [f"{k} {v['before']} -> {v['after']}" for k, v in result.items() if isinstance(v, dict) and "before" in v]
    return [
        f"proposed {result['moves']} moves in {result['path'] or 'no patch'}, {len(result['boundaries'])} boundaries",
        ", ".join(sizes),
        *result.get("findings", []),
    ]


def _applied(result: Json) -> list[str]:
    if result.get("applied"):
        return [f"applied in place: {len(result.get('changed', []))} files changed, tree synced"]
    return [f"not applied ({result['apply_refused']}); the patch stays saved"] if "apply_refused" in result else []


def _render_name(result: Json) -> list[str]:
    if result["action"] == "next" and result["step"] in ("L", "M"):
        what = (
            f"pack {result['pack']} holds {result['slots']} slots in {result['targets']} functions"
            if result["pack"]
            else "no slot with evidence"
        )
        return [f"{result['step']}: {what}, {result['no_evidence']} without evidence, {result['functions_left']} more"]
    if result["action"] == "next":
        pack = f"pack {result['pack']} holds {result['targets']} targets, " if result["pack"] else ""
        why = [f"  {n} {r}" for r, n in sorted(result.get("reasons", {}).items(), key=lambda x: -x[1])]
        debt = [f"naming debt by file: {result['naming_debt']}"] if result.get("naming_debt") else []
        out = [f"{result['step']}: {pack}{result['moves']} moves in patch {result['path'] or 'none'}", *why, *debt]
        if "outside" in result:
            out += [f"preview: {len(result['files'])} files, {len(result['outside'])} outside: {result['outside'][:8]}"]
        return out
    if result["action"] == "submit":
        rules = ", ".join(f"{k} {v}" for k, v in sorted(result["by_rule"].items()))
        return [
            f"{result['accepted']} of {result['rows']} names accepted as {result['moves']} moves"
            f" in {result['path'] or 'no patch'}",
            *([f"refused {result['refused']}: {rules}"] if result["refused"] else []),
            *result.get("refusals", []),
            *_applied(result),
        ]
    return [f"{len(result['files'])} banner candidates, {result['skipped']} skipped"]


_RENDER: dict[str, Callable[[Json], list[str]]] = {
    "look": _render_look,
    "structure": _render_structure,
    "apply": lambda r: [f"applied {r['patch']}, {r['left']} patches left, unbake sync done"],
    "name": _render_name,
    "style": _render_style,
}


def render(verb: str, result: Json) -> str:
    return "\n".join([*_RENDER[verb](result), f"next: {result['next']}"])


def _action(action: str) -> str:
    """The recipe verb a finding sends the caller to; an unbake command is no recipe verb."""
    return action if action and "unbake " not in action else "recipe name next"


def _finding(finding: Finding) -> list[str]:
    lines = errors.render(finding)
    return [*lines[:-1], f"next: {_action(finding.action)}"]


_RETRY = ("internal.race", "land.rebase", "recipe.reason")  # the same command again is their one fix


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recipe")
    parser.add_argument("--host", type=Path, default=None, help="unbake host config (default: UNBAKE_HOST)")
    parser.add_argument("--project", type=Path, default=None, help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="verb", required=True)

    def verb(name: str, help_: str) -> argparse.ArgumentParser:
        one = sub.add_parser(name, help=help_)
        one.add_argument("--json", action="store_true", help="print one JSON object")
        return one

    one = verb("look", "show a prototype, type, field or global")
    one.add_argument("name")
    one = verb("structure", "propose module structure: one JSON object {patch, files, moves} on stdout")
    one.add_argument("--segment", help="only these segments (comma list)")
    one.add_argument("--paths", nargs="+", metavar="FILE", help="only the modules holding these files and linked moves")
    one = verb("name", "name things from evidence: next (pack or tag-named subsystem moves), submit FILE, banners")
    one.add_argument("action", choices=("next", "submit", "banners"))
    one.add_argument("file", nargs="?", help="the names file for submit")
    one.add_argument(
        "--step",
        choices=("N1", "N2", "N3", "N4", "N5", "T", "L", "M"),
        help="the naming step (default: N3); N5: functions only their own folder calls;"
        " T: original types from the debug catalog (a preview until --apply);"
        " L: locals and parameters of --target; M: unkNN members of a record",
    )
    one.add_argument(
        "--no-apply", action="store_true", help="next/submit leave the patch saved instead of applying it to the tree"
    )
    one.add_argument("--apply", action="store_true", help="step T: apply the patch (default: preview, files listed)")
    one.add_argument(
        "--allow-outside", action="store_true", help="step T --apply: allow files outside the --target folders"
    )
    one.add_argument("--count", type=int, default=40, help="targets per pack")
    one.add_argument("--target", help="pack this one function, datum or folder only (the one already chosen)")
    one = verb("style", "style findings; exit 1 when one is an error")
    one.add_argument("paths", nargs="*", help="check only these paths")
    one.add_argument("--paths", nargs="+", dest="only", metavar="PATH", help="the same, as `structure` takes them")
    one.add_argument(
        "--base", default="HEAD", help="only findings the files did not have at this commit gate (default HEAD)"
    )
    one.add_argument("--full", action="store_true", help="every finding gates, whatever the base had")
    one = verb("apply", "git apply the newest saved patch, then unbake sync")
    one.add_argument("patch", nargs="?", help="a patch file (default: the newest saved one)")
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
    say = "ship.parked: {} no longer apply; git apply --3way {} then delete it"
    return [say.format(" ".join(r["files"]), r["patch"]) for r in rows if Path(r["patch"]).is_file()]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    for line in _parked(args.project or Path.cwd()):
        sys.stderr.write(line + "\n")
    began = time.monotonic()
    host = args.host or (Path(os.environ["UNBAKE_HOST"]) if os.environ.get("UNBAKE_HOST") else None)
    host = host or _default_host()
    result: dict[str, Any] | None = None
    code, findings = 0, []
    loaded: Config | None = None
    try:
        with lifecycle.bound():
            if host is None:
                raise Refusal(Finding("config.missing", _NO_HOST))
            os.environ["UNBAKE_HOST"] = str(host.resolve())  # every unbake command this one runs reads the same host
            loaded = api.load(args.project or Path.cwd(), host)
            given = {k: v for k, v in vars(args).items() if k not in ("host", "project")}
            if args.verb == "style":
                given["paths"] = [*given["paths"], *(given.pop("only") or [])]
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
    except world.AsmMissing as gone:
        code, findings = 3, [Finding("internal.race", f"{gone}: retry the same command", action=f"recipe {args.verb}")]
    except Exception as exc:
        again = f"recipe {args.verb}"
        code, findings = 3, [Finding("internal.error", f"{_where(exc)}: retry the same command once", action=again)]
    if result is not None:
        result["wall_s"] = round(time.monotonic() - began)
        stream = result.pop("stream", "")
        if stream and not args.json:  # the patch owns stdout so that `recipe structure | jq -r .patch` works
            sys.stdout.write(stream)
            sys.stderr.write(render(args.verb, result) + "\n")
        else:
            _emit(render(args.verb, result), {"ok": code == 0, **result}, args.json)
    else:
        lines = [line for f in findings for line in _finding(f)]
        if lines[-1] == f"next: recipe {args.verb}" and findings[-1].key not in _RETRY:
            lines[-1] = "next: recipe name next"  # a refusal never sends the caller back to its command
        document = {"ok": False, "findings": [asdict(f) for f in findings]}
        _emit("\n".join(lines), document, args.json)
    return code
