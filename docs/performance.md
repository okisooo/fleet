# panel performance checks

Fleet keeps its existing native WPF panel, controls, fonts, themes and collectors.
The panel reuses frozen brushes, keeps its controls on timestamp-only model
updates, and watches cache-file notifications with a one-second recovery timer.
The normal tray-window message path is unchanged. File notifications are debounced
so creating and writing a request does not toggle the popup twice.

Live glass still captures up to 60 frames per second. Its UI handoff now runs only
at that rate, instead of subscribing continuously to display-rate rendering.
Entrance and exit animations retain their display-rate animation. Closing stops
and joins the capture worker; closing the window disposes its watcher, timers and
capture resources. Error logging keeps two bounded files and omits exception
messages that could contain input.

## reproduce

Use a synthetic `panel.json` generated with `panel_model.build`, including
`summary`, `coverage`, `api_accounts` and dashboard history. Do not copy credentials
or a user's raw logs into a fixture. Install `pythonnet` and `psutil` in a test
environment. Both checks require Windows; neither collects provider data.

```powershell
python scripts/measure-panel.py PANEL_EXE SAMPLE_JSON OUTPUT_DIR 3
python scripts/check-panel-performance.py PANEL_EXE SAMPLE_JSON OUTPUT_DIR
python scripts/check-dashboard.py PANEL_EXE SAMPLE_JSON OUTPUT_DIR
```

Use different output directories for each binary and run. Keep all generated
artifacts, virtual environments, package caches, TEMP and TMP on the requested
scratch/output drive. Do not run performance checks concurrently.

`measure-panel.py` starts real standalone panel processes with isolated caches,
samples fresh-hidden, open and closed-after-open states, then requests graceful
exit. Its windows stay offscreen and use the real open/close and capture paths.
It reports private resident memory (USS), total working set, private commit,
process CPU time, handles and threads separately. CPU time is per process, not a
percentage of every logical processor.

`check-panel-performance.py` hosts the same assembly in Python/CLR to exercise 100
timestamp-only refreshes and 100 open/close/navigation cycles. It checks that
changed content still replaces controls, and that coverage, settings, summary and
account changes are not lost. Its memory samples include Python/CLR test overhead
and must not be presented as the standalone app's footprint. Managed allocation
totals include the host's other managed work. No check forces garbage collection
or trims the working set.

## acceptance and limits

Compare identical fixtures, display settings, timing and process age. Separate
startup memory from the memory after interaction. Check late-cycle ranges for
private resident memory, working set, private commit, kernel handles, GDI objects
and threads. A short plateau is evidence for that workload, not a guarantee about
multi-day behavior.

For appearance, render each binary with `--render` against the same synthetic
model and background. Compare usage, dashboard, settings and expanded-provider
pages in both themes. Static pixel parity does not by itself prove animation,
multi-monitor, or live-background parity; use the dashboard interaction check and
a real desktop smoke check as well.

Unit tests should not depend on a developer's real Credential Manager entries.
An unisolated provider-enablement test can discover an already-connected admin
account. For reproducible local tests, mock `api_credentials.read` to return
`None` around test discovery/execution; credential-specific tests use their own
mocks. Never clear real credentials to make a test pass.
