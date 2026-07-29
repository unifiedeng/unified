# cost / time — first-principles CNC machining estimates

Get a machining time and cost estimate for an NX part in about a second,
with no quoting service in the loop:

```
cost model.prt --material=6061 --qty=3          full cost breakdown
cost model.prt --material=316 --qty=1 --time    machining time only
cost model.prt --json                           machine-readable output
```

(From Git Bash: `cost.cmd`. From Claude Code: `/cost` and `/time`, e.g.
"/cost bracket.prt in 6061 and qty 5".) Materials: `6061` (Aluminum
6061-T6) and `316` (Stainless 316L). The first run on a `.prt` extracts
geometry headlessly inside NX (~20–60 s) and caches `<model>_geom.json`
next to the part; every run after that is instant. A `*_geom.json` can
also be passed directly.

Example output:

```
part      t3_holes.prt
material  Aluminum 6061-T6   qty 1
stock     84.0x64.0x24.0 mm  (0.348 kg)   removed 33.0 cm3
setups    2   tools 3   small-tool factor 1.00

time/part (min)   rough    0.8   finish    1.7   drill   8.3
                  spindle  20.2  handling+inspect 14.3  ->  34.5
batch setup       25 min  (amortized: 59.6 min/part at qty 1)

cost/part  material    6.62
           machine    46.79
           labor      32.99
           setup     181.20 (amortized)
           margin   x1.35
unit price  $361.27     total (x1)  $361.27
```

## Why it's built this way

The point is that the number reflects a real fact about the part — how
hard it is to machine — not a curve fitted to one vendor's quotes. The
machining time is assembled from physical material-removal mechanics, so
it stays meaningful for any 3-axis shop:

| term | physics |
|---|---|
| roughing | (stock − part − hole) volume ÷ material removal rate (MRR) |
| finishing | surface area ÷ (feed × stepover); slower ball-mill rate for contoured faces; penalty when the smallest internal corner radius forces small tools |
| drilling | per hole: depth ÷ penetration rate at the material's cutting speed, plus fixed per-hole handling (spot, peck, deburr, inspect) |
| setups | minimum set of spindle directions that reach every face and hole — a block is 2 setups (top + flip, walls milled peripherally); side holes force a third |
| non-cutting | tool changes, rapids/approach overhead, per-setup load/unload |

Cost is then that time run through transparent shop economics: machine
rate × time, stock material at cut-block prices, per-setup and per-order
fixed costs amortized over quantity, margin, and an instant-quote floor.
`--time` gives you the physics layer alone.

Everything lives in two files: `machinist.py` (model + default constants
in `MATERIALS` and `SHOP`, every value labeled) and `machinist_cal.json`
(calibration overrides — delete it to fall back to handbook defaults).

## Calibration against Protolabs

The shop-economics knobs were fitted against 24 real Protolabs CNC quotes
(July 2026): six NX test parts, each isolating one cost driver — plain
block, pocket, hole pattern, small-radius complexity, side holes forcing
an extra setup, and a larger block — priced at qty 1 / 5 / 10 / 25 in
6061, all Factory channel at Standard lead. The physics structure was
frozen during the fit; only rate-like constants moved
(`fit_calibration.py`).

Results (full method, raw data and pitfalls in
[testparts/quotes/VALIDATION.md](testparts/quotes/VALIDATION.md)):

- **In-sample: 5% rms error** across the 20 fitted points (worst +13%,
  at Protolabs' extra qty-10 discount step).
- **Out-of-sample: +50%** on a round bracket with a real quote the fit
  never saw — the square-stock assumption and 3D-contour rates over-bill
  round parts; treat estimates for turned-looking geometry as an upper
  bound.
- The featureless big block was excluded: Protolabs prices bare prisms
  like premium stock (~2× any machining-time explanation).

What the comparison revealed about Protolabs pricing, now reflected in
the calibrated constants: a ~$123 per-order fixed cost dominates qty-1
prices (their quantity curve is essentially `marginal + fixed/qty`);
holes bill at ~41 s each — roughly 20× raw drill time; stock is billed
near single-cut-block prices (~$19/kg for 6061); effective rate ≈
$139/hr.

**316 caveat:** Protolabs would not instant-quote steels on the test
account (configure page shows "Request for Quote"), so the 316 numbers
are handbook machining ratios (≈6.7× slower roughing than 6061, ~4×
finishing, ~5.5× drilling) under the same calibrated economics —
physically grounded but not vendor-validated. The model puts 316 at
≈1.45× the 6061 price for the same part at qty 1, in line with published
SS/Al machining ratios.

## Files

- `machinist.py` — the estimator (pure Python, no dependencies).
- `machinist_cal.json` — fitted calibration; regenerate with
  `python fit_calibration.py --write` after adding quote data.
- `nx_geom.py` + `geom.cmd` — headless NX geometry census to JSON.
- `nx_testparts.py` — rebuilds the six test parts (`testparts/`).
- `sweep_quotes.py`, `sweep_review.py` — collect Protolabs price data
  through the `quote` daemon (see AGENTS.md) into
  `testparts/quotes/sweep_results.json`.

## Limits

3-axis milling assumptions; no turning, threads, tolerancing tighter than
default, or finishes. Estimates for parts beyond the CNC envelope
(559 × 356 × 95 mm) extrapolate. It prices the part you give it — for an
orderable number, run `quote`.
