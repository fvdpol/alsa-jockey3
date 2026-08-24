# Debug kernels are audibly worse, and ALSA cannot see why

## The measurement, 2026-08-09

The same FLAC plays with a light continuous crackle on `x86_64-debug` and
cleanly on `x86_64-prod`. On **both**, `xrun_counter` stays at zero and
`avail_max` sits around 5500. Feeding `aplay` from a file instead of through
a pipe changes nothing.

So ALSA is equally comfortable on both, and the shortfall is entirely
downstream of it. That points at the free-running URB ring: KASAN inflates
the completion handlers and the codec enough that URBs are resubmitted
late, the device's packet stream gets gaps, and it is audible. Nothing in
`/proc/asound` can observe past the point where the driver hands bytes to
usbcore, which is why the defect is audible and unreported at the same
time.

## What this settles for the test suite

- **Functional verdicts stay valid on a debug kernel** -- does it enumerate,
  play, capture, switch rate, recover from a stall, survive a reload. These
  are what the debug target exists for, and KASAN and lockdep are the
  reason to run them there.
- **Verdicts about how it sounds are void.** `JT-AUDIO-003` and
  `JT-INTEROP-004` are disabled on the debug targets (see
  [test_strategy.md](../docs/test_strategy.md) §4). Running them would
  record a failure against the driver for the kernel's overhead, which is
  worse than not running them at all.
- **`JT-AUDIO-001` stays enabled.** It asks which output a tone came from and
  at what pitch; a crackle does not make either ambiguous.
- A latency or throughput figure from a debug kernel is likewise a fact
  about KASAN, not the driver, which is why those figures are collected on
  `x86_64-prod` instead.

## Why the instrumentation cannot see it

This is the same instrumentation boundary described in
[test_strategy.md](../docs/test_strategy.md) §11a: `/proc/asound` can
observe the driver only up to the point where it hands bytes to usbcore.
URB scheduling, host-controller behavior, and the timing of packets on the
wire are all past it. A late resubmission starves the *device* rather than
underrunning the *ALSA buffer* -- exactly the gap this measurement fell
into. An OpenVizsla trace of the playback endpoint (see
[usb/openvizsla/README.md](usb/openvizsla/README.md)) is the escalation
path if this needs to be characterized further, but on a debug kernel the
explanation is already known and already actionable, so there has been no
need to reach for one yet.
