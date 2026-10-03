// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#include <linux/types.h>
#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>
#include <linux/delay.h>
#include <linux/bitops.h>
#include <linux/mutex.h>
#include <linux/cleanup.h>
#include <sound/core.h>
#include <sound/initval.h>
#include "ploytec_proto.h"

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
 */

/**
 * DOC: Resource lifetime
 *
 * The card is created with snd_card_new(), not snd_devm_card_new(), and
 * released by jockey3_disconnect() with snd_card_free_when_closed(). A managed
 * card would instead be freed from the devres unwind inside
 * usb_unbind_interface(), where snd_card_free() blocks until userspace has
 * closed every file descriptor on the card -- on an unplug that unwind runs on
 * the USB hub work queue, so a single process holding the card open would
 * stall hotplug for the whole hub. Every other USB sound driver in the tree
 * avoids that the same way.
 *
 * That splits teardown in two, and the split is the thing to keep straight:
 *
 * - jockey3_disconnect() does everything that must happen while the device is
 *   still there: latch JOCKEY3_FLAG_DISCONNECTED (for either interface, since
 *   the core takes them down one at a time) and release interface 1. The
 *   disconnect() that finishes last, counted by @active_intfs, then hands the
 *   card to snd_card_free_when_closed(). Both interfaces can be unbound at
 *   once from sysfs, and neither may free the chip while the other is still
 *   inside it.
 *
 * - jockey3_card_free(), reached through card->private_free once the last file
 *   descriptor is closed, frees the transfer buffer and gives the card slot
 *   back. It runs arbitrarily long after the disconnect and must attempt no
 *   USB operation, though the objects themselves stay referenced until it
 *   returns; see jockey3_free_resources().
 *
 * struct jockey3_chip lives in card->private_data, so it is freed with the
 * card and outlives the USB binding.
 *
 * probe's error path performs the disconnect half by hand and then calls
 * snd_card_free(); the card is never registered on any path that reaches
 * there, so the synchronous free cannot block.
 */

/**
 * struct jockey3_chip - per-device driver state
 * @card: the ALSA card; read-only after probe
 * @dev: the USB device; read-only after probe. Referenced for the chip's
 *	lifetime, as @intf0 and @intf1 are, because the card can outlive the
 *	unbind and usb_disconnect() would otherwise free them before
 *	jockey3_free_resources() runs, which releases them.
 * @intf0: interface 0, which the driver is bound to; read-only after probe
 * @intf1: interface 1, claimed explicitly because it owns EP 0x86
 * @xfer_buf: bounce buffer for EP0 control transfers, USB_XFER_BUF_SIZE bytes.
 * @flags: JOCKEY3_FLAG_* bits, atomic bitops
 * @dev_idx: card slot held in jockey3_devices_used
 * @active_intfs: interfaces that have not finished jockey3_disconnect(): 2
 *	after probe. Each disconnect drops it after its last access to the chip,
 *	and the one that reaches zero hands the card to
 *	snd_card_free_when_closed(). The USB core can run the two disconnects
 *	concurrently (unbinding both interfaces from sysfs), and
 *	usb_driver_release_interface() does not wait for one already underway,
 *	so neither may free the chip while the other is still inside it.
 */
struct jockey3_chip {
	struct snd_card *card;
	struct usb_device *dev;
	struct usb_interface *intf0;
	struct usb_interface *intf1;
	unsigned char *xfer_buf;
	unsigned long flags;
	unsigned int dev_idx;
	atomic_t active_intfs;
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

	dev_dbg(&chip->intf0->dev, "Ploytec initialized successfully\n");
	return 0;
}

