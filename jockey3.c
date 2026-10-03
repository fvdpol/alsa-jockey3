// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/types.h>
#include <linux/atomic.h>
#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>
#include <linux/delay.h>
#include <linux/jiffies.h>
#include <linux/bitops.h>
#include <linux/mutex.h>
#include <linux/wait.h>
#include <linux/cleanup.h>
#include <sound/core.h>
#include <sound/initval.h>
#include <sound/pcm.h>
#include "ploytec_proto.h"
#include "ploytec_codec.h"

#define RELOOP_VENDOR_ID         0x200c
#define RELOOP_JOCKEY3_ME_PID    0x1019
#define RELOOP_JOCKEY3_REMIX_PID 0x1037

enum { JOCKEY3_ME, JOCKEY3_REMIX };
#define CARD_NAME "Reloop Jockey 3"

/* Human-readable edition name for a usb_device_id.driver_info value. */
static const char *jockey3_model_name(int model)
{
	switch (model) {
	case JOCKEY3_ME:
		return "Master Edition";
	case JOCKEY3_REMIX:
		return "Remix";
	default:
		return "Unknown";
	}
}

static int index[SNDRV_CARDS] = SNDRV_DEFAULT_IDX;
static char *id[SNDRV_CARDS] = SNDRV_DEFAULT_STR;
static bool enable[SNDRV_CARDS] = SNDRV_DEFAULT_ENABLE_PNP;

module_param_array(index, int, NULL, 0444);
MODULE_PARM_DESC(index, "Index value for " CARD_NAME " soundcard.");
module_param_array(id, charp, NULL, 0444);
MODULE_PARM_DESC(id, "ID string for " CARD_NAME " soundcard.");
module_param_array(enable, bool, NULL, 0444);
MODULE_PARM_DESC(enable, "Enable " CARD_NAME " soundcard.");

/**
 * DOC: Reverse-engineering references
 *
 * Comments below cite documents by paths beginning "re/". Those are the
 * reverse-engineering notes this driver was derived from -- USB protocol
 * analysis, vendor trace comparisons and the experiments behind several of the
 * timing constants. They are not part of the kernel tree: they live in the
 * driver's development repository, linked from its MAINTAINERS entry.
 */

/**
 * DOC: Device model
 *
 * The Reloop Jockey 3 presents two USB interfaces and speaks a proprietary
 * Ploytec protocol rather than USB Audio Class. Interface 0 is claimed by
 * probe(); interface 1 is claimed explicitly, as it owns the capture endpoint.
 * Three bulk endpoints carry everything:
 *
 * - EP 0x05 OUT: PCM playback, with the MIDI OUT byte stream multiplexed into
 *   a reserved slot of every packet (see PLOYTEC_MIDI_OUT_OFFSET)
 * - EP 0x86 IN:  PCM capture
 * - EP 0x83 IN:  MIDI input
 *
 * Audio is not sample-interleaved but bit-plane interleaved; see
 * ploytec_codec.c for the wire format and the encode/decode implementations.
 *
 * URBs run free for the lifetime of the device rather than being started and
 * stopped around PCM use: the playback stream must keep flowing because it
 * carries MIDI OUT, and the device expects a continuous packet stream. The PCM
 * callbacks therefore only toggle whether a URB's payload is filled from (or
 * copied to) an ALSA buffer.
 */

/**
 * DOC: Resource lifetime
 *
 * The card is created with snd_card_new(), not snd_devm_card_new(), and
 * released by jockey3_disconnect() with snd_card_free_when_closed(). A managed
 * card would instead be freed from the devres unwind inside
 * usb_unbind_interface(), where snd_card_free() blocks until userspace has
 * closed every file descriptor on the card -- on an unplug that unwind runs on
 * the USB hub work queue, so a single process sitting on a PCM fd would stall
 * hotplug for the whole hub. Every other USB sound driver in the tree avoids
 * that the same way.
 *
 * That splits teardown in two, and the split is the thing to keep straight:
 *
 * - jockey3_disconnect() does everything that must happen while the device is
 *   still there: latch JOCKEY3_FLAG_DISCONNECTED (for either interface, since
 *   the core takes them down one at a time), kill the URBs, release
 *   interface 1, and hand the card to snd_card_free_when_closed().
 *
 * - jockey3_card_free(), reached through card->private_free once the last file
 *   descriptor is closed, frees the buffers and URBs and gives the card slot
 *   back. It runs arbitrarily long after the disconnect and must attempt no
 *   USB operation, though the objects themselves stay referenced until it
 *   returns; see jockey3_free_resources().
 *
 * struct jockey3_chip lives in card->private_data, so it is freed with the
 * card and outlives the USB binding -- which is what makes it safe for an ALSA
 * callback that is still in flight during an unplug to keep dereferencing it.
 * Such a callback finds JOCKEY3_FLAG_DISCONNECTED set and returns -ENODEV.
 *
 * probe's error path performs the disconnect half by hand and ends the same
 * way, with snd_card_free_when_closed(): a failing snd_card_register() may
 * already have created the device nodes, so userspace can be holding the card
 * open even there.
 */

