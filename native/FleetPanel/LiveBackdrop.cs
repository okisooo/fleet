using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

namespace FleetPanel
{
    /// <summary>
    /// Follows the screen behind the flyout on its own thread. Reading the screen
    /// back costs 6-9 ms (Windows syncs the composed frame first), so doing it on
    /// the UI thread capped drawing near 100 fps. Here it never blocks drawing:
    /// the UI takes the newest frame when it renders. No WPF objects live here.
    /// </summary>
    internal sealed class LiveBackdrop
    {
        private readonly object gate = new object();
        private Thread thread;
        private volatile bool running;
        private readonly ManualResetEventSlim stopping = new ManualResetEventSlim(false);
        private int left, top, width, height;
        private byte[] frame;
        private int frameWidth, frameHeight;
        private double luminance;
        private bool pending;
        private int captures;
        private double totalMs;

        public int Captures { get { lock (gate) return captures; } }

        public double TotalMs { get { lock (gate) return totalMs; } }

        public void Start(int l, int t, int w, int h)
        {
            Move(l, t, w, h);
            if (running)
            {
                return;
            }
            lock (gate)
            {
                captures = 0;
                totalMs = 0;
            }
            running = true;
            stopping.Reset();
            thread = new Thread(Run) { IsBackground = true, Name = "fleet-glass" };
            thread.Start();
        }

        public void Move(int l, int t, int w, int h)
        {
            lock (gate)
            {
                left = l;
                top = t;
                width = w;
                height = h;
            }
        }

        public void Stop()
        {
            running = false;
            stopping.Set();
            // A new open must not revive the previous worker before it exits.
            // Wake its paced wait immediately; only an in-flight copy can remain.
            thread?.Join();
            thread = null;
        }

        /// <summary>The newest frame, if one arrived since the last call.</summary>
        public bool TryTake(ref byte[] target, out int w, out int h, out double lum)
        {
            lock (gate)
            {
                w = frameWidth;
                h = frameHeight;
                lum = luminance;
                if (!pending || frame == null)
                {
                    return false;
                }
                if (target == null || target.Length != frame.Length)
                {
                    target = new byte[frame.Length];
                }
                Buffer.BlockCopy(frame, 0, target, 0, frame.Length);
                pending = false;
                return true;
            }
        }

        private void Run()
        {
            IntPtr dc = IntPtr.Zero, dib = IntPtr.Zero, bits = IntPtr.Zero;
            int dw = 0, dh = 0, still = 0;
            long last = 0;
            var clock = new Stopwatch();
            try
            {
                while (running)
                {
                    int l, t, w, h;
                    lock (gate)
                    {
                        l = left;
                        t = top;
                        w = width;
                        h = height;
                    }
                    if (w <= 0 || h <= 0)
                    {
                        stopping.Wait(16);
                        continue;
                    }
                    var cw = Math.Max(1, w / ScreenCapture.Downscale);
                    var ch = Math.Max(1, h / ScreenCapture.Downscale);
                    if (cw != dw || ch != dh || dc == IntPtr.Zero)
                    {
                        Free(ref dc, ref dib);
                        var header = new Native.BITMAPINFOHEADER { biSize = 40, biWidth = cw, biHeight = -ch, biPlanes = 1, biBitCount = 32 };
                        var screen0 = Native.GetDC(IntPtr.Zero);
                        dc = Native.CreateCompatibleDC(screen0);
                        dib = Native.CreateDIBSection(screen0, ref header, 0, out bits, IntPtr.Zero, 0);
                        Native.SelectObject(dc, dib);
                        // The default (BLACKONWHITE) darkens colour images when shrinking.
                        Native.SetStretchBltMode(dc, Native.COLORONCOLOR);
                        Native.ReleaseDC(IntPtr.Zero, screen0);
                        dw = cw;
                        dh = ch;
                    }
                    clock.Restart();
                    var screen = Native.GetDC(IntPtr.Zero);
                    // Nearest-pixel shrink: the glass blurs it anyway, and HALFTONE
                    // filtering would add about 2.5 ms to every copy.
                    var ok = Native.StretchBlt(dc, 0, 0, cw, ch, screen, l, t, w, h, Native.SRCCOPY);
                    Native.ReleaseDC(IntPtr.Zero, screen);
                    clock.Stop();
                    if (!ok)
                    {
                        stopping.Wait(16);
                        continue;
                    }
                    // Sixty copies a second while something behind moves: under a blur that
                    // is as live as the display, at a fraction of a core. A still screen
                    // is checked four times less often until it moves again.
                    var signature = ScreenCapture.FingerprintOf(bits, cw, ch);
                    still = signature == last ? still + 1 : 0;
                    var rest = (still > 12 ? 66.0 : 1000.0 / 60) - clock.Elapsed.TotalMilliseconds;
                    lock (gate)
                    {
                        captures++;
                        totalMs += clock.Elapsed.TotalMilliseconds;
                    }
                    if (rest >= 1)
                    {
                        stopping.Wait((int)Math.Ceiling(rest));
                    }
                    lock (gate)
                    {
                        if (signature == last)
                        {
                            continue;
                        }
                        last = signature;
                        var size = cw * ch * 4;
                        if (frame == null || frame.Length != size)
                        {
                            frame = new byte[size];
                        }
                        Marshal.Copy(bits, frame, 0, size);
                        frameWidth = cw;
                        frameHeight = ch;
                        luminance = ScreenCapture.Measure(bits, cw, ch);
                        pending = true;
                    }
                }
            }
            finally
            {
                Free(ref dc, ref dib);
            }
        }

        private static void Free(ref IntPtr dc, ref IntPtr dib)
        {
            // Release the DC's selection before deleting its bitmap. GDI refuses
            // DeleteObject while the bitmap is still selected into a live DC.
            if (dc != IntPtr.Zero)
            {
                Native.DeleteDC(dc);
                dc = IntPtr.Zero;
            }
            if (dib != IntPtr.Zero)
            {
                Native.DeleteObject(dib);
                dib = IntPtr.Zero;
            }
        }
    }
}
