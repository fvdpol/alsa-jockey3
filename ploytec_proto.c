// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *   Ploytec USB Protocol Handling
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#include <linux/delay.h>
#include "ploytec_proto.h"

/*
 * None of the helpers below validate @intf/@xfer_buf for NULL: callers own
 * the chip's usb_interface and control-transfer buffer for the entire time
 * the PCM/rawmidi devices can be open, so these arguments are always valid
 * here.
 */

/**
 * ploytec_ctrl_ep_unresponsive - Has the control endpoint stopped answering?
 * @err: error returned by an EP0 control transfer
 *
 * Distinguishes a device that answered -- even to refuse -- from one whose
 * control endpoint has gone silent. A STALL (-EPIPE) or a short reply
 * (-EREMOTEIO) both mean live firmware: the transfer completed and the device
 * drove the bus. A timeout or a transport-level error means EP0 itself is gone,
 * and every further control transfer will only burn another
 * PLOYTEC_CTRL_TIMEOUT_MS before failing the same way.
 *
 * The list is affirmative rather than an exclusion, so an unfamiliar error
 * counts as "device still there" and a new class has to be added deliberately.
 *
 * Return: true if EP0 has stopped responding.
 */
static bool ploytec_ctrl_ep_unresponsive(int err)
{
	switch (err) {
	case -ETIMEDOUT:
	case -ENODEV:
	case -ESHUTDOWN:
	case -ECONNRESET:
	case -EPROTO:
	case -EILSEQ:
	case -ETIME:
		return true;
	default:
		return false;
	}
}

/**
 * ploytec_get_firmware - Read firmware version from the device
 * @intf: USB interface
 * @xfer_buf: Temporary transfer buffer (at least 3 bytes)
 * @fw_version: Optional, set to the packed firmware/hardware version on success
 *
 * Performs a request to the device to retrieve the firmware and/or hardware version.
 * Required as part of the handshake sequence regardless of whether the caller
 * wants the value; pass @fw_version as NULL to discard it.
 *
 * Return: 0 on success, negative errno on failure.
 */
int ploytec_get_firmware(struct usb_interface *intf, void *xfer_buf, u32 *fw_version)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	u8 *buf = xfer_buf;
	int ret;

	ret = usb_control_msg_recv(dev, 0, PLOYTEC_REQ_FIRMWARE, PLOYTEC_REQ_FIRMWARE_TYPE, 0, 0,
				   buf, 3, PLOYTEC_CTRL_TIMEOUT_MS, GFP_KERNEL);
	if (ret < 0)
		return ret;

	/*
	 * Three-byte reply: a suspected hardware revision, then the firmware
	 * major and minor. See re/protocol_analysis.md.
	 */
	if (fw_version)
		*fw_version = (buf[0] << 16) | (buf[1] << 8) | buf[2];
	return 0;
}

/**
 * ploytec_get_status - Read device status byte
 * @intf: USB interface
 * @xfer_buf: Temporary transfer buffer (at least 1 byte)
 * @status: Pointer to store the status byte
 *
 * Return: 0 on success, negative errno on failure.
 */
int ploytec_get_status(struct usb_interface *intf, void *xfer_buf, u8 *status)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	u8 *buf = xfer_buf;
	int ret;

	// Read Status (Request 0x49)
	ret = usb_control_msg_recv(dev, 0, PLOYTEC_REQ_STATUS, PLOYTEC_REQ_STATUS_TYPE, 0, 0,
				   buf, 1, PLOYTEC_CTRL_TIMEOUT_MS, GFP_KERNEL);
	if (ret < 0)
		return ret;

	*status = buf[0];
	return 0;
}

/**
 * ploytec_initialize_device - Perform Ploytec handshake sequence as observed in USB traces.
 * @intf: USB interface
 * @xfer_buf: Temporary transfer buffer
 * @bounce_alt0: drive both interfaces to alt 0 before selecting alt 1, which
 *	the vendors do only when changing the rate of a running device
 * @fw_version: Optional, set to the packed firmware/hardware version on success
 *
 * Aborts early if EP0 stops responding, rather than continuing into the
 * alt-setting sequence below; see the comment on the firmware read for why
 * that distinction matters.
 *
 * Return: 0 on success, negative errno on failure.
 */
