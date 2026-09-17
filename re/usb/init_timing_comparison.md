# Initialization and Rate-Change Timing: macOS vs Windows vs Linux

Motivation: this driver's initialization is not reliably successful. It
succeeds every time on a debug kernel with dynamic debug printing enabled and
fails intermittently on a production kernel without it. Since the dynamic
debug output changes nothing except how long each step takes, that behavior
points at the timing of the initialization sequence rather than its content.

This document compares what the vendor drivers do on the wire against what
this driver does, using the captures in `usb capture 2026/` processed through
`parse_openvizsla.py` -> `extract_events.py`.

**Working assumption**, stated because it shapes every conclusion below: the
Windows and macOS drivers are correct. Where they agree with each other and
disagree with us, they are taken as the reference.

## Corpus

Only captures containing EP0 traffic can say anything here, and several of the
2026 captures turn out to be stream-only:

| Capture | Control-plane events |
|---|---|
| `capture_macos_44k_poweron` | 1 enumeration, **1 cold init** |
| `capture_macos_rate_change` | **7 warm rate changes** |
| `capture_2026-08-13_macos_poweron` | 6 enumerations, **6 cold inits** at 44.1 kHz |
| `capture_2026-08-13_macos_poweron_96k` | 6 enumerations, **6 cold inits**, 6 rate changes to 96 kHz |
| `capture_2026-08-13_macos_usb_disconnect_96k` | 5 enumerations, **5 cold inits**, 4 rate changes to 96 kHz |
| `capture_2026-08-13_macos_poweron_noapp` | 6 enumerations, **6 cold inits**, 6 rate changes, **no application running** |
| `capture_macos_96k_512` | none -- no EP0 traffic captured |
| `capture_macos_96k_to_44k1` | none -- no EP0 traffic captured |
| `capture_macos_44k1_{128,512,1024}` | none -- no SETUP tokens in the raw trace |
| `capture_win_power-on_samplerate` | **1 cold init**, 6 warm rate changes |
| `capture_win_power_on_off_cycle` | **4 cold inits** |
| `capture_linux` | 1 init (see caveat) |
| `capture_stalled_linux` | 8 init+rate sequences |
| `capture_linux_44k1`, `capture_linux_88k` | none -- no SETUP tokens |

Vendor sample sizes: macOS cold init **n=24**, macOS rate change **n=23**,
Windows cold init **n=5**, Windows warm rate change **n=6**. Fifty-eight
vendor sequences in total.

The four `2026-08-13` captures were taken specifically to close the macOS
cold-init gap, which stood at n=1 when this document was first written. They
also settle three questions the earlier corpus could not: whether the quiet
window is rate-dependent, whether the device retains its rate across a USB
disconnect, and whether the vendor driver streams without an application.

**Caveat on the Linux captures.** The driver revision that produced
`capture_linux` and `capture_stalled_linux` was not recorded, so every Linux
row below is provisional. Future captures must record the module GNU build-id
from `/sys/module/snd_reloop_jockey3/notes/`, which is the only reliable
identity for a loaded module here.

## Finding 1: the rate-write burst ends on the wrong endpoint

> **Acted on; the "this driver" row below is historical.** The trailing
> `0x05` write was dropped and `ploytec_get_rate()` gained an endpoint
> argument, so the driver now ends the burst on `0x86` and verifies from it,
> as the vendors do. The row is kept because the rest of the finding is the
> evidence for that change.

This is the hardest result in the data: an invariant that holds in all 58
vendor sequences, at every rate, on both platforms, and which we violate.

Both vendors end the `SET_RATE` burst on **EP 0x86** and then read the rate
back from **EP 0x86**.

| | Burst | Readback from |
|---|---|---|
| macOS cold init (n=24) | `86 05 86 05 86`, no pause | `0x86` |
| macOS rate change (n=23) | `86 \| 86 05 86 05 86` | `0x86` |
| Windows cold init | `86 05 \| 86 05 86` | `0x86` |
| Windows, samplerate capture | `86 05 \| 86 05 86 05 86` | `0x86` |
| **this driver** | `86 \| 86 05 86 05 86 05` | **`0x05`** |

(`\|` marks the mid-burst pause -- see finding 6.)

The write *count* is plainly not load-bearing: it varies from 5 to 7 within a
single platform, and even within a single capture. The **parity** may well be.
We are the only implementation that leaves `0x05` as the last endpoint
written, and the only one that verifies against `0x05` rather than `0x86`.

