# ⚠️ DEPRECATED: polymarket-spike

**Status**: DEPRECATED as of 2026-06-16 (post-Shadow-Stations epic)

This directory contains the original validated spike code from May 2026 that proved the weather envelope edge on Polymarket weather markets. All code has been promoted into the main `src/` tree and refactored into the target architecture.

## Historical Value

The spike validated:
- Weather envelope model: **88.4% win rate** on 4,867 real Polymarket trades (May 1–2, 2026)
- Market parser with bracket detection (4 regex patterns)
- Polymarket Gamma API + CLOB client with pagination
- Settlement tracking and reconciliation

## What Moved Where

| File | Promoted to | Status |
|---|---|---|
| `config.py` | `src/config.py` | ✅ Integrated |
| `spike.py` (market parser) | `src/data/polymarket.py` + `src/strategy/scanner.py` | ✅ Integrated |
| `polymarket_client.py` | `src/data/polymarket.py` | ✅ Integrated |
| `settle.py` | `src/scripts/settle.py` | ✅ Integrated |
| `envelope.py` | `src/model/envelope.py` | ✅ Integrated |
| `tests/` | `src/tests/` | ✅ Migrated & ported |

## If You Need Original Code

This snapshot is preserved at commit: `<git commit hash TBD>`. If you need to reference the original implementation:

```bash
git show <commit>:archive/polymarket-spike/spike.py
```

## References

- **Technical Spec**: [TECHNICAL_SPECIFICATION.md](../../docs/TECHNICAL_SPECIFICATION.md)
- **Spike Documentation**: [SPIKE_DOCUMENTATION.md](../../docs/SPIKE_DOCUMENTATION.md)
- **Current Implementation**: [src/](../../src/)
