# Myo Presenter

Turns a Thalmic Labs Myo armband into a PowerPoint presenter and laser pointer on Windows 11.
It runs standalone with no admin rights, using the Myo's blue USB dongle (a plain COM port to Windows) when it's plugged in, or the PC's built-in Bluetooth when it isn't. Force one with `--dongle` or `--ble`.

| Gesture | Action |
|---|---|
| Double tap | Arm / disarm |
| Wave out | Next slide |
| Wave in | Previous slide |
| Fingers spread | Laser pointer on/off (gyro moves it) |
| Fist | Black screen |

It lives in the system tray: click the icon for gesture tips, right-click > Exit to close. Log: `%LOCALAPPDATA%\MyoPresenter\log.txt`.

Download `MyoPresenter.exe` from the latest Actions run (Artifacts).
