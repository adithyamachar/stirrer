# mock_gpio.py

class GPIO:
    BCM = "BCM"
    OUT = "OUT"
    HIGH = 1
    LOW = 0

    @staticmethod
    def setmode(mode):
        print(f"[MOCK GPIO] Mode set to {mode}")

    @staticmethod
    def setup(pin, mode):
        print(f"[MOCK GPIO] Pin {pin} set as {mode}")

    @staticmethod
    def output(pin, state):
        if state == GPIO.HIGH:
            print(f"[MOCK GPIO] Pin {pin} set to HIGH (LED ON)")
        else:
            print(f"[MOCK GPIO] Pin {pin} set to LOW (LED OFF)")

    @staticmethod
    def cleanup():
        print("[MOCK GPIO] Cleanup called")
