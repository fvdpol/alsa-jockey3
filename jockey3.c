// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#include <linux/types.h>
#include <linux/atomic.h>
#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>
#include <linux/delay.h>
#include <linux/jiffies.h>
#include <linux/bitops.h>
#include <linux/timekeeping.h>
#include <linux/mutex.h>
#include <linux/cleanup.h>
#include <sound/core.h>
#include <sound/initval.h>
#include <sound/rawmidi.h>
#include <sound/pcm.h>
#include "ploytec_proto.h"
#include "ploytec_codec.h"
#include "midi.h"

#define RELOOP_VENDOR_ID         0x200c
#define RELOOP_JOCKEY3_ME_PID    0x1009
#define RELOOP_JOCKEY3_REMIX_PID 0x1037

enum { JOCKEY3_ME, JOCKEY3_REMIX };
#define CARD_NAME "Reloop Jockey 3"

static int index[SNDRV_CARDS] = SNDRV_DEFAULT_IDX;
static char *id[SNDRV_CARDS] = SNDRV_DEFAULT_STR;
static bool enable[SNDRV_CARDS] = SNDRV_DEFAULT_ENABLE_PNP;

module_param_array(index, int, NULL, 0444);
MODULE_PARM_DESC(index, "Index value for " CARD_NAME " soundcard.");
module_param_array(id, charp, NULL, 0444);
MODULE_PARM_DESC(id, "ID string for " CARD_NAME " soundcard.");
module_param_array(enable, bool, NULL, 0444);
MODULE_PARM_DESC(enable, "Enable " CARD_NAME " soundcard.");

#define JOCKEY3_N_URBS 8

/* Chip flags */
#define JOCKEY3_FLAG_DISCONNECTED	0
#define JOCKEY3_FLAG_STOPPING		1
#define JOCKEY3_FLAG_RESETTING		2
#define JOCKEY3_FLAG_RATE_CHANGING	2

struct jockey3_pcm_urb_stream {
	struct snd_pcm_substream *substream;
	struct usb_anchor anchor;
	struct urb *urbs[JOCKEY3_N_URBS];
	unsigned char *bufs[JOCKEY3_N_URBS];
	atomic_t urbs_in_flight;	// keep track of in-flight URBs
	atomic64_t last_callback_time;	// keep track of active/stall
	spinlock_t lock; // protects playback stream state and buffer offsets
	unsigned int dma_off;
	unsigned int period_off;
	bool running;
	bool callback_processing;
};

struct jockey3_chip {
	/* Core ALSA and USB handles (Mostly read-only after probe) */
	struct snd_card *card;
	struct usb_device *dev;
	struct usb_interface *intf0;
	struct usb_interface *intf1;
	struct snd_pcm *pcm;
	struct snd_rawmidi *rmidi;
	unsigned char *xfer_buf;
	struct mutex rate_mutex; // serializes sample rate changes and active stream tracking
	unsigned long flags;
	unsigned int current_rate;

	/* MIDI Path */
	struct snd_rawmidi_substream *midi_in_substream;
	struct snd_rawmidi_substream *midi_out_substream;
	struct urb *midi_in_urb;
	unsigned char *midi_in_buf;
	spinlock_t midi_lock;
	unsigned int midi_out_acc;	// for the 'Leaky Bucket' rate limiter
	struct midi_running_status midi_state;
	bool midi_callback_processing;

	/* PCM urb streams */
	struct jockey3_pcm_urb_stream playback;
	struct jockey3_pcm_urb_stream capture;
};

static struct usb_driver jockey3_driver;

static inline bool jockey3_is_disconnected(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
}

static inline bool jockey3_is_stopping(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_STOPPING, &chip->flags);
}

static inline bool jockey3_is_resetting(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
}

static inline bool jockey3_is_rate_changing(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_RATE_CHANGING, &chip->flags);
}

static int jockey3_wait_for_rate_change_completion(const struct jockey3_chip *chip)
{
	unsigned long timeout_jiffies = jiffies + msecs_to_jiffies(1000);

	if (jockey3_is_rate_changing(chip))
		dev_dbg(&chip->intf0->dev, "Waiting for rate change completion\n");

	while (jockey3_is_rate_changing(chip)) {
		usleep_range(5000, 20000);
		if (jockey3_is_disconnected(chip))
			return -ENODEV;
		if (time_after(jiffies, timeout_jiffies)) {
			/*
			 * Empirical testing shows that the rate changing typically takes
			 * around 200 ms; so a 1000 ms timeout should give us sufficient
			 * headroom for the rate-change to complete.
			 */
			dev_warn(&chip->intf0->dev, "Timeout waiting for rate change completion\n");
			return -EAGAIN;
		}
	}

	return 0;
}

/*
 * Bounded, synchronous wait for a device reset queued via
 * usb_queue_reset_device() to complete (JOCKEY3_FLAG_RESETTING cleared by
 * jockey3_post_reset()).
 *
 * Deliberately does NOT call usb_reset_device() itself: doing so from an
 * ALSA ioctl context risks a self-deadlock, since a failed/aborted reset
 * can lead to jockey3_disconnect() and the resulting synchronous
 * snd_card_free() (via the card's devm cleanup) running in the same calling
 * thread — which then blocks forever waiting for the very file descriptor
 * this ioctl is still executing under to be closed. Polling here instead
 * lets the actual reset (and any resulting disconnect/card-free) run on the
 * USB core's own workqueue thread.
 */
static int jockey3_wait_for_reset_completion(const struct jockey3_chip *chip)
{
	unsigned long timeout_jiffies = jiffies + msecs_to_jiffies(1000);

	if (jockey3_is_resetting(chip))
		dev_dbg(&chip->intf0->dev, "Waiting for reset completion\n");

	while (jockey3_is_resetting(chip)) {
		usleep_range(5000, 20000);
		if (jockey3_is_disconnected(chip))
			return -ENODEV;
		if (time_after(jiffies, timeout_jiffies)) {
			/*
			 * Empirical testing shows that the reset cycle typically takes
			 * around 334 ms; so a 1000 ms timeout should give us sufficient
			 * headroom for the reset to complete.
			 */
			dev_warn(&chip->intf0->dev, "Timeout waiting for reset completion\n");
			return -EAGAIN;
		}
	}

	return 0;
}

static inline struct jockey3_pcm_urb_stream *jockey3_get_pcm_urb_stream(struct jockey3_chip *chip,
									const int direction)
{
	if (direction == SNDRV_PCM_STREAM_PLAYBACK)
		return &chip->playback;
	else
		return &chip->capture;
}

static int jockey3_active_streams(struct jockey3_chip *chip)
{
	int active_streams = 0;

	scoped_guard(spinlock_irqsave, &chip->capture.lock) {
		if (chip->capture.running)
			active_streams++;
	}

	scoped_guard(spinlock_irqsave, &chip->playback.lock) {
		if (chip->playback.running)
			active_streams++;
	}

	return active_streams;
}

