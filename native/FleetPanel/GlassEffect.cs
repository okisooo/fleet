using System;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Media.Media3D;

namespace FleetPanel
{
    /// <summary>The acrylic layers (Glass.hlsl). Every parameter maps to one float4 register.</summary>
    internal sealed class GlassEffect : ShaderEffect
    {
        private static readonly PixelShader Shader = Load();

        private static PixelShader Load()
        {
            var shader = new PixelShader();
            using (var stream = typeof(GlassEffect).Assembly.GetManifestResourceStream("Glass.ps"))
            {
                shader.SetStreamSource(stream);
            }
            shader.Freeze();
            return shader;
        }

        public static readonly DependencyProperty InputProperty =
            RegisterPixelShaderSamplerProperty("Input", typeof(GlassEffect), 0);
        public static readonly DependencyProperty SizeProperty = Register("Size", 0);
        public static readonly DependencyProperty LuminosityProperty = Register("Luminosity", 1);
        public static readonly DependencyProperty TintProperty = Register("Tint", 2);
        public static readonly DependencyProperty GrainProperty = Register("Grain", 3);

        private static DependencyProperty Register(string name, int register) =>
            DependencyProperty.Register(name, typeof(Point4D), typeof(GlassEffect),
                new UIPropertyMetadata(new Point4D(), PixelShaderConstantCallback(register)));

        public GlassEffect(Theme theme)
        {
            PixelShader = Shader;
            UpdateShaderValue(InputProperty);
            Luminosity = Layer(theme.Luminosity, theme.LuminosityOpacity);
            Tint = Layer(theme.Tint, theme.TintOpacity);
            Grain = new Point4D(theme.Grain, 0, 0, 0);
            UpdateShaderValue(SizeProperty);
        }

        private static Point4D Layer(Color color, double opacity) =>
            new Point4D(color.R / 255.0, color.G / 255.0, color.B / 255.0, opacity);

        public Brush Input { get => (Brush)GetValue(InputProperty); set => SetValue(InputProperty, value); }
        public Point4D Size { get => (Point4D)GetValue(SizeProperty); set => SetValue(SizeProperty, value); }
        public Point4D Luminosity { get => (Point4D)GetValue(LuminosityProperty); set => SetValue(LuminosityProperty, value); }
        public Point4D Tint { get => (Point4D)GetValue(TintProperty); set => SetValue(TintProperty, value); }
        public Point4D Grain { get => (Point4D)GetValue(GrainProperty); set => SetValue(GrainProperty, value); }
    }
}
