#!/usr/bin/env python3
"""Incremental sync of the production server's ``logs/`` and ``data/`` trees.

Driver half of the sync; the worker half is :mod:`scripts.remote_sync_agent`,
which this module uploads to the remote host on every run. Normally invoked
through ``scripts/Fetch-RemoteData.ps1``.

What it fixes
-------------
The previous implementation was ``scp -r logs data``: it re-downloaded all
~6.8 GB every run (~4 minutes at best), and it copied the live SQLite databases
byte-for-byte while the bot was writing them, so the local copies were
regularly torn and unreadable.

This driver instead:

1. **Inventories the remote once** (one SSH round trip) and transfers only what
   is genuinely new or changed -- compared on size and mtime, so a file we
   already hold identically costs nothing.
2. **Resumes append-only logs**, sending just the bytes past what we already
   have once the remote confirms our copy is still a byte-exact prefix.
3. **Never copies a live database file.** It asks SQLite on the server for a
   transactionally consistent snapshot (online backup API) and transfers that,
   verified end-to-end by digest. ``-wal`` / ``-shm`` sidecars are never
   transferred, and stale local ones are removed rather than left to be
   replayed against a fresh snapshot.
4. **Skips unchanged databases entirely** using a remembered fingerprint
   (size + mtime of the DB and its sidecars), and when one *has* changed sends
   only the 4 MiB blocks that actually differ from the copy we hold.

State lives in ``.remote-sync-state.json`` at the repo root. Deleting it (or
passing ``--full``) forces a complete, verified refresh.

Exit codes: ``0`` success, ``1`` any failure (unreachable host, missing
credentials, failed verification). Partial progress is kept and reported.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

try:                                        # imported as scripts.remote_sync
    from scripts import remote_sync_agent as agent
except ImportError:                         # run as scripts/remote_sync.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import remote_sync_agent as agent       # type: ignore[no-redef]

STATE_FILE = ".remote-sync-state.json"
STATE_VERSION = 2
DEFAULT_DIRS = ("logs", "data")
TAIL_SUFFIXES = (".log", ".jsonl", ".ndjson", ".txt", ".csv", ".out", ".err")
MTIME_TOLERANCE = 2.0                       # seconds; FS/transport granularity
REMOTE_TMP_PREFIX = "/tmp/meteoedge-sync-"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    # Fail fast when the host is unreachable rather than hanging: health-triage
    # sessions call this at startup and must not stall for minutes.
    "-o", "ConnectTimeout=10",
    # ...but do not drop a long snapshot/transfer that is still making progress.
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=10",
]


class SyncError(Exception):
    """Fatal, user-facing sync failure."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def parse_env_file(path):
    """Parse ``KEY=value`` lines, tolerating CRLF, ``export`` and quotes."""
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip().lstrip("﻿")
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def expand_home(path):
    if path.startswith("~"):
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return home + path[1:]
    return path


def split_patterns(value):
    if not value:
        return []
    out = []
    for chunk in value.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def matches_any(rel, patterns):
    """fnmatch ``rel`` against repo-relative patterns, bare names included.

    ``data/*.bak*``, ``cryptoedge.db`` and ``logs/*`` all work.
    """
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def load_config(project_root, env_file):
    env_path = env_file if os.path.isabs(env_file) else os.path.join(project_root, env_file)
    if not os.path.isfile(env_path):
        raise SyncError(
            "%s not found.\n       Create one by copying .env.example and "
            "filling in the REMOTE_* values." % env_path)
    values = parse_env_file(env_path)
    missing = [key for key in ("REMOTE_HOST", "REMOTE_USER", "REMOTE_KEY_PATH",
                               "REMOTE_PROJECT_ROOT")
               if not values.get(key)]
    if missing:
        raise SyncError("%s is not set in %s" % (", ".join(missing), env_path))
    key_path = expand_home(values["REMOTE_KEY_PATH"])
    if not os.path.isfile(key_path):
        raise SyncError("SSH key not found at %s" % key_path)
    return {
        "host": values["REMOTE_HOST"],
        "user": values["REMOTE_USER"],
        "key": key_path,
        "root": values["REMOTE_PROJECT_ROOT"].rstrip("/"),
        "dirs": split_patterns(values.get("REMOTE_SYNC_DIRS")) or list(DEFAULT_DIRS),
        "exclude": split_patterns(values.get("REMOTE_SYNC_EXCLUDE")),
        "db_method": (values.get("REMOTE_SYNC_DB_METHOD") or "backup").strip().lower(),
    }


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def load_state(path, host, root):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    # A state file describing a different server tells us nothing about this one.
    if (state.get("version") != STATE_VERSION or state.get("host") != host
            or state.get("root") != root):
        state = {"version": STATE_VERSION, "host": host, "root": root, "dbs": {}}
    state.setdefault("dbs", {})
    return state