Given that the known rate-change failure mode is the *capture* endpoint
(`0x86`) failing to restart -- `implementation_plan.md` milestone 13 -- an
ordering difference that specifically concerns `0x86` deserves attention
beyond its size.

To be clear about what is *not* broken: in `capture_stalled_linux` the
`GET_RATE` from `0x05` does return the rate just programmed, so the
verification in `ploytec_set_rate()` is reading real data and its "Rate
mismatch!" warning is not silently defective. The divergence is about which
endpoint is written last, not about whether the readback works.

The change is small: drop the trailing `0x05` write from the loop in
`ploytec_set_rate()`, and give `ploytec_get_rate()` an endpoint argument so
the readback can target `0x86`.

Note that at probe this burst does not run at all -- see finding 7, which is
the more serious problem.

## Finding 2: the vendors leave a ~50 ms quiet window; we never do

Every vendor sequence contains at least one window of very close to 50 ms in
which the host sends nothing at all. Across 58 sequences the value lands
between 50.2 ms and 52.0 ms -- a spread under 4%.

**It is not rate-dependent.** That was the open question the 96 kHz captures
were taken to settle, and the answer is clean: 50.227 ms in a 44.1 kHz cold
init against 51.076 ms in a 96 kHz one, and 51.177 / 50.211 ms in a 96 kHz
rate change. A fixed 50 ms in the driver is correct at every supported rate;
there is nothing to scale.

| Sequence | n | Quiet window | Position |
|---|---|---|---|
| Windows cold init (power cycle) | 4 | 50.863 - 51.372 ms | after `SET_INTERFACE(1, alt 1)` |
| Windows, samplerate capture | 7 | 50.316 - 51.956 ms | after `SET_INTERFACE(1, alt 1)` |
| macOS rate change | 7 | 50.225 - 51.149 ms | before the first `SET_RATE` |
| macOS rate change | 7 | 50.210 - 50.893 ms | after the `GET_RATE` readback |
| macOS cold init, 44.1 kHz | 6 | ~50.2 ms | after the `GET_RATE` readback |
| macOS cold init, 96 kHz | 6 | ~51.1 ms | after the `GET_RATE` readback |
| macOS rate change to 96 kHz | 16 | 50.2 - 51.2 ms | both positions |

This driver has no such window anywhere. Its largest inter-transfer gap is the
deliberate ~11 ms inside the `SET_RATE` burst; every other step follows the
previous one within about 5 ms:

```
   114.988 |     1.505 | SET_INTERFACE intf=1 alt=1
   115.627 |     0.615 | CLEAR_FEATURE(ENDPOINT_HALT) ep=0x86
   116.137 |     0.486 | CLEAR_FEATURE(ENDPOINT_HALT) ep=0x05
   116.567 |     0.406 | CLEAR_FEATURE(ENDPOINT_HALT) ep=0x83
   117.029 |     0.433 | GET_STATUS len=1 -> 32
```

Both vendors wait ~50 ms somewhere in that span. We wait 2 ms.

### The window is genuine silence, not retries

`parse_openvizsla.py` discards NAK and STALL transactions, so a window full of
rejected transfers and a window of true silence look identical in the parsed
output. That distinction decides whether the vendor *waits* or *retries until
accepted*, which are different fixes, so it was checked against the raw trace.

Between the completion of `GET_RATE` at 10.832055 and the `SETUP` of the first
`SET_RATE` at 10.883037 in `capture_macos_rate_change.txt`, the wire carries
**nothing** -- no tokens, no NAKs, no PINGs, no bulk traffic. The same holds
for the Windows cold init between 9.409201 and 9.460573 in
`capture_win_power_on_off_cycle.txt`. The host is not polling. It is idle.

### It is a fixed sleep, not a periodic worker tick

The window's position varies -- Windows anchors it after the interface reaches
alt 1, macOS anchors it around the rate write, and macOS warm rate changes
have **two**. A varying position is also what a ~20 Hz periodic worker would
produce, with whichever step misses the tick showing up as ~50 ms, so the two
readings were separated by checking the phase of the window start against a
capture-wide clock.

Across the 7 macOS warm rate changes the window starts at phases of 0.0, 37.7,
0.7, 7.5, 43.5, 40.6 and 5.6 ms modulo 50 ms -- scattered across the whole
interval with no common phase. A periodic tick would cluster. These are fixed
sleeps that begin whenever the previous step finished.

## Finding 3: cold init and rate change are two different sequences

