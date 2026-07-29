"""supplier.py — read any supplier's catalog in a real browser.

The companion to siteapi.py. Where siteapi issues a supplier's own JSON calls
for per-part data at API speed, this reads *page-shaped* data — what categories
exist, what materials a category is offered in, what a filter can narrow to.
That information lives in rendered pages and facet sidebars, not in JSON.

    supplier.py mcmaster rods
    supplier.py mcmaster standard-washers --facet Material
    supplier.py grainger "safety glasses"
    supplier.py https://www.mcmaster.com/products/rods/ --json

Two things make catalog pages lie to naive scrapers, and both are handled here:

1. First paint is empty. These are SPAs; a page can report 700 characters of
   chrome and no products for several seconds. A fixed sleep either wastes
   time or reads too early and looks exactly like a block. We poll until the
   content stops growing.

2. Filter facets are virtualized. Only ~18 options exist in the DOM at a time,
   and the list scrolls internally. Reading page text yields a clean-looking
   alphabetical list that silently stops mid-alphabet — McMaster's washer
   materials appear to end at "Rubber", hiding Steel, Stainless, and Titanium.
   We scroll each facet's own container until its option set stops growing.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("supplier.py needs Playwright:\n  pip install playwright\n  playwright install chromium")

HERE = Path(__file__).resolve().parent
PROFILE_ROOT = HERE / "browser-agent"


# --- Supplier adapters -----------------------------------------------------
#
# Only two things are supplier-specific: how a query becomes a URL, and any
# per-site quirk worth remembering. Everything else is generic DOM work, so a
# site with no entry here still works if you hand it a URL directly.

SUPPLIERS = {
    "mcmaster": {
        "home": "https://www.mcmaster.com",
        # McMaster has no ?q= search URL; /products/<slug>/ doubles as both
        # category and search. Multi-word queries become hyphenated slugs.
        "search": "https://www.mcmaster.com/products/{slug}/",
        "note": "Category pages ARE the search. Rate-limited: keep to a few "
                "page loads per question. Fastener categories list types, not "
                "materials — read the Material facet for those.",
    },
    "grainger": {
        "home": "https://www.grainger.com",
        "search": "https://www.grainger.com/search?searchQuery={q}",
    },
    "misumi": {
        "home": "https://us.misumi-ec.com",
        "search": "https://us.misumi-ec.com/vona2/result/?Keyword={q}",
    },
    "fastenal": {
        "home": "https://www.fastenal.com",
        "search": "https://www.fastenal.com/product?query={q}",
    },
    "digikey": {
        "home": "https://www.digikey.com",
        "search": "https://www.digikey.com/en/products/result?keywords={q}",
    },
    "mouser": {
        "home": "https://www.mouser.com",
        "search": "https://www.mouser.com/c/?q={q}",
    },
}

# Phrases that mean "you are not seeing the catalog", regardless of HTTP status.
BLOCK_PATTERNS = [
    ("restricted", "Access has been restricted"),
    ("login_wall", "please log in"),
    ("captcha", "Enter the characters you see"),
    ("captcha", "unusual traffic"),
    ("cloudflare", "Checking your browser"),
    ("cloudflare", "Verify you are human"),
    ("denied", "Access Denied"),
]


def build_url(target: str, query: str | None) -> tuple[str, dict]:
    """Resolve (supplier, query) or a bare URL into a URL plus its adapter."""
    if target.startswith(("http://", "https://")):
        return target, {}
    site = SUPPLIERS.get(target.lower())
    if not site:
        known = ", ".join(sorted(SUPPLIERS))
        sys.exit(f"unknown supplier '{target}'. Known: {known}\n"
                 f"(or pass a full URL instead)")
    if not query:
        return site["home"], site
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    return site["search"].format(q=query.replace(" ", "+"), slug=slug), site


# --- In-page extraction ----------------------------------------------------

JS_STATE = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  return {len: t.length, title: document.title, text: t.slice(0, 200000)};
}
"""

