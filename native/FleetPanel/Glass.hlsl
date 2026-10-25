// Acrylic for the Fleet flyout (ps_2_0, so WPF can also run it in software,
// which is how the offscreen previews are made).
//
// Input is the live capture of whatever sits behind the window, already
// frosted by a WPF BlurEffect. Windows composes acrylic in three layers, and
// so does this: a luminosity layer pulls the backdrop to one brightness while
// keeping a hint of its colour, a colour layer tints it, and fine grain keeps
// the surface from looking like flat paint. What is behind can no longer show
// through as bright blocks of colour, so the card reads as one object on any
// wallpaper and text never sits on a moving contrast.
// Compile: fxc /T ps_2_0 /E main /Fo Glass.ps Glass.hlsl

sampler2D input : register(s0);
float4 size : register(c0);        // xy: element size in pixels, for the grain grid
float4 luminosity : register(c1);  // rgb: luminosity colour; a: its opacity
float4 tint : register(c2);        // rgb: tint colour; a: its opacity
float4 grain : register(c3);       // x: grain amplitude

float Lum(float3 c)
{
    return dot(c, float3(0.30, 0.59, 0.11));
}

// W3C non-separable blending: move a colour to luminance l, then pull any
// channel that left 0..1 back toward grey without changing the luminance.
float3 SetLum(float3 c, float l)
{
    c += l - Lum(c);
    float k = Lum(c);
    float lo = min(c.r, min(c.g, c.b));
    float hi = max(c.r, max(c.g, c.b));
    c = lerp(c, k + (c - k) * k / max(k - lo, 0.0001), lo < 0);
    c = lerp(c, k + (c - k) * (1 - k) / max(hi - k, 0.0001), hi > 1);
    return c;
}

float4 main(float2 uv : TEXCOORD) : COLOR
{
    float3 c = tex2D(input, uv).rgb;

    // Luminosity blend: the backdrop's hue and saturation at the layer's brightness.
    c = lerp(c, SetLum(c, Lum(luminosity.rgb)), luminosity.a);

    // Colour blend with a neutral tint: the tint's grey at the backdrop's brightness.
    float l = Lum(c);
    c = lerp(c, tint.rgb * (l / max(Lum(tint.rgb), 0.0001)), tint.a);

    // Grain on the pixel grid, the same few per cent Windows uses.
    float2 p = floor(uv * size.xy);
    float n = frac(sin(dot(p, float2(12.9898, 78.233))) * 43758.5453);
    c += (n - 0.5) * grain.x;

    return float4(saturate(c), 1);
}
