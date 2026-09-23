---
capture: capture_2026-09-23_linux_issue49_set_rate_stall_44k1.txt
captured: 2026-09-23
platform: Linux
host: alsa-test-32bit; Elitedesk G2; i5 6500; i386-prod kernel
os_version: Linux alsa-test-32bit 7.3.0-rc1-alsa-prod+ #1 SMP PREEMPT_DYNAMIC Tue Sep 22 09:21:39 AST 2026 i686 GNU/Linux
driver_version: main
application: issue49_vbus_cut_sweep.py --arm active
module_build_id: 77fdbe2ae1ef5d2c13da357ba1176604cd9db3e2
kernel_config: i386-prod
device: Reloop Jockey 3 Remix
usb_address: 37
has_control_traffic: yes
filter_nak: true
capture_tool: ov-snapshot (triggered), pi4test capture host, pypy3
---

# capture_2026-09-23_linux_issue49_set_rate_stall_44k1.txt

## Objective

First OpenVizsla capture of a real, live `set_rate_ep` failure
(github.com/fvdpol/alsa-jockey3/issues/49): triggered automatically off
the kernel log line `Failed to set rate on EP 0x86: -71` during
`issue49_vbus_cut_sweep.py --arm active`, to see what the device is doing
on the wire at the exact point host-side traces have always shown clean
right up to.

## Conclusion

`GET_RATE` succeeds (44100 Hz), then `SET_RATE(ep=0x86)`'s SETUP stage is
sent and cleanly ACKed by the device -- and then nothing. No OUT data
phase, no STATUS stage, no STALL, no rejection: ~41.4ms of silence on
that address (device address 37), 3 unanswered PING probes, 6 retries of
an unrelated `SET_INTERFACE(intf=1, alt=0)` SETUP also unanswered, then
the device vanishes and reappears at a new address (50) -- a full bus
reset. Full analysis, including a second capture with `filter_nak=false`
that rules out a hidden NAK storm, per-token timing comparison against a
successful `SET_RATE`, and SOF/frame-counter proof the bus itself never
stalled: `re/usb/issue49_set_rate_stall_analysis.md`.
