# contributing

Fleet is a small Windows-only tray app. Issues and pull requests are welcome.

## before you file an issue

Include your Windows build, which provider clients are installed and signed in,
and what the tray or flyout showed. Run `.\.venv\Scripts\python.exe tray.py --dry`
and paste the output.

Never paste tokens, credential files, `.cache` contents, or provider response
bodies into an issue. See [SECURITY.md](SECURITY.md) for private reporting.

## development setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`tray.py --dry` prints one line per provider without opening any window, which is
the fastest way to check a data change. `fleet.py --debug` prints status codes and
exception types only; it must never print tokens or response bodies.
`fleet.py --providers` shows which API-key providers are wired up.

## adding a provider

Most providers need no Python: they are one spec in `apikeys.py` — a URL, the
environment variable holding the key, and dotted paths to the numbers. See
[docs/providers.md](docs/providers.md) for the mapping language, and add a test
with a recorded payload rather than a live call.

Rules for a new built-in:

- Only documented endpoints, or clearly marked experimental ones. No scraping a
  logged-in console session, and no cookies.
- If reading usage costs money, it belongs behind `probe`, off by default.
- Keys come from environment variables or the current user's Windows Credential
  Manager. Never read secrets from config or write them into JSON, logs or caches.

## pull requests

- Keep changes focused, and add a test next to the behaviour you change.
- The full suite must pass: `python -m unittest discover -s tests -v`.
- Do not commit `.cache/`, build output, or screenshots of real usage data.
- Provider artwork must come from a documented provider-owned source and include
  provenance plus the ownership/no-endorsement notice in
  `THIRD_PARTY_NOTICES.md`.
- Match the surrounding style: short modules, comments that explain *why* a
  Windows or provider quirk forced the code, not what the line does.

## building a release locally

```powershell
.\scripts\build-release.ps1 -Version 0.3.0
```

This requires a .NET SDK as well as Python. It creates an isolated build
environment under the scratch directory, builds `dist\Fleet\Fleet.exe` and
`FleetPanel.exe`, and writes `dist\Fleet-0.3.0-portable.zip`. The whole `Fleet`
directory is required. Use `-ScratchRoot` and `-OutputRoot` to choose physical
storage; `-BuildVenv` can select an existing build environment.

Building the installer additionally needs Inno Setup 6:

```powershell
$env:FLEET_VERSION = "0.3.0"
$env:FLEET_APP_ICON = "<scratch directory>\assets\fleet.ico"
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\Fleet.iss
```

The **release build** workflow runs when a `v*` tag is pushed. It creates a draft
with the installer, portable ZIP and `SHA256SUMS.txt`. Verify the workflow,
contents, version and digests before publishing the draft; do not substitute
hand-built binaries. Keep the README, provider guide, changelog, screenshots,
repository description and topics consistent with the shipped behavior.
