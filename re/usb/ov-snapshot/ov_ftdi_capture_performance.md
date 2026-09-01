# OpenVizsla capture throughput on a busy High-Speed bus

Status: **open investigation.** This is a separate document on purpose --
`../triggered_capture.md` is about the triggered-capture tool; this is about
whether `ov_ftdi` / `LibOV` can keep up with the data rate at all, which is a
prerequisite the tool cannot fix from above. Expect this to be a deep dive.

## The problem

`ov-snapshot` needs a lossless continuous `sniff hs` of the Jockey 3 while it
streams. The receive path is single-threaded Python -- `LibOV.OVDevice.__comms`
runs one daemon thread that reads the FTDI stream, frames it
(`presentBytes` / `consume`), and calls the registered handlers, all holding
the GIL. At the Jockey 3's packet rates that thread saturates one core and the
OpenVizsla's 16 MB SDRAM ring overflows.

Measured on `alsa-test` (i5-6500), single-host (Jockey 3 + driver + capture on
one box that day), with `py-spy` on the live `__comms` thread:

| Rate | ~wire packets/s | `__comms` thread CPU | Overflow |
|---|---|---|---|
| 44.1 kHz duplex | ~250k | ~88 % of one core | **zero** (`ov_snapshot.py`) |
| 96 kHz duplex | ~2x that | ~91-95 %, pegged | heavy, continuous |

So **44.1 kHz just fits** -- `ov_snapshot.py` captured it with zero overflow
and zero dropped packets, but only ~12 % headroom on the framing thread. 96 kHz
is over the cliff. The cliff sits between the two; every intermediate rate
(48 / 64 / 88.2) is untested.

### It is not bus contention, and the rate is inherent

`pi4test` USB tree when the Jockey 3 was there: root hub -> hub -> hub ->
Jockey 3, nothing else. The packet volume is 96 kHz duplex plus High-Speed
SOF/handshake/NAK overhead -- ~250k packets/s at 44.1 kHz, roughly double at
96 kHz -- not other devices.

### The designed relief valve is broken

`--filter-sof` / `--filter-nak` drop those packets in gateware before they
reach the host. On the bundled bitstream (`ov3.fwpkg`, 2024/02/11) they
instead corrupt the byte stream: the output degenerates into
`Unmatched byte NN - discarding` from the `LibOV` framing layer (~777 usable
lines in 30 s vs ~3.6 M unfiltered). So today every wire packet reaches
Python.

## Profiling results (2026-09-01, `py-spy` on `alsa-test`, single-host)

Counter caveat: `OVF_INSERT_NUM_TOTAL` / `sdram_packets_total` sometimes read
`0xFFFFFFFF`; `OVF_INSERT_NUM_OVF` can be non-monotonic; `ovctl.py`'s 1 Hz
status loop stalls under load because its register reads are starved by the
`__comms` thread. Also: **overflow-marker packets are delivered to handlers**,
so `packets_seen` in `ov_snapshot.py` spikes (1-3 M/s bursts) once behind --
the steady ~250k/s figure at 44.1 kHz, taken when `overflow_total` stayed 0,
is the trustworthy one. `py-spy` per-thread CPU and stack samples are solid.

### Bare `ovctl.py sniff hs` -- the verbose renderer dominates

`__comms` thread ~92 % of a core. Of that, **~85 % is
`USBInterpreter.handlePacket`**:

- `usb_interp.py:77` -> `hd()` (`usb_interp.py:3`): `" ".join("%02x" % i for i
  in buf[1:])` -- per-byte hex-formatting of *every* DATA packet payload
  (~512 B each, ~8000/s). ~1370 of ~2350 interpreter samples.
- `usb_interp.py:153`: the per-packet `print(...)` with a wide format string.
  ~980 samples.

The framing layer (`presentBytes`, `consume`, `getPacketSize`, the `__buf`
slicing) shows up but *well* below `handlePacket`. **This corrects the earlier
note in this doc** that `--format pcap` / `iti1480a` "failed identically, so
it's not the interpreter" -- those runs were contaminated by a framing desync
(`Unmatched byte...`), so the comparison never held. For `sniff hs`, the
interpreter is the cost.