static bool jockey3_process_out_packet(struct jockey3_chip *chip, u8 *urb_buf)
{
	struct snd_pcm_substream *substream = chip->playback.substream;
	struct jockey3_pcm_urb_stream *urb_stream = &chip->playback;
	struct snd_pcm_runtime *runtime;
	unsigned int pcm_buffer_size;
	unsigned int alsa_frame_size;
	unsigned int frames_in_batch;
	unsigned int bytes_avail;
	int f = 0;

	if (unlikely(!substream || !substream->runtime))
		return false;

	runtime = substream->runtime;
	if (unlikely(!runtime->dma_area))
		return false;

	pcm_buffer_size = snd_pcm_lib_buffer_bytes(substream);
	alsa_frame_size = runtime->channels * 3;  // 4 * 3 = 12 bytes

	while (f < PLOYTEC_PLAYBACK_FRAMES) {
		/* calculate how many samples we can process in one batch */
		frames_in_batch = PLOYTEC_PLAYBACK_FRAMES - f;
		bytes_avail = pcm_buffer_size - urb_stream->dma_off;

		/* Respect circular buffer wrap-around */
		if (bytes_avail < frames_in_batch * alsa_frame_size)
			frames_in_batch = bytes_avail / alsa_frame_size;

		if (frames_in_batch == 0)
			break;

		ploytec_encode_batch(urb_buf + f * PLOYTEC_PLAYBACK_FRAME_SIZE,
				     runtime->dma_area + urb_stream->dma_off,
				     frames_in_batch);

		urb_stream->dma_off += frames_in_batch * alsa_frame_size;
		if (urb_stream->dma_off >= pcm_buffer_size)
			urb_stream->dma_off -= pcm_buffer_size;

		urb_stream->period_off += frames_in_batch * alsa_frame_size;

		f += frames_in_batch;
	}

	if (urb_stream->period_off >= runtime->period_size * alsa_frame_size) {
		urb_stream->period_off %= runtime->period_size * alsa_frame_size;
		return true;
	}

	return false;
}

static bool jockey3_process_in_packet(struct jockey3_chip *chip, const u8 *urb_buf)
{
	struct snd_pcm_substream *substream = chip->capture.substream;
	struct jockey3_pcm_urb_stream *urb_stream = &chip->capture;
	struct snd_pcm_runtime *runtime;
	unsigned int pcm_buffer_size;
	unsigned int alsa_frame_size;
	unsigned int frames_in_batch;
	unsigned int bytes_left;
	int f = 0;

	if (unlikely(!substream || !substream->runtime))
		return false;

	runtime = substream->runtime;
	if (unlikely(!runtime->dma_area))
		return false;

	pcm_buffer_size = snd_pcm_lib_buffer_bytes(substream);
	alsa_frame_size = runtime->channels * 3; // 6 * 3 = 18 bytes

	while (f < PLOYTEC_CAPTURE_FRAMES) {
		frames_in_batch = PLOYTEC_CAPTURE_FRAMES - f;
		bytes_left = pcm_buffer_size - urb_stream->dma_off;

		/* Respect circular buffer wrap-around */
		if (bytes_left < frames_in_batch * alsa_frame_size)
			frames_in_batch = bytes_left / alsa_frame_size;

		if (frames_in_batch == 0)
			break;

		ploytec_decode_batch(runtime->dma_area + urb_stream->dma_off,
				     urb_buf + f * PLOYTEC_CAPTURE_FRAME_SIZE,
				     frames_in_batch);

		/* Advance pointers */
		urb_stream->dma_off += frames_in_batch * alsa_frame_size;
		if (urb_stream->dma_off >= pcm_buffer_size)
			urb_stream->dma_off -= pcm_buffer_size;

		urb_stream->period_off += frames_in_batch * alsa_frame_size;

		f += frames_in_batch;
	}

	if (urb_stream->period_off >= runtime->period_size * alsa_frame_size) {
		urb_stream->period_off %= runtime->period_size * alsa_frame_size;
		return true;
	}

	return false;
}

static inline bool jockey3_urb_error_fatal(struct jockey3_chip *chip,
					   struct urb *urb,
					   const char *type)
{
	if (likely(urb->status == 0))
		return false;

	if (urb->status == -ENOENT || urb->status == -ECONNRESET || urb->status == -ESHUTDOWN)
		return true;  /* Silent return, no resubmit */

	/* Fatal error */
	dev_err(&chip->intf0->dev, "%s URB fatal error: %d\n", type, urb->status);
	set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
	return true;
}

static void jockey3_capture_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	struct jockey3_pcm_urb_stream *urb_stream = &chip->capture;
	struct snd_pcm_substream *substream = NULL;
	bool period_elapsed = false;
	int ret;

	atomic_dec(&urb_stream->urbs_in_flight);
	atomic64_set(&urb_stream->last_callback_time, ktime_get_mono_fast_ns());

	if (unlikely(urb_stream->callback_processing))
		dev_warn_ratelimited(&chip->intf0->dev, "Capture: callback_processing already true on new callback!\n");

	if (unlikely(jockey3_urb_error_fatal(chip, urb, "Capture")))
		return;

	if (unlikely(jockey3_is_disconnected(chip) || jockey3_is_stopping(chip)))
		return;

	if (unlikely(urb->actual_length < PLOYTEC_CAPTURE_FRAMES * PLOYTEC_CAPTURE_FRAME_SIZE)) {
		dev_err(&chip->intf0->dev, "Capture URB too small: %d; required: %d\n",
			urb->actual_length, PLOYTEC_CAPTURE_FRAMES * PLOYTEC_CAPTURE_FRAME_SIZE);
		return;
	}

	/* Step 1: Safely fetch the pointer and flag that the callback is active */
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		/* Mark we're active in a critical section */
		urb_stream->callback_processing = true;

		if (urb_stream->running && urb_stream->substream) {
			period_elapsed = jockey3_process_in_packet(chip, urb->transfer_buffer);
			substream = urb_stream->substream;
		}
	}

	/*
	 * Step 2: Safe Zone. ALSA core can't free 'substream' because our
	 * .close path is waiting for 'callback_processing' to become false.
	 * Our lock released to avoid ABBA deadlock with ALSA's internal locking
	 */
	if (period_elapsed && substream)
		snd_pcm_period_elapsed(substream);

	ret = 0;
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		/* mark we're done in the critical processing */
		urb_stream->callback_processing = false;

		/* Keep resubmitting the URB while the interface is alive */
		if (!jockey3_is_stopping(chip) && !jockey3_is_disconnected(chip)) {
			atomic_inc(&urb_stream->urbs_in_flight);
			usb_anchor_urb(urb, &urb_stream->anchor);
			ret = usb_submit_urb(urb, GFP_ATOMIC);
			if (ret < 0) {
				atomic_dec(&urb_stream->urbs_in_flight);
				usb_unanchor_urb(urb);
			}
		}
	}
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit capture URB: %d\n", ret);
}

