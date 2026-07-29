---
description: Browse any supplier's catalog in a real browser and answer from what's on the page.
argument-hint: <supplier> <what you're looking for>  (or a catalog URL)
allowed-tools: Bash, Read, Glob
---

Answer what the user asks in `$ARGUMENTS` using live supplier catalog pages.
You translate natural language into a query; the `browse` command (on PATH,
from any folder) opens a real Chromium window, reads the rendered page, and
prints what it found.

```
browse grainger "flat washers"             search a supplier
browse fastenal "socket head cap screw"    part rows with links
browse mcmaster rods --facet Material      expand a filter facet completely
browse https://site.com/catalog/ --json    read any catalog URL
```

Options: `--facet <Name>` (repeatable), `--json`, `--settle N`,
`--keep-open N`, `--profile DIR`, `--no-warmup`.
From Git Bash invoke as `browse.cmd`.

`browse` always opens a visible Chromium window — that window IS the deliverable
the user expects to see. Never substitute the in-app browser pane, Claude in
Chrome, or any other browsing tool for it.

## Which tool for which question

- **"What is this part / what does it cost / get me the CAD"** on McMaster →
  use `/mcm`. It hits McMaster's own JSON in under a second.
- **"What materials does X come in", "what categories exist", "what can I
  filter by"** → use `browse`. That data is page-shaped; McMaster's keyword
  search returns `PageCount: 0` for it.
- **Any supplier that isn't McMaster** → `browse` is the only option.

## How to work

1. Pick the supplier and turn the request into the query a customer would
   type. Known names: `mcmaster`, `grainger`, `misumi`, `fastenal`,
   `digikey`, `mouser`. Anything else: pass a full catalog URL.
2. Read the output. `categories/materials` are landing-page tiles with counts;
   `products` are item rows with prices where the site renders them.
3. If the answer is "which materials/brands/sizes can I get", pass
   `--facet Material` (or `Brand`, `Diameter`, …). Facet names that exist on
   the page are listed for you when nothing else matches.
4. Answer with concrete data — counts, part numbers, prices, filter values —
   and say which supplier and page it came from.

## Two traps this tool handles, and you should not re-introduce

- **Facets are virtualized.** Only ~18 filter options exist in the DOM at
  once; the rest load as the list scrolls. Reading page text yields a
  clean-looking alphabetical list that silently stops mid-alphabet (McMaster
  washer materials appear to end at "Rubber", hiding Steel, Stainless,
  Titanium). `--facet` scrolls to completion. Never report a facet list read
  any other way — if it ends suspiciously early in the alphabet, it is
  truncated.
- **First paint is empty.** These are SPAs. A page can show ~700 characters
  and no products for several seconds, which looks exactly like a block.
  `browse` polls until content stops growing. Don't add your own fixed sleeps.

## Category pages differ in kind

A *raw materials* category lists materials as tiles (McMaster rods: Steel
3,426, Aluminum 950, …). A *fastener* category lists product types instead
(washers: O-Rings, Lock Washers, Shims), and the materials only appear in the
Material facet. If tiles don't answer a material question, reach for `--facet
Material` before concluding anything.

## Blocks and limits — IMPORTANT

- `BLOCKED: rate-limited` means the supplier throttled you. STOP. Tell the
  user to wait 15–30 minutes. Retrying extends the block; do not loop and do
  not start debugging the tool.
- `BLOCKED: login_wall` on McMaster usually means a cold browser profile. Use
  `/mcm` for McMaster part data instead — its daemon holds a warm session.
- Keep volume low: a few page loads per question, never a crawl. Suppliers
  score request cadence, and that is what trips these blocks — not the
  browser and not the fingerprint.
- Never enter credentials into the browser yourself. If a site needs a login,
  say so and let the user sign in.
