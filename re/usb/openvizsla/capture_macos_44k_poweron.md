---
capture: capture_macos_44k_poweron.txt
captured: 2026-06-28
platform: macOS
host: TODO -- machine this was captured on
os_version: macOS 10.15.7 "Catalina"
driver_version: Reloop/Ploytec CoreAudio driver 3.3.17
application: Native Instruments Traktor Pro 3, version 3.8.46
module_build_id: n/a (vendor driver)
kernel_config: n/a (not Linux)
device: Reloop Jockey 3 (confirm Remix 200c:1037 or Master Edition 200c:1019)
size_raw: 107.6 MB
usb_address: 0, 8
has_control_traffic: yes
---

# capture_macos_44k_poweron.txt

## Objective

Capture the macOS driver bringing the device up from power-on, to
establish the reference cold-initialization sequence.

## Conclusion

One enumeration and one cold init. The only macOS cold init in the corpus
(n=1), which is why more macOS power-on captures are wanted. Analyzed in
`../init_timing_comparison.md`.

## Contents (derived)

- Duration: 4.582 s, 33967 USB transactions
- Device address(es): 0, 8
- Endpoints seen: 0x00 (74), 0x03 (8), 0x05 (15061), 0x06 (18824)
- Control transfers: 27 in 2 events
- Event kinds: enumeration x1, init+rate x1
- Sample rates programmed: 44100 Hz

### Events

| # | kind | at (s) | transfers | span (ms) |
|---|---|---|---|---|
| 1 | enumeration | 3.041335 | 11 | 16.050 |
| 2 | init+rate | 4.087164 | 16 | 108.775 |