JS_EXTRACT = r"""
() => {
  const t = document.body.innerText;
  const num = s => { const m = t.match(s); return m ? m[1] : null; };
  const total = num(/([\d,]+)\s+[Pp]roducts/) || num(/([\d,]+)\s+[Rr]esults/)
             || num(/of\s+([\d,]+)/);

  // Tiles: a labelled thing with a count next to it ("Aluminum / 950 products").
  // Common to catalog landing pages across suppliers. Page chrome (share and
  // print controls, pagers) sits next to the page-total line and would match
  // the shape, so screen those labels out.
  const CHROME = /^(forward|print|share|email|back|next|previous|view all|see all|sort by)$/i;
  const tiles = [];
  for (const e of document.querySelectorAll('a,div,li')) {
    const s = (e.innerText || '').trim();
    if (!/^[A-Z][^\n]{1,44}\n[\s\S]{0,240}\n[\d,]+ (?:[Pp]roducts?|[Ii]tems?|[Rr]esults?)$/.test(s)) continue;
    const L = s.split('\n');
    if (CHROME.test(L[0].trim())) continue;
    const a = e.closest('a') || e.querySelector('a');
    tiles.push([L[0].trim(), L[L.length - 1].trim(),
                a ? (a.getAttribute('href') || '') : '']);
  }
  // Inline-count variant: "Terminals - Ring Connectors (18,432 Items)".
  // DigiKey (and others) render refinement links this way on one line.
  for (const a of document.querySelectorAll('a')) {
    const s = (a.innerText || '').replace(/\s+/g, ' ').trim();
    const m = /^(.{2,60}?)\s*\(([\d,]+)\s*(?:items?|products?|results?)\)$/i.exec(s);
    if (m && !CHROME.test(m[1].trim())) tiles.push([m[1].trim(), m[2] + ' items']);
  }

  // Product links: anchors that look like they point at an item, with a price
  // nearby if one is rendered.
  const products = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const label = (a.innerText || '').trim().split('\n')[0];
    if (!label || label.length < 3 || label.length > 90) continue;
    const href = a.getAttribute('href') || '';
    // Item-shaped paths (incl. Misumi /vona2/detail/, Mouser /ProductDetail/),
    // plus McMaster's bare part-number links (/94610A236/).
    if (!/\/(p|product|products|item|itm|dp)\//i.test(href) &&
        !/detail/i.test(href) &&
        !/^\/?\d{4,6}[A-Z]{1,3}\d*\/?$/.test(href)) continue;
    // Category and filter links live under the same /product/ prefix as items
    // on Fastenal and DigiKey, so path shape alone can't tell them apart.
    if (/\/([a-z-]*categor(y|ies)|filter|browse)\/|\/result\?|\/products\/\?|categoryId=|[?&]fsi=|~~/i.test(href)) continue;
    // /products/<slug>/ with nothing deeper is catalog navigation, not an item.
    if (/\/products\/[a-z0-9-]+\/?$/i.test(href)) continue;
    const near = (a.closest('li,div,article,tr') || a).innerText || '';
    const price = (near.match(/\$[\d,]+\.\d{2}/) || [])[0] || null;
    products.push([label, href, price]);
  }
  // A real results page prices its rows. When several priced rows exist, the
  // unpriced ones are navigation, so drop them; when none do, keep everything
  // rather than returning an empty list on a category landing page.
  const priced = products.filter(p => p[2]);
  const productRows = priced.length >= 3 ? priced : products;

  const dedupe = arr => {
    const m = new Map();
    for (const row of arr) if (!m.has(row[0])) m.set(row[0], row);
    return [...m.values()];
  };

  // Candidate facet headings: short leaf labels that sit above a cluster of
  // links. Cheap heuristic, but it surfaces the names to pass to --facet.
  const facetNames = [];
  for (const e of document.querySelectorAll('*')) {
    if (e.children.length) continue;
    const s = (e.textContent || '').trim();
    if (!s || s.length > 34 || !/^[A-Z]/.test(s) || /\d/.test(s)) continue;
    let box = e.parentElement;
    for (let i = 0; i < 3 && box; i++) box = box.parentElement;
    if (box && box.querySelectorAll('a').length >= 3) facetNames.push(s);
  }

  return {
    title: document.title,
    url: location.href,
    total,
    tiles: dedupe(tiles).slice(0, 200),
    products: dedupe(productRows).slice(0, 60),
    facetNames: [...new Set(facetNames)].slice(0, 60),
  };
}
"""

