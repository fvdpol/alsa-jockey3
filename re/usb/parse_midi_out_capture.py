# python script to analyse the OpenVizsla capture_midi_out.txt 
#
# objective is to validate the midi stream encapsulated in the playback stream
# confirm handling of MIDI running status: which the device does not support
# and confirm the use of the MIDI byte, SYNC byte and frame padding


import re
import sys

# Regex to match OUT transaction and DATA packet
out_pattern = re.compile(r"\[HS\s+\]\s+([\d.]+)\s+d=\s*[\d.]+\s+\[.*\]\s+\[\s*\d+\]\s+OUT\s+:\s+\d+\.5")
data_pattern = re.compile(r"DATA[01]:\s+(.*)")

def parse_capture(file_path):
    print(f"Parsing {file_path}...")
    midi_bytes_found = []
    expecting_data = False
    last_timestamp = 0.0
    packet_count = 0
    
    with open(file_path, 'r') as f:
        for line_num, line in enumerate(f, 1):
            out_match = out_pattern.search(line)
            if out_match:
                last_timestamp = float(out_match.group(1))
                expecting_data = True
                continue
            
            if expecting_data:
                if "OUT  :" in line or "IN   :" in line or "PING :" in line:
                    expecting_data = out_pattern.search(line) is not None
                    continue

                match = data_pattern.search(line)
                if match:
                    packet_count+=1
                    raw_hex = match.group(1).split()
                    if len(raw_hex) >= 512:
                        midi_byte = raw_hex[480]
                        sync_byte = raw_hex[481]
                        padding = raw_hex[482:512]
                        
                        if midi_byte != "fd" or any(p != "00" for p in padding):
                            midi_bytes_found.append({
                                "line": line_num,
                                "packet_count": packet_count,
                                "time": last_timestamp,
                                "midi": midi_byte,
                                "sync": sync_byte,
                                "padding_all_zero": all(p == "00" for p in padding)
                            })
                    expecting_data = False

    return midi_bytes_found

if __name__ == "__main__":
    #results = parse_capture("capture_midi_out.txt")
    results = parse_capture("usb capture 2026/capture_macos_44k1_512.txt")
    #results = parse_capture("capture_macos_96k_512.txt")
    
    #results = parse_capture("capture_linux_44k1_2.txt")
    #results = parse_capture("usb capture 2026/capture_linux_88k.txt")
    
    print(f"\nExtracted {len(results)} interesting MIDI packets (Endpoint 5):")
    print("-" * 80)
    
    last_time = None
    for i, r in enumerate(results):
        delta = (r['time'] - last_time) if last_time is not None else 0
        padding_status = "ZERO" if r['padding_all_zero'] else "NON-ZERO!"
        print(f"T={r['time']:.6f} (dt={delta:.6f}) | Packet {r['packet_count']:5d} | MIDI: {r['midi']} | Sync: {r['sync']} | Padding: {padding_status}")
        last_time = r['time']
        
        if i >= 40: # Show more to see a full MIDI message sequence
            print("...")
            break
