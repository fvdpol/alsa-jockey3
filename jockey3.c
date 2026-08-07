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
#include <linux/completion.h>
#include <linux/mutex.h>
#include <linux/wait.h>
#include <linux/cleanup.h>
#include <sound/core.h>
#include <sound/initval.h>
#include <sound/rawmidi.h>
#include <sound/pcm.h>
#include "ploytec_proto.h"
#include "ploytec_codec.h"
#include "ploytec_midi.h"

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

/*
 * Locking overview
 * ================
 *
 *   rate_mutex                     process context only, outermost
 *     |- playback.lock             IRQ-safe leaf
 *     |- capture.lock              IRQ-safe leaf
 *     `- midi_lock                 IRQ-safe leaf
 *
 * The three leaf spinlocks are never nested inside one another; anything that
 * needs more than one takes them in sequence, not nested. rate_mutex is never
 * taken from atomic context.
 *
 * Against the ALSA core the order is:
 *
 *   snd_pcm_stream_lock  ->  playback.lock / capture.lock
 *
 * which is why the URB completion handlers drop their stream spinlock before
 * calling snd_pcm_period_elapsed() or snd_pcm_stop_xrun(); both take the
 * stream lock, and taking it while holding ours would invert the order.
 *
 * .trigger and .pointer are called by the core with the stream lock held and
 * interrupts disabled, so neither may sleep.
 */

#define JOCKEY3_N_URBS 8

/* Consecutive URB transport errors tolerated before a direction is given up on */
#define JOCKEY3_MAX_URB_ERRORS 8

/* Chip flags */
#define JOCKEY3_FLAG_DISCONNECTED	0
#define JOCKEY3_FLAG_RESETTING		1

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
	/*
	 * Number of URB completions currently inside the "safe zone" where they
	 * may still touch substream/runtime->dma_area. Protected by @lock; the
	 * last one out wakes @drain_wait. A count rather than a flag because
	 * there are JOCKEY3_N_URBS URBs per direction and their completions can
	 * overlap on SMP.
	 */
	unsigned int callbacks_active;
	wait_queue_head_t drain_wait;
	/*
	 * Set by jockey3_stop_urbs() before the anchor is killed, cleared by
	 * jockey3_start_urbs(). Protected by @lock and tested by the completion
	 * handler inside the same critical section that anchors and resubmits,
	 * so a callback can never re-anchor a URB after the kill has drained the
	 * anchor.
	 */
	bool stopping;
	unsigned int consec_errors;	// consecutive URB transport errors; @lock
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
	unsigned int current_rate;	// @rate_mutex
	unsigned int dev_idx;		// slot held in jockey3_devices_used
	struct completion reset_done;	// signalled by post_reset/disconnect

	/* MIDI Path */
	struct snd_rawmidi_substream *midi_in_substream;
	struct snd_rawmidi_substream *midi_out_substream;
	struct urb *midi_in_urb;
	unsigned char *midi_in_buf;
	spinlock_t midi_lock;
	unsigned int midi_out_acc;	// for the 'Leaky Bucket' rate limiter
	unsigned int midi_rate_divisor;	// current_rate / PLOYTEC_PLAYBACK_FRAMES; @midi_lock
	struct ploytec_midi_running_status midi_state;
	bool midi_stopping;		// see jockey3_pcm_urb_stream::stopping
	unsigned int midi_consec_errors;	// consecutive MIDI IN URB errors; @midi_lock

	/* PCM urb streams */
	struct jockey3_pcm_urb_stream playback;
	struct jockey3_pcm_urb_stream capture;
};

static struct usb_driver jockey3_driver;

/*
 * Card index allocation. A plain incrementing counter would both race between
 * concurrent probes and never reuse a slot, so after SNDRV_CARDS successful
 * probes no further device could attach even if all of them had been unplugged.
 */
static DEFINE_MUTEX(jockey3_devices_mutex);
static DECLARE_BITMAP(jockey3_devices_used, SNDRV_CARDS);

static inline bool jockey3_is_disconnected(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
}

static inline bool jockey3_is_resetting(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
}

/*
 * Publish a new sample rate.
 *
 * chip->current_rate is protected by rate_mutex, but the MIDI OUT rate limiter
 * runs in URB completion (atomic) context and cannot take it. Rather than have
 * that path read current_rate unlocked, the value it actually needs is derived
 * here and published under midi_lock -- which also gets the division off the
 * per-URB hot path.
 */
static void jockey3_set_current_rate(struct jockey3_chip *chip, unsigned int rate)
{
	lockdep_assert_held(&chip->rate_mutex);

	chip->current_rate = rate;
	scoped_guard(spinlock_irqsave, &chip->midi_lock)
		chip->midi_rate_divisor = rate / PLOYTEC_PLAYBACK_FRAMES;
}

/*
 * Rate changes are serialized by chip->rate_mutex alone: jockey3_pcm_hw_params()
 * performs the whole stop/set-rate/start sequence while holding it, so any other
 * sleepable callback that takes the mutex is automatically excluded for the
 * duration. There is deliberately no separate "rate changing" flag to poll --
 * the only caller that could not take the mutex was .trigger, which runs in
 * atomic context and must not block at all.
 */

/*
 * Bounded, synchronous wait for a device reset queued via
 * usb_queue_reset_device() to complete. chip->reset_done is completed by
 * jockey3_post_reset(), and also by jockey3_disconnect() so that a waiter is
 * released when the USB core skips post_reset() entirely (a failed reset marks
 * the interface for rebinding and unbinds it instead).
 *
 * Deliberately does NOT call usb_reset_device() itself: doing so from an
 * ALSA ioctl context risks a self-deadlock, since a failed/aborted reset
 * can lead to jockey3_disconnect() and the resulting synchronous
 * snd_card_free() (via the card's devm cleanup) running in the same calling
 * thread — which then blocks forever waiting for the very file descriptor
 * this ioctl is still executing under to be closed. Queuing the reset instead
 * lets it (and any resulting disconnect/card-free) run on the USB core's own
 * workqueue thread.
 */
