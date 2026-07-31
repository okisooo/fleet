# changelog

## 0.1.0 — unreleased

First public release.

- Tray icon and compact WPF flyout for Windows 11, showing one row per quota
  window with live/cached freshness, usage bars and reset times.
- Local clients: Claude Code, Codex, Grok, Antigravity, read from the credential
  state already on the machine.
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
