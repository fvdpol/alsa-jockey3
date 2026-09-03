#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# ov-snapshot -- rolling-buffer OpenVizsla capture with an external trigger.
#
# Runs on a capture host (ideally a machine doing nothing else) with an
# OpenVizsla wired into the USB path of a device under test. It sniffs
# high-speed traffic continuously into a time-bounded in-memory ring and keeps
# nothing on disk until something POSTs /trigger. On a trigger it writes just
# the pre-event ring plus a short post-event tail, as an ordinary
# `ovctl.py sniff hs` verbose-format file, with a Markdown sidecar.
#
# Deliberately standalone: no imports from any driver source tree, stdlib only
# apart from ov_ftdi's own LibOV / usb_interp, which are loaded from a path
# given in the config file. This is a candidate to graduate to its own repo.
#
# (C) 2026 Frank van de Pol

import argparse
import collections
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
import tomllib
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TICK_HZ = 60_000_000          # OpenVizsla timestamp tick rate
SDRAM_RING_SIZE = 16 * 1024 * 1024


# --------------------------------------------------------------------------- config

def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cap = cfg.get("capture", cfg)
    ov_dir = cap.get("ov_ftdi_host_dir")
    if not ov_dir:
        sys.exit("config: capture.ov_ftdi_host_dir is required "
                 "(path to ov_ftdi/software/host)")
    cap["ov_ftdi_host_dir"] = os.path.abspath(os.path.expanduser(ov_dir))
    cap.setdefault("fwpkg", os.path.join(cap["ov_ftdi_host_dir"], "ov3.fwpkg"))
    cap["fwpkg"] = os.path.abspath(os.path.expanduser(cap["fwpkg"]))
    cap.setdefault("listen_host", "0.0.0.0")
    cap.setdefault("listen_port", 8464)
    cap.setdefault("pre_seconds", 5.0)
    cap.setdefault("post_seconds", 5.0)
    cap.setdefault("filter_nak", False)
    out = cap.get("output_dir", ".")
    cap["output_dir"] = os.path.abspath(os.path.expanduser(out))
    cap.setdefault("filename_prefix", "snapshot")
    return cap


# ----------------------------------------------------------------------------- ring

class Ring:
    """Time-bounded ring of raw wire packets, plus a post-trigger tail.

    on_packet() runs in LibOV's FTDI reader thread -- the hot path, ~250k
    calls/s -- and is deliberately lock-free: it is the *only* writer of every
    field here. The HTTP thread only reads, and requests a pre-window snapshot
    by setting a flag that on_packet services at a safe point (so the deque is
    never copied while it is being mutated). Profiling showed the previous
    per-packet `threading.Lock` plus a prune scan on every packet was ~40% of
    this thread's CPU.
    """

    _PRUNE_EVERY = 256          # amortize the age-prune scan

    def __init__(self, pre_seconds, post_seconds):
        self.pre_ticks = int(pre_seconds * TICK_HZ)
        self.post_seconds = post_seconds
        self.buf = collections.deque()        # (ts, bytes, flags, orig_len)
        self.tail = []
        self._pre = None
        self.capturing = False
        self.pkts_total = 0
        self.first_ts = None
        self.last_ts = 0
        self._prune_ctr = self._PRUNE_EVERY
        self._begin_req = False
        self._begin_done = threading.Event()

    def on_packet(self, ts, buf, flags, orig_len):
        # buf is already an independent immutable `bytes` -- LibOV's consume()
        # hands us `raw[offset:]`, a fresh slice, not a view of a reused
        # buffer -- so no defensive copy is needed here.
        rec = (ts, buf, flags, orig_len)
        self.pkts_total += 1
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts
        dq = self.buf
        dq.append(rec)

        if self._begin_req:                   # HTTP thread asked for a snapshot
            self._pre = list(dq)
            self.tail = []
            self.capturing = True
            self._begin_req = False
            self._begin_done.set()

        if self.capturing:
            self.tail.append(rec)

        self._prune_ctr -= 1
        if self._prune_ctr <= 0:
            self._prune_ctr = self._PRUNE_EVERY
            horizon = ts - self.pre_ticks
            while dq and dq[0][0] < horizon:
                dq.popleft()

    def begin(self, timeout=3.0):
        """Have on_packet snapshot the pre-window. Blocks until it does; if the
        stream is silent (no packet to service the request) takes the snapshot
        itself, which is safe precisely because nothing else is mutating the
        deque then. Returns False if a capture is already in flight."""
        if self.capturing or self._begin_req:
            return False
        self._begin_done.clear()
        self._begin_req = True
        if self._begin_done.wait(timeout):
            return True
        self._begin_req = False
        for _ in range(3):
            try:
                self._pre = list(self.buf)
                break
            except RuntimeError:              # deque mutated mid-copy; retry
                continue
        self.tail = []
        self.capturing = True
        return True

    def end(self):
        """Stop collecting the tail and return pre + tail."""
        self.capturing = False
        time.sleep(0.05)                      # let any in-flight on_packet finish
        recs = (self._pre or []) + self.tail
        self._pre = None
        self.tail = []
        return recs

    def stats(self):
        dq = self.buf
        try:
            span = (self.last_ts - dq[0][0]) / TICK_HZ if dq else 0.0
        except IndexError:
            span = 0.0
        return {
            "packets_seen": self.pkts_total,
            "ring_packets": len(dq),
            "ring_seconds": round(span, 3),
            "capturing": self.capturing,
        }


