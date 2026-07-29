---
description: First-principles CNC machining time estimate for a part.
argument-hint: <part.prt | part_geom.json> [material] [qty]
allowed-tools: Bash, Read, Glob
---

Estimate how long the part in `$ARGUMENTS` takes to CNC machine. `$1` = path
to a `.prt` (Siemens NX) or a `*_geom.json`. Natural phrasing is fine
("/time example.prt in 6061 and qty 1") — map material/qty yourself.
Materials: `6061` (Aluminum 6061-T6) and `316` (Stainless 316L).

Run:

```
python C:\code\unified\machinist.py <part> --material=<6061|316> --qty=<N> --time
```

If `$1` is bare with no directory, Glob for it under the working directory;
if nothing matches, STOP and say so. First run on a `.prt` extracts geometry
headlessly via NX (~20-60 s) and caches `<model>_geom.json`. Use `--json`
for the full machine-readable breakdown (times, setups, tools).

The time is built from physical material-removal rates — roughing volume /
MRR, finishing area / (feed x stepover), per-hole drilling at material
cutting speed, plus non-cutting overhead, tool changes and per-setup
handling — so it is a true difficulty measure for the part on any 3-axis
mill, independent of any one shop's pricing.

Report: cutting time (rough/finish/drill split), per-part total, setups,
and batch setup time amortized at the given quantity. If the user asks for
cost, use /cost instead.
