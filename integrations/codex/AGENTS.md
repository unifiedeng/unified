# Global agent notes (all folders)

## NX drawings (any part file, any folder)

To generate an ASME engineering drawing from a Siemens NX `.prt`, run
(`draw` is on PATH):

```
draw <absolute part path>
```

Headless NX, ~1–2 min. Outputs land next to the model: `<model>_dwg.prt`,
`<model>_dwg.pdf`, `<model>_dwg_report.txt` — summarize the report and give
the PDF path. If it aborts saying port 8500 is busy, run the PowerShell
one-liner it prints and retry (Siemens cloud-auth/plotter port collision).
Requires a licensed NX install; "Unable to reserve license" means the
machine's only NX cannot run journals — report it, don't retry.

## McMaster-Carr product search (API speed, no browsing)

`mcm` (on PATH) returns McMaster's own live JSON in under a second — never
drive a browser for McMaster data:

```
mcm search "socket head cap screw"    keyword or part-number search
mcm product 91290A115                 specs, price, available CAD
mcm cadlinks 91290A115                CAD formats for a part
mcm cad 91290A115 STEP                download CAD to browser-agent\files (in the install folder)
```

First call auto-starts a logged-in daemon in a visible Chromium window (~10 s).
Rate limits: one
search + 2–3 product lookups per question; a burst of
`{"_error":"upstream","status":403}` means throttled — wait 15–30 min, do not
retry-loop or debug the tool.

## Supplier catalog browsing (any supplier, page-shaped data)

`mcm` answers "what is this part". It cannot answer "what materials does this
come in" — that lives in rendered category pages and filter sidebars, and
McMaster's keyword search returns `PageCount: 0` for it. Use `browse` (on
PATH) for that, and for any supplier other than McMaster:

```
browse grainger "flat washers"             search a supplier
browse fastenal "socket head cap screw"    part rows with links
browse mcmaster rods --facet Material      expand a filter facet completely
browse https://site.com/catalog/ --json    read any catalog URL
```

Known suppliers: mcmaster, grainger, misumi, fastenal, digikey, mouser;
anything else takes a full URL. It opens a real Chromium window, polls until
the SPA finishes rendering, and scrolls virtualized filter facets to
completion — a facet read any other way truncates around 18 values and looks
complete. `BLOCKED: rate-limited` means stop and wait 15–30 min; retrying
extends it. Keep to a few page loads per question — suppliers score request
cadence, not browser fingerprint.

## Protolabs quoting (any part file, any folder)

To get a manufacturing quote for a CAD part (`.prt` Siemens NX or
`.step`/`.stp`), run the `quote` command — it is on PATH:

```
quote <absolute part path> [--material="Aluminum 6061-T651/T6"] [--qty=N] [--lead=Standard|Expedite] [--service="CNC Machining"|"3D Printing"|"Injection Molding"|"Sheet Metal"] [--itar=no]
```

- It auto-starts a signed-in visible Chromium daemon on 127.0.0.1:8766 if one
  is not already running (sign-in ~15 s, handled by the script — never type
  credentials yourself), then prints the result JSON: quote number, unit
  price, subtotal, shipping, total, standard receive-by date, and the path of
  the downloaded quote PDF (saved next to the part).
- A `.prt` is converted to STEP automatically (headless NX, ~1–2 min cold,
  cached next to the part). Typical total time for a `.step`: ~75 s.
- Map natural-language lead times: ≤ ~9 business days → `Standard`; rush →
  `Expedite`.
- It quotes only — checkout is never clicked, ITAR defaults to "no", and the
  DFM manufacturing analysis is clicked through and approved automatically
  (this records a named approval on the account; mention it when reporting).
- Exit code 0 = priced (or manual RFQ submitted); 1 = failed. Step-by-step
  log with per-step `dt` timings: `%TEMP%\proto.out.log`.
- Full documentation and interactive/step-wise HTTP routes: `AGENTS.md` at
  the repo root.
