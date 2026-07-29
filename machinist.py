r"""
machinist — first-principles CNC machining time & cost estimator.

Not a regression on anyone's quotes: the core output is a machining TIME
built from physical material-removal rates, and the cost is that time run
through transparent shop economics. Protolabs quotes are used only to
calibrate a handful of shop-economics knobs (machine rate, setup minutes,
margin); the physics layer stays independent, so the estimate remains a
true statement about how hard the part is to machine on any shop's 3-axis
mill.

Usage:
  python machinist.py <part.prt|part_geom.json> [--material=6061|316]
                      [--qty=N] [--json] [--time]

Reads <part>_geom.json (runs geom.cmd via NX automatically if missing or
stale). Calibration overrides load from machinist_cal.json next to this
script, if present.

Model, per part from stock = bbox + allowance:
  t_rough  = (V_stock - V_part - V_holes) / MRR(material, tool)
  t_finish = A_prismatic / ARR_flat + A_contour / ARR_3d   (x small-tool factor)
  t_drill  = sum(depth / penetration_rate(d, material) + handling)
  t_aux    = tool changes + per-setup part handling
  batch    = n_setups x setup_min + program/inspect overhead   (amortized /qty)
Setups come from a greedy direction cover: a spindle direction D machines
faces with n.D > -0.05 (top-facing or vertical-wall) and holes with
|axis.D| ~ 1.
"""

import argparse
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(HERE, "machinist_cal.json")

# ---------------------------------------------------------------- materials
# Physical cutting data for a mid-size 3-axis VMC with carbide tooling.
# These are conservative production numbers, not brochure maxima.
MATERIALS = {
    "al6061": {
        "label": "Aluminum 6061-T6",
        "density_g_cm3": 2.70,
        "price_per_kg": 7.0,        # plate stock, USD
        "mrr_rough_cm3_min": 30.0,  # 12 mm carbide EM, HSM-ish adaptive
        "arr_flat_cm2_min": 60.0,   # finishing: feed x stepover, flat/wall
        "arr_3d_cm2_min": 12.0,     # ball-mill contour finishing
        "drill_vc_m_min": 120.0,    # carbide drill surface speed
        "drill_f_mm_rev": 0.20,     # at ~6 mm dia; scaled by sqrt(d/6)
        "wear_factor": 1.00,        # tool cost folded into machine rate
    },
    "ss316": {
        "label": "Stainless Steel 316L",
        "density_g_cm3": 7.98,
        "price_per_kg": 11.0,
        "mrr_rough_cm3_min": 4.5,
        "arr_flat_cm2_min": 14.0,
        "arr_3d_cm2_min": 3.5,
        "drill_vc_m_min": 22.0,
        "drill_f_mm_rev": 0.10,
        "wear_factor": 1.30,
    },
}

ALIASES = {
    "al6061": "al6061", "6061": "al6061", "al": "al6061",
    "aluminum": "al6061", "aluminium": "al6061",
    "aluminum 6061-t651/t6": "al6061", "6061-t6": "al6061",
    "ss316": "ss316", "316": "ss316", "316l": "ss316",
    "stainless": "ss316", "stainless steel 316/316l": "ss316",
    "stainless 316": "ss316",
}

# ------------------------------------------------------------- shop economics
# The calibratable layer. Physics above should NOT be touched by calibration
# except via the global mrr/arr scale (same knob for every part & feature).
SHOP = {
    "machine_rate_hr": 100.0,   # USD/hr, machine + operator + tooling
    "setup_min": 20.0,          # per setup direction, per batch
    "program_min": 15.0,        # CAM/prove-out per job (batch fixed)
    "inspect_min_part": 3.0,    # per-part inspection & deburr minutes
    "handling_min_setup": 1.5,  # per part, per setup: load/unload
    "toolchange_s": 12.0,
    "drill_handling_s": 5.0,    # position + approach + peck overhead per hole
    "stock_allow_mm": 2.0,      # stock allowance per side
    "material_markup": 1.6,     # stock as billed vs commodity price
    "cut_efficiency": 0.55,     # fraction of spindle time actually cutting
                                # (rapids, approaches, corner slowdowns)
    "margin": 1.35,             # shop margin / overhead on the whole job
    "min_part_price": 75.0,     # instant-quote floor per part
    "order_fixed": 0.0,         # per-order fixed cost
}


def load_cal():
    if os.path.exists(CAL_FILE):
        with open(CAL_FILE) as f:
            cal = json.load(f)
        SHOP.update(cal.get("shop", {}))
        for m, upd in cal.get("materials", {}).items():
            if m in MATERIALS:
                MATERIALS[m].update(upd)
    return SHOP