The 2026-08-13 captures make this unmistakable, and it is the finding with the
most direct consequence for our code. macOS uses two structurally distinct
sequences, and we use one.

| | macOS cold init | macOS rate change |
|---|---|---|
| Transfers | 16 | 18 |
| Interface handling | `SET_INTERFACE` to alt 1 only | bounce: alt 0 on both, then alt 1 on both |
| Preceded by | `SET_CONFIGURATION` | nothing |
| Lead-in before burst | ~14 ms | **~51 ms** |
| Burst | 5 writes, contiguous | 1 write, **~11 ms**, then 5 |
| Quiet windows | one, before the final `GET_STATUS` | two |

The cold init always programs **44100 Hz**, whatever the application is
configured for. When Traktor was set to 96 kHz, every power-on produced a
44.1 kHz cold init followed about half a second later by a *separate* rate
change to 96 kHz. The initialization is not parameterized by the desired rate
at all.

We have only one path: `ploytec_initialize_device()` followed by
`ploytec_set_rate()`, so our initialization uses the *rate-change* burst shape
-- one write, a 10 ms sleep, then a loop -- where macOS uses five contiguous
writes with a 14 ms lead-in and no mid-burst pause. We also perform the alt 0
bounce during initialization, which macOS does only on rate changes.

## Finding 4: no application is required, which validates the free-running URBs

`capture_2026-08-13_macos_poweron_noapp` was taken with nothing open on the
Mac. macOS still enumerated the device, ran the full cold init, programmed the
rate -- including the change to 96 kHz -- and then streamed continuously:
141524 packets on EP 0x05 over 38.4 s, about 3700 per second, with gaps only
while the device was physically powered off.

This driver keeps its URBs submitted for the device's lifetime rather than
starting and stopping them around PCM open and close. That was reasoned from
the MIDI multiplexing -- MIDI OUT rides in byte 480 of every playback packet,
so the stream cannot stop while MIDI is expected to work. It now has direct
vendor evidence behind it: the reference implementation streams with no
application in the picture at all.

## Finding 5: the device resets its rate on USB re-enumeration

`capture_2026-08-13_macos_usb_disconnect_96k` was taken to test whether the
device retains its configured rate when the USB cable is unplugged and
replugged, since that does not power-cycle it -- the Jockey 3 is self-powered
(`bmAttributes 0xc0`).

**It does not.** Every `GET_RATE wIndex=0` after a replug reads 44100 Hz, in
cycles where the device had been running at 96 kHz moments earlier.

After a power cycle that reading is unremarkable -- 44100 Hz is evidently the
power-on default. The unplug case is the informative one: the device kept its
power the whole time and still came back at 44100 Hz, so the reset is
triggered by USB re-enumeration rather than by loss of power.

That reading is genuinely live rather than a constant, which the older
`capture_macos_rate_change` establishes: there the same request tracks the
programmed rate through 44100, 48000, 44100, 48000, 88200, 96000 and 44100.

The status byte tells the same story. The first `GET_STATUS` of a cold init
reads **0x12** after a replug, exactly as it does after a power cycle, with
the streaming bit clear. On both paths the device presents itself to the host
as freshly initialized.

The likely explanation is that the firmware runs its own initialization when
USB is disconnected. It is not a full reset: the control surface LED pattern
is unchanged across an unplug, which it would not be after a genuine reset.

What the trace cannot distinguish is *when* the rate goes back to 44100 --
at the disconnect, or when the host sends `SET_CONFIGURATION` on the way back
up. Nothing is on the wire while the cable is out, and by the time any host
reads the rate it has already reconfigured the device. The LED observation is
what separates "partial firmware re-init" from "full reset", and that is an
out-of-band observation, not something in the capture.

Two consequences. A driver may not assume a rate survives re-enumeration, so
the rate must be reprogrammed on every probe -- which we do. And the vendors
issue this initial read with `wIndex = 0x0000`, a form our
`ploytec_get_rate()` cannot express; see finding 7.

## Finding 6: the mid-burst pause is real and ours is about right

Every implementation splits the rate writes with a pause:

| | Mid-burst pause |
|---|---|
| macOS warm rate change (n=7) | 10.468 - 11.175 ms |
| macOS cold init (n=1) | 13.444 ms |
| Windows (n=11) | 6.252 - 22.861 ms |
| this driver | 11.075 ms (`usleep_range(10000, 11000)`) |

Ours sits inside the observed vendor range. This one is fine, and the comment
in `ploytec_set_rate()` attributing it to macOS behavior is accurate.