static u8 jockey3_get_next_midi_out_byte(struct jockey3_chip *chip)
{
	struct snd_rawmidi_substream *substream;
	u8 byte;
	u8 b;
	unsigned long flags;

	spin_lock_irqsave(&chip->midi_lock, flags);

	/*
	 * Rate limit MIDI to ~3125 bytes/sec. Sending at higher rates causes buffer
	 * overflows and message truncation in the device.
	 */
	chip->midi_out_acc += 3125;
	if (chip->midi_out_acc < (chip->current_rate / PLOYTEC_PLAYBACK_FRAMES)) {
		spin_unlock_irqrestore(&chip->midi_lock, flags);
		return PLOYTEC_MIDI_IDLE_BYTE;
	}
	chip->midi_out_acc -= (chip->current_rate / PLOYTEC_PLAYBACK_FRAMES);

	/* Handle queued byte from Running Status expansion first before consuming from ALSA */
	if (chip->midi_state.has_queued_byte) {
		byte = chip->midi_state.queued_byte;
		chip->midi_state.has_queued_byte = false;
		spin_unlock_irqrestore(&chip->midi_lock, flags);
		return byte;
	}

	substream = chip->midi_out_substream;
	spin_unlock_irqrestore(&chip->midi_lock, flags);

	if (!substream)
		return PLOYTEC_MIDI_IDLE_BYTE;

	if (snd_rawmidi_transmit(substream, &b, 1) != 1)
		return PLOYTEC_MIDI_IDLE_BYTE;

	spin_lock_irqsave(&chip->midi_lock, flags);
	byte = midi_running_status_expand(&chip->midi_state, b, &chip->intf0->dev);
	spin_unlock_irqrestore(&chip->midi_lock, flags);
	return byte;
}

static void jockey3_playback_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	struct jockey3_pcm_urb_stream *urb_stream = &chip->playback;
	unsigned char *buf = (unsigned char *)urb->transfer_buffer;
	struct snd_pcm_substream *substream = NULL;
	bool period_elapsed = false;
	int i, ret;

	atomic_dec(&urb_stream->urbs_in_flight);
	atomic64_set(&urb_stream->last_callback_time, ktime_get_mono_fast_ns());

	if (unlikely(jockey3_urb_error_fatal(chip, urb, "Playback")))
		return;

	if (unlikely(jockey3_is_disconnected(chip) || jockey3_is_stopping(chip)))
		return;

	/* Step 1: Safely fetch the pointer and flag that the callback is active */
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		/* Mark we're active in a critical section */
		urb_stream->callback_processing = true;

		if (urb_stream->running && urb_stream->substream) {
			period_elapsed = jockey3_process_out_packet(chip, buf);
			substream = urb_stream->substream;
		} else {
			ploytec_prepare_out_packet(buf);
		}
	}

	/* The outgoing MIDI data is encapsulated in the playback stream */
	buf[PLOYTEC_MIDI_OUT_OFFSET] = jockey3_get_next_midi_out_byte(chip);

	/* Ploytec Sync byte and gap padding */
	buf[PLOYTEC_SYNC_BYTE_OFFSET] = PLOYTEC_SYNC_BYTE_VALUE;
	for (i = PLOYTEC_SYNC_BYTE_OFFSET + 1; i < PLOYTEC_PKT_SIZE; i++)
		buf[i] = 0x00;

	/*
	 * Step 2: Safe Zone. ALSA core can't free 'substream' because our
	 * .close path is waiting for 'callback_processing' to become false.
	 * Our lock released to avoid ABBA deadlock with ALSA's internal locking
	 */
	if (period_elapsed && substream)
		snd_pcm_period_elapsed(substream);

	ret = 0;
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		/* mark we're done in the critical processing */
		urb_stream->callback_processing = false;

		/* Keep resubmitting the URB while the interface is alive */
		if (!jockey3_is_stopping(chip) && !jockey3_is_disconnected(chip)) {
			atomic_inc(&urb_stream->urbs_in_flight);
			usb_anchor_urb(urb, &urb_stream->anchor);
			ret = usb_submit_urb(urb, GFP_ATOMIC);
			if (ret < 0) {
				atomic_dec(&urb_stream->urbs_in_flight);
				usb_unanchor_urb(urb);
			}
		}
	}
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit playback URB: %d\n", ret);
}

static void jockey3_midi_in_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	struct snd_rawmidi_substream *substream;
	unsigned char *buf = (unsigned char *)urb->transfer_buffer;
	int i, ret;

	if (jockey3_urb_error_fatal(chip, urb, "MIDI IN"))
		return;

	if (unlikely(jockey3_is_disconnected(chip) || jockey3_is_stopping(chip)))
		return;

	scoped_guard(spinlock_irqsave, &chip->midi_lock) {
		chip->midi_callback_processing = true;	// Mark we're active in a critical section
		substream = chip->midi_in_substream;
	}

	if (substream) {
		for (i = 0; i < urb->actual_length; i++)
			if (buf[i] != PLOYTEC_MIDI_IDLE_BYTE && buf[i] != 0xF9)
				snd_rawmidi_receive(substream, &buf[i], 1);
	}

	ret = 0;
	scoped_guard(spinlock_irqsave, &chip->midi_lock) {
		/* Mark we're done in critical section */
		chip->midi_callback_processing = false;

		if (!jockey3_is_stopping(chip) && !jockey3_is_disconnected(chip))
			ret = usb_submit_urb(urb, GFP_ATOMIC);
	}
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit MIDI IN URB: %d\n", ret);
}

/*
 * The URB callback signals when it is in a critical section processing data. This is meant to
 * prevent other functions from de-allocating resources required by the callback while it is
 * processing. Can only be called from non-atomic context since this function sleeps.
 */
static void jockey3_wait_for_callback_completion(struct jockey3_chip *chip)
{
	unsigned long timeout_jiffies = jiffies + msecs_to_jiffies(20);
	unsigned int spin_count = 0;

	while (1) {
		bool midi_busy, capture_busy, playback_busy;

		scoped_guard(spinlock_irqsave, &chip->midi_lock) {
			midi_busy = chip->midi_callback_processing;
		}
		scoped_guard(spinlock_irqsave, &chip->capture.lock) {
			capture_busy = chip->capture.callback_processing;
		}
		scoped_guard(spinlock_irqsave, &chip->playback.lock) {
			playback_busy = chip->playback.callback_processing;
		}

		if (!midi_busy && !capture_busy && !playback_busy)
			break;

		if (time_after(jiffies, timeout_jiffies)) {
			dev_err(&chip->intf0->dev,
				"Timeout waiting for URB callback processing to complete.\n");
			break;
		}

		/* fast path: spin for couple of iterations before sleeping */
		if (spin_count < 50) {
			cpu_relax();
			spin_count++;
		} else {
			usleep_range(10, 50);
		}

		/* Yield CPU slightly to let the Tasklet/BH context finish on the other core */
		cpu_relax();
	}
}

