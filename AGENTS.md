# Protolabs CNC quoting — agent instructions

> Cheap local alternative: `cost.cmd <part.prt> --material=6061|316 --qty=N`
> (`machinist.py`, see ESTIMATOR.md) gives a first-principles machining
> time + cost estimate in ~1 s with no Protolabs round-trip — physics-based
> (removal rates, setups), calibrated against real quotes from this daemon.
> Use it for iteration; use the real quote below when the number has to be
> orderable.

Agent-agnostic. Works from Codex, Claude Code, or a plain shell. Everything runs
through `protolabs.py` in this directory, which drives a real Playwright browser
and exposes it over `http://127.0.0.1:8766`. The agent is only ever an HTTP
client — it never automates the browser directly.

## One-command path (start here)

`quote.cmd` is on PATH, so from ANY folder, any agent or human can run:

```
quote C:\hardware\reaction_wheel\wheel.prt --material="Aluminum 6061-T651/T6" --qty=3 --lead=Standard
```

It health-checks the daemon, auto-starts it hidden if absent (and restarts it
if it is stale-versioned or its browser window was closed), runs the whole
quote, and prints the result JSON. Exit 0 = priced or manual RFQ submitted.
Outputs (STEP conversion, quote PDF) land next to the part file. Everything
below is the daemon detail beneath that command.

## The daemon

One signed-in, **visible** browser is held open across calls. Protolabs issues
session-only cookies, so a fresh process means a fresh sign-in (1–3 minutes).

Start it **detached, outside the agent's process tree**. If it is a child of the
agent, interrupting the agent kills the browser mid-quote:

```powershell
Start-Process -FilePath 'python' -ArgumentList 'protolabs.py','serve' `
  -WorkingDirectory 'C:\code\unified' -WindowStyle Hidden `
  -RedirectStandardOutput "$env:TEMP\proto.out.log" `
  -RedirectStandardError  "$env:TEMP\proto.err.log"
```

Verify it landed on the interactive desktop — `SessionId` must be 1, or the
window exists but nobody can see it:

```powershell
Get-Process python* | Select-Object Id, SessionId
Get-Process chrome  | Where-Object MainWindowTitle | Select-Object Id, MainWindowTitle
```

Health check: `curl -s --max-time 10 http://127.0.0.1:8766/health` → expects
`{"ok": true, "version": >=5}`. A lower version predates the fast event-driven
waits and the `service` parameter; `curl http://127.0.0.1:8766/stop` and
relaunch.

Every step record in the log carries `dt` — seconds since the previous record.
When a run feels slow, scan for the big `dt`: that step is the stall.

**Never pass a headless flag.** `serve()` hardcodes `headless: False` so a human
can intervene on 2FA or an unexpected interception. ("headless" in the convert
step log refers to NX, the CAD converter — not the browser.)

The server is single-threaded (Playwright's sync API is not thread-safe). While
a long `/quote` runs, **every** other route including `/health` blocks. A timing
out `/health` means busy, not dead.

## Quoting

`.prt` (Siemens NX) files are converted to STEP automatically via `step.cmd`
(~1–2 min). Don't convert manually.

```
curl -s --max-time 600 "http://127.0.0.1:8766/quote?path=<STEP path>&material=<material>&qty=<qty>&lead=Standard&service=CNC%20Machining&itar=no"
```

URL-encode the path with forward slashes. Default material
`Aluminum 6061-T651/T6`, default qty 1, default lead `Standard`, default
service `CNC Machining` (also: `3D Printing`, `Injection Molding`,
`Sheet Metal`). All waits are event-driven: a simple STEP part prices in
roughly 60-90 seconds end to end. The remaining time is Protolabs'
server-side geometry analysis, not the client. Budget up to 10 minutes only
for large/complex parts. Run it in the background and poll a log file; do
not sit in a blocking wait.

The flow is: new quote → upload → ITAR dialog → configure material → View
Analysis → approve advisories → **Return to Quote** → read price → download PDF.

## Reading the result

Report: quote number, unit price, quantity, **standard lead time**
(`standard_receive_by`) and the `order_by` cutoff, subtotal, shipping, total,
any DFM advisories, and `itar_declared`.

Trust field values, **not step names.** A step can report `ok` for the part it
attempted while leaving the browser somewhere useless. Specifically:

- `view_analysis` returns `returned_to_quote`. If false, the browser is stranded
  on `/dfm-ui` where delivery options read `[]` and every price field is null —
  which looks exactly like an unpriceable part but isn't.
- `quote()` returns `error: "stranded_on_analysis"` or `"unpriced_off_review"`
  for that case. Neither means the part can't be priced. Navigate back
  (`/goto?url=<review URL>`) and re-read `/lead` and `/price`.

If genuinely unpriceable, the tile reads "Request for Quote", the process line
disappears, and figures render `$—`. The script submits the manual RFQ itself
and returns `manual_rfq_submitted: true`. Don't stop and ask — report that it
went in, that Protolabs emails a quote within a few hours, and give the bounding
box against the envelope (3-axis aluminum maxes at 559 × 356 × 95.3 mm; every
5-axis envelope is much smaller, and >254 mm effectively forces aluminum).

## Rules

- **Never click Checkout.** This quotes; it does not order. Stop at the price.
- **ITAR defaults to `no`** per the account holder's standing instruction. Always
  surface `itar_declared` so it is never silent. If a part IS export-controlled,
  pass `itar=yes` and expect the automated flow to end there.
- **Never tick "save as my default choice"** in the ITAR dialog — it answers for
  all future uploads.
- The analysis step records an attributable named approval
  ("Approved By: <account holder>"). Required to make a quote orderable, but say
  so in the report.
- Material selection needs a **real** Playwright click; the Vue SPA ignores
  synthetic/JS clicks and silently reverts to "Make a selection". The script
  handles this — don't work around it with JS.
- Leave the daemon running. `/stop` only when asked.

## Useful routes

`/health` `/quote` `/price` `/analysis` `/lead?kind=Standard` `/pdf?dir=<dir>`
`/goto?url=` `/click?text=` `/text` `/snapshot` `/materials` `/stop`

`/text` and `/snapshot` are how the agent "sees" the page — there is no vision
into the window. When a step's outcome is in doubt, read the page rather than
trusting the step name.
