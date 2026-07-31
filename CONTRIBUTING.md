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

## pull requests

- Keep changes focused, and add a test next to the behaviour you change.
- The full suite must pass: `python -m unittest discover -s tests -v`.
- Do not commit `.cache/`, build output, screenshots of real usage data, or
  vendor logo artwork. Provider marks are original monograms on purpose.
- Match the surrounding style: short modules, comments that explain *why* a
  Windows or provider quirk forced the code, not what the line does.

## building a release locally

```powershell
.\scripts\build-release.ps1 -Version 0.1.0
```

That creates `.build-venv`, generates the app icon and version resource, builds
`dist\Fleet.exe`, and writes a portable ZIP. Building the installer additionally
needs Inno Setup 6:

```powershell
$env:FLEET_VERSION = "0.1.0"
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\Fleet.iss
```

Releases are published by the **release build** workflow when a `v*` tag is
pushed. Maintainers should not upload hand-built binaries.
