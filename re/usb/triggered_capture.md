# Triggered OpenVizsla Capture for Rare Streaming Faults

Status: idea / design sketch. Nothing built yet. This document captures the
motivation and a high-level shape so we have a starting point when we decide
to implement it.

## Why

Some faults only appear deep into a long run. The one that prompted this:
`JT-PCM-008` (8 h PCM soak) on `x86_64-prod`, 2026-08-31, produced a single
playback URB-completion gap of 32 ms at ~4 h 07 m, which cascaded through the
watchdog's URB restart into a full USB reset and killed the capture stream.
`arm64-prod` ran the same 8 h clean.

The open question that only wire data can answer: during that 32 ms gap, was
the **device** still clocking its OUT DMA (so the fault is host-side -- xHCI
event-ring / IRQ servicing starvation, completions pending but unserviced), or
did the **device** actually pause its consumer (firmware / bus fault)? Every
ALSA and driver counter we have is blind to this distinction.

`re/usb/usb_trace_escalation` already names wire tracing as the last resort for
glitches no ALSA counter reports. The problem is purely practical: the fault is
rare and the tooling only knows how to dump a whole capture to a file.
`ovctl.py sniff hs` for 8 h is not viable -- the file would be enormous and
nothing downstream can process it.

## The model: a scope / logic-analyzer trigger

A DJ controller soak is exactly the situation a bench scope is built for: you
do not know *when* the interesting event happens, only what it looks like when
it does. The answer there is a trigger plus a ring buffer with a pre-event and
a post-event window. Same idea here:

- OpenVizsla streams high-speed traffic continuously into a **rolling in-memory
  buffer** holding the last N seconds (pre-event window). Old data is
  discarded, never written to disk.
- On a **trigger**, the capture process freezes: it keeps the buffered
  pre-event window and continues recording for a further M seconds (post-event
  window), then writes just that pre+post slice to disk with a metadata
  sidecar (see `re/usb/*` sidecar convention) and re-arms.
- Only slices around real events ever hit storage. An 8 h run that fires the
  trigger twice yields two short captures, not an 8 h file.

Typical windows: a few seconds pre, a few seconds post -- enough to see the
healthy stream cadence on both sides of the gap and the full
stall -> restart -> reset -> recovery sequence.

## Trigger source

The driver already emits the events we would trigger on, as kernel log lines
("Playback URB stream stalled: no completion for N ms", "queuing full USB
reset", etc.), and the hardware test runner already tails dmesg. So the runner
is the natural trigger author: when it sees the stall line, it fires.

We can also have the driver write explicit `JT-MARK` markers to `/dev/kmsg` at
the moments we care about (it does this already for test phases), giving a
clean, greppable trigger condition.

## Run the capture on a separate machine

The OpenVizsla capture and decode add CPU, memory-bandwidth and interrupt load.
Running them on the device-under-test would perturb the very thing we are
measuring -- and host-side scheduling starvation is a leading hypothesis for
this fault, so added load on the DUT is exactly wrong.

So: OpenVizsla is physically wired to the DUT's USB path, but the capture host
is a **second machine**. The trigger therefore has to cross the network. It
should stay dumb and low-latency -- an HTTP request, an MQTT publish, a UDP
datagram -- carrying little more than "arm", "trigger now", and an event tag
for the sidecar. The DUT-side runner sends it; the capture host acts on it.

Network trigger latency eats into the post-event window budget but not the
pre-event window (that data is already buffered by the time the trigger
arrives), which is the more valuable side for a "what led up to this" question.

## Open questions for implementation time

- Streaming path out of `ovctl.py` (`sniff hs`) into a ring buffer instead of a
  file sink -- does the current tool expose the frames incrementally, or does
  it need patching.
- Sustained USB 2.0 high-speed throughput vs. what the capture host can
  actually ingest and hold in RAM without dropping frames; how big N can be.
- Slice file format -- reuse the existing `parse_openvizsla.py` input format so
  the current pipeline (`parse` -> `reduce` -> `extract_events`) works on
  slices unchanged.
- Whether to also capture a slice on a timed cadence (e.g. one healthy
  reference slice per hour) for comparison against the triggered ones.
