#!/usr/bin/env python3
#
# Trace Metadata Sidecar Generator
#
# An OpenVizsla trace is only useful for as long as someone remembers why it
# was taken. Filenames do not carry that: a capture called "stalled_linux"
# stops being self-explanatory within months, and a capture containing no EP0
# traffic is indistinguishable from a botched one unless the objective was
# written down.
#
# So every trace gets a Markdown sidecar next to it, named after it. This
# script writes the skeleton, filling in everything that can be derived from
# the trace itself and leaving TODO markers for the things only the person who
# took the capture knows -- the host, the OS and driver version, and above all
# why it was taken.
#
# (C) 2026 Frank van de Pol
#

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reduce_transactions import parse_tx_line			# noqa: E402
from extract_events import collect_transfers, group_events, classify, rate_of  # noqa: E402

RATE_REQUESTS = ("SET_RATE", "GET_RATE")


def human_size(n):
	for unit in ("B", "KB", "MB", "GB"):
		if n < 1024 or unit == "GB":
			return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
		n /= 1024.0


def looks_parsed(path):
	"""Distinguish a parsed transaction log from a raw OpenVizsla trace."""
	with open(path, "r", encoding="ascii", errors="replace") as f:
		for _ in range(3):
			line = f.readline()
			if not line:
				break
			if line.startswith("Timestamp") or "|" in line:
				return True
			if line.startswith("[") or line.startswith("High-Speed"):
				return False
	return False


def survey(path):
	"""Single pass over a parsed log, collecting everything derivable."""
	addresses = {}
	endpoints = {}
	first_ts = last_ts = None
	n_tx = 0

	with open(path, "r", encoding="ascii", errors="replace") as f:
		for line in f:
			tx = parse_tx_line(line)
			if tx is None:
				continue
			n_tx += 1
			if first_ts is None:
				first_ts = tx["timestamp"]
			last_ts = tx["timestamp"]
			if "." in (tx["target"] or ""):
				address = tx["target"].rsplit(".", 1)[0]
				addresses[address] = addresses.get(address, 0) + 1
			if tx["ep"] is not None:
				endpoints[tx["ep"]] = endpoints.get(tx["ep"], 0) + 1

	with open(path, "r", encoding="ascii", errors="replace") as f:
		transfers = collect_transfers(f)
	events = group_events(transfers, 0.25)

	rates = set()
	for transfer in transfers:
		if any(transfer["decoded"].startswith(r) for r in RATE_REQUESTS):
			rate = rate_of(transfer)
			if rate:
				rates.add(rate)

	return {
		"n_tx": n_tx,
		"duration": (last_ts - first_ts) if first_ts is not None else 0.0,
		"addresses": addresses,
		"endpoints": endpoints,
		"transfers": transfers,
		"events": events,
		"rates": sorted(rates),
	}


