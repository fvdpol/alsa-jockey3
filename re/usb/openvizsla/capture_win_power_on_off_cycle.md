---
capture: capture_win_power_on_off_cycle.txt
captured: 2026-07-09
platform: Windows
host: TODO -- machine this was captured on
os_version: Windows 11 Pro, booted in test mode so the older Reloop driver can load
driver_version: Reloop/Ploytec Windows driver 2.9.73
application: Native Instruments Traktor Pro 3, version 3.11.117
module_build_id: n/a (vendor driver)
kernel_config: n/a (not Linux)
device: Reloop Jockey 3 (confirm Remix 200c:1037 or Master Edition 200c:1019)
size_raw: 804.4 MB
usb_address: 0, 16, 17, 18, 19
has_control_traffic: yes
---

# capture_win_power_on_off_cycle.txt

## Objective

Capture the Windows driver across repeated device power-off/power-on
cycles, to see the cold-initialization sequence more than once.

## Conclusion

Four cold inits, highly consistent. Main source of the Windows cold-init
timing in `../init_timing_comparison.md`.

## Contents (derived)

- Duration: 40.765 s, 260077 USB transactions
- Device address(es): 0, 16, 17, 18, 19
- Endpoints seen: 0x00 (364), 0x02 (14), 0x03 (25), 0x05 (115552), 0x06 (144122)
- Control transfers: 128 in 8 events
- Event kinds: control x4, init+rate x4
- Sample rates programmed: 44100 Hz

### Events

| # | kind | at (s) | transfers | span (ms) |
|---|---|---|---|---|
| 1 | init+rate | 9.110302 | 30 | 432.854 |
| 2 | control | 9.991061 | 2 | 0.853 |
| 3 | init+rate | 21.674729 | 30 | 446.283 |
| 4 | control | 22.565278 | 2 | 0.964 |
| 5 | init+rate | 32.186668 | 30 | 411.161 |
| 6 | control | 33.098714 | 2 | 0.796 |
| 7 | init+rate | 43.146440 | 30 | 440.555 |
| 8 | control | 44.045897 | 2 | 0.838 |