# Expand one facet by scrolling its own container. Generic: keyed off a heading
# with the given text and the nearest scrollable ancestor, not site CSS classes.
JS_FACET = r"""
async (name) => {
  const heads = [...document.querySelectorAll('*')].filter(
    e => !e.children.length && (e.textContent || '').trim() === name);
  if (!heads.length) return {found: false, values: []};

  const scrollableUnder = root => {
    for (const el of [root, ...root.querySelectorAll('*')])
      if (el.scrollHeight > el.clientHeight + 8 && el.querySelectorAll('a,label').length > 2)
        return el;
    return root;
  };

  let best = [];
  for (const h of heads) {
    let box = h.parentElement;
    for (let up = 0; up < 3 && box && box.querySelectorAll('a,label').length < 3; up++)
      box = box.parentElement;
    if (!box) continue;
    const scroller = scrollableUnder(box);
    const seen = new Map();
    const grab = () => {
      for (const a of scroller.querySelectorAll('a,label')) {
        const txt = (a.textContent || '').trim();
        if (txt && txt !== name && txt.length < 60 && !seen.has(txt))
          seen.set(txt, a.getAttribute('href') || '');
      }
    };
    grab();
    let stable = 0;
    for (let i = 0; i < 80 && stable < 4; i++) {
      const before = seen.size;
      scroller.scrollTop = scroller.scrollHeight;
      await new Promise(r => setTimeout(r, 220));
      grab();
      stable = seen.size === before ? stable + 1 : 0;
    }
    if (seen.size > best.length) best = [...seen.entries()];
  }
  return {found: true, values: best};
}
"""


def detect_block(text: str) -> str | None:
    for kind, needle in BLOCK_PATTERNS:
        if needle.lower() in text.lower():
            return kind
    return None


# --- Login walls -----------------------------------------------------------
#
# McMaster shows a "To continue browsing, please log in" wall to browser
# profiles it does not trust yet — which is every fresh profile, so without
# this the first run always fails. One login with the mcmaster.com account in
# browser-agent/logins.json clears it, and the session cookie then lives in
# the profile, so later runs sail straight through. Same recipe as siteapi.py.


def find_logins(profile: Path) -> dict:
    """Load logins.json, looking next to the profile dir first so --profile
    pointing into another checkout picks up that checkout's credentials."""
    for root in (profile.parent, PROFILE_ROOT):
        p = root / "logins.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                break
    return {}


def _wall_visible(page, wait_ms: int = 0) -> bool:
    # The wall renders in shadow DOM: Playwright locators pierce it, but
    # body.innerText — and therefore detect_block — does not always see it.
    loc = page.get_by_text("please log in").first
    try:
        if wait_ms:
            loc.wait_for(state="visible", timeout=wait_ms)
            return True
        return bool(loc.count())
    except Exception:
        return False


def clear_mcmaster_wall(page, profile: Path, wait_ms: int = 0) -> str:
    """Returns 'clear' (no wall), 'cleared' (logged in — reload the target),
    or 'walled' (no credentials, or the login didn't take)."""
    if not _wall_visible(page, wait_ms):
        return "clear"
    entry = find_logins(profile).get("mcmaster.com")
    if not entry:
        print("mcmaster login wall: add mcmaster.com to browser-agent/"
              "logins.json (see logins.example.json) to clear it automatically",
              file=sys.stderr)
        return "walled"
    pw_box = page.locator("input[type=password]").first
    email = pw_box.locator("xpath=preceding::input[@type='text' or @type='email'][1]")
    # Unhurried pacing: the bot check scores interaction timing, not just
    # the fingerprint.
    email.fill(entry["username"])
    time.sleep(0.8)
    pw_box.fill(entry["password"])
    time.sleep(0.8)
    page.get_by_role("button", name="Log in", exact=True).first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)
    if _wall_visible(page):
        return "walled"
    print("mcmaster login wall cleared; session saved in the profile",
          file=sys.stderr)
    return "cleared"


WALL_CLEARERS = {"mcmaster.com": clear_mcmaster_wall}


def wall_clearer_for(url: str):
    for domain, fn in WALL_CLEARERS.items():
        if domain in url:
            return fn
    return None


def _eval(page, script, arg=None, tries: int = 4):
    """evaluate() that survives a redirect landing mid-call.

    Search URLs routinely bounce (query -> canonical category), which destroys
    the execution context. That is normal navigation, not an error, so wait for
    the new document and try again.
    """
    last = None
    for _ in range(tries):
        try:
            return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
        except Exception as e:
            if "context was destroyed" not in str(e) and "navigating" not in str(e).lower():
                raise
            last = e
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(1.0)
    raise last


def read_page(page, settle: float, quiet_rounds: int = 3):
    """Poll until the rendered text stops growing, or a block appears."""
    last, stable = -1, 0
    for _ in range(40):
        time.sleep(settle)
        state = _eval(page, JS_STATE)
        blocked = detect_block(state["text"])
        if blocked:
            return state, blocked
        if state["len"] == last:
            stable += 1
            if stable >= quiet_rounds and state["len"] > 1500:
                break
            # A page that stopped changing but never grew (a login wall, an
            # empty shell) is done rendering too — don't spin out the full
            # poll budget on it.
            if stable >= 8:
                break
        else:
            stable = 0
        last = state["len"]
    return state, None


