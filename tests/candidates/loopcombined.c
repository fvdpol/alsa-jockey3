// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Candidate: the original per-bit codec with the loop fused.
 *
 * This is the shape the driver used before the lookup-table variants landed:
 * the same bit-by-bit scatter/gather, but with a single loop over the eight
 * bit positions so the compiler gets one straight-line body to schedule and
 * vectorize rather than several nested loops.
 *
 * It is kept as a candidate because it is the baseline the SWAR variants are
 * measured against, and because it is the only implementation here that a
 * reader can check against the format description by eye.
 *
 * Historical measurements, from the notes in the harness this replaced:
 * on x86_64 it encoded 10-25% faster and decoded 2-10% faster than the
 * unfused original - a modest win, later dwarfed by the 4-10x from the
 * bit-spread tables.
 *
 * (C) 2026 Frank van de Pol <fvdpol@gmail.com>
 */

#include "candidate.h"

void loopcombined_init(void)
{
	/* No lookup tables to build. */
}

static void loopcombined_encode_frame(u8 *dest, const u8 *src)
{
	for (int i = 0; i < 8; i++) {
		/* First 24 bytes: ALSA channels 1 and 3 */
		dest[i]      = (((src[2] >> (7 - i)) & 1) << 0) |
			       (((src[8] >> (7 - i)) & 1) << 1);
		dest[8 + i]  = (((src[1] >> (7 - i)) & 1) << 0) |
			       (((src[7] >> (7 - i)) & 1) << 1);
		dest[16 + i] = (((src[0] >> (7 - i)) & 1) << 0) |
			       (((src[6] >> (7 - i)) & 1) << 1);

		/* Second 24 bytes: ALSA channels 2 and 4 */
		dest[24 + i] = (((src[5] >> (7 - i)) & 1) << 0) |
			       (((src[11] >> (7 - i)) & 1) << 1);
		dest[32 + i] = (((src[4] >> (7 - i)) & 1) << 0) |
			       (((src[10] >> (7 - i)) & 1) << 1);
		dest[40 + i] = (((src[3] >> (7 - i)) & 1) << 0) |
			       (((src[9] >> (7 - i)) & 1) << 1);
	}
}

static void loopcombined_decode_frame(u8 *dest, const u8 *src)
{
	memset(dest, 0, PLOYTEC_CAPTURE_PCM_FRAME_SIZE);

	for (int i = 0; i < 8; i++) {
		/* Channel 1: bit 0 of wire bytes 0x00-0x17 */
		dest[0x00] |= (src[0x10 + i] & 0x01) << (7 - i);
		dest[0x01] |= (src[0x08 + i] & 0x01) << (7 - i);
		dest[0x02] |= (src[0x00 + i] & 0x01) << (7 - i);

		/* Channel 2: bit 0 of wire bytes 0x20-0x37 */
		dest[0x03] |= (src[0x30 + i] & 0x01) << (7 - i);
		dest[0x04] |= (src[0x28 + i] & 0x01) << (7 - i);
		dest[0x05] |= (src[0x20 + i] & 0x01) << (7 - i);

		/* Channel 3: bit 1 of wire bytes 0x00-0x17 */
		dest[0x06] |= ((src[0x10 + i] & 0x02) >> 1) << (7 - i);
		dest[0x07] |= ((src[0x08 + i] & 0x02) >> 1) << (7 - i);
		dest[0x08] |= ((src[0x00 + i] & 0x02) >> 1) << (7 - i);

		/* Channel 4: bit 1 of wire bytes 0x20-0x37 */
		dest[0x09] |= ((src[0x30 + i] & 0x02) >> 1) << (7 - i);
		dest[0x0A] |= ((src[0x28 + i] & 0x02) >> 1) << (7 - i);
		dest[0x0B] |= ((src[0x20 + i] & 0x02) >> 1) << (7 - i);

		/* Channel 5: bit 2 of wire bytes 0x00-0x17 */
		dest[0x0C] |= ((src[0x10 + i] & 0x04) >> 2) << (7 - i);
		dest[0x0D] |= ((src[0x08 + i] & 0x04) >> 2) << (7 - i);
		dest[0x0E] |= ((src[0x00 + i] & 0x04) >> 2) << (7 - i);

		/* Channel 6: bit 2 of wire bytes 0x20-0x37 */
		dest[0x0F] |= ((src[0x30 + i] & 0x04) >> 2) << (7 - i);
		dest[0x10] |= ((src[0x28 + i] & 0x04) >> 2) << (7 - i);
		dest[0x11] |= ((src[0x20 + i] & 0x04) >> 2) << (7 - i);
	}
}

PLOYTEC_CANDIDATE_FROM_FRAME(loopcombined, loopcombined_encode_frame,
			     loopcombined_decode_frame);
