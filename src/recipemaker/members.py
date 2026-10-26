"""Evidence for the members a decompiler could not name (`unk1C`): what the tree does with each, read from every access
of the record in the project (the accesses typed by `access.Types`, so another record's `unk1C` is never counted).

  param CALLEE NAME   the member is passed as an argument whose parameter the prototype of CALLEE calls NAME
  stored NAME         a variable, parameter or member called NAME is stored into it
  loaded NAME         it is loaded into something called NAME"""

import re
from collections.abc import Mapping
from pathlib import Path

from unbake import api
from unbake.contracts import Json, Refusal

from recipemaker import access, keys, locals_, state, structure

UNKNOWN = re.compile(r"unk_?[0-9A-Fa-f]+")
_CHAIN = re.compile(
    r"(?:\([^()]*\)\s*)*&?\s*(?:\w+\s*(?:->|\.)\s*|\(\*?\w+\)\s*(?:->|\.)\s*)*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?"
)


def _statement(text: str, at: int) -> tuple[int, int]:
    lo = max(text.rfind(c, 0, at) for c in ";{}") + 1
    hi = text.find(";", at)
    return lo, len(text) if hi < 0 else hi


def _top_equal(stmt: str) -> int:
    depth = 0
    for i, c in enumerate(stmt):
        depth += (c in "([") - (c in ")]")
        if c == "=" and not depth and stmt[i - 1 : i] not in ("=", "!", "<", ">") and stmt[i + 1 : i + 2] != "=":
            return i
    return -1


def _tail_word(expr: str) -> str:
    """The name an expression ends in when it is a plain variable or member chain, else ''."""
    found = _CHAIN.fullmatch(expr.strip())
    return found[1] if found and locals_.named(found[1]) and not found[1].isdigit() else ""


def site_evidence(masked: str, at: int, end: int, protos: Mapping[str, list[list[str]]]) -> list[str]:
    """The evidence strings the access whose member word spans masked[at:end] gives."""
    lo, hi = _statement(masked, at)
    stmt = masked[lo:hi]
    p, out = at - lo, []
    eq = _top_equal(stmt)
    if eq >= 0:
        left, right = stmt[:eq], stmt[eq + 1 :]
        if p < eq and not re.search(r"[(\[]", left.rstrip()[-1:]) and (name := _tail_word(right)):
            out.append(f"stored {name}")
        elif (
            p > eq
            and stmt[end - lo :].strip() == ""
            and (name := _tail_word(left.split("(")[-1] if "(" in left else left))
        ):
            out.append(f"loaded {name}")
    depth, i, commas = 0, p, 0
    while i > 0:  # the call this access is an argument of: back to its unmatched open paren
        i -= 1
        c = stmt[i]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            commas += 1
    callee = re.search(r"([A-Za-z_]\w*)\s*$", stmt[:i]) if stmt[i : i + 1] == "(" else None
    if callee and callee[1] in protos:
        argument = locals_.split(stmt[i + 1 :])
        text = argument[commas] if commas < len(argument) else ""
        if text.strip().endswith(masked[at:end]):
            out += [f"param {callee[1]} {pr[commas]}" for pr in protos[callee[1]] if commas < len(pr) and pr[commas]]
    return list(dict.fromkeys(out))


def gather(
    texts: Mapping[str, str], types: access.Types, key: str, members: list[str], protos: locals_.Prototypes
) -> dict[str, list[str]]:
    """Per member the evidence over every typed access of KEY."""
    masked = {
        p: access.mask(t)
        for p, t in texts.items()
        if p.lower().endswith((".c", ".cpp")) and any(m in t for m in members)
    }
    sites = [s for s in types.sites({p: texts[p] for p in masked}, members) if s.owner == key]
    wanted = {c for s in sites for c in locals_.calls(masked[s.path][max(s.at - 400, 0) : s.at + 200])}
    known = protos.get(wanted)
    found: dict[str, list[str]] = {m: [] for m in members}
    for s in sites:
        word = re.match(r"\w+", masked[s.path][s.at :])
        if word:
            found[word[0]] += site_evidence(masked[s.path], s.at, s.at + len(word[0]), known)
    return {m: list(dict.fromkeys(v)) for m, v in found.items()}


def texts_of(root: Path) -> dict[str, str]:
    """Every C source and header of the project by path."""
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for d in ("src", "include")
        for p in (root / d).rglob("*")
        if p.is_file() and p.suffix.lower() in (".c", ".cpp", ".h")
    }


def record(texts: Mapping[str, str], types: access.Types, name: str) -> tuple[str, str, str, str]:
    """(key, header path, tag, alias) of the one record a name or header names, else a refusal."""
    key = types.key.get(re.sub(r"^(?:struct|union)\s+", "", name), "")
    if not key:
        raise state.stop(
            "recipe.name", f"no record is named {name}", action="recipe name next --step M --target STRUCT"
        )
    tag, alias = ("", key) if not key.startswith(("struct ", "union ")) else (key.split(" ", 1)[1], "")
    for path, text in texts.items():
        if path.endswith((".h", ".H")):
            for _, _, t, a in structure.bodies(text):
                if (alias and a == alias) or (tag and t == tag):
                    return key, path, t, a
    raise state.stop("recipe.name", f"no header defines {name}", action="recipe name next --step M")


