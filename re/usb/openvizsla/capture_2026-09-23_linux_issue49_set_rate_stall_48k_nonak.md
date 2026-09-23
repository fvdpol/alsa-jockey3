---
capture: capture_2026-09-23_linux_issue49_set_rate_stall_48k_nonak.txt
captured: 2026-09-23
platform: Linux
host: alsa-test-32bit; Elitedesk G2; i5 6500; i386-prod kernel
os_version: Linux alsa-test-32bit 7.3.0-rc1-alsa-prod+ #1 SMP PREEMPT_DYNAMIC Tue Sep 22 09:21:39 AST 2026 i686 GNU/Linux
driver_version: main
application: issue49_vbus_cut_sweep.py --arm active
module_build_id: 77fdbe2ae1ef5d2c13da357ba1176604cd9db3e2
kernel_config: i386-prod
device: Reloop Jockey 3 Remix
usb_address: 49
has_control_traffic: yes
filter_nak: false
capture_tool: ov-snapshot (triggered), pi4test capture host, pypy3
---

# capture_2026-09-23_linux_issue49_set_rate_stall_48k_nonak.txt

## Objective

Follow-up to `capture_2026-09-23_linux_issue49_set_rate_stall_44k1.txt`,
with the OpenVizsla NAK filter disabled: that first capture's ~41ms
silence could in principle have been a NAK storm the filter hid rather
than genuine silence, since `filter_nak=true` drops PING/NAK handshake
traffic in gateware. Rerun the identical scenario without the filter to
tell the two apart.

## Conclusion

Identical shape to the first capture (device address 49 this time,
48000 Hz): `GET_RATE` succeeds, `SET_RATE(ep=0x86)` SETUP sent and ACKed,
then ~41.8ms of silence, 3 unanswered PINGs, 6 unanswered
`SET_INTERFACE(1,0)` retries, device reappears at a new address. **With
the filter off, there is still no NAK anywhere in that window** -- checked
that the tooling reliably captures NAK/ACK responses when the device
gives one (every other PING elsewhere in this same trace gets an ACK
within 24us). So it's genuine device silence, not a hidden NAK storm.
Full analysis: `re/usb/issue49_set_rate_stall_analysis.md`.