static int jockey3_wait_for_reset_completion(struct jockey3_chip *chip)
{
	/*
	 * Empirical testing shows that the reset cycle typically takes around
	 * 334 ms; a 1000 ms timeout gives sufficient headroom.
	 */
	if (!jockey3_is_resetting(chip))
		return 0;

	dev_dbg(&chip->intf0->dev, "Waiting for reset completion\n");

	if (!wait_for_completion_timeout(&chip->reset_done, msecs_to_jiffies(1000))) {
		dev_warn(&chip->intf0->dev, "Timeout waiting for reset completion\n");
		return -EAGAIN;
	}

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

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

enum jockey3_urb_state {
	JOCKEY3_URB_OK,		/* completed normally */
	JOCKEY3_URB_STOPPED,	/* teardown in progress; return without resubmitting */
	JOCKEY3_URB_ERROR,	/* transport error, potentially transient */
};

static inline enum jockey3_urb_state jockey3_urb_check(const struct urb *urb)
{
	if (likely(urb->status == 0))
		return JOCKEY3_URB_OK;

	if (urb->status == -ENOENT || urb->status == -ECONNRESET || urb->status == -ESHUTDOWN)
		return JOCKEY3_URB_STOPPED;

	return JOCKEY3_URB_ERROR;
}

/*
 * Account for a transport error (-EPROTO, -EPIPE, -EOVERFLOW, -ETIME, ...).
 *
 * These are frequently transient -- marginal cabling and some host controllers
 * produce them routinely -- so a single one must not disable the card. Keep
 * resubmitting while the consecutive count stays below JOCKEY3_MAX_URB_ERRORS;
 * beyond that, stop feeding this direction and leave recovery to the next
 * .prepare, which already carries the stall detection and reset path.
 *
 * Return: true if the caller should give up and not resubmit.
 */
static bool jockey3_urb_error_give_up(struct jockey3_chip *chip,
				      struct jockey3_pcm_urb_stream *urb_stream,
				      int status, const char *type)
{
	struct snd_pcm_substream *substream = NULL;
	unsigned int errors;
	bool crossed_limit;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		errors = ++urb_stream->consec_errors;
		/*
		 * The increment and this test share one critical section, so
		 * exactly one caller observes the transition even though up to
		 * JOCKEY3_N_URBS completions arrive together.
		 */
		crossed_limit = errors == JOCKEY3_MAX_URB_ERRORS;

		if (crossed_limit && urb_stream->running && urb_stream->substream) {
			/* Join the safe zone so the substream cannot be freed below */
			urb_stream->callbacks_active++;
			substream = urb_stream->substream;
		}
	}

	/*
	 * Already given up. When the device goes away every one of the
	 * JOCKEY3_N_URBS in-flight URBs completes with an error, so without this
	 * the limit would be re-reported once per URB (and the stream stopped
	 * repeatedly). Report the transition only.
	 */
	if (errors > JOCKEY3_MAX_URB_ERRORS)
		return true;

	dev_err_ratelimited(&chip->intf0->dev, "%s URB error: %d (%u consecutive)\n",
			    type, status, errors);

	if (!crossed_limit)
		return false;

	dev_err(&chip->intf0->dev,
		"%s stopped after %u consecutive URB errors; deferring recovery\n",
		type, errors);

	/* snd_pcm_stop_xrun() takes the stream lock, so call it with ours dropped */
	if (substream) {
		snd_pcm_stop_xrun(substream);
		scoped_guard(spinlock_irqsave, &urb_stream->lock) {
			if (!--urb_stream->callbacks_active)
				wake_up(&urb_stream->drain_wait);
		}
	}

	return true;
}