### `ov_snapshot.py` -- interpreter bypassed, cost redistributes

`ov_snapshot.py` replaces the handler list with `Ring.on_packet` (raw
`bytes()` + `deque` append + age-prune) and defers all rendering to
slice-flush. No `handlePacket`, no `hd`, no `print` on the hot path. Result at
44.1 kHz: **zero overflow, zero PERR, a clean triggered slice** -- but the
`__comms` thread is still ~88 % of a core, split roughly:

| ~share of thread | where | detail |
|---|---|---|
| ~40 % | `Ring.on_packet` (our code) | `with self.lock:` per packet (`ov_snapshot.py:84`); `(ts, bytes(buf), flags, orig_len)` tuple + copy (`:83`); the age-prune `while` loop run every packet (`:91`) |
| ~45 % | `LibOV` framing | `presentBytes` per-byte scan (`LibOV.py:299/302/305/310`), `getPacketSize` (`:492`), `consume`, `__buf += b` / re-slice |
| ~15 % | `read_async` / `callback` | FTDI read plumbing, `string_at` copy out of the ctypes buffer |

At 96 kHz the same split, thread pegged, heavy overflow, `PERR: 0002` printed
~450x/s (LibOV prints one per overflow-flagged packet -- once behind, that is
every packet).

### `ovctl.py sniff hs --timeout N` does not self-terminate under overload

Its `do_sniff` status loop blocks on a register read that the busy `__comms`
thread starves, so `elapsed_time > timeout` is never re-checked. Kill it by
PID.

## The fork

Frank forked `OpenVizslaTNG/ov_ftdi` to his own GitHub. Working copy:
**`~/jockey3_linux/ov_ftdi`** (a plain clone, *not* a git submodule of
`alsa-jockey3`). If `ov-snapshot` graduates to its own repository later,
`ov_ftdi` becomes a submodule of *that* repo.

`capture.toml`'s `ov_ftdi_host_dir` should therefore point at
`~/jockey3_linux/ov_ftdi/software/host` going forward (the test hosts
currently still use an older `~/ov_ftdi`).

**A fresh clone needs `make -C software/host` before anything will run** --
it ships no `libov.so`. Without it, `import LibOV` fails instantly with a
plain `OSError: ... libov.so: cannot open shared object file`; through
`ov_snapshot.py` this looked, at first, like the process hanging rather than
crashing, purely because of sloppy PID tracking across several overlapping
ad-hoc test runs on `alsa-test` that evening -- worth remembering so it
doesn't cost the next person the same hour. Needs `libusb-1.0-dev` (present on
`alsa-test`); `make` succeeds cleanly with `gcc`/`pkg-config` already there.

## B3 tried and REVERTED -- do not revisit (2026-09-01)

B3 was a `LibOV.py` framing rewrite: `bytearray` + integer read cursor in both
`OVDevice.__comms` and `SDRAMRead.__SDRAMReadService.consume` (replacing
`buf = buf + b` / `buf = buf[code:]`); a `0xD0` fast-dispatch skipping the
service list for the packet-carrying frames; `memoryview` sub-slices instead
of `bytes` slices, with a single `bytes()` snapshot of the payload where it
escapes to handlers.

It was implemented, deployed, and swept at 44.1 / 48 / 88.2 / 96 kHz.
**Framing stayed correct** (a B3 slice parsed to 29441 transactions;
`extract_events.py` decoded a full enumeration). **It changed nothing
measurable** -- `__comms` still ~93.5 %, still ~50 pkt/s dropped at every rate
(96 kHz `overflow_delta` 1029 vs 1074 pre-B3; 44.1 kHz 575 vs 700 -- noise).

Why it can't help, so nobody tries again:

- The receive buffer was never the bottleneck. `buf += b` / `buf[code:]` is
  only O(n^2) once framing has fallen *far* behind; in the real regime the
  buffer stays a few KB and those ops are already cheap.