/**
 * DOC: Locking
 *
 * There are three locks, all IRQ-safe leaf spinlocks: playback.lock,
 * capture.lock and midi_lock. They are never nested inside one another;
 * anything that needs more than one takes them in sequence, not nested.
 *
 * Against the ALSA core the order is::
 *
 *     snd_pcm_stream_lock  ->  playback.lock / capture.lock
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

/**
 * struct jockey3_pcm_urb_stream - per-direction PCM streaming state
 * @substream: the open ALSA substream, or NULL; @lock
 * @anchor: anchor holding the submitted URBs, for stop/kill
 * @urbs: the URB ring
 * @bufs: transfer buffer for each URB
 * @urbs_in_flight: number of submitted URBs; diagnostic, must reach 0 after a stop
 * @lock: protects the fields marked "@lock" below; IRQ-safe leaf
 * @dma_off: byte offset into runtime->dma_area, i.e. the hardware pointer; @lock
 * @period_off: bytes accumulated towards the current period; @lock
 * @running: stream is triggered and its payload should be filled; @lock
 * @callbacks_active: number of URB completions currently inside the "safe zone"
 *	where they may still touch @substream or runtime->dma_area; @lock. The
 *	last one out wakes @drain_wait. A count rather than a flag because there
 *	are JOCKEY3_N_URBS URBs per direction and their completions can overlap
 *	on SMP.
 * @drain_wait: waited on by jockey3_pcm_sync_stop() until @callbacks_active is 0
 * @stopping: set by jockey3_stop_urbs() before the anchor is killed, cleared by
 *	jockey3_start_urbs(); @lock. Tested by the completion handler inside the
 *	same critical section that anchors and resubmits, so a callback can never
 *	re-anchor a URB after the kill has drained the anchor.
 * @consec_errors: consecutive URB transport errors; @lock. Reset on any
 *	successful completion and by jockey3_start_urbs().
 */
struct jockey3_pcm_urb_stream {
	struct snd_pcm_substream *substream;
	struct usb_anchor anchor;
	struct urb *urbs[JOCKEY3_N_URBS];
	unsigned char *bufs[JOCKEY3_N_URBS];
	atomic_t urbs_in_flight;
	spinlock_t lock;	/* protects this stream's state; IRQ-safe leaf */
	unsigned int dma_off;
	unsigned int period_off;
	bool running;
	unsigned int callbacks_active;
	wait_queue_head_t drain_wait;
	bool stopping;
	unsigned int consec_errors;
};

/**
 * struct jockey3_chip - per-device driver state
 * @card: the ALSA card; read-only after probe
 * @dev: the USB device; read-only after probe. Referenced for the chip's
 *	lifetime, as @intf0 and @intf1 are, because the card can outlive the
 *	unbind and usb_disconnect() would otherwise free them underneath an ALSA
 *	entry point. Released by jockey3_free_resources().
 * @intf0: interface 0, which the driver is bound to; read-only after probe
 * @intf1: interface 1, claimed explicitly because it owns EP 0x86
 * @pcm: the PCM device; read-only after probe
 * @xfer_buf: bounce buffer for EP0 control transfers, USB_XFER_BUF_SIZE bytes.
 * @flags: JOCKEY3_FLAG_* bits, atomic bitops
 * @dev_idx: card slot held in jockey3_devices_used
 * @midi_in_urb: the single MIDI IN URB; not anchored, killed directly
 * @midi_in_buf: transfer buffer for @midi_in_urb
 * @midi_lock: protects the MIDI fields; IRQ-safe leaf
 * @midi_stopping: see jockey3_pcm_urb_stream.stopping; @midi_lock
 * @midi_consec_errors: consecutive MIDI IN URB errors; @midi_lock
 * @playback: playback streaming state
 * @capture: capture streaming state
 */
struct jockey3_chip {
	struct snd_card *card;
	struct usb_device *dev;
	struct usb_interface *intf0;
	struct usb_interface *intf1;
	struct snd_pcm *pcm;
	unsigned char *xfer_buf;
	unsigned long flags;
	unsigned int dev_idx;

	/* MIDI Path */
	struct urb *midi_in_urb;
	unsigned char *midi_in_buf;
	spinlock_t midi_lock;	/* protects the MIDI fields; IRQ-safe leaf */
	bool midi_stopping;
	unsigned int midi_consec_errors;

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

/*
 * jockey3_claim_slot() and jockey3_release_slot() are separate functions so
 * that jockey3_probe(), which unwinds with goto, holds no scope-based cleanup
 * of its own: the cleanup helpers and goto are not to be mixed in one function.
 */

/**
 * jockey3_claim_slot - take the first enabled, unused card slot
 *
 * Return: the slot index, or -ENODEV if every enabled slot is in use.
 */
static int jockey3_claim_slot(void)
{
	unsigned int dev_idx;

	guard(mutex)(&jockey3_devices_mutex);

	for (dev_idx = 0; dev_idx < SNDRV_CARDS; dev_idx++) {
		if (enable[dev_idx] && !test_bit(dev_idx, jockey3_devices_used)) {
			__set_bit(dev_idx, jockey3_devices_used);
			return dev_idx;
		}
	}
	return -ENODEV;
}

static void jockey3_release_slot(unsigned int dev_idx)
{
	guard(mutex)(&jockey3_devices_mutex);
	__clear_bit(dev_idx, jockey3_devices_used);
}

/* Chip flags */
#define JOCKEY3_FLAG_DISCONNECTED	0

static inline bool jockey3_is_disconnected(const struct jockey3_chip *chip)
{
	return test_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
}

static inline struct jockey3_pcm_urb_stream *jockey3_get_pcm_urb_stream(struct jockey3_chip *chip,
									const int direction)
{
	if (direction == SNDRV_PCM_STREAM_PLAYBACK)
		return &chip->playback;
	else
		return &chip->capture;
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

/**
 * jockey3_warn_unexpected_stop() - report a URB that someone else cancelled
 * @chip: driver state
 * @stopping: whether this direction's teardown fence was set
 * @status: the urb->status that retired the URB
 * @type: direction name, for the log message
 *
 * -ENOENT, -ECONNRESET and -ESHUTDOWN normally mean the driver killed the URB
 * itself, and jockey3_urb_check() maps them to JOCKEY3_URB_STOPPED on that
 * assumption. Nothing verifies it: the USB core flushes an endpoint's URBs from
 * usb_disable_endpoint(), so an alt-setting change or an endpoint teardown
 * started anywhere else retires the whole ring by the same route, and every URB
 * of the direction would return here without a word.
 *
 * A physical unplug arrives here too and is not worth reporting.
 * usb_disconnect() moves the device to USB_STATE_NOTATTACHED before unbinding
 * the interfaces, so URBs that complete ahead of jockey3_disconnect() are
 * recognized by that rather than mistaken for an unexplained teardown.
 *
 * This is defensive. No failure observed so far has been traced to this path.
 */
static void jockey3_warn_unexpected_stop(struct jockey3_chip *chip, bool stopping,
					 int status, const char *type)
{
	if (stopping || jockey3_is_disconnected(chip))
		return;

	if (chip->dev->state == USB_STATE_NOTATTACHED)
		return;

	/* Ratelimited: a whole ring of URBs retires together */
	dev_warn_ratelimited(&chip->intf0->dev,
			     "%s URB cancelled without a driver-initiated stop: %d\n",
			     type, status);
}

/**
 * jockey3_report_xrun() - tell userspace this direction lost its data
 * @urb_stream: the affected direction
 *
 * Reports an xrun on the open substream, if there is one. Nothing else in the
 * driver can do this safely by hand: snd_pcm_stop_xrun() takes the stream lock,
 * and the documented order is snd_pcm_stream_lock -> urb_stream->lock, so the
 * call has to be made with our spinlock dropped. Between dropping it and taking
 * it again the substream could be freed underneath us, which is what the
 * callbacks_active "safe zone" prevents -- jockey3_pcm_sync_stop() waits for
 * that count to reach zero before the ALSA core releases the buffer.
 *
 * Callers must hold neither @urb_stream->lock nor any driver mutex.
 */
static void jockey3_report_xrun(struct jockey3_pcm_urb_stream *urb_stream)
{
	struct snd_pcm_substream *substream = NULL;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		if (urb_stream->running && urb_stream->substream) {
			/* Join the safe zone so the substream cannot be freed below */
			urb_stream->callbacks_active++;
			substream = urb_stream->substream;
		}
	}

	if (!substream)
		return;

	snd_pcm_stop_xrun(substream);

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		if (!--urb_stream->callbacks_active)
			wake_up(&urb_stream->drain_wait);
	}
}

