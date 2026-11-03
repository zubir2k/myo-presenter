using System;
using System.Linq;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using System.Threading.Tasks;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.Advertisement;
using Windows.Devices.Bluetooth.GenericAttributeProfile;
using Windows.Storage.Streams;

namespace MyoPresenter
{
    /// <summary>
    /// The PC's built-in Bluetooth LE, through Windows' own WinRT stack. No pairing and no admin needed.
    /// </summary>
    public sealed class BleLink : MyoLink
    {
        private BluetoothLEDevice _device;
        private GattCharacteristic _command, _imu, _classifier;   // keep references so events keep firing
        private TaskCompletionSource<bool> _disconnected;
        private volatile bool _connected;

        public override string Name => "Bluetooth";

        public override async Task RunAsync(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    OnStatus("Searching for Myo via Bluetooth...");
                    ulong address = await ScanAsync(ct);
                    await ConnectAsync(address);
                    OnStatus("Connected via Bluetooth - wave out to sync", true);
                    using (ct.Register(() => _disconnected.TrySetResult(true)))
                        await _disconnected.Task;
                    if (!ct.IsCancellationRequested) OnStatus("Myo disconnected - reconnecting...", true);
                }
                catch (OperationCanceledException) { break; }
                catch (Exception e)
                {
                    Log.Write("Bluetooth error: " + e);
                    OnStatus("Bluetooth: " + e.Message);
                    try { await Task.Delay(3000, ct); } catch (OperationCanceledException) { break; }
                }
                finally { Cleanup(); }
            }
        }

        public override void SendCommand(byte[] command)
        {
            var ch = _command;
            if (!_connected || ch == null) return;
            Task.Run(async () =>
            {
                try { await ch.WriteValueAsync(command.AsBuffer(), GattWriteOption.WriteWithResponse); }
                catch (Exception e) { Log.Write("Command failed: " + e.Message); }
            });
        }

        private static async Task<ulong> ScanAsync(CancellationToken ct)
        {
            var found = new TaskCompletionSource<ulong>(TaskCreationOptions.RunContinuationsAsynchronously);
            var watcher = new BluetoothLEAdvertisementWatcher { ScanningMode = BluetoothLEScanningMode.Active };
            watcher.Received += (w, e) =>
            {
                bool isMyo = e.Advertisement.ServiceUuids.Contains(MyoHw.ControlService)
                             || (e.Advertisement.LocalName ?? "").StartsWith("Myo", StringComparison.OrdinalIgnoreCase);
                if (isMyo) found.TrySetResult(e.BluetoothAddress);
            };
            watcher.Stopped += (w, e) =>
            {
                if (e.Error != BluetoothError.Success)
                    found.TrySetException(new InvalidOperationException("Bluetooth is off or unavailable (" + e.Error + ")"));
            };
            watcher.Start();
            try
            {
                using (ct.Register(() => found.TrySetCanceled()))
                    return await found.Task;
            }
            finally { watcher.Stop(); }
        }

        private async Task ConnectAsync(ulong address)
        {
            _disconnected = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _device = await BluetoothLEDevice.FromBluetoothAddressAsync(address);
            if (_device == null) throw new InvalidOperationException("Could not open the Myo");
            _device.ConnectionStatusChanged += (d, _) =>
            {
                if (d.ConnectionStatus == BluetoothConnectionStatus.Disconnected) _disconnected.TrySetResult(true);
            };

            _command = await GetCharacteristic(MyoHw.ControlService, MyoHw.CommandChar);
            _imu = await GetCharacteristic(MyoHw.ImuService, MyoHw.ImuChar);
            _classifier = await GetCharacteristic(MyoHw.ClassifierService, MyoHw.ClassifierChar);

            _imu.ValueChanged += (c, e) => OnImu(e.CharacteristicValue.ToArray(), 0);
            _classifier.ValueChanged += (c, e) => OnClassifier(e.CharacteristicValue.ToArray(), 0);

            await Subscribe(_imu);
            await Subscribe(_classifier);

            foreach (var cmd in new[] { MyoHw.SetMode, MyoHw.NeverSleep, MyoHw.UnlockHold, MyoHw.LedsBlue, MyoHw.VibrateShort })
                await _command.WriteValueAsync(cmd.AsBuffer(), GattWriteOption.WriteWithResponse);
            _connected = true;
        }

        private async Task<GattCharacteristic> GetCharacteristic(Guid service, Guid characteristic)
        {
            var sr = await _device.GetGattServicesForUuidAsync(service, BluetoothCacheMode.Uncached);
            if (sr.Status != GattCommunicationStatus.Success || sr.Services.Count == 0)
                throw new InvalidOperationException("Myo service not found (" + sr.Status + ")");
            var cr = await sr.Services[0].GetCharacteristicsForUuidAsync(characteristic, BluetoothCacheMode.Uncached);
            if (cr.Status != GattCommunicationStatus.Success || cr.Characteristics.Count == 0)
                throw new InvalidOperationException("Myo characteristic not found (" + cr.Status + ")");
            return cr.Characteristics[0];
        }

        private static async Task Subscribe(GattCharacteristic ch)
        {
            var mode = ch.CharacteristicProperties.HasFlag(GattCharacteristicProperties.Indicate)
                ? GattClientCharacteristicConfigurationDescriptorValue.Indicate
                : GattClientCharacteristicConfigurationDescriptorValue.Notify;
            var status = await ch.WriteClientCharacteristicConfigurationDescriptorAsync(mode);
            if (status != GattCommunicationStatus.Success)
                throw new InvalidOperationException("Could not subscribe to Myo data (" + status + ")");
        }

        private void Cleanup()
        {
            _connected = false;
            _command = _imu = _classifier = null;
            try { _device?.Dispose(); } catch { }
            _device = null;
        }
    }
}
