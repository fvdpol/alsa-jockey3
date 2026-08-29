---
capture: capture_macos_rate_change.txt
captured: 2026-06-28
platform: macOS
host: TODO -- machine this was captured on
os_version: macOS 10.15.7 "Catalina"
driver_version: Reloop/Ploytec CoreAudio driver 3.3.17
application: Native Instruments Traktor Pro 3, version 3.8.46
module_build_id: n/a (vendor driver)
kernel_config: n/a (not Linux)
device: Reloop Jockey 3 (confirm Remix 200c:1037 or Master Edition 200c:1019)
size_raw: 691.4 MB
usb_address: 8
has_control_traffic: yes
---

# capture_macos_rate_change.txt

## Objective

Capture the macOS driver performing repeated sample-rate changes, to
establish the reference rate-change sequence and its timing.

## Conclusion

Seven clean warm rate changes. This is the primary macOS reference for
`../init_timing_comparison.md` -- source of the ~50 ms quiet window
measurements and the finding that the rate-write burst ends on EP 0x86.

## Contents (derived)

- Duration: 22.392 s, 236046 USB transactions
- Device address(es): 8
- Endpoints seen: 0x00 (322), 0x02 (86), 0x03 (883), 0x05 (104531), 0x06 (130224)
- Control transfers: 126 in 7 events
- Event kinds: init+rate x7
- Sample rates programmed: 44100 Hz, 48000 Hz, 88200 Hz, 96000 Hz

### Events

| # | kind | at (s) | transfers | span (ms) |
|---|---|---|---|---|
| 1 | init+rate | 10.822215 | 18 | 123.915 |
| 2 | init+rate | 12.559643 | 18 | 124.133 |
| 3 | init+rate | 14.173147 | 18 | 123.154 |
| 4 | init+rate | 16.379605 | 18 | 124.294 |
| 5 | init+rate | 17.765546 | 18 | 123.189 |
| 6 | init+rate | 19.312582 | 18 | 124.033 |
| 7 | init+rate | 20.827644 | 18 | 124.539 |
