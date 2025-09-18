import sys
import os
import json
import glob
import re
import time
import threading
from pathlib import Path

TANKS_FILE = "tanks.json"
PROCESS_DATA_FILE = "process_data.json"

# ----------------- GPIO SETUP -----------------
try:
    import RPi.GPIO as GPIO

    print("✅ Using Raspberry Pi GPIO library")
except ImportError:
    from mock_gpio import GPIO

    print("⚠️ Using Mock GPIO (development mode)")

# Define GPIO pins for 8 tanks
TANK1_GPIO_PINS = {
    1: 17, 2: 18, 3: 27, 4: 22,
    5: 23, 6: 24, 7: 25, 8: 4
}

# Initialize GPIO
GPIO.setmode(GPIO.BCM)
for pin in TANK1_GPIO_PINS.values():
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, GPIO.LOW)


# ----------------- Serial/RS485 Communication -----------------
class SerialManager:
    def __init__(self, port="/dev/ttyUSB0", baudrate=9600):
        self.port = port
        self.baudrate = baudrate
        self.serial_connection = None

    def connect(self):
        """Connect to RS485 serial port"""
        try:
            import serial
            self.serial_connection = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=0.1
            )
            print(f"✅ Connected to RS485 on {self.port}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to RS485: {e}")
            return False

    def read_weight(self):
        """Send A0 command and read weight from RS485"""
        if not self.serial_connection:
            print("⚠️ No serial connection available")
            return None

        try:
            self.serial_connection.write(b"A0\r")
            time.sleep(0.05)
            response = self.serial_connection.readline().strip()

            if response:
                decoded = response.decode(errors="ignore").strip()
                decoded = decoded.lstrip('*').rstrip('#').strip()
                try:
                    value = float(decoded.lstrip('+'))
                    return value
                except ValueError:
                    print(f"⚠️ Could not parse weight from: {decoded}")
            return None
        except Exception as e:
            print(f"❌ Error reading weight: {e}")
            return None

    def disconnect(self):
        """Disconnect from serial port"""
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
            print("✅ Serial connection closed")


# ----------------- Process Data Handler -----------------
class ProcessDataHandler:
    def __init__(self):
        self.serial_manager = SerialManager()
        self.stop_flag = threading.Event()

    def load_process_data(self, file_path=None):
        """Load process_data.json file"""
        if file_path is None:
            # Try multiple common locations
            possible_paths = [
                PROCESS_DATA_FILE,
                f"/home/adith/{PROCESS_DATA_FILE}",
                f"./{PROCESS_DATA_FILE}"
            ]
        else:
            possible_paths = [file_path]

        for path in possible_paths:
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                    print(f"✅ Loaded process data from {path}")
                    return data
            except FileNotFoundError:
                continue
            except Exception as e:
                print(f"❌ Error loading {path}: {e}")
                continue

        raise FileNotFoundError(f"Could not find {PROCESS_DATA_FILE} in any of the expected locations")

    def get_activations(self, entry):
        """Extract stirrer activations from step entry"""
        activations = []
        lines = entry.split('\n') if isinstance(entry, str) else [str(entry)]

        for line in lines:
            if 'Stirrer' in line and 'kg' in line:
                match = re.match(r"Stirrer (\d+):.*?(\d+\.?\d*)kg", line)
                if match:
                    stirrer_num = int(match.group(1))
                    weight = float(match.group(2))
                    activations.append((f"stirrer{stirrer_num}", "tank1", weight))

        return activations

    def wait_for_weight_target(self, target_weight, stirrer_num, tolerance=0.1, timeout=300):
        """Wait for weight to meet criteria with RS485 monitoring"""
        print(f"  ⏳ Monitoring weight for stirrer {stirrer_num} - Target: {target_weight:.3f}kg")

        start_time = time.time()

        # Connect to RS485 if available
        rs485_available = self.serial_manager.connect()

        while time.time() - start_time < timeout:
            if self.stop_flag.is_set():
                print("⚠️ Process stopped by user")
                break

            current_weight = None

            if rs485_available:
                current_weight = self.serial_manager.read_weight()

            if current_weight is not None:
                weight_diff = abs(current_weight - target_weight)
                progress = min((current_weight / target_weight) * 100, 100) if target_weight > 0 else 100

                print(
                    f"    📊 Current: {current_weight:.3f}kg | Target: {target_weight:.3f}kg | Progress: {progress:.1f}%")

                # Check if weight meets criteria
                if weight_diff <= tolerance:
                    print(f"✅ Weight target achieved! Diff: {weight_diff:.3f}kg (tolerance: {tolerance}kg)")
                    break

                if current_weight >= target_weight:
                    print(f"✅ Target weight reached: {current_weight:.3f}kg >= {target_weight:.3f}kg")
                    break

            else:
                # Fallback: simulate based on time (1 second per kg)
                elapsed = time.time() - start_time
                simulated_progress = min((elapsed / target_weight) * 100, 100) if target_weight > 0 else 100
                print(f"    🔄 Simulated progress: {simulated_progress:.1f}% ({elapsed:.1f}s)")

                if elapsed >= target_weight:
                    print(f"✅ Simulated dispensing complete")
                    break

            time.sleep(1)

        # Turn off stirrer
        pin = TANK1_GPIO_PINS[stirrer_num]
        GPIO.output(pin, GPIO.LOW)
        print(f"🔴 Stirrer {stirrer_num} DONE: Target {target_weight:.3f}kg (Tank {stirrer_num} OFF)")

        if rs485_available:
            self.serial_manager.disconnect()

    def execute_process_step(self, step_data, step_number):
        """Execute a single process step"""
        print(f"\n🔄 Starting PROCESS STEP {step_number}")

        # Get activations for this step
        activations = []

        if isinstance(step_data, dict):
            for key, value in step_data.items():
                if key.startswith('stirrer'):
                    step_activations = self.get_activations(value)
                    activations.extend(step_activations)
        elif isinstance(step_data, str):
            activations = self.get_activations(step_data)

        if not activations:
            print(f"⚠️ No valid activations found for step {step_number}")
            return

        # Process all activations in this step
        for stirrer_id, tank_id, weight in activations:
            if self.stop_flag.is_set():
                print("⚠️ Process stopped by user")
                break

            stirrer_num = int(stirrer_id.replace('stirrer', ''))

            if stirrer_num not in TANK1_GPIO_PINS:
                print(f"⚠️ Invalid stirrer number: {stirrer_num}")
                continue

            if weight <= 0:
                print(f"⚠️ Skipping Stirrer {stirrer_num} - invalid weight: {weight}")
                continue

            print(f"\n⏱️ Stirrer {stirrer_num} - Target {weight:.3f}kg")

            # Turn on stirrer
            pin = TANK1_GPIO_PINS[stirrer_num]
            GPIO.output(pin, GPIO.HIGH)
            print(f"  🟢 Tank {stirrer_num} ON (GPIO {pin})")

            # Wait for weight criteria to be met
            self.wait_for_weight_target(weight, stirrer_num)

        print(f"✅ Process Step {step_number} completed\n")

    def execute_process(self, process_data_path=None):
        """Execute the complete process from process_data.json"""
        try:
            # Reset stop flag
            self.stop_flag.clear()

            # Load process data
            process_data = self.load_process_data(process_data_path)

            if not process_data:
                raise ValueError("No process data loaded")

            print(f"🚀 Starting process execution with {len(process_data)} steps")

            # Execute each step
            for step_number, step_data in enumerate(
                    process_data.items() if isinstance(process_data, dict) else enumerate(process_data), 1):
                if self.stop_flag.is_set():
                    print("⚠️ Process stopped by user")
                    break

                if isinstance(process_data, dict):
                    step_key, step_value = step_data
                    print(f"📋 Processing step: {step_key}")
                    self.execute_process_step(step_value, step_number)
                else:
                    self.execute_process_step(step_data, step_number)

            if not self.stop_flag.is_set():
                print("\n✅ All process steps completed successfully!")

            return True

        except Exception as e:
            print(f"❌ Process execution error: {e}")
            self.stop_all_tanks()
            return False

    def stop_all_tanks(self):
        """Emergency stop all tanks"""
        self.stop_flag.set()

        for stir_no, pin in TANK1_GPIO_PINS.items():
            GPIO.output(pin, GPIO.LOW)
            print(f"🔴 Tank {stir_no} (GPIO {pin}) OFF")

        print("⏹️ All tanks turned OFF.")

    def cleanup(self):
        """Cleanup GPIO resources"""
        self.stop_all_tanks()
        GPIO.cleanup()
        print("🧹 GPIO cleanup completed.")


