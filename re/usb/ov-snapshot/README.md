# ov-snapshot

Triggered OpenVizsla capture: sniff USB high-speed traffic continuously into a
rolling in-memory ring, and write a short slice to disk only when something
external says "now". For catching faults that are too rare to sit and watch for
and too deep into a run to capture whole.

Design rationale and the measurements behind it: `../triggered_capture.md`.

This tool is **standalone on purpose** -- stdlib only, no imports from any
driver source tree. Its one dependency is a checkout of
[`ov_ftdi`](https://github.com/OpenVizslaTNG/ov_ftdi) (`LibOV.py`,
`usb_interp.py`, `ov3.fwpkg`), loaded from a path in the config. It is a
candidate to graduate to its own repository; keep it that way.

## Parts

| Program | Runs on | Does |
|---|---|---|
| `ov_snapshot.py` | capture host (ideally a machine doing nothing else, with the OpenVizsla wired into the DUT's USB path) | continuous `sniff hs` into a time-bounded ring; HTTP `arm` / `trigger` / `disarm` / `status`; on trigger writes `pre + post` as a verbose `sniff hs` file plus a Markdown sidecar |
| `ov_snapshot_trigger.py` | device under test | tails `dmesg --follow`, matches configured regexes, POSTs `/trigger` with the sidecar facts only the DUT knows (host, kernel, module build-id, config) |

## Setup

```sh
mkdir -p ~/.config/ov-snapshot
cp capture.toml.example ~/.config/ov-snapshot/capture.toml   # on the capture host
cp trigger.toml.example ~/.config/ov-snapshot/trigger.toml   # on the DUT
$EDITOR ~/.config/ov-snapshot/{capture,trigger}.toml
```

## Run

Capture host:

```sh
./ov_snapshot.py                 # add --arm to skip waiting for POST /arm
curl -s localhost:8464/status | python3 -m json.tool
```

DUT:

```sh
./ov_snapshot_trigger.py --dry-run   # see what matches without capturing
./ov_snapshot_trigger.py             # arm, watch, trigger
```

Fire one by hand:

```sh
curl -XPOST localhost:8464/arm
curl -XPOST localhost:8464/trigger -d '{"tag":"manual","objective":"smoke test"}'
```

## Output

`<prefix>_<UTCstamp>_<tag>.txt` -- a verbose `ovctl.py sniff hs` capture,
directly usable with `../parse_openvizsla.py` and the rest of that pipeline.
`<prefix>_<UTCstamp>_<tag>.md` -- the sidecar. **Check `overflow events during
window` first**: non-zero means the OpenVizsla SDRAM ring overran and the trace
cannot support a timing conclusion.

## Known limits / escape routes

- **Sustained capture of a busy DUT can outrun `ov_ftdi` / `LibOV`'s
  single-threaded Python receive path**, overflowing the OpenVizsla SDRAM ring
  (a Ploytec audio device polling its IN endpoints puts ~200k packets/s on the
  wire, most of it PING/NAK). Set `filter_nak = true` in `capture.toml` to have
  the FPGA drop the handshake storm (`CSTREAM_CFG` bit 2), which cuts the host
  packet rate by ~10x. The filter is widely believed to corrupt the byte
  stream, but that only happens when register I/O runs concurrently with the
  capture; ov-snapshot does none while streaming, so it is safe here. Full
  analysis: `ov_ftdi_capture_performance.md`. Frank maintains a fork of
  `ov_ftdi` for this (clone at `~/jockey3_linux/ov_ftdi`); point
  `capture.toml`'s `ov_ftdi_host_dir` there.
- **The OpenVizsla SDRAM ring is not cleared between capture sessions**
  (`ov_ftdi` #25). A warm start can begin reading on top of the previous
  session's bytes, and LibOV's framer desyncs where the stale data meets the
  live stream. `ov_snapshot` handles it in three layers, all stopgaps for the
  missing gateware ring-reset (drop them if LibOV gains a proper
  start-of-stream drain):
  1. clean shutdown waits for the gateware `HF0_LAST` end-marker so its *own*
     next run starts with an empty ring (`teardown_drain_timeout`);
  2. a warm start first turns the capture stream off (watchdog'd). If a
     hard-killed predecessor's stream is still running that write blocks --
     LibOV has no in-process recovery (#25 measured ~130 s stalls) -- so
     ov-snapshot exits non-zero after 6 s with a one-line fix (reload the
     FPGA, `reload_bitstream = true`, or let a supervisor restart it). A clean
     shutdown never triggers this;
  3. it then discards the first `drain_seconds` of stream (default 4 s, from a
     rig measurement) before the ring starts collecting.
- `render_verbose` replays raw packets through `usb_interp.USBInterpreter`; the
  first few lines of a slice, before the first SOF, carry no frame number and
  are dropped by `parse_openvizsla.py`. Bounded and expected.
- `Ring.on_packet` (the ~250k/s hot path) is lock-free -- it is the only
  writer, and the HTTP thread gets its pre-window snapshot by setting a flag
  that `on_packet` services at a safe point. The age-prune is amortized over
  `_PRUNE_EVERY` packets. `Ring.end()` relies on a 50 ms settle after clearing
  `capturing` rather than a lock.
