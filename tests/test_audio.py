"""The Nintendo audio bank reader and ADPCM decoder: a synthetic image, and the real ROM when one is given."""

import os
import struct
from pathlib import Path

import pytest

from recipemaker import audio

ORIGIN = audio.POINTER_ORIGIN


def test_clamp_and_frame_decode_of_a_silent_frame():
    book = audio.Book(1, 1, (((0,) * 8,),))
    assert audio.decode(bytes([0x00]) + bytes(8), book) == [0] * 16


def test_residuals_scale_and_sign():
    book = audio.Book(1, 1, (((0,) * 8,),))
    frame = bytes([0x20, 0x1F]) + bytes(7)  # scale 2, residuals +1 and -1
    out = audio.decode(frame, book)
    assert out[:2] == [1 << 2, -(1 << 2)]


def test_reencode_returns_the_original_bytes_for_unclamped_frames():
    book = audio.Book(2, 1, (((0,) * 7 + (2048,), (0,) * 7 + (1024,)),))
    data = bytes(
        [0x30, 0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0, 0x20, 0x43, 0x65, 0x87, 0xA9, 0xCB, 0xED, 0x0F, 0x10]
    )
    pcm = audio.decode(data, book)
    rebuilt, free = audio.reencode(pcm, data, book)
    assert rebuilt == data and free == []


def test_wav_header_is_riff_pcm_mono():
    wav = audio.wav([1, -1, 2], 22050)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) == 44 + 6
    assert struct.unpack_from("<HHI", wav, 20) == (1, 1, 22050)


def test_read_waves_refuses_a_bad_revision():
    ctl = struct.pack(">II", 1, ORIGIN + 12) + bytes(4) + struct.pack(">HH", 0x1234, 1)
    with pytest.raises(audio.BankError):
        audio.read_waves(ctl, 100)


def _graph() -> bytes:
    """One bank file, one bank, one instrument, one sound with envelope, key map, wave, loop and an order-2 book."""
    at = {"files": 0, "file": 12, "bank": 20, "instrument": 36, "sound": 56, "env": 72, "key": 88, "wave": 96}
    at |= {"loop": 120, "book": 168}
    image = bytearray(168 + 8 + 2 * 8 * 2)
    struct.pack_into(">II", image, 0, 1, ORIGIN + at["file"])
    struct.pack_into(">HHI", image, at["file"], audio.BANK_REVISION, 1, ORIGIN + at["bank"])
    struct.pack_into(">HBBIII", image, at["bank"], 1, 0, 0, 22050, 0, ORIGIN + at["instrument"])
    struct.pack_into(">B13xHI", image, at["instrument"], 127, 1, ORIGIN + at["sound"])
    struct.pack_into(">III", image, at["sound"], ORIGIN + at["env"], ORIGIN + at["key"], ORIGIN + at["wave"])
    struct.pack_into(">I", image, at["env"], 1)
    struct.pack_into(">BBBB", image, at["key"], 0, 127, 0, 127)
    struct.pack_into(">IIB3xII", image, at["wave"], 0, 9, 0, ORIGIN + at["loop"], ORIGIN + at["book"])
    struct.pack_into(">III", image, at["loop"], 0, 16, 0)
    struct.pack_into(">II", image, at["book"], 2, 1)
    struct.pack_into(">h", image, at["book"] + 8, 2048)
    return bytes(image)


def test_the_graph_explains_every_nonzero_byte_of_the_control_image():
    image = _graph()
    kinds = [kind for kind, _at, _size in audio.structures(image)]
    assert kinds == ["files", "bankfile", "bank", "instrument", "sound", "envelope", "keymap", "wave", "loop", "book"]
    seen = audio.explain(image)
    assert seen["unreached"] == [] and seen["records"] + seen["zero_between"] == len(image)
    stray = bytearray(image)
    stray[94] = 1  # inside the two bytes between the key map and the wave table
    assert audio.explain(bytes(stray))["unreached"] == [[94, 95]]


SEQUENCES = (  # the nine compressed sequences of us-rev1
    (0x507FF8, 0x508408),
    (0x508408, 0x508FC0),
    (0x508FC0, 0x50A060),
    (0x50A060, 0x50B080),
    (0x50B080, 0x50BE50),
    (0x50BE50, 0x50E5C8),
    (0x50E5C8, 0x50E708),
    (0x50E708, 0x50EBC0),
    (0x50EBC0, 0x50F768),
)


def _sequence(track: bytes) -> bytes:
    header = struct.pack(">If16Ii", 60, 1.0, *([audio.TRACKS * 4 + 4] + [0] * (audio.TRACKS - 1)), 480)
    return header + track + bytes(3)


def test_a_sequence_parses_to_its_end_meta_through_running_status_and_back_references():
    # tempo, program change, a note, a running-status note, a back reference replaying that note, the end meta
    track = bytes.fromhex("00ff5107a12000c00d00903c6460103e6460fe00040400ff2f")
    receipt = audio.sequence(_sequence(track))
    start = audio.SEQUENCE_PREFIX + audio.TRACKS * 4 + 4
    assert receipt["tracks"] == [[start, start + len(track), 5]] and receipt["division"] == 480
    assert receipt["unreached"] == [] and receipt["zero_between"] == 3
    with pytest.raises(audio.BankError, match="meta"):
        audio.sequence(_sequence(bytes.fromhex("00ff7700")))


@pytest.mark.skipif("RECIPE_TEST_ROM" not in os.environ, reason="needs RECIPE_TEST_ROM (Rage Wars us-rev1)")
def test_rage_wars_banks_decode_and_rebuild():
    rom = Path(os.environ["RECIPE_TEST_ROM"]).read_bytes()
    receipt = audio.prove(rom[2667108:2725136], rom[2725136:5258424])
    assert receipt["waves"] == 251 and receipt["pcm_identical"] == 251
    assert receipt["bytes_identical"] >= 250
    assert receipt["control"]["unreached"] == []
    for start, end in SEQUENCES:
        assert audio.sequence(rom[start:end])["unreached"] == []
