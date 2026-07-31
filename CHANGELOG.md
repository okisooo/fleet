# changelog

## unreleased

- fix: a portable build no longer fails to start when `%LOCALAPPDATA%\Fleet`
  does not exist yet.
- fix: the CI secret scan now fails the build on a finding instead of only
  printing one.
- fix: release artifacts use the bare version number for both tag and manual
  builds, so `v0.1.0` and `0.1.0` produce the same file names.
- add: packaged builds carry a generated app icon and Windows version details.
- add: the installer waits for a running tray, and removes cached runtime state
  on uninstall.
- add: `requirements-build.txt` pins the packaging toolchain.

## 0.1.0

First public release: tray icon, WPF flyout, Claude Code / Codex / Grok /
Antigravity readings, portable ZIP and per-user installer.