static void jockey3_stop_urbs(struct jockey3_chip *chip)
{
	dev_dbg(&chip->intf0->dev, "Stopping all URBs\n");

	set_bit(JOCKEY3_FLAG_STOPPING, &chip->flags);

	usb_kill_urb(chip->midi_in_urb);
	usb_kill_anchored_urbs(&chip->playback.anchor);
	usb_kill_anchored_urbs(&chip->capture.anchor);

	jockey3_wait_for_callback_completion(chip);

	/* after killing the URBs there will be no in-flight requests anymore since the callback
	 * function has been called as part of the shutdown. The number of in-flight URBs should
	 * therefore be zero at this point. Log an inconsistency error if not.
	 */
	if (atomic_read(&chip->playback.urbs_in_flight) != 0)
		dev_err(&chip->intf0->dev, "Inconsistent URB in-flight count: playback=%d != 0\n",
			atomic_read(&chip->playback.urbs_in_flight));
	if (atomic_read(&chip->capture.urbs_in_flight) != 0)
		dev_err(&chip->intf0->dev, "Inconsistent URB in-flight count: capture=%d != 0\n",
			atomic_read(&chip->capture.urbs_in_flight));
}

static void jockey3_start_urbs(struct jockey3_chip *chip)
{
	int i, ret;

	if (jockey3_is_disconnected(chip))
		return;

	dev_dbg(&chip->intf0->dev, "Starting all URBs\n");
	clear_bit(JOCKEY3_FLAG_STOPPING, &chip->flags);
	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		atomic_inc(&chip->playback.urbs_in_flight);
		usb_anchor_urb(chip->playback.urbs[i], &chip->playback.anchor);
		ret = usb_submit_urb(chip->playback.urbs[i], GFP_KERNEL);
		if (ret < 0) {
			atomic_dec(&chip->playback.urbs_in_flight);
			usb_unanchor_urb(chip->playback.urbs[i]);
			dev_err(&chip->intf0->dev, "Failed to submit playback URB %d: %d\n",
				i, ret);
		}

		atomic_inc(&chip->capture.urbs_in_flight);
		usb_anchor_urb(chip->capture.urbs[i], &chip->capture.anchor);
		ret = usb_submit_urb(chip->capture.urbs[i], GFP_KERNEL);
		if (ret < 0) {
			atomic_dec(&chip->capture.urbs_in_flight);
			usb_unanchor_urb(chip->capture.urbs[i]);
			dev_err(&chip->intf0->dev, "Failed to submit capture URB %d: %d\n",
				i, ret);
		}
	}
	ret = usb_submit_urb(chip->midi_in_urb, GFP_KERNEL);
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to submit MIDI IN URB: %d\n", ret);
}

static int jockey3_set_rate(struct jockey3_chip *chip, unsigned int rate)
{
	int ret;
	int current_hw_rate;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	dev_dbg(&chip->intf0->dev, "Setting rate to %u Hz\n", rate);

	ret = ploytec_initialise_device(chip->dev, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to initialise device to change rate: %d\n",
			ret);
		return ret;
	}

	ploytec_get_rate(chip->dev, chip->xfer_buf, &current_hw_rate);
	dev_dbg(&chip->intf0->dev, "Current hardware rate: %u Hz\n", current_hw_rate);
	if (current_hw_rate != rate) {
		dev_dbg(&chip->intf0->dev, "Setting new hardware rate: %u Hz\n", rate);
		ret = ploytec_set_rate(chip->dev, chip->xfer_buf, rate);
		if (ret < 0) {
			dev_err(&chip->intf0->dev, "Failed to set rate: %d\n", ret);
			return ret;
		}
	} else {
		dev_dbg(&chip->intf0->dev, "Hardware rate already at requested value: %u Hz\n",
			current_hw_rate);
	}
	ret = ploytec_start_streaming(chip->dev, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to start streaming after rate change: %d\n",
			ret);
		return ret;
	}

	dev_dbg(&chip->intf0->dev, "Rate set OK\n");
	return 0;
}

static bool jockey3_stream_is_open(struct jockey3_chip *chip, const int direction)
{
	struct jockey3_pcm_urb_stream *urb_stream = jockey3_get_pcm_urb_stream(chip, direction);
	bool open;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		open = urb_stream->substream;
	}
	return open;
}

static bool jockey3_check_urb_steam_alive(const struct jockey3_pcm_urb_stream *urb_stream)
{
	u64 last_time = atomic64_read(&urb_stream->last_callback_time);

	if (!last_time)
		return false;

	/* alive if we had activity within the last 1 ms = 1,000,000 ns */
	return (ktime_get_mono_fast_ns() - last_time <= NSEC_PER_MSEC);
}

/*
 * Poll a stream's URB liveness for up to timeout_ms. Always logs a dev_warn
 * on timeout regardless of whether the caller ends up acting on the result,
 * so that stall frequency (Playback and Capture alike) can be tracked in the
 * field via dmesg while we narrow down the Ploytec firmware behavior.
 */
static bool jockey3_wait_urb_stream_started(struct jockey3_chip *chip, const int direction,
					    const unsigned int timeout_ms)
{
	unsigned long timeout_jiffies = msecs_to_jiffies(timeout_ms);
	unsigned long deadline = jiffies + timeout_jiffies;

	while (time_before(jiffies, deadline)) {
		if (jockey3_is_disconnected(chip))
			return false;

		if (jockey3_check_urb_steam_alive(jockey3_get_pcm_urb_stream(chip, direction)))
			return true;

		usleep_range(500, 2000);
	}

	if (direction == SNDRV_PCM_STREAM_PLAYBACK)
		dev_warn(&chip->intf0->dev, "Playback URB has stalled.\n");
	else
		dev_warn(&chip->intf0->dev, "Capture URB has stalled.\n");
	return false;
}

/*
 * Attempt to recover a stalled Capture URB stream outside of a sample-rate
 * change.
 *
 * This is the deferred counterpart to the post-rate-change check in
 * jockey3_pcm_hw_params(): if Capture stalled while no capture stream was
 * open there, we don't force a disruptive device reset on the spot (it would
 * interrupt any in-progress Playback audio for the sake of a direction
 * nobody is using yet).
 *
 * Instead, the next time a capture stream is opened, jockey3_pcm_prepare()
 * calls this to first retry a lightweight URB stop/start; if Capture still
 * doesn't come back, escalate to a full USB device reset, queued via
 * usb_queue_reset_device() and awaited with jockey3_wait_for_reset_completion()
 * (bounded at 1000 ms; the reset itself measures ~334 ms) rather than calling
 * usb_reset_device() directly from this ioctl context — see
 * jockey3_wait_for_reset_completion() for why.
 */