## Finding 7: at probe we skip the rate programming altogether

This one is not visible on our wire as a *difference in* a sequence -- it is a
sequence that never happens. It lives in our control flow, and the vendor
corpus is what made it visible.

`jockey3_set_rate()` reads the hardware rate and only calls
`ploytec_set_rate()` if it differs from the requested one:

```c
ret = ploytec_get_rate(chip->intf0, chip->xfer_buf, &current_hw_rate);
...
if (current_hw_rate != rate) {
        ret = ploytec_set_rate(chip->intf0, chip->xfer_buf, rate);
```

`jockey3_initialize()` requests 44100 Hz at probe, and 44100 Hz is the
device's power-on default -- established by every macOS cold init in the
corpus, all of which read exactly that before programming anything. So on a
cold power-on the condition is false and **the entire rate burst is skipped**.
Our initialization is `initialize_device` -> `get_rate` -> `start_streaming`,
with no `SET_RATE` in it at all.

All 24 macOS cold inits program the rate unconditionally, writing 44100 Hz to
a device already reporting 44100 Hz, and only then read it back and set the
status. Windows does the same. No vendor sequence in the corpus, on either
platform, skips the burst.

If those writes are what arms the endpoints rather than merely setting a
frequency, this is not a timing bug at all but a missing step, and the
intermittency is in whatever sometimes makes the device work without it.

Two things make this the strongest candidate for the initialization failure:

- It is the only divergence that removes work entirely rather than
  reordering or re-timing it.
- `jockey3_initialize()` already retries the whole sequence ten times with
  50-100 ms between attempts. A device that merely needed a settling period
  would be very likely to succeed on the second attempt; ten consecutive
  failures over about a second fit an absent step far better than a missed
  window.

The fix is to program the rate unconditionally at probe, as both vendors do.
The `current_hw_rate != rate` guard is reasonable for a later rate *change*,
but initialization is not a rate change -- finding 3.

## Finding 8: smaller divergences

- **`GET_RATE` wIndex.** Both vendors issue the *initial* rate read with
  `wIndex = 0x0000` and the *readback* with `wIndex = 0x0086`. We use
  `wIndex = 0x0005` for both, because `ploytec_get_rate()` hardcodes
  `PLOYTEC_EP_NUM_PCM_OUT` and cannot express either vendor form.

- **The alt 0 bounce.** We always drive both interfaces to alt 0 and then back
  to alt 1. Windows never does this -- it goes straight to alt 1 on both cold
  init and rate change. macOS does it only on rate changes, never on cold
  init, and in the opposite order to ours: macOS deactivates interface 1 first
  (`intf=1 alt=0, intf=0 alt=0, intf=0 alt=1, intf=1 alt=1`), we deactivate
  interface 0 first.

- **The 3-5 ms sleep between alt 0 and alt 1** in `ploytec_initialize_device()`
  has no counterpart in either vendor: macOS moves from alt 0 to alt 1 in
  about 0.5 ms. Whatever it was added for, it does not come from these traces.

- **`CLEAR_FEATURE(ENDPOINT_HALT)` placement.** macOS clears all three
  endpoints *before* reading status, and we match that. Windows clears only
  `0x86` and `0x05`, and does it several hundred milliseconds *after*
  `SET_STATUS`.

## Where this stands, 2026-08-13

The alignment changes are in (findings 1, 2, 3 and 7), and the driver now puts
a vendor-shaped sequence on the wire -- confirmed in
`capture_2026-08-13_linux_power-on_events.txt`. **They did not fix the
problem.** They were necessary; the sequence is right now. What is wrong is
when it starts.

### The failure has an objective signature

The capture endpoint returns bit-exact zero. A running converter is never
silent -- it carries a noise floor whether or not anything is patched in, and
healthy cycles measure 95-99% of frames non-zero with open inputs. An
entirely-zero capture is a converter that never started, not a quiet input.
Playback is unaffected on the wire: 100% of packets carry real audio at the
correct cadence with the MIDI idle and sync bytes in place, every cycle, and
the device simply does not sound it.

That signature needs neither OpenVizsla nor a listener, which is what makes
the fault cheap to chase. It is what `JT-AUDIO-005` keys on.

### It takes a cold boot, and then it is deterministic

| what was done | cycles | silent |
|---|---|---|
| re-enumeration via the ppps hub switch | 10 | **0** |
| mains power cycle, by hand | 4 | 3 |
| mains power cycle, via the network switch | 9 measured | **9** |

