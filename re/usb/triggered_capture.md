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

- Whether to also capture a slice on a timed cadence (e.g. one healthy
  reference slice per hour) for comparison against the triggered ones. Left as
  a question; not in v1.

## 2026-09-01: prototyping notes (measured on `alsa-test`)

`alsa-test` currently carries the whole rig on one box -- OpenVizsla sniffer
(`1d50:607c`), the Jockey 3 Remix (`200c:1037`) and the driver -- so the
questions below could be answered directly. The target topology still splits
capture onto a second host; these numbers were taken single-host and the
contention that caused is itself a data point.

### The `ovctl.py` extension point (closes "does it need patching")

No patch to `ov_ftdi` is needed. `LibOV.py` exposes
`dev.rxcsniff.service.handlers`, a plain Python list of callbacks with the
signature `handler(ts, buf, flags, orig_len)` -- `ts` a cumulative 60 MHz
tick count, `buf` the raw wire packet as `bytes`, `flags` carrying the speed
and FIRST/LAST bits, `orig_len` the pre-truncation length
(`LibOV.py:456-535`). The default entry is `handle_usb_verbose`, which just
feeds a stateful `USBInterpreter` that `print()`s the verbose text lines
(`usb_interp.py:20-160`). The triggered-capture tool imports `LibOV`, opens
the device itself (or drives `ovctl.py` as a library), and appends its own
handler: append `(ts, bytes(buf), flags, orig_len)` tuples to a
time-bounded `collections.deque`, prune by `ts` age on each call. This is
strictly lighter than the verbose interpreter that already keeps up today.

### Throughput and RAM sizing

`sniff hs` against the Jockey 3 in 96 kHz duplex is ~122k wire packets/s
(mostly 1-3 byte token/handshake packets plus ~4000 512-byte `DATA` packets/s;
on-wire payload order 2-4 MB/s). A raw-tuple ring costs roughly 10-20 MB per
second of pre-window once Python object overhead is included -- a 10 s
pre-window is ~150 MB, a 60 s one well under 1 GB. **RAM is not the
constraint.**

### The blocker: capture cannot yet sustain 96 kHz

Sustained lossless `sniff hs` of the Jockey 3 at 96 kHz does not currently
work -- not even bare `ovctl.py`, before `ov-snapshot` is involved. The
bottleneck is `ov_ftdi` / `LibOV`'s Python receive path, and the gateware
SOF/NAK filters that would relieve it are broken on the bundled bitstream.
This is a prerequisite `ov-snapshot` cannot fix from above; the investigation
and fix tracking live in **`ov-snapshot/ov_ftdi_capture_performance.md`**.

### Two design rules that stand regardless

- The tool **must** sample `OVF_INSERT_NUM_OVF` at arm and at slice-flush and
  record the delta in the sidecar. A triggered slice exists to explain a
  *gap*; a silently dropped-packet gap is indistinguishable from a real one.
  Non-zero overflow over the slice window marks it untrustworthy for timing --
  `re/usb/README.md`'s rule ("a capture that dropped packets cannot support
  any timing conclusion") applies with full force.
- The capture host wants nothing else running on it.

### Slice format round-trips through the existing pipeline

Verbose text is the slice output format: the slice writer replays buffered
packets through its own `USBInterpreter` instance and captures the rendered
lines. Verified 2026-09-01: a 120k-line span cut from the *middle* of a live
`sniff hs` capture (first line a bare `PING`, mid-microframe) fed straight
through `parse_openvizsla.py` -> `reduce_transactions.py` ->
`extract_events.py` with no errors -- 9595 transactions extracted, stream
traffic elided, zero control events reported (correct; the span was pure
audio). `parse_openvizsla.py`'s `LOG_PATTERN` tolerates a mid-stream start;
the only loss is the handful of leading lines before the first SOF locks
`USBInterpreter.frameno`, which self-heals within one microframe.

Still to confirm once the ring writer exists: that replaying *raw tuples*
through a *fresh* `USBInterpreter` (rather than slicing already-rendered
text, as tested here) produces good lines from the first packet. The
cross-packet state is only frame/subframe tracking and the `d=` delta, both
of which re-lock on the first SOF, so the risk is bounded to those same
leading lines.

`reduce_transactions.py` takes `-o FILE`, not `-` for stdout (unlike the
other two); minor, noted so the slice-processing wrapper gets it right.

## Design shape (v1)

### Loose coupling -- this is a candidate for its own project

The tool has no reason to be jockey3-specific: it snapshots OpenVizsla
traffic around an external trigger, which is useful for any USB device under
investigation. It should be built so it can be lifted into its own repo
(`git subtree split`) with zero edits:

- Lives in a **self-contained subdirectory**, `re/usb/ov-snapshot/`, for now.
- **No imports** from the driver source tree or from `tests/hw/` -- not
  `lib/machineconf.py`, not the runner, nothing.
- The only jockey3-specific inputs are *data, not code*: the kernel-log regex
  patterns to trigger on and the kernel module name to resolve a build-id
  from. Both live in the tool's config file, not in its source.
- Reuse of `parse_openvizsla.py` / `make_trace_sidecar.py` is treated as
  optional post-processing the tool may shell out to, never a hard import.
  A slice is a valid `ovctl.py sniff hs` verbose file on its own.

### Two programs -- names

Neither is a daemon in the run-at-boot sense; both are applications you start
by hand, typically the capture one on a separate host, and it may grow a UI
(live packet rate, ring fill, list of snapshots taken).

