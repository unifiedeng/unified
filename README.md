# unified

CAD workflows from the terminal. Five commands (browse, mcm, draw, quote,
cost), each works from any folder and for any coding agent. Outputs are
written next to the input part.

- `cost model.prt --material=6061 --qty=3` estimates CNC machining cost and
  time locally in ~1 s, from first principles (material-removal rates, setup
  count, stock) — no quoting service in the loop. Calibrated against real
  Protolabs quotes (5% rms on the test family); `--time` for the physics
  layer alone. See [ESTIMATOR.md](ESTIMATOR.md).
- `draw model.prt` makes an ASME engineering drawing from a Siemens NX part
  (views, dimensions, GD&T) and writes a drawing .prt, a PDF, and a report.
  Takes about 1 to 2 minutes.
- `mcm search "socket head cap screw"` returns live McMaster-Carr product JSON
  in under a second. Also `mcm product <pn>`, `mcm cadlinks <pn>`, and
  `mcm cad <pn> STEP` to download CAD.
- `browse grainger "flat washers"` reads any supplier's catalog in a real
  browser: categories, part rows with prices, and filter facets. Use it for
  page-shaped questions ("what materials is this sold in") and for suppliers
  other than McMaster. `browse mcmaster rods --facet Material` expands a
  filter list completely.
- `quote part.prt --material="Aluminum 6061-T651/T6" --qty=3` gets a Protolabs
  manufacturing quote end to end: upload, configure, manufacturing analysis,
  price, and the quote PDF. About 75 seconds for a STEP file.

## Setup

1. Windows with Python 3.9 or newer.
2. Add this folder to PATH.
3. `pip install playwright` then `playwright install chromium` (needed for
   mcm, browse, and quote).
4. Copy `browser-agent\logins.example.json` to `logins.json` in the same
   folder and fill in your own McMaster and Protolabs credentials. The file
   is gitignored and never leaves your machine.
5. For draw and .prt conversion: a Siemens NX install that can run journals.
   The free Student Edition license cannot. Auto-detected, or set NX_DIR.

From Git Bash, call the commands as `browse.cmd`, `mcm.cmd`, `draw.cmd`,
`quote.cmd`, `cost.cmd`. Bare names work in PowerShell and cmd. (`cost`
needs NX only for the first run on each part; after that its geometry is
cached and estimates are instant, offline, and account-free.)

## Notes

- mcm and browse always run a visible Chromium window. You can watch what
  they read, and when a session lapses you sign in right in the window.
- Protolabs: sign-in is scripted from logins.json. Checkout is never clicked,
  and the ITAR question is only answered from an explicit flag.
- McMaster: keep request volume low. A burst of 403 responses means you are
  throttled. Wait 15 to 30 minutes before retrying.
- Suppliers score request *cadence*, not browser fingerprint. A few page loads
  per question is fine; a crawl gets the session blocked. `browse` reports
  `BLOCKED: rate-limited` when that happens. Stop and wait rather than retry.
- `browse` never types credentials. If a catalog needs a login, sign in
  yourself in the window it opens; the profile persists for later runs.
- Agent integration files (Claude Code slash commands, Codex notes) are in
  `integrations/`. Protocol details are in `AGENTS.md` and `SITEAPI.md`.
- Example NX parts are in `examples/`.