static void jockey3_capture_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	struct jockey3_pcm_urb_stream *urb_stream = &chip->capture;
	struct snd_pcm_substream *substream = NULL;
	bool period_elapsed = false;
	bool data_valid = true;
	bool active = false;
	int ret;

	atomic_dec(&urb_stream->urbs_in_flight);
	atomic64_set(&urb_stream->last_callback_time, ktime_get_mono_fast_ns());

	switch (jockey3_urb_check(urb)) {
	case JOCKEY3_URB_STOPPED:
		return;
	case JOCKEY3_URB_ERROR:
		if (jockey3_urb_error_give_up(chip, urb_stream, urb->status, "Capture"))
			return;
		/* Transient: resubmit, but this buffer holds no usable data */
		data_valid = false;
		break;
	case JOCKEY3_URB_OK:
		break;
	}

	if (unlikely(jockey3_is_disconnected(chip)))
		return;

	if (data_valid &&
	    unlikely(urb->actual_length < PLOYTEC_CAPTURE_FRAMES * PLOYTEC_CAPTURE_FRAME_SIZE)) {
		dev_err(&chip->intf0->dev, "Capture URB too small: %d; required: %d\n",
			urb->actual_length, PLOYTEC_CAPTURE_FRAMES * PLOYTEC_CAPTURE_FRAME_SIZE);
		data_valid = false;
	}

	/* Step 1: Safely fetch the pointer and join the safe zone */
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		if (data_valid)
			urb_stream->consec_errors = 0;

		if (data_valid && !urb_stream->stopping &&
		    urb_stream->running && urb_stream->substream) {
			urb_stream->callbacks_active++;
			active = true;
			period_elapsed = jockey3_process_in_packet(chip, urb->transfer_buffer);
			substream = urb_stream->substream;
		}
	}

	/*
	 * Step 2: Safe Zone. ALSA core can't free 'substream' because
	 * jockey3_pcm_sync_stop() waits for 'callbacks_active' to drain before
	 * the core releases the buffer. Our lock is released here to avoid an
	 * ABBA deadlock with ALSA's internal locking: snd_pcm_period_elapsed()
	 * takes the stream lock, and the order is stream lock -> urb_stream->lock.
	 */
	if (period_elapsed && substream)
		snd_pcm_period_elapsed(substream);

	ret = 0;
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		/* Leave the safe zone; last one out wakes any waiter */
		if (active && !--urb_stream->callbacks_active)
			wake_up(&urb_stream->drain_wait);

		/*
		 * Keep resubmitting the URB while the interface is alive. The
		 * 'stopping' test and the anchor+submit below must stay in this
		 * one critical section: that is what stops a URB being re-added
		 * to an anchor jockey3_stop_urbs() has already drained.
		 */
		if (!urb_stream->stopping && !jockey3_is_disconnected(chip)) {
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

/*
 * midi_lock is held across snd_rawmidi_transmit() rather than being dropped
 * around it: that keeps chip->midi_out_substream from changing under us, and
 * the lock order is safe because snd_rawmidi_output_trigger() is invoked by the
 * rawmidi core without substream->lock held. Holding a driver lock across
 * snd_rawmidi_transmit() is the established idiom (see sound/usb/midi.c, which
 * calls it under ep->buffer_lock).
 */
static u8 jockey3_get_next_midi_out_byte(struct jockey3_chip *chip)
{
	u8 b;

	guard(spinlock_irqsave)(&chip->midi_lock);

	/*
	 * Rate limit MIDI to ~3125 bytes/sec. Sending at higher rates causes buffer
	 * overflows and message truncation in the device.
	 */
	chip->midi_out_acc += 3125;
	if (chip->midi_out_acc < chip->midi_rate_divisor)
		return PLOYTEC_MIDI_IDLE_BYTE;
	chip->midi_out_acc -= chip->midi_rate_divisor;

	/* Handle queued byte from Running Status expansion first before consuming from ALSA */
	if (chip->midi_state.has_queued_byte) {
		chip->midi_state.has_queued_byte = false;
		return chip->midi_state.queued_byte;
	}

	if (!chip->midi_out_substream)
		return PLOYTEC_MIDI_IDLE_BYTE;

	if (snd_rawmidi_transmit(chip->midi_out_substream, &b, 1) != 1)
		return PLOYTEC_MIDI_IDLE_BYTE;

	return ploytec_midi_running_status_expand(&chip->midi_state, b, &chip->intf0->dev);
}

/*
 * FIXME: the MIDI-idle-byte/sync-byte writes below are redundant at both
 * call sites: jockey3_playback_callback()'s idle branch immediately
 * overwrites both fields again a few lines further down regardless of
 * which branch ran, and the buffer-allocation call site hands us an
 * already-zeroed kzalloc() buffer, making the memset() here redundant
 * there. Left as a straight move for now; worth restructuring later
 * instead of duplicating these writes.
 */
static void jockey3_prepare_idle_out_packet(u8 *buf)
{
	memset(buf, 0, PLOYTEC_PKT_SIZE);
	buf[PLOYTEC_MIDI_OUT_OFFSET] = PLOYTEC_MIDI_IDLE_BYTE;
	buf[PLOYTEC_SYNC_BYTE_OFFSET] = PLOYTEC_SYNC_BYTE_VALUE;
}

static void jockey3_playback_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	struct jockey3_pcm_urb_stream *urb_stream = &chip->playback;
	unsigned char *buf = (unsigned char *)urb->transfer_buffer;
	struct snd_pcm_substream *substream = NULL;
	bool period_elapsed = false;
	bool data_valid = true;
	bool active = false;
	int i, ret;

	atomic_dec(&urb_stream->urbs_in_flight);
	atomic64_set(&urb_stream->last_callback_time, ktime_get_mono_fast_ns());

	switch (jockey3_urb_check(urb)) {
	case JOCKEY3_URB_STOPPED:
		return;
	case JOCKEY3_URB_ERROR:
		if (jockey3_urb_error_give_up(chip, urb_stream, urb->status, "Playback"))
			return;
		data_valid = false;
		break;
	case JOCKEY3_URB_OK:
		break;
	}

	if (unlikely(jockey3_is_disconnected(chip)))
		return;

	/* Step 1: Safely fetch the pointer and join the safe zone */
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		if (data_valid)
			urb_stream->consec_errors = 0;

		if (data_valid && !urb_stream->stopping &&
		    urb_stream->running && urb_stream->substream) {
			urb_stream->callbacks_active++;
			active = true;
			period_elapsed = jockey3_process_out_packet(chip, buf);
			substream = urb_stream->substream;
		} else {
			jockey3_prepare_idle_out_packet(buf);
		}
	}

	/* The outgoing MIDI data is encapsulated in the playback stream */
	buf[PLOYTEC_MIDI_OUT_OFFSET] = jockey3_get_next_midi_out_byte(chip);

	/* Ploytec Sync byte and gap padding */
	buf[PLOYTEC_SYNC_BYTE_OFFSET] = PLOYTEC_SYNC_BYTE_VALUE;
	for (i = PLOYTEC_SYNC_BYTE_OFFSET + 1; i < PLOYTEC_PKT_SIZE; i++)
		buf[i] = 0x00;

	/*
	 * Step 2: Safe Zone. ALSA core can't free 'substream' because
	 * jockey3_pcm_sync_stop() waits for 'callbacks_active' to drain before
	 * the core releases the buffer. Our lock is released here to avoid an
	 * ABBA deadlock with ALSA's internal locking: snd_pcm_period_elapsed()
	 * takes the stream lock, and the order is stream lock -> urb_stream->lock.
	 */
	if (period_elapsed && substream)
		snd_pcm_period_elapsed(substream);

	ret = 0;
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		/* Leave the safe zone; last one out wakes any waiter */
		if (active && !--urb_stream->callbacks_active)
			wake_up(&urb_stream->drain_wait);

		/*
		 * Keep resubmitting the URB while the interface is alive. The
		 * 'stopping' test and the anchor+submit below must stay in this
		 * one critical section: that is what stops a URB being re-added
		 * to an anchor jockey3_stop_urbs() has already drained.
		 */
		if (!urb_stream->stopping && !jockey3_is_disconnected(chip)) {
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
	unsigned char *buf = (unsigned char *)urb->transfer_buffer;
	unsigned int errors;
	int i, n = 0, ret;

	switch (jockey3_urb_check(urb)) {
	case JOCKEY3_URB_STOPPED:
		return;
	case JOCKEY3_URB_ERROR:
		scoped_guard(spinlock_irqsave, &chip->midi_lock)
			errors = ++chip->midi_consec_errors;

		/* Already given up; see jockey3_urb_error_give_up() */
		if (errors > JOCKEY3_MAX_URB_ERRORS)
			return;

		dev_err_ratelimited(&chip->intf0->dev,
				    "MIDI IN URB error: %d (%u consecutive)\n",
				    urb->status, errors);
		if (errors == JOCKEY3_MAX_URB_ERRORS) {
			dev_err(&chip->intf0->dev,
				"MIDI IN stopped after %u consecutive URB errors\n", errors);
			return;
		}
		/* Transient: resubmit, but there is no usable data in this buffer */
		urb->actual_length = 0;
		break;
	case JOCKEY3_URB_OK:
		scoped_guard(spinlock_irqsave, &chip->midi_lock)
			chip->midi_consec_errors = 0;
		break;
	}

	if (unlikely(jockey3_is_disconnected(chip)))
		return;

	/*
	 * Compact the payload in place, dropping the padding the device emits
	 * between real MIDI bytes. The transfer buffer is ours, so this needs no
	 * lock; doing it up front turns the delivery below into a single locked
	 * call instead of one per byte.
	 *
	 * For different devices (firmware revision) different padding bytes have
	 * been observed: 0xF9, 0xFB, 0xFD, 0xFF. Since the device is not sending
	 * any MIDI system or real-time messages, we can safely ignore any byte
	 * 0xF0..0xFF received from the device.
	 */
	for (i = 0; i < urb->actual_length; i++)
		if (buf[i] < 0xF0)
			buf[n++] = buf[i];

	ret = 0;
	scoped_guard(spinlock_irqsave, &chip->midi_lock) {
		/*
		 * Deliver under midi_lock so the substream cannot be cleared by
		 * jockey3_midi_in_close() while we are dereferencing it.
		 */
		if (n && !chip->midi_stopping && chip->midi_in_substream)
			snd_rawmidi_receive(chip->midi_in_substream, buf, n);

		if (!chip->midi_stopping && !jockey3_is_disconnected(chip))
			ret = usb_submit_urb(urb, GFP_ATOMIC);
	}
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit MIDI IN URB: %d\n", ret);
}

