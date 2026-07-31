# security policy

## supported branch

Security fixes land on the default branch.

## reporting a vulnerability

Do not open a public issue for a suspected credential leak, token disclosure,
or local privilege problem. Contact the repository owner privately with a
minimal reproduction and affected version.

Do not include live tokens, credential files, `.cache` contents, browser data,
or provider session logs in a report.

## local data

Fleet handles provider credentials only on the local machine. The `.cache/`
directory is runtime state and must remain untracked. If it is ever committed,
revoke affected tokens immediately and remove the secret from Git history.
