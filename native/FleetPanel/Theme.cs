using System;
using System.Windows.Media;
using Microsoft.Win32;

namespace FleetPanel
{
    /// <summary>
    /// Colours for one OS theme, taken from Windows 11's own design tokens (the
    /// values WinUI ships), so the flyout sits beside the system's flyouts as one
    /// of them. Text and lines are tokens; colour is kept for state.
    /// </summary>
    internal sealed class Theme
    {
        public bool Light;
        // Text: primary, secondary, tertiary (WinUI TextFillColor*).
        public Color Text, TextSecondary, TextTertiary;
        // Lines and quiet fills: card edge, hairline dividers, hover and press.
        public Color Edge, Divider, Hover, Pressed;
        // Buttons and switches (ControlFill / ControlStroke / ControlStrong*).
        public Color ControlFill, ControlHover, ControlPressed, ControlStroke, ControlStrokeBottom, StrongStroke, StrongFill, OnAccent;
        // Meters: track, normal fill (the user's accent), and the two states that matter.
        public Color Track, Accent, Caution, Critical, CautionText, CriticalText;
        public Color Shadow;
        public double ShadowOpacity;
        // Acrylic, as Windows composes it: a luminosity layer evens out the
        // brightness of whatever is behind, a colour layer tints it, grain on top.
        public Color Fallback, Luminosity, Tint;
        public double LuminosityOpacity, TintOpacity, Grain;

        public static bool SystemIsLight()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
                {
                    return Convert.ToInt32(key?.GetValue("AppsUseLightTheme") ?? 1) != 0;
                }
            }
            catch (Exception)
            {
                return true;
            }
        }

        /// <summary>Windows' "Transparency effects" switch; off means an opaque surface.</summary>
        public static bool TransparencyEnabled()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
                {
                    return Convert.ToInt32(key?.GetValue("EnableTransparency") ?? 1) != 0;
                }
            }
            catch (Exception)
            {
                return true;
            }
        }

        public static Theme Create(bool light)
        {
            var accent = SystemAccent(light) ?? (light ? C(0xFF005FB8) : C(0xFF60CDFF));
            if (light)
            {
                return new Theme
                {
                    Light = true,
                    Text = C(0xE4000000), TextSecondary = C(0x9E000000), TextTertiary = C(0x72000000),
                    Edge = C(0x14000000), Divider = C(0x0F000000), Hover = C(0x09000000), Pressed = C(0x06000000),
                    ControlFill = C(0xB3FFFFFF), ControlHover = C(0x80F9F9F9), ControlPressed = C(0x4DF9F9F9),
                    ControlStroke = C(0x0F000000), ControlStrokeBottom = C(0x29000000),
                    StrongStroke = C(0x72000000), StrongFill = C(0x72000000), OnAccent = C(0xFFFFFFFF),
                    Track = C(0x14000000), Accent = accent,
                    Caution = C(0xFFE08A00), Critical = C(0xFFC42B1C), CautionText = C(0xFF9D5D00), CriticalText = C(0xFFC42B1C),
                    Shadow = C(0xFF172238), ShadowOpacity = 0.15,
                    Fallback = C(0xFFF3F3F3), Luminosity = C(0xFFF9F9F9), Tint = C(0xFFFCFCFC),
                    LuminosityOpacity = 0.90, TintOpacity = 0.0, Grain = 0.010,
                };
            }
            return new Theme
            {
                Light = false,
                Text = C(0xFFFFFFFF), TextSecondary = C(0xC5FFFFFF), TextTertiary = C(0x87FFFFFF),
                Edge = C(0x1FFFFFFF), Divider = C(0x15FFFFFF), Hover = C(0x0FFFFFFF), Pressed = C(0x0AFFFFFF),
                ControlFill = C(0x0FFFFFFF), ControlHover = C(0x15FFFFFF), ControlPressed = C(0x08FFFFFF),
                ControlStroke = C(0x12FFFFFF), ControlStrokeBottom = C(0x12FFFFFF),
                StrongStroke = C(0x8BFFFFFF), StrongFill = C(0x8BFFFFFF), OnAccent = C(0xFF000000),
                Track = C(0x1FFFFFFF), Accent = accent,
                Caution = C(0xFFF7C948), Critical = C(0xFFFF6B78), CautionText = C(0xFFFCE100), CriticalText = C(0xFFFF99A4),
                Shadow = C(0xFF000000), ShadowOpacity = 0.32,
                Fallback = C(0xFF202020), Luminosity = C(0xFF2C2C2C), Tint = C(0xFF2C2C2C),
                LuminosityOpacity = 0.92, TintOpacity = 0.32, Grain = 0.010,
            };
        }

        /// <summary>A meter's fill: the accent while calm, amber or red when it is not.</summary>
        public Color ForState(string state)
        {
            if (state == "full" || state == "high") return Critical;
            if (state == "ahead") return Caution;
            return Accent;
        }

        public static Color WithAlpha(Color color, double alpha) =>
            Color.FromArgb((byte)Math.Round(Math.Max(0, Math.Min(1, alpha)) * 255), color.R, color.G, color.B);

        private static Color C(uint argb) =>
            Color.FromArgb((byte)(argb >> 24), (byte)(argb >> 16), (byte)(argb >> 8), (byte)argb);

        /// <summary>The Windows accent, unless it is warm enough to read as a warning.</summary>
        private static Color? SystemAccent(bool light)
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"))
                {
                    if (!(key?.GetValue("AccentPalette") is byte[] palette) || palette.Length < 24)
                    {
                        return null;
                    }
                    // The palette runs light to dark: light surfaces take Dark 1, dark ones Light 2,
                    // the shades WinUI's AccentFillColorDefault uses.
                    var i = light ? 16 : 4;
                    var color = Color.FromRgb(palette[i], palette[i + 1], palette[i + 2]);
                    return IsWarm(color) ? (Color?)null : color;
                }
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static bool IsWarm(Color c)
        {
            double r = c.R / 255.0, g = c.G / 255.0, b = c.B / 255.0;
            double max = Math.Max(r, Math.Max(g, b)), min = Math.Min(r, Math.Min(g, b));
            if (max <= 0 || (max - min) / max <= 0.25) return false;
            double hue;
            if (max == r) hue = 60 * (((g - b) / (max - min)) % 6);
            else if (max == g) hue = 60 * ((b - r) / (max - min) + 2);
            else hue = 60 * ((r - g) / (max - min) + 4);
            if (hue < 0) hue += 360;
            return hue < 45 || hue > 330;
        }
    }
}