static void jockey3_stop_urbs(struct jockey3_chip *chip)
{
	dev_dbg(&chip->intf0->dev, "Stopping all URBs\n");

	/*
	 * Fence the completion handlers before killing anything. Each 'stopping'
	 * store pairs with the test the matching handler makes while holding the
	 * same lock it uses to anchor and resubmit, so once these guards are
	 * released no handler can add a URB back to an anchor we are about to
	 * drain. The spinlocks provide the required ordering; no explicit barrier
	 * is needed.
	 */
	scoped_guard(spinlock_irqsave, &chip->playback.lock)
		chip->playback.stopping = true;
	scoped_guard(spinlock_irqsave, &chip->capture.lock)
		chip->capture.stopping = true;
	scoped_guard(spinlock_irqsave, &chip->midi_lock)
		chip->midi_stopping = true;

	/*
	 * usb_kill_urb()/usb_kill_anchored_urbs() do not return until the
	 * completion handler of each URB has finished, so no callback can still
	 * be in its safe zone once these return -- no separate drain is needed
	 * here (jockey3_pcm_sync_stop() covers the ALSA buffer-teardown path).
	 */
	usb_kill_urb(chip->midi_in_urb);
	usb_kill_anchored_urbs(&chip->playback.anchor);
	usb_kill_anchored_urbs(&chip->capture.anchor);

	/*
	 * Drop the liveness timestamps: a stale value would otherwise make
	 * jockey3_check_urb_steam_alive() report a stopped stream as alive for
	 * up to its 1 ms window.
	 */
	atomic64_set(&chip->playback.last_callback_time, 0);
	atomic64_set(&chip->capture.last_callback_time, 0);

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

/* Return: 0 on success, or the first submit error encountered. */
static int jockey3_start_urbs(struct jockey3_chip *chip)
{
	int i, ret, first_err = 0;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	dev_dbg(&chip->intf0->dev, "Starting all URBs\n");

	/*
	 * Clear the error budget as well: a stream that was given up on must get
	 * a fresh JOCKEY3_MAX_URB_ERRORS allowance, otherwise the first error
	 * after a restart would immediately exceed the stale count and give up
	 * again with no retries.
	 */
	scoped_guard(spinlock_irqsave, &chip->playback.lock) {
		chip->playback.stopping = false;
		chip->playback.consec_errors = 0;
	}
	scoped_guard(spinlock_irqsave, &chip->capture.lock) {
		chip->capture.stopping = false;
		chip->capture.consec_errors = 0;
	}
	scoped_guard(spinlock_irqsave, &chip->midi_lock) {
		chip->midi_stopping = false;
		chip->midi_consec_errors = 0;
	}

	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		atomic_inc(&chip->playback.urbs_in_flight);
		usb_anchor_urb(chip->playback.urbs[i], &chip->playback.anchor);
		ret = usb_submit_urb(chip->playback.urbs[i], GFP_KERNEL);
		if (ret < 0) {
			atomic_dec(&chip->playback.urbs_in_flight);
			usb_unanchor_urb(chip->playback.urbs[i]);
			dev_err(&chip->intf0->dev, "Failed to submit playback URB %d: %d\n",
				i, ret);
			if (!first_err)
				first_err = ret;
		}

		atomic_inc(&chip->capture.urbs_in_flight);
		usb_anchor_urb(chip->capture.urbs[i], &chip->capture.anchor);
		ret = usb_submit_urb(chip->capture.urbs[i], GFP_KERNEL);
		if (ret < 0) {
			atomic_dec(&chip->capture.urbs_in_flight);
			usb_unanchor_urb(chip->capture.urbs[i]);
			dev_err(&chip->intf0->dev, "Failed to submit capture URB %d: %d\n",
				i, ret);
			if (!first_err)
				first_err = ret;
		}
	}
	ret = usb_submit_urb(chip->midi_in_urb, GFP_KERNEL);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to submit MIDI IN URB: %d\n", ret);
		if (!first_err)
			first_err = ret;
	}

	return first_err;
}

