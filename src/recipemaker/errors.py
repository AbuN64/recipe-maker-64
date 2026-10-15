"""Compiler and unbake errors translated into a class, a line, a plain statement and a fix."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from unbake import headers, layout, types
from unbake.contracts import Finding, Snapshot

from recipemaker import advice, index, resource, table

_STANDARD = {"NULL": "stddef.h", "size_t": "stddef.h", "offsetof": "stddef.h", "bool": "stdbool.h", "true": "stdbool.h"}
_LINE = re.compile(r":(\d+)(?::\d+)?:")
_UNKNOWN_FIX = "report it with: recipe skip ITEM --reason other"


@dataclass(frozen=True)
class Translated:
    cls: str  # a class id from knowledge/errors.toml, or "unknown"
    line: int
    what: str
    fix: str
    autofix: str = ""  # the auto-fix id to run before the next try, or ""
    name: str = ""  # the identifier the error is about, when the class names one


def unbuilt(message: str) -> bool:
    """Whether a proof message says the candidate did not preprocess or compile."""
    return "compile.error" in message or "preprocess" in message


# keys unbake's sentence table may not know
_OWN = ("recipe.", "draft.", "types.m2c", "member.split-delay-slot", "move.")


def own(finding: Finding) -> bool:
    return finding.key.startswith(_OWN)


def render(finding: Finding) -> list[str]:
    row = table("knowledge/errors.toml").get("key", {}).get(finding.key, {})
    sentence = row.get("sentence", finding.reason)
    lines = [f"{'refused' if finding.blocking else 'debt'}: {sentence}"]
    if finding.reason and sentence != finding.reason:
        lines.append(f"because: {finding.reason}")
    lines += [f"why: {m}" for m in finding.missing]
    if finding.path:
        lines.append(f"in: {finding.path}" + (f":{finding.line}" if finding.line else ""))
    lines += [f"fix: {row['fix']}"] if "fix" in row else []
    return [*lines, f"next: {finding.action or 'recipe status'}"]


_BANNED = (
    ("recipe.placeholder", re.compile(r"M2C_"), "M2C_ text", "placeholder"),
    ("recipe.perm", re.compile(r"PERM_"), "PERM_ marks", "perm"),
    (
        "recipe.cast",
        re.compile(
            r"(?<!sizeof)\(\s*(?:const\s+)?(?:struct\s+|union\s+)?\w+(?:\s+\w+)*\s*\*+\s*\)\s*[\w&(*-]|\(\s*[\w\s*]+\(\s*\*+\s*\)\s*\([^()]*\)\s*\)"
        ),
        "a cast to another type or view",
        "cast",
    ),
)
_CAST = re.compile(r"(?<!sizeof)\(\s*(?:const\s+)?(struct\s+|union\s+)?([A-Za-z_]\w*)\s*\)\s*[\w&(*\"'-]")
_VALUE = re.compile(r"[suf]\d+|vu\d+|int|char|short|long|float|double|unsigned|signed|void|size_t")


def _indirect(name: str, declared: Mapping[str, tuple[str, int, str]], text: str) -> bool:
    """Whether a typedef name stands for a pointer, a function pointer, a struct or a union."""
    pointer = rf"typedef\b.*(?:\(\s*\*\s*{name}\s*\)|(?:\*|\b(?:struct|union)\b).*\b{name}\s*(?:\[[^\]]*\])?\s*;?\s*$)"
    found = re.findall(rf"typedef\b[^;]*\b{name}\b[^;]*;", text)
    return any(re.match(pointer, d, re.S) for d in (declared.get(name, ("", 0, ""))[2], *found))


def _cast(line: str, declared: Mapping[str, tuple[str, int, str]], text: str) -> bool:
    return any(m[1] or (not _VALUE.fullmatch(m[2]) and _indirect(m[2], declared, text)) for m in _CAST.finditer(line))


def _neighbours(text: str, declared: Mapping[str, tuple[str, int, str]]) -> dict[int, str]:
    """Line number -> the scalar global whose address a pointer holds and that pointer is then indexed or offset."""
    held = {}
    for pointer, name in re.findall(r"\b(\w+)\s*=\s*&\s*(\w+)\s*;", text):
        found = declared.get(name, ("", 0, ""))[2] or "".join(re.findall(rf"extern[^;]*\b{name}\b[^;]*;", text))
        if found and "[" not in found and "struct" not in found:
            held[pointer] = name
    return {
        number: name
        for number, line in enumerate(text.splitlines(), 1)
        for pointer, name in held.items()
        if re.search(rf"\b{pointer}\s*\[\s*(?!0\s*\])[^\]]+\]|\*\s*\(\s*{pointer}\s*[+-]\s*(?!0\b)", line)
    }


_LOCAL = re.compile(
    r"^[ \t]+(?!return\b|goto\b|else\b|case\b|extern\b|static\b|typedef\b)(?:\w+[ \t]+)+\**[ \t]*(\w+)"
    r"[ \t]*(?:\[[^\]]*\])?[ \t]*(?:=[^;=][^;]*)?;",
    re.M,
)


def _unused(text: str) -> dict[int, str]:
    """Line number -> a local whose name is only ever assigned after its declaration."""
    plain = re.sub(r"\b(?:struct|union|enum)\b[^;{}]*\{[^{}]*\}", lambda m: re.sub(r"[^\n]", " ", m[0]), text)
    found = {}
    for local in _LOCAL.finditer(plain):
        name = local[1]
        rest = [m for m in re.finditer(rf"\b{name}\b", plain) if m.start() != local.start(1)]
        write = r"\s*(?:=(?!=)|[-+*/|&^]=|<<=|>>=|\+\+|--)"
        if all(re.match(write, plain[m.end() :]) and not re.search(r"[*&.>]\s*$", plain[: m.start()]) for m in rest):
            found[plain.count("\n", 0, local.start()) + 1] = name
    return found


def banned(
    text: str, declared: Mapping[str, tuple[str, int, str]], old: str = ""
) -> list[tuple[str, str, str, int, str]]:
    """(key, what, why, line number, line) per candidate line holding text no landing may have, minus `old`'s lines."""
    lines = text.splitlines()
    found = (
        [
            (key, what, advice(why), number, line.strip())
            for number, line in enumerate(text.splitlines(), 1)
            for key, pattern, what, why in _BANNED
            if pattern.search(line) or (key == "recipe.cast" and _cast(line, declared, text))
        ]
        + [
            (
                "recipe.neighbour",
                f"pointer arithmetic on the address of {name}",
                advice("neighbour"),
                number,
                text.splitlines()[number - 1].strip(),
            )
            for number, name in _neighbours(text, declared).items()
        ]
        + [
            ("recipe.unused", f"an unused local {name}", advice("unused"), number, lines[number - 1].strip())
            for number, name in _unused(text).items()
        ]
    )
    kept = {line.strip() for line in old.splitlines()}
    return [hit for hit in found if hit[4] not in kept]


