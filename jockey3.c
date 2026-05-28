/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Reloop Jockey 3 Remix ALSA Driver (MIDI OUT & LED Control)
 *
 * This driver claims the Reloop Jockey 3 Remix (200c:1037), performs the
 * Ploytec handshake, and registers a duplex ALSA MIDI device. It supports
 * MIDI IN (with 0xFD filtering) and MIDI OUT (via EP 0x05 injection).
 */

#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>
#include <linux/delay.h>
#include <sound/core.h>
#include <sound/initval.h>
#include <sound/rawmidi.h>

#define RELOOP_VENDOR_ID          0x200c
#define RELOOP_JOCKEY3_REMIX_PID  0x1037
#define RELOOP_JOCKEY3_MASTER_PID 0x1009

/* Ploytec Vendor Commands */
#define PLOYTEC_CMD_FIRMWARE        0x56
#define PLOYTEC_CMD_STATUS          0x49
#define PLOYTEC_REG_AJ_INPUT_SEL    0

#define PLOYTEC_CMD_SET_RATE_REQ    0x01
#define PLOYTEC_CMD_SET_RATE_TYPE   0x22
#define PLOYTEC_EP_RATE_IN          0x0086
#define PLOYTEC_EP_RATE_OUT         0x0005

#define PLOYTEC_EP_MIDI_IN          0x83
#define PLOYTEC_EP_PCM_OUT          0x05
#define PLOYTEC_PKT_SIZE            512
#define PLOYTEC_MIDI_IDLE_BYTE      0xFD

static int index[SNDRV_CARDS] = SNDRV_DEFAULT_IDX;
static char *id[SNDRV_CARDS] = SNDRV_DEFAULT_STR;
static bool enable[SNDRV_CARDS] = SNDRV_DEFAULT_ENABLE_PNP;

module_param_array(index, int, NULL, 0444);
MODULE_PARM_DESC(index, "Index value for Reloop Jockey 3 Remix soundcard.");
module_param_array(id, charp, NULL, 0444);
MODULE_PARM_DESC(id, "ID string for Reloop Jockey 3 Remix soundcard.");
module_param_array(enable, bool, NULL, 0444);
MODULE_PARM_DESC(enable, "Enable Reloop Jockey 3 Remix soundcard.");

static const struct usb_device_id jockey3_ids[] = {
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_REMIX_PID) },
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_MASTER_PID) },
	{ }
};
MODULE_DEVICE_TABLE(usb, jockey3_ids);

struct jockey3_chip {
	struct snd_card *card;
	struct usb_device *dev;
	struct usb_interface *intf0;
	struct usb_interface *intf1;
	unsigned char *xfer_buf;

	/* MIDI IN */
	struct urb *midi_in_urb;
	unsigned char *midi_in_buf;
	struct snd_rawmidi *rmidi;
	struct snd_rawmidi_substream *midi_in_substream;
	spinlock_t midi_in_lock;

	/* MIDI OUT / Heartbeat */
	struct urb *out_urb;
	unsigned char *out_buf;
	struct snd_rawmidi_substream *midi_out_substream;
	spinlock_t midi_out_lock;
};

static void jockey3_midi_in_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	unsigned long flags;
	int i, ret;

	if (urb->status) {
		if (urb->status != -ENOENT && urb->status != -ECONNRESET &&
		    urb->status != -ESHUTDOWN)
			dev_err(&chip->intf0->dev, "MIDI IN URB failed: %d\n", urb->status);
		return;
	}

	spin_lock_irqsave(&chip->midi_in_lock, flags);
	if (chip->midi_in_substream && urb->actual_length > 0) {
		for (i = 0; i < urb->actual_length; i++) {
			if (chip->midi_in_buf[i] != PLOYTEC_MIDI_IDLE_BYTE) {
				snd_rawmidi_receive(chip->midi_in_substream,
						    &chip->midi_in_buf[i], 1);
			}
		}
	}
	spin_unlock_irqrestore(&chip->midi_in_lock, flags);

	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit MIDI IN URB: %d\n", ret);
}

static void jockey3_out_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	unsigned long flags;
	int ret;

	if (urb->status) {
		if (urb->status != -ENOENT && urb->status != -ECONNRESET &&
		    urb->status != -ESHUTDOWN)
			dev_err(&chip->intf0->dev, "OUT Heartbeat URB failed: %d\n", urb->status);
		return;
	}

	/* Check if we have MIDI bytes to send */
	spin_lock_irqsave(&chip->midi_out_lock, flags);
	if (chip->midi_out_substream) {
		u8 byte;
		if (snd_rawmidi_transmit(chip->midi_out_substream, &byte, 1) == 1)
			chip->out_buf[480] = byte;
		else
			chip->out_buf[480] = PLOYTEC_MIDI_IDLE_BYTE;
	} else {
		chip->out_buf[480] = PLOYTEC_MIDI_IDLE_BYTE;
	}
	spin_unlock_irqrestore(&chip->midi_out_lock, flags);

	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit OUT URB: %d\n", ret);
}

