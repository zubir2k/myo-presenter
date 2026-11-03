using System;
using System.Threading.Tasks;

namespace MyoPresenter
{
    /// <summary>Turns Myo gestures into PowerPoint keys and laser-pointer mouse moves.</summary>
    public sealed class Presenter
    {
        // ---------- tuning ----------
        public double Sensitivity = 0.35;     // pixels per (deg/s) per IMU sample
        public double Deadzone = 4.0;         // deg/s ignored, kills hand tremor
        public bool InvertX = false;          // flip if the laser moves the wrong way left/right
        public bool InvertY = false;          // flip if it moves the wrong way up/down
        public TimeSpan PoseCooldown = TimeSpan.FromMilliseconds(700);

        private readonly MyoLink _link;
        private DateTime _lastPose = DateTime.MinValue;
        private int _xDirSign = 1;

        public bool Armed { get; private set; }
        public bool Laser { get; private set; }
        public event Action<string, bool> Status;

        public Presenter(MyoLink link)
        {
            _link = link;
            link.PoseChanged += OnPose;
            link.ArmChanged += OnArm;
            link.Imu += OnImu;
        }

        private void Buzz(int times)
        {
            Task.Run(async () =>
            {
                for (int i = 0; i < times; i++)
                {
                    if (i > 0) await Task.Delay(250);
                    _link.Vibrate();
                }
            });
        }

        private void OnArm(Arm arm, XDirection xdir)
        {
            _xDirSign = xdir == XDirection.TowardElbow ? -1 : 1;
            Log.Write($"Arm: {arm} {xdir}");
            if (arm == Arm.Unknown)
            {
                Armed = false;
                if (Laser) ToggleLaser();
                Status?.Invoke("Unsynced - do the sync gesture (wave out)", true);
            }
            else
            {
                Status?.Invoke($"Synced on {arm.ToString().ToLower()} arm - double tap to arm", true);
            }
        }

        private void OnPose(Pose pose)
        {
            var now = DateTime.UtcNow;
            if (pose == Pose.Rest || pose == Pose.Unknown || now - _lastPose < PoseCooldown) return;
            _lastPose = now;
            Log.Write("Pose: " + pose);

            if (pose == Pose.DoubleTap)
            {
                Armed = !Armed;
                if (!Armed && Laser) ToggleLaser();
                Status?.Invoke(Armed ? "Armed - gestures active" : "Disarmed", true);
                Buzz(Armed ? 2 : 1);
                return;
            }
            if (!Armed) return;

            switch (pose)
            {
                case Pose.WaveOut: Native.Press(Native.VK_RIGHT); break;
                case Pose.WaveIn: Native.Press(Native.VK_LEFT); break;
                case Pose.FingersSpread: ToggleLaser(); break;
                case Pose.Fist: Native.Press(Native.VK_B); break;
            }
        }

        private void ToggleLaser()
        {
            Laser = !Laser;
            Native.Press(Native.VK_CONTROL, Laser ? Native.VK_L : Native.VK_A);   // PowerPoint: laser / back to arrow
            Log.Write("Laser " + (Laser ? "ON" : "OFF"));
            Buzz(1);
        }

        private void OnImu(short[] v)
        {
            if (!Armed || !Laser) return;
            // Myo gyro is in units of 1/16 deg/s: v[7..9] = x, y, z
            double gy = v[8] / 16.0, gz = v[9] / 16.0;
            double yaw = Math.Abs(gz) < Deadzone ? 0 : gz;
            double pitch = Math.Abs(gy) < Deadzone ? 0 : gy;
            int dx = (int)(-yaw * Sensitivity * (InvertX ? -1 : 1));
            int dy = (int)(-pitch * Sensitivity * _xDirSign * (InvertY ? -1 : 1));
            if (dx != 0 || dy != 0) Native.MoveMouse(dx, dy);
        }
    }
}