def save_state(path, state):
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# planning helpers (pure; unit-tested in src/tests/test_remote_sync.py)
# --------------------------------------------------------------------------- #
def mtime_close(a, b, tol=MTIME_TOLERANCE):
    return abs(float(a) - float(b)) <= tol


def is_tail_candidate(rel):
    """Append-only text logs can be resumed; compressed/rotated ones cannot."""
    return rel.lower().endswith(TAIL_SUFFIXES)


def safe_rel(rel):
    """Reject anything that would escape the project root when joined."""
    if not rel or rel.startswith(("/", "\\")) or ":" in rel:
        return False
    parts = rel.replace("\\", "/").split("/")
    return ".." not in parts and "" not in parts


def plan_regular(rel, remote_size, remote_mtime, local_path, force=False):
    """Decide what to do with one non-database file.

    Returns ``("skip"|"maybe_same"|"maybe_tail"|"full", extra_dict)``:

    ``skip``
        Same size, same mtime: we already hold it. Costs one ``stat``.
    ``maybe_same``
        Same size but an mtime we cannot vouch for -- the remote compares
        digests and only sends the file if it really differs. This is what keeps
        the ~750 rotated archives in ``logs/`` from being re-fetched the first
        time the new sync runs against copies the old ``scp`` left undated.
    ``maybe_tail``
        The remote file is longer; if our copy is still a byte-exact prefix the
        remote sends only the appended bytes.
    ``full``
        Anything else.
    """
    try:
        st = os.stat(local_path)
    except OSError:
        return "full", {}
    if force:
        return "full", {}
    if st.st_size == remote_size:
        if mtime_close(st.st_mtime, remote_mtime):
            return "skip", {}
        return "maybe_same", {"local_size": st.st_size,
                              "local_digest": agent.file_digest(local_path)}
    if is_tail_candidate(rel) and remote_size > st.st_size > 0:
        return "maybe_tail", {"local_size": st.st_size,
                              "local_digest": agent.file_digest(local_path,
                                                                limit=st.st_size)}
    return "full", {}


def db_is_current(remote_fp, entry, local_path):
    """True when the local snapshot still corresponds to this remote database.

    Requires both that the remote fingerprint (size + mtime of the DB and its
    ``-wal``/``-shm``) is unchanged since we last fetched it, and that the local
    file is still exactly the snapshot we wrote.
    """
    if not entry or entry.get("fp") != remote_fp:
        return False
    try:
        st = os.stat(local_path)
    except OSError:
        return False
    return (st.st_size == entry.get("local_size")
            and mtime_close(st.st_mtime, entry.get("local_mtime", -1)))


