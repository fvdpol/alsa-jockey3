#
# OpenVizsla USB Capture Parser
#
# The OpenViszla records USB traffic at the wire level, including all the
# low-level protocol handshake packets.  For our analysis to reverse
# engineer the Reloop Jockey 3 protocol this is far too detailed.  The
# purpose of this script to reduce the level op detail by parsing the USB
# transactions.
# 
# (C) 2026 Frank van de Pol
#

import sys
import os
import re
from pathlib import Path

# Same base regex to parse the raw line components
LOG_PATTERN = re.compile(
	r"^\[(?P<flags>[^\]]+)\]\s+"
	r"(?P<timestamp>[\d.]+)\s+"
	r"d=\s*(?P<delta>[\d.]+)\s+"
	r"\[\s*(?P<frame>[\d.]+)\s*\+\s*(?P<uframe>[\d.]+)\s*\]\s+"
	r"\[\s*(?P<len_info>\d+)\s*\]"
	r"(?:\s+(?P<packet_info>.*))?$"
)

def parse_line(line):
	match = LOG_PATTERN.match(line.strip())
	if not match: return None
	data = match.groupdict()

	if data.get("packet_info") is None:
		return None
	else:
		packet_info = data.get("packet_info", "").strip()
	p_type, p_target, p_bytes = None, None, []

	if ":" in packet_info:
		p_type, remainder = packet_info.split(":", 1)
		p_type = p_type.strip()
		remainder = remainder.strip()
		if re.match(r'^([0-9a-fA-F]{2}\s*)+$', remainder):
			p_bytes = remainder.split()
		else:
			p_target = remainder
	else:
		p_type = packet_info

	return {
		"timestamp": float(data["timestamp"]),
		"type": p_type,
		"target": p_target,
		"bytes": p_bytes
	}

def extract_transactions(log_lines):
	current_tx = None
	successful_transactions = []

	for line in log_lines:
		parsed = parse_line(line)
		if not parsed or not parsed["type"]:
			continue

		p_type = parsed["type"]

		# 1. Start of a new transaction block (SETUP, IN, OUT tokens)
		if p_type in ["SETUP", "IN", "OUT"]:
			current_tx = {
				"start_time": parsed["timestamp"],
				"type": p_type,
				"target": parsed["target"],
				"data": None,
				"status": None
			}
			continue

		if current_tx is None:
			continue

		# 2. Capture payload phase
		if "DATA" in p_type:
			raw_bytes = parsed["bytes"]
			if len(raw_bytes) >= 2:
				# Slice off the last 2 bytes (the CRC16)
				current_tx["data"] = raw_bytes[:-2]
			else:
				# If it's somehow less than 2 bytes, keep it as is
				current_tx["data"] = raw_bytes			
			continue

		# 3. Handshake phase (closes the transaction)
		# Treat NYET identically to ACK since data was accepted by the device
		if p_type in ["ACK", "NYET"]:
			current_tx["status"] = "SUCCESS"
			# Only save transactions that actually completed cleanly with data or setup tokens
			successful_transactions.append(current_tx)
			current_tx = None
		elif p_type in ["NAK", "STALL"]:
			# Ignore NAKs entirely—this filters out the host polling noise
			current_tx = None

	return successful_transactions



def main():

	if len(sys.argv) < 2:
		print("Usage: {} <input_file>".format(os.path.basename(sys.argv[0])))
		sys.exit(1)
	
	#filename = "usb capture 2026/capture_macos_44k_poweron.txt"
	filename = sys.argv[1].strip()
 
	path = Path(filename)
	parsed_path = path.with_name(f"{path.stem}_parsed{path.suffix}")
	parsed_filename = str(parsed_path)
	
	with open(filename, "r", encoding="ascii") as f:
		
		log_lines = f.readlines()
		print(f"Read {len(log_lines)} lines from capture file.")
		
		transactions = extract_transactions(log_lines)
		print(f"Extracted {len(transactions)} successful transactions.")
  
		with open(parsed_filename, "w", encoding="ascii") as out_file:
			out_file.write(f"{'Timestamp':<12} | {'Direction':<10} | {'Target':<8} | {'Bytes'} | {'Payload Data'}\n")
			out_file.write("-" * 100 + "\n")
			for tx in transactions:
				# Format payload bytes cleanly or indicate no data (e.g., zero-length packets)
				data_str = " ".join(tx["data"]) if tx["data"] else "[]"
				data_len = len(tx["data"]) if tx["data"] else 0
				out_file.write(f"{tx['start_time']:<12.6f} | {tx['type']:<10} | {tx['target']:<8} | {data_len:<5} | {data_str}\n")


if __name__ == "__main__":
	main()
