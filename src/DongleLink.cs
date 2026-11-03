using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO.Ports;
using System.Linq;
using System.Management;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace MyoPresenter
{
    /// <summary>
    /// The Myo's blue USB dongle (BlueGiga BLED112). Windows sees it as a plain COM port using the
    /// built-in usbser driver, so no driver install or admin rights are needed. We speak BGAPI to it.
    /// </summary>
    public sealed class DongleLink : MyoLink
    {
        // GATT handles on the Myo (firmware 1.x)
        private const ushort H_COMMAND = 0x19, H_IMU = 0x1c, H_IMU_CCCD = 0x1d, H_CLASSIFIER = 0x23, H_CLASSIFIER_CCCD = 0x24;

        // Myo control service UUID, little-endian, as it appears at the end of the advertisement
        private static readonly byte[] AdvTail = { 0x06, 0x42, 0x48, 0x12, 0x4A, 0x7F, 0x2C, 0x48, 0x47, 0xB9, 0xDE, 0x04, 0xA9, 0x01, 0x00, 0x06, 0xD5 };

        private readonly string _portName;
        private SerialPort _port;
        private readonly object _cmdLock = new object();
        private readonly BlockingCollection<Packet> _responses = new BlockingCollection<Packet>();
        private readonly List<Func<Packet, bool>> _waiters = new List<Func<Packet, bool>>();
        private int _conn = -1;
        private volatile bool _connected;
        private TaskCompletionSource<bool> _disconnected;
        private TaskCompletionSource<byte[]> _found;

        public DongleLink(string portName) { _portName = portName; }

        public override string Name => "USB dongle";

        private sealed class Packet
        {
            public byte Type, Cls, Cmd;
            public byte[] Payload;
        }

        /// <summary>Find the dongle's COM port (USB VID 2458, PID 0001). Read-only WMI query, no admin.</summary>
        public static string FindPort()
        {
            try
            {
                using (var s = new ManagementObjectSearcher(
                    "SELECT Name, DeviceID FROM Win32_PnPEntity WHERE DeviceID LIKE 'USB\\\\VID_2458&PID_0001%'"))
                {
                    foreach (ManagementObject o in s.Get())
                    {
                        var m = Regex.Match(o["Name"]?.ToString() ?? "", @"\((COM\d+)\)");
                        if (m.Success) return m.Groups[1].Value;
                    }
                }
            }
            catch (Exception e) { Log.Write("Dongle lookup failed: " + e.Message); }
            return null;
        }

        public override async Task RunAsync(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    Open();
                    OnStatus($"Searching for Myo via USB dongle ({_portName})...");
                    var addr = await Task.Run(() => Scan(ct), ct);
                    ConnectAndSetup(addr);
                    OnStatus("Connected via USB dongle - wave out to sync", true);
                    using (ct.Register(() => _disconnected.TrySetResult(true)))
                        await _disconnected.Task;
                    if (!ct.IsCancellationRequested) OnStatus("Myo disconnected - reconnecting...", true);
                }
                catch (OperationCanceledException) { break; }
                catch (Exception e)
                {
                    Log.Write("Dongle error: " + e);
                    OnStatus("Dongle error: " + e.Message);
                    await Task.Delay(2000, ct).ContinueWith(_ => { });
                }
                finally
                {
                    _connected = false;
                    Close();
                }
            }
        }

        public override void SendCommand(byte[] command)
        {
            if (!_connected) return;
            Task.Run(() =>
            {
                try { WriteAttr(H_COMMAND, command); }
                catch (Exception e) { Log.Write("Command failed: " + e.Message); }
            });
        }

        // ---------- connection steps ----------

        private void Open()
        {
            _port = new SerialPort(_portName, 9600) { DtrEnable = true, RtsEnable = true, ReadTimeout = 500 };
            _port.Open();
            _port.DiscardInBuffer();
            _disconnected = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var reader = new Thread(ReadLoop) { IsBackground = true, Name = "dongle-reader" };
            reader.Start(_port);

            // stop anything left over from a previous session
            Command(6, 4);                     // gap_end_procedure
            for (byte h = 0; h < 3; h++) Command(3, 0, h);  // connection_disconnect
        }

        private void Close()
        {
            try
            {
                if (_conn >= 0 && _port != null && _port.IsOpen) Command(3, 0, (byte)_conn);
            }
            catch { }
            _conn = -1;
            try { _port?.Close(); } catch { }
            _port = null;
            while (_responses.TryTake(out _)) { }
        }

        private byte[] Scan(CancellationToken ct)
        {
            _found = new TaskCompletionSource<byte[]>();
            Command(6, 2, 0x01);               // gap_discover (generic)
            try
            {
                while (!_found.Task.Wait(500))
                    ct.ThrowIfCancellationRequested();
            }
            finally { Command(6, 4); }          // end scan
            return _found.Task.Result;
        }

        private void ConnectAndSetup(byte[] addr)
        {
            // gap_connect_direct: address, address type, conn interval min/max, timeout, latency
            var payload = new List<byte>(addr) { 0 };
            payload.AddRange(BitConverter.GetBytes((ushort)6));
            payload.AddRange(BitConverter.GetBytes((ushort)6));
            payload.AddRange(BitConverter.GetBytes((ushort)64));
            payload.AddRange(BitConverter.GetBytes((ushort)0));
            var resp = Command(6, 3, payload.ToArray());
            _conn = resp.Payload[resp.Payload.Length - 1];
            WaitEvent(3, 0, 10000);            // connection_status

            WriteAttr(H_IMU_CCCD, new byte[] { 0x01, 0x00 });          // IMU notifications
            WriteAttr(H_CLASSIFIER_CCCD, new byte[] { 0x02, 0x00 });   // classifier indications
            WriteAttr(H_COMMAND, MyoHw.SetMode);
            WriteAttr(H_COMMAND, MyoHw.NeverSleep);
            WriteAttr(H_COMMAND, MyoHw.UnlockHold);
            WriteAttr(H_COMMAND, MyoHw.LedsBlue);
            WriteAttr(H_COMMAND, MyoHw.VibrateShort);
            _connected = true;
        }

        // ---------- BGAPI plumbing ----------

        private void WriteAttr(ushort handle, byte[] value)
        {
            var p = new List<byte> { (byte)_conn };
            p.AddRange(BitConverter.GetBytes(handle));
            p.Add((byte)value.Length);
            p.AddRange(value);
            lock (_cmdLock)
            {
                var done = Waiter(4, 1);       // attclient_procedure_completed
                Command(4, 5, p.ToArray());
                done.Wait(3000);
            }
        }

        private Packet Command(byte cls, byte cmd, params byte[] payload)
        {
            lock (_cmdLock)
            {
                var frame = new byte[4 + payload.Length];
                frame[1] = (byte)payload.Length;
                frame[2] = cls;
                frame[3] = cmd;
                Buffer.BlockCopy(payload, 0, frame, 4, payload.Length);
                _port.Write(frame, 0, frame.Length);
                if (!_responses.TryTake(out var r, 3000))
                    throw new TimeoutException($"No response from dongle to command {cls}/{cmd}");
                return r;
            }
        }

        private Task Waiter(byte cls, byte cmd)
        {
            var tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            lock (_waiters)
                _waiters.Add(p =>
                {
                    if (p.Cls != cls || p.Cmd != cmd) return false;
                    tcs.TrySetResult(true);
                    return true;
                });
            return tcs.Task;
        }

        private void WaitEvent(byte cls, byte cmd, int timeoutMs)
        {
            if (!Waiter(cls, cmd).Wait(timeoutMs))
                throw new TimeoutException("Myo did not respond");
        }

        private void ReadLoop(object state)
        {
            var port = (SerialPort)state;
            var buf = new List<byte>();
            int len = 0;
            while (port.IsOpen)
            {
                int c;
                try { c = port.ReadByte(); }
                catch (TimeoutException) { continue; }
                catch { break; }
                if (c < 0) break;

                if (buf.Count == 0)
                {
                    if (c == 0x00 || c == 0x80 || c == 0x08 || c == 0x88) buf.Add((byte)c);
                    continue;
                }
                buf.Add((byte)c);
                if (buf.Count == 2) { len = 4 + (buf[0] & 0x07) + buf[1]; continue; }
                if (buf.Count < len) continue;

                var p = new Packet { Type = buf[0], Cls = buf[2], Cmd = buf[3], Payload = buf.Skip(4).ToArray() };
                buf.Clear();
                try
                {
                    if (p.Type == 0x00) _responses.Add(p);
                    else HandleEvent(p);
                }
                catch (Exception e) { Log.Write("Event error: " + e); }
            }
            _disconnected?.TrySetResult(true);
        }

        private void HandleEvent(Packet p)
        {
            lock (_waiters) _waiters.RemoveAll(w => w(p));

            if (p.Cls == 6 && p.Cmd == 0)                       // gap_scan_response
            {
                var pl = p.Payload;
                if (pl.Length >= AdvTail.Length && pl.Skip(pl.Length - AdvTail.Length).SequenceEqual(AdvTail))
                    _found?.TrySetResult(pl.Skip(2).Take(6).ToArray());
            }
            else if (p.Cls == 3 && p.Cmd == 4)                  // connection_disconnected
            {
                _connected = false;
                _disconnected?.TrySetResult(true);
            }
            else if (p.Cls == 4 && p.Cmd == 5 && p.Payload.Length >= 5)   // attclient_attribute_value
            {
                ushort attr = BitConverter.ToUInt16(p.Payload, 1);
                if (attr == H_IMU) OnImu(p.Payload, 5);
                else if (attr == H_CLASSIFIER) OnClassifier(p.Payload, 5);
            }
        }
    }
}
