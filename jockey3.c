/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Reloop Jockey 3 Remix ALSA Driver (Handshake Part 2)
 *
 * This driver claims the Reloop Jockey 3 Remix (200c:1037) and performs
 * the full Ploytec handshake: firmware read, status read, altsetting
 * switch, and status confirmation (arming).
 */

#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>

#define RELOOP_VENDOR_ID          0x200c
#define RELOOP_JOCKEY3_REMIX_PID  0x1037
#define RELOOP_JOCKEY3_MASTER_PID 0x1009

/* Ploytec Vendor Commands */
#define PLOYTEC_CMD_FIRMWARE        0x56
#define PLOYTEC_CMD_STATUS          0x49
#define PLOYTEC_REG_AJ_INPUT_SEL    0

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
};

static int jockey3_handshake(struct jockey3_chip *chip)
{
	int ret;
	uint8_t status;
	uint16_t wvalue;

	/* 1. Read Firmware Version (15 bytes) */
	ret = usb_control_msg(chip->dev, usb_rcvctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_FIRMWARE, 0xC0, 0x0000, 0,
			      chip->xfer_buf, 15, 2000);
	if (ret < 0)
		return ret;

	dev_info(&chip->intf0->dev, "Firmware read: %*ph\n", 15, chip->xfer_buf);

	/* 2. Read Status Byte */
	ret = usb_control_msg(chip->dev, usb_rcvctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_STATUS, 0xC0, 0x0000,
			      PLOYTEC_REG_AJ_INPUT_SEL,
			      chip->xfer_buf, 1, 2000);
	if (ret < 0)
		return ret;

	status = chip->xfer_buf[0];
	dev_info(&chip->intf0->dev, "Initial status: 0x%02x\n", status);

	/* 3. Set Alternate Setting 1 for both interfaces */
	ret = usb_set_interface(chip->dev, 0, 1);
	if (ret < 0)
		return ret;

	ret = usb_set_interface(chip->dev, 1, 1);
	if (ret < 0)
		return ret;

	/* 4. Confirm Status (write back with bit 5 set)
	 * Official driver uses (short)(char)(status | 0x20)
	 * which sets MODE5 (LegacyActive) and sign-extends.
	 */
	wvalue = (uint16_t)(int16_t)(int8_t)(status | 0x20);

	ret = usb_control_msg(chip->dev, usb_sndctrlpipe(chip->dev, 0),
			      PLOYTEC_CMD_STATUS, 0x40,
			      wvalue, PLOYTEC_REG_AJ_INPUT_SEL,
			      NULL, 0, 2000);
	if (ret < 0)
		return ret;

	dev_info(&chip->intf0->dev, "Handshake complete, device armed (wrote 0x%04x)\n", wvalue);

	return 0;
}

static struct usb_driver jockey3_driver; /* forward declaration */

static int jockey3_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	struct usb_interface *intf1;
	struct jockey3_chip *chip;
	int ret;

	/* Only handle Interface 0 in probe; we claim Interface 1 manually */
	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;

	dev_info(&intf->dev, "Reloop Jockey 3 Remix detected (VID=0x%04x, PID=0x%04x)\n",
		 id->idVendor, id->idProduct);

	intf1 = usb_ifnum_to_if(dev, 1);
	if (!intf1) {
		dev_err(&intf->dev, "Interface 1 not found\n");
		return -ENODEV;
	}

	chip = kzalloc(sizeof(*chip), GFP_KERNEL);
	if (!chip)
		return -ENOMEM;

	chip->xfer_buf = kmalloc(64, GFP_KERNEL);
	if (!chip->xfer_buf) {
		kfree(chip);
		return -ENOMEM;
	}

	chip->dev = dev;
	chip->intf0 = intf;
	chip->intf1 = intf1;

	/* Claim Interface 1 */
	ret = usb_driver_claim_interface(&jockey3_driver, intf1, chip);
	if (ret < 0) {
		dev_err(&intf->dev, "Failed to claim interface 1: %d\n", ret);
		goto err_free;
	}

	usb_set_intfdata(intf, chip);

	ret = jockey3_handshake(chip);
	if (ret < 0) {
		dev_err(&intf->dev, "Handshake failed: %d\n", ret);
		goto err_unclaim;
	}

	return 0;

err_unclaim:
	usb_driver_release_interface(&jockey3_driver, intf1);
err_free:
	kfree(chip->xfer_buf);
	kfree(chip);
	return ret;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip) {
		/* Only handle disconnect for the primary interface */
		if (intf == chip->intf0) {
			usb_driver_release_interface(&jockey3_driver, chip->intf1);
			dev_info(&intf->dev, "Reloop Jockey 3 Remix disconnected.\n");
			kfree(chip->xfer_buf);
			kfree(chip);
		}
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
MODULE_DESCRIPTION("Reloop Jockey 3 Remix ALSA Driver (Handshake Part 2)");
MODULE_LICENSE("GPL");
