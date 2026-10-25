# providers

Fleet shows two kinds of provider:

- **local clients** — Claude Code, Codex, Grok, Antigravity. Detected automatically
  from the CLI/app state already on the machine. Nothing to configure.
- **public API keys** — an environment variable, or a Claude key saved through
  Settings. Each adds its own row to the flyout.

Anything Fleet does not ship can be added as a **custom entry**, which uses the
same engine: one HTTPS GET, and dotted paths that pick the numbers out of the
response.

## quick start

the native **Dashboard** button opens a larger view of the same provider readings.
it keeps cached and unavailable states visible and expands every reported limit,
including rows condensed in the tray flyout. local activity charts, model/project
breakdowns and token counts come only from Claude Code and Codex logs; they are
API-price estimates. organization spending and promotional credits remain separate.

Set the variable for a provider you use, then choose **Refresh now** in Fleet:

```powershell
setx OPENROUTER_API_KEY "sk-or-..."
```

Check what Fleet sees:

```powershell
.\.venv\Scripts\python.exe fleet.py --providers
```

Keys come from environment variables or, for Claude API accounts, Windows
Credential Manager. Fleet never puts keys in JSON, caches or logs. Config holds
variable *names*, never secrets. Environment variables take precedence over saved keys.

### Claude API accounts

**Open billing** in Settings works without saving a key. Use it to check the
included Max/Team credit balance directly in Console.

Open **Settings → Claude API**, choose **Connect** beside **API key** or **Admin
key**, paste the key into the hidden field, and choose **Save key**. **Change**
replaces a saved key; **Remove** removes only Fleet's saved credential. The key
stays in the current Windows user's Credential Manager (`Fleet/api/anthropic`
or `Fleet/api/anthropic-admin`). Fleet refreshes after saving or removing it.

- A regular key uses a free `GET /v1/models` connection check. **Open billing**
  opens the Claude Console to view prepaid credits. No inference request is sent.
- An organization billing credential shows month-to-date and today's spend, in
  UTC days, for the whole organization. Amounts are converted from decimal cents
  and summed across the complete paginated report. Empty reports show $0.00.
