---
description: Local first-principles CNC cost estimate for a part (no Protolabs).
argument-hint: <part.prt | part_geom.json> [material] [qty]
allowed-tools: Bash, Read, Glob
---

Estimate the CNC machining cost of the part in `$ARGUMENTS` locally, with no
Protolabs round-trip. `$1` = path to a `.prt` (Siemens NX) or a
`*_geom.json` produced earlier. The user may phrase it naturally
("/cost example.prt in 6061 and qty 1") — map material/qty yourself.
Materials supported: `6061` (Aluminum 6061-T6) and `316` (Stainless 316L).

Run:

```
python C:\code\unified\machinist.py <part> --material=<6061|316> --qty=<N>
```

(`cost.cmd` in `C:\code\unified` is the same thing.) If `$1` is bare with no
directory, Glob for it under the working directory; if nothing matches, STOP
and say so. First run on a `.prt` extracts geometry headlessly via NX
(~20-60 s) and caches `<model>_geom.json`; subsequent runs are instant.
Add `--json` when you want to compute with the numbers rather than show them.

The estimate is physics-first: machining time built from material-removal
rates (roughing volume, finishing area, drilling), setup count from a
tool-access direction cover, then transparent shop economics (machine rate,
setup amortization over qty, stock material, margin, instant-quote floor).
Constants live in `machinist.py` (`MATERIALS`, `SHOP`) with calibration
overrides in `machinist_cal.json`, fitted against real Protolabs quotes —
see `testparts/quotes/` for the validation data.

Report to the user: unit price, total, machining time per part, setup count,
and the dominant cost driver (compare material / machine / setup components).
Note it is a local estimate — for an orderable price they should run /quote.
If the part is outside CNC envelopes (bbox > 559 x 356 x 95 mm) say the
estimate extrapolates.