/**
 * jockey3_err_device_gone() - did the device simply leave?
 * @err: a transfer's return value
 *
 * Keyed on the error code, not on jockey3_is_disconnected(): a failed reset
 * also leaves the device %USB_STATE_NOTATTACHED and sets that flag on its way
 * to unbinding, and that one is a fault worth logging.
 *
 * Return: true if @err means the device is gone rather than misbehaving.
 */
static bool jockey3_err_device_gone(int err)
{
	return err == -ENODEV || err == -ESHUTDOWN;
}

/*
 * An unplug fails every in-flight URB's resubmit at once, so demote that case
 * as the submit and rate paths do. Log level only: @consec_errors comes from
 * jockey3_urb_error_give_up() and the completion status, not from here.
 */
static void jockey3_resubmit_failed(struct jockey3_chip *chip, int err, const char *what)
{
	if (jockey3_err_device_gone(err)) {
		dev_dbg(&chip->intf0->dev,
			"Not resubmitting the %s URB: device is gone (%d)\n", what, err);
		return;
	}

	dev_err(&chip->intf0->dev, "Failed to resubmit %s URB: %d\n", what, err);
}

/**
 * jockey3_urb_error_give_up() - account for a URB transport error
 * @chip: driver state
 * @urb_stream: the affected direction
 * @status: the urb->status that was seen
 * @type: direction name, for log messages
 *
 * Accounts for a transport error (-EPROTO, -EPIPE, -EOVERFLOW, -ETIME, ...).
 *
 * These are frequently transient -- marginal cabling and some host controllers
 * produce them routinely -- so a single one must not disable the card. Keep
 * resubmitting while the consecutive count stays below JOCKEY3_MAX_URB_ERRORS;
 * beyond that, stop feeding this direction.
 *
 * Return: true if the caller should give up and not resubmit.
 */
static bool jockey3_urb_error_give_up(struct jockey3_chip *chip,
				      struct jockey3_pcm_urb_stream *urb_stream,
				      int status, const char *type)
{
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

	jockey3_report_xrun(urb_stream);

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
	bool stopping;
	int ret;

	atomic_dec(&urb_stream->urbs_in_flight);

	switch (jockey3_urb_check(urb)) {
	case JOCKEY3_URB_STOPPED:
		scoped_guard(spinlock_irqsave, &urb_stream->lock)
			stopping = urb_stream->stopping;
		jockey3_warn_unexpected_stop(chip, stopping, urb->status, "Capture");
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

	if (data_valid && unlikely(urb->actual_length < PLOYTEC_PKT_SIZE)) {
		dev_err(&chip->intf0->dev, "Capture URB too small: %d; required at least %d\n",
			urb->actual_length, PLOYTEC_PKT_SIZE);
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
			substream = urb_stream->substream;

			period_elapsed = jockey3_process_in_packet(chip, urb->transfer_buffer);
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
		jockey3_resubmit_failed(chip, ret, "capture");
}

/*
 * Fill the sample area of a playback packet with silence, for when there is
 * no PCM data to send.
 *
 * Only the sample area is touched: everything from PLOYTEC_MIDI_OUT_OFFSET
 * onwards -- the MIDI slot, the sync byte and the trailing gap -- is rewritten
 * unconditionally by jockey3_playback_callback() immediately afterwards.
 */
static void jockey3_silence_out_packet(u8 *buf)
{
	memset(buf, 0, PLOYTEC_MIDI_OUT_OFFSET);
}

/*
 * Prime a freshly allocated playback buffer so the first URB, which is
 * submitted before any completion handler has run, carries a valid idle
 * packet. The buffer comes from kzalloc(), so the sample area and the
 * trailing gap are already silent.
 */
static void jockey3_init_out_packet(u8 *buf)
{
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
	bool stopping;
	int i, ret;

	atomic_dec(&urb_stream->urbs_in_flight);

	switch (jockey3_urb_check(urb)) {
	case JOCKEY3_URB_STOPPED:
		scoped_guard(spinlock_irqsave, &urb_stream->lock)
			stopping = urb_stream->stopping;
		jockey3_warn_unexpected_stop(chip, stopping, urb->status, "Playback");
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
			substream = urb_stream->substream;

			period_elapsed = jockey3_process_out_packet(chip, buf);
		} else {
			jockey3_silence_out_packet(buf);
		}
	}

	/*
	 * Every packet reserves one slot for a MIDI OUT byte. Nothing feeds it
	 * yet, so it carries the idle byte.
	 */
	buf[PLOYTEC_MIDI_OUT_OFFSET] = PLOYTEC_MIDI_IDLE_BYTE;

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
		jockey3_resubmit_failed(chip, ret, "playback");
}

static void jockey3_midi_in_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	unsigned int errors;
	bool stopping;
	int ret;

	switch (jockey3_urb_check(urb)) {
	case JOCKEY3_URB_STOPPED:
		scoped_guard(spinlock_irqsave, &chip->midi_lock)
			stopping = chip->midi_stopping;
		jockey3_warn_unexpected_stop(chip, stopping, urb->status, "MIDI IN");
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
		break;
	case JOCKEY3_URB_OK:
		scoped_guard(spinlock_irqsave, &chip->midi_lock)
			chip->midi_consec_errors = 0;
		break;
	}

	if (unlikely(jockey3_is_disconnected(chip)))
		return;

	/*
	 * Nothing consumes the payload yet, but the endpoint is kept polled: the
	 * device is only ever run with all three bulk endpoints serviced.
	 */
	ret = 0;
	scoped_guard(spinlock_irqsave, &chip->midi_lock) {
		if (!chip->midi_stopping && !jockey3_is_disconnected(chip))
			ret = usb_submit_urb(urb, GFP_ATOMIC);
	}
	if (ret < 0)
		jockey3_resubmit_failed(chip, ret, "MIDI IN");
}

