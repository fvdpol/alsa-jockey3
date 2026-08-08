#!/usr/bin/env python3
#
# Independent model of the Ploytec bit-plane wire format.
#
# Objective:
#   Provide a third, independent implementation of the Ploytec playback
#   encoder and capture decoder, written from the *structure* of the wire
#   format rather than transcribed from the C driver. It serves as the oracle
#   for both the in-kernel KUnit suite and the user-space test bench, and as
#   the generator for the shared golden test vectors.
#
#   Independence is the whole point: if this were a line-by-line port of
#   ploytec_codec.c, a misunderstanding of the format would be reproduced
#   identically in the "oracle" and the test would prove nothing. So the bit
#   maps here are *derived* from a structural description (channel pairing,
#   plane ordering, bit assignment), and then checked against a literal
#   transcription of the driver's reference loops. Those two must agree; that
#   agreement is itself a test, run by main().
#
# (C) 2026 Frank van de Pol <fvdpol@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import sys

# --- Wire format geometry (mirrors ploytec_codec.h) -------------------------

PLAYBACK_FRAMES = 10            # PCM frames per USB OUT packet
PLAYBACK_FRAME_SIZE = 48        # wire bytes per playback frame
PLAYBACK_CHANNELS = 4
PLAYBACK_PCM_FRAME_SIZE = 12    # 4 ch * 3 bytes

CAPTURE_FRAMES = 8              # PCM frames per USB IN packet
CAPTURE_FRAME_SIZE = 64         # wire bytes per capture frame
CAPTURE_CHANNELS = 6
CAPTURE_PCM_FRAME_SIZE = 18     # 6 ch * 3 bytes

BYTES_PER_SAMPLE = 3            # S24_3LE
PLANE_SIZE = 8                  # one bit-plane spans 8 wire bytes

# Capture uses only 48 of the 64 wire bytes; these ranges are never read.
CAPTURE_UNUSED_RANGES = ((0x18, 0x20), (0x38, 0x40))


# --- Structural derivation of the bit maps ----------------------------------
#
# Both directions share one rule. A group of channels is written into a
# 24-byte block, split into three 8-byte planes. The planes are ordered by
# significance, most significant first, so sample byte b (0 = LSB, 2 = MSB of
# an S24_3LE sample) lives at plane offset:
#
#     plane_offset(b) = (2 - b) * 8
#
# Within a plane, wire byte i carries bit (7 - i) of the sample byte: MSB
# first. Which *bit* of the wire byte a channel occupies is its index within
# the group. That single rule generates both tables below.

def _plane_offset(sample_byte):
    """Offset of the plane holding sample byte `sample_byte` within a block."""
    return (BYTES_PER_SAMPLE - 1 - sample_byte) * PLANE_SIZE


def _build_encode_map():
    """
    Playback: 4 channels in two pairs.

    Pair 0 is (ch0, ch2) and occupies wire bytes 0..23; pair 1 is (ch1, ch3)
    and occupies 24..47. A channel's index within its pair selects the bit of
    the wire byte it lands in.

    Returns a list of (src_idx, dst_base, dst_bit) rows, each meaning:
        dest[dst_base + i] |= bit(7 - i) of src[src_idx], shifted to dst_bit
    """
    pairs = [(0, 2), (1, 3)]
    rows = []
    for pair_idx, channels in enumerate(pairs):
        block_base = pair_idx * (BYTES_PER_SAMPLE * PLANE_SIZE)
        for sample_byte in reversed(range(BYTES_PER_SAMPLE)):   # MSB plane first
            for dst_bit, channel in enumerate(channels):
                src_idx = channel * BYTES_PER_SAMPLE + sample_byte
                rows.append((src_idx, block_base + _plane_offset(sample_byte),
                             dst_bit))
    return tuple(rows)


