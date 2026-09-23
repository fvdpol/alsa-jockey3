---
capture: capture_2026-09-23_linux_issue49_x86_64_44k1_3rd.txt
captured: 2026-09-23
platform: Linux
host: alsa-test; Elitedesk G2; i5 6500; x86_64-prod kernel
os_version: Linux alsa-test 7.3.0-rc1-alsa-prod+ #8 SMP PREEMPT_DYNAMIC Thu Sep 17 12:58:49 AST 2026 x86_64 GNU/Linux
driver_version: test/issue48-ep0-set-interface-trace @ b9c4557 (--uncommitted build)
application: issue49_vbus_cut_sweep.py --arm active
module_build_id: e886bb15527d5c1bc78f4646cbd9ae085dcceb3c
kernel_config: x86_64-prod
device: Reloop Jockey 3 Remix
usb_address: 50
has_control_traffic: yes
filter_nak: true
capture_tool: ov-snapshot (triggered), pi4test capture host, pypy3
---

# capture_2026-09-23_linux_issue49_x86_64_44k1_3rd.txt

## Objective

Third x86_64-prod capture from the same sweep as
`capture_2026-09-23_linux_issue49_x86_64_44k1.md` -- corroborating sample.

## Conclusion

Same signature: `SET_RATE(ep=0x86)` SETUP accepted on address 50, then
**197.8ms** to the first sign of other bus activity (this one also shows
a hub interrupt-endpoint status change immediately preceding it),
followed by re-enumeration and a successful retry. Third distinct
duration in this small x86_64 sample set (54.5ms, 52.6ms, 197.8ms) --
same "bounded below, not fixed" pattern already seen on i386-prod.
