## Context

`scripts/Fetch-RemoteData.ps1` is the only way an analysis or health-triage
session gets production `logs/` and `data/` onto the desktop, and it has two
defects that make it both slow and unsafe to trust.

**It transfers everything, every time.** The implementation is
`scp -r logs data`, which has no delta support: all ~6.8 GB (6.3 GB of `data/`,
533 MB of `logs/`, 754 files) moves on every run, taking ~4 minutes at best
(249 s measured 2026-07-31). Almost none of it has changed — five of the ten
databases in `data/` are frozen archives (`ce_snapshot.db`, three `.bak` copies,
`meteoedge_pre749.db`, `meteoedge_pre_wave2.db`) and ~700 of the log files are
rotated `.gz` archives that will never be written again.

**It corrupts the databases it copies.** `data/*.db` are live SQLite files in
WAL mode that the bot writes continuously. A byte-for-byte `scp` of a file being
written captures torn pages, and the copy is paired with a `-wal`/`-shm` grabbed
at a different instant, which makes it worse: replaying a stale WAL against a
half-copied main file is exactly how an unreadable local database is produced.
The user reports this happening repeatedly. `scripts/extract_data.sh` already
recognises the problem on the server side ("plain file copy ... can capture torn
pages during concurrent writes") and uses `sqlite3 .backup`; the sync path never
got the same treatment.

The two defects compound: because a run is expensive, a session takes the
snapshot it has rather than re-syncing, and because a snapshot may be torn, a
SQLite error during triage is ambiguous — transport artefact or real production
fault. `.claude/skills/health-triage/SKILL.md` currently has to warn agents about
both.

## Acceptance criteria

- A file already held locally and identical to the remote one is **not**
  transferred. Equality must be established by content (digest), not only by
  size and mtime, because the previous `scp` did not preserve mtimes and so no
  local file's timestamp can be trusted on the first run after this change.
- Append-only logs (`.log`, `.jsonl`, `.csv`, ...) transfer only the bytes
  appended since the local copy, and only after the remote confirms the local
  copy is still a byte-exact prefix. Rotation or truncation falls back to a
  whole-file transfer.
- No live SQLite database is ever copied byte-for-byte. Each is transferred as a
  transactionally consistent server-side snapshot that neither blocks nor is
  blocked by the bot's writers, and that arrives fully checkpointed (no `-wal`
  needed to read it).
- `-wal` / `-shm` / `-journal` sidecars are never transferred, and stale local
  ones are removed when the database beside them is replaced.
- Every transferred payload is digest-verified before it replaces a local file;
  a failed verification leaves the previous local file intact and is reported.
- A database whose remote state is unchanged since the last successful fetch is
  skipped without being snapshotted or hashed. The change signal must not be
  invalidated by the sync's own activity (opening a WAL database read-only
  creates and re-times its sidecars).
- A database that *has* changed transfers only the parts that differ from the
  local copy; if that saving is negligible the whole snapshot is sent instead.
- Steady-state run time and bytes transferred are an order of magnitude below
  the current full copy, measured against the live server and reported in the PR.
- Existing entry point and contract are preserved: `powershell -File
  scripts\Fetch-RemoteData.ps1` with no arguments, exit 0 on success, exit 1 on
  failure, and it still fails fast (seconds, not minutes) when the host is
  unreachable or `REMOTE_*` credentials are absent — health-triage sessions call
  it at startup and must not stall.
- Selective operation is possible without editing the script: restrict to
  specific paths, exclude paths, skip databases, force a full refresh, and a
  dry run that reports what would move without moving it.
- Windows PowerShell 5.1 compatible (this machine has no `pwsh`), and no new
  external tooling: `rsync` is unavailable on Windows, so the solution must work
  with the OpenSSH `ssh`/`scp` already present.

## Technical notes

- Entry point stays `scripts/Fetch-RemoteData.ps1`; it is reasonable for it to
  become a thin wrapper and for the logic to live in Python, which the repo
  already depends on and which can be unit-tested. Interpreter discovery should
  prefer `.venv-win\Scripts\python.exe` and fall back to `PATH` / `py -3`.
- The consistent-snapshot options are SQLite's online backup API
  (`sqlite3.Connection.backup`, single step) and `VACUUM INTO`. Both read inside
  one read transaction, which in WAL mode does not block writers. They differ in
  one way that matters here: the backup API copies page for page and so
  preserves physical layout, while `VACUUM INTO` re-lays the file out and makes
  nearly every block look changed. Measure both on the server before choosing a
  default, and leave the choice configurable.
- Non-SQLite files that merely match the `*.db` naming pattern must not be fed
  to a snapshot call; check the `SQLite format 3\0` header and fall back to a
  plain copy.
- `REMOTE_*` parsing must tolerate CRLF, `export ` prefixes and quoted values.
- PowerShell's `-File` invocation mode hands an array parameter over as a single
  comma-joined string, so path-list flags must split on commas themselves.
- New sync bookkeeping (state file, staging directories, `.part` files) must be
  added to `.gitignore`, and the optional `.env` knobs documented in
  `.env.example`.
- `scripts/fetch_remote_data.sh` (the rsync variant) has the same
  torn-database defect and should at minimum carry a warning pointing at the
  safe path.
- `.claude/skills/health-triage/SKILL.md` documents the old timings and warns
  agents that databases may be torn; it must be updated, since a SQLite error
  on a verified snapshot is now a real finding.

## Test requirements

Unit tests (no network; `tmp_path` only):

- `REMOTE_*` parsing: CRLF, `export`, single/double quotes, comments, missing
  credentials reported together.
- Path classification: `meteoedge.db`, `meteoedge.db.bak-20260828-112302`,
  `x.sqlite3` are databases; `*-wal`, `*-shm`, `notes.dbf`, `bot.log` are not.
- Planning decisions per file: identical (skip), same size with untrusted mtime
  (ask the remote to confirm), grown append-only log (resume), shrunken file
  (whole), rotated `.gz` (whole), forced refresh (whole).
- Database currency check: unchanged remote + unchanged local copy is current;
  a grown WAL, a written main file, a replaced local copy, a missing local copy
  and a never-fetched database are all not current.
- The change signal survives the sidecar re-timing that a read-only open causes,
  but still registers a WAL append.
- Delta round trip reconstructs the snapshot byte for byte and digest for digest
  when it is unchanged (zero bytes sent), when one interior block changed, when
  it grew, when it shrank (truncation), and when there is no local copy at all;
  a truncated delta must raise rather than silently corrupt.
- Snapshot of a live WAL database with a writer holding uncommitted work is
  `integrity_check`-clean, contains every committed row and none of the
  uncommitted one, and leaves no `-wal`/`-shm` beside the output; a previous
  snapshot and its sidecars at the destination are overwritten.
- Path traversal in a remote-supplied path is refused.
- `inventory` and `prepare` wire protocol: reported actions, tail offsets,
  bundle presence, and the negligible-delta fallback.

Manual verification against the live server (report numbers in the PR):

- Dry run lists the expected work and transfers nothing.
- First pass, steady-state pass, and a pass with nothing to do.
- Every synced database passes `PRAGMA integrity_check` / `quick_check` locally,
  including ones reconstructed from a partial transfer, with no sidecars left.

## Dependencies

None.