def _build_decode_map():
    """
    Capture: 6 channels in two groups of three.

    Group 0 occupies wire bytes 0x00..0x17, group 1 occupies 0x20..0x37.
    Channels alternate between the groups (ch0, ch2, ch4 in group 0;
    ch1, ch3, ch5 in group 1), and a channel's index within its group selects
    which bit of the wire byte it occupies.

    Returns a list of (dst_idx, src_base, src_bit) rows, each meaning:
        dest[dst_idx] |= bit `src_bit` of src[src_base + i], placed at (7 - i)
    """
    rows = []
    for channel in range(CAPTURE_CHANNELS):
        group = channel % 2
        src_bit = channel // 2
        block_base = group * 0x20
        for sample_byte in range(BYTES_PER_SAMPLE):             # LSB byte first
            dst_idx = channel * BYTES_PER_SAMPLE + sample_byte
            rows.append((dst_idx, block_base + _plane_offset(sample_byte),
                         src_bit))
    return tuple(rows)


ENCODE_MAP = _build_encode_map()
DECODE_MAP = _build_decode_map()


# --- The model itself -------------------------------------------------------

def encode_frame(src):
    """Encode one 12-byte S24_3LE frame (4 ch) into a 48-byte wire frame."""
    if len(src) != PLAYBACK_PCM_FRAME_SIZE:
        raise ValueError(f"encode_frame needs {PLAYBACK_PCM_FRAME_SIZE} bytes, "
                         f"got {len(src)}")

    dest = bytearray(PLAYBACK_FRAME_SIZE)
    for src_idx, dst_base, dst_bit in ENCODE_MAP:
        value = src[src_idx]
        for i in range(PLANE_SIZE):
            bit = (value >> (7 - i)) & 1
            dest[dst_base + i] |= bit << dst_bit
    return bytes(dest)


def decode_frame(src):
    """Decode one 64-byte wire frame into an 18-byte S24_3LE frame (6 ch)."""
    if len(src) != CAPTURE_FRAME_SIZE:
        raise ValueError(f"decode_frame needs {CAPTURE_FRAME_SIZE} bytes, "
                         f"got {len(src)}")

    dest = bytearray(CAPTURE_PCM_FRAME_SIZE)
    for dst_idx, src_base, src_bit in DECODE_MAP:
        value = 0
        for i in range(PLANE_SIZE):
            bit = (src[src_base + i] >> src_bit) & 1
            value |= bit << (7 - i)
        dest[dst_idx] = value
    return bytes(dest)


def encode_batch(src, n_frames):
    """Encode `n_frames` consecutive playback frames."""
    out = bytearray()
    for f in range(n_frames):
        off = f * PLAYBACK_PCM_FRAME_SIZE
        out += encode_frame(src[off:off + PLAYBACK_PCM_FRAME_SIZE])
    return bytes(out)


def decode_batch(src, n_frames):
    """Decode `n_frames` consecutive capture frames."""
    out = bytearray()
    for f in range(n_frames):
        off = f * CAPTURE_FRAME_SIZE
        out += decode_frame(src[off:off + CAPTURE_FRAME_SIZE])
    return bytes(out)


# --- Cross-check against a literal transcription of the driver reference ----
#
# The functions below are deliberate, mechanical transcriptions of
# ploytec_encode_s24_3le() and ploytec_decode_s24_3le() from ploytec_codec.c.
# They exist only so main() can confirm that the structural derivation above
# describes the same format. They are NOT used by the model itself.

def _literal_encode_frame(src):
    dest = bytearray(48)
    for i in range(8):
        dest[i] = (((src[2] >> (7 - i)) & 1) << 0) | (((src[8] >> (7 - i)) & 1) << 1)
        dest[8 + i] = (((src[1] >> (7 - i)) & 1) << 0) | (((src[7] >> (7 - i)) & 1) << 1)
        dest[16 + i] = (((src[0] >> (7 - i)) & 1) << 0) | (((src[6] >> (7 - i)) & 1) << 1)
        dest[24 + i] = (((src[5] >> (7 - i)) & 1) << 0) | (((src[11] >> (7 - i)) & 1) << 1)
        dest[32 + i] = (((src[4] >> (7 - i)) & 1) << 0) | (((src[10] >> (7 - i)) & 1) << 1)
        dest[40 + i] = (((src[3] >> (7 - i)) & 1) << 0) | (((src[9] >> (7 - i)) & 1) << 1)
    return bytes(dest)


