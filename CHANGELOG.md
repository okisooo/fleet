# changelog

## 0.2.0 - 2026-08-07

- Rebuilt the flyout around spacious provider cards, full-width quota meters,
  clearer reset metadata, and a calmer blue/amber/coral pressure scale.
- Collapsed repeated zero-use quota windows behind an accessible disclosure and
  gave Antigravity's Gemini and Claude windows distinct labels.
- Replaced tiny tray rings with full-bleed provider tiles and a four-pixel bottom
  usage meter that remains legible at Windows' 16-pixel notification size.
- Kept balance-only providers out of the tray cycle; OpenRouter now prefers the
  account credit balance and falls back to regular-key budget and spend.
- Cleaned up Codex's optional Spark and credit rows, and reduced repeated Grok
  authentication pressure with a short successful-reading cache.
- Made API keys written with `setx` discoverable without restarting Fleet.
- Hardened the solid-window fallback for systems where acrylic is unavailable.
- Bundled the provider identification artwork and its third-party notice in both
  portable and installed releases.

## 0.1.1 - 2026-07-31

- Refreshed expired Antigravity quota tokens from local installation metadata
  while the Antigravity app is closed.

## 0.1.0

First public release.

- Tray icon and compact WPF flyout for Windows 11, showing one row per quota
  window with live/cached freshness, usage bars and reset times.
- Local clients: Claude Code, Codex, Grok, Antigravity, read from the credential
  state already on the machine.
- Antigravity closed-app refresh: expired access tokens can be renewed from local
  OAuth state without opening the IDE; Fleet does not bundle or persist vendor
  OAuth client metadata.
- Public API-key providers with free usage endpoints: OpenRouter, DeepSeek, Kimi
  (Moonshot), MiniMax, Z.AI, LiteLLM, plus organisation cost for OpenAI and
  Anthropic admin keys. A provider switches on when its variable exists.
- Opt-in rate-limit probes for OpenAI, Anthropic, Groq, Mistral, Together,
  Fireworks, Cerebras, DeepInfra, Nebius, xAI and Perplexity, which publish no
  free usage endpoint. Off by default because a probe spends a token.
- Custom entries: any HTTPS JSON endpoint mapped to metrics with dotted paths,
  configured in `providers.json`. Keys stay in environment variables.
- `fleet.py --providers` lists every provider, its variable, and its state.
- One-click Windows delivery: per-user Inno Setup installer with an optional
  startup entry, plus a portable ZIP, both built and published by the release
  workflow on a `v*` tag.
