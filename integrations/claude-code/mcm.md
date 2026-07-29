---
description: Search McMaster-Carr with natural language at API speed (live JSON, no browsing).
argument-hint: <what you're looking for, or a part number>
allowed-tools: Bash, Read, Glob
---

Find what the user describes in `$ARGUMENTS` on McMaster-Carr and answer with
real product data. You translate natural language into queries; the `mcm`
command (on PATH, from any folder) returns the site's own live JSON in
~0.2–0.8 s per call — never open a browser for this.

```
mcm search "socket head cap screw"    keyword or part-number search
mcm product 91290A115                 full product JSON: specs, price, CAD list
mcm cadlinks 91290A115                {format: path} of available CAD files
mcm cad 91290A115 STEP                download CAD into browser-agent/files
```

First call auto-starts a logged-in browser daemon (~5–10 s warmup). It opens a
visible Chromium window and keeps it warm; subsequent calls are fast. From Git
Bash invoke as `mcm.cmd` (bare `mcm` works in PowerShell/cmd).

## How to work

1. Turn the request into a search query the way a McMaster user would type it
   (noun phrase, imperial sizes spelled like "1/4"-20"). Part numbers go
   straight to `mcm product`.
2. `mcm search` returns categories/families; pick the best matches and pull
   `mcm product <pn>` for the few strongest candidates (2–3, not ten — see
   rate limits). Product JSON has specs, price, and available CAD.
3. Answer with: part number(s), the specs that match the user's requirements,
   price, and whether STEP CAD is available. Offer to download CAD.

## Rate limits and failure modes — IMPORTANT

- McMaster watches request PATTERNS. Keep volume sane: one search plus a few
  product lookups per question. Never loop over dozens of parts.
- A sudden run of `{"_error":"upstream","status":403}` with an Akamai comment
  body means THROTTLED, not broken. Stop, tell the user to wait 15–30 min. Do
  not retry in a loop and do not start debugging the tool.
- `401/403` on the first call can also mean the login lapsed — say so; the
  user re-logs via the browser-agent profile.
- Only one process may hold the shared browser profile: if the daemon reports
  "profile already in use", another browser-agent window/McMaster session is
  open and must be closed first. (The Protolabs quote daemon uses its own
  separate profile — no conflict there.)

## What `mcm` is not for

`mcm` answers "what is this part". It does not answer "what does McMaster
sell rods in" — keyword search returns `PageCount: 0` for that shape of
question, because category taxonomy is page-shaped data, not JSON. Use
`/browse` for that.
