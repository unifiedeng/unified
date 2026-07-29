# siteapi — fast API access to McMaster (and, soon, Protolabs)

A local HTTP daemon that fronts the sites the browser-agent logs into, so you get
live JSON in **~200–800 ms** instead of a 30–120 s "open a browser and click
through the site" agent run. Nothing is cached — every call hits the site live.

## Why it's built this way

McMaster's product endpoints sit behind **Akamai Bot Manager**, which validates a
per-session sensor cookie (`_abck`). Plain `requests`/`curl_cffi` get a 403 no
matter the headers or TLS fingerprint. So siteapi keeps **one logged-in browser
warm** (a visible Chromium window, so you can see what it does and log in when a
session lapses) and issues the site's *own* JSON `fetch()` calls from inside the
page, where the anti-bot session and fingerprint are automatically valid.

- One-time warmup per site: ~5–10 s (navigates a real page once).
- Every call after that: ~0.2–0.8 s. CAD downloads ~1–2 s.
- Reuses the browser-agent's persistent `profile\` — same logins as everything else.

## Usage

The client auto-starts the daemon on first call:

```
python siteapi.py mcmaster search "socket head screw"   # part/keyword search -> JSON
python siteapi.py mcmaster product 91290A115            # full product JSON (specs, CAD, image)
python siteapi.py mcmaster cadlinks 91290A115           # {format: file_path} for every CAD file
python siteapi.py mcmaster cad 91290A115 STEP           # download CAD -> browser-agent\files
```

Daemon control:

```
python siteapi.py serve     # run the daemon in the foreground (see logs)
python siteapi.py health    # up / down
python siteapi.py stop      # shut it down (releases the browser profile)
```

It's a real HTTP API too — once up, curl it directly:

```
curl http://127.0.0.1:8765/mcmaster/product/91290A115
curl "http://127.0.0.1:8765/mcmaster/search?q=91290A115"
curl "http://127.0.0.1:8765/mcmaster/cad/91290A115?fmt=STEP"
```

Set `SITEAPI_PORT` to change the port (default 8765).

## Routes

| Route | Returns |
|---|---|
| `GET /health` | `{ok, warmed:[origins]}` |
| `GET /mcmaster/search?q=…` | McMaster search-engine JSON (part-number resolves; keyword differs in shape) |
| `GET /mcmaster/product/<pn>` | `ProductContent.aspx` JSON — image, `cadControlDat.AvailableCAD` (2-D + 3-D), etc. |
| `GET /mcmaster/cadlinks/<pn>` | `{DisplayName: FilePath}` for every CAD file |
| `GET /mcmaster/cad/<pn>?fmt=STEP` | downloads the file into `browser-agent\files`, returns `{downloaded, bytes, format}` |

3-D formats seen: IGES, Parasolid (± no threads), 3-D PDF, SAT, Solidworks, STEP
(± no threads). Verified: a real `ISO-10303-21` STEP AP203 file, 1.5 MB, in 1.2 s.

## Important operational notes

- **Only one process can hold the browser profile at a time.** Stop the browser
  MCP window / other browser-agent runs before the daemon warms, or you'll get
  "profile already in use". `siteapi.py stop` frees it again.
- **Rate limits still apply.** These are the same endpoints the site's own pages
  call, but McMaster watches request *patterns*, not just browsers. Keep volume
  sane; if you ever see "Access has been restricted", stop for 15–30 min.
- **A sudden run of `{"_error":"upstream","status":403}` with an Akamai comment
  body means you're throttled, not broken.** Product *pages* still load normally
  while the XHR endpoints 403. Wait 15–30 min and retry before debugging code.
- **Sessions can expire.** If a call returns `{_error:"upstream", status:401/403}`,
  the login lapsed — open the profile in the browser-agent and log in again.

## Protolabs — Playwright browser automation (not the fast API)

Protolabs stays on browser automation by design. Its portal is a BFF + IdentityServer
SPA that issues **session-only cookies**: the auth cookie dies when the browser
closes, so there's no session to persist and no token the browser ever sees. A
token/refresh-token client was tried and removed — it can't be made to work without
a live browser session anyway.

So Protolabs is driven by a real Playwright window you sign into:

```
python siteapi.py protolabs discover   # opens a browser, sign in, records API traffic
```

That runs `netspy.py`, which waits for you to log in and captures the portal's JSON
calls to `netlog_protolabs.json`. `protolabs_capture.py` is a richer variant (full
response bodies + storage dump). Each run needs its own sign-in — that's a property
of the site, not the tool.

## Files

- `siteapi.py` — the daemon + client (this is the tool).
- `netspy.py` — network-capture helper used to discover a site's JSON endpoints.
- `netlog_*.json` — captured endpoint samples (cookies stripped), for reference.
