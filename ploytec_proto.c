// SPDX-License-Identifier: GPL-2.0-or-later
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *   Ploytec USB Protocol Handling
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#include <linux/delay.h>
#include "ploytec_proto.h"

/**
 * ploytec_prepare_out_packet - Prepare a playback packet with default sync/MIDI padding
 * @buf: 512-byte destination buffer
 *
 * Sets the initial pattern: MIDI slot (480) is idle (0xFD), sync byte is
 * set to 0xFF at offset 481, and the padding gap (482-511) is zero-filled.
 */
void ploytec_prepare_out_packet(u8 *buf)
{
	memset(buf, 0, PLOYTEC_PKT_SIZE);
	buf[PLOYTEC_MIDI_OUT_OFFSET] = PLOYTEC_MIDI_IDLE_BYTE;
	buf[PLOYTEC_SYNC_BYTE_OFFSET] = PLOYTEC_SYNC_BYTE_VALUE;
}

/**
 * ploytec_get_firmware - Read firmware version from the device
 * @dev: USB device
 * @xfer_buf: Temporary transfer buffer (at least 3 bytes)
 *
 * Performs a request to the device to retrieve the firmware and/or hardware version.
 * We are doing anything useful with the firmware version yet, but it seems to be required
 * to request as per the USB traces.
 */
int ploytec_get_firmware(struct usb_device *dev, void *xfer_buf)
{
	u8 *buf = xfer_buf;
	int ret;

	if (!dev)
		return -ENODEV;
	if (!xfer_buf)
		return -EINVAL;

	ret = usb_control_msg_recv(dev, 0, PLOYTEC_REQ_FIRMWARE, PLOYTEC_REQ_FIRMWARE_TYPE, 0, 0,
				   buf, 3, 2000, GFP_KERNEL);
	if (ret < 0)
		return ret;

	// device with firmware v1.0.3  returns: 0x31, 0x01, 0x03
	// device with firmware v1.0.6  returns: 0x31, 0x01, 0x06
	// buf[0] = 0x31, educated guess this may be hardware model/revision
	pr_debug("ploytec: Firmware  0x%02x v%d.%d.%d\n", buf[0],
		 buf[1], buf[2] >> 4, buf[2] & 0x0F);
	return 0;
}

int ploytec_get_status(struct usb_device *dev, void *xfer_buf)
{
	u8 *buf = xfer_buf;
	int ret;

	if (!dev)
		return -ENODEV;
	if (!xfer_buf)
		return -EINVAL;

	/* Read Status (Request 0x49) */
	ret = usb_control_msg_recv(dev, 0, PLOYTEC_REQ_STATUS, PLOYTEC_REQ_STATUS_TYPE, 0, 0,
				   buf, 1, 2000, GFP_KERNEL);
	if (ret < 0)
		return ret;

	return buf[0];
}

/**
 * ploytec_initialise_device - Perform Ploytec handshake sequence as observed in USB traces.
 * @dev: USB device
 * @xfer_buf: Temporary transfer buffer
 */
int ploytec_initialise_device(struct usb_device *dev, void *xfer_buf)
{
	int ret;

	// USB trace shows we need to read firmware version after power-up
	ploytec_get_firmware(dev, xfer_buf);

	// Select Alt Setting 0 to deactivates the audio interface
	ret = usb_set_interface(dev, 0, 0);
	if (ret < 0)
		return ret;
	ret = usb_set_interface(dev, 1, 0);
	if (ret < 0)
		return ret;

	/* Give the hardware some time to respond, otherwise it might not be ready */
	usleep_range(3000, 5000);

	// Select Alt Setting 1 to activate the audio interface
	ret = usb_set_interface(dev, 0, 1);
	if (ret < 0)
		return ret;
	ret = usb_set_interface(dev, 1, 1);
	if (ret < 0)
		return ret;

	// Clear Feature (ENDPOINT_HALT):
	usb_clear_halt(dev, usb_rcvbulkpipe(dev, 0x86));
	usb_clear_halt(dev, usb_sndbulkpipe(dev, 0x05));
	usb_clear_halt(dev, usb_rcvbulkpipe(dev, 0x83));

	return ploytec_get_status(dev, xfer_buf);
}

/**
 * ploytec_start_streaming - Trigger the device to start streaming as observed in USB traces.
 * @dev: USB device
 * @xfer_buf: Temporary transfer buffer
 */
