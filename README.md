# fleet

small Windows 11 tray utility for viewing quota and usage windows from local
Claude Code, Codex, Grok, and Antigravity installations.

Fleet stays in the notification area, cycles provider status in its icon, and
opens a compact flyout with live/cached freshness, usage bars, and reset times.

## status

Windows-only experimental project. It works with locally installed provider
clients; vendor endpoints and response formats can change without notice.

Fleet is an independent project. It is not affiliated with Anthropic, OpenAI,
xAI, Google, Claude Code, Codex, Grok, or Antigravity.

## install

Requirements: Windows 11, Python 3.13+, and whichever provider CLIs you want to
inspect.

```powershell
git clone <your-repository-url> fleet
cd fleet
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\fleet-tray.cmd
```

Left-click the tray icon to toggle the flyout. Right-click for refresh and quit.

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

The project deliberately uses original provider monograms instead of vendor
logos, so the repository does not redistribute third-party artwork.

## security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