- **`ov-snapshot`** -- the capture application on the capture host. Opens the
  OpenVizsla via `LibOV`, runs the continuous `sniff hs` handler into the
  raw-tuple ring, and listens on a small HTTP endpoint for
  `arm` / `trigger` / `disarm`. `trigger` carries a JSON body: an event tag
  plus the DUT-side facts the sidecar needs (`module_build_id`,
  `kernel_config`, host, rate). On `trigger` it snapshots the ring, keeps
  recording for the post-window, renders the pre+post span to verbose text,
  writes it plus a sidecar (Objective = the event tag, `OVF_INSERT_NUM_OVF`
  delta recorded), then re-arms. Output directory is configurable; on this
  rig it points at the Seafile folder, same as manual captures today.
- **`ov-snapshot-trigger`** -- the standalone watcher on the DUT (decided
  2026-09-01, Frank). Independent of any `tests/hw/runner.py` profile: it
  tails `dmesg --follow` for the stall/reset lines and `JT-MARK` markers and
  POSTs to `ov-snapshot`. Standalone on purpose -- it has to cover ad-hoc and
  overnight bring-up sessions that are not a runner profile.

(`ov-capture` is the alternative name if the broader term is preferred;
`ov-snapshot` is chosen here because the triggered snapshot-around-an-event is
the novel behavior, vs `ovctl.py sniff` which only dumps.)

### Config -- separate from jockey3

The tool's config is **not** under `~/.config/jockey3/`; it gets its own
directory named after itself: **`~/.config/ov-snapshot/`**. This keeps the
decoupling honest and survives the move to a separate repo.

- `~/.config/ov-snapshot/capture.toml` (capture host): OpenVizsla device
  selection, ring pre/post window seconds, output directory, listen
  address/port, overflow-abort policy.
- `~/.config/ov-snapshot/trigger.toml` (DUT): `ov-snapshot` URL, the list of
  kmsg trigger regexes, the kernel module name for build-id lookup, a
  debounce/cooldown so one fault storm yields one snapshot.

TOML, read with stdlib `tomllib` -- no PyYAML dependency, unlike the jockey3
test framework's YAML. A deliberate break, for a tool meant to stand alone.

### Sidecar facts the standalone watcher must gather itself

Not being inside a runner, the watcher has no handed-down run metadata, so it
collects the sidecar fields at trigger time and puts them in the POST body:

| Field | Source on the DUT |
|---|---|
| `host` | `uname -n` |
| `os_version` | `/etc/os-release`, `uname -r` |
| `module_build_id` | `/sys/module/<module>/notes/` -- module name from `trigger.toml` (the GNU build-id is the only reliable identity of a loaded module here) |
| `kernel_config` | debug vs prod from the kernel `LOCALVERSION` (`-alsa-debug+` / `-alsa-prod+`) |
| `driver_version` | left as the build-id above; git-hash resolution against the `write-manifest.sh` manifests is a jockey3-side post-analysis step, not done by the tool |
| `application` | unknown to the watcher -- sidecar TODO, or "concurrent/unknown" |
| Objective | the matched kernel-log line / `JT-MARK` text |

## 2026-09-01: v1 built and smoke-tested

Code: `re/usb/ov-snapshot/` -- `ov_snapshot.py` (capture app),
`ov_snapshot_trigger.py` (DUT watcher), `capture.toml.example`,
`trigger.toml.example`, `README.md`. Stdlib only; the sole external import is
`LibOV` / `usb_interp` from the `ov_ftdi` checkout named in `capture.toml`.
`http.server` for the control endpoint, `urllib` for the client -- no `requests`.

Smoke-tested end to end on `alsa-test` (single-host rig) against the Jockey 3
in 96 kHz duplex: server loads the bitstream, arms, the ring prunes to the
configured pre-window (`ring_seconds` holds at 3.0), `POST /trigger` returns a
result and writes a `.txt` + `.md` pair, and the `.txt` feeds straight into
`parse_openvizsla.py` (`Extracted 121360 successful transactions`) and
`extract_events.py`. `render_verbose` replaying raw tuples through a fresh
`USBInterpreter` had **0 failures** over ~1.5 M packets and the first printed
line already carried a frame number -- the pre-first-SOF loss is a line or two,
as predicted.

Gotcha found and handled: **`OVF_INSERT_NUM_OVF` / `NUM_TOTAL` read back 0
unless `OVF_INSERT_CTL.wr(0)` is strobed first.** `ovctl.py`'s `do_sniff` does
this before every read; `ov_snapshot.py`'s `overflow_counters()` now does the
same. Without it the per-slice overflow figure -- the whole trustworthiness
signal -- was a constant zero. (LibOV also prints `PERR: 0002 (Overflow)` to
stdout per overflow-flagged packet; heavy spam there just means the capture is
losing data -- see the performance doc.)

Slice size: ~285 MB / ~1.5 M lines for a 6 s window at this packet rate
(verbose text with full DATA hex). `parse_openvizsla.py` streams it fine;
a `reduce`-on-write option can come later if it bites.

Not yet done (blocked on capture throughput, see
`ov-snapshot/ov_ftdi_capture_performance.md`): a lossless run against the real
separate capture host; wiring `ov_snapshot_trigger.py` to live dmesg on
`pi4test`; the real `~/.config/ov-snapshot/` deployment.
