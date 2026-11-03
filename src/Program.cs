using System;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace MyoPresenter
{
    internal static class Log
    {
        private static readonly object Gate = new object();
        public static readonly string Folder =
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MyoPresenter");
        private static readonly string FilePath = Path.Combine(Folder, "log.txt");

        static Log()
        {
            try { Directory.CreateDirectory(Folder); File.WriteAllText(FilePath, ""); } catch { }
        }

        public static void Write(string text)
        {
            lock (Gate)
            {
                try { File.AppendAllText(FilePath, $"{DateTime.Now:HH:mm:ss} {text}{Environment.NewLine}"); } catch { }
            }
        }
    }

    internal static class Program
    {
        public const string AppName = "Myo Presenter";

        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Contains("--selftest")) return SelfTest();

            using (var mutex = new Mutex(true, @"Local\MyoPresenterSingleInstance", out bool first))
            {
                if (!first)
                {
                    MessageBox.Show("Myo Presenter is already running - look for it in the system tray.",
                        AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return 0;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                var mode = args.Contains("--ble") ? LinkMode.Ble : args.Contains("--dongle") ? LinkMode.Dongle : LinkMode.Auto;
                Application.Run(new TrayApp(mode));
            }
            return 0;
        }

        /// <summary>Used by the build to prove the WinRT Bluetooth and WMI parts load.</summary>
        private static int SelfTest()
        {
            try
            {
                Log.Write("selftest: dongle port = " + (DongleLink.FindPort() ?? "none"));
                var w = new Windows.Devices.Bluetooth.Advertisement.BluetoothLEAdvertisementWatcher();
                Log.Write("selftest: WinRT watcher status = " + w.Status);
                Log.Write("selftest ok " + Application.ProductVersion);
                return 0;
            }
            catch (Exception e)
            {
                Log.Write("selftest FAILED: " + e);
                return 1;
            }
        }
    }

    internal enum LinkMode { Auto, Dongle, Ble }

    /// <summary>Tray icon, menu, tips and notifications.</summary>
    internal sealed class TrayApp : ApplicationContext
    {
        private const string Tips =
            "Double tap  -  arm / disarm (safety)\n" +
            "Wave out  -  next slide\n" +
            "Wave in  -  previous slide\n" +
            "Fingers spread  -  laser pointer on / off\n" +
            "      (rotate your forearm to move it)\n" +
            "Fist  -  black screen on / off\n\n" +
            "First time on: wave out once to sync the band.\n" +
            "Right-click the tray icon to exit.";

        private readonly NotifyIcon _icon;
        private readonly ToolStripMenuItem _statusItem;
        private readonly Control _ui = new Control();
        private readonly CancellationTokenSource _cts = new CancellationTokenSource();
        private string _status = "Starting...";
        private bool _tipsOpen;

        public TrayApp(LinkMode mode)
        {
            _ui.CreateControl();
            var menu = new ContextMenuStrip();
            menu.Items.Add(new ToolStripMenuItem("Gesture tips", null, (s, e) => ShowTips()) { Font = new Font(menu.Font, FontStyle.Bold) });
            _statusItem = new ToolStripMenuItem(_status) { Enabled = false };
            menu.Items.Add(_statusItem);
            menu.Items.Add(new ToolStripMenuItem("Open log folder", null, (s, e) => System.Diagnostics.Process.Start("explorer.exe", Log.Folder)));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Exit", null, (s, e) => ExitApp()));

            Icon icon;
            using (var stream = typeof(TrayApp).Assembly.GetManifestResourceStream("myo.ico"))
                icon = new Icon(stream, SystemInformation.SmallIconSize);

            _icon = new NotifyIcon { Icon = icon, Text = Program.AppName, ContextMenuStrip = menu, Visible = true };
            _icon.MouseClick += (s, e) => { if (e.Button == MouseButtons.Left) ShowTips(); };

            ShowTips();
            Task.Run(() => RunLinkAsync(mode));
        }

        private async Task RunLinkAsync(LinkMode mode)
        {
            try
            {
                string port = mode == LinkMode.Ble ? null : DongleLink.FindPort();
                MyoLink link;
                if (port != null) link = new DongleLink(port);
                else if (mode == LinkMode.Dongle)
                {
                    SetStatus("Myo dongle not found - plug it in and restart", true);
                    return;
                }
                else link = new BleLink();

                Log.Write("Using " + link.Name);
                var presenter = new Presenter(link);
                link.Status += SetStatus;
                presenter.Status += SetStatus;
                await link.RunAsync(_cts.Token);
            }
            catch (Exception e)
            {
                Log.Write("Stopped: " + e);
                SetStatus("Stopped: " + e.Message, true);
            }
        }

        private void SetStatus(string text, bool notify)
        {
            Log.Write(text);
            if (_ui.IsDisposed) return;
            _ui.BeginInvoke((Action)(() =>
            {
                _status = text;
                _statusItem.Text = text;
                var tip = Program.AppName + " - " + text;
                _icon.Text = tip.Length > 63 ? tip.Substring(0, 63) : tip;
                if (notify) _icon.ShowBalloonTip(3000, Program.AppName, text, ToolTipIcon.None);
            }));
        }

        private void ShowTips()
        {
            if (_tipsOpen) return;
            _tipsOpen = true;
            _ui.BeginInvoke((Action)(() =>
            {
                MessageBox.Show($"Status: {_status}\n\n{Tips}", $"{Program.AppName} {Application.ProductVersion}",
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                _tipsOpen = false;
            }));
        }

        private void ExitApp()
        {
            _cts.Cancel();
            _icon.Visible = false;
            _icon.Dispose();
            ExitThread();
            Environment.Exit(0);
        }
    }
}
