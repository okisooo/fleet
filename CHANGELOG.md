# changelog

## unreleased

- add: public API-key providers. Free usage endpoints for OpenRouter, DeepSeek,
  Kimi, MiniMax, Z.AI, LiteLLM, and organisation cost for OpenAI and Anthropic
  admin keys. A provider switches on when its environment variable exists.
- add: opt-in rate-limit probes for OpenAI, Anthropic, Groq, Mistral, Together,
  Fireworks, Cerebras, DeepInfra, Nebius, xAI and Perplexity, which have no free
  usage endpoint. Off by default because a probe spends a token.
- add: custom entries — any HTTPS JSON endpoint mapped to metrics with dotted
  paths, configured in `providers.json`. Keys stay in environment variables.
- add: `fleet.py --providers` lists every provider, its variable, and its state.
- change: API providers render as one compact row each, and the icon cycle is
  capped and ordered worst-first so a dozen keys do not slow it down.

- fix: a portable build no longer fails to start when `%LOCALAPPDATA%\Fleet`
  does not exist yet.
- fix: the CI secret scan now fails the build on a finding instead of only
  printing one.
- fix: release artifacts use the bare version number for both tag and manual
  builds, so `v0.1.0` and `0.1.0` produce the same file names.
- add: packaged builds carry a generated app icon and Windows version details.
- add: the installer waits for a running tray, and removes cached runtime state
  on uninstall.
- add: `requirements-build.txt` pins the packaging toolchain.

## 0.1.0

First public release: tray icon, WPF flyout, Claude Code / Codex / Grok /
Antigravity readings, portable ZIP and per-user installer.
