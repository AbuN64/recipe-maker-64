"""Recover resource-defined graphics macros from word assignment pairs."""

import re

from unbake.contracts import Json

from recipemaker import table

_NUMBER = r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[0-7]*|[1-9][0-9]*)(?:[uU](?:[lL]{1,2})?|[lL]{1,2}[uU]?)?"
_OPAQUE = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
_POINTER = r"[A-Za-z_]\w*(?:\s*\[[^;{}]+?\]|\s*(?:->|\.)\s*[A-Za-z_]\w*)*?"
_ASSIGN = re.compile(rf"\s*({_POINTER})\s*(?:->|\.)\s*words\s*\.\s*w([01])\s*=\s*(.+);\s*", re.S)


def _strip(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for _index, char in enumerate(text):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                break
        if _index != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


def _integer(text: str) -> int | None:
    text = _strip(text)
    if not re.fullmatch(_NUMBER, text):
        return None
    text = re.sub(r"[uUlL]+$", "", text)
    base = next((b for prefix, b in (("0x", 16), ("0b", 2), ("0", 8)) if text.lower().startswith(prefix)), 10)
    return int(text, base)


def _word(text: str) -> tuple[int, list[tuple[str, int, int]]] | None:
    text = _strip(text)
    parts, start, depth = [], 0, 0
    for index, char in enumerate(text):
        depth += (char in "([") - (char in ")]")
        if depth < 0:
            return None
        if char == "|" and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    if depth:
        return None
    parts.append(text[start:])
    literal = 0
    terms: list[tuple[str, int, int]] = []
    for part in parts:
        part = _strip(part)
        value = _integer(part)
        if value is not None:
            literal |= value
            continue
        match = re.fullmatch(rf"\((.+)\s*&\s*({_NUMBER})\s*\)\s*<<\s*({_NUMBER})", part, re.S)
        if not match:
            match = re.fullmatch(rf"(.+)\s*&\s*({_NUMBER})", part, re.S)
            if match:
                expression, mask_text = match.groups()
                shift_text = "0"
            else:
                expression, mask_text, shift_text = part, "0xFFFFFFFF", "0"
        else:
            expression, mask_text, shift_text = match.groups()
        mask, shift = _integer(mask_text), _integer(shift_text)
        if (
            mask is None
            or shift is None
            or mask <= 0
            or mask & (mask + 1)
            or shift > 31
            or mask << shift > 0xFFFFFFFF
            or re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", expression)
        ):
            return None
        if any((m << s) & (mask << shift) for _, m, s in terms) or literal & (mask << shift):
            return None
        terms.append((_strip(expression), mask, shift))
    return (literal, terms) if literal <= 0xFFFFFFFF else None


def _format(value: int) -> str:
    return hex(value) if value > 9 else str(value)


def _value(field: Json, value: str, previous: dict[str, str]) -> str | None:
    number = _integer(value)
    xor, divisor, bias, scale = (
        field.get(k, default) for k, default in (("xor", 0), ("divisor", 1), ("bias", 0), ("scale", 1))
    )
    if "match" in field:
        return "" if number == field["match"] else None
    if number is not None:
        number ^= xor
        if number % divisor:
            return None
        number = (number // divisor + bias) * scale
        if "reverse" in field:
            number = field["reverse"] - number
        if "subtract" in field:
            other = _integer(previous[field["subtract"]])
            if other is None or number < other:
                return None
            number -= other
        choices = field.get("choices", {})
        if choices:
            found: str | None = choices.get(str(number), None if field.get("strict") else _format(number))
            return found
        flags = field.get("flags", {})
        if flags:
            remainder = number & ~sum(int(bit) for bit in flags)
            names = [name for bit, name in flags.items() if number & int(bit)]
            return " | ".join(names + ([_format(remainder)] if remainder else [])) or "0"
        return _format(number)
    if field.get("strict") or "reverse" in field or "subtract" in field:
        return None
    if divisor != 1:
        product = re.fullmatch(rf"(.+)\s*\*\s*({_NUMBER})", _strip(value), re.S)
        if product is None or _integer(product[2]) != divisor:
            return None
        value, divisor = _strip(product[1]), 1
    for op, amount in (
        ("^", xor),
        ("/", divisor if divisor != 1 else 0),
        ("+", bias),
        ("*", scale if scale != 1 else 0),
    ):
        if amount:
            value = f"({value}) {op} {_format(amount)}"
    return value


def _decode(row: Json, literal: int, terms: list[tuple[str, int, int]], word1: str) -> list[str] | None:
    low = _word(word1)
    if low is None:
        fields = [f for f in row["fields"] if f["word"] == 1 and "constant" not in f]
        pure = not re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", word1)
        if len(fields) != 1 or (fields[0]["shift"], fields[0]["bits"]) != (0, 32) or not pure:
            return None
        low = (0, [(_strip(word1), 0xFFFFFFFF, 0)])
    words, covered = [(literal, terms), low], [0xFF000000, 0]
    used: list[set[str]] = [set(), set()]
    values: dict[str, str] = {}
    for field in row["fields"]:
        if "constant" in field:
            values[field["name"]] = field["constant"]
            continue
        word, shift, bits = field["word"], field["shift"], field["bits"]
        mask = (1 << bits) - 1
        if shift + bits > 32 or covered[word] & (mask << shift):
            return None
        covered[word] |= mask << shift
        constant, computed = words[word]
        matches = [i for i, term in enumerate(computed) if term[1:] == (mask, shift)]
        if len(matches) > 1:
            return None
        if matches:
            index = matches[0]
            if index in used[word] or constant & (mask << shift):
                return None
            used[word].add(index)
            value = computed[index][0]
        else:
            value = _format((constant >> shift) & mask)
        value = _value(field, value, values)
        if value is None:
            return None
        if "match" not in field:
            values[field["name"]] = value
    if any(words[w][0] & ~covered[w] or len(used[w]) != len(words[w][1]) for w in (0, 1)):
        return None
    if row["macro"] == "gSPSetOtherMode":
        shift, length = _integer(values["shift"]), _integer(values["length"])
        if shift is None or length is None or shift < 0 or length < 1 or shift + length > 32:
            return None
    return [values[name] for name in row.get("args", values)]


def _rewrite(text: str) -> tuple[str, int, str]:
    """The rewritten text, its count, and the microcode lines it needs ahead of the headers."""
    encodings: dict[int, list[tuple[Json, str | None]]] = {}
    macros = (("f3dex2", "F3DEX_GBI_2"), ("f3dex", "F3DEX_GBI"), ("f3d", "F3D_GBI"))
    variant = next((name for name, macro in macros if re.search(rf"^\s*#\s*define\s+{macro}\b", text, re.M)), None)
    for row in table("tables/gbi.toml")["command"]:
        variants = row.get("variants", [{"opcode": row["opcode"], "microcode": row.get("microcode")}])
        for encoding in variants:
            encodings.setdefault(encoding["opcode"], []).append((row, encoding["microcode"]))
    masked = _OPAQUE.sub(lambda match: "".join("\n" if c == "\n" else " " for c in match[0]), text)
    statements, start, parens, braces = [], 0, 0, 0
    for boundary in re.finditer(r"[;{}()]", masked):
        end = boundary.end()
        char = boundary[0]
        if char in "()":
            parens += (char == "(") - (char == ")")
            continue
        if parens:
            continue
        if char == "{" and (
            braces or re.match(rf"\s*{_POINTER}\s*(?:->|\.)\s*words\s*\.\s*w[01]\s*=", masked[start:end])
        ):
            braces += 1
            continue
        if braces:
            braces -= char == "}"
            continue
        if boundary[0] == ";":
            match = _ASSIGN.fullmatch(masked[start:end])
            statements.append((start, end, match))
        else:
            statements.append((start, end, None))
        start = end
    derived, fits = variant is None, []
    if variant is None:
        evidence = set()
        for start, _end, match in statements:
            if match is None or match[2] != "0":
                continue
            word = _word(text[start + match.start(3) : start + match.end(3)])
            if word is not None:
                microcodes = {microcode for _row, microcode in encodings.get(word[0] >> 24, ())}
                fits += [{m for m in microcodes if m}] * (None not in microcodes or len(microcodes) > 1)
                if len(microcodes) == 1 and None not in microcodes:
                    evidence.update(microcodes)
        if len(evidence) == 1:
            variant = evidence.pop()
    opcodes = {
        opcode: [row for row, microcode in rows if variant is None or microcode in (None, variant)]
        for opcode, rows in encodings.items()
    }
    edits: list[tuple[int, int, str]] = []
    consumed: set[int] = set()
    count = 0
    for index, (start, end, match) in enumerate(statements):
        if index in consumed or match is None or match[2] != "0":
            continue
        word = _word(text[match.start(3) + start : match.end(3) + start].strip())
        if word is None:
            continue
        literal, terms = word
        pointer = re.sub(r"\s+", "", match[1])
        if re.search(r"\+\+|--|(?<![=!<>])=(?!=)|\b[A-Za-z_]\w*\s*\(", pointer):
            continue
        for other in range(index + 1, min(index + 4, len(statements))):
            second_start, second_end, second = statements[other]
            if masked[second_end - 1] in "{}":
                break
            if second is None or re.sub(r"\s+", "", second[1]) != pointer:
                continue
            if second[2] == "0" or other in consumed:
                break
            operand = text[second_start + second.start(3) : second_start + second.end(3)].strip()
            candidates = []
            for row in opcodes.get(literal >> 24, ()):
                values = _decode(row, literal, terms, operand)
                if values is not None:
                    priority = sum("match" in f for f in row["fields"])
                    candidates.append((priority, f"{row['macro']}({', '.join([match[1].strip(), *values])});"))
            if candidates:
                priority = max(p for p, _ in candidates)
                choices = {c for p, c in candidates if p == priority}
                if len(choices) == 1:
                    edits.extend(
                        ((start + match.start(1), end, choices.pop()), (second_start + second.start(1), second_end, ""))
                    )
                    consumed.add(other)
                    count += 1
            break
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    prelude, fit = "", set.intersection(*(f for f in fits if f)) if any(fits) else set()
    if count and derived and fit and "f3dex2" not in fit:  # the project default microcode is not this code's
        chosen = variant or ("f3d" if "f3d" in fit else sorted(fit)[0])
        prelude = "".join(f"#undef {m}\n" for n, m in macros if n != chosen) + f"#define {dict(macros)[chosen]}\n"
    return text, count, prelude


_STORE = re.compile(
    r"^[ \t]*(?:M2C_FIELD\((\w+), (?:s32|u32|void) \*+, (0[xX][0-9a-fA-F]+|\d+)\)|(\w+)->unk([0-9A-Fa-f]+)"
    r"|(\w+)->words\.w([01])) = ([^\n]*);[ \t]*\n",
    re.M,
)


def _slots(text: str, base: str) -> dict[int, list[tuple[int, int, str]]]:
    """The stores through `base` grouped by 8-byte slot: slot -> [(word, start of the statement, value)]."""
    slots: dict[int, list[tuple[int, int, str]]] = {}
    for match in _STORE.finditer(text):
        name = match[1] or match[3] or match[5]
        if name != base:
            continue
        offset = int(match[2], 0) if match[2] else int(match[4], 16) if match[4] else int(match[6]) * 4
        slots.setdefault(offset // 8, []).append((offset % 8 // 4, match.start(), match[7]))
    return slots


def _indexed(value: str) -> str:
    """A word loaded through a plain pointer as an index of it, for the decoder: M2C_FIELD(p, s32 *, 4) as p[1]."""
    value = re.sub(r"^\((?:void|s32|u32) ?\*?\) ?(?=\()", "", value) if "\n" not in value else value
    return re.sub(
        r"M2C_FIELD\((\w+), (?:s32|u32|void) \*+, (0[xX][0-9a-fA-F]+|\d+)\)",
        lambda m: f"{m[1]}[{int(m[2], 0) // 4}]" if int(m[2], 0) % 4 == 0 else m[0],
        value,
    )


def _runs(text: str) -> tuple[str, str] | None:
    """m2c's word stores through one copy of a display-list pointer as w0/w1 pairs on `ptr[i]`, and that pointer.

    The run is the whole of slots 0..n-1 each stored once as a pair and the pointer advanced to the end of it."""
    for load in re.finditer(r"^[ \t]*(\w+) = (\w+);[ \t]*\n", text, re.M):
        base, pointer = load.groups()
        advances = list(re.finditer(rf"^[ \t]*{pointer} = {base} \+ (0[xX][0-9a-fA-F]+|\d+);[ \t]*\n", text, re.M))
        slots = _slots(text, base)
        if not advances or not slots:
            continue
        count = max(int(a[1], 0) for a in advances) // 8
        if sorted(slots) != list(range(count)) or any(sorted(w for w, _, _ in v) != [0, 1] for v in slots.values()):
            continue
        if text.count(base) != len(advances) + sum(len(v) for v in slots.values()) + 2:
            continue  # the pointer copy is used for something else too
        first = min(start for v in slots.values() for _, start, _ in v)
        drop = [(m.start(), m.end()) for m in _STORE.finditer(text) if (m[1] or m[3] or m[5]) == base]
        drop += [(a.start(), a.end()) for a in advances] + [(load.start(), load.end())]
        drop += [(d.start(), d.end()) for d in re.finditer(rf"^[ \t]*[\w ]+ \*{base};[ \t]*\n", text, re.M)]
        pairs = "".join(
            f"    {pointer}[{i}].words.w{w} = {_indexed(v)};\n" for i in range(count) for w, _, v in sorted(slots[i])
        )
        out, position = "", 0
        for start, end in sorted(drop):
            out += text[position:start] + (pairs if start == first else "")
            position = end
        return out + text[position:], pointer
    return None


def _shape(text: str) -> str:
    """The shapes the hand matches share: a table base read as words is `u32 *` summed as `(u32)X[i] + (u32)X`,
    and a float global read twice by one call is read once into a local."""
    for name in set(re.findall(r"\b(\w+)\[\d+\] \+ \1\b", text)):
        text = re.sub(rf"\b{name}\[(\d+)\] \+ {name}\b", rf"(u32){name}[\1] + (u32){name}", text)
        text = text.replace(f"extern void *{name};", f"extern u32 *{name};")
    for name in dict.fromkeys(re.findall(r"extern f32 (\w+);", text)):
        if re.search(rf"^[^\n]*\b{name}\b[^\n]*\b{name}\b[^\n]*;$", text, re.M):
            text = re.sub(rf"\b{name}\b(?=[,)])", "temp_f", text)
            text = re.sub(r"(\) \{\n)", rf"\1    f32 temp_f = {name};\n", text, count=1)
    return text


def rewrite(text: str) -> tuple[str, int]:
    """GBI macros for the word assignments in `text`, a run of display-list stores as `gX(ptr++, ...)` calls."""
    return recover(text)[:2]


def recover(text: str) -> tuple[str, int, str]:
    """`rewrite` and the microcode lines the macros need ahead of the project headers."""
    run = _runs(text)
    if run is not None:
        shaped, count, prelude = _rewrite(run[0])
        arg = rf"\({re.escape(run[1])}\[\d+\]([,)])"
        if count and not re.search(rf"{re.escape(run[1])}\[\d+\]", re.sub(arg, "(", shaped)):
            return _shape(_indexed(re.sub(arg, lambda m: f"({run[1]}++{m[1]}", shaped))), count, prelude
    return _rewrite(text)