def human(num_bytes):
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
class Remote(object):
    def __init__(self, cfg, verbose=True):
        self.cfg = cfg
        self.verbose = verbose
        self.target = "%s@%s" % (cfg["user"], cfg["host"])
        self.python = "python3"
        self.tmp = None
        self.agent_path = None

    # -- low level ---------------------------------------------------------- #
    def _run(self, argv, what, stream=False):
        """Run a child process, optionally echoing its ``# progress`` lines live.

        Staging a first-run set of database snapshots keeps the remote busy for
        minutes; streaming keeps that from looking like a hang.
        """
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, encoding="utf-8",
                                    errors="replace", bufsize=1)
        except FileNotFoundError:
            raise SyncError("%s not found on PATH (needs Windows OpenSSH)" % argv[0])
        chunks = []
        for line in proc.stdout:
            chunks.append(line)
            if stream and self.verbose and line.startswith("# "):
                print("  " + line[2:].rstrip(), flush=True)
        stdout = "".join(chunks)
        stderr = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        if proc.wait() != 0:
            detail = (stderr or stdout or "").strip().splitlines()
            raise SyncError("%s failed (exit %d)%s"
                            % (what, proc.returncode,
                               ":\n       " + "\n       ".join(detail[-6:]) if detail else ""))
        return stdout

    def ssh(self, command, what="ssh", stream=False):
        return self._run(["ssh", "-i", self.cfg["key"]] + SSH_OPTS
                         + [self.target, command], what, stream=stream)

    def push(self, local_paths, remote_dir):
        self._run(["scp", "-q", "-i", self.cfg["key"]] + SSH_OPTS
                  + list(local_paths) + ["%s:%s/" % (self.target, remote_dir)],
                  "scp upload")

    def pull(self, remote_path, local_path, compress=False):
        argv = ["scp", "-q", "-i", self.cfg["key"]] + SSH_OPTS
        if compress:
            argv.append("-C")
        argv += ["%s:%s" % (self.target, _shell_quote(remote_path)), local_path]
        self._run(argv, "scp download of %s" % os.path.basename(remote_path))

    # -- agent -------------------------------------------------------------- #
    def connect(self):
        """Create the remote staging dir, locate python3, upload the agent."""
        out = self.ssh(
            "set -e; d=$(mktemp -d %sXXXXXX); echo \"TMP=$d\"; "
            "echo \"PY=$(command -v python3 || command -v python || true)\""
            % REMOTE_TMP_PREFIX, "remote handshake")
        for line in out.splitlines():
            if line.startswith("TMP="):
                self.tmp = line[4:].strip()
            elif line.startswith("PY="):
                self.python = line[3:].strip() or "python3"
        if not self.tmp:
            raise SyncError("could not create a staging directory on the remote host")
        if not self.python:
            raise SyncError("no python3 on the remote host; cannot take safe DB snapshots")
        local_agent = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "remote_sync_agent.py")
        self.push([local_agent], self.tmp)
        self.agent_path = "%s/remote_sync_agent.py" % self.tmp

    def agent_json(self, args, what, stream=False):
        command = "%s -u %s %s" % (_shell_quote(self.python),
                                   _shell_quote(self.agent_path),
                                   " ".join(_shell_quote(a) for a in args))
        out = self.ssh(command, what, stream=stream)
        marker = out.rfind(agent.JSON_MARKER)
        if marker < 0:
            raise SyncError("%s: remote agent produced no report:\n       %s"
                            % (what, out.strip()[-500:]))
        payload = out[marker + len(agent.JSON_MARKER):].strip()
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise SyncError("%s: unreadable remote report (%s)" % (what, exc))

    def cleanup(self):
        if not self.tmp:
            return
        try:
            # Belt and braces: the shell refuses anything outside our prefix.
            self.ssh('d=%s; case "$d" in %s*) rm -rf "$d";; *) echo refused;; esac'
                     % (_shell_quote(self.tmp), REMOTE_TMP_PREFIX), "remote cleanup")
        except SyncError:
            pass                            # a leftover temp dir is not worth failing over


