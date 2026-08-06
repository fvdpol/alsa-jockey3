/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *   Ploytec PCM encoding/decoding functions for the S24_3LE format (3 bytes per sample)
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#ifndef PLOYTEC_CODEC_H
#define PLOYTEC_CODEC_H

#include <linux/types.h>

#define PLOYTEC_PLAYBACK_FRAMES		10	// number of samples per frame
#define PLOYTEC_PLAYBACK_FRAME_SIZE	48

#define PLOYTEC_CAPTURE_FRAMES		8	// number of samples per frame
#define PLOYTEC_CAPTURE_FRAME_SIZE	64

/* batching of the sample processing */
void ploytec_initialise_codec(void);
void ploytec_encode_batch(u8 *dest, const u8 *src, const int n_frames);
void ploytec_decode_batch(u8 *dest, const u8 *src, const int n_frames);

#endif /* PLOYTEC_CODEC_H */