static int jockey3_initialize(struct jockey3_chip *chip, int model)
{
	int ret;
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

	for (int retry = 10; retry > 0; retry--) {
		ret = jockey3_initialize_ploytec(chip, &fw_version);
		if (ret == 0)
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

	return 0;
}

/*
 * Free everything jockey3_probe() allocated into @chip, and give the card slot
 * back. NULL-safe throughout, so it is correct at any point of a half-built
 * probe as well as at the end of the device's life.
 *
 * One rule governs what may go in here, stemming from where it runs:
 * card->private_free, i.e. after the last file descriptor on the card is
 * closed, which on an unplug is arbitrarily long after jockey3_disconnect().
 *
 *  - No USB *operation* may be attempted. The objects are still allocated,
 *    since jockey3_probe() references each and this function drops them, but
 *    they are unbound by now and the hardware may be gone. Reading is safe,
 *    talking is not. The dev_dbg(&chip->intf0->dev, ...) idiom is avoided too:
 *    the interface is no longer ours, so the attribution would be wrong.
 */
static void jockey3_free_resources(struct jockey3_chip *chip)
{
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
	 * holding the card open would stall hotplug for the whole hub. The card is
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
	atomic_set(&chip->active_intfs, 2);

	/*
	 * From here on every failure goes to err_free_card, and the card slot is
	 * given back by jockey3_free_resources() rather than by err_free_idx.
	 */
	card->private_free = jockey3_card_free;

	chip->xfer_buf = kmalloc(USB_XFER_BUF_SIZE, GFP_KERNEL);
	if (!chip->xfer_buf) {
		ret = -ENOMEM;
		goto err_free_card;
	}

	/* Interface 1 owns the capture endpoint (0x86) */
	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
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
	 * Same order as jockey3_disconnect(): latch DISCONNECTED, and only then
	 * let go of interface 1 and the card. All of it is safe on a half-built
	 * chip -- everything jockey3_free_resources() touches is NULL until it
	 * is allocated.
	 *
	 * snd_card_free() rather than the disconnect path's
	 * snd_card_free_when_closed(): the card is not registered yet on any
	 * path that reaches here, so nothing can have it open and the
	 * synchronous free cannot block.
	 */
	usb_set_intfdata(intf, NULL);
	set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);
	usb_driver_release_interface(&jockey3_driver, intf1);
	snd_card_free(card);
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
	 * Latch DISCONNECTED first, and for EITHER interface. The driver is
	 * bound to both interfaces and the USB core takes them down one at a
	 * time. Losing either interface means the card is going away regardless
	 * -- interface 1 owns the capture endpoint -- so there is nothing to
	 * keep running for.
	 */
	if (chip)
		set_bit(JOCKEY3_FLAG_DISCONNECTED, &chip->flags);

	if (chip && intf == chip->intf0) {
		/*
		 * Release interface 1 here, not from card->private_free: that
		 * runs when the last file descriptor is closed, by which time
		 * usb_disconnect() may already have freed the interface. This
		 * recurses into jockey3_disconnect() for interface 1, which only
		 * latches DISCONNECTED, clears its intfdata and drops its share
		 * of @active_intfs; if the core got there first,
		 * usb_driver_release_interface() sees a condition other than
		 * USB_INTERFACE_BOUND and returns.
		 */
		usb_driver_release_interface(&jockey3_driver, chip->intf1);
	}
	usb_set_intfdata(intf, NULL);

	/*
	 * Whichever disconnect() comes last frees the card. The core does not
	 * serialize the two against each other: unbinding both interfaces from
	 * sysfs at once can have this one running while the other has already
	 * returned from usb_driver_release_interface(), which does not wait for
	 * an unbind in flight. Dropping the count only after the last access to
	 * @chip above means the freeing side never pulls it out from under the
	 * other.
	 *
	 * snd_card_free_when_closed() disconnects the card now and frees it once
	 * userspace has closed it -- it never blocks, which is the whole point
	 * of not using a managed card (see the comment in jockey3_probe()).
	 * @chip lives in card->private_data and may be gone the moment this
	 * returns.
	 */
	if (chip && atomic_dec_and_test(&chip->active_intfs))
		snd_card_free_when_closed(chip->card);
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
	.id_table = jockey3_ids
};

module_usb_driver(jockey3_driver);

MODULE_AUTHOR("Frank van de Pol <fvdpol@gmail.com>");
MODULE_DESCRIPTION(CARD_NAME " ALSA Driver");
MODULE_LICENSE("GPL");