/**
 * jockey3_stop_urbs() - stop all PCM and MIDI URBs
 * @chip: driver state
 *
 * Fences the completion handlers, then kills every URB. Sleeps, so it must not
 * be called from atomic context. On return no completion handler is running and
 * none can resubmit.
 */
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
	 *
	 * Worth spelling out for the anchored form, which keeps getting read as
	 * a use-after-free: __usb_hcd_giveback_urb() does unanchor before
	 * urb->complete(), so the kill loop cannot find a running handler on the
	 * list. anchor->suspend_wakeups covers it -- raised before the unanchor
	 * and dropped after complete() returns, and usb_kill_anchored_urbs()
	 * drains until usb_anchor_check_wakeup() sees it zero.
	 */
	usb_kill_urb(chip->midi_in_urb);
	usb_kill_anchored_urbs(&chip->playback.anchor);
	usb_kill_anchored_urbs(&chip->capture.anchor);

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

/*
 * Demote an unplug. -%ENOENT stays loud: the endpoints were disabled while the
 * driver is still bound, which takes a device reset to undo.
 *
 * @slot is the ring slot, or -1 for the single MIDI IN URB.
 */
static void jockey3_submit_failed(struct jockey3_chip *chip, int err,
				  const char *what, int slot)
{
	if (jockey3_err_device_gone(err)) {
		if (slot < 0)
			dev_dbg(&chip->intf0->dev,
				"Not submitting the %s URB: device is gone (%d)\n", what, err);
		else
			dev_dbg(&chip->intf0->dev,
				"Not submitting %s URB %d: device is gone (%d)\n",
				what, slot, err);
		return;
	}

	if (slot < 0)
		dev_err(&chip->intf0->dev, "Failed to submit the %s URB: %d\n", what, err);
	else
		dev_err(&chip->intf0->dev, "Failed to submit %s URB %d: %d\n",
			what, slot, err);
}

/**
 * jockey3_start_urbs() - submit all PCM and MIDI URBs
 * @chip: driver state
 *
 * Clears the stop fences and error budgets, then submits the full URB ring for
 * both directions plus the MIDI IN URB. Uses GFP_KERNEL, so process context
 * only. A failure to submit one URB does not prevent the others being tried.
 *
 * The return value must be checked. Only a completion handler resubmits a URB,
 * so a ring that comes up short stays short: nothing retries the URBs that
 * failed here, and the direction runs at reduced depth for as long as the
 * device stays bound, with no bookkeeping that would ever notice.
 *
 * Return: 0 on success, or the first submit error encountered.
 */
static int jockey3_start_urbs(struct jockey3_chip *chip)
{
	int i, ret, first_err = 0;
	int n_playback = 0, n_capture = 0;

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
			jockey3_submit_failed(chip, ret, "playback", i);
			if (!first_err)
				first_err = ret;
		} else {
			n_playback++;
		}

		atomic_inc(&chip->capture.urbs_in_flight);
		usb_anchor_urb(chip->capture.urbs[i], &chip->capture.anchor);
		ret = usb_submit_urb(chip->capture.urbs[i], GFP_KERNEL);
		if (ret < 0) {
			atomic_dec(&chip->capture.urbs_in_flight);
			usb_unanchor_urb(chip->capture.urbs[i]);
			jockey3_submit_failed(chip, ret, "capture", i);
			if (!first_err)
				first_err = ret;
		} else {
			n_capture++;
		}
	}
	ret = usb_submit_urb(chip->midi_in_urb, GFP_KERNEL);
	if (ret < 0) {
		jockey3_submit_failed(chip, ret, "MIDI IN", -1);
		if (!first_err)
			first_err = ret;
	}

	if (n_playback < JOCKEY3_N_URBS || n_capture < JOCKEY3_N_URBS) {
		if (jockey3_err_device_gone(first_err))
			dev_dbg(&chip->intf0->dev,
				"Started only %d/%d playback and %d/%d capture URBs; device is gone\n",
				n_playback, JOCKEY3_N_URBS, n_capture, JOCKEY3_N_URBS);
		else
			dev_err(&chip->intf0->dev,
				"Started only %d/%d playback and %d/%d capture URBs; ring will not refill\n",
				n_playback, JOCKEY3_N_URBS, n_capture, JOCKEY3_N_URBS);
	}

	return first_err;
}