# ------------------------------------------------------------------- device wrangling

class Device:
    """Thin wrapper around a LibOV OVDevice: open, arm sniff, read counters."""

    def __init__(self, cfg):
        self.cfg = cfg
        sys.path.insert(0, cfg["ov_ftdi_host_dir"])
        import LibOV
        import usb_interp
        self._LibOV = LibOV
        self._usb_interp = usb_interp
        self.reg_lock = threading.Lock()

        pkg = zipfile.ZipFile(cfg["fwpkg"], "r")
        self.dev = LibOV.OVDevice(mapfile=pkg.open("map.txt", "r"), verbose=False)
        err = self.dev.open(bitstream=pkg.open("ov3.bit", "r"))
        if err:
            raise SystemExit(f"OpenVizsla: error opening device ({err})")
        if not self.dev.isLoaded():
            self.dev.close()
            self.dev.open(bitstream=pkg.open("ov3.bit", "r"))
        # ovctl.py does this before any subcommand; harmless, keeps parity.
        self.dev.dev.write(LibOV.FTDI_INTERFACE_A, b"\x00" * 512, async_=False)

    # -- register helpers ---------------------------------------------------

    def _r(self, name):
        with self.reg_lock:
            return getattr(self.dev.regs, name).rd()

    def _w(self, name, val):
        with self.reg_lock:
            getattr(self.dev.regs, name).wr(val)

    def perr_total(self):
        """Count of overflow/error-flagged packets seen so far.

        A plain Python counter on the comms thread (LibOV RXCSniff), so reading
        it is instant and never touches the device. The FPGA `OVF_INSERT_NUM_*`
        registers carry the same information but a register read has to
        round-trip through the saturated comms thread and *hangs* under load --
        it must never be on the /status path.
        """
        svc = getattr(self.dev.rxcsniff, "service", None)
        return getattr(svc, "perr_total", 0)

    # -- sniff setup (trimmed from ovctl.py do_sniff) ----------------------

    def start_sniff(self):
        d = self.dev
        with self.reg_lock:
            d.regs.LEDS_MUX_2.wr(0)
            d.regs.LEDS_OUT.wr(0)
            d.regs.LEDS_MUX_0.wr(2)
            d.regs.LEDS_MUX_1.wr(2)
            d.regs.SDRAM_SINK_GO.wr(0)
            d.regs.SDRAM_HOST_READ_GO.wr(0)
            d.regs.SDRAM_SINK_RING_BASE.wr(0)
            d.regs.SDRAM_SINK_RING_END.wr(SDRAM_RING_SIZE)
            d.regs.SDRAM_HOST_READ_RING_BASE.wr(0)
            d.regs.SDRAM_HOST_READ_RING_END.wr(SDRAM_RING_SIZE)
            d.regs.SDRAM_SINK_GO.wr(1)
            d.regs.SDRAM_HOST_READ_GO.wr(1)
            d.regs.OVF_INSERT_CTL.wr(1)     # clear perf counters
            d.regs.OVF_INSERT_CTL.wr(0)
            if not (d.regs.ucfg_stat.rd() & 0x1):
                raise SystemExit("OpenVizsla: ULPI clock has not started (osc?)")
            d.ulpiregs.func_ctl.wr(0x48)    # HS, non-drive
            # CSTREAM_CFG bit 0 = stream enable, bit 2 = gateware NAK filter.
            # The NAK filter drops the PING/NAK + IN/NAK handshake storm before
            # it reaches the host, cutting the packet rate by ~10x. It corrupts
            # the byte stream only when register I/O runs concurrently with the
            # capture (the sync libusb_bulk_transfer for an EP0 read races the
            # comms thread's stream reaping); this tool does zero register I/O
            # while streaming -- see perr_total() -- so the filter is safe here.
            cfg_bits = 1
            if self.cfg.get("filter_nak"):
                cfg_bits |= (1 << 2)
            d.regs.CSTREAM_CFG.wr(cfg_bits)

    def stop_sniff(self):
        d = self.dev
        with self.reg_lock:
            d.regs.SDRAM_SINK_GO.wr(0)
            d.regs.SDRAM_HOST_READ_GO.wr(0)
            d.regs.CSTREAM_CFG.wr(0)

    def install_handler(self, fn):
        # Replace the default verbose printer outright -- we do not want its
        # stdout stream, only the raw callback.
        self.dev.rxcsniff.service.handlers = [fn]

    def close(self):
        with contextlib.suppress(Exception):
            self.stop_sniff()
        with contextlib.suppress(Exception):
            self.dev.close()

    # -- rendering --------------------------------------------------------

    def render_verbose(self, recs):
        """Replay raw packets through a fresh USBInterpreter, capturing its text.

        This is exactly what ovctl.py's verbose path does per packet; the
        output is a valid `sniff hs` capture that parse_openvizsla.py accepts.
        """
        ui = self._usb_interp.USBInterpreter()
        out = io.StringIO()
        bad = 0
        with contextlib.redirect_stdout(out):
            for ts, buf, flags, orig_len in recs:
                try:
                    ui.handlePacket(ts, buf, flags, orig_len)
                except Exception:
                    bad += 1
        return out.getvalue(), bad