static int jockey3_set_rate(struct jockey3_chip *chip, unsigned int rate)
{
	int ret;
	u32 current_hw_rate;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	dev_dbg(&chip->intf0->dev, "Setting rate to %u Hz\n", rate);

	ret = ploytec_initialize_device(chip->intf0, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to initialize device to change rate: %d\n",
			ret);
		return ret;
	}

	ret = ploytec_get_rate(chip->intf0, chip->xfer_buf, &current_hw_rate);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to read current hardware rate: %d\n", ret);
		return ret;
	}
	dev_dbg(&chip->intf0->dev, "Current hardware rate: %u Hz\n", current_hw_rate);
	if (current_hw_rate != rate) {
		dev_dbg(&chip->intf0->dev, "Setting new hardware rate: %u Hz\n", rate);
		ret = ploytec_set_rate(chip->intf0, chip->xfer_buf, rate);
		if (ret < 0) {
			dev_err(&chip->intf0->dev, "Failed to set rate: %d\n", ret);
			return ret;
		}
	} else {
		dev_dbg(&chip->intf0->dev, "Hardware rate already at requested value: %u Hz\n",
			current_hw_rate);
	}
	ret = ploytec_start_streaming(chip->intf0, chip->xfer_buf);
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
	reinit_completion(&chip->reset_done);
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

/*
 * SNDRV_PCM_INFO_BATCH: the hardware pointer only advances once per completed
 * bulk URB (a whole Ploytec frame group), never per sample, so userspace must
 * not assume a sample-accurate position.
 *
 * SNDRV_PCM_INFO_RESUME is deliberately absent: the device loses stream
 * synchronization across a suspend, so the ALSA core should return -ESTRPIPE
 * and have userspace re-prepare rather than issue TRIGGER_RESUME.
 */
#define JOCKEY3_PCM_INFO	(SNDRV_PCM_INFO_MMAP |		\
				 SNDRV_PCM_INFO_INTERLEAVED |	\
				 SNDRV_PCM_INFO_BLOCK_TRANSFER |\
				 SNDRV_PCM_INFO_BATCH |		\
				 SNDRV_PCM_INFO_MMAP_VALID)

#define JOCKEY3_PCM_RATES	(SNDRV_PCM_RATE_44100 |	\
				 SNDRV_PCM_RATE_48000 |	\
				 SNDRV_PCM_RATE_88200 |	\
				 SNDRV_PCM_RATE_96000)

static const struct snd_pcm_hardware jockey3_pcm_hw_playback = {
	.info			= JOCKEY3_PCM_INFO,
	.formats		= SNDRV_PCM_FMTBIT_S24_3LE,
	.rates			= JOCKEY3_PCM_RATES,
	.rate_min		= 44100,
	.rate_max		= 96000,
	.channels_min		= 4,
	.channels_max		= 4,
	.buffer_bytes_max	= 1024 * 1024,
	/* One playback URB carries 10 frames * 4 channels * 3 bytes = 120 bytes */
	.period_bytes_min	= PLOYTEC_PLAYBACK_FRAMES * 4 * 3,
	.period_bytes_max	= 512 * 1024,
	.periods_min		= 2,
	.periods_max		= 1024,
};

static const struct snd_pcm_hardware jockey3_pcm_hw_capture = {
	.info			= JOCKEY3_PCM_INFO,
	.formats		= SNDRV_PCM_FMTBIT_S24_3LE,
	.rates			= JOCKEY3_PCM_RATES,
	.rate_min		= 44100,
	.rate_max		= 96000,
	.channels_min		= 6,
	.channels_max		= 6,
	.buffer_bytes_max	= 1024 * 1024,
	/* One capture URB carries 8 frames * 6 channels * 3 bytes = 144 bytes */
	.period_bytes_min	= PLOYTEC_CAPTURE_FRAMES * 6 * 3,
	.period_bytes_max	= 512 * 1024,
	.periods_min		= 2,
	.periods_max		= 1024,
};

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

	runtime->hw = substream->stream == SNDRV_PCM_STREAM_PLAYBACK ?
		      jockey3_pcm_hw_playback : jockey3_pcm_hw_capture;

	/* The period accounting assumes a whole number of periods per buffer */
	ret = snd_pcm_hw_constraint_integer(runtime, SNDRV_PCM_HW_PARAM_PERIODS);
	if (ret < 0)
		return ret;

	/*
	 * Rate constraints under rate_mutex, which also excludes a concurrent
	 * rate change in jockey3_pcm_hw_params(). Re-check for disconnect here:
	 * the check above raced with anything that happened while we were not
	 * holding the mutex.
	 */
	scoped_guard(mutex, &chip->rate_mutex) {
		if (jockey3_is_disconnected(chip))
			return -ENODEV;

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

	/*
	 * No drain is needed here: the ALSA core has already run .sync_stop via
	 * snd_pcm_release_substream() -> snd_pcm_drop() -> do_hw_free(), which is
	 * also where runtime->dma_area is released.
	 */
	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		urb_stream->substream = NULL;
		urb_stream->running = false;
	}

	return 0;
}

