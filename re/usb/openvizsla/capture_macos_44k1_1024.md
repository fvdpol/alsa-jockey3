---
capture: capture_macos_44k1_1024.txt
captured: 2026-06-28
platform: macOS
host: TODO -- machine this was captured on
os_version: macOS 10.15.7 "Catalina"
driver_version: Reloop/Ploytec CoreAudio driver 3.3.17
application: Native Instruments Traktor Pro 3, version 3.8.46
module_build_id: n/a (vendor driver)
kernel_config: n/a (not Linux)
device: Reloop Jockey 3 (confirm Remix 200c:1037 or Master Edition 200c:1019)
size_raw: 91.3 MB
usb_address: 27
has_control_traffic: no
---

# capture_macos_44k1_1024.txt

## Objective

The buffer-size study. These traces were taken at different buffer-size
settings in Traktor to establish whether that setting affects only the
host-side CoreAudio buffer or also the on-wire packet format.
This one at a 1024-sample buffer, 44.1 kHz.

## Conclusion

**The buffer size is local to the host.** It changes nothing in the USB
transactions -- the same 512-byte packets at the same cadence regardless of
the setting. The absence of EP0 traffic in these traces is therefore expected
and correct, not a botched capture: only the streaming phase was of interest.

## Contents (derived)

- Duration: 2.900 s, 28986 USB transactions
- Device address(es): 27
- Endpoints seen: 0x03 (196), 0x05 (12796), 0x06 (15994)
- Control transfers: 0 in 0 events
- Event kinds: none
- Sample rates programmed: none observed

> **No EP0 traffic.** This trace cannot contribute to any
> control-plane analysis (initialization, rate change). If that was
> not the intent, the capture missed the event; if it was, say so
> under Objective.