# ------------------------------------------------------------------------- snapshots

class Snapshotter:
    def __init__(self, cfg, ring, device):
        self.cfg = cfg
        self.ring = ring
        self.device = device
        self.lock = threading.Lock()
        self.last = None
        os.makedirs(cfg["output_dir"], exist_ok=True)

    def trigger(self, meta):
        if not self.ring.begin():
            return {"ok": False, "error": "capture already in flight"}, 409

        perr0 = self.device.perr_total()
        t0 = datetime.now(timezone.utc)
        time.sleep(self.cfg["post_seconds"])
        recs = self.ring.end()
        perr1 = self.device.perr_total()

        stamp = t0.strftime("%Y%m%dT%H%M%SZ")
        tag = _slug(meta.get("tag") or "trigger")
        base = f"{self.cfg['filename_prefix']}_{stamp}_{tag}"
        trace_path = os.path.join(self.cfg["output_dir"], base + ".txt")
        side_path = os.path.join(self.cfg["output_dir"], base + ".md")

        text, bad = self.device.render_verbose(recs)
        with open(trace_path, "w") as f:
            f.write(text)

        info = {
            "trace": os.path.basename(trace_path),
            "packets": len(recs),
            "render_failures": bad,
            "overflow_delta": perr1 - perr0,
            "taken": t0.isoformat(),
        }
        with open(side_path, "w") as f:
            f.write(_sidecar(base, info, meta, self.cfg))

        with self.lock:
            self.last = info
        return {"ok": True, **info, "sidecar": os.path.basename(side_path)}, 200


def _slug(s):
    keep = "-_."
    return "".join(c if c.isalnum() or c in keep else "-" for c in s)[:48].strip("-") or "x"


def _sidecar(name, info, meta, cfg):
    kcfg = meta.get("kernel_config") or "unknown"
    if info["overflow_delta"] == 0:
        ovf_note = "trace is timing-sound"
    else:
        ovf_note = "TRACE DROPPED PACKETS -- not usable for timing conclusions"
    return f"""# {name}

- **trace**: `{info['trace']}`
- **taken**: {info['taken']}
- **tool**: ov-snapshot (triggered capture), verbose `sniff hs` format
- **trigger tag**: {meta.get('tag', '(none)')}
- **matched kernel line**: `{meta.get('kmsg_line', '(not supplied)')}`

## Capture

- speed: High Speed
- pre-window: {cfg['pre_seconds']} s
- post-window: {cfg['post_seconds']} s
- packets in slice: {info['packets']}  (render failures: {info['render_failures']})
- overflow/error-flagged packets during window: **{info['overflow_delta']}** ({ovf_note})

## Device under test  (reported by ov-snapshot-trigger)

- host: {meta.get('host', 'TODO')}
- os: {meta.get('os', 'TODO')}
- kernel: {meta.get('kernel', 'TODO')}
- module build-id: {meta.get('build_id', 'TODO')}
- kernel config: {kcfg}
- application: {meta.get('application', 'unknown / concurrent')}

## Objective

{meta.get('objective') or meta.get('kmsg_line') or 'TODO -- why was this captured'}

## Conclusion

TODO -- what the trace showed once analyzed.

<!-- Re-run re/usb/make_trace_sidecar.py on the parsed trace to fill in the
     derived fields (endpoints, control events, rates). -->
"""


