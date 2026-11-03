using System;
using System.Runtime.InteropServices;

namespace MyoPresenter
{
    /// <summary>Keyboard and mouse injection via SendInput. Works as a standard (non-admin) user.</summary>
    internal static class Native
    {
        private const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
        private const uint KEYEVENTF_KEYUP = 0x0002;
        private const uint MOUSEEVENTF_MOVE = 0x0001;

        public const ushort VK_LEFT = 0x25, VK_RIGHT = 0x27, VK_CONTROL = 0x11;
        public const ushort VK_A = 0x41, VK_B = 0x42, VK_L = 0x4C;

        [StructLayout(LayoutKind.Sequential)]
        private struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }

        [StructLayout(LayoutKind.Sequential)]
        private struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }

        [StructLayout(LayoutKind.Explicit)]
        private struct InputUnion
        {
            [FieldOffset(0)] public MOUSEINPUT mi;
            [FieldOffset(0)] public KEYBDINPUT ki;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct INPUT { public uint type; public InputUnion u; }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

        private static INPUT Key(ushort vk, bool up) => new INPUT
        {
            type = INPUT_KEYBOARD,
            u = new InputUnion { ki = new KEYBDINPUT { wVk = vk, dwFlags = up ? KEYEVENTF_KEYUP : 0 } }
        };

        /// <summary>Press a key or a combo, e.g. Press(VK_CONTROL, VK_L).</summary>
        public static void Press(params ushort[] keys)
        {
            var inputs = new INPUT[keys.Length * 2];
            for (int i = 0; i < keys.Length; i++)
            {
                inputs[i] = Key(keys[i], false);
                inputs[inputs.Length - 1 - i] = Key(keys[i], true);
            }
            SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        }

        public static void MoveMouse(int dx, int dy)
        {
            var input = new[]
            {
                new INPUT { type = INPUT_MOUSE, u = new InputUnion { mi = new MOUSEINPUT { dx = dx, dy = dy, dwFlags = MOUSEEVENTF_MOVE } } }
            };
            SendInput(1, input, Marshal.SizeOf(typeof(INPUT)));
        }
    }
}