static int jockey3_midi_in_open(struct snd_rawmidi_substream *substream)
{
	return 0;
}

static int jockey3_midi_in_close(struct snd_rawmidi_substream *substream)
{
	return 0;
}

static void jockey3_midi_in_trigger(struct snd_rawmidi_substream *substream, int up)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;
	unsigned long flags;

	spin_lock_irqsave(&chip->midi_in_lock, flags);
	chip->midi_in_substream = up ? substream : NULL;
	spin_unlock_irqrestore(&chip->midi_in_lock, flags);
}

static int jockey3_midi_out_open(struct snd_rawmidi_substream *substream)
{
	return 0;
}

static int jockey3_midi_out_close(struct snd_rawmidi_substream *substream)
{
	return 0;
}

static void jockey3_midi_out_trigger(struct snd_rawmidi_substream *substream, int up)
{
	struct jockey3_chip *chip = substream->rmidi->private_data;
	unsigned long flags;

	spin_lock_irqsave(&chip->midi_out_lock, flags);
	chip->midi_out_substream = up ? substream : NULL;
	spin_unlock_irqrestore(&chip->midi_out_lock, flags);
}

static const struct snd_rawmidi_ops jockey3_midi_in_ops = {
	.open = jockey3_midi_in_open,
	.close = jockey3_midi_in_close,
	.trigger = jockey3_midi_in_trigger,
};

static const struct snd_rawmidi_ops jockey3_midi_out_ops = {
	.open = jockey3_midi_out_open,
	.close = jockey3_midi_out_close,
	.trigger = jockey3_midi_out_trigger,
};

static int jockey3_midi_init(struct jockey3_chip *chip)
{
	struct snd_rawmidi *rmidi;
	int ret;

	/* 1 Output (LEDs), 1 Input (Controls) */
	ret = snd_rawmidi_new(chip->card, "Jockey 3 MIDI", 0, 1, 1, &rmidi);
	if (ret < 0) return ret;

	strscpy(rmidi->name, "Jockey 3 MIDI", sizeof(rmidi->name));
	rmidi->private_data = chip;
	rmidi->info_flags = SNDRV_RAWMIDI_INFO_OUTPUT |
			   SNDRV_RAWMIDI_INFO_INPUT |
			   SNDRV_RAWMIDI_INFO_DUPLEX;

	snd_rawmidi_set_ops(rmidi, SNDRV_RAWMIDI_STREAM_INPUT, &jockey3_midi_in_ops);
	snd_rawmidi_set_ops(rmidi, SNDRV_RAWMIDI_STREAM_OUTPUT, &jockey3_midi_out_ops);

	chip->rmidi = rmidi;
	return 0;
}

static int jockey3_set_rate(struct jockey3_chip *chip, unsigned int rate)
{
	int ret;
	chip->xfer_buf[0] = rate & 0xFF;
	chip->xfer_buf[1] = (rate >> 8) & 0xFF;
	chip->xfer_buf[2] = (rate >> 16) & 0xFF;

	ret = usb_control_msg(chip->dev, usb_sndctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_SET_RATE_REQ, PLOYTEC_CMD_SET_RATE_TYPE,
			      0x0100, PLOYTEC_EP_RATE_IN, chip->xfer_buf, 3, 2000);
	if (ret < 0) return ret;
	msleep(20);

	ret = usb_control_msg(chip->dev, usb_sndctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_SET_RATE_REQ, PLOYTEC_CMD_SET_RATE_TYPE,
			      0x0100, PLOYTEC_EP_RATE_OUT, chip->xfer_buf, 3, 2000);
	return ret;
}

static int jockey3_handshake(struct jockey3_chip *chip)
{
	int ret;
	uint8_t status;
	uint16_t wvalue;

	ret = usb_control_msg(chip->dev, usb_rcvctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_FIRMWARE, 0xC0, 0x0000, 0,
			      chip->xfer_buf, 15, 2000);
	if (ret < 0) return ret;
	msleep(20);

	ret = usb_control_msg(chip->dev, usb_rcvctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_STATUS, 0xC0, 0x0000,
			      PLOYTEC_REG_AJ_INPUT_SEL,
			      chip->xfer_buf, 1, 2000);
	if (ret < 0) return ret;
	status = chip->xfer_buf[0];
	msleep(20);

	ret = usb_set_interface(chip->dev, 0, 1);
	if (ret < 0) return ret;
	msleep(20);
	ret = usb_set_interface(chip->dev, 1, 1);
	if (ret < 0) return ret;
	msleep(20);

	ret = jockey3_set_rate(chip, 44100);
	if (ret < 0) return ret;
	msleep(20);

	if (!(status & 0x20)) {
		wvalue = (uint16_t)(int16_t)(int8_t)(status | 0x20);
		usb_control_msg(chip->dev, usb_sndctrlpipe(chip->dev, 0),
				PLOYTEC_CMD_STATUS, 0x40,
				wvalue, PLOYTEC_REG_AJ_INPUT_SEL,
				NULL, 0, 2000);
	}

	ret = usb_submit_urb(chip->out_urb, GFP_KERNEL);
	if (ret < 0) return ret;

	ret = usb_submit_urb(chip->midi_in_urb, GFP_KERNEL);
	return ret;
}

