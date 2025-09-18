import serial
import time
import json
import string

def configure_serial(port='COM10', baudrate=19600, timeout=0.01):
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout
    )

# Query a specific load cell
def read_weight(ser, channel):
    cmd = f"{channel}0\r".encode()
    ser.write(cmd)
    print(f"Sent: {channel}0")

    time.sleep(0.04)
    resp = ser.read_all()
    if not resp:
        print(f"No response received from load cell {channel}.")
        return None

    text = resp.decode('ascii', errors='ignore').strip()
    clean = text.replace('*', '').replace('#', '').strip()
    try:
        return float(clean)
    except ValueError:
        print(f"Invalid weight format from {channel}: '{clean}'")
        return None

def run_process(json_path='process_data.json'):
    with open(json_path, 'r') as f:
        all_steps = json.load(f)

    sorted_steps = sorted(all_steps.items(), key=lambda x: int(x[0].lstrip('step')))
    if not sorted_steps:
        print("No process steps found.")
        return

    ser = configure_serial()
    try:
        for step_name, data in sorted_steps:
            tank = data.get('tank', 'Unknown')
            chemical = data.get('chemical', 'Unknown')
            target_str = data.get('weight', '0kg')
            stirrer = data.get('stirrer', 'Unknown Stirrer')

            try:
                target = float(target_str.replace('kg', '').strip())
            except ValueError:
                print(f"Skipping {step_name}: Invalid target weight.")
                continue

            # Get tank number from "tankX" string → find corresponding channel
            tank_number = int(tank.replace("tank", "").strip())
            channel = chr(ord('A') + (tank_number - 1))  # tank1 → A, tank2 → B, ... tank23 → W

            print(f"\nProcessing {step_name}: {chemical} ({tank}, Loadcell {channel}) → Unloading into {stirrer}")
            print(f"Target to dispense: {target:.3f} kg")

            prev_weight = read_weight(ser, channel) or 0.0

            while True:
                current = read_weight(ser, channel)
                if current is None:
                    time.sleep(0.1)
                    continue

                dispensed = prev_weight - current
                print(f"Loadcell {channel}: Dispensed {dispensed:+.3f} kg (waiting for {target:.3f} kg)")
                if dispensed >= target:
                    print(f"Target reached for {step_name}. {chemical} unloaded into {stirrer}.")
                    break

                time.sleep(0.1)

            print(f"{tank} dispensing complete → {stirrer}")

    except KeyboardInterrupt:
        print("Process interrupted.")
    finally:
        ser.close()

# Example usage

run_process(json_path='process_data.json')
