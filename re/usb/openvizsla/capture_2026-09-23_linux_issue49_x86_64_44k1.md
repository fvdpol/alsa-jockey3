---
capture: capture_2026-09-23_linux_issue49_x86_64_44k1.txt
captured: 2026-09-23
platform: Linux
host: alsa-test; Elitedesk G2; i5 6500; x86_64-prod kernel
os_version: Linux alsa-test 7.3.0-rc1-alsa-prod+ #8 SMP PREEMPT_DYNAMIC Thu Sep 17 12:58:49 AST 2026 x86_64 GNU/Linux
driver_version: test/issue48-ep0-set-interface-trace @ b9c4557 (--uncommitted build)
application: issue49_vbus_cut_sweep.py --arm active
module_build_id: e886bb15527d5c1bc78f4646cbd9ae085dcceb3c
kernel_config: x86_64-prod
device: Reloop Jockey 3 Remix
usb_address: 36
has_control_traffic: yes
filter_nak: true
capture_tool: ov-snapshot (triggered), pi4test capture host, pypy3
---

# capture_2026-09-23_linux_issue49_x86_64_44k1.txt

## Objective

First OpenVizsla capture of a live `set_rate_ep` failure on **x86_64-prod**
(github.com/fvdpol/alsa-jockey3/issues/49), on the same physical
EliteDesk/xHC that produced the i386-prod captures, immediately after
rebooting it into its separate 64-bit rootfs. Answers whether the
host-controller-level mechanism already confirmed on i386-prod
(`re/usb/issue49_set_rate_stall_analysis.md`) is word-size/build-specific
or reproduces identically here.

## Conclusion

Identical signature to every i386-prod capture: `GET_RATE` succeeds
(44100 Hz), `SET_RATE(ep=0x86)` SETUP is sent (device address 36) and
accepted, then the device goes silent on that address -- 54.5ms to the
first sign of any other bus activity (a hub-port descriptor read at the
root, implying disconnect), followed by a full re-enumeration to a new
address (37) that takes about 985ms wall-clock end to end (descriptor
reads, `SET_ADDRESS`, firmware handshake, `SET_INTERFACE`,
`CLEAR_FEATURE(ENDPOINT_HALT)` x3), after which the retried
`SET_RATE(ep=0x86)` succeeds cleanly on the first attempt.

Confirms this is not an i386/32-bit-specific interaction: same physical
xHC, same failure shape, same qualitative recovery path, on a completely
different kernel build. Two more samples from the same sweep
(`capture_2026-09-23_linux_issue49_x86_64_44k1_2nd.md`,
`..._3rd.md`) show the same 54.5ms-class initial silence (52.6ms, 197.8ms)
-- consistent with the i386 finding that this gap is bounded below by
roughly the same duration but not fixed.
