---
description: Upload a part to Protolabs, then configure and price it interactively.
argument-hint: <part.prt | part.step> [material] [qty]
allowed-tools: Bash, Read, Glob
---

Quote the part in `$ARGUMENTS` on Protolabs. `$1` = path to a `.prt` (Siemens NX)
or `.step`/`.stp` file. `$2` (optional) = material, default `Aluminum 6061-T651/T6`.
`$3` (optional) = quantity, default `1`. The user may also phrase it naturally
("quote example.prt in 6061 aluminum cnc machined, qty 3, 1 week lead time") —
map material/qty/process/lead yourself: lead time under ~9 business days →
`Standard`; anything explicitly rush → `Expedite`; process → `service` param
(`CNC Machining` | `3D Printing` | `Injection Molding` | `Sheet Metal`).

**Default to the one-shot batch call** (`/quote?...`) — since daemon version 5
every wait is event-driven and a simple STEP part completes in ~60–90 s
including the DFM analysis click-through and the PDF. Fall back to the
step-wise phase 2 routes only when the batch call reports a failure or the
part needs unusual decisions.

Everything runs through `C:\code\unified\protolabs.py`, which drives a real
Playwright browser and exposes it at `http://127.0.0.1:8766`. No Anthropic API
tokens are consumed by the browser work — do NOT use `mcp__cad-web__web_task`
for this, it bills the API key separately.

## 0. Daemon

```
curl -s --max-time 10 http://127.0.0.1:8766/health
```

- `{"ok": true, "version": >=5}` → reuse it. Do **not** restart.
- `ok: true` but version below 5 → predates the event-driven waits and the
  `service` param. Stop it (`curl -s http://127.0.0.1:8766/stop`) and start
  fresh.
- Connection refused → start it.
- **Timeout** → single-threaded server (Playwright's sync API is not
  thread-safe); a long call blocks every route including `/health`. Timing out
  means busy, not dead. Do not restart it on a timeout.

Start it **detached**, so an interrupt doesn't kill the browser mid-quote:

```powershell
Start-Process -FilePath 'python' -ArgumentList 'protolabs.py','serve' `
  -WorkingDirectory 'C:\code\unified' -WindowStyle Hidden `
  -RedirectStandardOutput "$env:TEMP\proto.out.log" `
  -RedirectStandardError  "$env:TEMP\proto.err.log"
