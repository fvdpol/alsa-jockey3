/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *   Ploytec PCM encoding functions for the S24_3LE format (3 bytes per sample)
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#ifndef __SOUND_USB_JOCKEY3_PLOYTEC_CODEC_H
#define __SOUND_USB_JOCKEY3_PLOYTEC_CODEC_H

#include <linux/types.h>

/*
 * Playback: 4 channels of S24_3LE (3 bytes/sample).
 * The *_FRAME_SIZE values are the Ploytec wire format; the *_PCM_FRAME_SIZE
 * values are the corresponding ALSA-side frame.
 */
#define PLOYTEC_PLAYBACK_FRAMES		10	// number of samples per frame
#define PLOYTEC_PLAYBACK_FRAME_SIZE	48
#define PLOYTEC_PLAYBACK_PCM_FRAME_SIZE	12	// 4 channels * 3 bytes

/* batching of the sample processing */
void ploytec_encode_batch(u8 *dest, const u8 *src, const int n_frames);

#endif /* __SOUND_USB_JOCKEY3_PLOYTEC_CODEC_H */