static int jockey3_recover_capture_stream(struct jockey3_chip *chip)
{
	int ret;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	scoped_guard(mutex, &chip->rate_mutex) {
		dev_warn(&chip->intf0->dev, "Restarting URBs to recover stalled Capture stream\n");
		jockey3_stop_urbs(chip);
		jockey3_start_urbs(chip);
	}

	if (jockey3_wait_urb_stream_started(chip, SNDRV_PCM_STREAM_CAPTURE, 50))
		return 0;

	dev_warn(&chip->intf0->dev,
		 "Capture stream still stalled after URB restart; queuing full USB reset\n");
	set_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
	usb_queue_reset_device(chip->intf0);

	ret = jockey3_wait_for_reset_completion(chip);
	if (ret < 0)
		return ret;

	if (!jockey3_wait_urb_stream_started(chip, SNDRV_PCM_STREAM_CAPTURE, 50))
		dev_err(&chip->intf0->dev,
			"Capture stream still stalled after full USB reset; hardware may need power-cycling\n");

	return 0;
}

static int jockey3_pcm_open(struct snd_pcm_substream *substream)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);
	int ret;

	dev_dbg(&chip->intf0->dev, "PCM open stream %d\n", substream->stream);

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	/* Ensure the card is not in-process of a rate-change */
	ret = jockey3_wait_for_rate_change_completion(chip);
	if (ret < 0)
		return ret;

	runtime->hw.info =
		SNDRV_PCM_INFO_MMAP |
		SNDRV_PCM_INFO_INTERLEAVED |
		SNDRV_PCM_INFO_BLOCK_TRANSFER |
		SNDRV_PCM_INFO_MMAP_VALID;
	runtime->hw.formats = SNDRV_PCM_FMTBIT_S24_3LE;
	runtime->hw.rates =
		SNDRV_PCM_RATE_44100 |
		SNDRV_PCM_RATE_48000 |
		SNDRV_PCM_RATE_88200 |
		SNDRV_PCM_RATE_96000;
	runtime->hw.rate_min = 44100;
	runtime->hw.rate_max = 96000;
	runtime->hw.buffer_bytes_max = 1024 * 1024;

	/*
	 * The period minimum bytes is limited by packet size of the USB URB frames
	 * - Playback URB: 10 frames * 4 channels * 3 bytes/sample = 120 bytes
	 * - Capture URB: 8 frames 6 channels * 3 bytes/sample = 144 bytes
	 */
	runtime->hw.period_bytes_min = 144;
	runtime->hw.period_bytes_max = 512 * 1024;
	runtime->hw.periods_min = 2;
	runtime->hw.periods_max = 1024;

	if (substream->stream == SNDRV_PCM_STREAM_PLAYBACK) {
		runtime->hw.channels_min = 4;
		runtime->hw.channels_max = 4;
	} else {
		runtime->hw.channels_min = 6;
		runtime->hw.channels_max = 6;
	}

	/* Rate constraints under proper locking */
	scoped_guard(mutex, &chip->rate_mutex) {
		if (jockey3_active_streams(chip) > 0) {
			/* Force the new stream to match the existing hardware rate */
			ret = snd_pcm_hw_constraint_single(runtime,
							   SNDRV_PCM_HW_PARAM_RATE,
							   chip->current_rate);
			if (ret < 0)
				return ret;
		}
	}

	/* Substream registration under spinlock to ensure memory consistency to the ISR*/
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		urb_stream->substream = substream;
	}

	return 0;
}

static int jockey3_pcm_close(struct snd_pcm_substream *substream)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);

	dev_dbg(&chip->intf0->dev, "PCM close stream %d\n", substream->stream);

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		urb_stream->substream = NULL;
		urb_stream->running = false;
	}

	/*
	 * Wait for any currently running callback to finish its safe-zone execution so we are
	 * sure to not accessing it anymore before returning to ALSA, and prevent potential
	 * Use-After-Free issues.
	 */
	jockey3_wait_for_callback_completion(chip);

	return 0;
}

static int jockey3_pcm_prepare(struct snd_pcm_substream *substream)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);
	int ret = 0;

	dev_dbg(&chip->intf0->dev, "PCM prepare stream %d\n", substream->stream);
	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	ret = jockey3_wait_for_reset_completion(chip);
	if (ret < 0)
		return ret;

	/* Ensure the card is not in-process of a rate-change */
	ret = jockey3_wait_for_rate_change_completion(chip);
	if (ret < 0)
		return ret;

	/*
	 * Capture may have been left stalled by an earlier rate change that
	 * happened while no capture stream was open (see jockey3_pcm_hw_params()).
	 * Catch it here, before this newly-opened capture stream starts relying
	 * on it.
	 */
	if (substream->stream == SNDRV_PCM_STREAM_CAPTURE &&
	    !jockey3_check_urb_steam_alive(&chip->capture)) {
		dev_warn(&chip->intf0->dev,
			 "Capture URB stalled when opening capture stream; attempting recovery\n");
		ret = jockey3_recover_capture_stream(chip);
		if (ret < 0)
			return ret;
	}

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		urb_stream->dma_off = 0;
		urb_stream->period_off = 0;
	}
	return 0;
}

static int jockey3_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);
	int ret = 0;

	dev_dbg(&chip->intf0->dev, "PCM trigger stream %d, cmd %d\n", substream->stream, cmd);

	if (jockey3_is_disconnected(chip))
		return -ENODEV;
	if (jockey3_is_resetting(chip))
		return -EBUSY;

	/* Ensure the card is not in-process of a rate-change */
	ret = jockey3_wait_for_rate_change_completion(chip);
	if (ret < 0)
		return ret;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		switch (cmd) {
		case SNDRV_PCM_TRIGGER_START:
		case SNDRV_PCM_TRIGGER_RESUME:
			urb_stream->running = true;
			break;
		case SNDRV_PCM_TRIGGER_STOP:
		case SNDRV_PCM_TRIGGER_SUSPEND:
			urb_stream->running = false;
			break;
		default:
			return -EINVAL;
		}
	}
	return 0;
}

static snd_pcm_uframes_t jockey3_pcm_pointer(struct snd_pcm_substream *substream)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);
	unsigned int dma_off;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		dma_off = urb_stream->dma_off;
	}
	return bytes_to_frames(substream->runtime, dma_off);
}

static int jockey3_initialise_ploytec(struct jockey3_chip *chip)
{
	int ret;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	ploytec_initialise_codec();

	ret = ploytec_initialise_device(chip->dev, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Ploytec failed to initialise: %d\n", ret);
		return ret;
	}

	ret = ploytec_start_streaming(chip->dev, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Ploytec failed to start streaming: %d\n", ret);
		return ret;
	}

	dev_dbg(&chip->intf0->dev, "Ploytec initialised successfully; status = 0x%02x\n", ret);
	return 0;
}

