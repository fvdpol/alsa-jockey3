---
capture: capture_2026-09-23_linux_issue49_set_rate_stall_96k_3rd.txt
captured: 2026-09-23
platform: Linux
host: alsa-test-32bit; Elitedesk G2; i5 6500; i386-prod kernel
os_version: Linux alsa-test-32bit 7.3.0-rc1-alsa-prod+ #1 SMP PREEMPT_DYNAMIC Tue Sep 22 09:21:39 AST 2026 i686 GNU/Linux
driver_version: main
application: issue49_vbus_cut_sweep.py --arm active
module_build_id: 77fdbe2ae1ef5d2c13da357ba1176604cd9db3e2
kernel_config: i386-prod
device: Reloop Jockey 3 Remix
usb_address: 7
has_control_traffic: yes
filter_nak: true
capture_tool: ov-snapshot (triggered), pi4test capture host, pypy3
---

# capture_2026-09-23_linux_issue49_set_rate_stall_96k_3rd.txt

## Objective

Third OpenVizsla capture of a live `set_rate_ep` failure
(github.com/fvdpol/alsa-jockey3/issues/49), caught by the same triggered
pipeline as the other two, at 96000 Hz this time (the first two were
44100/48000) -- picked up automatically right before Frank's planned
reboot of this host into its separate 64-bit rootfs, so it landed
unprocessed until after that reboot.

## Conclusion

Same signature as the other two: `GET_RATE` succeeds (96000 Hz), then
`SET_RATE(ep=0x86)`'s SETUP stage is sent (device address 7), no NAKs
anywhere in the capture (`filter_nak=true`, and `parse_openvizsla.py`
aggregated zero NAK/STALL bursts), and then **273.75ms** of total silence
on the bus -- no PING, no retry, nothing -- before a full bus reset and
re-enumeration to a new address (50).

This is the same qualitative mechanism as the 44k1/48k captures (device
accepts the SETUP, host/device both go silent, recovery is a full
re-enumeration), but the duration is **not** the same: 273.75ms here vs.
~41.4-41.8ms in the other two wire captures and 42.6-42.8ms in the two
`ISSUE48_TRACE()` kernel samples. Four samples clustered near 42ms and
one at 273ms is not "tight repeatability" -- **the earlier characterization
in `re/usb/issue49_set_rate_stall_analysis.md` overstated this: the
duration is bounded below by something like ~42ms but is not fixed.**
Consistent with a bounded xHCI error-recovery/retry sequence whose actual
length depends on what else is queued, rather than a single hardcoded
timeout constant. Doc and GH issue #49 updated to reflect this.
