# Fleet — AI usage and quota tracker for Windows 11

track Claude Code, OpenAI Codex, Grok, Antigravity, and API balances from your
Windows tray. Fleet shows quota meters, reset times, and reading freshness
without opening each service.

**Windows 11 · x64 builds · [MIT license](LICENSE)**

[download](#download) · [setup](#quick-start) · [provider guide](docs/providers.md) · [report a bug](https://github.com/okisooo/fleet/issues/new?template=bug_report.yml)

## download

**[v0.2.0 release notes](https://github.com/okisooo/fleet/releases/tag/v0.2.0)** · [check for the latest release](https://github.com/okisooo/fleet/releases/latest)

| download | how to use it |
| --- | --- |
| **[Windows installer — Fleet-Setup-0.2.0.exe](https://github.com/okisooo/fleet/releases/download/v0.2.0/Fleet-Setup-0.2.0.exe)** | per-user install, no administrator prompt; optional start at sign-in |
| **[portable ZIP — Fleet-0.2.0-portable.zip](https://github.com/okisooo/fleet/releases/download/v0.2.0/Fleet-0.2.0-portable.zip)** | extract the archive and run `Fleet.exe` |

Python is bundled. local clients must be installed and signed in; API providers
need keys. to upgrade, rerun the installer or quit Fleet before replacing
portable files.

for v0.2.0, follow the setup and provider guides here: the ZIP's older bundled
README has outdated setup copy and links to documents omitted from the archive.

> [!WARNING]
> binaries are unsigned; Windows SmartScreen may show a reputation warning.
> download from this repository's releases. SHA-256 checksums are in the
> [v0.2.0 release notes](https://github.com/okisooo/fleet/releases/tag/v0.2.0).

![Fleet v0.2.0 flyout showing provider usage meters, reset times, and an OpenRouter balance](docs/screenshot.png)

*the public v0.2.0 screenshot; readings depend on your connected accounts.*

## what Fleet shows

- **local client quotas:** Claude Code, OpenAI Codex, Grok CLI, and Google
  Antigravity, using the client credentials already on your machine.
- **API balances and usage:** OpenRouter, DeepSeek, Kimi/Moonshot, MiniMax, Z.AI,
  LiteLLM, and organisation cost reports with OpenAI or Anthropic admin keys.
- **optional rate-limit probes:** OpenAI, Anthropic, Groq, Mistral, Together,
  Fireworks, Cerebras, DeepInfra, Nebius, xAI, and Perplexity.
- **custom providers:** map an HTTPS JSON usage endpoint for a gateway, proxy,
  or self-hosted service through `providers.json`.

quota providers cycle in the tray; balances stay in the flyout. repeated zero-use
windows collapse. readings are marked **live, cached, or unavailable**, with
reset times where available.

support and available metrics vary by provider; see the
[provider matrix, requirements, and limitations](docs/providers.md).

## quick start

1. install or extract Fleet, then launch `Fleet.exe`.
2. sign in to your local clients on the same Windows account. keep Claude Code
   on `PATH` for login renewal.
3. for API providers, set the matching environment variable, then choose
   **Refresh now** from Fleet's tray menu. for example:

   ```powershell
   setx OPENROUTER_API_KEY "your-key"
   ```

left-click to toggle the flyout; right-click for **Refresh now** or **Quit**.
check Windows' tray overflow if the icon is hidden.

API usage endpoints activate when their key is available. **rate-limit probes
are off by default:** `"probe": true` sends a one-token inference request each
poll and may cost money.

custom endpoints and overrides go in `%LOCALAPPDATA%\Fleet\providers.json`
(packaged) or `providers.json` beside `fleet.py` (source). keys stay in environment
variables. see the [setup guide](docs/providers.md) and
[example configuration](providers.example.json).

## troubleshooting and feedback

- **missing or stale reading:** check client sign-in or API-key setup, then
  choose **Refresh now**. cached readings retain an older result.
- **unexpected balance:** check the [provider guide](docs/providers.md#built-in).
  OpenRouter management keys show account credits; regular keys use a
  key budget/spend fallback.
- **still stuck:** [report a bug](https://github.com/okisooo/fleet/issues/new?template=bug_report.yml)
  with Fleet version, install type, Windows build, provider, and expected versus
  actual behaviour. redact diagnostics before sharing.

never post tokens, credential files, `.cache` contents, or provider response
bodies. suspected security issues follow the private reporting guidance in
[SECURITY.md](SECURITY.md).

## privacy and compatibility

Fleet has no telemetry or Fleet backend. it reads local credentials and contacts
providers directly; authentication flows may refresh those credentials.
runtime state lives in `%LOCALAPPDATA%\Fleet\.cache` (packaged) or `.cache` (source).

this is an early public Windows 11 release; provider APIs and credential formats
can change without notice. Fleet is independent of the listed providers, whose
names and artwork remain theirs. see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## run from source

requires Windows 11, Git, and 64-bit Python 3.13 (used by CI). the flyout uses
Windows' .NET Framework/WPF. configure providers as above.

```powershell
git clone https://github.com/okisooo/fleet.git
cd fleet
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\fleet-tray.cmd
```

source diagnostics, without opening the flyout:

```powershell
.\.venv\Scripts\python.exe fleet.py --providers
.\.venv\Scripts\python.exe tray.py --dry
```

`--providers` lists API-key configuration; `--dry` polls and prints readings.
see [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## more from OKISO

[**Backdrop**](https://github.com/okisooo/backdrop) adds custom images and animated
GIF wallpapers to Claude Desktop, Codex Desktop, and T3 Code, with blur and
readability controls. [explore more projects by OKISO](https://github.com/okisooo).
