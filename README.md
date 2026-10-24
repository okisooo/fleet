<p><a href="https://github.com/okisooo/fleet/releases/latest"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/okisooo/fleet/main/docs/fleet-sleeve-dark.svg">
  <img src="https://raw.githubusercontent.com/okisooo/fleet/main/docs/fleet-sleeve-light.svg" alt="fleet — AI usage and quota tracker for Windows 11, one tray away. Fleet's ring mark stands in for the F; every reading is marked live, cached or unavailable; Claude Code, Codex, Grok CLI, Antigravity, OpenRouter, DeepSeek, Kimi, MiniMax, Z.AI and LiteLLM" width="900">
</picture></a></p>

<p align="center">
  <strong>ai usage and quota tracker for Windows 11</strong> · v0.2.0 · MIT<br>
  <a href="https://github.com/okisooo/fleet/releases/download/v0.2.0/Fleet-Setup-0.2.0.exe">Windows installer</a> · <a href="https://github.com/okisooo/fleet/releases/download/v0.2.0/Fleet-0.2.0-portable.zip">portable ZIP</a> · <a href="#quick-start">setup</a> · <a href="docs/providers.md">provider guide</a>
</p>

track Claude Code, OpenAI Codex, Grok CLI, Google Antigravity, and API providers
without opening each service. quota meters cycle in the Windows tray; the flyout
brings usage, balances, and reset times together. readings are marked **live,
cached, or unavailable**, so an older result stays visibly older.

<p align="center">
  <img src="docs/screenshot.png" width="320" height="592" alt="Fleet v0.2.0 flyout with Claude, Codex, Grok, and Antigravity quota meters, reset times, and an OpenRouter balance">
  <br><sub>the public v0.2.0 screenshot; readings depend on your connected accounts.</sub>
</p>

## download

| v0.2.0 · Python included | getting started |
| --- | --- |
| **[Windows installer](https://github.com/okisooo/fleet/releases/download/v0.2.0/Fleet-Setup-0.2.0.exe)** | per-user install; no administrator prompt; optional start at sign-in |
| **[portable ZIP](https://github.com/okisooo/fleet/releases/download/v0.2.0/Fleet-0.2.0-portable.zip)** | extract and run `Fleet.exe` |

[release notes and SHA-256 checksums](https://github.com/okisooo/fleet/releases/tag/v0.2.0)
· [latest release](https://github.com/okisooo/fleet/releases/latest)

> [!WARNING]
> binaries are unsigned; Windows SmartScreen may show a reputation warning.
> download from this repository's releases and check the published checksums.

to upgrade, rerun the installer or quit Fleet before replacing portable files.
the v0.2.0 ZIP contains an older README and omits linked guides; use the
[online setup](https://github.com/okisooo/fleet#quick-start) and
[provider guide](https://github.com/okisooo/fleet/blob/main/docs/providers.md).

## what you can track

| source | available readings |
| --- | --- |
| **local clients** | Claude Code, OpenAI Codex, Grok CLI, and Google Antigravity quotas, using existing client credentials |
| **API providers** | OpenRouter, DeepSeek, Kimi/Moonshot, MiniMax, Z.AI, and LiteLLM balances or usage; OpenAI and Anthropic organisation costs with admin keys |
| **opt-in probes** | rate-limit headers from OpenAI, Anthropic, and other supported APIs; real inference requests that may cost money |
| **custom providers** | map an HTTPS JSON usage endpoint for a gateway, proxy, or self-hosted service |

balances stay in the flyout; repeated zero-use quota windows collapse. support
and metrics vary by provider. see the [provider matrix and limitations](docs/providers.md),
including unsupported Gemini API keys and console-only dashboards.

## quick start

1. install or extract Fleet, then launch `Fleet.exe`.
2. install and sign in to the local clients you use on the same Windows account.
   keep Claude Code on `PATH` for login renewal.
3. for API providers, set the matching key, then choose **Refresh now** from the
   tray menu. for example:

   ```powershell
   setx OPENROUTER_API_KEY "your-key"
   ```

left-click the icon to toggle the flyout; right-click for **Refresh now** or
**Quit**. check Windows' tray overflow if the icon is hidden.

API usage endpoints activate when their key is available. **rate-limit probes
are off by default:** `"probe": true` sends a one-token inference request on each
poll, may cost money, and appears in the provider's logs.

custom endpoints and overrides go in `%LOCALAPPDATA%\Fleet\providers.json`
(packaged) or `providers.json` beside `fleet.py` (source). keep keys in environment
variables, not the config file. see the [setup guide](docs/providers.md) and
[example configuration](providers.example.json).

## privacy and troubleshooting

Fleet has no telemetry or Fleet backend. it reads local credentials and contacts
providers directly; authentication flows may refresh those credentials. runtime
state lives in `%LOCALAPPDATA%\Fleet\.cache` (packaged) or `.cache` (source).

for missing or stale readings, check sign-in or key setup, then **Refresh now**.
OpenRouter management keys show account credits; regular keys use a key
budget/spend fallback. see the [provider guide](docs/providers.md#built-in).

[report a bug](https://github.com/okisooo/fleet/issues/new?template=bug_report.yml)
with Fleet version, install type, Windows build, provider, and expected versus
actual behaviour. redact diagnostics; never post tokens, credential files,
`.cache` contents, or provider response bodies. report security issues privately
through [SECURITY.md](SECURITY.md).

this is an early public Windows 11 release. provider APIs and credential formats
can change without notice. Fleet is independent of the listed providers; their
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

diagnostics without opening the flyout:

```powershell
.\.venv\Scripts\python.exe fleet.py --providers
.\.venv\Scripts\python.exe tray.py --dry
```

`--providers` lists API-key configuration; `--dry` polls and prints readings.
see [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## more from OKISO

Fleet is no.02 in OKISO's little tools. no.01, [**Backdrop**](https://github.com/okisooo/backdrop),
adds custom images and animated GIF wallpapers to Claude Desktop, Codex Desktop, and
T3 Code, with blur and readability controls. no.03, [**practice room**](https://github.com/okisooo/NihongoRevisionSuite),
offers short Japanese practice rounds. [more projects by OKISO](https://github.com/okisooo).