- The one genuine per-packet `bytes()` payload copy is irreducible -- the data
  must leave the transient receive buffer -- and B3 only *moved* it (from
  `on_packet` into `LibOV.consume`), ~10 % of the thread either way.
- The post-B3 profile is a **flat spread across ~15 lines** of per-packet
  Python (`bytes()` snapshot, `presentBytes` overhead, `getPacketSize` header
  parse, the `consume` bodies, `on_packet`, the `ctypes` read-out) with **no
  hotspot**. At ~350k packets/s that is just CPython executing ~10M simple
  ops/s -- most of a core. Rearranging Python does not change the op count.

B3 was reverted on 2026-09-01 (Frank's call): both fork clones are back to
**B1 + B2 only**. Kept: **B1** (`ov_snapshot.Ring` lock-free `on_packet`,
batched prune -- ~5 pts) and **B2** (`LibOV` PERR-print throttle + `perr_total`
counter -- kills a self-reinforcing print storm). Anything more on the Python
side is dead; lossless needs **fix A** (cut the packet count in gateware) or a
**C framing** rewrite (see below).

## Can it be spread over cores?

- **Plain `threading`: no.** The `__comms` thread is GIL-bound pure-Python;
  more threads just time-slice one core. That is exactly why it caps at ~1
  core today. (The FTDI read itself -- `FTDIDevice_ReadStream` / libusb --
  does drop the GIL during the transfer, so read and framing already overlap
  a bit; nothing more there.)
- **`multiprocessing`** (separate GILs, real parallelism): framing has a
  sequential dependency -- `cumulative_ts` accumulates packet-to-packet,
  `got_start`/FIRST/LAST is stateful, and you must find packet boundaries
  *before* you can distribute, and finding boundaries is most of the cost.
  Workable shape: one process does only boundary-finding + the ts-delta add
  (~5 ops/packet), hands packet spans to N workers over **shared memory**
  (not `Queue`/pickle -- pickling 350k objects/s costs more than it saves), a
  serializer reassembles in order. Plausible 2-3x, at the cost of a real
  rearchitecture of LibOV's receive path. Fragile; keep as last resort.
- **Free-threaded CPython (3.13t / PEP 703):** the least-invasive multi-core
  option -- worker threads share the `bytearray`/`memoryview` directly, no
  IPC. A scanner thread finds offsets + ts + FIRST/LAST; workers do the
  `bytes()` snapshot + handler dispatch. Caveats: experimental, ~5-10 %
  slower single-thread, `crcmod` (LibOV's one C-ext dep) needs a
  free-threaded build or a pure-Python CRC fallback, and `Ring.on_packet`
  goes back to needing a lock (multi-writer) or per-worker rings merged at
  trigger. Plausible 2-3x.
- **The C parser that is already in `libov.so`.** `read_async` has a
  commented-out `libov.CStreamCallback` path; `usb_interp.c` compiles into
  `libov.so` and its `CStreamCallback` frames the stream in C. **But**
  `ChandlePacket` only `printf`s the verbose text in C -- no Python callback --
  and its packet-size / timestamp constants are 2009-era and look stale
  against the current wire format (almost certainly why it is disabled). To
  use it for `ov-snapshot` you would write a C function that frames + appends
  `(ts, flags, orig_len, payload)` to a C ring buffer that Python drains in
  batches. A proper C-extension effort, but the highest software ceiling
  (C framing is ~10-50x Python) and it sidesteps the GIL entirely.

Ordering: **A (gateware) still wins** -- ~10x, benefits everything, no
host-side complexity. If A stalls, the **C framing path** beats any
multithreading (more headroom, no GIL gymnastics). Free-threaded 3.13 is the
lightest multi-core option but only ~2-3x. `multiprocessing` last.

## Where this stands

Python side: **done.** B1 + B2 landed (kept), B3 tried and reverted (above).
Result: ~99.99 % capture, ~50 packets/s dropped at every sample rate. Not
lossless, and nothing left to squeeze in Python.

Remaining routes to lossless, in order of promise:

- **A -- gateware NAK filtering** (`--filter-nak`; SOF is a rounding error --
  8k/s of ~400k). The stream is mostly a NAK/handshake storm, so dropping NAKs
  in gateware cuts the host packet rate by ~an order of magnitude, which
  scales down every per-packet Python cost with it. Highest ceiling by far.
  `software/fpga` is migen/misoc (target `ov3`); reading it needs only Python,
  synthesizing a bitstream needs a **Xilinx ISE**-class toolchain (old, free
  with registration, not an apt package -- Frank is bringing it up, likely
  containerized). Fix whatever currently corrupts the byte stream when the
  filter bits are set, rebuild, re-sweep.
- **C -- C framing.** `usb_interp.c`'s `CStreamCallback` already frames the
  stream in C and is wired (commented out) into `read_async`; its
  `ChandlePacket` only `printf`s and its constants look 2009-stale. Reworking
  it to append `(ts, flags, orig_len, payload)` to a C ring buffer that Python
  drains in batches would sidestep the GIL and the per-packet Python entirely
  (~10-50x). A real C-extension effort; second choice if A stalls. See
  "Can it be spread over cores?" for why this beats multithreading.

`ov-snapshot` already records the overflow (`perr`) delta in every sidecar, so
if neither A nor C happens soon it can ship at ~99.99 % with that as the
honesty signal.

## The 2026-09-01 `alsa-test` freeze -- device contention, not the data rate

`alsa-test` hard-froze during the 44.1 kHz run and had to be reset. Its
previous-boot kernel log ends:

```
11:14:58  usb 1-4: reset high-speed USB device number 3 using xhci_hcd
11:16:07  usb 1-4: reset high-speed USB device number 3 using xhci_hcd
11:17:20  usb 1-4: usbfs: interface 0 claimed by usbfs while 'python3' sets config #1
          <hard hang -- nothing further logged>
11:30:09  BIOS-e820: ...            <- cold boot after Frank power-cycled
```

Confirmed against `rsyslog` too (not just journald): **no oops, no panic, no
watchdog -- the machine went silent at 11:17:20 and stayed dead ~13 min until a
power cycle.** `1-4` is the OpenVizsla.

Root cause: **two `LibOV` processes were opening the OpenVizsla over usbfs at
the same time.** An earlier ad-hoc test had backgrounded `ov_snapshot.py` and
tried to stop it with shell job control (`kill %1`), which is a no-op in a
non-interactive ssh shell -- so that instance leaked and kept the device while
`rate_test.sh` started a second one. Repeated `reset ... device number 3` then
the usbfs claim collision wedged `xhci_hcd`.

A userspace race over a usbfs device should at worst get `-EBUSY`, never hang
the kernel -- so there is a real fragility in this host's xHCI/usbfs path. We
cannot fix that; we must not trip it. Hence the guard below is not politeness,
it is protecting the capture host from a silent hard hang.

So this is **not** evidence that the capture data rate freezes the host -- the
overflow storms in the earlier runs did not. It is a device-contention bug,
now guarded:

- `ov_snapshot.py` binds its control port **before** it opens the OpenVizsla,
  so a second instance exits on the port bind instead of both grabbing the USB
  device.
- Operationally: never rely on `kill %n` over ssh; kill by explicit PID and
  verify, or `setsid` + pkill on a unique marker.

## Tooling on `alsa-test` (surveyed / set up 2026-09-01)

`sysstat` and `linux-perf` installed (Frank, via apt/sudo).

**`py-spy` lives in a venv, per Frank's preference** -- one place for it and
for `ov_ftdi`'s Python dependencies, kept off the system interpreter:

```sh
~/venvs/ov-tools/          # python3 -m venv, NOT under any git checkout
  bin/py-spy                 -- sampling profiler, attaches live, no code changes
  bin/python3 -m pip ...     -- crcmod (LibOV.py / usb_interp.py's one dependency)
```

`python3-venv` isn't installed on `alsa-test` and needs sudo neither of us had
in the moment, so the venv was created with `python3 -m venv --without-pip`
and pip bootstrapped from `https://bootstrap.pypa.io/get-pip.py` (no apt, no
root). `crcmod` was already satisfied for the system interpreter too (Debian's
`python3-crcmod`), so `ov_snapshot.py` / `ovctl.py` keep running under system
`python3` unchanged -- the venv is additive, for `py-spy` and any future
`ov_ftdi`-side Python work, not a runtime switch.

**`py-spy` needs elevated permissions to attach to a process it didn't start**
-- confirmed: `~/venvs/ov-tools/bin/py-spy dump --pid <pid>` gets
`Permission Denied` unprivileged (`alsa-test` has
`kernel.yama.ptrace_scope=1`, same restriction that made an earlier `strace`
attach fail). Two ways to sidestep it, either sudo-gated:

- `sudo ~/venvs/ov-tools/bin/py-spy dump --pid <pid>` each time -- no setup, needs
  the password interactively.
- One-time `sudo setcap cap_sys_ptrace=eip ~/venvs/ov-tools/bin/py-spy`, after
  which the venv's `py-spy` works unprivileged. Nicer for a repeated profiling
  session; Frank's call.

None of this is needed for fix A beyond reading the migen source (plain
Python); synthesizing a bitstream needs the Xilinx toolchain noted above,
which is a separate decision.

## Next actions

1. B1 + B2 kept; B3 tried, net-neutral, reverted (both fork clones back to
   B1 + B2). Python side is done -- ~99.99 % capture, ~50 pkt/s dropped at
   every rate.
2. **A -- gateware NAK filtering.** Now the only path to lossless. Frank is
   bringing up ancient Xilinx ISE (likely containerized). Once it builds: fix
   whatever corrupts the byte stream when the filter bits are set, rebuild,
   re-sweep. Expect the packet rate to drop ~10x. (C framing is the fallback
   if ISE stalls -- see "Where this stands".)
3. Rig deployment (`ov_snapshot_trigger.py` -> `pi4test` dmesg,
   `~/.config/ov-snapshot/`) -- once capture is lossless, or accept ~99.99 %
   and ship with the overflow (`perr`) delta in every sidecar as the honesty
   signal (already recorded).

## B1 / B2 implemented (2026-09-01)

- **B1** (`ov_snapshot.py`): `Ring.on_packet` is now lock-free -- it is the
  sole writer; the HTTP thread requests a pre-window snapshot via a flag that
  `on_packet` services at a safe point, and `Ring.end()` uses a 50 ms settle
  after clearing `capturing`. The age-prune is amortized over 256 packets.
- **B2** (fork `LibOV.py`, `__RXCSniffService`): the per-overflow-packet
  `print("PERR ...")` is throttled to the first hit + every 65536th, with a
  `perr_total` counter. It is a clean ~9-line diff against the fork HEAD (`git
  diff software/host/LibOV.py`), uncommitted, on both fork clones.
- **Bug B1 surfaced and fixed:** `/status` was calling
  `Device.overflow_counters()`, an FPGA register read that has to round-trip
  through the (saturated) comms thread and *hangs* under load -- it wedged the
  whole HTTP server. All overflow accounting now uses `perr_total` (the B2
  counter: pure Python, instant, monotonic). `/status` no longer does any
  device I/O. `overflow_counters()` was removed.

Also: `on_packet` no longer does `bytes(buf)` -- LibOV's `consume()` already
hands a fresh `raw[offset:]` slice, so the defensive copy was redundant.

### Cross-host sweep with B1 + B2 (2026-09-01, `pi4test` = DUT after a reboot)

| Rate | wire pkts/s | `__comms` CPU | overflow (trigger-window delta) |
|---|---|---|---|
| 44.1 kHz | ~430k | ~93.5 % | 700 |
| 48 kHz | ~425k | ~93.5 % | 701 |
| 88.2 kHz | ~390k | ~93.8 % | 1048 |
| 96 kHz | ~370k | ~93.5 % | 1074 |

Three things this settles:

1. **The packet rate is essentially sample-rate-independent** -- ~370-430k/s,
   drifting *down* slightly as the rate rises (fewer, larger DATA packets).
   Audio DATA is only ~8-16k packets/s of it; the rest is SOF (8k/s) and a
   large, roughly constant **PING/NAK/ACK handshake storm** plus MIDI-IN
   interrupt polling. So "96 kHz is the hard case" was wrong -- every rate is
   the same case.
2. **The pre-B1 "44.1 kHz captured clean" was a single-host artifact.** That
   run sniffed `alsa-test`'s own bus (~250k packets/s); `pi4test`'s bus
   presents ~420k. Different host controller, different NAK/retry behavior.
3. **B1 bought ~5-6 points** (the per-packet lock was ~40 % of `on_packet`,
   now gone) but the `__comms` thread is still pegged and **every rate now
   drops ~50 packets/s** once the ring fills -- ~99.99 % capture, not
   lossless. B2 works: `PERR` prints went 13541 -> 1.

Post-B1 profile leaf breakdown (`__comms`, ~93 % of a core): ~65 % is `LibOV`
framing -- `presentBytes` (`LibOV.py:299/302/305/310`), `consume` (many
lines), `getPacketSize` (`:497`); ~10 % is `on_packet` (now just the 4-tuple
build); the rest read plumbing.

## Run log

- 2026-09-01: findings above; investigation opened, split out of
  `triggered_capture.md` at Frank's request.
- 2026-09-01 (later): Frank's fork cloned to `~/jockey3_linux/ov_ftdi` on
  `alsa-dev` and `alsa-test`; built with `make -C software/host`; `ov_snapshot.py`
  hardened (port-bind-before-device-open guard) and smoke-tested clean against
  it (869k packets rendered, 0 failures, overflow_delta 0, clean shutdown).
  Tooling surveyed for the profiling work; gateware toolchain (Xilinx) noted as
  a separate decision. 44.1 kHz clean re-measurement not yet redone on the
  fork.
- 2026-09-01 (later still): `sysstat` + `linux-perf` installed (Frank). `py-spy`
  in `~/venvs/ov-tools`; Frank ran the one-time `setcap cap_sys_ptrace` and
  installed `python3-venv`.
- 2026-09-01 (profiling session): Jockey 3 had been moved back onto `alsa-test`,
  so profiled single-host there. `py-spy` on the live `__comms` thread at
  44.1 and 96 kHz. Findings: bare `ovctl sniff hs` is ~85 % `USBInterpreter`
  (`hd()` hex-format + `print`); `ov_snapshot.py` bypasses that and is
  ~40 % `Ring.on_packet` + ~45 % `LibOV` framing.
- 2026-09-01 (B1/B2 + cross-host sweep): B1 + B2 implemented, deployed. `pi4test`
  xHCI wedged mid-attempt (only a reboot fixed it). After the reboot, swept
  44.1 / 48 / 88.2 / 96 kHz cross-host. Result: packet rate ~370-430k/s and
  **sample-rate-independent**; `__comms` pegged ~93.5 % at every rate; ~50
  pkt/s dropped at every rate once the ring fills. Pre-B1 "44.1 kHz clean" was
  a single-host lower-rate artifact. B2 works (PERR 13541 -> 1).
- 2026-09-01 (B3): `bytearray`+cursor framing in the fork's `LibOV.py` +
  0xD0 fast-dispatch + memoryview slices. Framing verified correct (slice
  parses, enumeration decodes). **Net neutral** on CPU and overflow -- the
  buffer was never the bottleneck; the real cost is a flat spread of per-packet
  Python at ~350k pkt/s with no hotspot. **Reverted the same day** (Frank's
  call) -- both fork clones back to B1 + B2 only. Do not revisit; see the
  "B3 tried and REVERTED" section for why it cannot help.
- 2026-09-01: Frank asked whether multi-core could help -- written up in "Can
  it be spread over cores?". Short version: not plain threads (GIL); C framing
  or free-threaded 3.13 could, but A still wins.
