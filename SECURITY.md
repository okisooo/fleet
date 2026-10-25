# security policy

## supported branch

Security fixes land on the default branch.

## reporting a vulnerability

Do not open a public issue for a suspected credential leak, token disclosure,
or local privilege problem. Contact the repository owner privately through
[the published contact page](https://okiso.net/links), with a minimal
reproduction and affected version.

Do not include live tokens, credential files, `.cache` contents, browser data,
or provider session logs in a report.

## local data

Fleet handles provider credentials only on the local machine. The `.cache/`
directory is runtime state and must remain untracked. If it is ever committed,
revoke affected tokens immediately and remove the secret from Git history.

Claude API credentials entered in Settings are stored in Windows Credential
Manager under `Fleet/api/anthropic` or `Fleet/api/anthropic-admin`, scoped to the
signed-in Windows user. Existing environment-variable credentials take precedence.
Fleet does not copy keys into settings, provider configuration, panel/state JSON,
logs, or the local usage bridge. Removing a saved key does not revoke it at
Anthropic or remove an environment variable.

Organization admin credentials are privileged. Fleet uses them only for read-only
cost reports at Anthropic's official API; it does not manage the organization,
purchase credits, or send inference requests with them. Regular keys use a free
models-list request unless a paid rate-limit probe was explicitly enabled.
