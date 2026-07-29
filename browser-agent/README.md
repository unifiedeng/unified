# browser-agent

The shared browser layer for the unified tools. No model, no API keys — the
tools that import it (`protolabs.py`, `siteapi.py`) script every action.

- `agent.py` — the `Browser` class: persistent-profile Chromium via Playwright,
  with cookie-banner dismissal, settle logic, and element-indexed page reading.
  Uses your real Chrome install when present (its fingerprint trips less bot
  detection), falling back to Playwright's Chromium.

  Two constructor options matter when a site pushes back:

  - `Browser(attach_port=9222)` drives a browser *you* started with
    `--remote-debugging-port=9222` instead of launching one. Your everyday
    profile comes along, so its cookies and accumulated site standing apply.
    A fresh automation profile has neither, which is why anti-bot services
    wall it while hand-driven browsing on the same machine goes through.
    Closing detaches; it never shuts the window you opened. Note that only one
    process may hold a Chrome profile, so this conflicts with `siteapi.py`,
    and automated activity counts against that profile's standing — use it for
    reads you'd do by hand, not bulk crawling.
  - `Browser(pace=3)` waits three seconds before every action. Suppliers
    rate-limit on pages-per-minute, not on what the browser looks like.
    Enforced in `_settle()`, the one path every state-changing action takes.
- `logins.json` — your site credentials (copy `logins.example.json` and fill
  in). Gitignored; read locally by the tools, never printed or transmitted
  anywhere except the site's own login form.
- `site-notes.md` — accumulated per-site quirks (selectors, walls, gotchas).
- `profile/` — persistent Chrome profile for McMaster (`siteapi.py` holds it).
- `profile-protolabs/` — separate profile for the Protolabs quote daemon, so
  both can run at once (one process per profile).
- `profile-browse-<supplier>/` — created per supplier by `browse`
  (`supplier.py`), so one site's session state never affects another's.
  Every `profile-*/` directory is gitignored.
- `files/` — download workspace (`mcm cad` saves here).
- `state/` — siteapi daemon state and log.

## Setup (once)

```
pip install -r requirements.txt
playwright install chromium
copy logins.example.json logins.json    # then fill in your credentials
```

Profiles, credentials, downloads, and state are all gitignored — nothing in
this folder that is personal ever gets committed.
