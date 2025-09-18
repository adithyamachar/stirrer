import sys
import os
import json
import time
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem, QComboBox, QSpinBox,
    QCheckBox, QHeaderView, QInputDialog, QMessageBox, QDoubleSpinBox,
    QLabel, QScrollArea, QFrame, QTextEdit
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

# ---------------- PATHS ----------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_DIR = os.path.join(SCRIPT_DIR, "products")
TANKS_FILE = os.path.join(SCRIPT_DIR, "tanks.json")
PROCESS_FILE = os.path.join(SCRIPT_DIR, "process_data.json")
DISPENSING_LOG_FILE = os.path.join(SCRIPT_DIR, "dispensing_log.json")
GPIO_MAP_FILE = os.path.join(SCRIPT_DIR, "gpio_map.json")

os.makedirs(PRODUCTS_DIR, exist_ok=True)

# ---------------- SERIAL ----------------
import serial


def configure_serial(port='COM10', baudrate=19600, timeout=0.05):
    """Always use very small timeout to avoid blocking."""
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout
    )


def read_weight(ser, channel):
    """Non-blocking weight read"""
    try:
        if not ser or not ser.is_open:
            return None
        cmd = f"{channel}0\r".encode()
        ser.write(cmd)
        time.sleep(0.02)  # tiny delay
        resp = ser.read(ser.in_waiting or 1)  # non-blocking
        if not resp:
            return None
        text = resp.decode('ascii', errors='ignore').strip()
        clean = text.replace('*', '').replace('#', '').strip()
        return float(clean)
    except Exception:
        return None


# ---------------- DISPENSING LOG FUNCTIONS ----------------
def initialize_dispensing_log():
    """Initialize or reset the dispensing log for a new production run"""
    log_data = {
        "production_start_time": datetime.now().isoformat(),
        "stirrers": {}
    }

    with open(DISPENSING_LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=4)

    return log_data


def update_dispensing_log(stirrer_num, step_num, chemical, target_weight, dispensed_weight, status, tank_name=None,
                          tank_contents=None, tank_number=None):
    """Update the dispensing log with current step progress including tank information"""
    try:
        # Load existing log
        if os.path.exists(DISPENSING_LOG_FILE):
            with open(DISPENSING_LOG_FILE, "r") as f:
                log_data = json.load(f)
        else:
            log_data = initialize_dispensing_log()

        # Initialize stirrer section if not exists
        stirrer_key = f"stirrer_{stirrer_num}"
        if stirrer_key not in log_data["stirrers"]:
            log_data["stirrers"][stirrer_key] = {
                "stirrer_name": f"Stirrer {stirrer_num}",
                "steps": {}
            }

        # Update step data with tank information
        step_key = f"step_{step_num}"
        step_data = {
            "chemical": chemical,
            "target_weight_kg": round(target_weight, 3),
            "dispensed_weight_kg": round(dispensed_weight, 3),
            "progress_percent": round((dispensed_weight / target_weight * 100) if target_weight > 0 else 0, 1),
            "status": status,
            "last_updated": datetime.now().isoformat()
        }

        # Add tank information if provided
        if tank_name:
            step_data["source_tank"] = tank_name
        if tank_contents:
            step_data["tank_contents"] = tank_contents
        if tank_number:
            step_data["tank_number"] = tank_number

        log_data["stirrers"][stirrer_key]["steps"][step_key] = step_data

        # Save updated log
        with open(DISPENSING_LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=4)

    except Exception as e:
        print(f"Error updating dispensing log: {str(e)}")