def _shell_quote(value):
    value = str(value)
    if value and all(c.isalnum() or c in "@%+=:,./-_" for c in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def _finalize(part, target, digest, mtime, expected_size=None):
    actual = agent.file_digest(part)
    if actual != digest:
        os.remove(part)
        raise SyncError("verification failed for %s (digest mismatch)" % target)
    if expected_size is not None and os.path.getsize(part) != expected_size:
        os.remove(part)
        raise SyncError("verification failed for %s (size mismatch)" % target)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.replace(part, target)
    if mtime is not None:
        try:
            os.utime(target, (mtime, mtime))
        except OSError:
            pass


def apply_regular(tgz_path, project_root, entries, log):
    """Install the staged regular files: whole copies, appended tails, and the
    ones the remote confirmed we already hold byte for byte."""
    applied = {"full": 0, "tail": 0, "same": 0, "bytes": 0}
    errors = []
    tar = tarfile.open(tgz_path, "r:gz") if tgz_path else None
    try:
        for entry in entries:
            action = entry.get("action")
            rel = entry["p"]
            if action not in ("full", "tail", "same"):
                if action not in (None, "skip", "missing"):
                    errors.append("%s: %s" % (rel, entry.get("error", action)))
                continue
            if not safe_rel(rel):
                errors.append("%s: refused unsafe path" % rel)
                continue
            target = os.path.join(project_root, rel.replace("/", os.sep))
            if action == "same":
                # Identical bytes already here; adopt the remote timestamp so the
                # next run settles it with a stat instead of a digest.
                try:
                    os.utime(target, (entry["mtime"], entry["mtime"]))
                except OSError:
                    pass
                applied["same"] += 1
                continue
            part = target + ".part"
            member_name = ("tails/" if action == "tail" else "files/") + rel
            try:
                if tar is None:
                    raise SyncError("remote staged no bundle for %s" % rel)
                member = tar.getmember(member_name)
                stream = tar.extractfile(member)
                if stream is None:
                    raise SyncError("empty member %s" % member_name)
                parent = os.path.dirname(part)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if action == "tail":
                    # Patch a copy, never the file we already trust.
                    shutil.copyfile(target, part)
                    with open(part, "r+b") as fh:
                        fh.seek(entry["offset"])
                        fh.truncate()
                        shutil.copyfileobj(stream, fh, agent.READ_CHUNK)
                else:
                    with open(part, "wb") as fh:
                        shutil.copyfileobj(stream, fh, agent.READ_CHUNK)
                _finalize(part, target, entry["digest"], entry.get("mtime"),
                          expected_size=entry.get("size"))
                moved = entry.get("tail_size", entry.get("size", 0))
                applied[action] += 1
                applied["bytes"] += moved
                log("  %-8s %s (%s)" % ("appended" if action == "tail" else "updated",
                                        rel, human(moved)))
            except Exception as exc:                              # noqa: BLE001
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                errors.append("%s: %s" % (rel, exc))
    finally:
        if tar is not None:
            tar.close()
    return applied, errors


def _local_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def clear_sidecars(db_path):
    """Drop stale ``-wal``/``-shm``: replaying one onto a fresh snapshot corrupts it."""
    removed = []
    for suffix in ("-wal", "-shm", "-journal"):
        path = db_path + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(os.path.basename(path))
            except OSError:
                pass
    return removed


def apply_database(remote, entry, project_root, tmp_dir, block_size, log):
    """Fetch one database snapshot (whole or delta) and install it.

    Returns ``(bytes_moved, action_taken)``; the action can differ from the
    planned one when a delta has to fall back to the whole snapshot.
    """
    rel = entry["p"]
    if not safe_rel(rel):
        raise SyncError("%s: refused unsafe path" % rel)
    target = os.path.join(project_root, rel.replace("/", os.sep))
    part = target + ".part"
    action = entry["action"]
    snap_size = entry["snapshot_size"]
    moved = entry["payload_size"]

    if action == "delta" and not entry["blocks"] and _local_size(target) == snap_size:
        # Every block of the fresh snapshot matches the copy we already hold, at
        # the same length: the files are identical, so there is nothing to do but
        # say so. This is the common case for the archived/static databases.
        return 0, "same"

    if action == "delta" and not os.path.isfile(target):
        action = "full"                      # vanished since we planned the delta
        entry = dict(entry, action="full", payload=entry["payload_hint_full"],
                     payload_size=snap_size)
        moved = snap_size

    if action == "delta":
        blob = os.path.join(tmp_dir, rel.replace("/", "__") + ".blob")
        remote.pull(entry["payload"], blob, compress=True)
        shutil.copyfile(target, part)
        try:
            agent.apply_blocks(part, blob, entry["blocks"], snap_size, block_size)
        finally:
            try:
                os.remove(blob)
            except OSError:
                pass
        try:
            _finalize(part, target, entry["snapshot_digest"], None,
                      expected_size=snap_size)
        except SyncError:
            # The delta did not reconstruct the snapshot: fall back to the whole
            # file, which is still staged on the remote.
            log("  WARN     %s: delta verification failed, refetching in full" % rel)
            remote.pull(entry["payload_hint_full"], part, compress=True)
            _finalize(part, target, entry["snapshot_digest"], None,
                      expected_size=snap_size)
            moved += snap_size
            action = "full"
    else:
        remote.pull(entry["payload"], part, compress=True)
        _finalize(part, target, entry["snapshot_digest"], None, expected_size=snap_size)

    dropped = clear_sidecars(target)
    if dropped:
        log("  cleaned  stale %s" % ", ".join(dropped))
    return moved, action


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="remote_sync",
        description="Incrementally sync the production server's logs/ and data/ trees.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--full", action="store_true",
                        help="ignore remembered state and refetch everything")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be transferred, then stop")
    parser.add_argument("--skip-databases", action="store_true",
                        help="logs/ and other plain files only")
    parser.add_argument("--databases-only", action="store_true")
    parser.add_argument("--no-delta", action="store_true",
                        help="always send whole database snapshots")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="GLOB", help="repeatable; also REMOTE_SYNC_EXCLUDE in .env")
    parser.add_argument("--only", action="append", default=[], metavar="GLOB",
                        help="repeatable; restrict the sync to matching paths")
    parser.add_argument("--block-size-mb", type=int, default=4)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    project_root = os.path.abspath(
        args.project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    block_size = max(1, args.block_size_mb) << 20

    def log(message=""):
        if not args.quiet:
            print(message, flush=True)

    started = time.time()
    try:
        cfg = load_config(project_root, args.env_file)
    except SyncError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    # Each flag may itself carry a comma-separated list: PowerShell's -File mode
    # hands an array parameter over as one joined string.
    def flag_patterns(values):
        out = []
        for value in values or []:
            out.extend(split_patterns(value))
        return out

    exclude = cfg["exclude"] + flag_patterns(args.exclude)
    only = flag_patterns(args.only)
    state_path = os.path.join(project_root, STATE_FILE)
    state = load_state(state_path, cfg["host"], cfg["root"])

    log("Syncing remote data (incremental)...")
    log("  Remote host: %s" % cfg["host"])
    log("  User: %s" % cfg["user"])
    log("  SSH key: %s" % cfg["key"])
    log("  Local project root: %s" % project_root)
    if exclude:
        log("  Excluding: %s" % ", ".join(exclude))
    log()

    remote = Remote(cfg, verbose=not args.quiet)
    errors = []
    stats = {"unchanged": 0, "updated": 0, "appended": 0, "excluded": 0,
             "db_unchanged": 0, "db_delta": 0, "db_full": 0,
             "bytes": 0, "remote_bytes": 0}
    # Staging lives beside the targets so that finished files land with a rename
    # on the same volume, and so a multi-GB bundle never lands on a small %TEMP%.
    tmp_dir = tempfile.mkdtemp(prefix=".sync-tmp-", dir=project_root)

    try:
        for top in cfg["dirs"]:
            os.makedirs(os.path.join(project_root, top), exist_ok=True)

        remote.connect()
        inv_args = ["inventory", "--root", cfg["root"]]
        for top in cfg["dirs"]:
            inv_args += ["--dir", top]
        inventory = remote.agent_json(inv_args, "remote inventory")
        files = inventory["files"]
        by_path = dict((f["p"], f) for f in files)

        # ---- plan ---------------------------------------------------------- #
        plan = {"block_size": block_size, "db_method": cfg["db_method"],
                "regular": [], "dbs": []}
        db_candidates = []
        for f in files:
            rel, size, mtime = f["p"], f["s"], f["m"]
            if agent.is_sidecar(rel):
                continue                     # never transferred; see module docstring
            if only and not matches_any(rel, only):
                continue
            if exclude and matches_any(rel, exclude):
                stats["excluded"] += 1
                continue
            stats["remote_bytes"] += size
            local_path = os.path.join(project_root, rel.replace("/", os.sep))
            if agent.is_database(rel):
                if args.skip_databases:
                    stats["excluded"] += 1
                    continue
                db_candidates.append((rel, f, local_path))
                continue
            if args.databases_only:
                continue
            mode, extra = plan_regular(rel, size, mtime, local_path, force=args.full)
            if mode == "skip":
                stats["unchanged"] += 1
                continue
            item = {"p": rel, "mode": mode}
            item.update(extra)
            plan["regular"].append(item)

        for rel, f, local_path in db_candidates:
            wal = by_path.get(rel + "-wal")
            # Same shape as the agent's source_fingerprint(): see its docstring
            # for why the -wal contributes only its size and the -shm nothing.
            remote_fp = {"db": [f["s"], f["m"]], "wal": wal["s"] if wal else None}
            entry = state["dbs"].get(rel)
            if not args.full and db_is_current(remote_fp, entry, local_path):
                stats["db_unchanged"] += 1
                stale = [s for s in ("-wal", "-shm")
                         if os.path.exists(local_path + s)
                         and os.path.getsize(local_path + s) > 0]
                if stale:
                    log("  WARN     %s has stale %s next to an up-to-date snapshot; "
                        "delete them before reading it" % (rel, ", ".join(stale)))
                continue
            item = {"p": rel, "mode": "full"}
            if not args.no_delta and os.path.isfile(local_path):
                # Hash what we already hold so the remote can send only the
                # blocks that differ from it.
                item = {"p": rel, "mode": "delta",
                        "blocks": agent.block_digests(local_path, block_size)}
            plan["dbs"].append(item)

        if args.dry_run:
            log("Would transfer:")
            for item in plan["regular"]:
                f = by_path[item["p"]]
                log("  %-10s %s (%s)" % (item["mode"], item["p"], human(f["s"])))
            for item in plan["dbs"]:
                f = by_path[item["p"]]
                log("  %-10s %s (%s source)" % ("db/" + item["mode"], item["p"],
                                                human(f["s"])))
            log("\n  %d regular file(s), %d database(s); %d file(s) and %d database(s) "
                "already current." % (len(plan["regular"]), len(plan["dbs"]),
                                      stats["unchanged"], stats["db_unchanged"]))
            return 0

        if not plan["regular"] and not plan["dbs"]:
            log("Everything already up to date.")
        else:
            # ---- stage on the remote --------------------------------------- #
            plan_path = os.path.join(tmp_dir, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(plan, fh)
            remote.push([plan_path], remote.tmp)
            stage = "%s/stage" % remote.tmp
            log("Preparing %d file(s) and %d database snapshot(s) on the remote..."
                % (len(plan["regular"]), len(plan["dbs"])))
            report = remote.agent_json(
                ["prepare", "--root", cfg["root"],
                 "--plan", "%s/plan.json" % remote.tmp, "--stage", stage],
                "remote prepare", stream=True)
            errors.extend(report.get("errors", []))

            # ---- regular files -------------------------------------------- #
            if report["regular"]:
                local_bundle = None
                if report.get("bundle"):
                    local_bundle = os.path.join(tmp_dir, "bundle.tgz")
                    log("Fetching %s bundle..." % human(report["bundle_size"]))
                    remote.pull(report["bundle"], local_bundle)
                    if agent.file_digest(local_bundle) != report["bundle_digest"]:
                        raise SyncError("bundle digest mismatch; transfer corrupted")
                    stats["bytes"] += report["bundle_size"]
                applied, regular_errors = apply_regular(local_bundle, project_root,
                                                        report["regular"], log)
                stats["updated"] += applied["full"]
                stats["appended"] += applied["tail"]
                stats["unchanged"] += applied["same"]
                errors.extend(regular_errors)
                if applied["same"]:
                    log("  %d file(s) confirmed identical by digest -- not transferred"
                        % applied["same"])
                if local_bundle:
                    os.remove(local_bundle)

            # ---- databases ------------------------------------------------- #
            staged_full = dict((e["p"], "%s/db/%s" % (stage, e["p"].replace("/", "__")))
                               for e in report.get("dbs", []))
            for entry in report.get("dbs", []):
                rel = entry["p"]
                if entry.get("action") not in ("full", "delta"):
                    errors.append("%s: %s" % (rel, entry.get("error", entry.get("action"))))
                    continue
                entry["payload_hint_full"] = staged_full[rel]
                label = ("delta %s of %s" % (human(entry["payload_size"]),
                                             human(entry["snapshot_size"]))
                         if entry["action"] == "delta"
                         else "snapshot %s" % human(entry["snapshot_size"]))
                if entry["action"] == "delta" and not entry["blocks"]:
                    label = "unchanged"
                log("  database %s -- %s" % (rel, label))
                try:
                    moved, taken = apply_database(remote, entry, project_root, tmp_dir,
                                                  block_size, log)
                except SyncError as exc:
                    errors.append(str(exc))
                    continue
                stats["bytes"] += moved
                stats[{"delta": "db_delta", "same": "db_unchanged"}
                      .get(taken, "db_full")] += 1
                local_path = os.path.join(project_root, rel.replace("/", os.sep))
                st = os.stat(local_path)
                state["dbs"][rel] = {"fp": entry["fp"],
                                     "local_size": st.st_size,
                                     "local_mtime": st.st_mtime,
                                     "digest": entry["snapshot_digest"],
                                     "method": entry.get("method"),
                                     "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                              time.gmtime())}
        save_state(state_path, state)
    except SyncError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        try:
            save_state(state_path, state)
        except OSError:
            pass
        return 1
    finally:
        remote.cleanup()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---- summary ---------------------------------------------------------- #
    elapsed = time.time() - started
    log()
    if errors:
        print("WARNING: %d item(s) failed:" % len(errors), file=sys.stderr)
        for message in errors[:20]:
            print("  - %s" % message, file=sys.stderr)
    log("[%s] Data sync complete in %.1fs" % ("PARTIAL" if errors else "OK", elapsed))
    log("  files:     %d updated, %d appended, %d already current, %d skipped"
        % (stats["updated"], stats["appended"], stats["unchanged"], stats["excluded"]))
    log("  databases: %d refreshed by delta, %d refreshed in full, %d already current"
        % (stats["db_delta"], stats["db_full"], stats["db_unchanged"]))
    log("  transferred %s (a full copy of the remote tree would be %s)"
        % (human(stats["bytes"]), human(stats["remote_bytes"])))
    if stats["db_delta"] or stats["db_full"]:
        log("  databases arrived as consistent SQLite snapshots (%s), digest-verified"
            % cfg["db_method"])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