/*
 * Log a failed rate-programming step at the severity it deserves: an unplug
 * is not an error.
 */
static void jockey3_rate_step_failed(struct jockey3_chip *chip, int err, const char *what)
{
	if (jockey3_err_device_gone(err)) {
		dev_dbg(&chip->intf0->dev, "%s: device is gone (%d)\n", what, err);
		return;
	}

	dev_err(&chip->intf0->dev, "%s: %d\n", what, err);
}

static int jockey3_set_rate(struct jockey3_chip *chip, unsigned int rate, bool cold_init)
{
	int ret;
	u32 current_hw_rate;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	dev_dbg(&chip->intf0->dev, "Setting rate to %u Hz\n", rate);

	ret = ploytec_initialize_device(chip->intf0, chip->xfer_buf, !cold_init, NULL);
	if (ret < 0) {
		jockey3_rate_step_failed(chip, ret, "Failed to initialize device to change rate");
		return ret;
	}

	ret = ploytec_get_rate(chip->intf0, chip->xfer_buf, PLOYTEC_RATE_IDX_DEVICE,
			       &current_hw_rate);
	if (ret < 0) {
		jockey3_rate_step_failed(chip, ret, "Failed to read current hardware rate");
		return ret;
	}
	dev_dbg(&chip->intf0->dev, "Current hardware rate: %u Hz\n", current_hw_rate);

	/*
	 * Program the rate even when the device already reports it. Skipping the
	 * write on a match would silently elide it during probe every time,
	 * since initialization always asks for 44100 Hz and that is also the
	 * device's power-on default. The write evidently does more than set a
	 * frequency; no vendor initialization omits it. See
	 * re/usb/init_timing_comparison.md.
	 */
	dev_dbg(&chip->intf0->dev, "Setting hardware rate: %u Hz\n", rate);
	ret = ploytec_set_rate(chip->intf0, chip->xfer_buf, rate, cold_init);
	if (ret < 0) {
		jockey3_rate_step_failed(chip, ret, "Failed to set rate");
		return ret;
	}
	ret = ploytec_start_streaming(chip->intf0, chip->xfer_buf);
	if (ret < 0) {
		jockey3_rate_step_failed(chip, ret, "Failed to start streaming after rate change");
		return ret;
	}

	dev_dbg(&chip->intf0->dev, "Rate set OK\n");
	return 0;
}

/*
 * SNDRV_PCM_INFO_BATCH: the hardware pointer only advances once per completed
 * bulk URB (a whole Ploytec frame group), never per sample, so userspace must
 * not assume a sample-accurate position.
 */
#define JOCKEY3_PCM_INFO	(SNDRV_PCM_INFO_MMAP |		\
				 SNDRV_PCM_INFO_INTERLEAVED |	\
				 SNDRV_PCM_INFO_BLOCK_TRANSFER |\
				 SNDRV_PCM_INFO_BATCH |		\
				 SNDRV_PCM_INFO_MMAP_VALID)

#define JOCKEY3_PCM_RATES	SNDRV_PCM_RATE_44100

