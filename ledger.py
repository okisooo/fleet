"""Token ledger: what local Claude Code and Codex usage would cost at API list prices.

Both CLIs write JSONL session logs. update() reads what was appended since its
last call, a few seconds at a time and newest files first, into SQLite
one-minute buckets of token counts. Dollars are never stored: pricing.py prices
the buckets when they are read, so a rate change re-prices history.
snapshot() turns the buckets into the flyout's figures. Fleet's own limit
readings are kept here too, for a limit's burn in percentage points per hour.

    python ledger.py --report    # index everything, then print today / 7 days / 30 days
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pricing

PROVIDERS = ("claude", "codex")
SCAN_DAYS = 35  # logs untouched for longer are not read
KEEP_DAYS = 40  # buckets, dedupe keys and limit samples are kept this long
CHUNK = 8 << 20
TAIL = 128  # bytes before a cursor that must be unchanged for a file to count as appended to
RATE_MINUTES = 30
RATE_FLOOR = 5  # minutes: a burst that just started is not extrapolated from seconds
SAMPLE_EVERY = 600  # a limit sample is stored at least this often, changed or not
SAME_WINDOW = 600  # reset times this close belong to the same limit window
PRUNE_EVERY = 3600
CODEX_WINDOWS = {300: "5-hour limit", 10080: "Weekly limit"}
CODEX_FAST_TIERS = ("priority", "fast")
_CODEX_KINDS = re.compile(rb'"(?:token_count|turn_context|session_meta|thread_settings_applied)"')

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    provider TEXT NOT NULL, minute INTEGER NOT NULL, model TEXT NOT NULL, project TEXT NOT NULL,
    input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
    cache_write_5m INTEGER NOT NULL DEFAULT 0, cache_write_1h INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0, reasoning INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, minute, model, project)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seen (key INTEGER PRIMARY KEY, minute INTEGER NOT NULL, output INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, provider TEXT NOT NULL, size INTEGER, mtime REAL,
    offset INTEGER NOT NULL, tail BLOB, state TEXT
);
CREATE TABLE IF NOT EXISTS limits (
    provider TEXT NOT NULL, label TEXT NOT NULL, ts REAL NOT NULL, pct REAL NOT NULL, reset_at REAL
);
CREATE INDEX IF NOT EXISTS limits_by_label ON limits (provider, label, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_UPSERT = """
INSERT INTO usage (provider, minute, model, project, input, output, cache_write_5m, cache_write_1h,
                   cache_read, reasoning, messages)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (provider, minute, model, project) DO UPDATE SET
    input = input + excluded.input, output = output + excluded.output,
    cache_write_5m = cache_write_5m + excluded.cache_write_5m,
    cache_write_1h = cache_write_1h + excluded.cache_write_1h,
    cache_read = cache_read + excluded.cache_read, reasoning = reasoning + excluded.reasoning,
    messages = messages + excluded.messages
"""


def claude_dirs():
    """Claude Code's projects folders: CLAUDE_CONFIG_DIR (comma-separated), ~/.config/claude, ~/.claude."""
    homes = [h.strip() for h in os.environ.get("CLAUDE_CONFIG_DIR", "").split(",") if h.strip()]
    homes += [Path.home() / ".config" / "claude", Path.home() / ".claude"]
    return [Path(home) / "projects" for home in homes]


def codex_dirs():
    """Codex's live and archived session folders under CODEX_HOME and ~/.codex."""
    homes = [h for h in (os.environ.get("CODEX_HOME"), Path.home() / ".codex") if h]
    return [Path(home) / sub for home in homes for sub in ("sessions", "archived_sessions")]


def project_name(cwd):
    """A project's display name: the working folder's name; Claude desktop scratch folders share one."""
    text = str(cwd or "").strip()
    if not text:
        return "unknown"
    if "scratch-workspaces" in text:
        return "Claude scratch"
    return re.split(r"[\\/]", text.rstrip("\\/"))[-1] or text


