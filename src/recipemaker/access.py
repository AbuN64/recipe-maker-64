"""Typed member access: which `.name` and `->name` of the project's C belong to which record. A base expression is
typed from the declarations in scope (the function's parameters and locals, the file's data, the project's data) and the
member types of the records; one that cannot be typed is unknown, never guessed."""

import re
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from typing import NamedTuple

from recipemaker import cparse

_WORD = re.compile(r"[A-Za-z_]\w*")
SKIP = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.S)
_CAST = re.compile(r"\s*\(\s*(?:const\s+)?(?:struct\s+|union\s+)?(\w+)[\s*]*\)")
_CHAIN = re.compile(r"[\s*&\w.\[\]>()-]*")
_DECLARED = re.compile(r"\s*(\*+)?\s*([A-Za-z_]\w*)((?:\s*\[[^\]]*\])*)")
_MORE = re.compile(r"\s*,\s*(\*+)?\s*([A-Za-z_]\w*)((?:\s*\[[^\]]*\])*)")


class Site(NamedTuple):
    path: str
    at: int  # the offset of the member word
    owner: str  # the key of the record the base expression has, "" when it cannot be typed


def mask(text: str) -> str:
    """The text with comments, strings and characters blanked, offsets kept."""
    return SKIP.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), text)


def active(text: str) -> str:
    """The text with the `#elif` and `#else` branches blanked, offsets kept: braces then pair up as in one branch."""
    out, stack = [], []
    for line in text.splitlines(keepends=True):
        word = re.match(r"[ \t]*#[ \t]*(\w+)", line)
        kind = word[1] if word else ""
        if kind in ("if", "ifdef", "ifndef"):
            stack.append(False)
        elif kind in ("elif", "else") and stack:
            stack[-1] = True
        elif kind == "endif" and stack:
            stack.pop()
        out.append(re.sub(r"[^\n]", " ", line) if any(stack) and not kind else line)
    return "".join(out)


def _open(text: str, close: int) -> int:
    """The index of the bracket that the one at `close` closes, or -1."""
    opener, depth = {")": "(", "]": "["}[text[close]], 0
    for i in range(close, -1, -1):
        depth += (text[i] == text[close]) - (text[i] == opener)
        if depth == 0:
            return i
    return -1


