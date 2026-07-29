r"""
Build the machining-estimator test-part family, headless.

Each part isolates one cost driver so the estimator can be validated
feature-by-feature against Protolabs quotes:

  t1_block      80 x 60 x 20 plain block            (baseline)
  t2_pocket     t1 + open rounded pocket 60x40x12 r6 (volume removal)
  t3_holes      t1 + 12 x D6 through holes           (drilling)
  t4_complex    t1 + deep r2 pocket + 8 x D4 holes   (small tools)
  t5_sideholes  t1 + 4 x D6 holes in a side face     (extra setup)
  t6_bigblock   160 x 120 x 40 plain block           (size scaling)

Run via run_journal.exe. Env: NXTP_DIR (default C:\code\unified_\testparts).
All dimensions mm.
"""

import math
import os
import traceback

import NXOpen
import NXOpen.UF

OUTDIR = os.environ.get("NXTP_DIR", r"C:\code\unified_\testparts")

s = NXOpen.Session.GetSession()
uf = NXOpen.UF.UFSession.GetUFSession()
lw = s.ListingWindow
lw.Open()
say = lw.WriteLine

SIGNS = NXOpen.UF.Modl.FeatureSigns
NEW, POS, NEG = SIGNS.NULLSIGN, SIGNS.POSITIVE, SIGNS.NEGATIVE


def block(corner, dims, sign):
    return uf.ModlFeatures.CreateBlock1(
        sign, list(map(float, corner)), [str(d) for d in dims])


def cyl(origin, axis, height, diam, sign):
    return uf.ModlFeatures.CreateCyl1(
        sign, list(map(float, origin)), str(height), str(diam),
        list(map(float, axis)))


def rounded_pocket(x0, y0, ztop, L, W, depth, r):
    """Open-top rounded-rectangle pocket, min corner radius r."""
    z = ztop - depth
    block([x0 + r, y0, z], [L - 2 * r, W, depth + 1], NEG)
    block([x0, y0 + r, z], [L, W - 2 * r, depth + 1], NEG)
    for cx in (x0 + r, x0 + L - r):
        for cy in (y0 + r, y0 + W - r):
            cyl([cx, cy, z], [0, 0, 1], depth + 1, 2 * r, NEG)


def new_part(name):
    path = os.path.join(OUTDIR, name + ".prt")
    if os.path.exists(path):
        os.remove(path)
    part = s.Parts.NewDisplay(path, NXOpen.Part.Units.Millimeters)
    return part, path


def save_close(part):
    part.Save(NXOpen.BasePart.SaveComponents.TrueValue,
              NXOpen.BasePart.CloseAfterSave.FalseValue)
    part.Close(NXOpen.BasePart.CloseWholeTree.TrueValue,
               NXOpen.BasePart.CloseModified.UseResponses, None)


def dump_plain_block_normals(part):
    """Settle AskFaceData norm_dir semantics on a known 6-face block."""
    for b in part.Bodies:
        if not b.IsSolidBody:
            continue
        for f in b.GetFaces():
            ft, pt, direc, box, radius, rad_data, norm_dir = \
                uf.Modeling.AskFaceData(f.Tag)
            if ft == 22:
                say("  BLOCKFACE dir=%s norm_dir=%s center_z~%s"
                    % ([round(v, 3) for v in direc], norm_dir,
                       round((box[2] + box[5]) / 2, 1)))


def t1(name="t1_block"):
    part, path = new_part(name)
    block([0, 0, 0], [80, 60, 20], NEW)
    if name == "t1_block":
        dump_plain_block_normals(part)
    save_close(part)
    say("BUILT " + path)


def t2():
    part, path = new_part("t2_pocket")
    block([0, 0, 0], [80, 60, 20], NEW)
    rounded_pocket(10, 10, 20, 60, 40, 12, 6)
    save_close(part)
    say("BUILT " + path)


def t3():
    part, path = new_part("t3_holes")
    block([0, 0, 0], [80, 60, 20], NEW)
    for i in range(4):
        for j in range(3):
            cyl([14 + i * 17.33, 12 + j * 18, -1], [0, 0, 1], 22, 6, NEG)
    save_close(part)
    say("BUILT " + path)


def t4():
    part, path = new_part("t4_complex")
    block([0, 0, 0], [80, 60, 20], NEW)
    rounded_pocket(8, 8, 20, 40, 44, 15, 2)     # deep, r2 corners
    rounded_pocket(54, 8, 20, 20, 44, 8, 3)     # second pocket
    for j in range(4):
        cyl([62, 12 + j * 12, -1], [0, 0, 1], 22, 4, NEG)
        cyl([72, 12 + j * 12, -1], [0, 0, 1], 22, 4, NEG)
    save_close(part)
    say("BUILT " + path)


def t5():
    part, path = new_part("t5_sideholes")
    block([0, 0, 0], [80, 60, 20], NEW)
    for i in range(4):
        cyl([16 + i * 16, -1, 10], [0, 1, 0], 15, 6, NEG)
    save_close(part)
    say("BUILT " + path)


def t6():
    part, path = new_part("t6_bigblock")
    block([0, 0, 0], [160, 120, 40], NEW)
    save_close(part)
    say("BUILT " + path)


def main():
    if not os.path.isdir(OUTDIR):
        os.makedirs(OUTDIR)
    for fn in (t1, t2, t3, t4, t5, t6):
        try:
            fn()
        except Exception:
            say("FAIL %s:\n%s" % (fn.__name__, traceback.format_exc()))
    say("DONE-TESTPARTS")


main()