def held(snapshot: Snapshot, text: str, member: str) -> list[tuple[str, str, str, int, str]]:
    """`banned` for a candidate of `member`, leaving out what its landed source already holds."""
    unit = layout.unit_of(snapshot, member)
    old = index.source(snapshot, unit.path) if unit else ""
    return banned(text, index.get(snapshot).declarations, old)


def classes() -> dict[str, dict[str, str]]:
    return {name: dict(row) for name, row in table("knowledge/errors.toml").get("class", {}).items()}


def classify(
    missing: Sequence[str], findings: Sequence[Finding] = (), context: Mapping[str, str] | None = None
) -> Translated:
    """The first class matching `missing` or a finding key, else `unknown`; templates fill from the regex groups."""
    keys = {f.key for f in findings}
    texts: list[str | None] = [*missing, *(f.reason for f in findings)] or [None]
    for name, row in classes().items():
        pattern = re.compile(row["match"]) if "match" in row else None
        for text in texts:
            found = pattern.search(text) if pattern and text is not None else None
            if found is None and not (row.get("key") and row["key"] in keys):
                continue
            line = _LINE.search(text) if text else None
            values = defaultdict(str, {**(context or {}), **(found.groupdict() if found else {})})
            values["line"] = line[1] if line else "0"
            what, fix = row["what"].format_map(values), row["fix"].format_map(values)
            return Translated(name, int(values["line"]), what, fix, row.get("autofix", ""), values["name"])
    first = next((m for m in missing), next((f.reason for f in findings), "no message"))
    line = _LINE.search(first)
    return Translated("unknown", int(line[1]) if line else 0, first.strip(), _UNKNOWN_FIX)