static const struct snd_pcm_hardware jockey3_pcm_hw_playback = {
	.info			= JOCKEY3_PCM_INFO,
	.formats		= SNDRV_PCM_FMTBIT_S24_3LE,
	.rates			= JOCKEY3_PCM_RATES,
	.rate_min		= 44100,
	.rate_max		= 44100,
	.channels_min		= 4,
	.channels_max		= 4,
	.buffer_bytes_max	= 1024 * 1024,
	/*
	 * One playback URB carries one packet of 10 frames * 4 channels * 3
	 * bytes = 120 bytes. A URB reports at most one elapsed period, so the
	 * minimum must cover at least one packet, or a period boundary inside a
	 * URB could be missed entirely.
	 */
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
	.rate_max		= 44100,
	.channels_min		= 6,
	.channels_max		= 6,
	.buffer_bytes_max	= 1024 * 1024,
	/*
	 * One capture URB carries one packet of 8 frames * 6 channels * 3 bytes
	 * = 144 bytes; see the playback .period_bytes_min comment above for why
	 * the minimum must cover at least one packet.
	 */
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

/**
 * jockey3_pcm_sync_stop() - wait for in-flight URB callbacks to drain
 * @substream: the substream being stopped
 *
 * Waits for every URB completion that could still touch this stream's substream
 * or runtime->dma_area to leave its safe zone.
 *
 * The ALSA core calls this from snd_pcm_do_prepare() and from do_hw_free()
 * *before* snd_pcm_lib_free_pages() releases the buffer, and only when the
 * stream actually ran (runtime->stop_operating). It runs in process context
 * with no stream lock held, so sleeping here is safe: a callback in its safe
 * zone has dropped urb_stream->lock and may freely take the stream lock, so the
 * stream lock -> urb_stream->lock order is never inverted.
 *
 * Return: 0 always; a timeout is logged but cannot usefully be reported.
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

	dev_dbg(&chip->intf0->dev, "PCM prepare stream %d\n", substream->stream);
	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	scoped_guard(spinlock_irqsave, &urb_stream->lock) {
		urb_stream->dma_off = 0;
		urb_stream->period_off = 0;
	}
	return 0;
}

/*
 * Called by the ALSA core with the substream stream lock held and interrupts
 * disabled, so this callback must never sleep.
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

static int jockey3_initialize_ploytec(struct jockey3_chip *chip, u32 *fw_version)
{
	int ret;

	if (jockey3_is_disconnected(chip))
		return -ENODEV;

	ret = ploytec_initialize_device(chip->intf0, chip->xfer_buf, false, fw_version);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Ploytec failed to initialize: %d\n", ret);
		return ret;
	}

	ret = ploytec_start_streaming(chip->intf0, chip->xfer_buf);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Ploytec failed to start streaming: %d\n", ret);
		return ret;
	}

	dev_dbg(&chip->intf0->dev, "Ploytec initialized successfully\n");
	return 0;
}

static const struct snd_pcm_ops jockey3_pcm_ops = {
	.open = jockey3_pcm_open,
	.close = jockey3_pcm_close,
	.prepare = jockey3_pcm_prepare,
	.trigger = jockey3_pcm_trigger,
	.sync_stop = jockey3_pcm_sync_stop,
	.pointer = jockey3_pcm_pointer,
};

static int jockey3_initialize(struct jockey3_chip *chip, int model)
{
	int ret;
	int rate;
	u32 fw_version = 0;

	/*
	 * Let the device finish booting before speaking to it.
	 *
	 * After a mains power cycle the Jockey 3 enumerates and answers control
	 * transfers while its audio engine is still coming up. Initialize it in
	 * that window and the engine never starts: every control transfer
	 * succeeds, the rate reads back correctly, ALSA accepts playback, and the
	 * device is silent -- capture returns bit-exact zero and nothing is
	 * logged. A USB re-enumeration does not provoke it; only a real
	 * power-off does, because the device is self-powered and a VBUS cut is
	 * merely a cable unplug to it.
	 *
	 * The threshold is sharp: the device needs between 144 and 156 ms after
	 * enumeration, below which the engine fails to start every time. 250 ms
	 * rather than the smallest passing value, because the time this driver
	 * otherwise takes to reach its first transfer is host-dependent, so a
	 * value that only topped that up would erode on a faster machine.
	 *
	 * See re/usb/init_timing_comparison.md for the measurements.
	 */
	msleep(250);

	/*
	 * Retry a device that refuses, not one that has stopped answering:
	 * ploytec_initialize_device() gives up after the first EP0 timeout, and
	 * retrying here would spend that timeout ten times over in probe.
	 */
	for (int retry = 10; retry > 0; retry--) {
		ret = jockey3_initialize_ploytec(chip, &fw_version);
		if (ret == 0 || ploytec_ctrl_ep_unresponsive(ret))
			break;
		usleep_range(50000, 100000); /* Wait 50-100 ms before retrying */
	}
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to initialize Ploytec: %d\n", ret);
		return ret;
	}

	// see ploytec_get_firmware() for the packing of buf[0..2] into fw_version
	dev_info(&chip->intf0->dev, CARD_NAME " %s Firmware 0x%02x v%d.%d.%d\n",
		 jockey3_model_name(model),
		 (fw_version >> 16) & 0xFF, (fw_version >> 8) & 0xFF,
		 (fw_version >> 4) & 0x0F, fw_version & 0x0F);

	rate = 44100;	// default sample rate at power-on
	ret = jockey3_set_rate(chip, rate, true);
	if (ret < 0)
		return ret;

	/*
	 * Fail the probe: a device that cannot accept its URBs has no working
	 * audio and no MIDI OUT.
	 */
	ret = jockey3_start_urbs(chip);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to start URBs during initialization: %d\n",
			ret);
		return ret;
	}

	dev_dbg(&chip->intf0->dev, "Initialization complete.\n");

	return 0;
}

/*
 * Free everything jockey3_probe() allocated into @chip, and give the card slot
 * back. NULL-safe throughout, so it is correct at any point of a half-built
 * probe as well as at the end of the device's life.
 *
 * Two rules govern what may go in here, both stemming from where it runs:
 * card->private_free, i.e. after the last file descriptor on the card is
 * closed, which on an unplug is arbitrarily long after jockey3_disconnect().
 *
 *  - No USB *operation* may be attempted. The objects are still allocated,
 *    since jockey3_probe() references each and this function drops them, but
 *    they are unbound by now and the hardware may be gone. Reading is safe,
 *    talking is not. The dev_dbg(&chip->intf0->dev, ...) idiom is avoided too:
 *    the interface is no longer ours, so the attribution would be wrong.
 *  - The URBs must already be dead. jockey3_disconnect() kills them, and so
 *    does probe's error path, both before the card is released.
 */
static void jockey3_free_resources(struct jockey3_chip *chip)
{
	int i;

	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		usb_free_urb(chip->playback.urbs[i]);
		kfree(chip->playback.bufs[i]);
		usb_free_urb(chip->capture.urbs[i]);
		kfree(chip->capture.bufs[i]);
	}
	usb_free_urb(chip->midi_in_urb);
	kfree(chip->midi_in_buf);
	kfree(chip->xfer_buf);

	jockey3_release_slot(chip->dev_idx);

	/* Last, reversing jockey3_probe(): everything above may read these. */
	usb_put_intf(chip->intf1);
	usb_put_intf(chip->intf0);
	usb_put_dev(chip->dev);
}

static void jockey3_card_free(struct snd_card *card)
{
	jockey3_free_resources(card->private_data);
}

static int jockey3_init_midi_urb(struct jockey3_chip *chip)
{
	struct usb_device *dev = chip->dev;

	chip->midi_in_buf = kmalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
	if (!chip->midi_in_buf)
		return -ENOMEM;

	chip->midi_in_urb = usb_alloc_urb(0, GFP_KERNEL);
	if (!chip->midi_in_urb)
		return -ENOMEM;

	usb_fill_bulk_urb(chip->midi_in_urb, dev,
			  usb_rcvbulkpipe(dev, PLOYTEC_EP_NUM_MIDI_IN),
			  chip->midi_in_buf, PLOYTEC_PKT_SIZE,
			  jockey3_midi_in_callback, chip);

	return 0;
}

