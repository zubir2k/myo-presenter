"""
Myo Presenter - standalone, no-admin presenter for Windows 11.

Needs: the Myo's blue USB dongle (shows up as a plain COM port, no driver),
Python 3 (per-user or portable) and `pip install --user pyomyo` (pulls pyserial).

Gestures (Myo's own on-board classifier):
  Double tap / thumb-to-pinky : arm or disarm the controller (safety)
  Wave out                    : next slide        (Right arrow)
  Wave in                     : previous slide    (Left arrow)
  Fingers spread              : laser pointer on/off (Ctrl+L / Ctrl+A)
  Fist                        : black screen toggle (B)
While the laser is on, rotate your forearm to move it (gyro -> mouse).
"""
import ctypes
import time
from ctypes import wintypes

from pyomyo import Myo, Pose, emg_mode

# ---------- tuning ----------
SENSITIVITY = 0.35      # pixels per (deg/s) per IMU sample
DEADZONE = 4.0          # deg/s ignored, kills hand tremor
INVERT_X = False        # flip if the laser moves the wrong way left/right
INVERT_Y = False        # flip if it moves the wrong way up/down
POSE_COOLDOWN = 0.7     # seconds between accepted gestures

# ---------- Win32 input (SendInput, works without admin) ----------
user32 = ctypes.WinDLL("user32", use_last_error=True)
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


# ---------- presenter logic ----------
class Presenter:
    def __init__(self, myo):
        self.myo = myo
        self.armed = False
        self.laser = False
        self.last_pose_time = 0.0
        self.x_dir_sign = 1

    def buzz(self, n=1):
        try:
            self.myo.vibrate(1)          # 1 = short
            if n > 1:
                time.sleep(0.25)
                self.myo.vibrate(1)
        except Exception:
            pass

    def on_arm(self, arm, xdir):
        # flip vertical axis if the band is worn upside down
        self.x_dir_sign = -1 if xdir.name == "X_TOWARD_ELBOW" else 1
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


def main():
    # FILTERED mode is the one that leaves the on-board pose classifier on
    myo = Myo(mode=emg_mode.FILTERED)
    p = Presenter(myo)
    myo.add_pose_handler(p.on_pose)
    myo.add_imu_handler(p.on_imu)
    myo.add_arm_handler(p.on_arm)
    myo.connect()

    # EMG stream off (we only need poses + IMU), IMU on, classifier on
    myo.write_attr(0x19, b"\x01\x03\x00\x01\x01")
    # unlock and stay unlocked
    myo.write_attr(0x19, b"\x0a\x01\x02")
    myo.set_leds([0, 128, 255], [0, 128, 255])
    myo.vibrate(1)

    print("Connected. Do the sync gesture (wave out) if the logo pulses,")
    print("then double tap to arm. Ctrl+C to quit.")
    try:
        while True:
            myo.run()
    except KeyboardInterrupt:
        pass
    finally:
        myo.disconnect()


if __name__ == "__main__":
    main()
