"""Nintendo audio sample banks: read the bank graph, decode the ADPCM samples, prove the decode by re-encoding.

The control image is a table of bank files (a count, then a pointer and a sample-table base per file). Every pointer
inside it is an offset from the image start plus 0x00800000. A bank file holds the revision 0x4231 and its banks; a
bank holds instruments (and optionally a percussion instrument); an instrument holds sounds; a sound points at a wave
table: sample offset and length in the sample table, a type (0 = ADPCM), an optional loop record (start, end, count and
sixteen state words) and the predictor book (order, predictor count, then order x 8 signed 16-bit coefficients per
predictor). The format was read off the ROM bytes and checked against the sample table extents (every wave must lie
inside the table).

A sample is a run of 9-byte frames: a header byte (scale in the high nibble, predictor in the low one) and sixteen
signed 4-bit residuals. Each half of eight samples is the sum of the previous `order` samples and the earlier residuals
of the half, weighted by the book, plus the residual, in 11-bit fixed point, clamped to 16 bits.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from unbake.contracts import Json

POINTER_ORIGIN = 0x00800000
BANK_REVISION = 0x4231
FRAME_BYTES = 9
FRAME_SAMPLES = 16
HALF = 8
PCM_MIN, PCM_MAX = -32768, 32767
ADPCM = 0


@dataclass(frozen=True)
class Book:
    """The predictor coefficients: coefficients[predictor][row][column], each row eight wide."""

    order: int
    predictors: int
    coefficients: tuple[tuple[tuple[int, ...], ...], ...]


@dataclass(frozen=True)
class Loop:
    start: int
    end: int
    count: int
    state: tuple[int, ...]


@dataclass(frozen=True)
class Wave:
    """One wave table (an ADPCM sample), with every graph path that reaches it."""

    descriptor: int  # offset in the control image
    base: int  # offset of the sample in the sample table
    length: int  # bytes
    kind: int
    book: Book
    loop: Loop | None
    rate: int  # the owning bank's sample rate
    paths: tuple[str, ...]

    @property
    def frames(self) -> int:
        return self.length // FRAME_BYTES


class BankError(ValueError):
    """The control image or a wave does not have the shape the format requires."""


def _u32(image: bytes, at: int) -> int:
    if not 0 <= at <= len(image) - 4:
        raise BankError(f"word at {at:#x} lies outside the control image")
    return int(struct.unpack_from(">I", image, at)[0])


def _pointer(image: bytes, at: int, size: int) -> int:
    value = _u32(image, at)
    target = value - POINTER_ORIGIN
    if value < POINTER_ORIGIN or not 0 <= target <= len(image) - size:
        raise BankError(f"pointer {value:#x} at {at:#x} leaves the control image")
    return target


def read_book(image: bytes, at: int) -> Book:
    order, predictors = struct.unpack_from(">II", image, at)
    if not 0 < order <= 16 or not 0 < predictors <= 16:
        raise BankError(f"book at {at:#x} has order {order} and {predictors} predictors")
    count = order * predictors * HALF
    if at + 8 + count * 2 > len(image):
        raise BankError(f"book at {at:#x} runs past the control image")
    words = struct.unpack_from(f">{count}h", image, at + 8)
    rows = tuple(
        tuple(tuple(words[(p * order + r) * HALF : (p * order + r + 1) * HALF]) for r in range(order))
        for p in range(predictors)
    )
    return Book(order, predictors, rows)


def read_loop(image: bytes, at: int) -> Loop:
    start, end, count = struct.unpack_from(">III", image, at)
    return Loop(start, end, count, tuple(struct.unpack_from(">16h", image, at + 12)))


def read_waves(image: bytes, table_size: int) -> list[Wave]:
    """Every distinct wave table of the image, in descriptor order, with its graph paths ("file/bank/instrument/sound";
    instrument -1 is the percussion instrument). Refuses a wave outside the sample table or of a non-ADPCM type."""
    files = _u32(image, 0)
    if not 0 < files <= 32:
        raise BankError(f"{files} bank files")
    found: dict[int, tuple[int, list[str]]] = {}
    for file in range(files):
        header = _pointer(image, 4 + file * 8, 4)
        revision, banks = struct.unpack_from(">HH", image, header)
        if revision != BANK_REVISION or not 0 < banks <= 32:
            raise BankError(f"bank file {file} has revision {revision:#x} and {banks} banks")
        for bank in range(banks):
            at = _pointer(image, header + 4 + bank * 4, 12)
            instruments, _flags, _pad, rate = struct.unpack_from(">HBBI", image, at)
            if instruments > 4096:
                raise BankError(f"{instruments} instruments")
            order = list(range(instruments))
            if _u32(image, at + 8):
                order.insert(0, -1)
            for number in order:
                instrument = _pointer(image, at + 12 + number * 4, 16)
                sounds = struct.unpack_from(">H", image, instrument + 14)[0]
                for sound in range(sounds):
                    wave = _pointer(image, _pointer(image, instrument + 16 + sound * 4, 16) + 8, 20)
                    entry = found.setdefault(wave, (rate, []))
                    entry[1].append(f"{file}/{bank}/{number}/{sound}")
    waves = []
    for descriptor, (rate, paths) in sorted(found.items()):
        base, length, kind = struct.unpack_from(">IIB", image, descriptor)
        if kind != ADPCM:
            raise BankError(f"wave {descriptor:#x} has type {kind}")
        if base + length > table_size or length % FRAME_BYTES:
            raise BankError(f"wave {descriptor:#x} ({base:#x}+{length:#x}) does not fit the sample table")
        loop = _pointer(image, descriptor + 12, 44) if _u32(image, descriptor + 12) else None
        waves.append(
            Wave(
                descriptor,
                base,
                length,
                kind,
                read_book(image, _pointer(image, descriptor + 16, 8)),
                read_loop(image, loop) if loop is not None else None,
                rate,
                tuple(paths),
            )
        )
    return waves


def _clamp(value: int) -> int:
    return PCM_MIN if value < PCM_MIN else PCM_MAX if value > PCM_MAX else value


def _residuals(frame: bytes) -> list[int]:
    scale = frame[0] >> 4
    nibbles = [n for byte in frame[1:FRAME_BYTES] for n in (byte >> 4, byte & 15)]
    return [(n - 16 if n >= 8 else n) << scale for n in nibbles]


def _predict(book: Book, predictor: int, history: list[int], residual: list[int], i: int) -> int:
    """The filter sum of sample i of a half, before the residual and the 11-bit shift."""
    rows = book.coefficients[predictor]
    order = book.order
    total = sum(rows[k][i] * history[len(history) - order + k] for k in range(order))
    return total + sum(residual[k] * rows[order - 1][i - 1 - k] for k in range(i))


def decode(data: bytes, book: Book) -> list[int]:
    """Signed 16-bit samples of a whole number of frames, from a silent start."""
    history = [0] * max(book.order, FRAME_SAMPLES)
    out: list[int] = []
    for start in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES):
        frame = data[start : start + FRAME_BYTES]
        predictor = frame[0] & 15
        if predictor >= book.predictors:
            raise BankError(f"frame {start // FRAME_BYTES} names predictor {predictor} of {book.predictors}")
        residual = _residuals(frame)
        for half in range(2):
            e = residual[half * HALF : (half + 1) * HALF]
            block = [_clamp((_predict(book, predictor, history, e, i) >> 11) + e[i]) for i in range(HALF)]
            history = (history + block)[-FRAME_SAMPLES:]
            out += block
    return out


def reencode(pcm: list[int], data: bytes, book: Book) -> tuple[bytes, list[int]]:
    """Rebuild the ADPCM bytes from the samples, taking each frame's header (scale, predictor) from `data`.

    The residual of an unclamped sample is the sample minus the filter output, so the nibbles are determined. A frame
    holding a clamped sample admits several residuals; it is rebuilt with the smallest one that clamps to the same
    sample. Returns the bytes and the indexes of the frames that were not determined (a clamped sample)."""
    history = [0] * max(book.order, FRAME_SAMPLES)
    out = bytearray()
    undetermined: list[int] = []
    for number in range(len(pcm) // FRAME_SAMPLES):
        header = data[number * FRAME_BYTES]
        scale, predictor = header >> 4, header & 15
        nibbles: list[int] = []
        free = False
        for half in range(2):
            y = pcm[number * FRAME_SAMPLES + half * HALF : number * FRAME_SAMPLES + (half + 1) * HALF]
            e: list[int] = []
            for i in range(HALF):
                base = _predict(book, predictor, history, e, i) >> 11
                want = y[i] - base
                clamped = y[i] in (PCM_MIN, PCM_MAX)
                q = -((-want) >> scale) if y[i] == PCM_MAX else want >> scale  # a clamped top sample: round up
                q = max(-8, min(7, q))
                free = free or clamped
                nibbles.append(q & 15)
                e.append(q << scale)
            history = (history + y)[-FRAME_SAMPLES:]
        if free:
            undetermined.append(number)
        out.append(header)
        out += bytes(nibbles[i] << 4 | nibbles[i + 1] for i in range(0, FRAME_SAMPLES, 2))
    return bytes(out), undetermined


def wav(pcm: list[int], rate: int) -> bytes:
    """A mono 16-bit RIFF/WAVE file of the samples."""
    data = struct.pack(f"<{len(pcm)}h", *pcm)
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16)
    header = struct.pack("<4sI4s", b"RIFF", 36 + len(data), b"WAVE") + fmt + struct.pack("<4sI", b"data", len(data))
    return header + data


# sizes of the graph records: sound (envelope, key map and wave pointers, pan, volume, flags), envelope (three times,
# two volumes), key map (velocity and key ranges, base key, detune), wave table (base, length, type, flags, loop and
# book pointers), loop (start, end, count, sixteen state words)
_RECORD = {"sound": 16, "envelope": 16, "keymap": 6, "wave": 20, "loop": 44}


def structures(image: bytes) -> list[tuple[str, int, int]]:
    """Every record of the bank graph as (kind, start, size), each once, in offset order: the file table, bank file
    headers, banks, instruments, sounds, envelopes, key maps, wave tables, loops and books."""
    found: dict[int, tuple[str, int]] = {}
    files = _u32(image, 0)
    found[0] = ("files", 4 + 8 * files)
    for file in range(files):
        header = _pointer(image, 4 + file * 8, 4)
        banks = struct.unpack_from(">H", image, header + 2)[0]
        found[header] = ("bankfile", 4 + 4 * banks)
        for bank in range(banks):
            at = _pointer(image, header + 4 + bank * 4, 12)
            instruments = struct.unpack_from(">H", image, at)[0]
            found[at] = ("bank", 12 + 4 * instruments)
            slots = [at + 12 + i * 4 for i in range(instruments)] + ([at + 8] if _u32(image, at + 8) else [])
            for slot in slots:
                instrument = _pointer(image, slot, 16)
                sounds = struct.unpack_from(">H", image, instrument + 14)[0]
                found[instrument] = ("instrument", 16 + 4 * sounds)
                for number in range(sounds):
                    sound = _pointer(image, instrument + 16 + number * 4, _RECORD["sound"])
                    found[sound] = ("sound", _RECORD["sound"])
                    for kind, field in (("envelope", 0), ("keymap", 4), ("wave", 8)):
                        found[_pointer(image, sound + field, _RECORD[kind])] = (kind, _RECORD[kind])
                    wave = _pointer(image, sound + 8, _RECORD["wave"])
                    if _u32(image, wave + 12):
                        found[_pointer(image, wave + 12, _RECORD["loop"])] = ("loop", _RECORD["loop"])
                    book = _pointer(image, wave + 16, 8)
                    order, predictors = struct.unpack_from(">II", image, book)
                    found[book] = ("book", 8 + order * predictors * HALF * 2)
    return [(kind, at, size) for at, (kind, size) in sorted(found.items())]


def explain(image: bytes) -> Json:
    """How much of the control image the bank graph accounts for: the bytes inside its records, the zero bytes between
    them (alignment) and the nonzero ranges no record reaches. Nothing unreached means the graph is the whole image."""
    held = bytearray(len(image))
    for _kind, at, size in structures(image):
        held[at : at + size] = b"\1" * size
    unreached: list[list[int]] = []
    for at, byte in enumerate(image):
        if not held[at] and byte:
            if unreached and unreached[-1][1] == at:
                unreached[-1][1] = at + 1
            else:
                unreached.append([at, at + 1])
    inside = held.count(1)
    loose = sum(end - start for start, end in unreached)
    return {"bytes": len(image), "records": inside, "zero_between": len(image) - inside - loose, "unreached": unreached}


SEQUENCE_PREFIX = 8  # a word and a float the game keeps before the libultra compressed MIDI header
TRACKS = 16
_BLOCK = 0xFE  # a back reference (offset high, offset low, length) or, doubled, a literal 0xFE
_META_BYTES = {0x51: 3, 0x2E: 2, 0x2D: 6}  # tempo; loop start (number, 0xFF); loop end (counts, 32-bit offset)
_DATA_BYTES = {0x8: 2, 0x9: 2, 0xA: 2, 0xB: 2, 0xC: 1, 0xD: 1, 0xE: 2}


class _Track:
    """The byte reader of one compressed track: a back reference replays `length` earlier stored bytes, counted back
    from the byte after it by `offset + 4`."""

    def __init__(self, data: bytes, at: int) -> None:
        self.data, self.at, self.back, self.left = data, at, 0, 0

    def byte(self) -> int:
        data = self.data
        if self.left:
            self.left -= 1
            self.back += 1
            return data[self.back - 1]
        if not 0 <= self.at < len(data):
            raise BankError(f"sequence track runs past its data at {self.at:#x}")
        value = data[self.at]
        self.at += 1
        if value != _BLOCK:
            return value
        if data[self.at] == _BLOCK:
            self.at += 1
            return _BLOCK
        offset, length = data[self.at] << 8 | data[self.at + 1], data[self.at + 2]
        self.at += 3
        self.back = self.at - (offset + 4)
        if self.back < 0 or not length:
            raise BankError(f"sequence back reference at {self.at - 4:#x} leaves the track")
        self.left = length - 1
        self.back += 1
        return data[self.back - 1]

    def number(self) -> int:
        value = 0
        while True:
            byte = self.byte()
            value = value << 7 | (byte & 0x7F)
            if not byte & 0x80:
                return value


def _events(data: bytes, at: int) -> tuple[int, int]:
    """(end, events) of the track at `at`: delta times, running status, notes with a duration, the tempo and loop
    metas, ending at the end-of-track meta."""
    track, status, events = _Track(data, at), 0, 0
    while True:
        track.number()
        first = track.byte()
        if first & 0x80:
            status, first = first, -1
        if not status:
            raise BankError(f"sequence track at {at:#x} has data before any status")
        if status == 0xFF:
            kind = track.byte() if first < 0 else first
            if kind == 0x2F:
                return track.at, events
            if kind not in _META_BYTES:
                raise BankError(f"sequence meta {kind:#x} near {track.at:#x}")
            for _ in range(_META_BYTES[kind]):
                track.byte()
        else:
            if status >> 4 not in _DATA_BYTES:
                raise BankError(f"sequence status {status:#x} near {track.at:#x}")
            for _ in range(_DATA_BYTES[status >> 4] - (first >= 0)):
                track.byte()
            if status >> 4 == 0x9:
                track.number()  # the note's duration
        events += 1


def sequence(data: bytes) -> Json:
    """Parse one compressed sequence: the prefix, sixteen track offsets (from the prefix end, 0 for no track), the
    division, then every track to its end meta. The receipt holds each track's span and event count, the zero bytes
    after the last track, and the nonzero bytes no part reaches (none means the parse is the whole sequence)."""
    header = SEQUENCE_PREFIX + 4 * TRACKS + 4
    if len(data) < header:
        raise BankError("sequence shorter than its header")
    offsets = struct.unpack_from(f">{TRACKS}I", data, SEQUENCE_PREFIX)
    tracks = []
    for offset in offsets:
        if offset:
            end, events = _events(data, SEQUENCE_PREFIX + offset)
            tracks.append([SEQUENCE_PREFIX + offset, end, events])
    held = bytearray(len(data))
    held[:header] = b"\1" * header
    for start, end, _events_count in tracks:
        held[start:end] = b"\1" * (end - start)
    unreached = [at for at in range(len(data)) if not held[at] and data[at]]
    return {
        "bytes": len(data),
        "division": struct.unpack_from(">i", data, SEQUENCE_PREFIX + 4 * TRACKS)[0],
        "tracks": tracks,
        "zero_between": held.count(0) - len(unreached),
        "unreached": unreached,
    }


def prove(ctl: bytes, table: bytes) -> Json:
    """Decode every wave and rebuild its bytes: the receipt of what is exact, what is clamped and what tiles."""
    waves = read_waves(ctl, len(table))
    rows: list[Json] = []
    frames = clamped = 0
    for w in waves:
        data = table[w.base : w.base + w.length]
        pcm = decode(data, w.book)
        rebuilt, free = reencode(pcm, data, w.book)
        same = decode(rebuilt, w.book) == pcm
        identical = rebuilt == data
        frames += w.frames
        clamped += len(free)
        rows.append(
            {
                "descriptor": w.descriptor,
                "base": w.base,
                "length": w.length,
                "samples": len(pcm),
                "rate": w.rate,
                "paths": list(w.paths),
                "bytes_identical": identical,
                "pcm_identical": same,
                "clamped_frames": len(free),
            }
        )
    return {
        "control": explain(ctl),
        "waves": len(waves),
        "frames": frames,
        "samples": frames * FRAME_SAMPLES,
        "clamped_frames": clamped,
        "bytes_identical": sum(1 for r in rows if r["bytes_identical"]),
        "pcm_identical": sum(1 for r in rows if r["pcm_identical"]),
        "rows": rows,
    }