static int jockey3_init_playback_urbs(struct jockey3_chip *chip)
{
	struct usb_device *dev = chip->dev;
	int i;

	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		chip->playback.bufs[i] = kzalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
		if (!chip->playback.bufs[i])
			return -ENOMEM;

		chip->playback.urbs[i] = usb_alloc_urb(0, GFP_KERNEL);
		if (!chip->playback.urbs[i])
			return -ENOMEM;

		jockey3_init_out_packet(chip->playback.bufs[i]);

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
	int i;

	for (i = 0; i < JOCKEY3_N_URBS; i++) {
		chip->capture.bufs[i] = kzalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
		if (!chip->capture.bufs[i])
			return -ENOMEM;

		chip->capture.urbs[i] = usb_alloc_urb(0, GFP_KERNEL);
		if (!chip->capture.urbs[i])
			return -ENOMEM;

		usb_fill_bulk_urb(chip->capture.urbs[i], dev,
				  usb_rcvbulkpipe(dev, PLOYTEC_EP_NUM_PCM_IN),
				  chip->capture.bufs[i], PLOYTEC_PKT_SIZE,
				  jockey3_capture_callback, chip);
	}

	return 0;
}

static bool jockey3_has_bulk_endpoint(struct usb_interface *intf, u8 ep_num, bool out)
{
	struct usb_host_interface *alts;
	int i;

	/*
	 * Alt 1 only: it is the setting ploytec_initialize_device() selects
	 * and the one the URBs run on. Alt 0 has no endpoints on this device,
	 * so an endpoint found only there would pass a check over every
	 * altsetting and then fail at URB submission.
	 */
	alts = usb_altnum_to_altsetting(intf, 1);
	if (!alts)
		return false;

	for (i = 0; i < alts->desc.bNumEndpoints; i++) {
		struct usb_endpoint_descriptor *epd = &alts->endpoint[i].desc;

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
 * Physical layout:
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

static void jockey3_setup_card_names(struct jockey3_chip *chip, int driver_info)
{
	/*
	 * card->driver is only char[16] and is what shows up as the card ID in
	 * /proc/asound, so it holds a short model identifier rather than the
	 * module name (which would be silently truncated).
	 */
	strscpy(chip->card->driver, "Jockey3", sizeof(chip->card->driver));
	strscpy(chip->card->shortname, CARD_NAME, sizeof(chip->card->shortname));
	strscpy(chip->card->mixername, CARD_NAME, sizeof(chip->card->mixername));

	snprintf(chip->card->longname, sizeof(chip->card->longname),
		 "%s %s at USB %s", CARD_NAME, jockey3_model_name(driver_info),
		 dev_name(&chip->dev->dev));
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

	ret = jockey3_claim_slot();
	if (ret < 0)
		return ret;
	dev_idx = ret;

	/*
	 * Deliberately not snd_devm_card_new(): a managed card is freed from the
	 * devres unwind inside usb_unbind_interface(), where snd_card_free()
	 * blocks until userspace closes every file descriptor on the card. On an
	 * unplug that unwind runs on the USB hub work queue, so one process
	 * sitting on a PCM fd would stall hotplug for the whole hub. The card is
	 * released from jockey3_disconnect() with snd_card_free_when_closed()
	 * instead, which is what every other USB sound driver does; the
	 * resources that outlive it go in card->private_free below.
	 */
	ret = snd_card_new(&intf->dev, index[dev_idx], id[dev_idx], THIS_MODULE,
			   sizeof(struct jockey3_chip), &card);
	if (ret < 0)
		goto err_free_idx;

	chip = card->private_data;
	chip->dev_idx = dev_idx;

	chip->card = card;
	/*
	 * The card outlives the unbind, so these pointers must too:
	 * snd_card_free_when_closed() defers the release until userspace closes
	 * the card, while usb_disconnect() device_unregister()s the interfaces
	 * as soon as the device leaves.
	 *
	 * This keeps the memory valid, not the device usable -- the interfaces
	 * are still unbound and every transfer through them fails.
	 * JOCKEY3_FLAG_DISCONNECTED remains the thing to test.
	 */
	chip->dev = usb_get_dev(dev);
	chip->intf0 = usb_get_intf(intf);
	chip->intf1 = usb_get_intf(intf1);
	chip->flags = 0;

	spin_lock_init(&chip->midi_lock);
	spin_lock_init(&chip->playback.lock);
	spin_lock_init(&chip->capture.lock);

	init_usb_anchor(&chip->playback.anchor);
	init_usb_anchor(&chip->capture.anchor);
	init_waitqueue_head(&chip->playback.drain_wait);
	init_waitqueue_head(&chip->capture.drain_wait);

	/*
	 * From here on every failure goes to err_free_card, and the card slot is
	 * given back by jockey3_free_resources() rather than by err_free_idx.
	 */
	card->private_free = jockey3_card_free;
	/*
	 * card->private_data is zeroed by snd_card_new(), but be explicit:
	 * this one is load-bearing for the stop bookkeeping.
	 */
	atomic_set(&chip->playback.urbs_in_flight, 0);
	atomic_set(&chip->capture.urbs_in_flight, 0);

	chip->xfer_buf = kmalloc(USB_XFER_BUF_SIZE, GFP_KERNEL);
	if (!chip->xfer_buf) {
		ret = -ENOMEM;
		goto err_free_card;
	}

	ret = jockey3_init_midi_urb(chip);
	if (ret < 0)
		goto err_free_card;

	ret = jockey3_init_playback_urbs(chip);
	if (ret < 0)
		goto err_free_card;

	ret = jockey3_init_capture_urbs(chip);
	if (ret < 0)
		goto err_free_card;

	/* Interface 1 owns the capture endpoint (0x86) */
	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
	if (ret < 0)
		goto err_free_card;

	ret = jockey3_init_pcm(chip);
	if (ret < 0)
		goto err_free_card;

	jockey3_setup_card_names(chip, usb_id->driver_info);

	if (card->id[0] == '\0')
		snd_card_set_id(card, "RJ3");

	usb_set_intfdata(intf, chip);
	ret = jockey3_initialize(chip, usb_id->driver_info);
	if (ret < 0)
		goto err_free_card;

	ret = snd_card_register(card);
	if (ret < 0)
		goto err_free_card;

	return 0;

err_free_card:
	/*
	 * Same order as jockey3_disconnect(): latch DISCONNECTED, kill the URBs,
	 * and only then let go of interface 1 and the card. All of it is safe on
	 * a half-built chip -- the locks and anchors are initialized above with
	 * no failure point between, and everything jockey3_free_resources()
	 * touches is NULL until it is allocated.
	 *
	 * snd_card_free_when_closed() rather than snd_card_free(), as in
	 * jockey3_disconnect(): snd_card_register() creates the device nodes
	 * before it can still fail, so userspace may have the card open, and
	 * snd_card_free() would block probe until it lets go.
	 *
	 * probe() owns the usb_device lock, as usb_driver_claim_interface()
	 * requires, and every unbind takes the same lock (see
	 * jockey3_disconnect()), so interface 1 cannot be unbound from sysfs
	 * while this unwinds.
	 */
	usb_set_intfdata(intf, NULL);
	set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
	jockey3_stop_urbs(chip);
	usb_driver_release_interface(&jockey3_driver, intf1);
	snd_card_free_when_closed(card);
	return ret;

err_free_idx:
	/* Only reached before the card exists */
	jockey3_release_slot(dev_idx);
	return ret;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	/*
	 * Latch DISCONNECTED first, and for EITHER interface. Every ALSA entry
	 * point tests it on the way in.
	 *
	 * Doing it for interface 1 as well is what covers the unbind order. The
	 * driver is bound to both interfaces and the USB core takes them down
	 * one at a time, calling disconnect() and then usb_disable_interface()
	 * for each, so whichever goes first has its endpoints flushed while the
	 * other is still bound. Those URBs come back -ESHUTDOWN through the
	 * ordinary completion path, and without this they are indistinguishable
	 * from an endpoint torn down behind the driver's back. Losing either
	 * interface means the card is going away regardless -- interface 1 owns
	 * the capture endpoint -- so there is nothing to keep running for.
	 */
	if (chip)
		set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);

	if (chip && intf == chip->intf0) {
		jockey3_stop_urbs(chip);

		/*
		 * Release interface 1 here, not from card->private_free: that
		 * runs when the last file descriptor is closed, by which time
		 * usb_disconnect() may already have freed the interface. This
		 * recurses into jockey3_disconnect() for interface 1, which only
		 * latches DISCONNECTED and clears its intfdata; if the core got
		 * there first, usb_driver_release_interface() sees a condition
		 * other than USB_INTERFACE_BOUND and returns.
		 *
		 * The two disconnects never overlap: every unbind of either
		 * interface holds the usb_device lock. usb_disconnect() holds it
		 * throughout, and a sysfs unbind or driver detach takes it first
		 * because usb_bus_type sets need_parent_lock. So interface 1's
		 * disconnect has either finished already or runs inside this
		 * call, and the chip is not freed under it.
		 */
		usb_driver_release_interface(&jockey3_driver, chip->intf1);

		/*
		 * snd_card_free_when_closed() disconnects the card now and frees
		 * it once userspace has closed it -- it never blocks, which is
		 * the whole point of not using a managed card (see the comment in
		 * jockey3_probe()). It runs snd_pcm_stop(DISCONNECTED) under the
		 * stream lock on the way, which drives our .trigger and clears
		 * 'running' with the proper locking, so there is nothing to clear
		 * here by hand. @chip lives in card->private_data and may be gone
		 * the moment this returns, so nothing below may touch it.
		 */
		snd_card_free_when_closed(chip->card);
	}
	usb_set_intfdata(intf, NULL);
}

static const struct usb_device_id jockey3_ids[] = {
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_ME_PID), .driver_info = JOCKEY3_ME },
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_REMIX_PID), .driver_info = JOCKEY3_REMIX },
	{}
};
MODULE_DEVICE_TABLE(usb, jockey3_ids);