# ------------------------------------------------------------------------------ http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # state is attached to the server instance (see main); this keeps the
    # control port claimable *before* the OpenVizsla is opened, so a second
    # instance fails on the port bind instead of both fighting over the USB
    # device -- which has frozen the capture host.

    def log_message(self, fmt, *a):
        print("[http] " + (fmt % a), file=sys.stderr)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        st = getattr(self.server, "state", None)
        if self.path.rstrip("/") not in ("", "/status"):
            self._send(404, {"error": "not found"})
            return
        if st is None:
            self._send(503, {"error": "still starting"})
            return
        self._send(200, {
            "armed": st["armed"]["v"],
            "overflow_total": st["device"].perr_total(),
            "last_snapshot": st["snap"].last,
            **st["ring"].stats(),
        })

    def do_POST(self):
        st = getattr(self.server, "state", None)
        p = self.path.rstrip("/")
        if st is None:
            self._send(503, {"error": "still starting"})
            return
        if p == "/arm":
            st["armed"]["v"] = True
            self._send(200, {"armed": True})
        elif p == "/disarm":
            st["armed"]["v"] = False
            self._send(200, {"armed": False})
        elif p == "/trigger":
            if not st["armed"]["v"]:
                self._send(425, {"ok": False, "error": "not armed"})
                return
            meta = self._body()
            try:
                result, code = st["snap"].trigger(meta)
            except Exception as e:                  # never take the server down
                self._send(500, {"ok": False, "error": repr(e)})
                return
            self._send(code, result)
        else:
            self._send(404, {"error": "not found"})


# ------------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config",
                    default=os.path.expanduser("~/.config/ov-snapshot/capture.toml"))
    ap.add_argument("--arm", action="store_true",
                    help="start already armed (default: wait for POST /arm)")
    args = ap.parse_args()

    # SIGTERM must run the same teardown as Ctrl-C: stop the SDRAM capture
    # engine and close the device. A hard kill skips that and leaves the FPGA
    # streaming into its ring, so the next run starts against a dirty buffer.
    def _on_sigterm(_signo, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

    cfg = load_config(args.config)
    print(f"[ov-snapshot] ov_ftdi: {cfg['ov_ftdi_host_dir']}")
    print(f"[ov-snapshot] output:  {cfg['output_dir']}")
    print(f"[ov-snapshot] window:  -{cfg['pre_seconds']}s / +{cfg['post_seconds']}s")

    # Bind the control port FIRST -- before touching the OpenVizsla. This is the
    # single-instance guard: two processes opening the same OV over usbfs at
    # once has hard-frozen the capture host. A second instance dies here.
    try:
        srv = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]),
                                  Handler)
    except OSError as e:
        sys.exit(f"[ov-snapshot] cannot bind {cfg['listen_host']}:"
                 f"{cfg['listen_port']} ({e}) -- another instance running?")

    device = None
    try:
        device = Device(cfg)
        ring = Ring(cfg["pre_seconds"], cfg["post_seconds"])
        device.install_handler(ring.on_packet)
        device.start_sniff()
        snap = Snapshotter(cfg, ring, device)
        srv.state = {"ring": ring, "snap": snap, "device": device,
                     "armed": {"v": bool(args.arm)}}
        print(f"[ov-snapshot] listening on {cfg['listen_host']}:"
              f"{cfg['listen_port']} ({'armed' if args.arm else 'not armed'})")
        srv.serve_forever()
    except KeyboardInterrupt:          # SIGINT (Ctrl-C) or SIGTERM
        print("\n[ov-snapshot] shutting down")
    finally:
        srv.shutdown()
        srv.server_close()
        if device is not None:
            device.close()


if __name__ == "__main__":
    main()
