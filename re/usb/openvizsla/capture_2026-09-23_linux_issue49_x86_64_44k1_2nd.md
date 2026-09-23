---
capture: capture_2026-09-23_linux_issue49_x86_64_44k1_2nd.txt
captured: 2026-09-23
platform: Linux
host: alsa-test; Elitedesk G2; i5 6500; x86_64-prod kernel
os_version: Linux alsa-test 7.3.0-rc1-alsa-prod+ #8 SMP PREEMPT_DYNAMIC Thu Sep 17 12:58:49 AST 2026 x86_64 GNU/Linux
driver_version: test/issue48-ep0-set-interface-trace @ b9c4557 (--uncommitted build)
application: issue49_vbus_cut_sweep.py --arm active
module_build_id: e886bb15527d5c1bc78f4646cbd9ae085dcceb3c
kernel_config: x86_64-prod
device: Reloop Jockey 3 Remix
usb_address: 43
has_control_traffic: yes
filter_nak: true
capture_tool: ov-snapshot (triggered), pi4test capture host, pypy3
---

# capture_2026-09-23_linux_issue49_x86_64_44k1_2nd.txt

## Objective

Second x86_64-prod capture from the same sweep as
`capture_2026-09-23_linux_issue49_x86_64_44k1.md` -- corroborating sample,
not analyzed in the same depth.

## Conclusion

Same signature: `SET_RATE(ep=0x86)` SETUP accepted on address 43, then
**52.6ms** to the first sign of other bus activity, full re-enumeration
to a new address, retry succeeds. Duration in the same class as the
first x86_64 sample (54.5ms) and the i386-prod samples.
