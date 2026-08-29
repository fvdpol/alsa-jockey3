---
capture: capture_win_power-on_samplerate.txt
captured: 2026-07-09
platform: Windows
host: TODO -- machine this was captured on
os_version: Windows 11 Pro, booted in test mode so the older Reloop driver can load
driver_version: Reloop/Ploytec Windows driver 2.9.73
application: Native Instruments Traktor Pro 3, version 3.11.117
module_build_id: n/a (vendor driver)
kernel_config: n/a (not Linux)
device: Reloop Jockey 3 (confirm Remix 200c:1037 or Master Edition 200c:1019)
size_raw: 2.1 GB
usb_address: 0, 15
has_control_traffic: yes
---

# capture_win_power-on_samplerate.txt

## Objective

Capture the Windows driver at power-on and then through a series of
sample-rate changes.

## Conclusion

One cold init plus six warm rate changes. Windows reference for
`../init_timing_comparison.md`. Note the SET_RATE burst here is 7 writes
against 5 in the power-cycle capture -- the count is not an invariant, the
trailing endpoint (0x86) is.

## Contents (derived)

- Duration: 66.224 s, 761300 USB transactions
- Device address(es): 0, 15
- Endpoints seen: 0x00 (511), 0x02 (144), 0x03 (1177), 0x05 (339404), 0x06 (420064)
- Control transfers: 184 in 9 events
- Event kinds: control x2, init+rate x7
- Sample rates programmed: 44100 Hz, 48000 Hz, 88200 Hz, 96000 Hz

### Events

| # | kind | at (s) | transfers | span (ms) |
|---|---|---|---|---|
| 1 | init+rate | 17.756658 | 32 | 439.153 |
| 2 | control | 18.645456 | 2 | 1.431 |
| 3 | init+rate | 62.586503 | 23 | 147.789 |
| 4 | control | 62.985228 | 2 | 0.805 |
| 5 | init+rate | 64.630754 | 25 | 393.507 |
| 6 | init+rate | 66.944204 | 25 | 401.619 |
| 7 | init+rate | 69.632565 | 25 | 397.484 |
| 8 | init+rate | 70.814436 | 25 | 395.367 |
| 9 | init+rate | 76.941846 | 25 | 392.972 |