def read_via_daemon(url: str, facets: list, settle: float) -> dict | None:
    """McMaster fast path: run the read on the siteapi daemon's warm page.

    The daemon's session is logged in and trusted, so it hits neither the
    login wall nor the cold-profile rate limit that a fresh browse profile
    does — the two ways a first run used to fail — and skipping browser
    launch + warmup is most of the speed. The extraction JS is the same one
    the window path uses; the daemon just lends its page (POST /eval).
    Returns None when this path can't serve (not McMaster, daemon won't
    start) so the caller falls back to opening its own window.
    """
    if "mcmaster.com" not in url:
        return None
    try:
        sys.path.insert(0, str(HERE))
        import siteapi
    except Exception:
        return None
    import urllib.error
    import urllib.request

    path = url.split("mcmaster.com", 1)[1] or "/"
    scripts = [{"code": JS_STATE}, {"code": JS_EXTRACT}]
    scripts += [{"code": JS_FACET, "arg": f} for f in facets]
    body = json.dumps({"path": path, "scroll": True, "settle": settle * 2,
                       "js": scripts}).encode("utf-8")

    def post():
        req = urllib.request.Request(
            siteapi.BASE + "/eval", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        siteapi.ensure_daemon()
        try:
            res = post()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            # A daemon from another checkout predating /eval holds the port.
            # Restart it on our code and try once more.
            print("daemon lacks /eval (older copy); restarting it...",
                  file=sys.stderr)
            urllib.request.urlopen(urllib.request.Request(
                siteapi.BASE + "/stop", method="POST"), timeout=5).read()
            time.sleep(6)
            siteapi.ensure_daemon()
            try:
                res = post()
            except urllib.error.HTTPError as e2:
                if e2.code != 404:
                    raise
                # Still 404 after a restart: the siteapi.py this checkout
                # spawns has itself lost /eval (another session's older copy
                # overwrote it). The window path would only trade this for a
                # login-wall/rate-limit roll of the dice, so fail loudly and
                # say what to fix instead.
                return {"requested": url, "ok": False, "error":
                        "siteapi.py on disk has no /eval route - an older "
                        "copy overwrote it. Restore a siteapi.py that has "
                        "eval_on (e.g. from git history) next to this "
                        "script, then rerun."}
    except (Exception, SystemExit) as e:
        print(f"daemon path unavailable ({e}); opening a browser window",
              file=sys.stderr)
        return None

    state, extract = res["results"][0], res["results"][1]
    out = {"requested": url, "via": "siteapi"}
    blocked = detect_block(state["text"])
    if blocked:
        out.update(ok=False, blocked=blocked, url=res["url"],
                   title=state["title"])
        return out
    out.update(ok=True, blocked=None, **extract)
    for name, fr in zip(facets, res["results"][2:]):
        out.setdefault("facets", {})[name] = fr["values"] if fr["found"] else None
    return out


def emit_result(out: dict, url: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps(out, indent=1))
        return 0 if out.get("ok") else 1

    if not out.get("ok"):
        if out.get("blocked") == "restricted":
            print("BLOCKED: rate-limited. Stop and wait 15-30 min — retrying extends it.")
        elif out.get("blocked") == "login_wall":
            print(f"BLOCKED: login_wall at {out.get('url', url)}\n"
                  "browse logs in automatically when browser-agent/logins.json "
                  "has credentials for this site (see logins.example.json).")
        elif out.get("blocked"):
            print(f"BLOCKED: {out['blocked']} at {out.get('url', url)}")
        else:
            print(f"FAILED: {out.get('error')}")
        return 1

    print(f"{out['title']}\n{out['url']}")
    if out.get("total"):
        print(f"total: {out['total']}")
    if out.get("tiles"):
        print(f"\ncategories/materials ({len(out['tiles'])}):")
        for row in out["tiles"]:
            label, count = row[0], row[1]
            href = row[2] if len(row) > 2 and row[2] else ""
            print(f"  {label:38s} {count:14s} {href[:60]}")
    if out.get("products"):
        print(f"\nproducts ({len(out['products'])}):")
        for label, href, price in out["products"][:25]:
            print(f"  {label[:52]:54s} {price or '':>10s}  {href[:48]}")
    for fname, vals in (out.get("facets") or {}).items():
        if vals is None:
            print(f"\nfacet '{fname}': not found on this page")
            continue
        print(f"\nfacet '{fname}' ({len(vals)} values):")
        for label, href in vals:
            print(f"  {label:38s} {href}")
    if not out.get("tiles") and not out.get("products") and out.get("facetNames"):
        print("\nno tiles/products matched. facet headings seen:")
        print("  " + ", ".join(out["facetNames"][:25]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read a supplier catalog page in a real browser.",
        epilog="Suppliers: " + ", ".join(sorted(SUPPLIERS)) + " (or pass a URL)")
    ap.add_argument("target", help="supplier name or full URL")
    ap.add_argument("query", nargs="?", help="what to search for")
    ap.add_argument("--facet", action="append", default=[],
                    help="expand this filter facet by name, e.g. --facet Material "
                         "(repeatable). Scrolls virtualized lists to completion.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="seconds between render polls (default 1.0)")
    ap.add_argument("--keep-open", type=float, default=0.0,
                    help="hold the window open this long after reading")
    ap.add_argument("--profile", help="browser profile dir (default: per-supplier)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the home-page visit before the target URL")
    args = ap.parse_args()

    url, site = build_url(args.target, args.query)

    # Fast path first: McMaster reads run on the siteapi daemon's warm,
    # logged-in window. An explicit --profile or --keep-open means the user
    # wants their own window — only then skip it.
    if not args.profile and not args.keep_open:
        out = read_via_daemon(url, args.facet, args.settle)
        if out is not None:
            return emit_result(out, url, args.json)

    name = args.target.lower() if not args.target.startswith("http") else "url"
    profile = Path(args.profile) if args.profile else PROFILE_ROOT / f"profile-browse-{name}"
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        # A profile just released by another process (e.g. `siteapi stop`)
        # stays locked for a few seconds while its Chromium exits; launching
        # into it then dies with TargetClosedError. Retry with backoff.
        ctx = None
        last_err = PlaywrightError("browser launch failed")
        for attempt in range(4):
            # Prefer the machine's real installed Chrome (same thing
            # browser-agent/agent.py does): several suppliers' bot checks
            # (Mouser, Grainger) deny the bundled Chromium outright but pass
            # the genuine browser. Fall back to bundled if Chrome is absent.
            for channel in ("chrome", None):
                try:
                    ctx = pw.chromium.launch_persistent_context(
                        str(profile),
                        channel=channel,
                        # Always a visible window: the user watches the read
                        # happen, and a headed browser is also what the
                        # suppliers' bot checks expect.
                        headless=False,
                        viewport={"width": 1400, "height": 950},
                        # Sites gate on the automation flag; this is the same
                        # masking a normal Chromium build would not need.
                        # Nothing here forges a different OS or device than
                        # the machine actually is.
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    break
                except PlaywrightError as e:
                    last_err = e
                    if channel == "chrome" and "chrome" in str(e).lower():
                        continue  # Chrome not installed; use bundled build
                    break     # profile busy or similar — back off and retry
            if ctx:
                break
            if attempt == 3:
                raise last_err
            print(f"profile busy, retrying in {3 * (attempt + 1)} s...",
                  file=sys.stderr)
            time.sleep(3 * (attempt + 1))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        out: dict = {"requested": url}
        try:
            # Enter through the front door. A cold profile deep-linking straight
            # into a category has no session cookie yet, and bot-management
            # services treat that as a scraper — McMaster serves a login wall
            # for it. Loading the home page first is what a person's browser
            # does anyway, and it costs one cheap request.
            wall_fn = wall_clearer_for(url)
            home = site.get("home")
            if home and not args.no_warmup and url.rstrip("/") != home.rstrip("/"):
                page.goto(home, wait_until="domcontentloaded", timeout=60000)
                time.sleep(args.settle * 2)
                # Catch the wall at the front door, where the shadow-DOM
                # variant hides from the innerText scan.
                if wall_fn:
                    wall_fn(page, profile, wait_ms=1500)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            state, blocked = read_page(page, args.settle)
            if not blocked and wall_fn and _wall_visible(page):
                blocked = "login_wall"
            if blocked == "login_wall" and wall_fn:
                if wall_fn(page, profile) == "cleared":
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    state, blocked = read_page(page, args.settle)
            if blocked:
                out.update(ok=False, blocked=blocked, url=page.url, title=state["title"])
            else:
                out.update(ok=True, blocked=None, **_eval(page, JS_EXTRACT))
                for f in args.facet:
                    res = _eval(page, JS_FACET, f)
                    out.setdefault("facets", {})[f] = res["values"] if res["found"] else None
            if args.keep_open:
                time.sleep(args.keep_open)
        except Exception as e:
            out.update(ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            ctx.close()

    return emit_result(out, url, args.json)


if __name__ == "__main__":
    sys.exit(main())
