#!/usr/bin/env python3
#
# Generate the shared golden test vectors for the Ploytec codec.
#
# Objective:
#   Emit ploytec_codec_test_vectors.h, a checked-in C header consumed by both
#   the in-kernel KUnit suite and the user-space test bench. Having both read
#   the same vectors means the two test paths cannot silently disagree about
#   what the wire format is.
#
#   The vectors are produced by ploytec_model.py, which derives the format
#   structurally rather than copying the driver, so a vector mismatch is real
#   evidence of a bug rather than two copies of the same mistake agreeing.
#
#   The header must be regenerated and committed whenever the model changes:
#       ./genvectors.py && git diff ../ploytec_codec_test_vectors.h
#
# (C) 2026 Frank van de Pol <fvdpol@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import hashlib
import os
import sys

import ploytec_model as model

OUTPUT_NAME = "ploytec_codec_test_vectors.h"
RANDOM_VECTORS = 16
BYTES_PER_LINE = 12


def _sample(channels, values):
    """Build an S24_3LE PCM frame from per-channel 24-bit integer values."""
    frame = bytearray()
    for ch in range(channels):
        value = values[ch] & 0xFFFFFF
        frame += bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
    return bytes(frame)


def encode_inputs():
    """Named 12-byte playback inputs (4 channels x 3 bytes)."""
    n = model.PLAYBACK_PCM_FRAME_SIZE
    ch = model.PLAYBACK_CHANNELS
    cases = [
        ("zeros", bytes(n)),
        ("ones", b"\xff" * n),
        ("alternating_55", b"\x55" * n),
        ("alternating_aa", b"\xaa" * n),
        ("ramp", bytes(range(n))),
    ]

    # One channel saturated at a time: proves channels do not bleed into
    # each other's bit lanes.
    for c in range(ch):
        values = [0] * ch
        values[c] = 0xFFFFFF
        cases.append((f"channel{c}_full", _sample(ch, values)))

    # Sign bit only: the most significant bit of each channel, which lives in
    # the first plane of the block and is the bit most likely to be misplaced.
    for c in range(ch):
        values = [0] * ch
        values[c] = 0x800000
        cases.append((f"channel{c}_sign", _sample(ch, values)))

    # Distinct per-channel values, so a channel swap is immediately visible.
    cases.append(("distinct", _sample(ch, [0x123456, 0x789ABC, 0xDEF012, 0x345678])))

    rng = model.Xorshift64(0xA5A5A5A5DEADBEEF)
    for i in range(RANDOM_VECTORS):
        cases.append((f"random{i:02d}", rng.bytes(n)))

    return cases


def decode_inputs():
    """Named 64-byte capture inputs."""
    n = model.CAPTURE_FRAME_SIZE
    cases = [
        ("zeros", bytes(n)),
        ("ones", b"\xff" * n),
        ("ramp", bytes(i & 0xFF for i in range(n))),
    ]

    # Only one channel bit-lane populated at a time (bits 0, 1 and 2 select
    # the channel within a group).
    for bit in range(3):
        cases.append((f"bitplane{bit}_only", bytes([1 << bit] * n)))

    # Only one channel group populated: group 0 lives at 0x00..0x17,
    # group 1 at 0x20..0x37.
    for group, (start, end) in enumerate(((0x00, 0x18), (0x20, 0x38))):
        buf = bytearray(n)
        for i in range(start, end):
            buf[i] = 0xFF
        cases.append((f"group{group}_only", bytes(buf)))

    # Only the unused wire bytes populated. This one must decode to all
    # zeros; if it does not, the decoder is reading bytes it has no business
    # reading.
    buf = bytearray(n)
    for start, end in model.CAPTURE_UNUSED_RANGES:
        for i in range(start, end):
            buf[i] = 0xFF
    cases.append(("unused_bytes_only", bytes(buf)))

    # Only the unused *bits* (3..7) of every wire byte populated. Also decodes
    # to all zeros.
    cases.append(("unused_bits_only", bytes([0xF8] * n)))

    rng = model.Xorshift64(0x5A5A5A5AFEEDFACE)
    for i in range(RANDOM_VECTORS):
        cases.append((f"random{i:02d}", rng.bytes(n)))

    return cases