/*
 * Wait for every URB completion that could still touch this stream's substream
 * or runtime->dma_area to leave its safe zone.
 *
 * The ALSA core calls this from snd_pcm_do_prepare() and from do_hw_free()
 * *before* snd_pcm_lib_free_pages() releases the buffer, and only when the
 * stream actually ran (runtime->stop_operating). It runs in process context
 * with no stream lock held, so sleeping here is safe: a callback in its safe
 * zone has dropped urb_stream->lock and may freely take the stream lock, so the
 * stream lock -> urb_stream->lock order is never inverted.
 */
static int jockey3_pcm_sync_stop(struct snd_pcm_substream *substream)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);
	long remaining;

	spin_lock_irq(&urb_stream->lock);
	remaining = wait_event_lock_irq_timeout(urb_stream->drain_wait,
						urb_stream->callbacks_active == 0,
						urb_stream->lock,
						msecs_to_jiffies(1000));
	spin_unlock_irq(&urb_stream->lock);

	if (!remaining)
		dev_err(&chip->intf0->dev,
			"Timeout draining %s URB callbacks\n",
			substream->stream == SNDRV_PCM_STREAM_PLAYBACK ? "playback" : "capture");

	return 0;
}

static int jockey3_pcm_prepare(struct snd_pcm_substream *substream)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);
	bool needs_recovery = false;
	int ret = 0;

	dev_dbg(&chip->intf0->dev, "PCM prepare stream %d\n", substream->stream);
	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	ret = jockey3_wait_for_reset_completion(chip);
	if (ret < 0)
		return ret;

	/*
	 * Taking rate_mutex here serializes against an in-flight rate change in
	 * jockey3_pcm_hw_params(), which holds it across the whole stop/set/start
	 * sequence -- so the capture liveness below is sampled from a settled
	 * state rather than from the middle of a URB restart.
	 */
	scoped_guard(mutex, &chip->rate_mutex) {
		if (jockey3_is_disconnected(chip))
			return -ENODEV;

		/*
		 * Capture may have been left stalled by an earlier rate change that
		 * happened while no capture stream was open (see jockey3_pcm_hw_params()).
		 * Catch it here, before this newly-opened capture stream starts relying
		 * on it.
		 */
		needs_recovery = substream->stream == SNDRV_PCM_STREAM_CAPTURE &&
				 !jockey3_check_urb_steam_alive(&chip->capture);
	}

	/*
	 * Recovery runs outside rate_mutex on purpose: it may escalate to a
	 * queued USB reset, and jockey3_pre_reset()/jockey3_post_reset() need to
	 * acquire the mutex themselves to complete.
	 */
	if (needs_recovery) {
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

/*
 * Called by the ALSA core with the substream stream lock held and interrupts
 * disabled, so this callback must never sleep. In particular it must not wait
 * for a rate change or a device reset: a URB completion can re-enter here via
 * snd_pcm_period_elapsed() -> snd_pcm_stop_xrun(), and blocking would then
 * stall the very rate change it is waiting on.
 *
 * Serialization against a concurrent rate change is provided by rate_mutex in
 * the sleepable callbacks instead; the ALSA state machine guarantees .prepare
 * runs before TRIGGER_START and after every XRUN.
 *
 * Lock order here is snd_pcm_stream_lock -> urb_stream->lock, which is why the
 * URB callbacks drop urb_stream->lock before calling snd_pcm_period_elapsed().
 */
static int jockey3_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	struct jockey3_pcm_urb_stream *urb_stream =
		jockey3_get_pcm_urb_stream(chip, substream->stream);

	dev_dbg(&chip->intf0->dev, "PCM trigger stream %d, cmd %d\n", substream->stream, cmd);

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		switch (cmd) {
		case SNDRV_PCM_TRIGGER_START:
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

static int jockey3_initialize_ploytec(struct jockey3_chip *chip)
{
	enum ploytec_codec_variant codec_variant;
	int ret;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	codec_variant = ploytec_initialize_codec();
	switch (codec_variant) {
	case PLOYTEC_CODEC_PORTABLE:
		dev_dbg(&chip->intf0->dev, "Using portable codec\n");
		break;
	case PLOYTEC_CODEC_OPTIMIZED_64BIT:
		dev_dbg(&chip->intf0->dev, "Using 64-bit optimized codec\n");
		break;
	case PLOYTEC_CODEC_OPTIMIZED_32BIT:
		dev_dbg(&chip->intf0->dev, "Using 32-bit optimized codec\n");
		break;
	}

	ret = ploytec_initialize_device(chip->intf0, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Ploytec failed to initialize: %d\n", ret);
		return ret;
	}

	ret = ploytec_start_streaming(chip->intf0, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Ploytec failed to start streaming: %d\n", ret);
		return ret;
	}

	dev_dbg(&chip->intf0->dev, "Ploytec initialized successfully; status = 0x%02x\n", ret);
	return 0;
}

static int jockey3_pcm_hw_params(struct snd_pcm_substream *substream,
				 struct snd_pcm_hw_params *hw_params)
{
	struct jockey3_chip *chip = snd_pcm_substream_chip(substream);
	unsigned int rate = params_rate(hw_params);
	bool playback_alive, capture_alive, capture_open;
	int ret = 0;

	dev_dbg(&chip->intf0->dev, "PCM hw_params rate %u, active_streams %d\n",
		rate, jockey3_active_streams(chip));

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	/*
	 * rate_mutex is held across the whole stop/set-rate/start sequence, which
	 * is what excludes a concurrent rate change from another substream: any
	 * other sleepable path that needs a settled rate takes the same mutex.
	 */
	scoped_guard(mutex, &chip->rate_mutex) {
		if (jockey3_is_disconnected(chip))
			return -ENODEV;

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

		jockey3_stop_urbs(chip);

		ret = jockey3_set_rate(chip, rate);
		if (ret != 0) {
			dev_err(&chip->intf0->dev, "Rate change to %u failed: %d\n", rate, ret);
			jockey3_start_urbs(chip);
			return ret;
		}

		jockey3_set_current_rate(chip, rate);

		jockey3_start_urbs(chip);
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
		reinit_completion(&chip->reset_done);
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
	.sync_stop = jockey3_pcm_sync_stop,
	.pointer = jockey3_pcm_pointer,
};

static int jockey3_midi_in_open(struct snd_rawmidi_substream *substream)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;
	return 0;
}

/*
 * Normally trigger(0) has already cleared the pointer, but the rawmidi core can
 * reach close without it (an interrupted drain, or close_substream() with
 * cleanup suppressed), so drop it here too rather than leave a dangling
 * reference for the URB callback.
 */
static int jockey3_midi_in_close(struct snd_rawmidi_substream *substream)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	guard(spinlock_irqsave)(&chip->midi_lock);
	if (chip->midi_in_substream == substream)
		chip->midi_in_substream = NULL;

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

/* See jockey3_midi_in_close() for why the pointer is cleared here as well. */
static int jockey3_midi_out_close(struct snd_rawmidi_substream *substream)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;

	guard(spinlock_irqsave)(&chip->midi_lock);
	if (chip->midi_out_substream == substream)
		chip->midi_out_substream = NULL;

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

static int jockey3_initialize(struct jockey3_chip *chip)
{
	int ret;
	int rate;

	for (int retry = 10; retry > 0; retry--) {
		ret = jockey3_initialize_ploytec(chip);
		if (ret == 0)
			break;
		usleep_range(50000, 100000); /* Wait 50-100 ms before retrying */
	}
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to initialize Ploytec: %d\n", ret);
		return ret;
	}

	rate = 44100;	// default sample rate at power-on
	scoped_guard(mutex, &chip->rate_mutex)
		jockey3_set_current_rate(chip, rate);

	ret = jockey3_set_rate(chip, rate);
	if (ret < 0)
		return ret;

	jockey3_start_urbs(chip);

	dev_dbg(&chip->intf0->dev, "Initialization complete.\n");

	return 0;
}

static void jockey3_release_dev_idx(void *data)
{
	struct jockey3_chip *chip = data;

	guard(mutex)(&jockey3_devices_mutex);
	__clear_bit(chip->dev_idx, jockey3_devices_used);
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

		jockey3_prepare_idle_out_packet(chip->playback.bufs[i]);

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

/*
 * Channel maps.
 *
 * This device exposes several independent stereo pairs, which is a different
 * thing from a multichannel speaker arrangement -- and speaker positions are
 * all the chmap enum can express. Claiming, say, RL/RR for the headphone pair
 * would invite an audio server to treat a cue output as rear speakers, so the
 * discrete pairs are reported as SNDRV_CHMAP_UNKNOWN.
 *
 * The one exception is the playback Master pair, which is marked FL/FR so that
 * userspace (PipeWire and friends) can identify the device's primary output.
 * There is no equivalent notion for the inputs, so capture is left entirely
 * unpositioned.
 *
 * Physical layout (see Documentation/sound/cards/jockey3.rst):
 *   Playback: 1-2 Master Out L/R, 3-4 Headphone L/R
 *   Capture:  1-2 Input 1 L/R, 3-4 Input 2 L/R, 5-6 Microphone
 *
 * The microphone is mono: the balanced input stage feeds the same analog
 * signal to both converters, so channels 5 and 6 carry identical content.
 */
static const struct snd_pcm_chmap_elem jockey3_playback_chmap[] = {
	{ .channels = 4,
	  .map = { SNDRV_CHMAP_FL, SNDRV_CHMAP_FR,
		   SNDRV_CHMAP_UNKNOWN, SNDRV_CHMAP_UNKNOWN } },
	{ }
};

static const struct snd_pcm_chmap_elem jockey3_capture_chmap[] = {
	{ .channels = 6,
	  .map = { SNDRV_CHMAP_UNKNOWN, SNDRV_CHMAP_UNKNOWN,
		   SNDRV_CHMAP_UNKNOWN, SNDRV_CHMAP_UNKNOWN,
		   SNDRV_CHMAP_UNKNOWN, SNDRV_CHMAP_UNKNOWN } },
	{ }
};

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

	ret = snd_pcm_add_chmap_ctls(chip->pcm, SNDRV_PCM_STREAM_PLAYBACK,
				     jockey3_playback_chmap, 4, 0, NULL);
	if (ret < 0)
		return ret;

	return snd_pcm_add_chmap_ctls(chip->pcm, SNDRV_PCM_STREAM_CAPTURE,
				      jockey3_capture_chmap, 6, 0, NULL);
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

	/*
	 * card->driver is only char[16] and is what shows up as the card ID in
	 * /proc/asound, so it holds a short model identifier rather than the
	 * module name (which would be silently truncated).
	 */
	strscpy(chip->card->driver, "Jockey3", sizeof(chip->card->driver));
	strscpy(chip->card->shortname, CARD_NAME, sizeof(chip->card->shortname));
	strscpy(chip->card->mixername, CARD_NAME, sizeof(chip->card->mixername));

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
	unsigned int dev_idx;
	int ret;

	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;

	intf1 = usb_ifnum_to_if(dev, 1);
	if (!intf1)
		return -ENODEV;

	ret = jockey3_validate_endpoints(intf, intf1);
	if (ret < 0)
		return ret;

	/* Claim the first enabled, unused card slot */
	scoped_guard(mutex, &jockey3_devices_mutex) {
		for (dev_idx = 0; dev_idx < SNDRV_CARDS; dev_idx++)
			if (enable[dev_idx] && !test_bit(dev_idx, jockey3_devices_used))
				break;

		if (dev_idx >= SNDRV_CARDS)
			return -ENODEV;

		__set_bit(dev_idx, jockey3_devices_used);
	}

	ret = snd_devm_card_new(&intf->dev, index[dev_idx], id[dev_idx], THIS_MODULE,
				sizeof(struct jockey3_chip), &card);
	if (ret < 0)
		goto err_free_idx;

	chip = card->private_data;
	chip->dev_idx = dev_idx;
	ret = devm_add_action_or_reset(&intf->dev, jockey3_release_dev_idx, chip);
	if (ret)
		return ret;

	chip->card = card;
	chip->dev = dev;
	chip->intf0 = intf;
	chip->intf1 = intf1;
	chip->flags = 0;
	spin_lock_init(&chip->midi_lock);
	spin_lock_init(&chip->playback.lock);
	spin_lock_init(&chip->capture.lock);
	mutex_init(&chip->rate_mutex);
	init_completion(&chip->reset_done);

	init_usb_anchor(&chip->playback.anchor);
	init_usb_anchor(&chip->capture.anchor);
	init_waitqueue_head(&chip->playback.drain_wait);
	init_waitqueue_head(&chip->capture.drain_wait);
	/*
	 * card->private_data is zeroed by snd_devm_card_new(), but be explicit:
	 * these two are load-bearing for the stop/stall bookkeeping.
	 */
	atomic_set(&chip->playback.urbs_in_flight, 0);
	atomic_set(&chip->capture.urbs_in_flight, 0);
	atomic64_set(&chip->playback.last_callback_time, 0);
	atomic64_set(&chip->capture.last_callback_time, 0);

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

	/*
	 * Claim interface 1 before registering the URB-stop action. devres
	 * unwinds LIFO, so registering in this order means the URBs are killed
	 * *before* the interface owning the capture endpoint (0x86) is released.
	 */
	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
	if (ret < 0)
		return ret;
	ret = devm_add_action_or_reset(&intf->dev, jockey3_release_intf1, intf1);
	if (ret)
		return ret;

	/* Stop all URBs on disconnect */
	ret = devm_add_action_or_reset(&intf->dev, jockey3_stop_urbs_action, chip);
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

	usb_set_intfdata(intf, chip);
	ret = jockey3_initialize(chip);
	if (ret < 0)
		return ret;

	ret = snd_card_register(card);
	if (ret < 0)
		return ret;

	return 0;

err_free_idx:
	/*
	 * Only reached before the card exists; past that point the slot is
	 * released by the jockey3_release_dev_idx() devres action.
	 */
	scoped_guard(mutex, &jockey3_devices_mutex)
		__clear_bit(dev_idx, jockey3_devices_used);
	return ret;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		/*
		 * Latch DISCONNECTED first. Every ALSA entry point tests it on the
		 * way in, so setting it before anything else is torn down closes
		 * the window where e.g. jockey3_pcm_hw_params() had already passed
		 * its check and would go on to resubmit URBs we just killed.
		 */
		set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
		clear_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		/*
		 * Release anyone blocked in jockey3_wait_for_reset_completion():
		 * a failed reset unbinds the interface instead of calling
		 * jockey3_post_reset(), so this is the only wakeup they get.
		 */
		complete_all(&chip->reset_done);

		jockey3_stop_urbs(chip);
		/*
		 * snd_card_disconnect() runs snd_pcm_stop(DISCONNECTED) under the
		 * stream lock, which drives our .trigger and clears 'running' with
		 * the proper locking -- so there is nothing to clear here by hand.
		 *
		 * Card cleanup, URB freeing, and interface release are all handled
		 * automatically by devres.
		 */
		snd_card_disconnect(chip->card);
	}
	usb_set_intfdata(intf, NULL);
}

