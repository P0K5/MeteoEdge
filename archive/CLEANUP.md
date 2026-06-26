# Archive Cleanup Guide

## Status
As of 2026-06-26, the following cleanup has been performed:

### ✅ Kept
- **`early-spike-results-may-2026/DEPRECATION.md`** — Historical reference for May 2026 spike validation (88.4% win rate)
- **`PROFITABILITY_PROJECTION.md`** — Updated June 2026 profitability analysis with current system improvements

### 🗑️ To Remove (code integrated into src/)
- **`polymarket-shadow/`** — Shadow mode development code (integrated into src/)
- **`polymarket-spike/`** — Original spike implementation (integrated into src/)

## Why Clean Up

These folders contained:
1. **Prototypical code** that has been refactored into the main `src/` tree
2. **Test logs and data** from May 2026 that we've summarized into PROFITABILITY_PROJECTION.md
3. **Development artifacts** (__pycache__, .gitignore) that add no value
4. **~150MB of JSONL snapshots** that are no longer needed (historical record complete)

## How to Remove

### On Linux/Unix (systemd deployment):
```bash
cd /home/p0k5/MeteoEdge  # or your install path
rm -rf archive/polymarket-spike archive/polymarket-shadow
git add -A
git commit -m "chore(archive): remove integrated spike and shadow folders"
git push
```

### On Windows (development):
```powershell
cd c:\Coding\MeteoEdge
Remove-Item archive/polymarket-spike -Recurse -Force
Remove-Item archive/polymarket-shadow -Recurse -Force
git add -A
git commit -m "chore(archive): remove integrated spike and shadow folders"
git push
```

### If Folder is Locked
The folder may be locked by VSCode or another IDE. Close the IDE and retry:
```powershell
# Close VSCode, then:
Remove-Item "c:\Coding\MeteoEdge\archive\polymarket-spike" -Recurse -Force
```

## What's Lost (and Why It's OK)

| Content | Lost? | Why OK |
|---------|-------|--------|
| Spike code (spike.py, envelope.py, etc.) | ✅ | Integrated into src/model/, src/strategy/, src/scripts/ |
| Shadow mode service config | ✅ | Replaced by systemd units in deploy/systemd/ |
| Settlement data & logs (May 2026) | ✅ | Key metrics extracted into PROFITABILITY_PROJECTION.md |
| __pycache__ artifacts | ✅ | Regenerated on next run |

## Disk Space Recovered

```
polymarket-spike/logs:  ~154 MB
polymarket-shadow:      ~0.5 MB
───────────────────────────────
Total:                  ~154.5 MB ✓
```

---

**Last Updated**: 2026-06-26  
**Approver**: Development Team

