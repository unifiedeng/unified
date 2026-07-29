r"""
Fit machinist.py's shop-economics knobs to the Protolabs sweep data.

Deliberately narrow: the physics structure (what scales with removed volume,
area, holes, setups, quantity) is fixed by machinist.py. This fits only:

  machine_rate_hr   effective billed $/hr (margin stays fixed — collinear)
  setup_min         per-setup batch minutes      (quantity curvature)
  program_min       per-job batch minutes        (quantity curvature)
  min_part_price    instant-quote floor
  speed_scale       ONE multiplier on removal rates for BOTH materials —
                    "how conservative are Protolabs toolpaths vs handbook
                    rates". The 6061:316 ratio stays pinned to handbook
                    physics (there is no 316 instant-quote data to fit).

Usage: python fit_calibration.py [--write]   (--write saves machinist_cal.json)
"""

import argparse
import copy
import json
import math
import os

import numpy as np
from scipy.optimize import minimize

import machinist

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP = os.path.join(HERE, "testparts", "quotes", "sweep_results.json")


EXCLUDE = {"t6_bigblock"}   # featureless block: Protolabs prices it as
                            # premium stock, not machining — pathological
                            # input that poisons the fit for real parts


def load_obs():
    with open(SWEEP) as f:
        res = json.load(f)
    obs = []
    for key, entry in res.items():
        part, mat = entry.get("part"), entry.get("material_key")
        if not part or not mat or part in EXCLUDE:
            continue
        gj = os.path.join(HERE, "testparts", part + "_geom.json")
        if not os.path.exists(gj):
            continue
        with open(gj) as f:
            geom = json.load(f)
        for q, p in entry.get("prices", {}).items():
            unit = p.get("unit") if isinstance(p, dict) else None
            if unit:
                obs.append({"part": part, "mat": mat, "qty": int(q),
                            "unit": float(unit), "geom": geom})
    return obs


BOUNDS = {
    "machine_rate_hr": (50.0, 200.0),
    "setup_min": (5.0, 90.0),
    "order_fixed": (0.0, 400.0),     # per-order fixed $; program_min stays
    "min_part_price": (30.0, 150.0),  # at its default (collinear with it)
    "speed_scale": (0.15, 2.5),
    "drill_handling_s": (3.0, 120.0),  # spot+peck+deburr+inspect per hole
    "material_markup": (1.0, 4.0),     # billed stock vs commodity $/kg
    "handling_min_setup": (0.5, 15.0),  # per part per setup load/unload
}
ORDER = list(BOUNDS)
SPEED_KEYS = ("mrr_rough_cm3_min", "arr_flat_cm2_min",
              "arr_3d_cm2_min", "drill_vc_m_min")

BASE_SHOP = copy.deepcopy(machinist.SHOP)
BASE_MAT = copy.deepcopy(machinist.MATERIALS)


def apply_params(p):
    machinist.SHOP.update(BASE_SHOP)
    for mk, base in BASE_MAT.items():
        machinist.MATERIALS[mk].update(base)
    machinist.SHOP["machine_rate_hr"] = p["machine_rate_hr"]
    machinist.SHOP["setup_min"] = p["setup_min"]
    machinist.SHOP["order_fixed"] = p["order_fixed"]
    machinist.SHOP["min_part_price"] = p["min_part_price"]
    machinist.SHOP["drill_handling_s"] = p["drill_handling_s"]
    machinist.SHOP["material_markup"] = p["material_markup"]
    machinist.SHOP["handling_min_setup"] = p["handling_min_setup"]
    for mk in ("al6061", "ss316"):
        for k in SPEED_KEYS:
            machinist.MATERIALS[mk][k] = BASE_MAT[mk][k] * p["speed_scale"]


def vec_to_params(x):
    p = {}
    for i, k in enumerate(ORDER):
        lo, hi = BOUNDS[k]
        p[k] = float(np.clip(x[i], lo, hi))
    return p


def residuals(obs, p):
    apply_params(p)
    out = []
    for o in obs:
        est = machinist.estimate(o["geom"], o["mat"], o["qty"])
        pred = est["cost"]["unit_price"]
        out.append(math.log(pred / o["unit"]))
    return np.array(out)


def objective(x, obs):
    r = residuals(obs, vec_to_params(x))
    # soft penalty outside bounds keeps Nelder-Mead honest
    pen = sum(max(0.0, BOUNDS[k][0] - x[i]) + max(0.0, x[i] - BOUNDS[k][1])
              for i, k in enumerate(ORDER))
    return float(np.sum(r * r)) + 10.0 * pen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    obs = load_obs()
    print("observations:", len(obs))
    if len(obs) < 8:
        raise SystemExit("not enough priced observations yet")

    x0 = np.array([machinist.SHOP["machine_rate_hr"],
                   machinist.SHOP["setup_min"],
                   150.0,
                   machinist.SHOP["min_part_price"],
                   0.7, 30.0, 3.0, 2.0])
    best = None
    for scale in (1.0, 0.7, 1.4):
        r = minimize(objective, x0 * scale, args=(obs,),
                     method="Nelder-Mead",
                     options={"maxiter": 4000, "xatol": 1e-3, "fatol": 1e-5})
        if best is None or r.fun < best.fun:
            best = r
    p = vec_to_params(best.x)
    res = residuals(obs, p)
    print("\nfitted:")
    for k in ORDER:
        print("  %-16s %8.2f" % (k, p[k]))
    print("rms log-residual: %.3f  (=%.0f%% typical error)"
          % (float(np.sqrt(np.mean(res ** 2))),
             100 * (math.exp(float(np.sqrt(np.mean(res ** 2)))) - 1)))

    apply_params(p)
    print("\n%-28s %4s %10s %10s %7s" % ("part|mat", "qty", "quote", "model",
                                         "err%"))
    for o in obs:
        est = machinist.estimate(o["geom"], o["mat"], o["qty"])
        pred = est["cost"]["unit_price"]
        print("%-28s %4d %10.2f %10.2f %+6.0f%%"
              % ("%s|%s" % (o["part"], o["mat"]), o["qty"], o["unit"], pred,
                 100 * (pred / o["unit"] - 1)))

    if args.write:
        cal = {
            "shop": {k: p[k] for k in
                     ("machine_rate_hr", "setup_min", "order_fixed",
                      "min_part_price", "drill_handling_s",
                      "material_markup", "handling_min_setup")},
            "materials": {
                mk: {k: round(BASE_MAT[mk][k] * p["speed_scale"], 3)
                     for k in SPEED_KEYS}
                for mk in ("al6061", "ss316")
            },
            "_fit": {
                "n_obs": len(obs),
                "rms_log_residual": round(float(np.sqrt(np.mean(res ** 2))), 4),
                "speed_scale": round(p["speed_scale"], 3),
                "note": "316 rates = handbook ratio x fitted speed_scale; "
                        "no 316 instant-quote data exists to validate them",
            },
        }
        with open(machinist.CAL_FILE, "w") as f:
            json.dump(cal, f, indent=2)
        print("\nwrote", machinist.CAL_FILE)


if __name__ == "__main__":
    main()