def _macro_header(snapshot: Snapshot, name: str) -> str:
    """The include line for a macro or standard name: the project header that defines it, else the standard one."""
    for path in headers.sources(snapshot):
        if re.search(rf"^#\s*define\s+{re.escape(name)}\b", snapshot.read(path).decode(errors="replace"), re.M):
            return f'"{path.removeprefix("include/")}"'
    return f"<{_STANDARD[name]}>" if name in _STANDARD else ""


def _type_header(snapshot: Snapshot, name: str) -> str:
    """The include line for a struct or typedef name: the project header that declares it, else ""."""
    for identity in index.get(snapshot).identities:
        if name in identity.names or f"struct {name}" in identity.names:
            paths = sorted({v.path for v in identity.views if v.path.endswith(".h")})
            return f'#include "{paths[0].removeprefix("include/")}"' if paths else ""
    return ""


def translate(snapshot: Snapshot, text: str, missing: Sequence[str], findings: Sequence[Finding]) -> Translated:
    """`classify`, with the header that declares the named symbol and the candidate line that repeats it."""
    name = classify(missing, findings).name
    line = rf"^\s*(?:extern\s+)?[\w\s*]+\b{re.escape(name)}\s*\([^)]*\)\s*;"
    at = next((n for n, row in enumerate(text.splitlines(), 1) if name and re.match(line, row)), 0)
    row = types.load(snapshot)
    decl = (row["global"].get(name) or {}).get("declaration") or (
        row["function"][name]["signature"] + ";" if name in row["function"] else ""
    )
    context = {
        "decl": f"`{decl}`" if decl else f"an extern declaration of {name}",
        "typed": "the type is from types.toml" if decl else f"its type is unknown: find it with `recipe look {name}`",
    }
    if not decl and (include := _macro_header(snapshot, name)):
        context |= {
            "decl": f"`#include {include}`",
            "typed": f"{name} is a macro, not a variable: never declare it extern",
        }
    if not decl and name and (kind := _type_header(snapshot, name)):
        context |= {"decl": f"`{kind}`", "typed": f"{name} is a type, not a variable: never declare it extern"}
    found = classify(missing, findings, context)
    number = found.line
    if "M2C_" in text and number > (offset := len(resource("tables/m2c_macros.h").decode().splitlines())):
        number -= offset  # the measured file has the macro block in front
    if found.cls in ("decl-conflict", "header-disagreement") and at:
        number = at  # the candidate's own declaration of the name
    return replace(found, line=number)


def _drop_definition(snapshot: Snapshot, text: str, translated: Translated) -> tuple[str, str]:
    name = translated.name
    known = index.get(snapshot).declarations.get(name)
    if not name or known is None:
        return text, ""
    block = re.compile(rf"^typedef\s+struct[^{{;]*\{{.*?\}}\s*{re.escape(name)}\s*;\n", re.S | re.M)
    prototype = re.compile(rf"^[^\n{{}};]*\b{re.escape(name)}\s*\([^)]*\)\s*;\n", re.M)
    for pattern in (block, prototype):
        fixed, count = pattern.subn("", text, count=1)
        if count:
            return fixed, f"Removed local {name} ({known[0]}:{known[1]} defines it)"
    return text, ""


_FIXES: dict[str, Callable[[Snapshot, str, Translated], tuple[str, str]]] = {
    "drop-local-definition": _drop_definition,
}


def autofix(snapshot: Snapshot, text: str, translated: Translated) -> tuple[str, str]:
    fix = _FIXES.get(translated.autofix)
    return fix(snapshot, text, translated) if fix else (text, "")