```

Sign-in normally lands in ~15 s now (event-driven); budget up to 3 minutes for
a slow redirect chain or a 2FA interception. Be patient rather than retrying.
Confirm `SessionId` is 1 or the window exists where nobody can see it:

```powershell
Get-Process chrome | Where-Object MainWindowTitle | Select-Object Id, SessionId, MainWindowTitle
```

**Never pass a headless flag.** `serve()` hardcodes `headless: False` so the user
can intervene on 2FA or an unexpected interception.

## Phase 1 — up to and including upload (one call)

If `$1` is bare (no directory), Glob for it under the working directory. If
nothing matches, STOP and say so. `.prt` files are converted to STEP
automatically via `step.cmd` (headless NX, ~1–2 min) — don't convert manually.
"Headless" in the convert log is NX, not the browser.

```
curl -s --max-time 600 "http://127.0.0.1:8766/session?path=<part path>&itar=no"
```

Convert → new quote → upload → ITAR dialog, handing the page back at
**Configure** with a snapshot. Accepts the `.prt` directly.

Report `quote_id` and `itar_declared`, then work with the user.

## Phase 2 — interactive, one decision per call

Each returns a fresh snapshot; read the result before choosing the next call.

| | |
|---|---|
| `/materials` | list what the picker actually offers |
| `/material?name=<exact label>` | set material (real click; verifies it stuck) |
| `/qty?n=<N>` | set quantity (verifies the SPA didn't discard it) |
| `/reprice` | wait for the async quote engine to settle |
| `/proceed` | leave Configure → Review (or the RFQ route) |
| `/analysis` | open DFM analysis, approve advisories, return to quote |
| `/lead?kind=Standard` | choose the delivery option |
| `/price` | read the final figures |
| `/pdf?dir=<dir>` | download the quote PDF |
| `/finish?lead=Standard&dir=<dir>` | **the tail in one call** — approve advisories if asked for, lead time, price, PDF |
| `/snapshot` `/text` | see the page |
| `/goto?url=` `/click?text=` | manual recovery |

Typical order: `/material` → `/qty` → `/reprice` → `/proceed` → `/finish`.

**Never stop to ask about the analysis.** Standing instruction from the account
holder: if the quote says the part needs attention / "Please view the analysis",
click View Analysis and click through until it is done. `/proceed` returns
`needs_analysis`, and `/finish` acts on it by itself — approving, then selecting
lead time, reading the price and pulling the PDF. Report the named approval
afterwards; do not seek permission for it.

Never block: run anything slow with `run_in_background` and read its log. Never
sleep or wait more than 10 seconds in one call.

**Never idle waiting for a background call.** Backgrounding a step is not
permission to stop working. A turn whose entire content is "still running", "let
me wait for the notification", or a bare `Read` of an unchanged log file is a
wasted turn — do not emit one. After launching a background call, immediately
poll it yourself in a loop that returns as soon as the file has content, e.g.

```
for i in $(seq 1 60); do [ -s "$TEMP/f.json" ] && break; sleep 5; done; cat "$TEMP/f.json"
```

That is one tool call that ends the moment the step lands, not a turn spent
doing nothing. `/analysis` and `/finish` routinely run 2-5 minutes; the daemon is
single-threaded, so a timed-out `/health` or `/snapshot` during that window means
*busy*, and is the only probe worth making. If there is genuinely nothing to poll
and nothing to prepare, say what the blocker is in one line — never narrate
waiting as if it were progress.

On the manufacturing-analysis page, read the visible enabled action and click
what the page asks for; don't assume a fixed sequence of button labels.

## Trust field values, not step names

A step can report `ok` for what it attempted while leaving the browser somewhere
useless. This has produced a wrong answer in practice:

- `/analysis` returns `returned_to_quote`. If false, the browser is stranded on
  `/dfm-ui`, where delivery options read `[]` and every price field is null —
  which looks exactly like an unpriceable part but isn't. Navigate back with
  `/goto?url=<review URL>`, then re-read `/lead` and `/price`.
- `/material` and `/qty` verify and return `ok: false` if the SPA discarded the
  change. A silent revert is the normal failure, not an exception.

There is no vision into the browser window. `/snapshot` and `/text` are how you
see the page. When a step's outcome is in doubt, read the page.

## Reporting

Quote number, unit price, quantity, **standard lead time**
(`standard_receive_by`), the `order_by` cutoff, subtotal, shipping, total, any
DFM advisories, and `itar_declared`. Mention the named approval. Give the
bounding box if it's near a limit — 3-axis aluminum maxes at 559 × 356 × 95.3 mm,
every 5-axis envelope is much smaller, and >254 mm effectively forces aluminum.

## When there is no instant price

The tile reads "Request for Quote", the process line disappears, every figure
renders `$—`. `/reprice` returns `rfq_only: true` and `/proceed` returns
`route: "rfq"`. Report that Protolabs emails a quote and manufacturing analysis
within a few hours, and give the bounding box against the relevant envelope so
the user can see why it didn't auto-price.

## Rules

- **Never click Checkout.** This quotes and may submit a request for pricing; it
  does not order. Stop at the price.
- **ITAR defaults to `no`** per the account holder's standing instruction; the
  response echoes `itar_declared` — surface it so it's never silent. If the user
  says a part IS export-controlled, pass `itar=yes` and expect the automated
  flow to end there.
- **Never tick "save as my default choice"** in the ITAR dialog — that would
  answer for all future uploads.
- `/analysis` records a named approval ("Approved By: <account holder>"),
  required to make a quote orderable. It is pre-authorized — run it whenever the
  quote asks for it, without checking first, and mention it in the report.
- Material selection needs a **real** Playwright click; the Vue SPA silently
  ignores synthetic ones and reverts to "Make a selection". The script handles
  it — don't work around it with JS.
- Leave the daemon running. `curl http://127.0.0.1:8766/stop` only if asked.

## Batch call (the default)

Simplest form — `quote.cmd` is on PATH, handles daemon startup/health/version
itself (including a zombie daemon whose browser window was closed), and works
from any folder:

```
quote <abs part path> --material="Aluminum 6061-T651/T6" --qty=3 --lead=Standard --service="CNC Machining" --itar=no
```

(From Git Bash invoke as `quote.cmd`; bare `quote` works in PowerShell/cmd.)

Equivalent raw route when the daemon is already up:

```
curl -s --max-time 600 "http://127.0.0.1:8766/quote?path=<path>&material=<m>&qty=<n>&lead=Standard&service=CNC%20Machining&itar=no"
```

Runs the whole flow — upload, ITAR, configure, DFM approval, lead time, price,
PDF — in one call, typically ~60–90 s for a simple STEP part. Run it in the
background and follow `$env:TEMP\proto.out.log`; each step record carries `dt`
(seconds since the previous step), so any stall is visible as a big `dt`.
Interactive phase 2 is the fallback for parts that need unusual decisions.
