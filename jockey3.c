/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Reloop Jockey 3 Remix ALSA Driver (MIDI IN & Heartbeat)
 *
 * This driver claims the Reloop Jockey 3 Remix (200c:1037), performs the
 * Ploytec handshake, and sets up a Bulk OUT heartbeat on EP 0x05 to
 * trigger the device's streaming engine, enabling MIDI IN on EP 0x83.
 */

#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>
#include <linux/delay.h>

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

static const struct usb_device_id jockey3_ids[] = {
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_REMIX_PID) },
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_MASTER_PID) },
	{ }
};
MODULE_DEVICE_TABLE(usb, jockey3_ids);

struct jockey3_chip {
	struct usb_device *dev;
	struct usb_interface *intf0;
	struct usb_interface *intf1;
	unsigned char *xfer_buf; /* DMA-safe buffer for control transfers */

	/* MIDI IN */
	struct urb *midi_in_urb;
	unsigned char *midi_in_buf;

	/* Heartbeat OUT (kickstarts the device engine) */
	struct urb *out_urb;
	unsigned char *out_buf;
};

static void jockey3_midi_in_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	int ret;

	if (urb->status) {
		if (urb->status != -ENOENT && urb->status != -ECONNRESET &&
		    urb->status != -ESHUTDOWN)
			dev_err(&chip->intf0->dev, "MIDI IN URB failed: %d\n", urb->status);
		return;
	}

	if (urb->actual_length > 0) {
		/* Log actual MIDI bytes, skipping 0xFD idle markers */
		int i;
		bool has_data = false;
		for (i = 0; i < urb->actual_length; i++) {
			if (chip->midi_in_buf[i] != 0xFD) {
				has_data = true;
				break;
			}
		}

		if (has_data) {
			dev_info(&chip->intf0->dev, "MIDI IN data: %*ph\n",
				 urb->actual_length > 32 ? 32 : urb->actual_length,
				 chip->midi_in_buf);
		}
	}

	/* Resubmit URB */
	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit MIDI IN URB: %d\n", ret);
}

static void jockey3_out_callback(struct urb *urb)
{
	struct jockey3_chip *chip = urb->context;
	int ret;

	if (urb->status) {
		if (urb->status != -ENOENT && urb->status != -ECONNRESET &&
		    urb->status != -ESHUTDOWN)
			dev_err(&chip->intf0->dev, "OUT Heartbeat URB failed: %d\n", urb->status);
		return;
	}

	/* Keep the heart beating */
	ret = usb_submit_urb(urb, GFP_ATOMIC);
	if (ret < 0)
		dev_err(&chip->intf0->dev, "Failed to resubmit OUT URB: %d\n", ret);
}

static int jockey3_set_rate(struct jockey3_chip *chip, unsigned int rate)
{
	int ret;

	chip->xfer_buf[0] = rate & 0xFF;
	chip->xfer_buf[1] = (rate >> 8) & 0xFF;
	chip->xfer_buf[2] = (rate >> 16) & 0xFF;

	dev_info(&chip->intf0->dev, "Setting sample rate to %u Hz (EP 0x86)...\n", rate);
	ret = usb_control_msg(chip->dev, usb_sndctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_SET_RATE_REQ, PLOYTEC_CMD_SET_RATE_TYPE,
			      0x0100, PLOYTEC_EP_RATE_IN, chip->xfer_buf, 3, 2000);
	if (ret < 0) return ret;
	msleep(20);

	dev_info(&chip->intf0->dev, "Setting sample rate to %u Hz (EP 0x05)...\n", rate);
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

	/* 1. Read Firmware Version */
	ret = usb_control_msg(chip->dev, usb_rcvctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_FIRMWARE, 0xC0, 0x0000, 0,
			      chip->xfer_buf, 15, 2000);
	if (ret < 0) return ret;
	msleep(20);

	/* 2. Read Status Byte */
	ret = usb_control_msg(chip->dev, usb_rcvctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_STATUS, 0xC0, 0x0000,
			      PLOYTEC_REG_AJ_INPUT_SEL,
			      chip->xfer_buf, 1, 2000);
	if (ret < 0) return ret;

	status = chip->xfer_buf[0];
	msleep(20);

	/* 3. Set Alternate Setting 1 */
	ret = usb_set_interface(chip->dev, 0, 1);
	if (ret < 0) return ret;
	msleep(20);

	ret = usb_set_interface(chip->dev, 1, 1);
	if (ret < 0) return ret;
	msleep(20);

	/* 4. Set Sample Rate */
	ret = jockey3_set_rate(chip, 44100);
	if (ret < 0) return ret;
	msleep(20);

	/* 5. Confirm Status (Arm) */
	if (!(status & 0x20)) {
		wvalue = (uint16_t)(int16_t)(int8_t)(status | 0x20);
		ret = usb_control_msg(chip->dev, usb_sndctrlpipe(chip->dev, 0),
				      PLOYTEC_CMD_STATUS, 0x40,
				      wvalue, PLOYTEC_REG_AJ_INPUT_SEL,
				      NULL, 0, 2000);
		if (ret < 0) return ret;
	}

	dev_info(&chip->intf0->dev, "Handshake complete, device armed.\n");

	/* 6. Start Heartbeat OUT Stream */
	ret = usb_submit_urb(chip->out_urb, GFP_KERNEL);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to submit Heartbeat OUT URB: %d\n", ret);
		return ret;
	}

	/* 7. Start MIDI IN URB */
	ret = usb_submit_urb(chip->midi_in_urb, GFP_KERNEL);
	if (ret < 0) {
		dev_err(&chip->intf0->dev, "Failed to submit MIDI IN URB: %d\n", ret);
		return ret;
	}

	return 0;
}

