r"""
NX geometry census for machining estimation — headless.

Opens a .prt and writes a JSON digest of everything a machining-time model
needs: volume, bounding box, surface area by face class, holes (dia/depth),
minimum concave (internal-corner) radius, and required tool-access directions.

Runs inside NX via run_journal.exe (see geom.cmd). Env vars:

  NXGEOM_MODEL  path to the .prt
  NXGEOM_OUT    output path (default: <model>_geom.json)

All output lengths are millimetres, areas mm^2, volumes mm^3.
"""

import json
import math
import os
import traceback

import NXOpen
import NXOpen.UF

MODEL = os.environ.get("NXGEOM_MODEL", "")
if not MODEL:
    raise RuntimeError("Set NXGEOM_MODEL to the part file path "
                       "(or run via geom.cmd)")
OUT = (os.environ.get("NXGEOM_OUT", "")
       or os.path.splitext(MODEL)[0] + "_geom.json")

s = NXOpen.Session.GetSession()
uf = NXOpen.UF.UFSession.GetUFSession()
lw = s.ListingWindow
lw.Open()
say = lw.WriteLine

# UF_MODL face type codes (per UF_MODL_ask_face_data)
FACE_TYPES = {
    16: "cylindrical", 17: "conical", 18: "spherical", 19: "toroidal",
    20: "extruded", 22: "planar", 23: "blend", 43: "freeform",
    65: "offset", 66: "foreign", 67: "convergent",
}


def vsub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def vdot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vnorm(a):
    m = math.sqrt(vdot(a, a)) or 1.0
    return [a[0] / m, a[1] / m, a[2] / m]


def face_area(part, faces, unit_factor2):
    """Sum of face areas in mm^2 via MeasureManager; None on failure."""
    if not faces:
        return 0.0
    try:
        mm = part.MeasureManager
        try:
            a_unit = part.UnitCollection.FindObject("SquareMilliMeter")
            l_unit = part.UnitCollection.FindObject("MilliMeter")
            f2 = 1.0
        except Exception:
            a_unit = part.UnitCollection.FindObject("SquareInch")
            l_unit = part.UnitCollection.FindObject("Inch")
            f2 = 645.16
        m = mm.NewFaceProperties(a_unit, l_unit, 0.999, faces)
        area = m.Area * f2
        m.Dispose()
        return area
    except Exception:
        say("face_area failed: " + traceback.format_exc().splitlines()[-1])
        return None