static struct usb_driver jockey3_driver = {
	.name = "snd-reloop-jockey3",
	/*
	 * Tear our own URBs down rather than having the core do it first.
	 *
	 * Without this, usb_unbind_interface() calls usb_disable_interface()
	 * before jockey3_disconnect(), so on an ordinary module unload the whole
	 * ring retires with -ESHUTDOWN while the driver still believes it is
	 * running -- indistinguishable, from inside the completion handler, from
	 * an endpoint torn down behind our back. jockey3_disconnect() already
	 * kills every URB, which is exactly the contract this flag asks for.
	 *
	 * It applies only while the device is still attached; on a physical
	 * unplug the core kills the URBs first regardless, which is why the
	 * completion path also tests USB_STATE_NOTATTACHED.
	 */
	.soft_unbind = 1,
	.probe = jockey3_probe,
	.disconnect = jockey3_disconnect,
	.id_table = jockey3_ids
};

/*
 * The codec's bit-spread lookup tables are module-global and unlocked, so they
 * are built once per module rather than per device.
 */
static int __init jockey3_module_init(void)
{
	enum ploytec_codec_variant codec_variant = ploytec_initialize_codec();

	switch (codec_variant) {
	case PLOYTEC_CODEC_PORTABLE:
		pr_debug("using portable codec\n");
		break;
	case PLOYTEC_CODEC_OPTIMIZED_64BIT:
		pr_debug("using 64-bit optimized codec\n");
		break;
	case PLOYTEC_CODEC_OPTIMIZED_32BIT:
		pr_debug("using 32-bit optimized codec\n");
		break;
	}

	return usb_register(&jockey3_driver);
}
module_init(jockey3_module_init);

static void __exit jockey3_module_exit(void)
{
	usb_deregister(&jockey3_driver);
}
module_exit(jockey3_module_exit);

MODULE_AUTHOR("Frank van de Pol <fvdpol@gmail.com>");
MODULE_DESCRIPTION(CARD_NAME " ALSA Driver");
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: snd-pcm");
