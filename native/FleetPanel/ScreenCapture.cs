using System;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace FleetPanel
{
    /// <summary>
    /// Copies the screen pixels behind the flyout into one reused bitmap.
    /// The flyout window is excluded from capture (WDA_EXCLUDEFROMCAPTURE), so
    /// the copy shows what is underneath it rather than the flyout itself.
    /// Frames live only in this bitmap; nothing is written anywhere.
    /// </summary>
    internal sealed class ScreenCapture : IDisposable
    {
        private IntPtr memoryDc = IntPtr.Zero;
        private IntPtr dib = IntPtr.Zero;
        private IntPtr bits = IntPtr.Zero;
        private int width;
        private int height;

        public WriteableBitmap Bitmap { get; private set; }

        /// <summary>Mean luminance of the last frame, 0 (black) to 1 (white).</summary>
        public double Luminance { get; private set; } = 0.5;

        /// <summary>A cheap fingerprint of the last frame: equal means nothing behind moved.</summary>
        public long Signature { get; private set; }

        /// <summary>
        /// The glass blurs it heavily, so a third of the resolution looks the same and
        /// is a ninth of the pixels: cheap enough to follow the screen 30 times a second.
        /// </summary>
        public const int Downscale = 3;

        public bool Capture(int left, int top, int sw, int sh)
        {
            var w = Math.Max(1, sw / Downscale);
            var h = Math.Max(1, sh / Downscale);
            if (sw <= 0 || sh <= 0)
            {
                return false;
            }
            if (w != width || h != height || Bitmap == null)
            {
                Allocate(w, h);
            }
            IntPtr screen = Native.GetDC(IntPtr.Zero);
            try
            {
                if (!Native.StretchBlt(memoryDc, 0, 0, w, h, screen, left, top, sw, sh, Native.SRCCOPY))
                {
                    return false;
                }
            }
            finally
            {
                Native.ReleaseDC(IntPtr.Zero, screen);
            }
            var signature = Fingerprint(bits, w, h);
            if (signature == Signature && Bitmap != null && !fresh)
            {
                return false;
            }
            fresh = false;
            Signature = signature;
            Bitmap.WritePixels(new Int32Rect(0, 0, w, h), bits, w * h * 4, w * 4);
            Luminance = Measure(bits, w, h);
            return true;
        }

        /// <summary>Samples a coarse grid; a few hundred reads per frame.</summary>
        public static double Measure(IntPtr pixels, int w, int h)
        {
            double sum = 0;
            var count = 0;
            for (var y = h / 24; y < h; y += Math.Max(1, h / 12))
            {
                for (var x = w / 24; x < w; x += Math.Max(1, w / 12))
                {
                    var bgra = System.Runtime.InteropServices.Marshal.ReadInt32(pixels, (y * w + x) * 4);
                    sum += 0.0722 * (bgra & 0xFF) + 0.7152 * ((bgra >> 8) & 0xFF) + 0.2126 * ((bgra >> 16) & 0xFF);
                    count++;
                }
            }
            return count == 0 ? 0.5 : sum / count / 255.0;
        }

        private bool fresh;

        public static long FingerprintOf(IntPtr pixels, int w, int h) => Fingerprint(pixels, w, h);

        private static long Fingerprint(IntPtr pixels, int w, int h)
        {
            long hash = 1469598103934665603;
            for (var y = 1; y < h; y += Math.Max(1, h / 96))
            {
                for (var x = 1; x < w; x += Math.Max(1, w / 64))
                {
                    hash = (hash ^ System.Runtime.InteropServices.Marshal.ReadInt32(pixels, (y * w + x) * 4)) * 1099511628211;
                }
            }
            return hash;
        }

        private void Allocate(int w, int h)
        {
            fresh = true;
            Release();
            var header = new Native.BITMAPINFOHEADER
            {
                biSize = 40, biWidth = w, biHeight = -h, biPlanes = 1, biBitCount = 32,
            };
            IntPtr screen = Native.GetDC(IntPtr.Zero);
            try
            {
                memoryDc = Native.CreateCompatibleDC(screen);
                dib = Native.CreateDIBSection(screen, ref header, 0, out bits, IntPtr.Zero, 0);
                Native.SelectObject(memoryDc, dib);
                Native.SetStretchBltMode(memoryDc, Native.HALFTONE);
            }
            finally
            {
                Native.ReleaseDC(IntPtr.Zero, screen);
            }
            width = w;
            height = h;
            // Bgr32: the screen has no meaningful alpha, and an opaque source keeps
            // the blur from fading the frame's edges into transparency.
            Bitmap = new WriteableBitmap(w, h, 96, 96, PixelFormats.Bgr32, null);
        }

        private void Release()
        {
            if (dib != IntPtr.Zero) { Native.DeleteObject(dib); dib = IntPtr.Zero; }
            if (memoryDc != IntPtr.Zero) { Native.DeleteDC(memoryDc); memoryDc = IntPtr.Zero; }
        }

        public void Dispose() => Release();
    }
}
