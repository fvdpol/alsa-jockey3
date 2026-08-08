import mido
import time
import sys
import select
import termios
import tty
import atexit


# Global for restoring terminal
old_terminal_settings = None

def setup_terminal():
    global old_terminal_settings
    if sys.platform != "win32":
        old_terminal_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        
        # Restore on exit
        def restore_terminal():
            if old_terminal_settings is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_terminal_settings)
        atexit.register(restore_terminal)

def get_key():
    if sys.platform == "win32":
        import msvcrt
        if msvcrt.kbhit():
            return msvcrt.getch().decode('utf-8', errors='ignore').lower()
        return None
    else:
        # Non-blocking check
        if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
            ch = sys.stdin.read(1)
            return ch.lower() if ch else None
        return None
        


def list_ports():
    print("Available Input Ports:")
    for port in mido.get_input_names():
        print(f"  - {port}")
    print("\nAvailable Output Ports:")
    for port in mido.get_output_names():
        print(f"  - {port}")

def find_jockey_ports():
    input_port_name = None
    output_port_name = None
    
    for name in mido.get_input_names():
        if "Jockey" in name:
            input_port_name = name
            break
            
    for name in mido.get_output_names():
        if "Jockey" in name:
            output_port_name = name
            break
            
    return input_port_name, output_port_name

def main():
    print("MIDI Performance Test Tool")
    print("--------------------------")
    
    list_ports()
    
    in_name, out_name = find_jockey_ports()
    
    if not in_name or not out_name:
        print("\nError: Reloop Jockey3 not found.")
        print("Please ensure it is connected and powered on.")
        return

    print(f"\nOpening Input: {in_name}")
    print(f"Opening Output: {out_name}")
    
    setup_terminal()

    try:
        with mido.open_input(in_name) as inport, mido.open_output(out_name) as outport:
            print("\nControls: [d] Hexdump, [i] Rate Mode, [s] Toggle Blink, [+/-] Speed, [q] Quit")
            print("Current Mode: Hexdump | Blink: OFF")
            
            mode = 'd' # 'd' = Hexdump, 'i' = Rate
            blinking_active = False
            blink_interval = 0.5 # Default 500ms (1.0 Hz)
            start_time = time.time()
            last_report_time = start_time
            
            # Rate tracking
            rx_byte_count = 0
            rx_peak_rate = 0
            tx_peak_rate = 0
            current_tx_rate = 0 # Calculated based on burst timing
            
            # Send Mode state
            last_send_time = 0
            led_is_on = False
            
            # List of notes to toggle for send test
            test_notes = [0x1B, 0x0B, 0x0C, 0x0D, 0x0E, 0x17, 0x18, 0x19, 0x1A, 0x01, 0x02,0x03,0x04, 0x05, 0x06,0x07,0x08]
            #test_notes = [0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08]

            bytes_per_burst = len(test_notes) * 2 * 3 # 18 notes * 3 bytes
            
            def set_all_leds(state):
                val = 0x7F if state else 0x00
                for channel in range(2):
                    for note in test_notes:
                        msg = mido.Message('note_on', channel=channel, note=note, velocity=val)
                        outport.send(msg)

            while True:
                # Check for key press
                key = get_key()
                if key:
                    if key == 'd':
                        mode = 'd'
                        print(f"\n>>> Mode: Hexdump | Blink: {'ON' if blinking_active else 'OFF'}")
                    elif key == 'i':
                        mode = 'i'
                        print(f"\n>>> Mode: Rate Mode | Blink: {'ON' if blinking_active else 'OFF'}")
                    elif key == 's':
                        blinking_active = not blinking_active
                        if not blinking_active:
                            set_all_leds(False)
                            current_tx_rate = 0
                        print(f"\n>>> Blink Toggle: {'ON' if blinking_active else 'OFF'}")
                        last_send_time = time.time()
                    elif key in ('+', '='):
                        blink_interval /= 1.1
                        hz = 1.0 / (2 * blink_interval)
                        print(f"\n>>> Frequency Increased: {hz:.2f} Hz")
                    elif key == '-':
                        blink_interval *= 1.1
                        hz = 1.0 / (2 * blink_interval)
                        print(f"\n>>> Frequency Decreased: {hz:.2f} Hz")
                    elif key == 'q':
                        set_all_leds(False)
                        break

                current_time = time.time()
                
                # Process MIDI input (always track rate)
                for msg in inport.iter_pending():
                    rx_byte_count += len(msg.bytes())
                    
                    if mode == 'd':
                        hex_data = ' '.join(f'{b:02X}' for b in msg.bytes())
                        print(f"[{current_time - start_time:.3f}] RX: {hex_data}")
                
                # Periodic rate reporting (every 1 second)
                if current_time - last_report_time >= 1.0:
                    rx_peak_rate = max(rx_peak_rate, rx_byte_count)
                    tx_peak_rate = max(tx_peak_rate, current_tx_rate)
                    
                    if mode == 'i':
                        hz = 1.0 / (2 * blink_interval)
                        report = f"[{current_time - start_time:.1f}s] "
                        report += f"RX: {rx_byte_count:4} B/s (Peak: {rx_peak_rate:4}) | "
                        report += f"TX: {int(current_tx_rate):4} B/s (Peak: {int(tx_peak_rate):4}) "
                        if blinking_active:
                            report += f"[Blinking @ {hz:.1f} Hz]"
                        print(report)
                    
                    rx_byte_count = 0
                    last_report_time = current_time

                # Handle Blinking (Independent of mode)
                if blinking_active:
                    if current_time - last_send_time >= blink_interval:
                        actual_dt = current_time - last_send_time
                        current_tx_rate = bytes_per_burst / actual_dt
                        
                        led_is_on = not led_is_on
                        set_all_leds(led_is_on)
                        last_send_time = current_time
                    
                time.sleep(0.001)
                
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()