The device is self-powered, so cutting VBUS at the hub is a cable unplug and
not a power-off; the two are not equivalent to it. A re-enumeration returns
the rate to 44100 Hz and the status byte to 0x12 while leaving the control
surface LEDs lit -- a partial firmware re-init. Only a cold boot provokes
this.

**The 100% rate is itself the strongest evidence for the mechanism.** Hand
switching failed three times in four; the network switch, holding power off a
measured five seconds with a clean break, fails every time. The more
completely the device is cold-booted, the more reliably it happens. That is
what "the driver starts talking before the device has finished booting"
predicts. A marginal supply or a flaky cable would do the opposite.

It also finally explains the observation this whole document started from: a
debug kernel is slower to reach probe, so by the time it speaks the device is
ready.

### The control plane is not where the difference is

Working and failing initializations are byte-identical, transfer by transfer:
same requests in the same order, same status values, same rate programmed and
read back, same inter-transfer gaps including the cold-init lead-in and the
~51 ms window. Whatever separates them is not in what the driver sends.

### The next experiment

Add a settling delay before the driver's first control transfer, high enough
to confirm the mechanism rather than to be shippable, and run:

```sh
cd tests/hw && ./runner.py --case JT-AUDIO-005 --unattended
```

Ten cold boots, about four minutes, no operator. With a deterministic
reproducer and an automated detector this is one run rather than a statistical
argument.

- `silent_cycles` drops to 0 -- bisect the delay down to the real threshold,
  which is the number that has to be justified for submission. The vendor
  traces cannot supply it: they show what the vendor chose, never what the
  device requires.
- `silent_cycles` stays at 9 -- the boot-race model is wrong. The next
  instrument is an OpenVizsla trace of a failing cycle, parsed with
  `parse_openvizsla.py --errors=0`, which will show whether the device NAKs or
  stalls any control transfer during that window. Nothing in the corpus so far
  covers a *failing* Linux initialization.

### Where the delay goes, and how big

Not in `ploytec_initialize_device()`, despite that being the obvious reading
of "before the first control transfer". That function runs **twice** on a
successful probe -- `jockey3_initialize_ploytec()` calls it, and then
`jockey3_set_rate()` calls it again -- up to eleven times on a failing one,
since the retry loop wraps the first call, and once more on every rate change
thereafter. A delay there is applied a variable number of times, half of it
landing *after* the first sequence has already gone out, which blurs precisely
the distinction being measured.

It goes in `jockey3_initialize()`, immediately before the retry loop. That is
the last point before the driver's first EP0 transfer of the device's life --
everything above it in `probe()` is ALSA and USB-core bookkeeping, with no
control request among it -- and it runs exactly once, touching neither the
retry path nor rate changes.

**2 s for the confirmation run, not 500 ms.** The run costs the same at any
delay, so a small first value buys nothing but a weaker result: 9/9 silent at
500 ms cannot distinguish "the boot-race model is wrong" from "the device
needs longer than 500 ms", which is the entire question. Overshoot to
establish the mechanism, then bisect 2000 -> 1000 -> 500 -> 250 for the
threshold.

One thing to watch in the result: `enumerate_ms_max` was 9432 ms at baseline
against a `settle_seconds` of 8, so the added delay eats into a budget that
was already not generous. If it overruns, `JT-AUDIO-005` fails with "no
playback, capture substream after re-enumeration" -- a timeout, loudly
distinct from a silent capture, and not the fault being chased.

**The sleep itself cannot ship**, whatever the threshold turns out to be: a
multi-second `msleep()` in USB probe runs on the hub thread and delays
enumeration of every device behind it. The deliverable here is the number, not
the code.

If the run comes back all-silent at 2 s, the cheaper next instrument than
OpenVizsla is this document's own discriminator -- an internal race is fixed
only by a delay in one *specific* place. Moving the same delay to just before
`ploytec_start_streaming()`, the transfer that arms the engine, and then to
just before the rate burst, is two more four-minute runs and localizes the
window. A wire capture is far more useful once it is known which transfer to
look at.

## The answer, 2026-08-14: it is a boot race

A 2 s settling delay before the driver's first control transfer **eliminates
the fault**.

| | cycles | silent | non-zero fraction |
|---|---|---|---|
| baseline, no delay (`f6a71204`) | 9 | **9** | 0.0000 every cycle |
| 2 s delay (`25355bfe`, commit `4be1ce5`) | 10 | **0** | **1.0000 every cycle** |