# ------------------------------------------------------------------ geometry
def ensure_geom(path):
    """Return geometry dict for a .prt or _geom.json path."""
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    if not path.lower().endswith(".prt"):
        raise SystemExit("Expected a .prt or _geom.json file: %s" % path)
    gj = os.path.splitext(path)[0] + "_geom.json"
    if (not os.path.exists(gj)
            or os.path.getmtime(gj) < os.path.getmtime(path)):
        print("extracting geometry via NX (headless)...", file=sys.stderr)
        r = subprocess.run(
            ["cmd", "/c", os.path.join(HERE, "geom.cmd"), path, gj],
            capture_output=True, text=True)
        if not os.path.exists(gj):
            sys.stderr.write(r.stdout[-2000:] + r.stderr[-2000:])
            raise SystemExit("geometry extraction failed for %s" % path)
    with open(gj) as f:
        return json.load(f)


def solve_setups(geom):
    """Setup count: primary direction + flip + one per leftover feature
    direction.

    A setup along D machines faces that FACE the spindle (n.D > 0.7),
    vertical walls (|n.D| < 0.3, peripheral milling), and holes along D.
    The primary direction is picked by facing area; the opposite face of
    the stock always needs one flip. Anything left (side holes, angled
    faces) costs one setup per direction cluster.
    """
    faces = [(pf["n"], pf.get("area") or 0.0)
             for pf in geom.get("planar_faces", [])
             if (pf.get("area") or 0.0) >= 5.0]
    holes = [h["axis"] for h in geom.get("holes", [])]
    if not faces and not holes:
        return 1, [[0.0, 0.0, 1.0]]

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    def covered_face(n, D):
        d = dot(n, D)
        return d > 0.7 or abs(d) < 0.3

    cands = [[0, 0, 1], [0, 0, -1], [0, 1, 0],
             [0, -1, 0], [1, 0, 0], [-1, 0, 0]]
    cands += [list(n) for n, _ in faces if max(abs(c) for c in n) < 0.99]

    # primary: direction with the largest truly-facing area
    P = max(cands, key=lambda D: sum(a for n, a in faces if dot(n, D) > 0.7))
    chosen = [P]
    rem_faces = [(n, a) for n, a in faces if not covered_face(n, P)]
    rem_holes = [a for a in holes if abs(dot(a, P)) <= 0.95]

    # the flip: anything facing away from P (stock back side)
    if any(dot(n, P) < -0.7 for n, _ in faces) or rem_faces or True:
        # a milled-from-stock part always needs its back side faced
        Pf = [-P[0], -P[1], -P[2]]
        chosen.append(Pf)
        rem_faces = [(n, a) for n, a in rem_faces if not covered_face(n, Pf)]
        rem_holes = [a for a in rem_holes if abs(dot(a, Pf)) <= 0.95]

    # leftover feature directions, clustered at ~15 deg
    leftovers = [n for n, _ in rem_faces] + rem_holes
    for v in leftovers:
        hit = False
        for D in chosen:
            d = dot(v, D)
            if d > 0.966 or (v in rem_holes and abs(d) > 0.95):
                hit = True
                break
        if not hit and len(chosen) < 6:
            chosen.append(list(v))
    return len(chosen), [[round(c, 3) for c in D] for D in chosen]


