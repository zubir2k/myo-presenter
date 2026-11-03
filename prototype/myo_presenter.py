"""
Myo Presenter - standalone, no-admin presenter for Windows 11.

Connects to a Thalmic Labs Myo armband in one of two ways, picked automatically:
  1. The Myo's blue USB dongle (BLED112, shows up as a plain COM port), if plugged in.
  2. Otherwise the PC's built-in Bluetooth LE (Windows' own stack, no pairing needed).
Force one with:  MyoPresenter.exe --dongle   or   MyoPresenter.exe --ble

Runs in the system tray: click the icon for gesture tips, right-click to exit.
Log file: %LOCALAPPDATA%\\MyoPresenter\\log.txt

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
import os
import threading
import re
import struct
import sys
import time
from ctypes import wintypes

from pyomyo import Arm, Myo, Pose, XDirection, emg_mode

VERSION = "0.3.0"

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
        self.status = lambda text, notify=False: print(text)   # set by the tray
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
        if arm != Arm.UNKNOWN:
            self.status("Synced on %s arm - double tap to arm" % arm.name.lower(), notify=True)

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
            self.status("Armed - gestures active" if self.armed else "Disarmed", notify=True)
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
            self.armed = False
            self.status("Unsynced - do the sync gesture (wave out)", notify=True)
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
    presenter.status("Connecting via USB dongle (%s)..." % port)
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
    presenter.status("Connected via USB dongle - wave out to sync", notify=True)
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

    while True:
        presenter.status("Searching for Myo via Bluetooth...")
        try:
            dev = await _find_myo_ble()
        except Exception as e:          # Bluetooth off / no adapter
            presenter.status("Bluetooth unavailable: %s" % e)
            await asyncio.sleep(5)
            continue
        if dev is None:
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
                presenter.status("Connected via Bluetooth - wave out to sync", notify=True)
                await gone.wait()
                presenter.armed = False
                presenter.status("Myo disconnected - reconnecting...", notify=True)
        except Exception as e:  # connection hiccups are common on shared radios
            print("Bluetooth error:", e, "- retrying in 2 s")
            await asyncio.sleep(2)


# ---------- tray app ----------
APP_NAME = "Myo Presenter"
TIPS = (
    "Double tap  -  arm / disarm (safety)\n"
    "Wave out  -  next slide\n"
    "Wave in  -  previous slide\n"
    "Fingers spread  -  laser pointer on / off\n"
    "     (rotate your forearm to move it)\n"
    "Fist  -  black screen on / off\n\n"
    "First time on: wave out once to sync the band.\n"
    "Right-click the tray icon to exit."
)


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def setup_logging():
    """A windowed exe has no console, so keep a log for troubleshooting."""
    if sys.stdout is not None:
        return
    folder = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MyoPresenter")
    os.makedirs(folder, exist_ok=True)
    f = open(os.path.join(folder, "log.txt"), "w", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = f


def already_running():
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, "Local\\MyoPresenterSingleInstance")
    return ctypes.get_last_error() == 183          # ERROR_ALREADY_EXISTS


def message_box(text, title=APP_NAME):
    # non-blocking so the tray keeps responding
    threading.Thread(target=lambda: user32.MessageBoxW(None, text, title, 0x40 | 0x40000),
                     daemon=True).start()


def run_backend(presenter, args):
    try:
        port = None if args.ble else find_dongle()
        if port:
            run_dongle(presenter, port)
        elif args.dongle:
            presenter.status("Myo dongle not found - plug it in and restart", notify=True)
        else:
            asyncio.run(run_ble(presenter))
    except Exception as e:
        presenter.status("Stopped: %s" % e, notify=True)
        raise


def selftest():
    """Used by the build to prove everything is bundled."""
    import serial.tools.list_ports  # noqa: F401
    import bleak  # noqa: F401
    import pystray  # noqa: F401
    from PIL import Image
    Image.open(resource("myo.png")).load()
    if sys.platform == "win32":
        import bleak.backends.winrt.client  # noqa: F401
        import bleak.backends.winrt.scanner  # noqa: F401
    print("selftest ok", VERSION)


def main():
    setup_logging()
    ap = argparse.ArgumentParser(description="Myo armband presenter")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dongle", action="store_true", help="only use the blue USB dongle")
    g.add_argument("--ble", action="store_true", help="only use the PC's built-in Bluetooth")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.selftest:
        try:
            selftest()
        except Exception:
            import traceback
            traceback.print_exc()
            sys.exit(1)
        return
    if already_running():
        message_box("Myo Presenter is already running - look for it in the system tray.")
        time.sleep(5)
        return

    import pystray
    from PIL import Image

    presenter = Presenter()
    state = {"text": "Starting..."}

    def show_tips(icon=None, item=None):
        message_box("Status: %s\n\n%s" % (state["text"], TIPS), APP_NAME + " " + VERSION)

    def quit_app(icon, item):
        icon.visible = False
        icon.stop()
        os._exit(0)                  # also ends the Bluetooth / serial thread

    icon = pystray.Icon(
        "MyoPresenter",
        Image.open(resource("myo.png")),
        APP_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("Gesture tips", show_tips, default=True),   # left-click
            pystray.MenuItem(lambda item: state["text"], None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", quit_app),
        ),
    )

    def status(text, notify=False):
        print(time.strftime("%H:%M:%S"), text)
        state["text"] = text
        try:
            icon.title = (APP_NAME + " - " + text)[:127]
            icon.update_menu()
            if notify:
                icon.notify(text, APP_NAME)
        except Exception:
            pass

    presenter.status = status

    def on_ready(icon):
        icon.visible = True
        threading.Thread(target=run_backend, args=(presenter, args), daemon=True).start()
        show_tips()                  # quick reminder on launch

    icon.run(setup=on_ready)


if __name__ == "__main__":
    main()
