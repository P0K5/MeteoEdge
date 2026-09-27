#!/usr/bin/env python3
"""Worker half of the incremental remote data sync (driver: ``remote_sync.py``).

This module is used at **both** ends of the sync:

* it is uploaded to the remote host on every run and executed there by
  ``/usr/bin/python3`` (``inventory`` / ``prepare`` / ``cleanup``), and
* it is imported by :mod:`scripts.remote_sync` locally for the local half of
  the work (block hashing, delta application, digest verification).

It is therefore **stdlib only** and imports nothing from this repo -- it has to
run standing alone from a temp directory on the server.

Wire protocol (JSON on stdout after a ``---JSON---`` marker line)
-----------------------------------------------------------------
``inventory --root R --dir logs --dir data``
    ``{"root": R, "files": [{"p": rel, "s": size, "m": mtime}, ...]}``

``prepare --root R --plan plan.json --stage STAGE``
    Stages exactly what the driver asked for and reports what it produced.
    See :func:`cmd_prepare` for the plan/report shapes.

``cleanup --stage STAGE``
    Removes the staging directory.

Why databases are never copied byte-for-byte
--------------------------------------------
``data/*.db`` are live SQLite files in WAL mode. A plain ``scp`` of a file the
bot is writing captures torn pages (and pairing it with a ``-wal`` copied at a
different instant makes it worse), which is how this sync used to produce
unreadable local databases. ``prepare`` instead asks SQLite on the server for a
consistent snapshot (the online backup API, or ``VACUUM INTO``, both from a
read-only connection) and transfers *that*; ``-wal`` / ``-shm`` sidecars are
never transferred at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import urllib.request

BLOCK_SIZE = 4 << 20                      # delta block granularity (4 MiB)
READ_CHUNK = 1 << 20
DIGEST_SIZE = 16                          # blake2b-128: fast, ample for change detection
SQLITE_MAGIC = b"SQLite format 3\x00"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
JSON_MARKER = "---JSON---"


# --------------------------------------------------------------------------- #
# digests / block maps
# --------------------------------------------------------------------------- #
def file_digest(path, limit=None):
    """blake2b-128 of ``path``, or of its first ``limit`` bytes."""
    h = hashlib.blake2b(digest_size=DIGEST_SIZE)
    remaining = limit
    with open(path, "rb") as fh:
        while True:
            if remaining is not None:
                if remaining <= 0:
                    break
                chunk = fh.read(min(READ_CHUNK, remaining))
            else:
                chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            h.update(chunk)
    return h.hexdigest()


def block_digests(path, block_size=BLOCK_SIZE):
    """Per-block digests of ``path`` in file order (the last block may be short)."""
    out = []
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(block_size)
            if not chunk:
                break
            out.append(hashlib.blake2b(chunk, digest_size=DIGEST_SIZE).hexdigest())
    return out


def changed_block_indices(have, want):
    """Indices of ``want`` blocks that ``have`` cannot supply, in file order.

    ``have`` is the receiver's block map, ``want`` the sender's. Blocks past the
    end of ``have`` are always needed, so a grown file degrades to "send the
    tail" and a missing file to "send everything".
    """
    return [i for i, digest in enumerate(want)
            if i >= len(have) or have[i] != digest]


def extract_blocks(src, indices, out, block_size=BLOCK_SIZE):
    """Concatenate the given blocks of ``src`` into ``out``. Returns bytes written."""
    written = 0
    with open(src, "rb") as fin, open(out, "wb") as fout:
        for idx in indices:
            fin.seek(idx * block_size)
            chunk = fin.read(block_size)
            fout.write(chunk)
            written += len(chunk)
    return written


def apply_blocks(target, blob, indices, total_size, block_size=BLOCK_SIZE):
    """Patch ``target`` in place from ``blob``, then size it to ``total_size``.

    ``indices`` must be in the order :func:`extract_blocks` used. Block lengths
    are derived from ``total_size`` rather than carried in the blob, so a short
    blob is caught here and any other damage by the caller's digest check.
    """
    with open(blob, "rb") as fin, open(target, "r+b") as fout:
        for idx in indices:
            offset = idx * block_size
            length = min(block_size, max(0, total_size - offset))
            chunk = fin.read(length)
            if len(chunk) != length:
                raise ValueError(
                    "delta blob truncated: block %d wanted %d bytes, got %d"
                    % (idx, length, len(chunk)))
            fout.seek(offset)
            fout.write(chunk)
        fout.truncate(total_size)


# --------------------------------------------------------------------------- #
# path classification (shared with the driver so both ends agree)
# --------------------------------------------------------------------------- #
def is_sidecar(rel):
    """True for SQLite ``-wal`` / ``-shm`` / ``-journal`` companions."""
    return rel.endswith(SIDECAR_SUFFIXES)


def is_database(rel):
    """True for SQLite database files, including ``foo.db.bak-20260828`` copies."""
    if is_sidecar(rel):
        return False
    name = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".db", ".sqlite", ".sqlite3"):
        idx = name.find(ext)
        if idx < 0:
            continue
        rest = name[idx + len(ext):]
        # ".db" must end the name or be followed by a separator, so that
        # "meteoedge.db.bak-20260828" counts and "foo.dbf" does not.
        if rest == "" or rest[0] in "._-":
            return True
    return False


def looks_like_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def source_fingerprint(path, wal_size=None):
    """Cheap change signature for a live database, as ``{"db": [size, mtime],
    "wal": size|None}``.

    In WAL mode a commit lands in the ``-wal`` without touching the main file,
    so the main file's size and mtime alone cannot tell us the database is
    unchanged -- hence the WAL size. Only its *size* though, and no ``-shm`` at
    all: merely opening the database read-only (which is what taking a snapshot
    does) creates and re-times those sidecars, so a fingerprint that included
    their mtimes would be invalidated by our own snapshot and never match again.
    A WAL that grows is a commit; a WAL that is reset is a checkpoint, and a
    checkpoint writes the main file, so its mtime catches that case.
    """
    try:
        st = os.stat(path)
        db = [st.st_size, st.st_mtime]
    except OSError:
        db = None
    if wal_size is None:
        try:
            wal_size = os.stat(path + "-wal").st_size
        except OSError:
            wal_size = None
    return {"db": db, "wal": wal_size}


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #
def _read_only_uri(path):
    return "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"


def _remove_db_files(path, suffixes=("", "-wal", "-shm", "-journal")):
    for suffix in suffixes:
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def snapshot_database(src, out, method="backup", timeout=60.0):
    """Write a transactionally consistent copy of live SQLite DB ``src`` to ``out``.

    Both methods read the source inside a single read transaction, which in WAL
    mode neither blocks nor is blocked by the bot's writers, and both produce a
    fully checkpointed standalone file (no ``-wal`` to pair it with).

    ``backup`` (default) uses the online backup API in one step. It copies page
    for page, so the snapshot keeps the source's physical layout and successive
    snapshots differ only where the data did -- measured on this server, an
    interval that changed 5% of ``cryptoedge.db`` produced a 5% block delta,
    where ``VACUUM INTO`` produced 32% because it re-lays the whole file out.

    ``vacuum`` runs ``VACUUM INTO``: it compacts (a smaller first transfer) and
    is 2-4x faster on the server, at the cost of that much worse delta rate.

    Returns the method actually used.

    The source is only ever opened **read-only**, and there is deliberately no
    read-write fallback: while neither method writes to the source itself, a
    read-write connection to a WAL database can run recovery on open and
    checkpoints on close (deleting the ``-wal`` and touching the main file). That
    would make a tool whose whole job is to read production data mutate it, and
    would invalidate the change fingerprint taken right after. If the read-only
    open fails -- an unwritable ``-shm``, or the file owned by another user --
    that is reported as an error for this database and the rest of the sync
    continues.
    """
    con = sqlite3.connect(_read_only_uri(src), uri=True, timeout=timeout)
    try:
        if method != "vacuum":
            _remove_db_files(out)
            try:
                dst = sqlite3.connect(out)
                try:
                    con.backup(dst)
                finally:
                    dst.close()
                method = "backup"
            except sqlite3.Error:
                # Contention or an unreadable page map: VACUUM INTO cannot be
                # restarted by a writer, so it is the safer second attempt.
                method = "vacuum"
        if method == "vacuum":
            _remove_db_files(out)
            con.execute("VACUUM INTO ?", (out,))
    finally:
        con.close()
    # Neither method should leave a WAL behind, but make certain we never hand
    # a sidecar to the receiver.
    _remove_db_files(out, suffixes=("-wal", "-shm"))
    return method


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_inventory(args):
    root = os.path.abspath(args.root)
    files = []
    for top in args.dir:
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".sync-")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                files.append({"p": rel, "s": st.st_size, "m": st.st_mtime})
    files.sort(key=lambda f: f["p"])
    _emit({"root": root, "files": files})


def _stage_copy(src, dest):
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.copyfile(src, dest)


def cmd_prepare(args):
    """Stage everything the driver asked for and report it.

    Plan (JSON)::

        {"block_size": 4194304,
         "db_method": "backup",
         "regular": [{"p": rel, "mode": "full"},
                     {"p": rel, "mode": "maybe_tail",
                      "local_size": N, "local_digest": hex}],
         "dbs":     [{"p": rel, "mode": "full"},
                     {"p": rel, "mode": "delta", "blocks": [hex, ...]}]}

    Report (JSON)::

        {"bundle": path|null, "bundle_size": N, "bundle_digest": hex,
         "regular": [{"p": rel, "action": "full"|"tail"|"missing"|"error", ...}],
         "dbs": [{"p": rel, "action": "full"|"delta"|"error", "payload": path,
                  "payload_size": N, "snapshot_size": N, "snapshot_digest": hex,
                  "blocks": [i, ...], "method": "backup"|"vacuum"|"copy",
                  "fp": {...}}],
         "errors": [str, ...]}
    """
    root = os.path.abspath(args.root)
    stage = os.path.abspath(args.stage)
    with open(args.plan, "r") as fh:
        plan = json.load(fh)
    block_size = int(plan.get("block_size") or BLOCK_SIZE)
    db_method = plan.get("db_method") or "backup"

    files_dir = os.path.join(stage, "files")
    tails_dir = os.path.join(stage, "tails")
    db_dir = os.path.join(stage, "db")
    os.makedirs(db_dir, exist_ok=True)

    report = {"bundle": None, "bundle_size": 0, "bundle_digest": None,
              "regular": [], "dbs": [], "errors": []}

    # ---- regular files: whole copy, or just the appended tail -------------- #
    for item in plan.get("regular", []):
        rel = item["p"]
        src = os.path.join(root, rel)
        entry = {"p": rel}
        try:
            st = os.stat(src)
            mode = item.get("mode", "full")
            if mode == "maybe_same":
                # Same size, unverified mtime: if the digests agree the receiver
                # already has the bytes and only needs the timestamp.
                if file_digest(src) == item["local_digest"]:
                    entry.update(action="same", size=st.st_size, mtime=st.st_mtime)
                    report["regular"].append(entry)
                    continue
                mode = "full"
            if mode == "maybe_tail":
                local_size = int(item["local_size"])
                # Resume only if what the receiver holds is still a byte-exact
                # prefix; rotation or truncation falls back to a full copy.
                if (st.st_size > local_size
                        and file_digest(src, limit=local_size) == item["local_digest"]):
                    dest = os.path.join(tails_dir, rel)
                    parent = os.path.dirname(dest)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(src, "rb") as fin, open(dest, "wb") as fout:
                        fin.seek(local_size)
                        shutil.copyfileobj(fin, fout, READ_CHUNK)
                    tail_size = os.path.getsize(dest)
                    total = local_size + tail_size
                    entry.update(action="tail", offset=local_size,
                                 tail_size=tail_size, size=total,
                                 mtime=st.st_mtime,
                                 digest=file_digest(src, limit=total))
                    report["regular"].append(entry)
                    continue
                mode = "full"
            dest = os.path.join(files_dir, rel)
            _stage_copy(src, dest)
            entry.update(action="full", size=os.path.getsize(dest),
                         mtime=st.st_mtime, digest=file_digest(dest))
        except FileNotFoundError:
            entry.update(action="missing")
        except Exception as exc:                                  # noqa: BLE001
            entry.update(action="error", error="%s: %s" % (type(exc).__name__, exc))
            report["errors"].append("%s: %s" % (rel, exc))
        report["regular"].append(entry)

    # ---- one compressed bundle carrying all of the above ------------------- #
    members = [(path, name) for path, name in ((files_dir, "files"), (tails_dir, "tails"))
               if os.path.isdir(path)]
    if members:
        _progress("compressing %d staged file(s)" % len(report["regular"]))
        bundle = os.path.join(stage, "bundle.tgz")
        with tarfile.open(bundle, "w:gz", compresslevel=6) as tar:
            for path, name in members:
                tar.add(path, arcname=name, recursive=True)
        for path, _ in members:
            shutil.rmtree(path, ignore_errors=True)
        report["bundle"] = bundle
        report["bundle_size"] = os.path.getsize(bundle)
        report["bundle_digest"] = file_digest(bundle)

    # ---- databases: consistent snapshot, then only the changed blocks ------ #
    for item in plan.get("dbs", []):
        rel = item["p"]
        src = os.path.join(root, rel)
        entry = {"p": rel}
        try:
            snap = os.path.join(db_dir, rel.replace("/", "__"))
            _progress("snapshotting %s (%.0f MB)"
                      % (rel, os.path.getsize(src) / 1048576.0))
            if looks_like_sqlite(src):
                entry["method"] = snapshot_database(src, snap, method=db_method)
            else:
                # Not a SQLite file (or empty): nothing to snapshot, but the
                # delta path below still applies.
                _stage_copy(src, snap)
                entry["method"] = "copy"
            # Taken *after* the snapshot: opening the source read-only leaves a
            # 0-byte -wal behind where there was none, and the receiver's next
            # inventory has to see the same state we recorded.
            entry["fp"] = source_fingerprint(src)
            snap_size = os.path.getsize(snap)
            entry.update(snapshot_size=snap_size, snapshot_digest=file_digest(snap),
                         payload=snap, payload_size=snap_size, action="full")
            if item.get("mode") == "delta":
                _progress("diffing %s against the local copy" % rel)
                indices = changed_block_indices(item.get("blocks") or [],
                                                block_digests(snap, block_size))
                blob = snap + ".blob"
                blob_size = extract_blocks(snap, indices, blob, block_size)
                # A delta that saves almost nothing is just a slower full copy.
                if blob_size <= snap_size * 0.9:
                    entry.update(action="delta", payload=blob,
                                 payload_size=blob_size, blocks=indices)
                else:
                    os.remove(blob)
        except Exception as exc:                                  # noqa: BLE001
            entry.update(action="error", error="%s: %s" % (type(exc).__name__, exc))
            report["errors"].append("%s: %s" % (rel, exc))
        report["dbs"].append(entry)

    _emit(report)


def cmd_cleanup(args):
    stage = os.path.abspath(args.stage)
    # Refuse to walk off into anything that is not one of our staging dirs.
    if os.path.isdir(stage) and os.path.basename(stage).startswith("stage"):
        shutil.rmtree(stage, ignore_errors=True)
    _emit({"cleaned": stage})


def cmd_digest(args):
    _emit({"p": args.file, "size": os.path.getsize(args.file),
           "digest": file_digest(args.file)})


def _progress(message):
    """Progress line for the driver to echo; ignored when parsing the report."""
    sys.stdout.write("# " + message + "\n")
    sys.stdout.flush()


def _emit(payload):
    sys.stdout.write(JSON_MARKER + "\n")
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def build_parser():
    parser = argparse.ArgumentParser(description="remote data sync worker")
    sub = parser.add_subparsers(dest="cmd")

    inv = sub.add_parser("inventory")
    inv.add_argument("--root", required=True)
    inv.add_argument("--dir", action="append", default=[])
    inv.set_defaults(func=cmd_inventory)

    prep = sub.add_parser("prepare")
    prep.add_argument("--root", required=True)
    prep.add_argument("--plan", required=True)
    prep.add_argument("--stage", required=True)
    prep.set_defaults(func=cmd_prepare)

    clean = sub.add_parser("cleanup")
    clean.add_argument("--stage", required=True)
    clean.set_defaults(func=cmd_cleanup)

    dig = sub.add_parser("digest")
    dig.add_argument("--file", required=True)
    dig.set_defaults(func=cmd_digest)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
