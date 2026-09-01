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

- **Sustained capture at 96 kHz does not work yet.** The bottleneck is
  `ov_ftdi` / `LibOV`'s Python receive path, and the gateware SOF/NAK filters
  that would relieve it are broken on the bundled bitstream. Full analysis and
  fix tracking: `ov_ftdi_capture_performance.md`. Frank maintains a fork of
  `ov_ftdi` for this (clone at `~/jockey3_linux/ov_ftdi`); point
  `capture.toml`'s `ov_ftdi_host_dir` there.
- `render_verbose` replays raw packets through `usb_interp.USBInterpreter`; the
  first few lines of a slice, before the first SOF, carry no frame number and
  are dropped by `parse_openvizsla.py`. Bounded and expected.
- `Ring.on_packet` (the ~250k/s hot path) is lock-free -- it is the only
  writer, and the HTTP thread gets its pre-window snapshot by setting a flag
  that `on_packet` services at a safe point. The age-prune is amortized over
  `_PRUNE_EVERY` packets. `Ring.end()` relies on a 50 ms settle after clearing
  `capturing` rather than a lock.