static int jockey3_pcm_hw_params(struct snd_pcm_substream *substream,
				 struct snd_pcm_hw_params *hw_params)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	unsigned int rate = params_rate(hw_params);
	unsigned long flags;
	bool playback_alive, capture_alive, capture_open;
	int ret = 0;

	dev_dbg(&chip->intf0->dev, "PCM hw_params rate %u, active_streams %d\n",
		rate, jockey3_active_streams(chip));

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	/* Ensure the card is not in-process of a rate-change */
	ret = jockey3_wait_for_rate_change_completion(chip);
	if (ret < 0)
		return ret;

	scoped_guard(mutex, &chip->rate_mutex) {
		if (chip->current_rate == rate) {
			dev_dbg(&chip->intf0->dev, "Rate already set to %u, skipping change\n",
				rate);
			return 0;
		}

		/*
		 * If multiple streams are active, the ALSA core should have
		 * enforced the constraint from jockey3_pcm_open. We still
		 * sanity check here to be safe.
		 */
		if (jockey3_active_streams(chip) > 1) {
			dev_err(&chip->intf0->dev, "Cannot change rate while other stream is active\n");
			return -EBUSY;
		}

		/* flag that we are in-process of rate changeing*/
		set_bit(JOCKEY3_FLAG_RATE_CHANGING, &chip->flags);

		jockey3_stop_urbs(chip);

		ret = jockey3_set_rate(chip, rate);
		if (ret != 0) {
			dev_err(&chip->intf0->dev, "Rate change to %u failed: %d\n", rate, ret);
			jockey3_start_urbs(chip);
			clear_bit(JOCKEY3_FLAG_RATE_CHANGING, &chip->flags);
			return ret;
		}

		spin_lock_irqsave(&chip->midi_lock, flags);
		chip->current_rate = rate;
		spin_unlock_irqrestore(&chip->midi_lock, flags);

		jockey3_start_urbs(chip);
		clear_bit(JOCKEY3_FLAG_RATE_CHANGING, &chip->flags);
	}

	/*
	 * Ploytec firmware re-synchronization:
	 * In some cases the sample rate change process can fail to restart the
	 * Capture EP (0x86): resulting in the device not transmitting data.
	 * When this happens the Ploytec firmware requires a full USB reset to
	 * re-synchronize the internal engine and restart sending packets.
	 * The stalled flow would otherwise lead to EIO errors in ALSA.
	 *
	 * The exact firmware trigger for this failure is still not fully
	 * understood (see notes.md); as mitigation we check URB liveness on
	 * both directions after every rate change:
	 *
	 *  - Playback always carries the MIDI OUT channel, so it must come
	 *    back alive unconditionally, or MIDI control of the device breaks.
	 *    A Playback stall always forces a reset.
	 *
	 *  - Capture only forces a reset here if a capture stream is currently
	 *    open. An idle Capture stall (no recording in progress) is logged
	 *    but not acted on immediately, to avoid an audible reset glitch on
	 *    unrelated, currently-working Playback audio. Recovery is deferred
	 *    to the next capture stream open (see jockey3_recover_capture_stream()
	 *    called from jockey3_pcm_prepare()).
	 *
	 * pre_reset/post_reset callbacks handle the URB lifecycle.
	 * We call this outside the rate_mutex to allow pre/post_reset to acquire it.
	 */
	playback_alive = jockey3_wait_urb_stream_started(chip, SNDRV_PCM_STREAM_PLAYBACK, 50);
	capture_alive = jockey3_wait_urb_stream_started(chip, SNDRV_PCM_STREAM_CAPTURE, 50);
	capture_open = jockey3_stream_is_open(chip, SNDRV_PCM_STREAM_CAPTURE);

	if (!playback_alive || (!capture_alive && capture_open)) {
		dev_warn(&chip->intf0->dev,
			 "Resetting device to recover from stall after rate change to %u Hz (playback_alive=%d, capture_alive=%d, capture_open=%d)\n",
			 rate, playback_alive, capture_alive, capture_open);
		set_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		usb_queue_reset_device(chip->intf0);
	} else {
		if (!capture_alive)
			dev_dbg(&chip->intf0->dev,
				"Capture URB stalled after rate change to %u Hz, but no capture stream is open; deferring recovery to next capture open\n",
				rate);
		dev_dbg(&chip->intf0->dev, "Rate changed to %u successfully\n", rate);
	}

	return 0;
}

static const struct snd_pcm_ops jockey3_pcm_ops = {
	.open = jockey3_pcm_open,
	.close = jockey3_pcm_close,
	.hw_params = jockey3_pcm_hw_params,
	.prepare = jockey3_pcm_prepare,
	.trigger = jockey3_pcm_trigger,
	.pointer = jockey3_pcm_pointer,
};

static int jockey3_midi_in_open(struct snd_rawmidi_substream *substream)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;
	return 0;
}

static int jockey3_midi_in_close(struct snd_rawmidi_substream *substream)
{
	return 0;
}

static void jockey3_midi_in_trigger(struct snd_rawmidi_substream *substream, int up)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	guard(spinlock_irqsave)(&chip->midi_lock);
	chip->midi_in_substream = up ? substream : NULL;
}

static int jockey3_midi_out_open(struct snd_rawmidi_substream *substream)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;
	return 0;
}

static int jockey3_midi_out_close(struct snd_rawmidi_substream *substream)
{
	return 0;
}

static void jockey3_midi_out_trigger(struct snd_rawmidi_substream *substream, int up)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	guard(spinlock_irqsave)(&chip->midi_lock);
	chip->midi_out_substream = up ? substream : NULL;
}

static const struct snd_rawmidi_ops jockey3_midi_in_ops = {
	.open = jockey3_midi_in_open,
	.close = jockey3_midi_in_close,
	.trigger = jockey3_midi_in_trigger
};

static const struct snd_rawmidi_ops jockey3_midi_out_ops = {
	.open = jockey3_midi_out_open,
	.close = jockey3_midi_out_close,
	.trigger = jockey3_midi_out_trigger
};

static int jockey3_initialise(struct jockey3_chip *chip)
{
	int ret;
	int rate;

	for (int retry = 10; retry > 0; retry--) {
		ret = jockey3_initialise_ploytec(chip);
		if (ret == 0)
			break;
		usleep_range(50000, 100000); /* Wait 50-100 ms before retrying */
	}
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to initialise Ploytec: %d\n", ret);
		return ret;
	}

	scoped_guard(spinlock_irqsave, &chip->midi_lock) {
		chip->current_rate = 44100;	// default sample rate at power-on
		rate = chip->current_rate;
	}
	ret = jockey3_set_rate(chip, rate);
	if (ret < 0)
		return ret;

	jockey3_start_urbs(chip);

	dev_dbg(&chip->intf0->dev, "Initialisation complete.\n");

	return 0;
}

static void jockey3_release_intf1(void *data)
{
	struct usb_interface *intf1 = data;

	usb_driver_release_interface(&jockey3_driver, intf1);
}

static void jockey3_free_urb_action(void *data)
{
	usb_free_urb(data);
}

static void jockey3_kfree_action(void *data)
{
	kfree(data);
}

static void jockey3_stop_urbs_action(void *data)
{
	jockey3_stop_urbs(data);
}

