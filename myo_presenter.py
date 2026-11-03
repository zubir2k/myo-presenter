"""
Myo Presenter - standalone, no-admin presenter for Windows 11.

Connects to a Thalmic Labs Myo armband in one of two ways, picked automatically:
  1. The Myo's blue USB dongle (BLED112, shows up as a plain COM port), if plugged in.
  2. Otherwise the PC's built-in Bluetooth LE (Windows' own stack, no pairing needed).
Force one with:  MyoPresenter.exe --dongle   or   MyoPresenter.exe --ble

Gestures (Myo's own on-board classifier):
  Double tap / thumb-to-pinky : arm or disarm the controller (safety)
  Wave out                    : next slide        (Right arrow)
  Wave in                     : previous slide    (Left arrow)
  Fingers spread              : laser pointer on/off (Ctrl+L / Ctrl+A)
  Fist                        : black screen toggle (B)
While the laser is on, rotate your forearm to move it (gyro -> mouse).
"""
import argparse
import asyncio
import ctypes
import re
import struct
import sys
import time
from ctypes import wintypes

from pyomyo import Arm, Myo, Pose, XDirection, emg_mode

VERSION = "0.2.0"

# ---------- tuning ----------
SENSITIVITY = 0.35      # pixels per (deg/s) per IMU sample
DEADZONE = 4.0          # deg/s ignored, kills hand tremor
INVERT_X = False        # flip if the laser moves the wrong way left/right
INVERT_Y = False        # flip if it moves the wrong way up/down
POSE_COOLDOWN = 0.7     # seconds between accepted gestures

# ---------- Myo commands (myohw.h) ----------
CMD_SET_MODE = b"\x01\x03\x00\x01\x01"   # EMG off, IMU on, classifier on
CMD_NEVER_SLEEP = b"\x09\x01\x01"
CMD_UNLOCK_HOLD = b"\x0a\x01\x02"
CMD_VIBRATE_SHORT = b"\x03\x01\x01"
CMD_LEDS_BLUE = b"\x06\x06" + bytes([0, 128, 255, 0, 128, 255])

# ---------- Win32 input (SendInput, works without admin) ----------
user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
VK = {"right": 0x27, "left": 0x25, "ctrl": 0x11, "l": 0x4C, "a": 0x41, "b": 0x42}
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key(vk, up=False):
    return INPUT(type=INPUT_KEYBOARD,
                 ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0))


def press(*names):
    """Press a key or a combo, e.g. press('ctrl', 'l')."""
    codes = [VK[n] for n in names]
    _send(*[_key(c) for c in codes], *[_key(c, True) for c in reversed(codes)])


def move_mouse(dx, dy):
    _send(INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, 0)))


# ---------- presenter logic (shared by both connection types) ----------
class Presenter:
    def __init__(self):
        self.vibrate = lambda: None      # set by the active backend
        self.armed = False
        self.laser = False
        self.last_pose_time = 0.0
        self.x_dir_sign = 1

    def buzz(self, n=1):
        for i in range(n):
            if i:
                time.sleep(0.25)
            try:
                self.vibrate()
            except Exception:
                pass

    def on_arm(self, arm, xdir):
        # flip vertical axis if the band is worn upside down
        self.x_dir_sign = -1 if xdir == XDirection.X_TOWARD_ELBOW else 1
        print("Arm:", arm.name, xdir.name)

    def on_pose(self, pose):
        now = time.time()
        if pose == Pose.REST or now - self.last_pose_time < POSE_COOLDOWN:
            return
        self.last_pose_time = now
        print("Pose:", pose.name)

        if pose == Pose.THUMB_TO_PINKY:          # double tap on newer firmware
            self.armed = not self.armed
            if not self.armed and self.laser:
                self.toggle_laser()
            print("ARMED" if self.armed else "disarmed")
            self.buzz(2 if self.armed else 1)
            return

        if not self.armed:
            return
        if pose == Pose.WAVE_OUT:
            press("right")
        elif pose == Pose.WAVE_IN:
            press("left")
        elif pose == Pose.FINGERS_SPREAD:
            self.toggle_laser()
        elif pose == Pose.FIST:
            press("b")

    def toggle_laser(self):
        self.laser = not self.laser
        press("ctrl", "l" if self.laser else "a")   # PowerPoint: laser / back to arrow
        print("Laser", "ON" if self.laser else "OFF")
        self.buzz()

    def on_imu(self, quat, acc, gyro):
        if not (self.armed and self.laser):
            return
        # Myo gyro is in units of 1/16 deg/s
        gy, gz = gyro[1] / 16.0, gyro[2] / 16.0
        yaw = 0 if abs(gz) < DEADZONE else gz
        pitch = 0 if abs(gy) < DEADZONE else gy
        dx = -yaw * SENSITIVITY * (-1 if INVERT_X else 1)
        dy = -pitch * SENSITIVITY * self.x_dir_sign * (-1 if INVERT_Y else 1)
        if dx or dy:
            move_mouse(dx, dy)

    def on_classifier(self, data):
        """Raw classifier event (same layout over dongle or built-in BLE)."""
        typ = data[0]
        if typ == 1 and len(data) >= 3:                       # synced on arm
            self.on_arm(_enum(Arm, data[1]), _enum(XDirection, data[2]))
        elif typ == 2:                                        # removed / unsynced
            self.on_arm(Arm.UNKNOWN, XDirection.UNKNOWN)
            print("Band unsynced - do the sync gesture (wave out) again.")
        elif typ == 3 and len(data) >= 3:                     # pose (uint16)
            self.on_pose(_enum(Pose, struct.unpack_from("<H", data, 1)[0]))


