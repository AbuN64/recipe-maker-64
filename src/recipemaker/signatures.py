"""m2c prototypes and header context."""

import re
from bisect import bisect_right
from collections.abc import Container, Mapping, Sequence
from pathlib import Path

from pycparser import CParser, c_ast, c_generator
from pycparser.c_parser import ParseError
from unbake import effort, headers, layout, recipes, store, types
from unbake import view as _view
from unbake.contracts import Finding, Refusal, Snapshot, UnitSpec, digest

from recipemaker import cparse


def signature(snapshot: Snapshot, names: Sequence[str]) -> dict[str, str]:
    """The prototype types.toml holds for each name that has one: authored, declared or a landed definition."""
    rows = types.load(snapshot)["function"]
    return {n: rows[n]["signature"] for n in names if n in rows}


def _type_map(snapshot: Snapshot, declared: Container[str] = ()) -> str:
    """The prototypes and declarations types.toml derived, for names no header in `declared` declares: a draft
    includes the header's declaration, so m2c must read that one and never a second that disagrees with it."""
    with effort.stage("types.context"):
        doc = types.load(snapshot)
        return "\n".join(
            [
                doc["function"][name]["signature"] + ";"
                for name in sorted(doc["function"])
                if doc["function"][name]["evidence"] != "authored" and name not in declared
            ]
            + [
                doc["global"][name]["declaration"]
                for name in sorted(doc["global"])
                if doc["global"][name]["evidence"] != "authored" and name not in declared
            ]
        )


def declarations(snapshot: Snapshot, names: Sequence[str]) -> dict[str, str]:
    with effort.stage("types.declarations"):
        doc = types.load(snapshot)
        sigs = signature(snapshot, [n for n in names if n not in doc["global"]])
        return {
            name: sigs[name] + ";" if name in sigs else doc["global"][name]["declaration"]
            for name in names
            if name in sigs or name in doc["global"]
        }


def context(snapshot: Snapshot, version: str) -> Path:
    """The preprocessed header context file m2c reads for `version`, built once and named by its content."""
    with effort.stage("headers.context"):
        catalog = headers.catalog(snapshot)
        extra = _type_map(snapshot, catalog)
        path = f"build/context/{version}-{digest(extra)[:16]}.c"
        source = "".join(f'#include "{p.removeprefix("include/")}"\n' for p in headers.sources(snapshot))
        output = snapshot.config.project.root / path
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            store.write(output, source.encode())
            # a plain snapshot reads the file just written, so the view key never carries the snapshot digest
            proposed = layout.overlay(snapshot, {path: source.encode()}) if snapshot.overlays else snapshot
            unit = UnitSpec(path, "c", "", (), snapshot.config.project.toolchain, {"add": [], "omit": []})
            recipe = recipes.resolve(snapshot.config, unit, unit.options)
            result = _view.get(proposed, unit, version, recipe, lines=False)
            output = output.with_name(f"{version}-{digest((result.key, extra))[:16]}.i")  # named by its content
            if not output.exists():
                text = _agree(_neutralise(result.text), catalog).rstrip("\n") + "\n"
                kept, dropped = _parseable(text, extra)
                store.write(output, (text + kept).encode())
                store.write(output.with_suffix(".dropped"), "\n".join(dropped).encode())  # lines C cannot read
        except (Refusal, OSError) as error:
            raise Refusal(Finding("draft.context", reason=str(error), path=path)) from error
        return output


_ASM = re.compile(r'\b(?:__asm__|__asm|asm)\b\s*(?:volatile\s*)?\(\s*(?:"(?:\\.|[^"\\])*"\s*)+\)\s*;')


def _neutralise(text: str) -> str:
    """The context with what m2c cannot parse blanked out (file-scope asm, `sizeof (T) - x` read as a cast)."""
    blanked = _ASM.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), text)
    return re.sub(r"\bsizeof\s*\(\s*((?:struct |union |enum )?\w+)\s*\)", r"(sizeof(\1))", blanked)


_MARKER = re.compile(r'^# \d+ "([^"\n]*)"')
_TABLE = re.compile(r"jtbl_\w+")  # a jump table: m2c reads it as a table only when no declaration types it


def _agree(text: str, catalog: Mapping[str, tuple[str, int, str]]) -> str:
    """The context with the declarations m2c must not read blanked: each declaration of a name that disagrees with
    the one in the header the catalog names for it (the header a draft includes, so m2c reads what the draft will
    compile against, where it would otherwise keep the last), and every declaration of a jump table (a typed table
    is read as an array and its switch is lost). Lines stay where they are."""
    lines = text.split("\n")
    files, current = [], ""
    for line in lines:
        if marker := _MARKER.match(line):
            current = re.sub(r"^build/views/\w+/", "", marker[1])
        files.append(current)
    try:
        tree = CParser().parse(headers.normal(re.sub(r'^# \d+ "[^"\n]*".*$', "", text, flags=re.M)))
    except ParseError:
        return text  # the context check that follows names what does not parse
    spelled = c_generator.CGenerator()
    found: dict[str, list[tuple[int, str]]] = {}
    for node in tree.ext:
        if isinstance(node, c_ast.Decl) and node.name and node.coord:
            found.setdefault(node.name, []).append((node.coord.line, spelled.visit(node.type)))
    blank: set[int] = set()
    for name, rows in found.items():
        if _TABLE.fullmatch(name):
            blank |= {line for line, _ in rows}
        elif len({kind for _, kind in rows}) > 1:
            home = catalog.get(name, ("",))[0]
            kept = next((kind for line, kind in rows if files[line - 1] == home), rows[0][1])
            blank |= {line for line, kind in rows if kind != kept}
    if not blank:
        return text
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    out = list(text)
    for begin, end, directive in cparse.spans(text):
        first, last = bisect_right(starts, begin), bisect_right(starts, end - 1)
        if not directive and any(first <= line <= last for line in blank):
            out[begin:end] = re.sub(r"[^\n]", " ", text[begin:end])
    return "".join(out)


def _parseable(text: str, extra: str) -> tuple[str, list[str]]:
    try:
        ast = CParser().parse(headers.normal(text))
    except ParseError as error:
        raise Refusal(Finding("draft.context", reason=f"the project headers do not parse: {error}")) from error
    names = {n.name for n in ast.ext if isinstance(n, c_ast.Typedef)}
    parser, lines, dropped = CParser(), [], []
    for line in extra.splitlines():  # each line on its own, after stubs of just the typedef names it uses
        words = dict.fromkeys(w for w in re.findall(r"\b[A-Za-z_]\w*\b", line) if w in names)
        try:
            parser.parse(headers.normal("".join(f"typedef int {w};\n" for w in words) + line + "\n"))
            lines.append(line)
        except ParseError:
            dropped.append(line)
    return "\n".join(lines) + ("\n" if lines else ""), dropped