int ploytec_start_streaming(struct usb_device *dev, void *xfer_buf)
{
	u8 status;
	int ret;

	if (!dev)
		return -ENODEV;
	if (!xfer_buf)
		return -EINVAL;

	status = ploytec_get_status(dev, xfer_buf);
	pr_debug("ploytec: Start Streaming: Status: 0x%02x\n", status);

	/* Enable device if STREAMING bit is not set */
	if (!(status & PLOYTEC_STATUS_STREAMING)) {
		ret = usb_control_msg_send(dev, 0, PLOYTEC_REQ_STATUS, 0x40,
					   (uint16_t)((status | PLOYTEC_STATUS_STREAMING) & 0xFF),
					   0, NULL, 0, 2000, GFP_KERNEL);
		if (ret < 0)
			return ret;
	}
	return ploytec_get_status(dev, xfer_buf);
}

/**
 * ploytec_get_rate - Read hardware sample rate
 * @dev: USB device
 * @xfer_buf: Temporary transfer buffer
 * @rate: Pointer to store the rate
 */
int ploytec_get_rate(struct usb_device *dev, void *xfer_buf, u32 *rate)
{
	u8 *buf = xfer_buf;
	int ret;

	if (!dev)
		return -ENODEV;
	if (!xfer_buf)
		return -EINVAL;

	/* Read rate from Playback EP 0x05 */
	ret = usb_control_msg_recv(dev, 0, PLOYTEC_REQ_GET_RATE, PLOYTEC_REQ_GET_RATE_TYPE,
				   0x0100, PLOYTEC_EP_NUM_PCM_OUT | USB_DIR_OUT,
				   buf, 3, 2000, GFP_KERNEL);
	if (ret < 0)
		return ret;

	*rate = (u32)buf[0] | ((u32)buf[1] << 8) | ((u32)buf[2] << 16);
	return 0;
}

/**
 * ploytec_set_rate - Set hardware sample rate
 * @dev: USB device
 * @xfer_buf: Temporary transfer buffer
 * @rate: Sample rate in Hz
 */
int ploytec_set_rate(struct usb_device *dev, void *xfer_buf, u32 rate)
{
	u8 *buf = xfer_buf;
	u32 current_hw_rate = 0;
	int ret;

	if (!dev)
		return -ENODEV;
	if (!xfer_buf)
		return -EINVAL;

	ploytec_get_rate(dev, xfer_buf, &current_hw_rate);
	pr_debug("ploytec: Setting rate %u Hz (current hw rate: %u Hz)\n",
		 rate, current_hw_rate);

	buf[0] = rate & 0xFF;
	buf[1] = (rate >> 8) & 0xFF;
	buf[2] = (rate >> 16) & 0xFF;

	/* Set rate on Capture EP 0x86 */
	ret = usb_control_msg_send(dev, 0, PLOYTEC_SET_RATE, PLOYTEC_SET_RATE_TYPE,
				   0x0100, PLOYTEC_EP_NUM_PCM_IN | USB_DIR_IN,
				   buf, 3, 2000, GFP_KERNEL);
	if (ret < 0) {
		pr_err("ploytec: Failed to set rate on EP 0x86: %d\n", ret);
		return ret;
	}

	/* 10ms delay to allow device to process the command, as per MacOS driver behavior */
	usleep_range(10000, 11000);

	/* and after that delay the device is repeatedly "hammered" with rate again... */
	for (int i = 0; i < 3; i++) {
		/* Set rate on Capture EP 0x86 */
		ret = usb_control_msg_send(dev, 0, PLOYTEC_SET_RATE, PLOYTEC_SET_RATE_TYPE,
					   0x0100, PLOYTEC_EP_NUM_PCM_IN | USB_DIR_IN,
					   buf, 3, 2000, GFP_KERNEL);
		if (ret < 0) {
			pr_err("ploytec: Failed to set rate on EP 0x86: %d\n", ret);
			return ret;
		}

		/* Set rate on Playback EP 0x05 */
		ret = usb_control_msg_send(dev, 0, PLOYTEC_SET_RATE, PLOYTEC_SET_RATE_TYPE,
					   0x0100, PLOYTEC_EP_NUM_PCM_OUT | USB_DIR_OUT,
					   buf, 3, 2000, GFP_KERNEL);
		if (ret < 0) {
			pr_err("ploytec: Failed to set rate on EP 0x05: %d\n", ret);
			return ret;
		}
	}

	if (ploytec_get_rate(dev, xfer_buf, &current_hw_rate) == 0) {
		if (current_hw_rate != rate)
			pr_warn("ploytec: Rate mismatch! Requested %u Hz, Hardware at %u Hz\n",
				rate, current_hw_rate);
		else
			pr_debug("ploytec: Rate verified as %u Hz\n", current_hw_rate);
	}

	return 0;
}