class Types:
    """The records of some texts by every spelling (typedef name, tag, `struct tag`) and each member's record type."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.key: dict[str, str] = {}
        self.member: dict[str, dict[str, str]] = {}
        self.defined: Counter[str] = Counter()  # how many definitions each record has
        self.twice: set[tuple[str, str]] = set()  # (record, member) that more than one definition of the record spells
        self._decls: dict[str, dict[str, set[str]]] = {}  # per file, the record-typed data it declares at file level
        self._includes: dict[str, list[str]] = {}
        self._by_tail: dict[str, list[str]] = defaultdict(list)
        self._seen: dict[
            str, tuple[str, str, list[tuple[int, int, dict[str, str]]]]
        ] = {}  # a file's masked text, scopes
        typedefs: list[tuple[str, str]] = []
        anonymous: dict[str, list[str]] = {}  # a record to the untagged records written inside it as members
        for text in texts.values():
            structs, plain = cparse.scan(text)
            typedefs += plain
            for alias, tag, members, keyword in structs:
                key = alias or f"{keyword} {tag}"
                self.key.update({n: key for n in (alias, tag) if n})
                self.defined[key] += 1
                held = self.member.setdefault(key, {})
                self.twice |= {(key, name) for _, _, name, _ in members if name in held}
                held.update({name: kind for kind, _, name, _ in members if name})
                anonymous.setdefault(key, []).extend(kind for kind, _, name, _ in members if not name)
        for _ in range(3):  # the members of an untagged record inside are the owner's own
            for key, inner in anonymous.items():
                for kind in inner:
                    self.member[key] = {**self.member.get(self.key.get(kind, ""), {}), **self.member[key]}
        for _ in range(2):
            self.key.update({n: self.key[k] for n, k in typedefs if k in self.key and n not in self.key})

    def rename(self, key: str, old: str, new: str) -> None:
        if old in self.member.get(key, {}):
            self.member[key][new] = self.member[key].pop(old)

    def _declared(self, text: str) -> dict[str, set[str]]:
        """The variables of a record type that a piece of C declares (`T *a, b[2]`), name to record keys."""
        out: dict[str, set[str]] = defaultdict(set)
        for found in _WORD.finditer(text):
            if (key := self.key.get(found[0])) is None or text[
                max(found.start() - 3, 0) : found.start()
            ].rstrip().endswith((".", ">")):
                continue
            at = _DECLARED.match(text, found.end())
            while at:
                out[at[2]].add(key)
                at = _MORE.match(text, at.end())
        return out

    def _scopes(self, path: str, text: str) -> list[tuple[int, int, dict[str, str]]]:
        """(start, end, variables) of each function of `text`: the data its includes declare, the file's own, and the
        function's parameters and locals; a name that two records are declared for is left out."""
        text = active(text)
        every = cparse.spans(text)
        spans = [s for s in every if not s[2]]
        seen: dict[str, set[str]] = defaultdict(set)
        for held in self._visible(path):
            for name, keys in self._decls.get(held, {}).items():
                seen[name] |= keys
        for lo, hi, _ in spans:
            if text[lo:hi].rstrip()[-1:] != "}":
                for name, keys in self._declared(text[lo:hi]).items():
                    seen[name] |= keys
        shared = {n: next(iter(k)) for n, k in seen.items() if len(k) == 1}
        return [
            (lo, hi, {**shared, **{n: next(iter(k)) for n, k in self._declared(text[lo:hi]).items() if len(k) == 1}})
            for lo, hi, _ in spans
            if text[lo:hi].rstrip()[-1:] == "}"
        ] + [(lo, hi, shared) for lo, hi, directive in every if directive]  # a macro body sees the data only

    def _visible(self, path: str) -> set[str]:
        """The files whose declarations `path` sees: itself and everything it includes, transitively."""
        found, todo = {path}, [path]
        while todo:
            for spelled in self._includes.get(todo.pop(), ()):
                for held in self._by_tail.get(spelled.rsplit("/", 1)[-1], ()):
                    if held.endswith(spelled) and held not in found:
                        found.add(held)
                        todo.append(held)
        return found

    def learn(self, texts: Mapping[str, str]) -> None:
        """The data each file declares at file level, and the includes that connect the files."""
        for path, raw in texts.items():
            text = active(mask(raw))
            self._includes[path] = re.findall(r'#[ \t]*include[ \t]*[<"]([^>"]+)[>"]', raw)
            self._by_tail[path.rsplit("/", 1)[-1]].append(path)
            self._decls[path] = {}
            for lo, hi, directive in cparse.spans(text):
                if not directive and text[lo:hi].rstrip()[-1:] != "}":
                    for name, keys in self._declared(text[lo:hi]).items():
                        self._decls[path].setdefault(name, set()).update(keys)

    def _type(self, text: str, end: int, scope: Mapping[str, str]) -> str:
        """The record key of the expression that ends just before `end`."""
        i = len(text[:end].rstrip())
        last = text[i - 1 : i]
        if last == "]":
            return self._type(text, _open(text, i - 1), scope) if _open(text, i - 1) >= 0 else ""
        if last == ")":
            j = _open(text, i - 1)
            inner = text[j + 1 : i - 1] if j >= 0 else ""
            callee = re.search(r"(\w+)\s*$", text[:j]) if j >= 0 else None
            if callee:
                return scope.get(callee[1], "")  # a call: the type the function returns, when it is declared to
            if cast := _CAST.match(inner):
                return self.key.get(cast[1], "")
            if "=" in inner and not re.search(r"[=!<>]=|=[=]", inner):
                return self._type(text, j + 1 + inner.index("="), scope)  # `(p = q)->x`: the type of p
            return self._type(text, i - 1, scope) if j >= 0 and inner.strip() and _CHAIN.fullmatch(inner) else ""
        if not (last.isalnum() or last == "_"):
            return ""
        k = i
        while k > 0 and (text[k - 1].isalnum() or text[k - 1] == "_"):
            k -= 1
        word, before = text[k:i], text[:k].rstrip()
        if before.endswith(("->", ".")):
            owner = self._type(text, len(before) - (2 if before.endswith("->") else 1), scope)
            return self.key.get(self.member.get(owner, {}).get(word, ""), "")
        return scope.get(word, "")

    def sites(self, texts: Mapping[str, str], words: Collection[str]) -> list[Site]:
        """Every `.word` and `->word` of the texts with the record its base expression has."""
        wanted = re.compile(r"(?:->|\.)\s*(" + "|".join(map(re.escape, words)) + r")(?!\w)")
        out: list[Site] = []
        for path, raw in texts.items():
            if not any(w in raw for w in words):
                continue
            if path not in self._seen or self._seen[path][0] != raw:
                masked = mask(raw)
                self._seen[path] = (raw, masked, self._scopes(path, masked))
            _, text, scopes = self._seen[path]
            hits = list(wanted.finditer(text))
            for hit in hits:
                inside = next((s for lo, hi, s in scopes if lo <= hit.start() < hi), None)
                owner = self._type(text, hit.start(), inside) if inside is not None else ""
                out.append(Site(path, hit.start(1), owner))
        return out