static int jockey3_init_midi_urb(struct jockey3_chip *chip)
{
	struct usb_device *dev = chip->dev;
	struct usb_interface *intf = chip->intf0;
	int ret;

	memset(&chip->midi_state, 0, sizeof(chip->midi_state));
	chip->midi_out_acc = 0;

	chip->midi_in_buf = kmalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
	if (!chip->midi_in_buf)
		return -ENOMEM;

	ret = devm_add_action_or_reset(&intf->dev, jockey3_kfree_action, chip->midi_in_buf);
	if (ret)
		return ret;

	chip->midi_in_urb = usb_alloc_urb(0, GFP_KERNEL);
	if (!chip->midi_in_urb)
		return -ENOMEM;

	ret = devm_add_action_or_reset(&intf->dev, jockey3_free_urb_action, chip->midi_in_urb);
	if (ret)
		return ret;

	usb_fill_bulk_urb(chip->midi_in_urb, dev,
			  usb_rcvbulkpipe(dev, PLOYTEC_EP_NUM_MIDI_IN),
			  chip->midi_in_buf, PLOYTEC_PKT_SIZE,
			  jockey3_midi_in_callback, chip);

	return 0;
}

static int jockey3_init_playback_urbs(struct jockey3_chip *chip)
{
	struct usb_device *dev = chip->dev;
	struct usb_interface *intf = chip->intf0;
	int i, ret;

	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		chip->playback.bufs[i] = kzalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
		if (!chip->playback.bufs[i])
			return -ENOMEM;
		ret = devm_add_action_or_reset(&intf->dev, jockey3_kfree_action,
					       chip->playback.bufs[i]);
		if (ret)
			return ret;

		chip->playback.urbs[i] = usb_alloc_urb(0, GFP_KERNEL);
		if (!chip->playback.urbs[i])
			return -ENOMEM;
		ret = devm_add_action_or_reset(&intf->dev, jockey3_free_urb_action,
					       chip->playback.urbs[i]);
		if (ret)
			return ret;

		ploytec_prepare_out_packet(chip->playback.bufs[i]);

		usb_fill_bulk_urb(chip->playback.urbs[i], dev,
				  usb_sndbulkpipe(dev, PLOYTEC_EP_NUM_PCM_OUT),
				  chip->playback.bufs[i], PLOYTEC_PKT_SIZE,
				  jockey3_playback_callback, chip);
	}

	return 0;
}

static int jockey3_init_capture_urbs(struct jockey3_chip *chip)
{
	struct usb_device *dev = chip->dev;
	struct usb_interface *intf = chip->intf0;
	int i, ret;

	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		chip->capture.bufs[i] = kzalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
		if (!chip->capture.bufs[i])
			return -ENOMEM;
		ret = devm_add_action_or_reset(&intf->dev, jockey3_kfree_action,
					       chip->capture.bufs[i]);
		if (ret)
			return ret;

		chip->capture.urbs[i] = usb_alloc_urb(0, GFP_KERNEL);
		if (!chip->capture.urbs[i])
			return -ENOMEM;
		ret = devm_add_action_or_reset(&intf->dev, jockey3_free_urb_action,
					       chip->capture.urbs[i]);
		if (ret)
			return ret;

		usb_fill_bulk_urb(chip->capture.urbs[i], dev,
				  usb_rcvbulkpipe(dev, PLOYTEC_EP_NUM_PCM_IN),
				  chip->capture.bufs[i], PLOYTEC_PKT_SIZE,
				  jockey3_capture_callback, chip);
	}

	return 0;
}

static bool jockey3_has_bulk_endpoint(struct usb_interface *intf, u8 ep_num, bool out)
{
	int i, j;

	for (i = 0; i < intf->num_altsetting; i++) {
		struct usb_host_interface *alts = &intf->altsetting[i];

		for (j = 0; j < alts->desc.bNumEndpoints; j++) {
			struct usb_endpoint_descriptor *epd = &alts->endpoint[j].desc;

			if (out) {
				if (usb_endpoint_is_bulk_out(epd) &&
				    usb_endpoint_num(epd) == ep_num)
					return true;
			} else {
				if (usb_endpoint_is_bulk_in(epd) &&
				    usb_endpoint_num(epd) == ep_num)
					return true;
			}
		}
	}
	return false;
}

static int jockey3_validate_endpoints(struct usb_interface *intf0, struct usb_interface *intf1)
{
	if (!jockey3_has_bulk_endpoint(intf0, PLOYTEC_EP_NUM_PCM_OUT, true) ||
	    !jockey3_has_bulk_endpoint(intf0, PLOYTEC_EP_NUM_MIDI_IN, false)) {
		dev_err(&intf0->dev, "Required bulk endpoints not found on Interface 0 (OUT: 0x%02x, IN: 0x%02x)\n",
			PLOYTEC_EP_NUM_PCM_OUT, PLOYTEC_EP_NUM_MIDI_IN);
		return -ENODEV;
	}

	if (!jockey3_has_bulk_endpoint(intf1, PLOYTEC_EP_NUM_PCM_IN, false)) {
		dev_err(&intf0->dev, "Required bulk IN endpoint not found on Interface 1 (IN: 0x%02x)\n",
			PLOYTEC_EP_NUM_PCM_IN);
		return -ENODEV;
	}
	return 0;
}

static int jockey3_init_pcm(struct jockey3_chip *chip)
{
	int ret = snd_pcm_new(chip->card, CARD_NAME " Audio", 0, 1, 1, &chip->pcm);

	if (ret < 0)
		return ret;

	strscpy(chip->pcm->name, CARD_NAME " Audio", sizeof(chip->pcm->name));
	chip->pcm->private_data = chip;
	snd_pcm_set_ops(chip->pcm, SNDRV_PCM_STREAM_PLAYBACK, &jockey3_pcm_ops);
	snd_pcm_set_ops(chip->pcm, SNDRV_PCM_STREAM_CAPTURE, &jockey3_pcm_ops);
	snd_pcm_set_managed_buffer_all(chip->pcm, SNDRV_DMA_TYPE_VMALLOC, NULL, 0, 0);
	return 0;
}

static int jockey3_init_midi(struct jockey3_chip *chip)
{
	int ret = snd_rawmidi_new(chip->card, CARD_NAME " MIDI", 0, 1, 1, &chip->rmidi);

	if (ret < 0)
		return ret;

	chip->rmidi->private_data = chip;
	strscpy(chip->rmidi->name, CARD_NAME " MIDI", sizeof(chip->rmidi->name));
	snd_rawmidi_set_ops(chip->rmidi, SNDRV_RAWMIDI_STREAM_INPUT, &jockey3_midi_in_ops);
	snd_rawmidi_set_ops(chip->rmidi, SNDRV_RAWMIDI_STREAM_OUTPUT, &jockey3_midi_out_ops);
	chip->rmidi->info_flags = SNDRV_RAWMIDI_INFO_INPUT |
				  SNDRV_RAWMIDI_INFO_OUTPUT |
				  SNDRV_RAWMIDI_INFO_DUPLEX;
	return 0;
}

