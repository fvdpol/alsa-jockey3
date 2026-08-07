/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 *   ALSA driver for Reloop Jockey 3 devices
 *   Ploytec USB Protocol Handling
 *
 *   Copyright (c) 2026 by Frank van de Pol <fvdpol@gmail.com>
 */

#ifndef PLOYTEC_PROTO_H
#define PLOYTEC_PROTO_H

#include <linux/types.h>
#include <linux/usb.h>

#define USB_XFER_BUF_SIZE		64	// temporary buffer for USB control transfers

/* Ploytec Protocol Constants */
#define PLOYTEC_PKT_SIZE		512

/* Playback & MIDI Out (EP 0x05) */
#define PLOYTEC_EP_NUM_PCM_OUT		0x05
#define PLOYTEC_MIDI_OUT_OFFSET		480	// location of MIDI data in the playback packet
#define PLOYTEC_MIDI_IDLE_BYTE		0xFD	// send when no MIDI data is available
#define PLOYTEC_SYNC_BYTE_OFFSET	481
#define PLOYTEC_SYNC_BYTE_VALUE		0xFF

/* Capture (EP 0x86) */
#define PLOYTEC_EP_NUM_PCM_IN		0x06	// 0x86 & 0x0F

/* MIDI In (EP 0x83) */
#define PLOYTEC_EP_NUM_MIDI_IN		0x03	// 0x83 & 0x0F

/* Protocol Commands */
#define PLOYTEC_SET_RATE		0x01	// bRequest to set sample rate
#define PLOYTEC_SET_RATE_TYPE		0x22	// bmRequestType to set sample rate
#define PLOYTEC_REQ_STATUS		0x49	// bRequest to get device status
#define PLOYTEC_REQ_STATUS_TYPE		0xC0	// bmRequestType to get device status
#define PLOYTEC_REQ_FIRMWARE		0x56	// bRequest to get firmware version
#define PLOYTEC_REQ_FIRMWARE_TYPE	0xC0	// bmRequestType to get firmware version
#define PLOYTEC_REQ_GET_RATE		0x81	// bRequest to get current sample rate
#define PLOYTEC_REQ_GET_RATE_TYPE	0xA2	// bmRequestType to get current sample rate

/* Status Bits */
#define PLOYTEC_STATUS_BIT0		0x01
#define PLOYTEC_STATUS_BIT1		0x02
#define PLOYTEC_STATUS_BIT2		0x04
#define PLOYTEC_STATUS_BIT3		0x08
#define PLOYTEC_STATUS_BIT4		0x10
#define PLOYTEC_STATUS_STREAMING	0x20

/* Protocol Helpers */
void ploytec_prepare_out_packet(u8 *buf);
int ploytec_initialise_device(struct usb_device *dev, void *xfer_buf);
int ploytec_start_streaming(struct usb_device *dev, void *xfer_buf);
int ploytec_get_rate(struct usb_device *dev, void *xfer_buf, u32 *rate);
int ploytec_set_rate(struct usb_device *dev, void *xfer_buf, u32 rate);
int ploytec_get_firmware(struct usb_device *dev, void *xfer_buf);
int ploytec_get_status(struct usb_device *dev, void *xfer_buf);

#endif /* PLOYTEC_PROTO_H */
