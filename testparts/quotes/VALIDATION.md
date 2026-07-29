# machinist.py validation against Protolabs — 2026-07-28

## Method

Six NX test parts built via `nx_testparts.py`, each isolating one cost
driver (baseline block / pocket volume / drilled holes / small-tool
complexity / extra-setup side holes / size scaling). Each was uploaded once
to Protolabs (CNC Machining, Aluminum 6061-T651/T6) and priced at qty 1, 5,
10, 25 by driving the protolabs.py daemon's configure page. All prices are
**Factory channel, Standard lead** (see channel pitfall below).

`fit_calibration.py` then fitted ONLY shop-economics knobs (rate, setup
minutes, per-order fixed, per-hole handling, stock markup, one global
toolpath-speed scale, per-part price floor). The physics structure — what
scales with removed volume, finish area, holes, setups, quantity — was
never touched.

## Fitted calibration (machinist_cal.json)

| knob | value | reading |
|---|---|---|
| machine_rate_hr | $138.70 | effective billed rate incl. their premium |
| setup_min | 5.0 | per setup direction per batch |
| order_fixed | $123.23 | per-order fixed (explains steep qty-1 premium) |
| min_part_price | $30 | floor never bound in this data |
| speed_scale | 1.42 | Protolabs toolpaths vs handbook removal rates |
| drill_handling_s | 40.9 | per hole: spot, peck, deburr, inspect |
| material_markup | 2.72 | stock billed ≈ $19/kg for 6061 (cut-block price) |
| handling_min_setup | 5.6 min | per part per setup load/unload |

## Fit quality

- **In-sample (t1–t5, 20 points): 5% rms** error, worst +13% (t1 qty 10 —
  Protolabs applies an extra discount step at qty 10 the a+b/qty
  amortization can't follow).
- **Out-of-sample (bracket_mount, a real earlier quote, qty 3 Standard):
  model $380 vs actual $254 (+50%).** Round part: square-stock assumption
  over-bills material and the conical underside is billed at slow 3D-contour
  rates where Protolabs mills circular interpolation cheaply.
- **t6 (featureless 160×120×40 block) was excluded from the fit**: Protolabs
  prices it $426/part at qty 25 — far above any machining-time explanation
  (3× less removal than the bracket at twice the price). A bare block is
  priced like premium stock, not machining. Model gives $175 — arguably the
  truer machining number; expect Protolabs to quote featureless prisms ~2×
  the model.

## Raw data

`sweep_results.json` — configure-page prices (t5 entry is the corrected
Factory/review number; its original Network-channel curve is preserved under
`t5_sideholes|al6061_network`). `review_results.json` — review-page
verification (t1 matched configure exactly; t5 required the review flow).

## Pitfalls discovered (cost future sessions real money/time)

1. **Two fulfillment channels share one configure page.** The part tile
   either reads "Machining Tolerance: ±0.005 in" (Protolabs Factory) or
   "Network Tolerance: ISO 2768-m" (partner network, ~2.8× cheaper, looser
   tolerance). Which one you get on upload is not controllable; the
   review page with lead=Standard always gives the Factory price.
2. **Stale prices after /qty.** The configure tile keeps the old price
   during async repricing; trust a read only after the tile shows
   "Quantity N" and two consecutive polls agree.
3. **Steels don't instant-price.** 316/304/1018 all render "Request for
   Quote" on configure (this account, July 2026). Aluminum prices
   instantly. No RFQs were submitted during this work.

## 316 stainless status

No instant-quote data exists to calibrate against (see pitfall 3). The 316
estimate uses handbook removal-rate ratios (≈6.7× slower roughing than
6061, 4× finishing, 5.5× drilling penetration) scaled by the same fitted
speed_scale, plus 316 density/stock price. Model gives ≈1.45× the aluminum
price for the same part at qty 1 — typical of published SS/Al CNC ratios.
To refine: submit one manual RFQ for a test part in 316 (Protolabs emails
the quote within hours) and add the point to sweep_results.json.
