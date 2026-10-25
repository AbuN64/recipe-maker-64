"""Stable row keys: a naming row names its target by what the ROM fixes (an address, a record and a byte offset, a
function and a slot), never by file, line or the name the tree holds today. The key is resolved against the current
tree each time it is used, so a row stays valid while other rows, file moves and matches land underneath it.

  fn:VER:VRAM8        a function          (`fn:80123456` = the names_from version)
  datum:VER:VRAM8     a global or static  (same short form)
  member:TAG+OFFSET   a record member by byte offset (`member:CPlayerPool_t+0x1C`)
  slot:VER:VRAM8:OLD  a local or parameter OLD of the function at VER:VRAM8"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from recipemaker import access, repair

_ADDRESS = re.compile(r"(fn|datum):(?:([A-Za-z0-9-]+):)?([0-9A-Fa-f]{8})")
_MEMBER = re.compile(r"member:([A-Za-z_][\w ]*?)\+(0[xX][0-9A-Fa-f]+|\d+)")
_SLOT = re.compile(r"slot:(?:([A-Za-z0-9-]+):)?([0-9A-Fa-f]{8}):([A-Za-z_]\w*)")
_NEUTRAL = re.compile(r"(func|D)_([0-9A-F]{8})(?:_(\w+))?")
_UNKNOWN = re.compile(r"unk_?([0-9A-Fa-f]+)")
KINDS = {"fn": "function", "datum": "global", "member": "field", "slot": "local"}


@dataclass(frozen=True)
class Key:
    kind: str  # fn | datum | member | slot
    version: str = ""
    vram: int = 0
    tag: str = ""
    offset: int = 0
    slot: str = ""

    def text(self) -> str:
        if self.kind == "member":
            return f"member:{self.tag}+{self.offset:#x}"
        head = f"{self.kind}:{self.version}:{self.vram:08X}"
        return f"{head}:{self.slot}" if self.kind == "slot" else head


def parse(text: str, first: str) -> Key | None:
    """The key a string spells (`first` = the names_from version for the short forms), None when it is no key."""
    text = text.strip()
    if m := _ADDRESS.fullmatch(text):
        return Key(m[1], m[2] or first, int(m[3], 16))
    if m := _MEMBER.fullmatch(text):
        return Key("member", tag=m[1].strip(), offset=int(m[2], 0))
    if m := _SLOT.fullmatch(text):
        return Key("slot", m[1] or first, int(m[2], 16), slot=m[3])
    return None


def of_target(kind: str, target: str, first: str, struct: str = "", function_key: str = "") -> str:
    """The key of a legacy row (kind + target): by the neutral spelling when the target is one, else ''."""
    m = _NEUTRAL.fullmatch(target)
    if kind == "function" and m and m[1] == "func":
        return Key("fn", (m[3] or first).replace("_", "-"), int(m[2], 16)).text()
    if kind in ("global", "static") and m and m[1] == "D":
        return Key("datum", (m[3] or first).replace("_", "-"), int(m[2], 16)).text()
    if kind == "field" and struct and (u := _UNKNOWN.fullmatch(target)):
        return Key("member", tag=re.sub(r"^(?:struct|union)\s+", "", struct), offset=int(u[1], 16)).text()
    if kind == "local" and function_key:
        return f"{function_key}:{target}".replace("fn:", "slot:", 1)
    return ""


def neutral(key: Key, first: str) -> str:
    """The neutral spelling a function or datum key has in its version's symbol table."""
    stem = "func" if key.kind == "fn" else "D"
    suffix = "" if key.version == first else "_" + key.version.replace("-", "_")
    return f"{stem}_{key.vram:08X}{suffix}"


def declared_line(root: Path, unit: str, function: str, old: str) -> int:
    """The 1-based line of the first spelling of OLD inside the definition of FUNCTION in UNIT, 0 when there is none."""
    path = root / unit
    if not unit or not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    span = repair.local_span(text, function)
    if span is None:
        return 0
    masked = access.mask(text)
    found = re.search(rf"\b{re.escape(old)}\b", masked[span[0] : span[1]])
    return masked.count("\n", 0, span[0] + found.start()) + 1 if found else 0


def member_old(held: Mapping[str, str], offset: int) -> str:
    """The unknown member (`unk1C`) of a record's members that sits at OFFSET, '' when none or more than one does."""
    found = [n for n in held if (m := _UNKNOWN.fullmatch(n)) and int(m[1], 16) == offset]
    return found[0] if len(found) == 1 else ""


Resolve = Callable[[Key], tuple[dict[str, object], str]]
