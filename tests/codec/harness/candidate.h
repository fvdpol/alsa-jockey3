/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Contract for experimental codec implementations in candidates/.
 *
 * A candidate is where a new algorithm is developed and proven before any of
 * it goes near the driver. Drop a file in candidates/, rerun
 * "./codecbench.py test", and it is held to exactly the same bar as the
 * shipped code: the golden vectors from the independent Python model,
 * agreement with the driver's portable reference, and the same property
 * tests the KUnit suite applies in the kernel.
 *
 * A file candidates/foo.c must define three functions named after itself:
 *
 *     void foo_init(void);                                  // may be empty
 *     void foo_encode_batch(u8 *dest, const u8 *src, const int n_frames);
 *     void foo_decode_batch(u8 *dest, const u8 *src, const int n_frames);
 *
 * codecbench.py derives those names from the filename and generates the
 * registry entry, so there is nothing to register by hand.
 *
 * The contract is the *batch* API rather than a single frame, deliberately.
 * The driver always encodes up to 10 frames and decodes up to 8 in one call,
 * so a candidate is free to optimize across frames - reusing loaded words,
 * unrolling across frame boundaries, or going wider with SIMD. That avenue is
 * invisible to a per-frame interface. For the common case of an algorithm
 * that genuinely works one frame at a time, PLOYTEC_CANDIDATE_FROM_FRAME()
 * generates the batch wrappers.
 *
 * (C) 2026 Frank van de Pol <fvdpol@gmail.com>
 */

#ifndef PLOYTEC_CANDIDATE_H
#define PLOYTEC_CANDIDATE_H

#include <linux/types.h>
#include <linux/unaligned.h>
#include <linux/string.h>

/*
 * The frame geometry comes from the driver's own header - the copy
 * codecbench.py places in build/src/ - so a candidate can never drift from
 * the sizes the driver actually uses.
 */
#include "ploytec_codec.h"

/**
 * PLOYTEC_CANDIDATE_FROM_FRAME() - build batch entry points from per-frame ones
 * @name: the candidate's name, matching its filename
 * @enc_frame: void (*)(u8 *dest, const u8 *src) encoding one frame
 * @dec_frame: void (*)(u8 *dest, const u8 *src) decoding one frame
 *
 * Strides match the driver: 48 wire / 12 PCM bytes for playback, 64 wire / 18
 * PCM bytes for capture.
 */
#define PLOYTEC_CANDIDATE_FROM_FRAME(name, enc_frame, dec_frame)		\
	void name##_encode_batch(u8 *dest, const u8 *src, const int n_frames)	\
	{									\
		for (int f = 0; f < n_frames; f++) {				\
			enc_frame(dest, src);					\
			dest += PLOYTEC_PLAYBACK_FRAME_SIZE;			\
			src += PLOYTEC_PLAYBACK_PCM_FRAME_SIZE;			\
		}								\
	}									\
	void name##_decode_batch(u8 *dest, const u8 *src, const int n_frames)	\
	{									\
		for (int f = 0; f < n_frames; f++) {				\
			dec_frame(dest, src);					\
			dest += PLOYTEC_CAPTURE_PCM_FRAME_SIZE;			\
			src += PLOYTEC_CAPTURE_FRAME_SIZE;			\
		}								\
	}									\
	struct ploytec_candidate_needs_semicolon_##name

#endif /* PLOYTEC_CANDIDATE_H */