def _enum(cls, val):
    try:
        return cls(val)
    except ValueError:
        return cls.UNKNOWN


# ---------- backend 1: the blue USB dongle ----------
def find_dongle():
    from serial.tools.list_ports import comports
    for p in comports():
        if re.search(r"PID=2458:0*1", p[2]):
            return p[0]
    return None


def run_dongle(presenter, port):
    print("Using the Myo USB dongle on", port)
    myo = Myo(tty=port, mode=emg_mode.FILTERED)   # FILTERED keeps the classifier on
    presenter.vibrate = lambda: myo.vibrate(1)
    myo.add_pose_handler(presenter.on_pose)
    myo.add_imu_handler(presenter.on_imu)
    myo.add_arm_handler(presenter.on_arm)
    myo.connect()
    myo.write_attr(0x19, CMD_SET_MODE)
    myo.write_attr(0x19, CMD_UNLOCK_HOLD)
    myo.write_attr(0x19, CMD_LEDS_BLUE)
    myo.vibrate(1)
    ready_message()
    try:
        while True:
            myo.run()
    finally:
        myo.disconnect()


# ---------- backend 2: the PC's built-in Bluetooth LE ----------
def _uuid(short):
    return f"d506{short:04x}-a904-deb9-4748-2c7f4a124842"


CONTROL_SERVICE = _uuid(0x0001)
COMMAND_CHAR = _uuid(0x0401)
IMU_CHAR = _uuid(0x0402)
CLASSIFIER_CHAR = _uuid(0x0103)


async def _find_myo_ble(timeout=10.0):
    from bleak import BleakScanner

    def match(dev, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        return CONTROL_SERVICE in uuids or (dev.name or "").lower().startswith("myo")

    return await BleakScanner.find_device_by_filter(match, timeout=timeout)


async def run_ble(presenter):
    from bleak import BleakClient

    print("No dongle - using this PC's built-in Bluetooth.")
    while True:
        print("Scanning for the Myo (tap it or move your arm to wake it)...")
        dev = await _find_myo_ble()
        if dev is None:
            print("Myo not found, retrying. Is Bluetooth on?")
            continue
        print("Found", dev.name or "Myo", dev.address)
        gone = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            async with BleakClient(dev, disconnected_callback=lambda _c: loop.call_soon_threadsafe(gone.set)) as client:
                async def cmd(payload):
                    await client.write_gatt_char(COMMAND_CHAR, payload, response=True)

                presenter.vibrate = lambda: loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(cmd(CMD_VIBRATE_SHORT)))

                def imu_cb(_char, data):
                    vals = struct.unpack("<10h", bytes(data[:20]))
                    presenter.on_imu(vals[:4], vals[4:7], vals[7:10])

                def cls_cb(_char, data):
                    presenter.on_classifier(bytes(data))

                await cmd(CMD_SET_MODE)
                await cmd(CMD_NEVER_SLEEP)
                await cmd(CMD_UNLOCK_HOLD)
                await cmd(CMD_LEDS_BLUE)
                await client.start_notify(IMU_CHAR, imu_cb)
                await client.start_notify(CLASSIFIER_CHAR, cls_cb)
                await cmd(CMD_VIBRATE_SHORT)
                ready_message()
                await gone.wait()
                print("Myo disconnected - reconnecting...")
        except Exception as e:  # connection hiccups are common on shared radios
            print("Bluetooth error:", e, "- retrying in 2 s")
            await asyncio.sleep(2)


# ---------- entry point ----------
def ready_message():
    print("Connected. Do the sync gesture (wave out) if the logo pulses,")
    print("then double tap to arm. Ctrl+C to quit.")


def selftest():
    """Used by the build to prove both backends are bundled."""
    import serial.tools.list_ports  # noqa: F401
    import bleak  # noqa: F401
    if sys.platform == "win32":
        import bleak.backends.winrt.client  # noqa: F401
        import bleak.backends.winrt.scanner  # noqa: F401
    print("selftest ok", VERSION)


def main():
    ap = argparse.ArgumentParser(description="Myo armband presenter")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dongle", action="store_true", help="only use the blue USB dongle")
    g.add_argument("--ble", action="store_true", help="only use the PC's built-in Bluetooth")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    presenter = Presenter()
    try:
        port = None if args.ble else find_dongle()
        if port:
            run_dongle(presenter, port)
        elif args.dongle:
            print("Myo dongle not found. Plug it in, or run without --dongle.")
            input("Press Enter to exit.")
        else:
            asyncio.run(run_ble(presenter))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