# ---------------- MAIN WINDOW ----------------
class MainWindow(QMainWindow):
    NUM_TANKS = 25        # user specified: 25 tanks
    NUM_STIRRERS = 4      # user specified: 4 stirrers
    GPIO_TANK_LIMIT = 5   # only tanks 1..5 will get GPIO mappings

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Production Control")
        self.setGeometry(300, 200, 1200, 800)

        # widgets - initialize to None
        self.main_menu_widget = None
        self.batch_widget = None
        self.product_widget = None
        self.tank_widget = None
        self.production_monitor_widget = None
        self.dispensing_log_widget = None

        # process state
        self.ser = None
        self.stirrer_processes = {}  # Store process data for each stirrer
        self.stirrer_tables = {}  # Store table widgets for each stirrer
        self.stirrer_labels = {}  # Store status labels for each stirrer
        self.tanks_data = {}  # Store tank information
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_process)

        # GPIO state
        self.gpio_map = {}
        self.gpio_sim = False  # if True, we simulate GPIO (print) instead of real RPi.GPIO
        self.available_pins = [
            2, 3, 4, 17, 27, 22, 10, 9, 11, 5, 6, 13, 19, 26, 14, 15, 18, 23, 24, 25,
            8, 7, 12, 16, 20, 21
        ]  # common BCM pins list (may be adapted by user)
        self.active_outputs = set()  # currently ON outputs as keys 'tank{n}_stirrer{m}'
        self.paused_outputs = set()  # remember outputs that were on when paused

        # attempt to load gpio mapping and initialize gpio
        self.load_gpio_map()
        self.init_gpio()

        # Load tank data
        self.load_tanks_data()

        # Start with main menu
        self.show_main_menu()

    # ---------- GPIO Helpers ----------
    def load_gpio_map(self):
        """Load (or create) a gpio mapping file mapping each tank+stirrer combo to a BCM pin.
        Only tanks 1..GPIO_TANK_LIMIT receive pins; others map to None."""
        try:
            if os.path.exists(GPIO_MAP_FILE):
                with open(GPIO_MAP_FILE, "r") as f:
                    self.gpio_map = json.load(f)
            else:
                # create default mapping for only tanks 1..GPIO_TANK_LIMIT
                needed = self.GPIO_TANK_LIMIT * self.NUM_STIRRERS
                pins = []
                idx = 0
                while len(pins) < needed:
                    pins.append(self.available_pins[idx % len(self.available_pins)])
                    idx += 1

                mapping = {}
                k = 0
                for t in range(1, self.NUM_TANKS + 1):
                    for s in range(1, self.NUM_STIRRERS + 1):
                        key = f"tank{t}_stirrer{s}"
                        if t <= self.GPIO_TANK_LIMIT:
                            mapping[key] = pins[k]
                            k += 1
                        else:
                            mapping[key] = None  # explicitly no GPIO for these tanks

                self.gpio_map = mapping
                with open(GPIO_MAP_FILE, "w") as f:
                    json.dump(self.gpio_map, f, indent=2)
        except Exception as e:
            print(f"Error loading gpio map: {e}")
            self.gpio_map = {}

    def init_gpio(self):
        """Try to initialize RPi.GPIO. If not present, fall back to simulation mode."""
        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO
            GPIO.setmode(GPIO.BCM)
            # set all mapped pins as outputs and set LOW (only non-None pins)
            unique_pins = {p for p in self.gpio_map.values() if p is not None}
            for p in unique_pins:
                try:
                    GPIO.setup(p, GPIO.OUT)
                    GPIO.output(p, GPIO.LOW)
                except Exception as e:
                    print(f"GPIO setup error for pin {p}: {e}")
                    # if any pin fails, switch to simulation to avoid runtime crashes
                    self.gpio_sim = True
                    break
            if not getattr(self, "gpio_sim", False):
                self.gpio_sim = False
        except Exception:
            # RPi.GPIO not available: enable simulation
            self.gpio_sim = True
            self.GPIO = None
            print("RPi.GPIO not available — running in GPIO simulation mode (no hardware changes).")

    def set_output(self, tank_number, stirrer_number, on: bool):
        """Set GPIO corresponding to tank{n}_stirrer{m} to on/off. Handles simulation fallback.
        If mapping is None (no pin assigned), this is a no-op."""
        key = f"tank{tank_number}_stirrer{stirrer_number}"
        pin = self.gpio_map.get(key)
        if pin is None:
            # no mapping - nothing to do
            return

        try:
            if self.gpio_sim or not hasattr(self, "GPIO") or self.GPIO is None:
                # simulation mode
                if on:
                    self.active_outputs.add(key)
                    print(f"[GPIO SIM] SET {key} -> PIN {pin} HIGH")
                else:
                    if key in self.active_outputs:
                        self.active_outputs.discard(key)
                    print(f"[GPIO SIM] SET {key} -> PIN {pin} LOW")
            else:
                # hardware mode
                if on:
                    self.GPIO.output(pin, self.GPIO.HIGH)
                    self.active_outputs.add(key)
                else:
                    self.GPIO.output(pin, self.GPIO.LOW)
                    if key in self.active_outputs:
                        self.active_outputs.discard(key)
        except Exception as e:
            print(f"Error setting GPIO {pin} for {key}: {e}")

    def cleanup_gpio(self):
        """Turn off all outputs and cleanup GPIO if using real hardware."""
        try:
            # turn off all mapped pins
            for key, pin in self.gpio_map.items():
                try:
                    if pin is None:
                        continue
                    if self.gpio_sim or not hasattr(self, "GPIO") or self.GPIO is None:
                        if key in self.active_outputs:
                            print(f"[GPIO SIM] CLEANUP: SET {key} -> PIN {pin} LOW")
                        self.active_outputs.discard(key)
                    else:
                        self.GPIO.output(pin, self.GPIO.LOW)
                        self.active_outputs.discard(key)
                except Exception:
                    pass
            if not self.gpio_sim and hasattr(self, "GPIO") and self.GPIO is not None:
                try:
                    self.GPIO.cleanup()
                except Exception:
                    pass
        except Exception as e:
            print(f"Error cleaning up GPIO: {e}")

    # -------- Tanks data ----------
    def load_tanks_data(self):
        """Load tank data for reference during production"""
        try:
            if os.path.exists(TANKS_FILE):
                with open(TANKS_FILE, "r") as f:
                    tanks_list = json.load(f)
                # Convert to dict for easy lookup
                self.tanks_data = {}
                for i, tank in enumerate(tanks_list):
                    tank_key = f"tank{i + 1}"
                    self.tanks_data[tank_key] = {
                        "name": tank.get("name", f"Tank {i + 1}"),
                        "contents": tank.get("contents", "")
                    }
            else:
                # Default tank data: now NUM_TANKS tanks
                tanks_default = [{"name": f"Tank {i + 1}", "contents": ""} for i in range(self.NUM_TANKS)]
                with open(TANKS_FILE, "w") as f:
                    json.dump(tanks_default, f, indent=2)
                self.tanks_data = {f"tank{i}": {"name": f"Tank {i}", "contents": ""} for i in range(1, self.NUM_TANKS + 1)}
        except Exception as e:
            print(f"Error loading tanks data: {str(e)}")
            self.tanks_data = {f"tank{i}": {"name": f"Tank {i}", "contents": ""} for i in range(1, self.NUM_TANKS + 1)}

    # -------- Main Menu --------
    def show_main_menu(self):
        """Create and show the main menu (recreate each time to avoid widget issues)"""
        self.main_menu_widget = QWidget()
        layout = QVBoxLayout(self.main_menu_widget)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(30)

        button_style = """
            QPushButton {
                background-color: #2E86C1;
                color: white;
                border-radius: 12px;
                font-size: 18px;
                padding: 12px 24px;
            }
            QPushButton:hover {
                background-color: #3498DB;
            }
        """

        self.btn_batch = QPushButton("Batch Production")
        self.btn_tank = QPushButton("Tank Settings")
        self.btn_products = QPushButton("Product Configurations")
        self.btn_dispensing_log = QPushButton("View Dispensing Log")

        for btn in (self.btn_batch, self.btn_tank, self.btn_products, self.btn_dispensing_log):
            btn.setStyleSheet(button_style)
            btn.setMinimumSize(250, 60)

        layout.addWidget(self.btn_batch)
        layout.addWidget(self.btn_tank)
        layout.addWidget(self.btn_products)
        layout.addWidget(self.btn_dispensing_log)

        self.btn_batch.clicked.connect(self.open_batch_production)
        self.btn_products.clicked.connect(self.open_product_configurations)
        self.btn_tank.clicked.connect(self.open_tank_settings)
        self.btn_dispensing_log.clicked.connect(self.open_dispensing_log)

        self.setCentralWidget(self.main_menu_widget)

    # -------- Dispensing Log Viewer --------
    def open_dispensing_log(self):
        """Open the dispensing log viewer"""
        self.stop_process()

        self.dispensing_log_widget = QWidget()
        layout = QVBoxLayout(self.dispensing_log_widget)

        # Title
        title = QLabel("Dispensing Log")
        title.setFont(QFont("Arial", 16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # Log display area
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Courier", 10))
        layout.addWidget(self.log_display)

        # Load and display log
        self.load_and_display_log()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_refresh = QPushButton("Refresh")
        btn_clear = QPushButton("Clear Log")
        btn_back = QPushButton("Back")

        btn_refresh.clicked.connect(self.load_and_display_log)
        btn_clear.clicked.connect(self.clear_dispensing_log)
        btn_back.clicked.connect(self.go_back_to_main)

        btn_layout.addWidget(btn_refresh)
        btn_layout.addWidget(btn_clear)
        btn_layout.addWidget(btn_back)
        layout.addLayout(btn_layout)

        self.setCentralWidget(self.dispensing_log_widget)

    def load_and_display_log(self):
        """Load and display the dispensing log in a formatted way with tank information"""
        try:
            if not os.path.exists(DISPENSING_LOG_FILE):
                self.log_display.setText("No dispensing log found. Start a production run to create one.")
                return

            with open(DISPENSING_LOG_FILE, "r") as f:
                log_data = json.load(f)

            # Format the log for display
            display_text = f"Production Started: {log_data.get('production_start_time', 'Unknown')}\n"
            display_text += "=" * 80 + "\n\n"

            for stirrer_key, stirrer_data in log_data.get("stirrers", {}).items():
                stirrer_name = stirrer_data.get("stirrer_name", stirrer_key)
                display_text += f">>> {stirrer_name} <<<\n"
                display_text += "-" * 40 + "\n"

                steps = stirrer_data.get("steps", {})
                if not steps:
                    display_text += "No steps recorded.\n\n"
                    continue

                for step_key in sorted(steps.keys(), key=lambda x: int(x.split('_')[1])):
                    step_data = steps[step_key]
                    display_text += f"{step_key.replace('_', ' ').title()}:\n"
                    display_text += f"  Chemical: {step_data.get('chemical', 'Unknown')}\n"

                    # Add tank information if available
                    if 'source_tank' in step_data:
                        tank_info = f"  Source Tank: {step_data['source_tank']}"
                        if 'tank_contents' in step_data and step_data['tank_contents']:
                            tank_info += f" (Contains: {step_data['tank_contents']})"
                        if 'tank_number' in step_data:
                            tank_info += f" - Tank #{step_data['tank_number']}"
                        display_text += tank_info + "\n"

                    display_text += f"  Target Weight: {step_data.get('target_weight_kg', 0):.3f} kg\n"
                    display_text += f"  Dispensed Weight: {step_data.get('dispensed_weight_kg', 0):.3f} kg\n"
                    display_text += f"  Progress: {step_data.get('progress_percent', 0):.1f}%\n"
                    display_text += f"  Status: {step_data.get('status', 'Unknown')}\n"
                    display_text += f"  Last Updated: {step_data.get('last_updated', 'Unknown')}\n"
                    display_text += "\n"

                display_text += "\n"

            self.log_display.setText(display_text)

        except Exception as e:
            self.log_display.setText(f"Error loading dispensing log: {str(e)}")

    def clear_dispensing_log(self):
        """Clear the dispensing log after confirmation"""
        reply = QMessageBox.question(
            self, "Clear Log",
            "Are you sure you want to clear the dispensing log? This action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            try:
                if os.path.exists(DISPENSING_LOG_FILE):
                    os.remove(DISPENSING_LOG_FILE)
                self.log_display.setText("Dispensing log cleared.")
                QMessageBox.information(self, "Success", "Dispensing log cleared successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error clearing log: {str(e)}")

    # -------- Tank Settings --------
    def open_tank_settings(self):
        # Stop any running processes first
        self.stop_process()

        self.tank_widget = QWidget()
        layout = QVBoxLayout(self.tank_widget)

        self.tank_table = QTableWidget()
        self.tank_table.setColumnCount(2)
        self.tank_table.setHorizontalHeaderLabels(["Tank Name", "Contents"])
        self.tank_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.tank_table)

        self.load_tanks()

        btn_layout = QHBoxLayout()
        btn_add = QPushButton("Add Tank")
        btn_remove = QPushButton("Remove Tank")
        btn_save = QPushButton("Save Changes")
        btn_back = QPushButton("Back")

        btn_add.clicked.connect(self.add_tank)
        btn_remove.clicked.connect(self.remove_tank)
        btn_save.clicked.connect(self.save_tanks)
        btn_back.clicked.connect(self.go_back_to_main)

        for btn in (btn_add, btn_remove, btn_save, btn_back):
            btn_layout.addWidget(btn)

        layout.addLayout(btn_layout)
        self.setCentralWidget(self.tank_widget)

    def load_tanks(self):
        self.tank_table.setRowCount(0)
        try:
            if os.path.exists(TANKS_FILE):
                with open(TANKS_FILE, "r") as f:
                    tanks_data = json.load(f)
            else:
                tanks_data = [{"name": f"Tank {i + 1}", "contents": ""} for i in range(self.NUM_TANKS)]
                with open(TANKS_FILE, "w") as f:
                    json.dump(tanks_data, f, indent=2)

            for i, tank in enumerate(tanks_data):
                self.tank_table.insertRow(i)
                self.tank_table.setItem(i, 0, QTableWidgetItem(tank["name"]))
                self.tank_table.setItem(i, 1, QTableWidgetItem(tank["contents"]))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error loading tanks: {str(e)}")

    def add_tank(self):
        name, ok = QInputDialog.getText(self, "Add Tank", "Enter tank name:")
        if ok and name:
            row = self.tank_table.rowCount()
            self.tank_table.insertRow(row)
            self.tank_table.setItem(row, 0, QTableWidgetItem(name))
            self.tank_table.setItem(row, 1, QTableWidgetItem(""))

    def remove_tank(self):
        row = self.tank_table.currentRow()
        if row >= 0:
            self.tank_table.removeRow(row)

    def save_tanks(self):
        try:
            tanks_data = []
            for row in range(self.tank_table.rowCount()):
                name_item = self.tank_table.item(row, 0)
                contents_item = self.tank_table.item(row, 1)
                name = name_item.text() if name_item else ""
                contents = contents_item.text() if contents_item else ""
                tanks_data.append({"name": name, "contents": contents})

            with open(TANKS_FILE, "w") as f:
                json.dump(tanks_data, f, indent=2)

            # Reload tank data for production use
            self.load_tanks_data()

            QMessageBox.information(self, "Success", "Tanks saved successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error saving tanks: {str(e)}")

    # -------- Batch Production --------
    def open_batch_production(self):
        # Stop any running processes first
        self.stop_process()

        self.batch_widget = QWidget()
        layout = QVBoxLayout(self.batch_widget)

        self.stirrer_table = QTableWidget()
        self.stirrer_table.setColumnCount(3)
        self.stirrer_table.setHorizontalHeaderLabels(["Select", "Product", "Amount"])
        # only NUM_STIRRERS stirrers now
        self.stirrer_table.setRowCount(self.NUM_STIRRERS)

        try:
            products = [f[:-5] for f in os.listdir(PRODUCTS_DIR) if f.endswith(".json")]
            if not products:
                products = ["No products available"]
        except Exception:
            products = ["Error loading products"]

        for row in range(self.NUM_STIRRERS):
            chk = QCheckBox(f"Stirrer {row + 1}")
            self.stirrer_table.setCellWidget(row, 0, chk)

            combo = QComboBox()
            combo.addItems(products)
            self.stirrer_table.setCellWidget(row, 1, combo)

            spin = QSpinBox()
            spin.setRange(0, 1000)
            self.stirrer_table.setCellWidget(row, 2, spin)

        self.stirrer_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.stirrer_table)

        btn_layout = QHBoxLayout()
        btn_start = QPushButton("Start Production")
        btn_start.clicked.connect(self.start_production)
        btn_back = QPushButton("Back")
        btn_back.clicked.connect(self.go_back_to_main)

        btn_layout.addWidget(btn_start)
        btn_layout.addWidget(btn_back)
        layout.addLayout(btn_layout)

        self.setCentralWidget(self.batch_widget)

    def start_production(self):
        try:
            # Clear previous process data
            self.stirrer_processes = {}

            # Reload tank data to ensure we have the latest information
            self.load_tanks_data()

            # Initialize dispensing log for new production run
            initialize_dispensing_log()

            for row in range(self.stirrer_table.rowCount()):
                chk = self.stirrer_table.cellWidget(row, 0)
                if chk and chk.isChecked():
                    combo = self.stirrer_table.cellWidget(row, 1)
                    spin = self.stirrer_table.cellWidget(row, 2)

                    if combo and spin:
                        product = combo.currentText()
                        total_amount = spin.value()

                        if total_amount > 0 and product not in ["No products available", "Error loading products"]:
                            product_file = os.path.join(PRODUCTS_DIR, f"{product}.json")
                            if os.path.exists(product_file):
                                with open(product_file, "r") as f:
                                    recipe = json.load(f)

                                steps = []
                                for step_idx, mat in enumerate(recipe):
                                    raw = mat.get("raw_material")
                                    perc = mat.get("percentage", 0)
                                    tank_number = mat.get("tank",
                                                          step_idx + 1)  # Use tank from recipe or default to step index + 1
                                    req_weight = (perc / 100.0) * total_amount

                                    if req_weight > 0:
                                        # Use the tank number from recipe, not stirrer row
                                        tank_key = f"tank{tank_number}"
                                        tank_info = self.tanks_data.get(tank_key,
                                                                        {"name": f"Tank {tank_number}", "contents": ""})

                                        steps.append({
                                            "tank": tank_key,
                                            "tank_number": tank_number,
                                            "tank_name": tank_info["name"],
                                            "tank_contents": tank_info["contents"],
                                            "chemical": raw,
                                            "target_weight": req_weight,
                                            "dispensed_weight": 0.0,
                                            "start_weight": 0.0,
                                            "status": "Waiting",
                                            "percentage": perc
                                        })

                                if steps:
                                    self.stirrer_processes[row + 1] = {
                                        "stirrer_name": f"Stirrer {row + 1}",
                                        "product": product,
                                        "total_amount": total_amount,
                                        "steps": steps,
                                        "current_step": 0,
                                        "status": "Active"
                                    }

            if self.stirrer_processes:
                # Save process data
                process_data = {}
                step_id = 1
                for stirrer_num, proc_data in self.stirrer_processes.items():
                    for step in proc_data["steps"]:
                        process_data[f"step{step_id}"] = {
                            "tank": step["tank"],
                            "tank_name": step["tank_name"],
                            "tank_number": step["tank_number"],
                            "chemical": step["chemical"],
                            "weight": f"{step['target_weight']:.3f}kg",
                            "stirrer": proc_data["stirrer_name"]
                        }
                        step_id += 1

                with open(PROCESS_FILE, "w") as f:
                    json.dump(process_data, f, indent=4)

                try:
                    self.ser = configure_serial()
                except Exception as e:
                    QMessageBox.critical(self, "Serial Error", f"Serial connection failed: {str(e)}")
                    return

                # Open production monitor
                self.open_production_monitor()
                self.update_timer.start(200)

            else:
                QMessageBox.information(self, "No Selection", "No valid stirrers selected for production.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error starting production: {str(e)}")

    # -------- Production Monitor --------
    def open_production_monitor(self):
        """Create and show the production monitoring interface"""
        self.production_monitor_widget = QWidget()
        main_layout = QVBoxLayout(self.production_monitor_widget)

        # Title
        title = QLabel("Production Monitor")
        title.setFont(QFont("Arial", 16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title)

        # Scroll area for stirrer tables
        scroll_area = QScrollArea()
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)

        self.stirrer_tables = {}
        self.stirrer_labels = {}

        # Create individual tables for each active stirrer
        for stirrer_num, proc_data in self.stirrer_processes.items():
            stirrer_frame = QFrame()
            stirrer_frame.setFrameStyle(QFrame.Box)
            stirrer_frame.setLineWidth(2)
            frame_layout = QVBoxLayout(stirrer_frame)

            header_label = QLabel(
                f"{proc_data['stirrer_name']} - {proc_data['product']} ({proc_data['total_amount']} units)"
            )
            header_label.setFont(QFont("Arial", 12, QFont.Bold))
            header_label.setStyleSheet("background-color: #3498DB; color: white; padding: 8px; border-radius: 4px;")
            frame_layout.addWidget(header_label)

            status_label = QLabel("Status: Initializing...")
            status_label.setFont(QFont("Arial", 10))
            self.stirrer_labels[stirrer_num] = status_label
            frame_layout.addWidget(status_label)

            table = QTableWidget()
            table.setColumnCount(8)
            table.setHorizontalHeaderLabels([
                "Step", "Tank #", "Source Tank", "Chemical", "Target (kg)", "Dispensed (kg)", "Progress (%)", "Status"
            ])
            table.setRowCount(len(proc_data["steps"]))
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            table.setMaximumHeight(200)

            for step_idx, step in enumerate(proc_data["steps"]):
                table.setItem(step_idx, 0, QTableWidgetItem(f"Step {step_idx + 1}"))
                table.setItem(step_idx, 1, QTableWidgetItem(str(step["tank_number"])))
                tank_display = step["tank_name"]
                if step["tank_contents"]:
                    tank_display += f" ({step['tank_contents']})"
                table.setItem(step_idx, 2, QTableWidgetItem(tank_display))
                table.setItem(step_idx, 3, QTableWidgetItem(step["chemical"]))
                table.setItem(step_idx, 4, QTableWidgetItem(f"{step['target_weight']:.3f}"))
                table.setItem(step_idx, 5, QTableWidgetItem("0.000"))
                table.setItem(step_idx, 6, QTableWidgetItem("0.0%"))
                table.setItem(step_idx, 7, QTableWidgetItem("Waiting"))

            self.stirrer_tables[stirrer_num] = table
            frame_layout.addWidget(table)
            scroll_layout.addWidget(stirrer_frame)

        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        main_layout.addWidget(scroll_area)

        # Control buttons
        btn_layout = QHBoxLayout()

        self.btn_pause = QPushButton("Pause Production")
        self.btn_pause.setStyleSheet("background-color: #F39C12; color: white; padding: 8px;")
        self.btn_pause.clicked.connect(self.toggle_pause)

        btn_stop = QPushButton("Stop Production")
        btn_stop.setStyleSheet("background-color: #E74C3C; color: white; padding: 8px;")
        btn_stop.clicked.connect(self.stop_production)

        btn_back = QPushButton("Back to Main")
        btn_back.clicked.connect(self.go_back_to_main)

        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(btn_stop)
        btn_layout.addWidget(btn_back)
        main_layout.addLayout(btn_layout)

        self.setCentralWidget(self.production_monitor_widget)

    def toggle_pause(self):
        """Pause or resume production"""
        try:
            if self.update_timer.isActive():
                # Pausing: stop timer and turn off active GPIO outputs, record which were active
                self.update_timer.stop()
                self.btn_pause.setText("Resume Production")
                self.btn_pause.setStyleSheet("background-color: #2ECC71; color: white; padding: 8px;")
                # remember active outputs and turn them off
                self.paused_outputs = set(self.active_outputs)
                for key in list(self.paused_outputs):
                    # key is like 'tankX_stirrerY'
                    try:
                        parts = key.replace("tank", "").split("_stirrer")
                        t = int(parts[0])
                        s = int(parts[1])
                        self.set_output(t, s, False)
                    except Exception:
                        pass
                for lbl in self.stirrer_labels.values():
                    # append paused note, but avoid adding duplicate '| Paused'
                    if "| Paused" not in lbl.text():
                        lbl.setText(lbl.text() + " | Paused")
            else:
                # Resuming: start timer and re-enable outputs that were paused for steps still dispensing
                self.update_timer.start(200)
                self.btn_pause.setText("Pause Production")
                self.btn_pause.setStyleSheet("background-color: #F39C12; color: white; padding: 8px;")
                # Re-enable GPIOs for steps that are still 'Dispensing'
                for key in list(self.paused_outputs):
                    try:
                        parts = key.replace("tank", "").split("_stirrer")
                        t = int(parts[0])
                        s = int(parts[1])
                        # only re-enable if that stirrer's current step is still dispensing and uses that tank
                        proc = self.stirrer_processes.get(s)
                        if proc:
                            idx = proc.get("current_step", 0)
                            if idx < len(proc["steps"]):
                                cur = proc["steps"][idx]
                                if cur["tank_number"] == t and cur["status"] == "Dispensing":
                                    self.set_output(t, s, True)
                    except Exception:
                        pass
                self.paused_outputs.clear()
                # remove paused note
                for lbl in self.stirrer_labels.values():
                    text = lbl.text()
                    lbl.setText(text.replace(" | Paused", ""))
        except Exception as e:
            print(f"Error toggling pause: {e}")

    def update_process(self):
        """Update the production process and GUI displays - now sequential stirrers"""
        try:
            # If we have no stirrers, stop
            if not self.stirrer_processes:
                self.stop_process()
                return

            # Determine which stirrer is currently active
            active_stirrers = sorted(self.stirrer_processes.keys())
            if not hasattr(self, "current_stirrer_index"):
                self.current_stirrer_index = 0

            if self.current_stirrer_index >= len(active_stirrers):
                # All stirrers are complete
                self.stop_process()
                QMessageBox.information(self, "Production Complete",
                                        "All stirrers completed successfully!")
                return

            current_stirrer_num = active_stirrers[self.current_stirrer_index]
            proc_data = self.stirrer_processes[current_stirrer_num]

            # Skip if already complete and move to next stirrer
            if proc_data["status"] == "Complete":
                self.current_stirrer_index += 1
                return

            steps = proc_data["steps"]
            current_step_idx = proc_data["current_step"]

            if current_step_idx >= len(steps):
                proc_data["status"] = "Complete"
                self.stirrer_labels[current_stirrer_num].setText("Status: Production Complete ✓")
                self.stirrer_labels[current_stirrer_num].setStyleSheet("color: green; font-weight: bold;")
                self.current_stirrer_index += 1  # Move to next stirrer on next cycle
                return

            current_step = steps[current_step_idx]

            # Get tank number → channel
            tank_number = current_step["tank_number"]
            channel = chr(ord('A') + (tank_number - 1))

            current_weight = read_weight(self.ser, channel)
            if current_weight is None:
                return

            if current_step["start_weight"] == 0:
                current_step["start_weight"] = current_weight
                current_step["status"] = "Dispensing"
                # Turn on the gpio for this tank/stirrer (if mapped)
                try:
                    self.set_output(tank_number, current_stirrer_num, True)
                except Exception as e:
                    print(f"Error setting GPIO ON for tank{tank_number}_stirrer{current_stirrer_num}: {e}")

            dispensed = current_step["start_weight"] - current_weight
            current_step["dispensed_weight"] = max(0, dispensed)

            target = current_step["target_weight"]
            progress = min(100.0, (dispensed / target) * 100) if target > 0 else 0

            # Log update
            update_dispensing_log(
                current_stirrer_num,
                current_step_idx + 1,
                current_step["chemical"],
                target,
                dispensed,
                current_step["status"],
                current_step["tank_name"],
                current_step["tank_contents"],
                tank_number
            )

            table = self.stirrer_tables[current_stirrer_num]
            table.setItem(current_step_idx, 5, QTableWidgetItem(f"{dispensed:.3f}"))
            table.setItem(current_step_idx, 6, QTableWidgetItem(f"{progress:.1f}%"))
            table.setItem(current_step_idx, 7, QTableWidgetItem(current_step["status"]))

            if progress >= 100:
                # Step complete: mark and turn off gpio
                current_step["status"] = "Complete"
                table.setItem(current_step_idx, 7, QTableWidgetItem("Complete"))
                table.item(current_step_idx, 7).setBackground(Qt.green)
                try:
                    self.set_output(tank_number, current_stirrer_num, False)
                except Exception as e:
                    print(f"Error setting GPIO OFF for tank{tank_number}_stirrer{current_stirrer_num}: {e}")

                update_dispensing_log(
                    current_stirrer_num,
                    current_step_idx + 1,
                    current_step["chemical"],
                    target,
                    dispensed,
                    "Complete",
                    current_step["tank_name"],
                    current_step["tank_contents"],
                    tank_number
                )

                proc_data["current_step"] += 1
                if proc_data["current_step"] < len(steps):
                    steps[proc_data["current_step"]]["start_weight"] = 0
                else:
                    proc_data["status"] = "Complete"
                    self.stirrer_labels[current_stirrer_num].setText("Status: Production Complete ✓")
                    self.stirrer_labels[current_stirrer_num].setStyleSheet("color: green; font-weight: bold;")
                    self.current_stirrer_index += 1  # Move to next stirrer after last step completes
            else:
                # update label with progress; leave GPIO on while dispensing
                if progress > 0:
                    table.item(current_step_idx, 6).setBackground(Qt.yellow)
                    table.item(current_step_idx, 7).setBackground(Qt.yellow)

                self.stirrer_labels[current_stirrer_num].setText(
                    f"Status: Step {current_step_idx + 1}/{len(steps)} - "
                    f"{current_step['chemical']} ({progress:.1f}%) "
                    f"from Tank {tank_number} ({current_step['tank_name']})"
                )

        except Exception as e:
            print(f"Error in update_process: {str(e)}")
            self.stop_process()

    def stop_production(self):
        """Stop production and return to batch setup"""
        self.stop_process()
        QMessageBox.information(self, "Production Stopped", "Production has been stopped.")
        self.open_batch_production()

    # -------- Product Configurations --------
    def open_product_configurations(self):
        # Stop any running processes first
        self.stop_process()

        self.product_widget = QWidget()
        layout = QVBoxLayout(self.product_widget)

        # Title
        title = QLabel("Product Configuration")
        title.setFont(QFont("Arial", 14, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.product_table = QTableWidget()
        self.product_table.setColumnCount(4)
        self.product_table.setHorizontalHeaderLabels(["Raw Material", "Percentage", "Tank Number", "Type"])
        # default rows: 10
        self.product_table.setRowCount(10)
        self.product_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        for row in range(10):
            # Percentage spinbox
            perc_spin = QDoubleSpinBox()
            perc_spin.setRange(0.0, 100.0)
            perc_spin.setDecimals(2)
            self.product_table.setCellWidget(row, 1, perc_spin)

            # Tank number spinbox - allow up to NUM_TANKS
            tank_spin = QSpinBox()
            tank_spin.setRange(1, self.NUM_TANKS)
            tank_spin.setValue(row + 1 if row + 1 <= self.NUM_TANKS else 1)  # Default to sequential tank numbers
            self.product_table.setCellWidget(row, 2, tank_spin)

        layout.addWidget(self.product_table)

        btn_layout = QHBoxLayout()
        btn_save = QPushButton("Save Product")
        btn_save.clicked.connect(self.save_product)

        btn_load = QPushButton("Load Product")
        btn_load.clicked.connect(self.load_product)

        btn_add_line = QPushButton("Add Line")
        btn_add_line.clicked.connect(self.add_product_line)

        btn_delete_line = QPushButton("Delete Line")
        btn_delete_line.clicked.connect(self.delete_product_line)

        btn_back = QPushButton("Back")
        btn_back.clicked.connect(self.go_back_to_main)

        btn_layout.addWidget(btn_save)
        btn_layout.addWidget(btn_load)
        btn_layout.addWidget(btn_add_line)
        btn_layout.addWidget(btn_delete_line)
        btn_layout.addWidget(btn_back)
        layout.addLayout(btn_layout)

        self.setCentralWidget(self.product_widget)

    def add_product_line(self):
        """Insert a new empty row at the end with proper widgets"""
        row = self.product_table.rowCount()
        self.product_table.insertRow(row)

        # Raw material and type columns start empty
        self.product_table.setItem(row, 0, QTableWidgetItem(""))

        # Percentage spinbox
        perc_spin = QDoubleSpinBox()
        perc_spin.setRange(0.0, 100.0)
        perc_spin.setDecimals(2)
        self.product_table.setCellWidget(row, 1, perc_spin)

        # Tank number spinbox
        tank_spin = QSpinBox()
        tank_spin.setRange(1, self.NUM_TANKS)
        tank_spin.setValue(row + 1 if row + 1 <= self.NUM_TANKS else 1)
        self.product_table.setCellWidget(row, 2, tank_spin)

        # Type column
        self.product_table.setItem(row, 3, QTableWidgetItem(""))

    def delete_product_line(self):
        """Delete the currently selected row, or the last row if none selected"""
        row = self.product_table.currentRow()
        if row < 0:
            row = self.product_table.rowCount() - 1
        if row >= 0:
            self.product_table.removeRow(row)
        else:
            QMessageBox.information(self, "No Rows", "There are no rows to delete.")

    def load_product(self):
        """Load an existing product configuration"""
        try:
            products = [f[:-5] for f in os.listdir(PRODUCTS_DIR) if f.endswith(".json")]
            if not products:
                QMessageBox.information(self, "No Products", "No saved products found.")
                return

            product, ok = QInputDialog.getItem(self, "Load Product", "Select product to load:", products, 0, False)
            if ok and product:
                product_file = os.path.join(PRODUCTS_DIR, f"{product}.json")
                with open(product_file, "r") as f:
                    recipe = json.load(f)

                # Ensure table has enough rows for the recipe
                needed_rows = len(recipe)
                if self.product_table.rowCount() < needed_rows:
                    # add missing rows
                    for _ in range(needed_rows - self.product_table.rowCount()):
                        self.add_product_line()

                # Clear table first
                for row in range(self.product_table.rowCount()):
                    self.product_table.setItem(row, 0, QTableWidgetItem(""))

                    perc_widget = self.product_table.cellWidget(row, 1)
                    if perc_widget:
                        perc_widget.setValue(0.0)

                    tank_widget = self.product_table.cellWidget(row, 2)
                    if tank_widget:
                        tank_widget.setValue(1)

                    self.product_table.setItem(row, 3, QTableWidgetItem(""))

                # Load recipe data
                for i, mat in enumerate(recipe):
                    # Ensure row exists
                    if i >= self.product_table.rowCount():
                        self.add_product_line()

                    self.product_table.setItem(i, 0, QTableWidgetItem(mat.get("raw_material", "")))

                    perc_widget = self.product_table.cellWidget(i, 1)
                    if perc_widget:
                        perc_widget.setValue(mat.get("percentage", 0.0))

                    tank_widget = self.product_table.cellWidget(i, 2)
                    if tank_widget:
                        tval = mat.get("tank", i + 1)
                        if isinstance(tval, int) and 1 <= tval <= self.NUM_TANKS:
                            tank_widget.setValue(tval)
                        else:
                            tank_widget.setValue(1)

                    self.product_table.setItem(i, 3, QTableWidgetItem(mat.get("type", "")))

                QMessageBox.information(self, "Success", f"Product '{product}' loaded successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error loading product: {str(e)}")

    def save_product(self):
        name, ok = QInputDialog.getText(self, "Save Product", "Enter product name:")
        if not ok or not name:
            return

        try:
            data = []
            total_percentage = 0.0

            for row in range(self.product_table.rowCount()):
                raw_item = self.product_table.item(row, 0)
                perc_widget = self.product_table.cellWidget(row, 1)
                tank_widget = self.product_table.cellWidget(row, 2)
                type_item = self.product_table.item(row, 3)

                if raw_item and raw_item.text().strip():
                    raw_material = raw_item.text().strip()
                    perc = perc_widget.value() if isinstance(perc_widget, QDoubleSpinBox) else 0.0
                    tank = tank_widget.value() if isinstance(tank_widget, QSpinBox) else 1
                    # clamp tank to valid range
                    if tank < 1 or tank > self.NUM_TANKS:
                        tank = 1
                    type_val = type_item.text().strip() if type_item else ""

                    if perc > 0:
                        data.append({
                            "raw_material": raw_material,
                            "percentage": perc,
                            "tank": tank,
                            "type": type_val
                        })
                        total_percentage += perc

            if data:
                if abs(total_percentage - 100.0) > 0.1:
                    reply = QMessageBox.question(
                        self, "Percentage Warning",
                        f"Total percentage is {total_percentage:.1f}%, not 100%. Save anyway?",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    if reply == QMessageBox.No:
                        return

                with open(os.path.join(PRODUCTS_DIR, f"{name}.json"), "w") as f:
                    json.dump(data, f, indent=2)
                QMessageBox.information(self, "Success", f"Product '{name}' saved successfully!")
            else:
                QMessageBox.warning(self, "Warning", "No valid product data to save!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error saving product: {str(e)}")

    # -------- Stop Process & Back --------
    def stop_process(self):
        """Safely stop all running processes"""
        try:
            if self.update_timer.isActive():
                self.update_timer.stop()
        except Exception:
            pass

        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass

        self.ser = None

        # Turn off any GPIO outputs that may be on
        try:
            self.cleanup_gpio()
        except Exception:
            pass

        # Clear process data
        self.stirrer_processes = {}
        self.stirrer_tables = {}
        self.stirrer_labels = {}

    def go_back_to_main(self):
        """Safely return to main menu"""
        # Stop any running processes first
        self.stop_process()

        # Clear current widget references
        self.batch_widget = None
        self.product_widget = None
        self.tank_widget = None
        self.production_monitor_widget = None
        self.dispensing_log_widget = None

        # Recreate and show main menu
        self.show_main_menu()

    def closeEvent(self, event):
        """Handle application close"""
        # cleanup gpio on exit
        try:
            self.cleanup_gpio()
        except Exception:
            pass
        self.stop_process()
        event.accept()


# ---------------- MAIN ----------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
