// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *   Ploytec PCM encoding functions for the S24_3LE format (3 bytes per sample)
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#include <linux/string.h>
#include "ploytec_codec.h"

/**
 * DOC: Why the wire format looks like this
 *
 * The "bit-plane" layout implemented below is not an arbitrary scramble; it is
 * a direct image of how the hardware moves audio. On the Jockey 3 the USB
 * device controller (NXP ISP1583) runs in split-bus mode, and the serial
 * data lines of the capture ADCs are wired straight onto the low bits of its
 * 16-bit DMA bus - one converter per bus bit. The controller samples the whole
 * bus once per bit clock and streams the result to the host, so every byte on
 * the wire is a snapshot of all the channel data lines at one instant.
 *
 * Decoding a frame is therefore a transpose:
 *
 * - The byte index is one bit-clock tick, most significant bit first: wire
 *   byte 0 carries bit 23 of the current sample, wire byte 23 carries bit 0.
 * - The bit position within each byte is one physical converter line, that is
 *   one channel (bit 0, bit 1, bit 2, ... for successive converters).
 * - The frame is two 24-byte halves, the left and right sub-frames of the
 *   underlying I2S frame. On capture an 8-byte gap sits between and after the
 *   halves: it is the unused tail of each 32-bit I2S slot (64-byte frame).
 *   Playback is generated on the byte-wide microprocessor bus instead, so its
 *   slots are packed to 24 bits with no gap (48-byte frame).
 *
 * Because the geometry follows from the I2S timing and the DMA bus width, it
 * is fixed regardless of how many converters are populated. That is why the
 * codec always works in 8-channel-wide slices and simply ignores the bit
 * positions that carry no converter (the Jockey 3 populates three capture
 * lines and two playback lines).
 *
 * This is a model inferred from the board schematic and the observed stream
 * rather than from vendor documentation, but it accounts for every bit of the
 * format the functions below implement.
 */

/*
 * This implementation of the Ploytec codec performs a bit gather/scatter
 * operation with minimal optimization. It mainly serves as a readable
 * definition of the wire format.
 */

/**
 * ploytec_encode_s24_3le() - Encode 4-channel S24_3LE to 48-byte Ploytec frame
 * @dest: 48-byte destination buffer
 * @src: 12-byte source buffer (4 channels * 3 bytes)
 *
 * Ploytec Bit-Plane Interleaving (Playback):
 * The firmware uses a non-standard "bit-plane" format where bits from different
 * channels are interleaved into the same byte.
 * - Each 48-byte frame carries one PCM frame: a single sample for each of
 *   the 4 channels, spread one bit per wire byte.
 * - Bytes 0-23: ALSA Channels 1 & 3
 * - Bytes 24-47: ALSA Channels 2 & 4
 * - Within each 24-byte block, bits are grouped by significance:
 *   - [0-7]: Most significant bits
 *   - [8-15]: Middle bits
 *   - [16-23]: Least significant bits
 * - bit 0 of each byte corresponds to the first channel in the pair.
 * - bit 1 of each byte corresponds to the second channel in the pair.
 */
static inline void ploytec_encode_s24_3le(u8 *dest, const u8 *src)
{
	int i;

	for (i = 0; i < 8; i++) {
		/* First 24 bytes: odd channels (ALSA Ch 1 & 3) */
		dest[i]      = (((src[2] >> (7 - i)) & 1) << 0) | (((src[8] >> (7 - i)) & 1) << 1);
		dest[8 + i]  = (((src[1] >> (7 - i)) & 1) << 0) | (((src[7] >> (7 - i)) & 1) << 1);
		dest[16 + i] = (((src[0] >> (7 - i)) & 1) << 0) | (((src[6] >> (7 - i)) & 1) << 1);

		/* Second 24 bytes: even channels (ALSA Ch 2 & 4) */
		dest[24 + i] = (((src[5] >> (7 - i)) & 1) << 0) | (((src[11] >> (7 - i)) & 1) << 1);
		dest[32 + i] = (((src[4] >> (7 - i)) & 1) << 0) | (((src[10] >> (7 - i)) & 1) << 1);
		dest[40 + i] = (((src[3] >> (7 - i)) & 1) << 0) | (((src[9]  >> (7 - i)) & 1) << 1);
	}
}

/**
 * ploytec_encode_batch() - Encode a number of 4-channel S24_3LE to 48-byte Ploytec frames
 * @dest: n_frames * 48-byte destination buffer (Ploytec playback frame)
 * @src: n_frames * 12-byte source buffer (4 channels * 3 bytes)
 * @n_frames: number of frames to process in this batch
 */
void ploytec_encode_batch(u8 *dest, const u8 *src, const int n_frames)
{
	for (int f = 0; f < n_frames; f++) {
		ploytec_encode_s24_3le(dest, src);
		dest += PLOYTEC_PLAYBACK_FRAME_SIZE;
		src += PLOYTEC_PLAYBACK_PCM_FRAME_SIZE;
	}
}