static struct usb_driver jockey3_driver;

static int jockey3_probe(struct usb_interface *intf, const struct usb_device_id *usb_id)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	struct usb_interface *intf1;
	struct snd_card *card;
	struct jockey3_chip *chip;
	int ret, i;

	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;

	intf1 = usb_ifnum_to_if(dev, 1);
	if (!intf1) return -ENODEV;

	for (i = 0; i < SNDRV_CARDS; i++)
		if (enable[i]) break;
	if (i >= SNDRV_CARDS) return -ENODEV;

	ret = snd_card_new(&intf->dev, index[i], id[i], THIS_MODULE,
			   sizeof(struct jockey3_chip), &card);
	if (ret < 0) return ret;

	chip = card->private_data;
	chip->card = card;
	chip->dev = dev;
	chip->intf0 = intf;
	chip->intf1 = intf1;
	spin_lock_init(&chip->midi_in_lock);
	spin_lock_init(&chip->midi_out_lock);

	chip->xfer_buf = kmalloc(64, GFP_KERNEL);
	chip->midi_in_buf = kmalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
	chip->out_buf = kmalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
	chip->midi_in_urb = usb_alloc_urb(0, GFP_KERNEL);
	chip->out_urb = usb_alloc_urb(0, GFP_KERNEL);

	if (!chip->xfer_buf || !chip->midi_in_buf || !chip->out_buf ||
	    !chip->midi_in_urb || !chip->out_urb) {
		ret = -ENOMEM;
		goto err_card;
	}

	strscpy(card->driver, "snd-reloop-jockey3", sizeof(card->driver));
	strscpy(card->shortname, "Jockey 3 Remix", sizeof(card->shortname));
	snprintf(card->longname, sizeof(card->longname), "Reloop Jockey 3 Remix at %s",
		 dev_name(&intf->dev));

	ret = jockey3_midi_init(chip);
	if (ret < 0) goto err_card;

	memset(chip->out_buf, 0, PLOYTEC_PKT_SIZE);
	chip->out_buf[480] = PLOYTEC_MIDI_IDLE_BYTE;
	chip->out_buf[481] = 0xFF;

	usb_fill_bulk_urb(chip->out_urb, dev, usb_sndbulkpipe(dev, PLOYTEC_EP_PCM_OUT),
			  chip->out_buf, PLOYTEC_PKT_SIZE, jockey3_out_callback, chip);
	usb_fill_bulk_urb(chip->midi_in_urb, dev, usb_rcvbulkpipe(dev, PLOYTEC_EP_MIDI_IN),
			  chip->midi_in_buf, PLOYTEC_PKT_SIZE, jockey3_midi_in_callback, chip);

	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
	if (ret < 0) goto err_card;

	ret = snd_card_register(card);
	if (ret < 0) goto err_unclaim;

	usb_set_intfdata(intf, chip);
	ret = jockey3_handshake(chip);
	if (ret < 0) goto err_unclaim;

	return 0;

err_unclaim:
	usb_driver_release_interface(&jockey3_driver, intf1);
err_card:
	snd_card_free(card);
	return ret;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);
	if (chip && intf == chip->intf0) {
		usb_kill_urb(chip->midi_in_urb);
		usb_kill_urb(chip->out_urb);
		snd_card_disconnect(chip->card);
		usb_driver_release_interface(&jockey3_driver, chip->intf1);
		usb_free_urb(chip->midi_in_urb);
		usb_free_urb(chip->out_urb);
		kfree(chip->out_buf);
		kfree(chip->midi_in_buf);
		kfree(chip->xfer_buf);
		snd_card_free_when_closed(chip->card);
	}
	usb_set_intfdata(intf, NULL);
}

static struct usb_driver jockey3_driver = {
	.name = "snd-reloop-jockey3",
	.probe = jockey3_probe,
	.disconnect = jockey3_disconnect,
	.id_table = jockey3_ids,
};

module_usb_driver(jockey3_driver);

MODULE_AUTHOR("Frank van de Pol");
MODULE_DESCRIPTION("Reloop Jockey 3 Remix ALSA Driver (MIDI OUT)");
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: snd-rawmidi");