static void jockey3_setup_card_names(struct jockey3_chip *chip, int driver_info)
{
	char *jockey3_type;

	strscpy(chip->card->driver, "snd-reloop-jockey3", sizeof(chip->card->driver));
	strscpy(chip->card->shortname, CARD_NAME, sizeof(chip->card->shortname));

	switch (driver_info) {
	case JOCKEY3_ME:
		jockey3_type = "Master Edition";
		break;
	case JOCKEY3_REMIX:
		jockey3_type = "Remix";
		break;
	default:
		jockey3_type = "Unknown";
	}
	snprintf(chip->card->longname, sizeof(chip->card->longname),
		 "%s %s at USB %s", CARD_NAME, jockey3_type, dev_name(&chip->dev->dev));
}

static int jockey3_probe(struct usb_interface *intf, const struct usb_device_id *usb_id)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	struct usb_interface *intf1;
	struct snd_card *card;
	struct jockey3_chip *chip;
	int ret;
	static int dev_idx;

	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;

	intf1 = usb_ifnum_to_if(dev, 1);
	if (!intf1)
		return -ENODEV;

	ret = jockey3_validate_endpoints(intf, intf1);
	if (ret < 0)
		return ret;

	while (dev_idx < SNDRV_CARDS && !enable[dev_idx])
		dev_idx++;

	if (dev_idx >= SNDRV_CARDS)
		return -ENODEV;

	ret = snd_devm_card_new(&intf->dev, index[dev_idx], id[dev_idx], THIS_MODULE,
				sizeof(struct jockey3_chip), &card);
	if (ret < 0)
		return ret;

	chip = card->private_data;
	chip->card = card;
	chip->dev = dev;
	chip->intf0 = intf;
	chip->intf1 = intf1;
	chip->flags = 0;
	spin_lock_init(&chip->midi_lock);
	spin_lock_init(&chip->playback.lock);
	spin_lock_init(&chip->capture.lock);
	mutex_init(&chip->rate_mutex);

	init_usb_anchor(&chip->playback.anchor);
	init_usb_anchor(&chip->capture.anchor);

	chip->xfer_buf = kmalloc(USB_XFER_BUF_SIZE, GFP_KERNEL);
	if (!chip->xfer_buf)
		return -ENOMEM;
	ret = devm_add_action_or_reset(&intf->dev, jockey3_kfree_action, chip->xfer_buf);
	if (ret)
		return ret;

	ret = jockey3_init_midi_urb(chip);
	if (ret < 0)
		return ret;

	ret = jockey3_init_playback_urbs(chip);
	if (ret < 0)
		return ret;

	ret = jockey3_init_capture_urbs(chip);
	if (ret < 0)
		return ret;

	/* Stop all URBs on disconnect */
	ret = devm_add_action(&intf->dev, jockey3_stop_urbs_action, chip);
	if (ret)
		return ret;

	ret = jockey3_init_pcm(chip);
	if (ret < 0)
		return ret;

	ret = jockey3_init_midi(chip);
	if (ret < 0)
		return ret;

	jockey3_setup_card_names(chip, usb_id->driver_info);

	if (card->id[0] == '\0')
		snd_card_set_id(card, "RJ3");

	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
	if (ret < 0)
		return ret;
	ret = devm_add_action_or_reset(&intf->dev, jockey3_release_intf1, intf1);
	if (ret)
		return ret;

	usb_set_intfdata(intf, chip);
	ret = jockey3_initialise(chip);
	if (ret < 0)
		return ret;

	ret = snd_card_register(card);
	if (ret < 0)
		return ret;

	dev_idx++;
	return 0;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		jockey3_stop_urbs(chip);
		snd_card_disconnect(chip->card);
		chip->playback.running = false;
		chip->capture.running = false;
		set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
		clear_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		/*
		 * Card cleanup, URB stopping/freeing, and interface release
		 * are all handled automatically by devres.
		 */
	}
	usb_set_intfdata(intf, NULL);
}

static int jockey3_pre_reset(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		set_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		mutex_lock(&chip->rate_mutex);
		jockey3_stop_urbs(chip);
	}
	return 0;
}

static int jockey3_post_reset(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);
	u32 hw_rate = 0;

	if (chip && intf == chip->intf0) {
		jockey3_initialise_ploytec(chip);

		/* Verify if the sample rate persisted through the reset */
		if (ploytec_get_rate(chip->dev, chip->xfer_buf, &hw_rate) == 0) {
			if (hw_rate != chip->current_rate) {
				dev_warn(&chip->intf0->dev,
					 "Rate mismatch after reset. HW: %u, Expected: %u. Re-applying...\n",
					 hw_rate, chip->current_rate);
				jockey3_set_rate(chip, chip->current_rate);
			} else {
				dev_dbg(&chip->intf0->dev, "Rate %u Hz persisted through reset\n",
					hw_rate);
			}
		}

		jockey3_start_urbs(chip);
		clear_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		mutex_unlock(&chip->rate_mutex);
	}
	return 0;
}

static int jockey3_suspend(struct usb_interface *intf, pm_message_t message)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		dev_dbg(&intf->dev, "USB suspend, stopping URBs\n");

		/* Notify ALSA core to transition state and unblock userspace */
		if (chip->pcm)
			snd_pcm_suspend_all(chip->pcm);

		/* Stop the physical URBs */
		jockey3_stop_urbs(chip);
	}
	return 0;
}

static int jockey3_restore_device(struct jockey3_chip *chip, bool reset)
{
	int ret;

	scoped_guard(mutex, &chip->rate_mutex) {
		if (reset) {
			ret = jockey3_initialise_ploytec(chip);
			if (ret < 0)
				return ret;
		}

		ret = jockey3_set_rate(chip, chip->current_rate);
		if (ret < 0)
			return ret;

		jockey3_start_urbs(chip);
		return 0;
	}
}

static int jockey3_resume(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		dev_dbg(&intf->dev, "USB resume, restoring device\n");
		return jockey3_restore_device(chip, false);
	}
	return 0;
}

static int jockey3_reset_resume(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		dev_dbg(&intf->dev, "USB reset resume, restoring device\n");
		return jockey3_restore_device(chip, true);
	}
	return 0;
}

static const struct usb_device_id jockey3_ids[] = {
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_ME_PID), .driver_info = JOCKEY3_ME },
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_REMIX_PID), .driver_info = JOCKEY3_REMIX },
	{}
};
MODULE_DEVICE_TABLE(usb, jockey3_ids);

static struct usb_driver jockey3_driver = {
	.name = "snd-reloop-jockey3",
	.probe = jockey3_probe,
	.disconnect = jockey3_disconnect,
	.pre_reset = jockey3_pre_reset,
	.post_reset = jockey3_post_reset,
	.suspend = jockey3_suspend,
	.resume = jockey3_resume,
	.reset_resume = jockey3_reset_resume,
	.id_table = jockey3_ids
};

module_usb_driver(jockey3_driver);

MODULE_AUTHOR("Frank van de Pol");
MODULE_DESCRIPTION(CARD_NAME " ALSA Driver");
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: snd-pcm snd-rawmidi");
