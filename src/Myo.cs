using System;
using System.Threading;
using System.Threading.Tasks;

namespace MyoPresenter
{
    public enum Pose { Rest = 0, Fist = 1, WaveIn = 2, WaveOut = 3, FingersSpread = 4, DoubleTap = 5, Unknown = 0xFFFF }
    public enum Arm { Unknown = 0, Right = 1, Left = 2 }
    public enum XDirection { Unknown = 0, TowardWrist = 1, TowardElbow = 2 }

    /// <summary>Myo protocol constants (from Thalmic's published myohw.h).</summary>
    public static class MyoHw
    {
        public static Guid Uuid(ushort shortId) => new Guid($"d506{shortId:x4}-a904-deb9-4748-2c7f4a124842");

        public static readonly Guid ControlService = Uuid(0x0001);
        public static readonly Guid CommandChar = Uuid(0x0401);
        public static readonly Guid ImuService = Uuid(0x0002);
        public static readonly Guid ImuChar = Uuid(0x0402);
        public static readonly Guid ClassifierService = Uuid(0x0003);
        public static readonly Guid ClassifierChar = Uuid(0x0103);

        public static readonly byte[] SetMode = { 0x01, 0x03, 0x00, 0x01, 0x01 };   // EMG off, IMU on, classifier on
        public static readonly byte[] NeverSleep = { 0x09, 0x01, 0x01 };
        public static readonly byte[] UnlockHold = { 0x0a, 0x01, 0x02 };
        public static readonly byte[] VibrateShort = { 0x03, 0x01, 0x01 };
        public static readonly byte[] LedsBlue = { 0x06, 0x06, 0, 128, 255, 0, 128, 255 };
    }

    /// <summary>A connection to the armband (USB dongle or built-in Bluetooth).</summary>
    public abstract class MyoLink
    {
        public event Action<Pose> PoseChanged;
        public event Action<Arm, XDirection> ArmChanged;
        public event Action<short[]> Imu;          // quat[4], acc[3], gyro[3]
        public event Action<string, bool> Status;  // text, show notification

        public abstract string Name { get; }

        /// <summary>Connect and keep reconnecting until cancelled.</summary>
        public abstract Task RunAsync(CancellationToken ct);

        /// <summary>Send a command to the Myo without blocking the caller.</summary>
        public abstract void SendCommand(byte[] command);

        public void Vibrate() => SendCommand(MyoHw.VibrateShort);

        protected void OnStatus(string text, bool notify = false) => Status?.Invoke(text, notify);

        protected void OnImu(byte[] data, int offset)
        {
            if (data.Length - offset < 20) return;
            var v = new short[10];
            for (int i = 0; i < 10; i++) v[i] = BitConverter.ToInt16(data, offset + i * 2);
            Imu?.Invoke(v);
        }

        /// <summary>Classifier event, same layout over dongle or Bluetooth.</summary>
        protected void OnClassifier(byte[] d, int o)
        {
            if (d.Length - o < 1) return;
            switch (d[o])
            {
                case 1 when d.Length - o >= 3:   // synced on arm
                    ArmChanged?.Invoke(ToEnum<Arm>(d[o + 1]), ToEnum<XDirection>(d[o + 2]));
                    break;
                case 2:                           // removed / unsynced
                    ArmChanged?.Invoke(Arm.Unknown, XDirection.Unknown);
                    break;
                case 3 when d.Length - o >= 3:   // pose (uint16)
                    PoseChanged?.Invoke(ToEnum<Pose>(BitConverter.ToUInt16(d, o + 1)));
                    break;
            }
        }

        private static T ToEnum<T>(int v) where T : struct
            => Enum.IsDefined(typeof(T), v) ? (T)Enum.ToObject(typeof(T), v) : (T)Enum.ToObject(typeof(T), typeof(T) == typeof(Pose) ? 0xFFFF : 0);
    }
}