static struct usb_driver jockey3_driver;

static int jockey3_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	struct usb_interface *intf1;
	struct jockey3_chip *chip;
	int ret;

	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;

	intf1 = usb_ifnum_to_if(dev, 1);
	if (!intf1) return -ENODEV;

	chip = kzalloc(sizeof(*chip), GFP_KERNEL);
	if (!chip) return -ENOMEM;

	chip->xfer_buf = kmalloc(64, GFP_KERNEL);
	chip->midi_in_buf = kmalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
	chip->out_buf = kmalloc(PLOYTEC_PKT_SIZE, GFP_KERNEL);
	chip->midi_in_urb = usb_alloc_urb(0, GFP_KERNEL);
	chip->out_urb = usb_alloc_urb(0, GFP_KERNEL);

	if (!chip->xfer_buf || !chip->midi_in_buf || !chip->out_buf ||
	    !chip->midi_in_urb || !chip->out_urb) {
		ret = -ENOMEM;
		goto err_free;
	}

	chip->dev = dev;
	chip->intf0 = intf;
	chip->intf1 = intf1;

	/* Setup Heartbeat buffer (Ploytec Idle Pattern) */
	memset(chip->out_buf, 0, PLOYTEC_PKT_SIZE);
	chip->out_buf[480] = 0xFD; /* MIDI Idle */
	chip->out_buf[481] = 0xFF; /* Sync */

	usb_fill_bulk_urb(chip->out_urb, dev,
			  usb_sndbulkpipe(dev, PLOYTEC_EP_PCM_OUT),
			  chip->out_buf, PLOYTEC_PKT_SIZE,
			  jockey3_out_callback, chip);

	usb_fill_bulk_urb(chip->midi_in_urb, dev,
			  usb_rcvbulkpipe(dev, PLOYTEC_EP_MIDI_IN),
			  chip->midi_in_buf, PLOYTEC_PKT_SIZE,
			  jockey3_midi_in_callback, chip);

	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
	if (ret < 0) goto err_free;

	usb_set_intfdata(intf, chip);

	ret = jockey3_handshake(chip);
	if (ret < 0) goto err_unclaim;

	return 0;

err_unclaim:
	usb_driver_release_interface(&jockey3_driver, intf1);
err_free:
	if (chip->out_urb) usb_free_urb(chip->out_urb);
	if (chip->midi_in_urb) usb_free_urb(chip->midi_in_urb);
	kfree(chip->out_buf);
	kfree(chip->midi_in_buf);
	kfree(chip->xfer_buf);
	kfree(chip);
	return ret;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip && intf == chip->intf0) {
		usb_kill_urb(chip->midi_in_urb);
		usb_kill_urb(chip->out_urb);
		usb_driver_release_interface(&jockey3_driver, chip->intf1);
		usb_free_urb(chip->midi_in_urb);
		usb_free_urb(chip->out_urb);
		kfree(chip->out_buf);
		kfree(chip->midi_in_buf);
		kfree(chip->xfer_buf);
		kfree(chip);
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
MODULE_DESCRIPTION("Reloop Jockey 3 Remix ALSA Driver (MIDI Heartbeat)");
MODULE_LICENSE("GPL");