Ten for ten, every cycle fully live, against nine for nine bit-exact silent on
the same rig, same kernel, same production configuration, minutes apart. The
fault was deterministic in both directions, which is what makes a single run
of ten decisive rather than suggestive.

The hypothesis this document has carried since it was written is therefore
confirmed: **the driver was starting to talk before the device had finished
booting.** The device's USB core answers early enough to enumerate and be
programmed while whatever brings up its audio engine is still coming up;
program it inside that window and the engine never starts. Everything the
earlier evidence predicted holds — it explains why the fault needs a real cold
boot, why it scales with how *completely* the device is power-cycled, and why
a debug kernel never showed it.

The competing hypothesis, an internal driver race papered over by the latency
of dynamic debug printing, is ruled out for practical purposes. The delay was
placed before the driver's first EP0 transfer, changing nothing about the
driver's internal ordering; if the race were the driver's own, moving the start
of an unchanged sequence later would not fix it.

### The value that ships: 250 ms

The bisect below puts the device's requirement between 144 and 156 ms after
enumeration. The shipped delay is **250 ms**, and it is deliberately not the
smallest value that passed.

The reason is that the ~124 ms we already spend between enumeration and the
first transfer is host-dependent, not a property of the device: it is the USB
core finishing enumeration plus this driver's own card, PCM and MIDI
registration before `jockey3_initialize()` runs. A faster machine, or a build
with less to do in probe, reaches the first transfer sooner and quietly eats
the margin. Adding 30 ms to a quantity that varies by host is not a fix; a
delay that on its own exceeds the requirement is. At 250 ms the constraint
holds even if probe arrived instantly, with about 1.7x margin over the measured
150 ms.

It also lands where the vendors already are. Windows begins initialization
~300 ms after `SET_ADDRESS` and macOS over a second; 250 ms puts this driver's
first transfer at ~300 ms, which is Windows' behavior almost exactly. Two
independent lines of evidence -- a hardware bisect on our unit, and the vendor
traces -- agree on the same order of magnitude.

Confirmed at **30 consecutive cold boots, 0 silent**, via the `soak` profile.
Ten cycles cannot distinguish a true 0% failure rate from about 25%, which is
adequate for bracketing an interval and not for choosing a shipped value.

### The bisect, 2026-08-14

Seven builds, 100 cold boots, each step a `--uncommitted` build so the branch
kept no history of the search. The controlled variable is the `msleep()`; the
observable is enumeration to first EP0 transfer, taken from the kernel log.

| delay | enum -> first EP0 (min-max) | cycles | silent |
|---|---|---|---|
| none | 123-124 ms | 10 | **10** |
| 15 ms | 140-144 ms | 10 | **10** |
| 30 ms | 156-164 ms | 10 | 0 |
| 60 ms | 184-188 ms | 10 | 0 |
| 125 ms | 252-260 ms | 10 | 0 |
| 250 ms | 380-408 ms | 10 | 0 |
| 250 ms | (soak profile) | **30** | 0 |
| 500 ms | 624-652 ms | 10 | 0 |
| 1000 ms | 1128-1152 ms | 10 | 0 |
| 2000 ms | 2128-2148 ms | 10 | 0 |

**The device needs between 144 and 156 ms after enumeration.** Below that the
audio engine never starts, every time; above it, never. Both sides are 10/10,
so the fall-over is a step and not a probabilistic race -- the device's own
readiness time barely varies, which is consistent with a fixed firmware boot
rather than contention.

Two measurement notes. Enumeration-to-first-EP0 is the quantity that
discriminates: within a run its spread is under 10 ms, and the failing and
passing sets do not overlap. Power-on-to-first-EP0 does *not* discriminate --
a cycle at 1739 ms failed while cycles at 1690 ms passed -- because that
interval also contains the relay's own switching latency and the device's
enumeration time, which vary by hundreds of milliseconds. Timing from
enumeration removes both.

That enumeration is the right zero point is itself informative: enumeration is
device-driven, so it tracks the device's boot rather than floating free of it.
One cycle took 6.2 s from mains-on to enumerating and still passed with the
same enum-to-EP0 figure as every other cycle. A delay measured from probe is
therefore the correct shape, since probe begins after enumeration.

Raw per-cycle data is checked in as `settling_delay_cycles.csv`, regenerated by
`settling_delay_dataset.py`. The build-id to delay mapping lives in that script
and is the only record of it -- the builds were deliberately uncommitted, so
git cannot supply it.