def format_bytes(data, indent):
    """Format a byte string as C initializer lines."""
    lines = []
    for off in range(0, len(data), BYTES_PER_LINE):
        chunk = data[off:off + BYTES_PER_LINE]
        lines.append(indent + " ".join(f"0x{b:02x}," for b in chunk))
    return "\n".join(lines)


def emit_vector_array(out, kind, cases, transform, src_len, dst_len):
    out.append(f"static const struct ploytec_{kind}_vector "
               f"ploytec_{kind}_vectors[] = {{")
    for name, src in cases:
        expect = transform(src)
        assert len(src) == src_len and len(expect) == dst_len
        out.append("\t{")
        out.append(f'\t\t.name = "{name}",')
        out.append("\t\t.src = {")
        out.append(format_bytes(src, "\t\t\t"))
        out.append("\t\t},")
        out.append("\t\t.expect = {")
        out.append(format_bytes(expect, "\t\t\t"))
        out.append("\t\t},")
        out.append("\t},")
    out.append("};")


def generate():
    enc = encode_inputs()
    dec = decode_inputs()

    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ploytec_model.py")
    with open(model_path, "rb") as f:
        model_hash = hashlib.sha256(f.read()).hexdigest()

    out = [
        "/* SPDX-License-Identifier: GPL-2.0-or-later */",
        "/*",
        " * Golden test vectors for the Ploytec bit-plane codec.",
        " *",
        " * GENERATED FILE - DO NOT EDIT.",
        " *",
        " * Produced by tests/codec/genvectors.py from tests/codec/ploytec_model.py, an",
        " * independent model of the wire format. Regenerate with:",
        " *",
        " *     cd tests/codec && ./genvectors.py",
        " *",
        f" * ploytec_model.py sha256: {model_hash}",
        " *",
        " * Shared by the KUnit suite (ploytec_codec_kunit.c) and the user-space",
        " * test bench, so both are held to an identical definition of the format.",
        " *",
        " * Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>",
        " */",
        "",
        "#ifndef PLOYTEC_CODEC_TEST_VECTORS_H",
        "#define PLOYTEC_CODEC_TEST_VECTORS_H",
        "",
        "struct ploytec_encode_vector {",
        "\tconst char *name;",
        f"\tu8 src[{model.PLAYBACK_PCM_FRAME_SIZE}];",
        f"\tu8 expect[{model.PLAYBACK_FRAME_SIZE}];",
        "};",
        "",
        "struct ploytec_decode_vector {",
        "\tconst char *name;",
        f"\tu8 src[{model.CAPTURE_FRAME_SIZE}];",
        f"\tu8 expect[{model.CAPTURE_PCM_FRAME_SIZE}];",
        "};",
        "",
    ]

    emit_vector_array(out, "encode", enc, model.encode_frame,
                      model.PLAYBACK_PCM_FRAME_SIZE, model.PLAYBACK_FRAME_SIZE)
    out.append("")
    emit_vector_array(out, "decode", dec, model.decode_frame,
                      model.CAPTURE_FRAME_SIZE, model.CAPTURE_PCM_FRAME_SIZE)

    out += [
        "",
        "#endif /* PLOYTEC_CODEC_TEST_VECTORS_H */",
        "",
    ]

    return "\n".join(out), len(enc), len(dec)


def main():
    text, n_enc, n_dec = generate()

    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                          OUTPUT_NAME)
    target = os.path.normpath(target)

    previous = None
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as f:
            previous = f.read()

    if previous == text:
        print(f"{OUTPUT_NAME} already up to date "
              f"({n_enc} encode, {n_dec} decode vectors)")
        return 0

    with open(target, "w", encoding="utf-8") as f:
        f.write(text)

    action = "updated" if previous is not None else "created"
    print(f"{action} {target} ({n_enc} encode, {n_dec} decode vectors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
