"""m2c prototypes and header context."""

import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from pathlib import Path

from pycparser import CParser, c_ast, c_generator
from pycparser.c_parser import ParseError
from unbake.contracts import Finding, Refusal, Snapshot, digest

from recipemaker import cparse, cpp, headers, host


def signature(snapshot: Snapshot, names: Sequence[str]) -> dict[str, str]:
    """The prototype a project header declares for each function name that has one, without its semicolon."""
    catalog = headers.catalog(snapshot)
    return {n: catalog[n][2].removesuffix(";") for n in names if n in catalog and "(" in catalog[n][2]}


def declarations(snapshot: Snapshot, names: Sequence[str]) -> dict[str, str]:
    """The header declaration of each name that has one."""
    catalog = headers.catalog(snapshot)
    return {n: catalog[n][2] for n in names if n in catalog}


def context(snapshot: Snapshot, version: str) -> Path:
    """The preprocessed header context file m2c reads for `version`, built once and named by its content."""
    with host.stage("headers.context"):
        catalog = headers.catalog(snapshot)
        source = "".join(f'#include "{p.removeprefix("include/")}"\n' for p in headers.sources(snapshot))
        folder = snapshot.config.project.root / "build/context"
        try:
            text = cpp.run(snapshot.config, "build/context/context.c", source, version)
            output = folder / f"{version}-{digest(text)[:16]}.i"  # named by its content
            if not output.exists():
                text = _agree(_neutralise(text), catalog).rstrip("\n") + "\n"
                _parseable(text)
                host.write(output, text.encode())
        except (Refusal, OSError) as error:
            reason = error.findings[0].reason if isinstance(error, Refusal) else str(error)
            raise Refusal(Finding("draft.context", reason=reason, path="build/context")) from error
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


def _parseable(text: str) -> None:
    try:
        CParser().parse(headers.normal(text))
    except ParseError as error:
        raise Refusal(Finding("draft.context", reason=f"the project headers do not parse: {error}")) from error