- [Anthropic's cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api)
  reports spend, not prepaid credit balance, and excludes Priority Tier costs.
  Reports usually lag by five minutes and can take longer. Fleet polls every five
  minutes, or when you choose **Refresh now**. Individual accounts without Admin
  API access cannot use organization reports.
- The supported billing credential is an admin key, or another credential with
  organization billing access. A workspace inference key cannot read reports.
- These figures do not enter the local **Cost at API prices** band and do not
  replace the Claude Code subscription quota. Fleet does not invent a remaining
  credit balance or infer a budget from spending.
- [Monthly credits included with Max and Team](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)
  appear under **Promotional credits** in Console billing, with their balance and
  expiry. They renew on your plan's billing date, expire without rollover, and
  are shared across keys in the linked organization. They are separate from both
  Claude Code limits and the calendar-month spend report. Fleet does not subtract
  calendar-month spending from a plan's allowance to claim a remaining balance.

Existing `ANTHROPIC_API_KEY` and `ANTHROPIC_ADMIN_KEY` environment variables also
work. If both are present, Fleet shows the admin billing report instead of a
duplicate regular-key connection row. An explicitly enabled paid probe remains
separate and opt-in.

### Antigravity when closed

Fleet first asks a running Antigravity language server for quota. When the app is
closed, Fleet reads the existing local OAuth state and calls Google's Code Assist
quota service directly. If the access token has expired, Fleet discovers OAuth
client metadata from an installed Antigravity executable, refreshes the token
through Google's token endpoint, and keeps only the refreshed access token in
Fleet's cache. Client metadata is not copied into the repository or persisted by
Fleet. If Antigravity is not installed, its refresh metadata is unavailable, or
Google revokes the refresh token, Fleet shows the last cached reading and explains
that reconnecting Antigravity is required.

## built in

Free usage endpoints — enabled as soon as the variable exists:

| id | variable | shows |
| --- | --- | --- |
| `openrouter` | `OPENROUTER_API_KEY` | account credit balance with a management key; key budget/spend fallback with a regular key |
| `deepseek` | `DEEPSEEK_API_KEY` | account balance |
| `moonshot` | `MOONSHOT_API_KEY` or `KIMI_API_KEY` | available balance |
| `minimax` | `MINIMAX_API_KEY` | coding-plan prompts used, window reset |
| `zai` | `ZAI_API_KEY`, `ZHIPUAI_API_KEY` or `GLM_API_KEY` | token quota and call quota |
| `litellm` | `LITELLM_API_KEY` + `LITELLM_BASE_URL` | key spend against its budget |
| `openai-admin` | `OPENAI_ADMIN_KEY` | organization cost, last 24h |
| `anthropic` | `ANTHROPIC_API_KEY` or saved key | free connection check and console billing link |
| `anthropic-admin` | `ANTHROPIC_ADMIN_KEY` or saved key | organization spend this month and today (UTC) |

Admin entries need organization billing access; workspace inference keys cannot
read billing reports. Use the Admin key field for Claude billing credentials.

`zai` uses a console endpoint that is not in Z.AI's public API reference. It can
change without notice; Fleet falls back to the last cached reading when it does.

## rate-limit probes (opt-in, costs a token)

The remaining providers publish no free usage endpoint. The only way to see their
quota is to send a real request and read the rate-limit headers on the response.
Fleet will not do that unless asked, because it spends money on your account and
shows up in that provider's logs.

Providers: `openai`, `anthropic`, `groq`, `mistral`, `together`, `fireworks`,
`cerebras`, `deepinfra`, `nebius`, `xai`, `perplexity`
(variables `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, and so on).

Enable one in `providers.json`:

```json
{
  "providers": {
    "groq": { "probe": true },
    "openai": { "probe": true, "model": "gpt-4o-mini" }
  }
}
```

Each poll sends one `max_tokens: 1` request — a fraction of a cent at current
prices. Automatic polling is paced per provider; Claude API checks use five minutes.
`model` overrides the model
Fleet asks for; use it when the default has been retired on your account.

## config file

| build | location |
| --- | --- |
| installer or portable | `%LOCALAPPDATA%\Fleet\providers.json` |
| source checkout | `providers.json` next to `fleet.py` |

The file is optional. A syntax error disables the file and leaves local providers
working; `fleet.py --providers` prints the reason.

```json
{
  "providers": {
    "openrouter": { "enabled": false },
    "deepseek": { "env": "WORK_DEEPSEEK_KEY" },
    "groq": { "probe": true }
  },
  "custom": []
}
```

- `enabled: false` hides a provider whose variable is set.
- `env` points a provider at a different variable name.
- `probe: true` opts into a paid rate-limit probe.
- `base_url` overrides the endpoint host for self-hosted deployments.

## custom entries

A custom entry is one GET request plus a mapping. Example — an OpenAI-compatible
gateway that reports spend at `/usage`:

```json
{
  "custom": [
    {
      "id": "gateway",
      "label": "Gateway",
      "url": "https://gateway.example.com/usage",
      "env": "GATEWAY_API_KEY",
      "auth": "bearer",
      "metrics": [
        { "label": "Budget", "kind": "pct", "used": "spend.month", "limit": "budget.month",
          "resets": "budget.renews_at" },
        { "label": "Balance", "kind": "money", "value": "credits.remaining",
          "currency_literal": "USD" }
      ]
    }
  ]
}
```

Entry fields:

| field | meaning |
| --- | --- |
| `id` | stable identifier, used for the cache file |
| `label` | name shown in the flyout (keep it short) |
| `url` | full HTTPS URL, requested with GET |
| `env` | variable holding the key; omit with `"auth": "none"` |
| `auth` | `bearer` (default), `x-api-key`, or `none` |
| `headers` | extra static headers, e.g. an API version |
| `plan` | path to a plan/tier string shown next to the name |
| `metrics` | one or more mappings, below |
| `allow_http_loopback` | allow `http://127.0.0.1:…` for a proxy you run yourself |

Metric kinds:

```json
{ "label": "Usage",   "kind": "pct",   "pct": "quota.used_percent", "resets": "quota.reset_at" }
{ "label": "Budget",  "kind": "pct",   "used": "spend", "limit": "budget" }
{ "label": "Left",    "kind": "pct",   "remaining": "quota.left", "limit": "quota.total" }
{ "label": "Prompts", "kind": "count", "used": "calls.used", "limit": "calls.total" }
{ "label": "Balance", "kind": "money", "value": "credits", "currency": "currency" }
```

- `pct` values are clamped to 0–100. Add `"pct_scale": 100` for a 0–1 fraction,
  or `"pct_is_remaining": true` when the number counts down instead of up.
- `money` takes `currency` (a path) or `currency_literal` (a fixed code).
- `resets` accepts RFC 3339 strings or epoch seconds/milliseconds.
- A mapping that finds nothing is dropped rather than displayed as zero.

Path syntax is deliberately small:

| form | meaning |
| --- | --- |
| `a.b.c` | nested objects |
| `items[0].value` | list index |
| `data[type=TOKENS_LIMIT].percentage` | first list item whose `type` equals `TOKENS_LIMIT` |

## what Fleet refuses to do

- Send a key anywhere except the URL you configured for it.
- Call a custom entry over plain HTTP, unless it is loopback and the entry sets
  `allow_http_loopback`.
- Read a response larger than 1 MB, or follow a usage endpoint that returns
  something other than JSON.
- Print a key, an `Authorization` header, or a response body — `--debug` prints
  status codes and exception types only.

## what is not supported

- **Google Gemini API keys**: no usage or quota endpoint, and no rate-limit
  headers to read. Antigravity covers Google usage on a local install.
- **Console-only dashboards** (Groq, Mistral, Grok web): their usage pages need a
  browser session cookie rather than an API key. Fleet does not scrape logged-in
  sessions.