### What the vendors do, and it corroborates the number

The traces answer the same question directly, via `init_start_timing.py`. The
anchor is `SET_ADDRESS`, the closest wire equivalent of the "new high-speed USB
device number N" line the Linux hub thread logs.

| | n | `SET_ADDRESS` -> `SET_CONFIGURATION` | -> `SET_STATUS` |
|---|---|---|---|
| this driver, before the fix | 4 | **46-47 ms** | 49-94 ms |
| Windows | 5 | **297-308 ms** | 372-411 ms |
| macOS | 24 | **1042-2056 ms** | 1148-2171 ms |
| macOS, after a USB replug | 5 | 2044-3568 ms | 2149-3675 ms |

All three platforms begin the vendor protocol within about 2 ms of
`SET_CONFIGURATION` -- macOS 1.2 ms after it, Windows 1.7 ms before, this
driver 0.25 ms after. **What differs is not the sequence but when it starts.**
We started it at 47 ms; Windows waits ~300 ms and macOS over a second.

Both vendors therefore leave far more than the ~150 ms the device turns out to
need, and neither is anywhere near the margin we were running on. Windows is
the tighter of the two at ~300 ms, roughly twice the measured requirement.

This is independent of the hardware bisect -- different method, different
instrument, different platforms -- and it lands in the same place. It is also
the first vendor evidence in this document that bears on *when* initialization
starts rather than what it contains, which is the question every other finding
here turned out not to answer.

### Does the rate-change fault share this mechanism? Probably not -- and the data points elsewhere

Milestone 13's capture-restart problem looks like a natural sibling of the
cold-boot fault: if the device needs time after power-on, perhaps it needs time
after being reconfigured. The traces say no, and in doing so suggest a
different and more mundane cause.

`rate_change_stream_timing.py` measures, per rate-change sequence, when bulk
traffic resumes relative to `SET_STATUS`:

| | n | sequence span | EP 0x05 resumes | EP 0x86 resumes |
|---|---|---|---|---|
| macOS | 47 | 103-142 ms | **0-1 ms** | **1-21 ms** |
| this driver | 4 | 166-211 ms | 70-76 ms | **70-85 ms** |

**macOS leaves no settling window at all.** It resumes playback within a
millisecond of `SET_STATUS` and the device produces capture data again within
21 ms, in every one of 47 sequences across four captures. A device that needed
time to reconfigure would not behave like that, and the vendor -- whose
behavior this document takes as correct -- would be waiting for it. So the
cold-boot mechanism does not transfer: a power cycle restarts the firmware, a
rate change does not.

What the same table shows instead is a plain arithmetic problem. After a rate
change `jockey3_pcm_hw_params()` waits **50 ms** for each direction to come
back alive, via `jockey3_wait_urb_stream_started(..., 50)`. Our own captures
show capture returning at **70-85 ms** -- later than that wait, in all four
sequences measured. The driver therefore declares "Capture URB has stalled" and
escalates into the milestone 13 mitigations for a stream that was about to
return on its own.

That fits the historical `JT-RATE-001` numbers, which are otherwise hard to
explain: `stalls_capture` of 77 to 99 per 100 rate changes. A device failing
four times in five is a broken device; a threshold sitting just under the
normal restart latency produces exactly this, and produces it *reliably*, which
is what the runs show.

Two caveats, both material:

- **n=4 for this driver.** The vendor side is well sampled; ours is not. Four
  sequences from two captures is enough to notice that 70-85 ms exceeds 50 ms,
  not enough to characterize the distribution.
- **All of it predates the current build.** The `JT-RATE-001` runs above are
  from 2026-08-09 and 2026-08-11, before the four vendor-alignment changes of
  2026-08-13 and before the cold-boot settling delay. The current rate-change
  reliability is simply unknown.

So the next step is measurement, not a fix: re-run `JT-RATE-001` on the current
build for a present-day `stalls_capture`. If it is still high, raising the
50 ms wait to something above the observed restart latency -- 250 ms, matching
the settling delay -- is a one-line experiment with the same shape as the one
that resolved milestone 15. It should be tried *before* any further mitigation
is added, because if this reading is right the existing watchdog, liveness
polling and queued-reset machinery are compensating for a timeout that is
simply too short.

### Two harness defects this run exposed

Neither affected the verdict, but both had been latent and would have
corrupted later measurements:

- **Card readiness was tested by name, not by liveness.** `/proc/asound/card<idx>`
  and its `id` file are created by `snd_devm_card_new()`/`snd_card_set_id()`,
  early in probe -- so the case could lock onto a card that
  `snd_card_register()` had not yet published, and `os.path.exists()` on
  `/dev/snd/*` was then satisfied by the *previous* incarnation's nodes, which
  udev had not yet reaped. arecord opened a dead control node and failed with
  "Cannot get card index". The case now opens the control node instead of
  stat-ing it. The window was sub-millisecond before; the settling delay
  widened it to seconds and made it certain.
- **Two `expect_dmesg` patterns could never match.** YAML single-quoting has no
  escape sequences, so `'\\d+'` reached `re` as a literal backslash. This is
  why `jockey3_urb_error_give_up: N callbacks suppressed` sat in the
  unclassified bucket of every run of this case, baseline included.

## What this evidence cannot settle

Two hypotheses were live when this document was written: that the device needs
a settling period the driver does not give it, or that the driver has an
internal race which the extra latency of dynamic debug printing papers over.
The JT-AUDIO-005 results above weigh heavily toward the first -- the failure
scales with how *complete* the cold boot is, which is a property of the device
coming up, not of anything the driver races against. They do not close it.
An internal race whose window happens to be widest right after a cold boot
would look the same from here.

What separates them is where the delay is placed. A device that needs time
after power-on will be fixed by waiting before the first control transfer,
whichever transfer that is. An internal driver race will not be, or will be
fixed only by a delay in one specific place. The bisect described above
therefore answers both questions at once, and its result should be recorded
here.

Note also that adding the ~50 ms quiet windows of finding 2 did **not** fix
this, so that divergence, real as it is, was not the cause. `capture_linux`
already hinted as much: it omits the window entirely -- 2 ms where the vendors
leave 50 -- and still came up streaming.

The vendor traces cannot give the *tolerance* either. They show what the
vendor chose, never what the device requires; a 50 ms sleep is consistent with
a device needing 45 ms and with one needing 2 ms. The usable minimum can only
come from bisecting in our own driver against repeated cold boots, which is
now a four-minute run rather than an afternoon.

## Do we need more vendor traces?

**No.** This was an open question when the corpus held 18 sequences and a
single macOS cold init. The 2026-08-13 captures took it to 58 sequences and 24
macOS cold inits, and every number they added fell inside the range already
established. The delays are settled, and the two questions the numbers alone
could not answer -- rate dependence and the streaming-without-an-application
assumption -- are settled too.

Two scenarios were considered and ruled out rather than left open.

**Stream stop / application close.** Because the vendor driver brings the
device up and keeps the stream flowing as soon as the OS loads it, with no
application involved, closing an application leaves the device in the same
state as never having opened one -- and that state is already captured, in
`capture_2026-08-13_macos_poweron_noapp`. The transition is not expected to
hold surprises, and the question we actually care about at close is whether
*our* driver tears down cleanly, which is a hardware-in-the-loop test rather
than a capture.

**Suspend/resume.** What matters here is that this driver's URBs are running
correctly again after a resume. How macOS or Windows handle suspend does not
bear on that, so a vendor trace would not inform the fix. This belongs in the
test suite too.

With those set aside, the vendor-side capture work is essentially complete.

**The blocking artifact is on the Linux side**: a failing init and a passing
init captured back to back on a production kernel, same driver build, with the
module build-id recorded. Nothing in the vendor corpus substitutes for it.

## Method note

`extract_events.py --event-gap` defaults to 250 ms. Windows leaves its
trailing `CLEAR_FEATURE` pair between about 250 ms and 900 ms after
`SET_STATUS`, which straddles that threshold: in
`capture_win_power-on_samplerate` one event absorbs the pair (an internal gap
of 249.781 ms), while in `capture_win_power_on_off_cycle` they consistently
split off as separate `control` events. Raising the threshold to 500 ms does
not fix this -- it just moves which captures land on which side.

This affects the reported *span* of Windows events and nothing else. Every gap
the findings above rest on is measured within the rate sequence, well before
the trailing `CLEAR_FEATURE`s.

## Reproducing

```sh
cd re/usb
python3 extract_events.py <capture>_parsed.txt --format summary
python3 extract_events.py <capture>_parsed.txt -o <capture>_events.txt
```

Or straight from a raw trace, without writing the intermediate:

```sh
python3 parse_openvizsla.py <capture>.txt - | python3 extract_events.py - --format summary
```

The extracted event files for every capture in the corpus are checked in
alongside the traces in `openvizsla/`.
