/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Reloop Jockey 3 Remix ALSA Driver (Skeleton)
 *
 * This is a minimal driver to claim the Reloop Jockey 3 Remix (200c:1037).
 * It does not yet implement any audio or MIDI functionality.
 */

#include <linux/module.h>
#include <linux/usb.h>
#include <linux/slab.h>

#define RELOOP_VENDOR_ID         0x200c
#define RELOOP_JOCKEY3_REMIX_PID 0x1037
#define RELOOP_JOCKEY3_MASTER_PID 0x1009

static const struct usb_device_id jockey3_ids[] = {
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_REMIX_PID) },
	{ USB_DEVICE(RELOOP_VENDOR_ID, RELOOP_JOCKEY3_MASTER_PID) }, /* Unverified */
	{ } /* Terminating entry */
};
MODULE_DEVICE_TABLE(usb, jockey3_ids);

struct jockey3_chip {
	struct usb_device *dev;
	struct usb_interface *intf;
};

static int jockey3_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	struct jockey3_chip *chip;

	dev_info(&intf->dev, "Reloop Jockey 3 Remix detected (VID=0x%04x, PID=0x%04x)\n",
		 id->idVendor, id->idProduct);

	/* For now, we only claim Interface 0. In a real Ploytec device,
	 * we usually need to claim all interfaces. */
	if (intf->cur_altsetting->desc.bInterfaceNumber != 0)
		return -ENODEV;

	chip = kzalloc(sizeof(*chip), GFP_KERNEL);
	if (!chip)
		return -ENOMEM;

	chip->dev = dev;
	chip->intf = intf;

	usb_set_intfdata(intf, chip);

	dev_info(&intf->dev, "Reloop Jockey 3 Remix interface 0 claimed.\n");

	return 0;
}

static void jockey3_disconnect(struct usb_interface *intf)
{
	struct jockey3_chip *chip = usb_get_intfdata(intf);

	if (chip) {
		dev_info(&intf->dev, "Reloop Jockey 3 Remix disconnected.\n");
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
MODULE_DESCRIPTION("Reloop Jockey 3 Remix ALSA Driver Skeleton");
MODULE_LICENSE("GPL");
