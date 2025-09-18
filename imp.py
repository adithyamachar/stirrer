import serial
import json
import time

# Try to import RPi.GPIO, otherwise simulate
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    gpio_available = True
except ImportError:
    print("⚠️ GPIO not available. Running in simulation mode.")
    gpio_available = False

# Map each “tankX stirrerY” combo to a GPIO pin
combo_pins = {
    "tank1 stirrer1": 17,
    "tank1 stirrer2": 18,
    "tank1 stirrer3": 19,
    "tank1 stirrer4": 20,
    "tank1 stirrer5": 21,
    "tank1 stirrer6": 22,
    "tank1 stirrer7": 23,
    "tank1 stirrer8": 24,
}

# Initialize GPIO pins to LOW
if gpio_available:
    for pin in combo_pins.values():
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)


def load_steps():
    """Force reload process_data.json from disk."""
    filename = 'process_data.json'
    with open(filename, 'r') as f:
        raw = json.load(f)

    if 'process' in raw and isinstance(raw['process'], list):
        # Flatten "process" list
        data = {}
        for e in raw['process']:
            n = e.get('step')
            if n is None:
                continue
            data[f"step{n}"] = {k: v for k, v in e.items() if k != 'step'}
    else:
        data = raw
    return data


def get_activations(entry):
    """
    Yields tuples (stirrer_name, tank_name, expected_weight_kg).
    Converts weight string like "1.079kg" to float (1.079).
    """
    if 'stirrer' in entry and 'tank' in entry and 'weight' in entry:
        weight_val = float(entry['weight'].replace('kg', '').strip())
        yield entry['stirrer'], entry['tank'], weight_val
    else:
        for key in sorted(entry):
            if key.startswith('stirrer') and isinstance(entry[key], dict):
                sub = entry[key]
                weight_val = float(sub.get('weight', '0').replace('kg', '').strip())
                yield key, sub.get('tank', ''), weight_val


def read_weight(ser):
    """Send A0 and read weight from RS485."""
    ser.write(b"A0\r")
    time.sleep(0.05)  # Allow device to respond
    response = ser.readline().strip()
    if response:
        decoded = response.decode(errors="ignore").strip()
        decoded = decoded.lstrip('*').rstrip('#').strip()
        try:
            value = float(decoded.lstrip('+'))
            return value
        except ValueError:
            print(f"⚠️ Could not parse weight from: {decoded}")
    return None


def main():
    # Open the serial port
    ser = serial.Serial(
        port="COM10",             # Replace with your COM port
        baudrate=9600,            # Update if needed
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        bytesize=serial.EIGHTBITS,
        timeout=0.1
    )

    print("✅ Connected to RS485. Starting process...")

    try:
        for step_num in range(1, 100):  # Support step1 to step99
            step_key = f"step{step_num}"
            steps = load_steps()  # 🔄 reload JSON each time
            if step_key not in steps:
                print(f"🚫 No {step_key} in JSON. Stopping.")
                break

            entry = steps[step_key]
            for stirrer_name, tank_name, target_weight in get_activations(entry):

                # 🛑 Skip entire step if not tank1
                if tank_name != "tank1":
                    print(f"⏭️ Skipping {step_key}: {tank_name} {stirrer_name} (not tank1)")
                    continue

                combo = f"{tank_name} {stirrer_name}"
                pin = combo_pins.get(combo)

                print(f"\n➡️ {step_key}: Activating {combo} (target {target_weight} kg)")

                # Activate GPIO
                if pin:
                    if gpio_available:
                        GPIO.output(pin, GPIO.HIGH)
                    print(f"🟢 GPIO {pin} HIGH ({combo})")
                else:
                    print(f"⚪ No GPIO mapping for '{combo}', skipping GPIO.")

                # Get initial weight
                print("📡 Reading initial weight...")
                initial_weight = None
                while initial_weight is None:
                    initial_weight = read_weight(ser)
                print(f"⚖️ Initial weight: {initial_weight:.3f} kg")

                # Keep reading until weight drop reaches target
                while True:
                    current_weight = read_weight(ser)
                    if current_weight is not None:
                        diff = initial_weight - current_weight
                        print(f"📡 Current weight: {current_weight:.3f} kg | Δ={diff:.3f} kg")

                        if diff >= target_weight:
                            print(f"✅ Target weight reached (Δ={diff:.3f} kg)")
                            break
                    else:
                        print("❌ No weight response, retrying…")
                    time.sleep(0.2)

                # Deactivate GPIO
                if pin:
                    if gpio_available:
                        GPIO.output(pin, GPIO.LOW)
                    print(f"🔴 GPIO {pin} LOW ({combo})")

                time.sleep(0.5)  # Small delay before next stirrer

            print(f"✅ Completed {step_key}")

        print("\n🎉 All steps completed!")

    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")
    finally:
        ser.close()
        if gpio_available:
            GPIO.cleanup()
        print("🔌 Serial port closed and GPIO cleaned up.")


if __name__ == "__main__":
    main()