def _literal_decode_frame(src):
    dest = bytearray(18)
    for i in range(8):
        dest[0x00] |= (src[0x10 + i] & 0x01) << (7 - i)
        dest[0x01] |= (src[0x08 + i] & 0x01) << (7 - i)
        dest[0x02] |= (src[0x00 + i] & 0x01) << (7 - i)
        dest[0x03] |= (src[0x30 + i] & 0x01) << (7 - i)
        dest[0x04] |= (src[0x28 + i] & 0x01) << (7 - i)
        dest[0x05] |= (src[0x20 + i] & 0x01) << (7 - i)
        dest[0x06] |= ((src[0x10 + i] & 0x02) >> 1) << (7 - i)
        dest[0x07] |= ((src[0x08 + i] & 0x02) >> 1) << (7 - i)
        dest[0x08] |= ((src[0x00 + i] & 0x02) >> 1) << (7 - i)
        dest[0x09] |= ((src[0x30 + i] & 0x02) >> 1) << (7 - i)
        dest[0x0A] |= ((src[0x28 + i] & 0x02) >> 1) << (7 - i)
        dest[0x0B] |= ((src[0x20 + i] & 0x02) >> 1) << (7 - i)
        dest[0x0C] |= ((src[0x10 + i] & 0x04) >> 2) << (7 - i)
        dest[0x0D] |= ((src[0x08 + i] & 0x04) >> 2) << (7 - i)
        dest[0x0E] |= ((src[0x00 + i] & 0x04) >> 2) << (7 - i)
        dest[0x0F] |= ((src[0x30 + i] & 0x04) >> 2) << (7 - i)
        dest[0x10] |= ((src[0x28 + i] & 0x04) >> 2) << (7 - i)
        dest[0x11] |= ((src[0x20 + i] & 0x04) >> 2) << (7 - i)
    return bytes(dest)


# --- Deterministic PRNG -----------------------------------------------------
#
# xorshift64*, mirrored bit-for-bit by the C harness and the KUnit suite so
# that every implementation on every architecture is fed identical test data
# and any failure reproduces exactly.

class Xorshift64:
    MASK = (1 << 64) - 1

    def __init__(self, seed=0x0123456789ABCDEF):
        self.state = seed & self.MASK or 1

    def next_u64(self):
        x = self.state
        x ^= (x >> 12)
        x = (x ^ (x << 25)) & self.MASK
        x ^= (x >> 27)
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & self.MASK

    def bytes(self, n):
        out = bytearray()
        while len(out) < n:
            out += self.next_u64().to_bytes(8, "little")
        return bytes(out[:n])


# --- Self-test --------------------------------------------------------------

def _check_maps_against_literal():
    """The structural derivation must describe the same format as the C."""
    rng = Xorshift64()
    failures = 0

    for _ in range(2000):
        src = rng.bytes(PLAYBACK_PCM_FRAME_SIZE)
        if encode_frame(src) != _literal_encode_frame(src):
            failures += 1
            if failures < 4:
                print(f"encode mismatch for src={src.hex()}", file=sys.stderr)

    for _ in range(2000):
        src = rng.bytes(CAPTURE_FRAME_SIZE)
        if decode_frame(src) != _literal_decode_frame(src):
            failures += 1
            if failures < 4:
                print(f"decode mismatch for src={src.hex()}", file=sys.stderr)

    return failures