# ----------------- Main Process Controller -----------------
class ProcessController:
    def __init__(self):
        self.handler = ProcessDataHandler()
        self.process_thread = None

    def run_process(self, process_data_path=None):
        """Run the process synchronously"""
        return self.handler.execute_process(process_data_path)

    def run_process_async(self, process_data_path=None, callback=None):
        """Run process in a separate thread"""

        def threaded_execute():
            try:
                result = self.handler.execute_process(process_data_path)
                if callback:
                    callback(success=result)
            except Exception as e:
                print(f"❌ Threaded process error: {e}")
                if callback:
                    callback(success=False, error=str(e))

        self.process_thread = threading.Thread(target=threaded_execute, daemon=True)
        self.process_thread.start()
        return self.process_thread

    def stop_process(self):
        """Stop the running process"""
        self.handler.stop_all_tanks()

    def is_running(self):
        """Check if process is currently running"""
        return self.process_thread and self.process_thread.is_alive()

    def cleanup(self):
        """Cleanup system resources"""
        self.handler.cleanup()


# ----------------- CLI Interface -----------------
def main():
    """Main execution function"""
    controller = ProcessController()

    print("🏭 Process Data Executor")
    print("=" * 50)

    try:
        # Check if process_data.json exists
        if not os.path.exists(PROCESS_DATA_FILE):
            print(f"⚠️ {PROCESS_DATA_FILE} not found in current directory")

            # Try alternative paths
            alt_path = f"/home/adith/{PROCESS_DATA_FILE}"
            if os.path.exists(alt_path):
                print(f"✅ Found {PROCESS_DATA_FILE} at {alt_path}")
                process_file = alt_path
            else:
                print(f"❌ Could not find {PROCESS_DATA_FILE} in expected locations")
                return
        else:
            process_file = PROCESS_DATA_FILE

        print(f"📄 Using process file: {process_file}")
        print("🚀 Starting automatic process execution...")

        # Run the process
        success = controller.run_process(process_file)

        if success:
            print("🎉 Process completed successfully!")
        else:
            print("❌ Process failed!")

    except KeyboardInterrupt:
        print("\n⚠️ Process interrupted by user")
        controller.stop_process()
    except Exception as e:
        print(f"❌ System error: {e}")
    finally:
        controller.cleanup()


if __name__ == "__main__":
    main()