def main():
    part, _ = s.Parts.OpenDisplay(MODEL)
    say("Model: " + part.FullPath)

    # unit conversion: everything UF returns is in part units
    is_inch = False
    try:
        is_inch = part.PartUnits == NXOpen.BasePart.Units.Inches
    except Exception:
        pass
    F = 25.4 if is_inch else 1.0          # length -> mm
    F2, F3 = F * F, F * F * F

    bodies = [b for b in part.Bodies if b.IsSolidBody]
    if not bodies:
        raise RuntimeError("No solid bodies in part")
    say("Solid bodies: %d" % len(bodies))

    # ---- mass properties (area, volume) ----
    tags = [b.Tag for b in bodies]
    volume = area_total = None
    try:
        # type=1 solids; units=3 grams/cm; density irrelevant (we take
        # area/volume only, which come back in part units regardless)
        # units=3 -> grams/cm: area cm^2, volume cm^3 (independent of
        # part units, so no F conversion here)
        props, _stats = uf.Modeling.AskMassProps3d(
            tags, len(tags), 1, 3, 0.0, 1, [0.999] + [0.0] * 10)
        area_total = props[0] * 100.0
        volume = props[1] * 1000.0
    except Exception:
        say("AskMassProps3d failed: "
            + traceback.format_exc().splitlines()[-1])

    # ---- bounding box (union over bodies) ----
    bb = None
    for b in bodies:
        box = uf.ModlGeneral.AskBoundingBox(b.Tag)
        box = [v * F for v in box]
        if bb is None:
            bb = list(box)
        else:
            for i in range(3):
                bb[i] = min(bb[i], box[i])
                bb[i + 3] = max(bb[i + 3], box[i + 3])
    bbox = [bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]]

    # ---- face census ----
    by_class = {}                  # class -> [Face,...]
    cyl_faces = []                 # concave cylinder records for hole logic
    concave_radii = []             # all concave radii (fillets + holes)
    access_dirs = []               # (nx,ny,nz) unit vectors needing access
    planar_faces = []              # per-face normal + area for setup cover
    n_faces = 0

    for b in bodies:
        for f in b.GetFaces():
            n_faces += 1
            try:
                ft, pt, direc, box, radius, rad_data, norm_dir = \
                    uf.Modeling.AskFaceData(f.Tag)
            except Exception:
                by_class.setdefault("other", []).append(f)
                continue
            cls = FACE_TYPES.get(ft, "other")
            concave = (norm_dir == -1)

            if cls == "cylindrical":
                key = "cyl_concave" if concave else "cyl_convex"
                by_class.setdefault(key, []).append(f)
                if concave:
                    r = abs(radius) * F
                    concave_radii.append(r)
                    ext = None
                    try:
                        uv = uf.Modeling.AskFaceUvMinmax(f.Tag)
                        ext = abs(uv[1] - uv[0])
                    except Exception:
                        pass
                    depth = abs(vdot(vsub(box[3:6], box[0:3]),
                                     vnorm(direc))) * F
                    cyl_faces.append({
                        "axis": [round(v, 6) for v in vnorm(direc)],
                        "pos": [round(v * F, 4) for v in pt],
                        "r": round(r, 4),
                        "arc": ext,
                        "depth": round(depth, 3),
                    })
            elif cls in ("toroidal", "spherical", "conical", "blend"):
                by_class.setdefault(cls, []).append(f)
                if concave and radius:
                    concave_radii.append(abs(rad_data if cls == "toroidal"
                                             and rad_data else radius) * F)
            elif cls == "planar":
                by_class.setdefault(cls, []).append(f)
                n = vnorm(direc)
                if norm_dir == -1:
                    n = [-n[0], -n[1], -n[2]]
                access_dirs.append(n)
                a = face_area(part, [f], F2)
                planar_faces.append({
                    "n": [round(v, 4) for v in n],
                    "area": round(a, 2) if a is not None else None,
                })
            else:
                by_class.setdefault(
                    cls if cls in ("freeform", "extruded", "revolved")
                    else "other", []).append(f)

    say("Faces: %d  classes: %s"
        % (n_faces, {k: len(v) for k, v in by_class.items()}))

    # ---- per-class areas ----
    areas = {}
    for cls, faces in by_class.items():
        a = face_area(part, faces, F2)
        areas[cls] = round(a, 2) if a is not None else None

    # ---- hole clustering: concave cylinders grouped by axis+radius+pos ----
    holes = []
    used = [False] * len(cyl_faces)
    for i, c in enumerate(cyl_faces):
        if used[i]:
            continue
        group = [c]
        used[i] = True
        for j in range(i + 1, len(cyl_faces)):
            d = cyl_faces[j]
            if used[j] or abs(d["r"] - c["r"]) > 0.01:
                continue
            if abs(abs(vdot(c["axis"], d["axis"])) - 1.0) > 1e-3:
                continue
            # axis-to-axis distance
            w = vsub(d["pos"], c["pos"])
            w_ax = vdot(w, c["axis"])
            perp = math.sqrt(max(vdot(w, w) - w_ax * w_ax, 0.0))
            if perp > 0.05:
                continue
            group.append(d)
            used[j] = True
        arc = sum((g["arc"] or 0.0) for g in group)
        if arc >= 5.5:            # ~full 360 deg -> a hole, not a fillet
            depth = max(g["depth"] for g in group)
            holes.append({
                "d": round(2 * c["r"], 3),
                "depth": round(depth, 3),
                "axis": c["axis"],
            })
            n = c["axis"]
            access_dirs.append(n)

    # ---- access-direction clustering (~15 deg tolerance) ----
    clusters = []
    for n in access_dirs:
        hit = False
        for cl in clusters:
            if vdot(n, cl) > 0.966:
                hit = True
                break
            # a hole axis can be reached from either end
        if not hit:
            clusters.append(n)
    # fold opposite hole axes: drilling can come from one side, but a
    # planar face needs its own normal. Keep raw cluster count; the
    # estimator maps it to setups.
    setup_dirs = [[round(v, 3) for v in c] for c in clusters]

    out = {
        "part": MODEL,
        "units": "mm",
        "is_inch_part": is_inch,
        "n_bodies": len(bodies),
        "n_faces": n_faces,
        "volume_mm3": round(volume, 2) if volume else None,
        "surface_area_mm2": round(area_total, 2) if area_total else None,
        "bbox_mm": [round(v, 3) for v in bbox],
        "areas_by_class_mm2": areas,
        "planar_faces": planar_faces,
        "holes": holes,
        "min_concave_radius_mm": (round(min(concave_radii), 4)
                                  if concave_radii else None),
        "access_dirs": setup_dirs,
        "n_access_dirs": len(setup_dirs),
    }
    with open(OUT, "w") as fp:
        json.dump(out, fp, indent=2)
    say("GEOM written: %s" % OUT)
    say(json.dumps(out)[:400])
    say("DONE-GEOM")


try:
    main()
except Exception:
    say("GEOM FAIL:\n" + traceback.format_exc())
    raise