/*
 * rate_mutex is taken and released within each of pre_reset()/post_reset()
 * rather than being held across the reset. The USB core does not guarantee
 * post_reset() runs at all: if the reset fails, the interface is marked for
 * rebinding and unbound instead, so a lock handed off from pre_reset() would
 * be leaked permanently and every later PCM ioctl would block on it forever.
 *
 * The window between the two callbacks is not left unguarded: the device is
 * physically in reset, so every EP0 transfer fails and is error-checked, and
 * JOCKEY3_FLAG_RESETTING gates the ALSA entry points.
 */
static int jockey3_pre_reset(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		set_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		scoped_guard(mutex, &chip->rate_mutex)
			jockey3_stop_urbs(chip);
	}
	return 0;
}

static int jockey3_post_reset(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);
	u32 hw_rate = 0;

	if (chip && intf == chip->intf0) {
		scoped_guard(mutex, &chip->rate_mutex) {
			jockey3_initialize_ploytec(chip);

			/* Verify if the sample rate persisted through the reset */
			if (ploytec_get_rate(chip->intf0, chip->xfer_buf, &hw_rate) == 0) {
				if (hw_rate != chip->current_rate) {
					dev_warn(&chip->intf0->dev,
						 "Rate mismatch after reset. HW: %u, Expected: %u. Re-applying...\n",
						 hw_rate, chip->current_rate);
					jockey3_set_rate(chip, chip->current_rate);
				} else {
					dev_dbg(&chip->intf0->dev,
						"Rate %u Hz persisted through reset\n", hw_rate);
				}
			}

			jockey3_start_urbs(chip);
		}

		clear_bit(JOCKEY3_FLAG_RESETTING, &chip->flags);
		complete_all(&chip->reset_done);
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

		/*
		 * Stop the physical URBs under rate_mutex, matching
		 * jockey3_restore_device() on the resume side -- otherwise a
		 * suspend could land in the middle of a rate change.
		 */
		scoped_guard(mutex, &chip->rate_mutex)
			jockey3_stop_urbs(chip);
	}
	return 0;
}

static int jockey3_restore_device(struct jockey3_chip *chip, bool reset)
{
	int ret;

	scoped_guard(mutex, &chip->rate_mutex) {
		if (reset) {
			ret = jockey3_initialize_ploytec(chip);
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

MODULE_AUTHOR("Frank van de Pol <fvdpol@gmail.com>");
MODULE_DESCRIPTION(CARD_NAME " ALSA Driver");
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: snd-pcm snd-rawmidi");
