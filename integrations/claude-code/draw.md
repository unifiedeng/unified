---
description: Generate an ASME drawing (.prt + PDF) from a Siemens NX part, headless.
argument-hint: <part.prt>
allowed-tools: Bash, Read, Glob
---

Generate an engineering drawing for the NX part in `$ARGUMENTS`.

`$1` is a path to a `.prt` file. If it is a bare filename, Glob for it under the
working directory; if nothing matches, STOP and say so. Only Siemens NX parts
work — this is not for STEP files.

## Run it

`draw.cmd` is on PATH (lives in `C:\code\unified`) and works from any folder:

```
draw <absolute part path>
```

(From Git Bash invoke as `draw.cmd`; bare `draw` works in PowerShell/cmd.)

Headless NX takes ~1–2 minutes — run it in the background and poll its output.
Three files land NEXT TO THE MODEL:

- `<model>_dwg.prt` — the NX drawing file
- `<model>_dwg.pdf` — the plotted drawing
- `<model>_dwg_report.txt` — what was generated (views, dimensions, notes)

## Report

Read the `_dwg_report.txt` and summarize it (views placed, dimensions count,
any warnings), give the PDF path, and send the PDF to the user so they can see
the drawing.

## Failure modes (read the command output, it names them)

- **Port 8500 already in use** → the tool refuses to launch NX on purpose: the
  Siemens cloud-license OAuth redirect and the PDF plotter both use port 8500,
  and launching anyway causes an endless browser sign-in loop. Run the
  PowerShell one-liner the error message prints, then retry.
- **"Unable to reserve license" (3615094)** → the NX Student Edition license
  cannot run journals; the tool prefers a Designcenter install automatically.
  If only Student Edition exists on the machine, this capability is
  unavailable — say so rather than retrying.
- **Could not find NX** → set `NX_DIR` to the install folder containing NXBIN.

A Siemens cloud sign-in window may appear on first run of the day — that is the
named-user license authenticating; the user handles it, not you.
