r"""
NX STEP export — convert a .prt to .step, headless.

Runs inside NX via run_journal.exe (see step.cmd). Env vars:

  NXSTEP_MODEL  path to the .prt
  NXSTEP_OUT    output path (default: <model>.step)
  NXSTEP_AP     protocol: 242 (default), 214, or 203
"""

import os
import traceback

import NXOpen

MODEL = os.environ.get("NXSTEP_MODEL", "")
if not MODEL:
    raise RuntimeError("Set NXSTEP_MODEL to the part file path "
                       "(or run via step.cmd)")
OUT = os.environ.get("NXSTEP_OUT", "") or os.path.splitext(MODEL)[0] + ".step"
AP = os.environ.get("NXSTEP_AP", "242").strip()

s = NXOpen.Session.GetSession()
lw = s.ListingWindow
lw.Open()
say = lw.WriteLine


def main():
    part, _ = s.Parts.OpenDisplay(MODEL)
    say("Model: " + part.FullPath)
    if os.path.exists(OUT):
        os.remove(OUT)

    sc = s.DexManager.CreateStepCreator()
    try:
        et = NXOpen.StepCreator.ExportAsOption
        names = [m for m in dir(et) if not m.startswith("_")]
        want = "Ap" + AP
        pick = next((n for n in names if n.lower() == want.lower()), None)
        if pick is None:
            say("AP%s not available; options: %s" % (AP, names))
            pick = next(n for n in names if n.startswith("Ap"))
        sc.ExportAs = getattr(et, pick)
        say("Protocol: %s" % pick)
        try:
            sc.ExportFrom = NXOpen.StepCreator.ExportFromOption.DisplayPart
        except Exception:
            pass
        try:
            ot = sc.ObjectTypes
            for attr in ("Solids", "Surfaces", "Curves", "Csys", "ProductData"):
                if hasattr(ot, attr):
                    setattr(ot, attr, attr in ("Solids", "Surfaces", "Curves"))
        except Exception:
            say("ObjectTypes not settable:\n"
                + traceback.format_exc().splitlines()[-1])
        sc.InputFile = MODEL
        sc.OutputFile = OUT
        try:
            sc.FileSaveFlag = False
        except Exception:
            pass
        try:
            sc.LayerMask = "1-256"
            sc.ProcessHoldFlag = True
        except Exception:
            pass
        sc.Commit()
    except Exception:
        say("STEP EXPORT FAIL:\n" + traceback.format_exc())
        say("DIR StepCreator: %s"
            % ", ".join(m for m in dir(sc) if not m.startswith("_")))
        raise
    finally:
        sc.Destroy()

    if os.path.isfile(OUT):
        say("STEP written: %s (%d bytes)" % (OUT, os.path.getsize(OUT)))
    else:
        say("ERROR: output not created")
    say("DONE-STEP")


if __name__ == "__main__":
    main()