def estimate(geom, mat_key, qty):
    m = MATERIALS[mat_key]
    sp = SHOP

    bbox = geom["bbox_mm"]
    v_part = geom["volume_mm3"]
    a_total = geom["surface_area_mm2"]
    areas = geom.get("areas_by_class_mm2", {}) or {}
    holes = geom.get("holes", [])
    r_min = geom.get("min_concave_radius_mm")

    # ---- stock ----
    a = sp["stock_allow_mm"]
    stock_dims = [d + 2 * a for d in bbox]
    v_stock = stock_dims[0] * stock_dims[1] * stock_dims[2]
    kg_stock = v_stock / 1000.0 * m["density_g_cm3"] / 1000.0
    mat_cost = kg_stock * m["price_per_kg"] * sp["material_markup"]

    # ---- roughing ----
    v_holes = sum(math.pi * (h["d"] / 2) ** 2 * h["depth"] for h in holes)
    v_removed = max(v_stock - v_part - v_holes, 0.0)
    t_rough = (v_removed / 1000.0) / m["mrr_rough_cm3_min"]      # min

    # ---- finishing ----
    a_hole_walls = sum(math.pi * h["d"] * h["depth"] for h in holes)
    a_contour = sum(areas.get(k) or 0.0 for k in
                    ("conical", "spherical", "toroidal", "freeform",
                     "extruded", "revolved", "blend", "other"))
    a_flat = max(a_total - a_hole_walls - a_contour, 0.0)
    small_tool = 1.0
    if r_min and r_min < 3.0:
        small_tool = min((3.0 / max(r_min, 0.25)) ** 0.5, 2.5)
    t_finish = ((a_flat / 100.0) / m["arr_flat_cm2_min"]
                + (a_contour / 100.0) / m["arr_3d_cm2_min"]) * small_tool

    # ---- drilling ----
    t_drill = 0.0
    for h in holes:
        d, depth = h["d"], h["depth"]
        rpm = 1000.0 * m["drill_vc_m_min"] / (math.pi * d)
        f = m["drill_f_mm_rev"] * math.sqrt(d / 6.0)
        pen = rpm * f                                            # mm/min
        t_drill += depth / pen + sp["drill_handling_s"] / 60.0

    # ---- setups, tools, aux ----
    n_setups, setup_dirs = solve_setups(geom)
    n_tools = 2 + len({round(h["d"], 1) for h in holes})
    if small_tool > 1.0:
        n_tools += 1
    t_toolchange = n_tools * sp["toolchange_s"] / 60.0
    t_handling = n_setups * sp["handling_min_setup"]

    t_cut = t_rough + t_finish + t_drill
    t_spindle = t_cut / sp["cut_efficiency"] + t_toolchange
    t_part = t_spindle + t_handling + sp["inspect_min_part"]
    t_batch = n_setups * sp["setup_min"] + sp["program_min"]

    # ---- cost ----
    rate_min = sp["machine_rate_hr"] / 60.0
    machine_cost = t_spindle * rate_min * m["wear_factor"]
    labor_cost = (t_handling + sp["inspect_min_part"]) * rate_min
    batch_cost = t_batch * rate_min
    unit = (mat_cost + machine_cost + labor_cost
            + (batch_cost + sp["order_fixed"]) / qty) * sp["margin"]
    unit = max(unit, sp["min_part_price"])

    return {
        "material": m["label"],
        "qty": qty,
        "stock_mm": [round(v, 1) for v in stock_dims],
        "stock_kg": round(kg_stock, 3),
        "removed_cm3": round(v_removed / 1000.0, 1),
        "time_min": {
            "rough": round(t_rough, 2),
            "finish": round(t_finish, 2),
            "drill": round(t_drill, 2),
            "toolchange": round(t_toolchange, 2),
            "cut_total": round(t_cut, 2),
            "spindle_with_noncut": round(t_spindle, 2),
            "handling_inspect": round(t_handling + sp["inspect_min_part"], 2),
            "per_part_total": round(t_part, 2),
            "batch_setup": round(t_batch, 1),
            "amortized_per_part": round(t_part + t_batch / qty, 2),
        },
        "setups": n_setups,
        "setup_dirs": setup_dirs,
        "tools": n_tools,
        "small_tool_factor": round(small_tool, 2),
        "cost": {
            "material": round(mat_cost, 2),
            "machine": round(machine_cost, 2),
            "labor": round(labor_cost, 2),
            "setup_amortized": round((batch_cost + sp["order_fixed"]) / qty, 2),
            "margin_x": sp["margin"],
            "unit_price": round(unit, 2),
            "total": round(unit * qty, 2),
        },
    }


def fmt_report(r, part):
    t = r["time_min"]
    c = r["cost"]
    lines = [
        "part      %s" % os.path.basename(part),
        "material  %s   qty %d" % (r["material"], r["qty"]),
        "stock     %s mm  (%.3f kg)   removed %.1f cm3"
        % ("x".join(str(v) for v in r["stock_mm"]), r["stock_kg"],
           r["removed_cm3"]),
        "setups    %d   tools %d   small-tool factor %.2f"
        % (r["setups"], r["tools"], r["small_tool_factor"]),
        "",
        "time/part (min)   rough %6.1f   finish %6.1f   drill %5.1f"
        % (t["rough"], t["finish"], t["drill"]),
        "                  spindle %5.1f  handling+inspect %4.1f  -> %5.1f"
        % (t["spindle_with_noncut"], t["handling_inspect"],
           t["per_part_total"]),
        "batch setup       %.0f min  (amortized: %.1f min/part at qty %d)"
        % (t["batch_setup"], t["amortized_per_part"], r["qty"]),
        "",
        "cost/part  material %7.2f" % c["material"],
        "           machine  %7.2f" % c["machine"],
        "           labor    %7.2f" % c["labor"],
        "           setup    %7.2f (amortized)" % c["setup_amortized"],
        "           margin   x%.2f" % c["margin_x"],
        "unit price  $%.2f     total (x%d)  $%.2f"
        % (c["unit_price"], r["qty"], c["total"]),
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("part")
    ap.add_argument("--material", default="6061")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--time", action="store_true",
                    help="report machining time only")
    args = ap.parse_args()

    key = ALIASES.get(args.material.strip().lower())
    if not key:
        raise SystemExit("Unknown material %r (use 6061 or 316)"
                         % args.material)
    load_cal()
    geom = ensure_geom(os.path.abspath(args.part))
    r = estimate(geom, key, max(args.qty, 1))

    if args.json:
        print(json.dumps(r, indent=2))
    elif args.time:
        t = r["time_min"]
        print("machining time  %s  qty %d" % (r["material"], r["qty"]))
        print("  cutting     %6.1f min  (rough %.1f / finish %.1f / drill %.1f)"
              % (t["cut_total"], t["rough"], t["finish"], t["drill"]))
        print("  per part    %6.1f min  (with non-cut, handling, inspect)"
              % t["per_part_total"])
        print("  batch setup %6.0f min, %d setups -> %.1f min/part at qty %d"
              % (t["batch_setup"], r["setups"],
                 t["amortized_per_part"], r["qty"]))
    else:
        print(fmt_report(r, args.part))


if __name__ == "__main__":
    main()