def _epoch(value):
    """Epoch seconds from an ISO timestamp (UTC when unzoned) or epoch s/ms; None if unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e12 else float(value)
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _minute(ts):
    """Buckets start on the minute."""
    return int(ts) // 60 * 60


def _hash(key):
    """Dedupe keys are stored as 64-bit hashes: small, fast, and collisions are negligible."""
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big", signed=True)


@contextlib.contextmanager
def _tx(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


# ---------- log parsing ----------
def _claude_events(lines):
    """Usage events in a block of complete Claude Code log lines."""
    events = []
    at = lines.find(b'"usage"')
    while at >= 0:
        start = lines.rfind(b"\n", 0, at) + 1
        end = lines.find(b"\n", at)
        event = _claude_event(lines[start:end])
        if event:
            events.append(event)
        at = lines.find(b'"usage"', end)
    return events


def _claude_event(line):
    """(dedupe key, minute, model key, project, tokens) for one assistant message, else None.

    tokens = (input, output, cache write 5m, cache write 1h, cache read, reasoning).
    The same message is logged again by streaming chunks and resumed sessions;
    ccusage's key (message id + request id) finds the copies.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage, model = message.get("usage"), message.get("model")
    if not isinstance(usage, dict) or not model or model == "<synthetic>":
        return None
    at = _epoch(entry.get("timestamp"))
    if at is None:
        return None
    message_id, request_id = message.get("id") or entry.get("uuid"), entry.get("requestId")
    key = message_id and f"claude:{message_id}:{request_id or ''}"
    fresh, read = _int(usage.get("input_tokens")), _int(usage.get("cache_read_input_tokens"))
    written = _int(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        write_1h = _int(split.get("ephemeral_1h_input_tokens"))
        write_5m = max(_int(split.get("ephemeral_5m_input_tokens")), written - write_1h)
    else:
        write_1h, write_5m = 0, written  # no breakdown: 5-minute writes
    details = usage.get("output_tokens_details")
    thinking = _int(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    model_key = pricing.key(model, fast=usage.get("speed") == "fast", prompt=fresh + write_5m + write_1h + read)
    tokens = (fresh, _int(usage.get("output_tokens")), write_5m, write_1h, read, thinking)
    return key, _minute(at), model_key, project_name(entry.get("cwd")), tokens


def _codex_events(lines, state, path):
    """(usage events, limit samples) in a block of complete Codex rollout lines.

    state carries the session, model, project and service tier from earlier
    lines (and earlier calls) of the same file.
    """
    events, samples = [], []
    for line in lines.split(b"\n"):
        if not _CODEX_KINDS.search(line, 0, 320):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        kind, sub = entry.get("type"), payload.get("type")
        if kind == "session_meta":
            state["session"] = payload.get("id") or payload.get("session_id") or state.get("session")
            if payload.get("cwd"):
                state["project"] = project_name(payload["cwd"])
            if payload.get("model"):
                state["model"] = payload["model"]
        elif kind == "turn_context":
            if payload.get("model"):
                state["model"] = payload["model"]
            if payload.get("cwd") and not state.get("project"):
                state["project"] = project_name(payload["cwd"])
        elif kind == "event_msg" and sub == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                state["fast"] = settings.get("service_tier") in CODEX_FAST_TIERS
        elif kind == "event_msg" and sub == "token_count":
            at = _epoch(entry.get("timestamp"))
            if at is None:
                continue
            samples += _codex_samples(payload.get("rate_limits"), at)
            event = _codex_event(payload.get("info"), at, state, path)
            if event:
                events.append(event)
    return events, samples


def _codex_event(info, at, state, path):
    """One request's usage from a token_count event's last_token_usage.

    Codex repeats a token_count (same cumulative total) when only the rate
    limits changed; the key, session + cumulative total, drops those repeats
    and is the same whichever file or folder the event is read from.
    input includes cached input; output includes reasoning.
    """
    if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
        return None
    last, total = info["last_token_usage"], info.get("total_token_usage")
    session = state.get("session") or Path(path).stem
    if isinstance(total, dict):
        key = f"codex:{session}:{_int(total.get('input_tokens'))}:{_int(total.get('output_tokens'))}"
    else:
        key = f"codex:{session}:{at}"
    sent, cached = _int(last.get("input_tokens")), _int(last.get("cached_input_tokens"))
    written = _int(last.get("cache_write_input_tokens"))
    model_key = pricing.key(state.get("model") or "unknown", fast=state.get("fast", False), prompt=sent)
    tokens = (max(0, sent - cached - written), _int(last.get("output_tokens")), written, 0, cached,
              _int(last.get("reasoning_output_tokens")))
    return key, _minute(at), model_key, state.get("project") or "unknown", tokens


def _codex_samples(limits, at):
    """Limit samples (label, ts, pct, reset_at) from a token_count's rate_limits."""
    if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
        return []
    samples = []
    for side in ("primary", "secondary"):
        window = limits.get(side)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        minutes = window.get("window_minutes")
        reset = window.get("resets_at")
        if reset is None and window.get("resets_in_seconds") is not None:
            reset = at + _int(window["resets_in_seconds"])
        samples.append((CODEX_WINDOWS.get(minutes) or f"{minutes} min limit", at,
                        float(window["used_percent"]), _epoch(reset) if reset is not None else None))
    return samples


# ---------- storage ----------
def _count(db, provider, events):
    """Add events not seen before to their buckets; return how many were new.

    A message seen again with more output (its first copy was a streaming
    chunk) adds only the extra output tokens.
    """
    batch, new = {}, []
    for key, minute, model, project, tokens in events:
        if not key:
            new.append((minute, model, project, tokens))
            continue
        digest = _hash(key)
        held = batch.get(digest)
        if held is None:
            batch[digest] = (minute, model, project, tokens)
        elif tokens[1] > held[3][1]:
            batch[digest] = (*held[:3], tokens)
    digests, known = list(batch), {}
    for i in range(0, len(digests), 500):
        part = digests[i:i + 500]
        query = f"SELECT key, minute, output FROM seen WHERE key IN ({','.join('?' * len(part))})"
        known.update((k, (m, o)) for k, m, o in db.execute(query, part))
    buckets = defaultdict(lambda: [0] * 7)
    fresh, grown = [], []
    for digest, (minute, model, project, tokens) in batch.items():
        old = known.get(digest)
        if old is None:
            fresh.append((digest, minute, tokens[1]))
            new.append((minute, model, project, tokens))
        elif tokens[1] > old[1]:
            grown.append((tokens[1], digest))
            buckets[(old[0], model, project)][1] += tokens[1] - old[1]
    for minute, model, project, tokens in new:
        bucket = buckets[(minute, model, project)]
        for i, count in enumerate(tokens):
            bucket[i] += count
        bucket[6] += 1
    db.executemany("INSERT INTO seen (key, minute, output) VALUES (?, ?, ?)", fresh)
    db.executemany("UPDATE seen SET output = ? WHERE key = ?", grown)
    db.executemany(_UPSERT, [(provider, *where, *counts) for where, counts in buckets.items()])
    return len(new)


def _same_window(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= SAME_WINDOW


def _add_samples(db, provider, samples):
    """Store limit samples that say something new: the pct moved, the window
    changed, or SAMPLE_EVERY seconds passed since the label's previous sample."""
    last = {}
    for label, ts, pct, reset in sorted(samples, key=lambda s: s[1]):
        prev = last.get(label)
        if prev is None or prev[0] > ts:
            prev = db.execute(
                "SELECT ts, pct, reset_at FROM limits WHERE provider = ? AND label = ? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1", (provider, label, ts)).fetchone()
        if prev is None or pct != prev[1] or ts - prev[0] >= SAMPLE_EVERY or not _same_window(reset, prev[2]):
            db.execute("INSERT INTO limits (provider, label, ts, pct, reset_at) VALUES (?, ?, ?, ?, ?)",
                       (provider, label, ts, pct, reset))
            prev = (ts, pct, reset)
        last[label] = prev


def _save_cursor(db, provider, path, size, mtime, offset, tail, state):
    """size and mtime stay empty until the file has been read to its end."""
    db.execute("INSERT OR REPLACE INTO files (path, provider, size, mtime, offset, tail, state) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)",
               (path, provider, size, mtime, offset, tail, json.dumps(state) if state else None))


def _walk(root):
    """Log files under root: (path, size, mtime), without following links below it."""
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.name.endswith((".jsonl", ".jsonl.zst")):
                    info = entry.stat()
                    yield entry.path, info.st_size, info.st_mtime
            except OSError:
                continue


def _grouped(db, start, end, by="", provider=None):
    """Token sums of the buckets from start to end per (provider, model[, by]):
    rows of (provider, model[, by], input, output, write 5m, write 1h, read, messages, first minute)."""
    extra = f", {by}" if by else ""
    where = "minute >= ? AND minute <= ?" + (" AND provider = ?" if provider else "")
    return db.execute(
        f"SELECT provider, model{extra}, SUM(input), SUM(output), SUM(cache_write_5m), SUM(cache_write_1h), "
        f"SUM(cache_read), SUM(messages), MIN(minute) FROM usage WHERE {where} GROUP BY provider, model{extra}",
        (start, end, provider) if provider else (start, end)).fetchall()


def _priced(model, sums):
    """(usd or None, estimated, totals) of one model's token sums; cost is linear in tokens."""
    fresh, output, write_5m, write_1h, read, messages = sums[:6]
    usd, estimated = pricing.cost(model, fresh, output, write_5m, write_1h, read)
    tokens = fresh + output + write_5m + write_1h + read
    return usd, estimated, (usd or 0.0, tokens, fresh, output, read, write_5m + write_1h, messages)


def _blank():
    return [0.0, 0, 0, 0, 0, 0, 0]  # cost, tokens, input, output, cache_read, cache_write, messages


def _add(total, values):
    for i, value in enumerate(values):
        total[i] += value


def _totals(total):
    cost, tokens, fresh, output, read, write, messages = total
    return {"cost": round(cost, 4), "tokens": tokens, "input": fresh, "output": output,
            "cache_read": read, "cache_write": write, "messages": messages}


class Ledger:
    """Token counts from local logs, priced on read; one SQLite file, safe across threads."""

    def __init__(self, path=None, claude_roots=None, codex_roots=None):
        if path is None:
            import fleet
            path = fleet.CACHE / "ledger.sqlite3"
        self.path = str(path)
        self.claude_roots = list(claude_dirs() if claude_roots is None else claude_roots)
        self.codex_roots = list(codex_dirs() if codex_roots is None else codex_roots)
        self._updating = threading.Lock()
        self._pruned = 0.0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        """A connection per call: the indexer thread and readers never share one."""
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=NORMAL")
            yield db
        finally:
            db.close()

    # ---------- indexing ----------
    def update(self, budget=4.0, now=None):
        """Index appended log lines for about `budget` seconds, newest files first.

        Returns {files, bytes, events, seconds, pending_files, complete, skipped_zst}:
        files opened, bytes of complete lines read, new usage events, wall time,
        files with unread changes left, whether none are left, and compressed
        Codex logs (.jsonl.zst) in the scan window, which are not read.
        """
        started = time.monotonic()
        deadline = started + budget
        now = time.time() if now is None else now
        stats = {"files": 0, "bytes": 0, "events": 0, "seconds": 0.0,
                 "pending_files": 0, "complete": False, "skipped_zst": 0}
        if not self._updating.acquire(blocking=False):
            return stats  # another thread is indexing right now
        try:
            found, stats["skipped_zst"] = self._discover(now)
            with self._db() as db:
                cursors = {row[0]: row[1:] for row in db.execute(
                    "SELECT path, size, mtime, offset, tail, state FROM files")}
                pending = sorted((item for item in found if self._changed(item, cursors.get(item[1]))),
                                 key=lambda item: item[3], reverse=True)
                finished = 0
                for item in pending:
                    if time.monotonic() >= deadline:
                        break
                    if self._read(db, item, cursors.get(item[1]), now, deadline, stats):
                        finished += 1
                stats["pending_files"] = len(pending) - finished
                stats["complete"] = not stats["pending_files"]
                if now - self._pruned >= PRUNE_EVERY:
                    self._prune(db, now, {item[1] for item in found})
                    self._pruned = now
                stats["seconds"] = round(time.monotonic() - started, 3)
                summary = {"at": now, "files": len(found), "pending_files": stats["pending_files"],
                           "complete": stats["complete"], "skipped_zst": stats["skipped_zst"]}
                with _tx(db):
                    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('update', ?)", (json.dumps(summary),))
            return stats
        finally:
            self._updating.release()

    def _discover(self, now):
        """[(provider, path, size, mtime)] of logs touched in the last SCAN_DAYS, and the
        number of .jsonl.zst logs in that span (the stdlib cannot read zstd)."""
        cutoff = now - SCAN_DAYS * 86400
        found, compressed, walked, paths = [], 0, set(), set()
        for provider, roots in (("claude", self.claude_roots), ("codex", self.codex_roots)):
            for root in roots:
                real = os.path.realpath(root)  # ~/.codex/sessions may be a link
                if real in walked or not os.path.isdir(real):
                    continue
                walked.add(real)
                for path, size, mtime in _walk(real):
                    if mtime < cutoff or path in paths:
                        continue
                    if path.endswith(".zst"):
                        compressed += 1
                        continue
                    paths.add(path)
                    found.append((provider, path, size, mtime))
        return found, compressed

    @staticmethod
    def _changed(item, cursor):
        return cursor is None or cursor[0] != item[2] or cursor[1] != item[3]

    def _read(self, db, item, cursor, now, deadline, stats):
        """Index one file from its cursor; True once it has been read to the end.

        A file that shrank, changed without growing, or whose bytes before the
        cursor changed was rewritten rather than appended to: it is read again
        from the start and the dedupe keys keep its old events from counting twice.
        """
        provider, path, size, mtime = item
        try:
            handle = open(path, "rb")
        except OSError:
            return False
        stats["files"] += 1
        oldest = now - KEEP_DAYS * 86400
        with handle:
            offset, tail, state = 0, b"", {}
            if cursor and cursor[2] <= size and size != cursor[0]:
                saved = cursor[3] or b""
                handle.seek(cursor[2] - len(saved))
                if handle.read(len(saved)) == saved:
                    offset, tail, state = cursor[2], saved, json.loads(cursor[4] or "{}")
            handle.seek(offset)
            pieces = []  # the unfinished line, which may span several chunks
            while True:
                block = handle.read(CHUNK)
                if not block:
                    break
                cut = block.rfind(b"\n") + 1
                if not cut:
                    pieces.append(block)
                    continue
                lines = b"".join(pieces + [block[:cut]]) if pieces else block[:cut]
                pieces = [block[cut:]]
                if provider == "claude":
                    events, samples = _claude_events(lines), []
                else:
                    events, samples = _codex_events(lines, state, path)
                offset += cut
                tail = lines[-TAIL:]
                with _tx(db):
                    stats["events"] += _count(db, provider, [e for e in events if e[1] >= oldest])
                    _add_samples(db, provider, [s for s in samples if s[1] >= oldest])
                    _save_cursor(db, provider, path, None, None, offset, tail, state)
                stats["bytes"] += cut
                if time.monotonic() >= deadline:
                    return False
        with _tx(db):
            _save_cursor(db, provider, path, size, mtime, offset, tail, state)
        return True

    def _prune(self, db, now, found):
        cutoff = now - KEEP_DAYS * 86400
        gone = [(path,) for (path,) in db.execute("SELECT path FROM files")
                if path not in found and not os.path.exists(path)]
        with _tx(db):
            db.execute("DELETE FROM usage WHERE minute < ?", (cutoff,))
            db.execute("DELETE FROM seen WHERE minute < ?", (cutoff,))
            db.execute("DELETE FROM limits WHERE ts < ?", (cutoff,))
            db.executemany("DELETE FROM files WHERE path = ?", gone)

    # ---------- reading ----------
    def snapshot(self, now=None, spans=None):
        """Everything the flyout shows, per provider with usage in the last 30 days.

        spans: {provider: {label: start epoch}}, e.g. the current 5-hour
        window's start; each is totalled from its start to now. Days and hours
        are local. rate covers the last 30 minutes, or since the first activity
        in them (at least RATE_FLOOR minutes), and is None when idle.
        """
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now)
        midnight = datetime(local.year, local.month, local.day)
        days = [midnight - timedelta(days=back) for back in range(13, -1, -1)]
        day_starts = [_minute(day.timestamp()) for day in days]
        hours_from = _minute(local.replace(minute=0, second=0, microsecond=0).timestamp()) - 23 * 3600
        starts = {"today": day_starts[-1], "week": _minute(now - 7 * 86400), "month": _minute(now - 30 * 86400),
                  "recent": _minute(now - RATE_MINUTES * 60)}
        span_starts = {(provider, label): _minute(start) for provider, labels in (spans or {}).items()
                       for label, start in (labels or {}).items() if start is not None}
        with self._db() as db:
            periods = {name: _grouped(db, start, now) for name, start in starts.items()}
            span_rows = {key: _grouped(db, start, now, provider=key[0]) for key, start in span_starts.items()}
            by_project = _grouped(db, starts["week"], now, "project")
            # Local hours and days both start on whole hours counted from the first day's midnight.
            by_hour = _grouped(db, day_starts[0], now, f"(minute - {day_starts[0]}) / 3600")
            meta = db.execute("SELECT value FROM meta WHERE key = 'update'").fetchone()
            indexed = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            oldest = db.execute("SELECT MIN(minute) FROM usage").fetchone()[0]
        meta = json.loads(meta[0]) if meta else {}

        acc = defaultdict(lambda: {
            "today": _blank(), "week": _blank(), "month": _blank(), "recent": _blank(), "spans": {},
            "hourly": [0.0] * 24, "daily": [[0.0, 0] for _ in days], "first": None,
            "models": defaultdict(lambda: [0.0, 0]), "projects": defaultdict(float),
            "estimated": False, "unpriced": 0})
        for name, rows in periods.items():
            for provider, model, *sums in rows:
                usd, estimated, values = _priced(model, sums)
                block = acc[provider]
                _add(block[name], values)
                if name == "month":
                    block["unpriced"] += sums[5] if usd is None else 0
                    block["estimated"] |= bool(usd is not None and estimated)
                elif name == "week":
                    held = block["models"][pricing.normalize(model)]
                    held[0] += values[0]
                    held[1] += values[1]
                elif name == "recent":
                    block["first"] = min(sums[6], block["first"] or sums[6])
        for (provider, label), rows in span_rows.items():
            total = acc[provider]["spans"].setdefault(label, _blank())
            for _, model, *sums in rows:
                _add(total, _priced(model, sums)[2])
        for provider, model, project, *sums in by_project:
            acc[provider]["projects"][project] += _priced(model, sums)[2][0]
        for provider, model, hour, *sums in by_hour:
            start, (usd, tokens) = day_starts[0] + hour * 3600, _priced(model, sums)[2][:2]
            day = acc[provider]["daily"][bisect.bisect_right(day_starts, start) - 1]
            day[0] += usd
            day[1] += tokens
            if start >= hours_from:
                acc[provider]["hourly"][min(23, (start - hours_from) // 3600)] += usd

        out = {}
        total = {name: _blank() for name in ("today", "week", "month")}
        for provider in sorted(acc, key=lambda p: (PROVIDERS.index(p) if p in PROVIDERS else len(PROVIDERS), p)):
            block = acc[provider]
            if not block["month"][1]:
                continue
            for name in total:
                _add(total[name], block[name])
            week_cost = block["week"][0]
            models = sorted(((cost, tokens, model) for model, (cost, tokens) in block["models"].items() if tokens),
                            reverse=True)
            projects = sorted(block["projects"].items(), key=lambda item: item[1], reverse=True)[:20]
            out[provider] = {
                "today": _totals(block["today"]),
                "week": _totals(block["week"]),
                "month": _totals(block["month"]),
                "spans": {label: _totals(values) for label, values in block["spans"].items()},
                "rate": self._rate(block["recent"], block["first"], now),
                "hourly": [round(cost, 4) for cost in block["hourly"]],
                "daily": [{"day": day.date().isoformat(), "cost": round(cost, 4), "tokens": tokens}
                          for day, (cost, tokens) in zip(days, block["daily"])],
                "models": [{"id": model, "name": pricing.label(model), "cost": round(cost, 4),
                            "share": round(cost / week_cost, 4) if week_cost else 0.0,
                            "tokens": tokens}
                           for cost, tokens, model in models],
                "projects": [{"name": name, "cost": round(cost, 4)} for name, cost in projects],
                "estimated": block["estimated"],
                "unpriced": block["unpriced"],
            }
        out["total"] = {name: _totals(values) for name, values in total.items()}
        out["complete"] = bool(meta.get("complete"))
        out["coverage"] = {"files": indexed, "pending_files": meta.get("pending_files"),
                           "skipped_zst": meta.get("skipped_zst"), "updated": meta.get("at"), "since": oldest}
        return out

    @staticmethod
    def _rate(recent, first, now):
        cost, tokens = recent[0], recent[1]
        if not tokens or first is None:
            return None
        minutes = min(RATE_MINUTES, max(RATE_FLOOR, (now - first) / 60))
        return {"cost_per_hour": round(cost / minutes * 60, 4), "tokens_per_min": round(tokens / minutes, 1),
                "minutes": round(minutes, 1)}

    # ---------- limit history ----------
    def record_limits(self, provider, rows, ts=None):
        """Keep Fleet's limit readings. rows: [{"label", "pct", "reset_at"}]; a sample is
        stored when the pct moved, the window changed, or 10 minutes passed."""
        ts = time.time() if ts is None else float(ts)
        samples = [(str(row["label"]), ts, float(row["pct"]),
                    _epoch(row["reset_at"]) if row.get("reset_at") is not None else None)
                   for row in rows or [] if row.get("label") and row.get("pct") is not None]
        if samples:
            with self._db() as db, _tx(db):
                _add_samples(db, provider, samples)

    def limit_series(self, provider, label, start, end):
        """[(ts, pct)] of a limit's samples between start and end, oldest first."""
        with self._db() as db:
            return [(ts, pct) for ts, pct in db.execute(
                "SELECT ts, pct FROM limits WHERE provider = ? AND label = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (provider, label, start, end))]

    def limit_rate(self, provider, label, now=None, minutes=60):
        """Percentage points per hour over the last `minutes`, from samples of the current
        window (same reset time); None with under two samples or five minutes between them."""
        now = time.time() if now is None else now
        with self._db() as db:
            rows = db.execute(
                "SELECT ts, pct, reset_at FROM limits WHERE provider = ? AND label = ? AND ts >= ? AND ts <= ? "
                "ORDER BY ts", (provider, label, now - minutes * 60, now)).fetchall()
        if not rows:
            return None
        window = rows[-1][2]
        rows = [row for row in rows if _same_window(row[2], window)]
        if len(rows) < 2 or rows[-1][0] - rows[0][0] < 300:
            return None
        return (rows[-1][1] - rows[0][1]) / (rows[-1][0] - rows[0][0]) * 3600


# ---------- report ----------
def _money(usd):
    return f"${usd:,.2f}"


def _short(count):
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if count >= size:
            return f"{count / size:.1f}{unit}"
    return str(count)


def report(snap):
    """A compact plain-text table of a snapshot, for sanity checks."""
    lines = [f"{'':8}{'today':>24}{'7 days':>24}{'30 days':>24}"]
    for name in [p for p in snap if p not in ("total", "complete", "coverage")] + ["total"]:
        block = snap[name]
        cells = [f"{_money(block[k]['cost'])} / {_short(block[k]['tokens'])} tok" for k in ("today", "week", "month")]
        lines.append(f"{name:8}" + "".join(f"{cell:>24}" for cell in cells))
        if name == "total":
            continue
        models = ", ".join(f"{m['name']} {_money(m['cost'])} ({m['share']:.0%})" for m in block["models"][:4])
        projects = ", ".join(f"{p['name']} {_money(p['cost'])}" for p in block["projects"])
        lines.append(f"{'':8}7d models: {models or '-'}")
        lines.append(f"{'':8}7d projects: {projects or '-'}")
        rate = block["rate"]
        burn = (f"{_money(rate['cost_per_hour'])}/h, {_short(int(rate['tokens_per_min']))} tok/min "
                f"over {rate['minutes']:g} min" if rate else "idle")
        notes = [burn] + (["some rates estimated"] if block["estimated"] else []) + (
            [f"{block['unpriced']} unpriced messages"] if block["unpriced"] else [])
        lines.append(f"{'':8}burn: " + "; ".join(notes))
    cover = snap["coverage"]
    lines.append(f"complete: {snap['complete']}; {cover['files']} files indexed; "
                 f"{cover['skipped_zst']} compressed Codex logs not read")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index local Claude Code and Codex logs; print API-equivalent cost.")
    parser.add_argument("--report", action="store_true", help="index until complete, then print today / 7d / 30d")
    parser.add_argument("--db", help="ledger file (default: Fleet's cache folder)")
    parser.add_argument("--budget", type=float, default=4.0, help="seconds of indexing per update() call")
    args = parser.parse_args(argv)
    if not args.report:
        parser.print_help()
        return 0
    ledger = Ledger(args.db)
    calls, seconds, read, events = 0, 0.0, 0, 0
    while True:
        stats = ledger.update(args.budget)
        calls += 1
        seconds += stats["seconds"]
        read += stats["bytes"]
        events += stats["events"]
        print(f"update {calls}: {stats['files']} files, {stats['bytes'] / 1e6:,.0f} MB, {stats['events']:,} events "
              f"in {stats['seconds']:.1f}s; {stats['pending_files']} files pending", flush=True)
        if stats["complete"] or not stats["files"]:
            break
    speed = read / seconds / 1e6 if seconds else 0.0
    print(f"indexed {read / 1e9:.2f} GB and {events:,} events in {seconds:.1f}s over {calls} calls ({speed:,.0f} MB/s)")
    started = time.perf_counter()
    snap = ledger.snapshot()
    print(f"snapshot built in {(time.perf_counter() - started) * 1000:.0f} ms")
    print(report(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
