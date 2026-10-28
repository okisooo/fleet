# changelog

## unreleased - panel efficiency

- preserved the installed popup's layout, fonts, themes, controls and navigation.
- kept controls intact on timestamp-only refreshes and reused frozen brushes.
- replaced frequent file checks with debounced notifications and a recovery timer.
- paced live-glass handoff to its 60 Hz capture rate while retaining display-rate
  entrance and exit motion; stopped capture workers before reopening.
- bounded panel error logs and closed window-owned timers and capture resources.
- added reproducible, isolated memory, refresh and navigation checks; see
  [performance checks](docs/performance.md). collectors and account behavior are
  unchanged.

## 0.4.1 - 2026-10-26

- changed **Dashboard** to switch pages inside the existing tray popup. removed
  the separate desktop window and its taskbar entry.
- kept Back, Refresh and Settings visible while the dashboard scrolls. page
  navigation resets scroll; fresh data preserves the current dashboard position.
- fitted the summary, charts and token tables to the popup's existing width,
  using the same acrylic, typography and controls as the main usage page.
- kept expanded local history, full reported limits, coverage notes and privacy
  behavior from 0.4.0. stale launch requests no longer reopen the popup later.

## 0.4.0 - 2026-10-26

- added a **Dashboard** button to the native flyout and tray menu. it opens one
  resizable desktop window with provider quota meters, reset times, API account
  spending and clear cached/unavailable states.
- expanded local Claude Code/Codex history with 24-hour and 14-day activity,
  input/output/cache token breakdowns, and up to 20 models and projects from the
  last 7 days. local history remains available while a client is signed out.
- added coverage notes for pending logs, skipped compressed Codex archives,
  estimated model prices and messages without a known price.
- kept the existing Windows typography, theme and control styles. the dashboard
  stays open on focus changes, preserves scroll position during updates, and
  returns to one column in a narrower window.
- the dashboard reads the tray's normalized local model; it adds no network
  collector, credentials or telemetry. API billing remains separate from local
  API-price estimates and subscription limits.

## 0.3.0 - 2026-10-25

- added Claude API setup to the native flyout, with hidden key entry and Windows
  Credential Manager storage. existing environment variables retain precedence.
- regular Claude keys get a free connection check. organization billing keys
  show today's and calendar-month spending in UTC, with decimal-cent conversion,
  complete pagination and real zero-spend readings.
- included Max/Team credits remain separate from subscription limits and local
  API-price estimates. **Open billing** opens the Console balance and expiry;
  no remaining promotional balance is invented from calendar-month spend.
- rebuilt the flyout as a native WPF usage dashboard with slim quota meters,
  acrylic, provider details, reset times and explicit cached-reading states.
- added local Claude Code/Codex token history at API list prices, activity,
  burn rate, cache share and per-model/project details. these are estimates,
  not subscription charges; compressed Codex archives are not yet indexed.
- added tray styles, provider visibility, alerts and settings. kept balance-only
  providers readable in the flyout instead of cycling them through the tray.
- improved tray recovery, panel lifetime, credential renewal through the clients
  themselves, and Antigravity's provider-supplied quota groups.
- prevented rejected keys, replacement keys and old UTC days from reusing
  misleading billing readings; paced failed requests and honored Retry-After.
- added the authenticated local loopback usage bridge for T3 Code. API admin keys
  are not exposed through that bridge.
- bundled the native panel and linked documentation in portable/installable
  packages. CI now builds the native panel; release builds include checksums and
  create drafts for verification before publication.
- the installer's start-at-sign-in option now defaults to enabled; it can be
  unchecked during setup. no separate recovery watchdog is installed.

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
