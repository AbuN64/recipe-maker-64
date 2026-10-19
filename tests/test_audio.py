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


@pytest.mark.skipif("RECIPE_TEST_ROM" not in os.environ, reason="needs RECIPE_TEST_ROM (Rage Wars us-rev1)")
def test_rage_wars_banks_decode_and_rebuild():
    rom = Path(os.environ["RECIPE_TEST_ROM"]).read_bytes()
    receipt = audio.prove(rom[2667108:2725136], rom[2725136:5258424])
    assert receipt["waves"] == 251 and receipt["pcm_identical"] == 251
    assert receipt["bytes_identical"] >= 250
