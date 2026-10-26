"""Source-evidenced repairs of nonportable identifiers, including scoped C locals.

The ordinary naming submit path owns these rows too. A repair needs no ROM map:
its evidence is an existing identifier at an explicit source path and line.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from unbake import api
from unbake.contracts import Json

from recipemaker import access, cparse, locals_

WORD = re.compile(r"[A-Za-z_]\w*")


def local_spans(text: str, function: str, aliased: bool = True) -> list[tuple[int, int]]:
    """Every complete definition (else prototype) of FUNCTION: more than one when version conditionals each carry a
    copy."""
    masked = access.mask(text)
    head = re.compile(rf"^[\w\s*]+\b{re.escape(function)}\s*\(")
    found = [(a, b) for a, b, directive in cparse.spans(masked) if not directive and head.match(masked[a:b])]
    if len(found) > 1:  # a prototype beside its definition (or two): the one with a body holds the locals
        found = [(a, b) for a, b in found if "{" in masked[a:b]]
    if not found and aliased:
        # a NON_MATCHING body written under another spelling: `#define ALIAS FUNCTION` then `s32 ALIAS(...) {`
        # (10-25 P3-Q20: 39 slot rows of eu/eu-x menu bodies refused "has no local")
        for alias in re.findall(
            rf"(?m)^[ 	]*#[ 	]*define[ 	]+(\w+)[ 	]+{re.escape(function)}[ 	]*$", text
        ):
            if alias != function and (spans := local_spans(text, alias, aliased=False)):
                return spans
        return []
    return found


def local_span(text: str, function: str, aliased: bool = True) -> tuple[int, int] | None:
    """One complete function definition or prototype; ambiguous conditional copies refuse."""
    found = local_spans(text, function, aliased)
    return found[0] if len(found) == 1 else None


def span_at(text: str, function: str, line: int) -> tuple[int, int] | None:
    """The definition of FUNCTION that holds the 1-based LINE when conditional copies make several: a row names the
    line it
    read, so the copy it read is the copy it renames (10-25 P3-R12: Pak_SelectMusicPreview's two bodies refused every
    slot)."""
    found = local_spans(text, function)
    if len(found) == 1:
        return found[0]
    at = sum(len(x) for x in access.mask(text).splitlines(keepends=True)[: max(line - 1, 0)])
    return next((s for s in found if s[0] <= at < s[1]), None)


class Repairs:
    """One source inventory per submitted batch, with collision checks before patch creation."""

    def __init__(self, root: Path, original: Callable[[], Mapping[str, Sequence[str]]] | None = None) -> None:
        self.root = root
        self._original = original  # read on the first local row: function -> its debug catalog variable names
        self.original: Mapping[str, Sequence[str]] | None = None
        self.text: dict[str, str] = {}
        self.taken: set[str] | None = None
        self.accepted: set[str] = set()
        self.scoped: dict[str, set[str]] = {}  # per function, the local names rows already queued or accepted gave
        self.protos = locals_.Prototypes(root)

    def read(self, path: str) -> str:
        if path not in self.text:
            self.text[path] = (self.root / path).read_text(encoding="utf-8")
        return self.text[path]

    def names(self) -> set[str]:
        if self.taken is None:
            self.taken = set()
            for folder in ("src", "include"):
                for path in sorted((self.root / folder).rglob("*")):
                    if path.is_file() and path.suffix.lower() in (".c", ".cpp", ".h", ".s"):
                        self.taken.update(WORD.findall(access.mask(self.read(path.relative_to(self.root).as_posix()))))
            for pattern in ("*/symbol_addrs.txt", "*/aliases.txt"):
                for path in (self.root / "versions").glob(pattern):
                    self.taken.update(re.findall(r"(?m)^\s*(\w+)\s*=", path.read_text(encoding="utf-8")))
        return self.taken | self.accepted

    def judge(self, row: Json) -> tuple[str, str, list[Json]]:
        old, new, path = (row.get(k) for k in ("target", "name", "path"))
        if (
            not isinstance(old, str)
            or not isinstance(new, str)
            or not WORD.fullmatch(old)
            or not WORD.fullmatch(new)
            or not isinstance(row.get("doc"), str)
            or not row["doc"].strip()
        ):
            return "shape", "a repair needs identifier target, name, source path, line and doc", []
        slot = row.get("kind") == "local" and locals_.placeholder(old)  # a decompiler's name for a register or slot
        if not slot and not api.name_issues(old):
            return "target", f"{old} is already portable", []
        if slot and locals_.placeholder(new):
            return "name", f"{new} is a decompiler slot name again", []
        if why := api.name_issues(new):
            return "portable", why[0], []
        if not isinstance(path, str) or not isinstance(row.get("line"), int) or row["line"] < 1:
            return "evidence", "name the source path and positive line of the existing identifier", []
        part = PurePosixPath(path)
        if not part.parts or part.is_absolute() or ".." in part.parts or part.parts[0] not in ("src", "include"):
            return "evidence", "source path must stay inside src/ or include/", []
        file = self.root / path
        if not file.is_file() or not file.resolve().is_relative_to(self.root.resolve()):
            return "evidence", "source path must name a project file", []
        text = self.read(path)
        masked = access.mask(text)
        lines = masked.splitlines()
        token = re.compile(rf"\b{re.escape(old)}\b")
        if (
            row["line"] <= len(lines)
            and not token.search(lines[row["line"] - 1])
            and re.search(rf"\b{re.escape(new)}\b", lines[row["line"] - 1])
        ):
            return "applied", f"{path}:{row['line']} already spells {new}", []
        if row["line"] > len(lines) or not token.search(lines[row["line"] - 1]):
            return "evidence", f"{path}:{row['line']} does not spell {old} as an identifier", []
        move: dict[str, str] = {"op": "symbol", "subject": old, "target": new}
        if row["kind"] == "local":
            function = row.get("function")
            span = (
                span_at(text, function, row["line"]) if isinstance(function, str) and WORD.fullmatch(function) else None
            )
            if span is None:
                return "scope", "name one unambiguous function definition or prototype", []
            a, b = span
            at = sum(len(line) for line in masked.splitlines(keepends=True)[: row["line"] - 1])
            if not a <= at < b:
                return "scope", "evidence line is outside the named function", []
            scope = masked[a:b]
            declaration = re.compile(rf"\b(\w+)(?:\s+|\s*(?:\*\s*)+|\s*\(\s*\*\s*){re.escape(old)}\s*(?=[=;,)\[])")
            declared = [m for m in declaration.finditer(scope) if m[1] not in ("return", "goto", "case")]
            if not declared or re.search(r"\b(?:struct|union)\s+\w*\s*\{", scope):
                return "scope", "local scope needs a declaration and no nested record definition", []
            if self.original is None:
                self.original = self._original() if self._original else {}
            known = self.original.get(str(function), ())
            if slot and (
                bad := locals_.verify(
                    scope,
                    old,
                    row.get("evidence"),
                    self.protos.get(locals_.calls(scope)),
                    known,
                    (masked.count("\n", 0, a) + 1, masked.count("\n", 0, b) + 1),
                    text.splitlines(),
                )
            ):
                return "evidence", bad, []
            taken = set(WORD.findall(scope)) | self.scoped.get(str(function), set())
            move |= {"op": "local", "path": path, "function": str(function), "line": row["line"]}
        else:
            taken = self.names()
        if new in taken or api.name_issues(new, taken=sorted(taken - {old})):
            return "duplicate", f"{new} is already used in the rename scope", []
        if row["kind"] == "local":
            self.scoped.setdefault(str(function), set()).add(new)
        self.accepted.add(new)
        return "", "", [move]