def _check_properties():
    """The format is a pure bit permutation; assert the properties we rely on."""
    rng = Xorshift64(0xFEEDFACECAFEBEEF)
    failures = 0

    def fail(msg):
        nonlocal failures
        failures += 1
        print(f"property failure: {msg}", file=sys.stderr)

    # Zero in, zero out.
    if encode_frame(bytes(PLAYBACK_PCM_FRAME_SIZE)) != bytes(PLAYBACK_FRAME_SIZE):
        fail("encode(0) != 0")
    if decode_frame(bytes(CAPTURE_FRAME_SIZE)) != bytes(CAPTURE_PCM_FRAME_SIZE):
        fail("decode(0) != 0")

    # GF(2) linearity: the map is a bit permutation, so it distributes over XOR.
    for _ in range(500):
        a = rng.bytes(PLAYBACK_PCM_FRAME_SIZE)
        b = rng.bytes(PLAYBACK_PCM_FRAME_SIZE)
        ab = bytes(x ^ y for x, y in zip(a, b))
        lhs = encode_frame(ab)
        rhs = bytes(x ^ y for x, y in zip(encode_frame(a), encode_frame(b)))
        if lhs != rhs:
            fail("encode is not GF(2)-linear")
            break

    for _ in range(500):
        a = rng.bytes(CAPTURE_FRAME_SIZE)
        b = rng.bytes(CAPTURE_FRAME_SIZE)
        ab = bytes(x ^ y for x, y in zip(a, b))
        lhs = decode_frame(ab)
        rhs = bytes(x ^ y for x, y in zip(decode_frame(a), decode_frame(b)))
        if lhs != rhs:
            fail("decode is not GF(2)-linear")
            break

    # Encode: every input bit reaches exactly one distinct output bit.
    seen = {}
    for byte_idx in range(PLAYBACK_PCM_FRAME_SIZE):
        for bit in range(8):
            src = bytearray(PLAYBACK_PCM_FRAME_SIZE)
            src[byte_idx] = 1 << bit
            out = encode_frame(bytes(src))
            positions = [(i, b) for i, v in enumerate(out)
                         for b in range(8) if v & (1 << b)]
            if len(positions) != 1:
                fail(f"encode one-hot src[{byte_idx}] bit {bit} set "
                     f"{len(positions)} output bits")
            elif positions[0] in seen:
                fail(f"encode collision at {positions[0]}")
            else:
                seen[positions[0]] = (byte_idx, bit)
    if len(seen) != PLAYBACK_PCM_FRAME_SIZE * 8:
        fail(f"encode permutation covers {len(seen)}/96 bits")

    # Encode: bits 2..7 of every wire byte are unused and must stay clear.
    all_ones = encode_frame(b"\xff" * PLAYBACK_PCM_FRAME_SIZE)
    if any(v & 0xFC for v in all_ones):
        fail("encode set reserved bits 2..7")

    # Decode: the unused wire bytes must not influence the result.
    base = bytearray(rng.bytes(CAPTURE_FRAME_SIZE))
    expect = decode_frame(bytes(base))
    for start, end in CAPTURE_UNUSED_RANGES:
        probe = bytearray(base)
        for i in range(start, end):
            probe[i] ^= 0xFF
        if decode_frame(bytes(probe)) != expect:
            fail(f"decode read unused wire bytes {start:#04x}..{end - 1:#04x}")

    # Decode: bits 3..7 of every wire byte are unused too.
    probe = bytes(v ^ 0xF8 for v in base)
    if decode_frame(probe) != expect:
        fail("decode read unused wire bits 3..7")

    return failures


def main():
    failures = _check_maps_against_literal()
    if failures == 0:
        print("structural bit maps match the driver reference transcription")

    failures += _check_properties()
    if failures == 0:
        print("format properties hold (linear, permutation, reserved bits clear)")
        print(f"encode map: {len(ENCODE_MAP)} rows, "
              f"decode map: {len(DECODE_MAP)} rows")
        return 0

    print(f"{failures} failure(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