def render(info, trace_name, trace_path, platform_hint):
	events = info["events"]
	kinds = {}
	for event in events:
		kind = classify(event)
		kinds[kind] = kinds.get(kind, 0) + 1

	try:
		mtime = datetime.date.fromtimestamp(os.path.getmtime(trace_path))
		size = human_size(os.path.getsize(trace_path))
	except OSError:
		mtime, size = "TODO", "TODO"

	endpoints = ", ".join(f"0x{ep:02x} ({count})"
			      for ep, count in sorted(info["endpoints"].items()))
	addresses = ", ".join(sorted(info["addresses"]))
	kinds_text = ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items())) or "none"
	rates_text = ", ".join(f"{r} Hz" for r in info["rates"]) or "none observed"
	has_ep0 = 0 in info["endpoints"]

	lines = [
		"---",
		f"capture: {trace_name}",
		f"captured: {mtime}",
		f"platform: {platform_hint or 'TODO -- macOS / Windows / Linux'}",
		"host: TODO -- machine this was captured on",
		"os_version: TODO",
		"driver_version: TODO -- for Linux, the alsa-jockey3 git hash",
		"application: TODO -- the software driving the device, with version",
		"module_build_id: TODO -- Linux only, from /sys/module/snd_reloop_jockey3/notes/",
		"kernel_config: TODO -- Linux only; debug or production, and which config",
		"device: Reloop Jockey 3 (confirm Remix 200c:1037 or Master Edition 200c:1019)",
		f"size_raw: {size}",
		f"usb_address: {addresses or 'unknown'}",
		f"has_control_traffic: {'yes' if has_ep0 else 'no'}",
		"---",
		"",
		f"# {trace_name}",
		"",
		"## Objective",
		"",
		"TODO -- why this trace was taken and what it was expected to show.",
		"**This is the field that matters most.** A trace with no EP0 traffic is",
		"indistinguishable from a botched capture unless the intent is recorded.",
		"",
		"## Conclusion",
		"",
		"TODO -- what the trace actually showed, once analyzed. Link the analysis",
		"document if there is one.",
		"",
		"## Contents (derived)",
		"",
		f"- Duration: {info['duration']:.3f} s, {info['n_tx']} USB transactions",
		f"- Device address(es): {addresses or 'unknown'}",
		f"- Endpoints seen: {endpoints}",
		f"- Control transfers: {len(info['transfers'])} in {len(events)} events",
		f"- Event kinds: {kinds_text}",
		f"- Sample rates programmed: {rates_text}",
		"",
	]

	if not has_ep0:
		lines += [
			"> **No EP0 traffic.** This trace cannot contribute to any",
			"> control-plane analysis (initialization, rate change). If that was",
			"> not the intent, the capture missed the event; if it was, say so",
			"> under Objective.",
			"",
		]

	if events:
		lines += [
			"### Events",
			"",
			f"| # | kind | at (s) | transfers | span (ms) |",
			"|---|---|---|---|---|",
		]
		for index, event in enumerate(events, 1):
			lines.append(
				f"| {index} | {classify(event)} | {event[0]['start']:.6f} | "
				f"{len(event)} | "
				f"{(event[-1]['end'] - event[0]['start']) * 1000:.3f} |"
			)
		lines.append("")

	return "\n".join(lines)


def main():
	parser = argparse.ArgumentParser(
		description="Generate a metadata sidecar for an OpenVizsla trace.")
	parser.add_argument("input",
			    help="parsed transaction log (fast), or a raw trace")
	parser.add_argument("--for", dest="trace_name", default=None,
			    help="name of the trace the sidecar describes, if it "
				 "differs from the input (e.g. the .txt.bz2 "
				 "original when the input is the parsed log)")
	parser.add_argument("--trace-path", default=None,
			    help="path to that original, for size and date")
	parser.add_argument("--platform", default=None,
			    help="macOS, Windows or Linux, to prefill the field")
	parser.add_argument("-o", "--output", default=None,
			    help="output file (default: <trace_name>.md beside it)")
	parser.add_argument("--force", action="store_true",
			    help="overwrite an existing sidecar")
	args = parser.parse_args()

	if not looks_parsed(args.input):
		sys.exit(f"error: {args.input} looks like a raw OpenVizsla trace.\n"
			 f"Parse it first:\n"
			 f"  python3 parse_openvizsla.py {args.input}\n"
			 f"then run this on the _parsed.txt output with "
			 f"--for {os.path.basename(args.input)}")

	trace_name = args.trace_name or os.path.basename(args.input)
	trace_path = args.trace_path or (
		os.path.join(os.path.dirname(os.path.abspath(args.input)), trace_name))

	info = survey(args.input)
	text = render(info, trace_name, trace_path, args.platform)

	out_path = args.output or os.path.join(
		os.path.dirname(os.path.abspath(trace_path)),
		os.path.splitext(trace_name)[0] + ".md")

	if os.path.exists(out_path) and not args.force:
		sys.exit(f"error: {out_path} exists; pass --force to overwrite. "
			 f"Hand-written fields would be lost.")

	with open(out_path, "w", encoding="ascii") as f:
		f.write(text)
	print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
	main()
