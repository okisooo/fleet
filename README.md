# fleet

small Windows 11 tray utility for every AI quota you are paying for: the local
Claude Code, Codex, Grok and Antigravity installs, plus public API keys like
OpenRouter, DeepSeek, Kimi, MiniMax, Z.AI, LiteLLM, and any endpoint you describe
yourself.

Fleet stays in the notification area, cycles provider status in its icon, and
opens a compact flyout with live/cached freshness, usage bars, and reset times.

<img src="docs/screenshot.png" alt="The Fleet flyout: one row per usage window, with bars, reset times and freshness" width="280">

Every row is a real quota window. Green means the reading is live, amber means it
is the last cached one and how old it is — above, Antigravity was closed, so its
numbers are five hours old while the rest are current.

## status

Windows-only experimental project. It works with locally installed provider
clients; vendor endpoints and response formats can change without notice.

Fleet is an independent project. It is not affiliated with Anthropic, OpenAI,
xAI, Google, Claude Code, Codex, Grok, or Antigravity.

## easy install

Download `Fleet-Setup-<version>.exe` from the project's GitHub Releases page,
run it, and optionally tick **Start Fleet when I sign in**. Fleet then appears in
the notification area; left-click toggles the flyout and right-click offers refresh
or quit. No Python or terminal is needed.

The installer is per-user (no admin prompt), installs to `%LOCALAPPDATA%\Fleet`,
and keeps runtime state in `%LOCALAPPDATA%\Fleet\.cache`. Uninstalling removes
both. Quit Fleet from the tray before installing an update: Setup will otherwise
ask you to close it.

A portable ZIP is published alongside the installer. Unzip it anywhere and run
`Fleet.exe`; it uses the same `%LOCALAPPDATA%\Fleet` state directory and adds no
Start menu or startup entry.

Fleet does not install any provider CLI or sign you in: install and sign in to
the provider apps you want Fleet to display first.

## from source

Requirements: Windows 11, Python 3.13+, and whichever provider CLIs you want to
inspect.

```powershell
git clone https://github.com/okisooo/fleet.git
cd fleet
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\fleet-tray.cmd
```

Left-click the tray icon to toggle the flyout. Right-click for refresh and quit.

A source checkout keeps its state in the repository's own `.cache\`, which is
ignored by Git.

```powershell
.\.venv\Scripts\python.exe tray.py --dry   # one line per provider, no window
.\.venv\Scripts\python.exe fleet.py --once # full terminal view (needs `rich`)
```

## providers

Local clients are detected on their own. Public API providers switch on as soon
as their key is in an environment variable:

```powershell
setx OPENROUTER_API_KEY "sk-or-..."
```

Free usage endpoints: OpenRouter, DeepSeek, Kimi (Moonshot), MiniMax, Z.AI,
LiteLLM, and organisation cost for OpenAI and Anthropic admin keys. Providers
that publish quota only in rate-limit headers (OpenAI, Anthropic, Groq, Mistral,
Together, Fireworks, Cerebras, DeepInfra, Nebius, xAI, Perplexity) need a real
1-token request, so they stay off until you opt in per provider.

Anything else — a gateway, a proxy, a self-hosted deployment — can be added as a
custom entry with one URL and a few field paths. Keys are read from environment
variables only; the config file holds variable names, never secrets.

Full list, config format and custom-entry schema: [docs/providers.md](docs/providers.md).

```powershell
.\.venv\Scripts\python.exe fleet.py --providers   # what is configured, and how
```

## releases

Maintainers build a portable app and installer with the **release build**
GitHub workflow. It runs the test suite first, uploads both artifacts for manual
runs, and publishes them automatically only for a `v*` version tag. Local build
steps are in [CONTRIBUTING.md](CONTRIBUTING.md).

Releases are unsigned until the project obtains a Windows code-signing
certificate, so Windows SmartScreen can warn about an unrecognised app the first
time a release is run. Verify the download came from the project's Releases page.

## privacy and credential behavior

- Fleet reads local CLI/app credential state only to request the corresponding
  provider's usage data.
- Tokens are sent only to that provider's HTTPS endpoints or to Antigravity's
  loopback language server. Fleet does not send telemetry to a Fleet service.
- Runtime state lives in `.cache/`, which is ignored by Git. Never commit it.
- Claude refreshes through Claude Code first. Grok can atomically update its own
  CLI credential file if its provider rotates a token. Codex refresh is opt-in
  for the console tool via `--refresh`.
- Antigravity reads live data from its local language server. When it is closed,
  Fleet may use an existing short-lived local access token, then falls back to
  the last cached reading. Fleet does not refresh that token with a copied
  third-party OAuth client.
- `--debug` prints status and exception types, never token or response-body data.

Use only with accounts and machines you control. Review each provider's terms
before using undocumented usage endpoints.

## development

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Contribution and local packaging instructions: [CONTRIBUTING.md](CONTRIBUTING.md).

The project deliberately uses original provider monograms instead of vendor
logos, so the repository does not redistribute third-party artwork. The app icon
is generated at build time by `scripts/build_assets.py`.

## security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