class Fields:
    """Judges `field` rows of one submitted batch: the records and the evidence are read once."""

    def __init__(self, root: Path, loose: bool = False) -> None:
        self.root = root
        self.loose = loose  # the caller compiles after the rename (`settle`), so an access of no known type is no stop
        self.texts: dict[str, str] | None = None
        self.types: access.Types | None = None
        self.index = locals_.Prototypes(root)
        self.gathered: dict[str, dict[str, list[str]]] = {}
        self.accepted: set[tuple[str, str]] = set()
        self.queued: list[tuple[str, str]] = []  # (record as spelled, new name) of rows queued but not yet in the tree
        self.words: set[str] | None = None
        self.renamed: set[str] = set()  # the names type rows of this batch already took

    def load(self) -> None:
        """Read the project's records once, and count the queued rows' names as taken."""
        if self.texts is None:
            self.texts = texts_of(self.root)
            self.types = access.Types(self.texts)
            self.types.learn(self.texts)
        assert self.types is not None
        for struct, new in self.queued:
            self.accepted.add((self.types.key.get(re.sub(r"^(?:struct|union)\s+", "", struct), ""), new))
        self.queued = []

    def unknown_at(self, tag: str, offset: int) -> tuple[str, str]:
        """(member, "") of the unknown member of record TAG at byte OFFSET, or ("", why)."""
        self.load()
        assert self.types is not None
        key = self.types.key.get(re.sub(r"^(?:struct|union)\s+", "", tag), "")
        if not key:
            return "", f"no record is named {tag}"
        held = self.types.member.get(key, {})
        old = keys.member_old(held, offset)
        return (old, "") if old else ("", f"{key} has no unknown member at +{offset:#x}")

    def judge_type(self, row: Json) -> tuple[str, str, list[Json]]:
        """A `type` row (key `type:TAG`): the record named TAG, by tag or typedef name, takes the name everywhere the
        tree spells it. It is a pure type rename: no member, function or datum row carries it."""
        given = [row.get(k) for k in ("target", "name")]
        if not all(isinstance(x, str) and x for x in given) or not str(row.get("doc") or "").strip():
            return "shape", "a type row needs a key (type:TAG), name and doc", []
        old, new = (str(x) for x in given)
        if old == new or api.name_issues(new) or locals_.placeholder(new) or UNKNOWN.fullmatch(new):
            return "name", f"{new} is no usable type name for {old}", []
        self.load()
        assert self.types is not None and self.texts is not None
        if old not in self.types.key:
            if new in self.types.key:
                return "applied", f"the tree already holds the type {new}", []
            return "target", f"no record type is named {old}", []
        if self.words is None:
            self.words = {w for t in self.texts.values() for w in locals_.WORD.findall(access.mask(t))}
        if new in self.words or new in self.renamed:
            return "duplicate", f"{new} is already spelled in the tree (renameproof refuses it); pick another word", []
        self.renamed.add(new)
        return "", "", [{"op": "type", "subject": old, "target": new}]

    def judge(self, row: Json) -> tuple[str, str, list[Json]]:
        given = [row.get(k) for k in ("target", "name", "struct")]
        if not all(isinstance(x, str) and x for x in given) or not str(row.get("doc") or "").strip():
            return "shape", "a field row needs struct, target (member), name and doc", []
        old, new, struct = (str(x) for x in given)
        if not UNKNOWN.fullmatch(old):
            return "target", f"{old} is not an unknown member (unkNN)", []
        if (
            not re.fullmatch(r"[a-z][A-Za-z0-9_]*", new)
            or locals_.placeholder(new)
            or UNKNOWN.fullmatch(new)
            or api.name_issues(new)
        ):
            return "name", f"{new} is no lower-case member name", []
        self.load()
        assert self.types is not None and self.texts is not None
        try:
            key, path, tag, alias = record(self.texts, self.types, struct)
        except Refusal as refusal:
            return "target", refusal.findings[0].reason, []
        held = self.types.member.get(key, {})
        if old not in held and new in held:
            return "applied", f"{key} already has {new} for {old}", []
        if old not in held:
            return "target", f"{key} has no member {old}", []
        if self.words is None:
            self.words = {w for t in self.texts.values() for w in locals_.WORD.findall(access.mask(t))}
        if new in self.words:  # `unbake renameproof` refuses a new name that is already a code token anywhere
            return "duplicate", f"{new} is already spelled in the tree (renameproof refuses it); pick another word", []
        if new in held or (key, new) in self.accepted:
            return "duplicate", f"{key} already has a member {new}", []
        if key not in self.gathered:
            unknown = [m for m in held if UNKNOWN.fullmatch(m)]
            self.gathered[key] = gather(self.texts, self.types, key, unknown, self.index)
        have = self.gathered[key].get(old, [])
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
            return "evidence", f"{old} needs evidence: {', '.join(have) or 'none is in the tree'}", []
        for item in evidence:
            if re.sub(r"\s+\(.*$", "", item.strip()) not in have:
                return (
                    "evidence",
                    f"`{item}` is not an access of {key}.{old}; it has: {', '.join(have) or 'no evidence'}",
                    [],
                )
        self.accepted.add((key, new))
        return (
            "",
            "",
            [
                {"op": "field", "subject": old, "target": new, "path": path, "tag": tag, "alias": alias}
                | ({"loose": True} if self.loose else {})
            ],
        )
