---
capture: capture_2026-08-29_linux_test_on-demand.txt
captured: 2026-08-29
platform: Linux
host: alsa-test; Elitedesk G2; i5 6500; 32GB ram
os_version: Linux alsa-test 7.2.0-rc7-alsa-prod+ #6 SMP PREEMPT_DYNAMIC Wed Aug 19 11:58:17 AST 2026 x86_64 GNU/Linux
driver_version: dev/on-demand-streaming test
application: JT-MIDI-008
module_build_id: 272fa23a796b27b83231dc96fbf96a89cf6fee1a
kernel_config: alsa-prod
device: Reloop Jockey 3 Remix
size_raw: 452.2 MB
usb_address: 51, 53
has_control_traffic: yes
---

# capture_2026-08-29_linux_test_on-demand.txt

## Objective

Experiment to see if we can get still receive MIDI data from the device while
the pcm streaming is stopped. This is a prerequisite for being able to 
implement the on-demand streaming feature.


## Conclusion

Full analysis: `re/on-demand_streaming.md`, "2026-08-29: OpenVizsla trace of
the same test". Summary:

Events 1-3 are the case's opening rebind (forces a fresh probe before the
idle wait, per the working document's design); events 4-5 are its closing
rebind. Event 3 ends at t=2.776s; the driver's own dev_dbg reported the
on-demand deactivation as "idle for 5005 ms", predicting the stop at
t=7.781s. The last real PCM packet on the wire is at **t=7.782163s** -- a
1 ms match, computed from this trace's clock alone. Three MIDI IN idle-padding
completions on EP 0x83 follow (7.784-7.793s), then **the bus is silent for
30.747s** until event 4 at t=38.540304 -- matching `watch_seconds: 30` in the
test parameters.

USB autosuspend is ruled out for this capture: a runtime-PM resume is never
silent on the wire (`jockey3_restore_device()` always runs a full cold init),
and no third `init`/`init+rate` event appears inside the 30.747s gap.

**Re-parsed with `--errors` kept (the default-parsed files above have NAK and
STALL dropped and cannot show this): EP 0x83 is polled continuously and
NAK'd for the whole gap.** From t=7.793325 -- within microseconds of the last
real completion -- the host retries the MIDI IN URB in an unbroken chain of
`ongoing` NAK bursts, ~4636-4639 attempts every ~100ms with sub-microsecond
gaps between consecutive burst windows (verified over the first 1.9s of the
gap: 88,103 NAKs, one every 21.6us). The MIDI IN URB stays alive exactly as
the driver design intends; the firmware has nothing to send while its audio
engine is idle. This also puts a number on the working document's "What the
payoff actually is" section, which had only argued from code that the USB
link cannot leave L0 while MIDI IN stays submitted -- ~46,000 NAK'd
attempts/s, sustained, is that argument measured rather than inferred.

## Contents (derived)

- Duration: 40.210 s, 84302 USB transactions
- Device address(es): 51, 53
- Endpoints seen: 0x00 (187), 0x03 (1520), 0x05 (36710), 0x06 (45885)
- Control transfers: 73 in 5 events
- Event kinds: enumeration x1, init x2, init+rate x2
- Sample rates programmed: 44100 Hz

### Events

| # | kind | at (s) | transfers | span (ms) |
|---|---|---|---|---|
| 1 | enumeration | 2.030689 | 19 | 3.557 |
| 2 | init | 2.403042 | 2 | 2.093 |
| 3 | init+rate | 2.693785 | 25 | 82.620 |
| 4 | init | 38.540304 | 2 | 1.048 |
| 5 | init+rate | 38.823622 | 25 | 83.832 |