int ploytec_initialize_device(struct usb_interface *intf, void *xfer_buf, bool bounce_alt0,
			      u32 *fw_version)
{
	struct usb_device *dev = interface_to_usbdev(intf);
	const unsigned int halt_pipes[] = {
		usb_rcvbulkpipe(dev, PLOYTEC_EP_NUM_PCM_IN),
		usb_sndbulkpipe(dev, PLOYTEC_EP_NUM_PCM_OUT),
		usb_rcvbulkpipe(dev, PLOYTEC_EP_NUM_MIDI_IN),
	};
	u8 status;
	unsigned int i;
	int ret;

	/*
	 * The vendors read the firmware version after power-up and the value
	 * is unused here, but this is the sequence's first EP0 transfer and so
	 * the last point at which the usb_set_interface() calls below can
	 * still be avoided -- which is what makes its return load-bearing.
	 * usb_set_interface() disables the interface's endpoints before it
	 * sends SET_INTERFACE and does not re-enable them if that request
	 * fails, so calling it on a device whose control endpoint has already
	 * gone silent leaves every endpoint of interface 0 permanently
	 * disabled and usb_submit_urb() returning -ENOENT until the device is
	 * reset. A device that merely refuses the request is fine; one that
	 * has stopped answering is not.
	 */
	ret = ploytec_get_firmware(intf, xfer_buf, fw_version);
	if (ret < 0) {
		dev_warn(&intf->dev, "Firmware version read failed: %d\n", ret);
		if (ploytec_ctrl_ep_unresponsive(ret))
			return ret;
	}

	/*
	 * Deactivate the audio interfaces before reactivating them, but only
	 * when the device is already running. On a device that has just been
	 * enumerated the interfaces are at alt 0 anyway, and neither vendor
	 * driver touches alt 0 there. macOS does bounce on a rate change, and
	 * takes interface 1 down first, which is the order used here.
	 */
	if (bounce_alt0) {
		ret = usb_set_interface(dev, 1, 0);
		if (ret < 0)
			return ret;
		ret = usb_set_interface(dev, 0, 0);
		if (ret < 0)
			return ret;

		/* Give the hardware some time to respond, otherwise it might not be ready */
		usleep_range(3000, 5000);
	}

	// Select Alt Setting 1 to activate the audio interface
	ret = usb_set_interface(dev, 0, 1);
	if (ret < 0)
		return ret;
	ret = usb_set_interface(dev, 1, 1);
	if (ret < 0)
		return ret;

	/*
	 * The bulk endpoints just activated are not immediately ready for the
	 * usb_clear_halt() calls below: without this pause, EP0 stops
	 * answering anything at all until a full USB re-enumeration clears
	 * it. Only ever reproduced on a single-core host (Pi 1B, armv6),
	 * never on a multi-core one; bisected down to this range with real
	 * device power cycles. See github.com/fvdpol/alsa-jockey3/issues/48.
	 */
	usleep_range(25, 35);

	/*
	 * Clear Feature (ENDPOINT_HALT). A failure here has never been fatal on
	 * a device that is still answering, so only a silent EP0 aborts -- which
	 * also avoids burning two more PLOYTEC_CTRL_TIMEOUT_MS on the remaining
	 * pipes once the first one has timed out.
	 */
	for (i = 0; i < ARRAY_SIZE(halt_pipes); i++) {
		ret = usb_clear_halt(dev, halt_pipes[i]);
		if (ret < 0) {
			dev_warn(&intf->dev, "Failed to clear halt on EP 0x%02x: %d\n",
				 usb_pipeendpoint(halt_pipes[i]) |
					(usb_pipein(halt_pipes[i]) ? USB_DIR_IN : 0),
				 ret);
			if (ploytec_ctrl_ep_unresponsive(ret))
				return ret;
		}
	}

	return ploytec_get_status(intf, xfer_buf, &status);
}
