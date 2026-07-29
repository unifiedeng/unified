r"""siteapi — a fast local HTTP API in front of the sites the browser-agent logs
into. Turns "open a browser and click through the site" into a one-line request
that returns live JSON in a few hundred milliseconds.

Why a daemon: McMaster's product endpoints sit behind Akamai Bot Manager, which
validates a per-session sensor cookie (`_abck`) that only stays valid inside the
real browser. So instead of scraping the DOM (slow) or replaying with plain
`requests` (403s), siteapi keeps ONE logged-in headed browser window warm and
issues the site's own JSON `fetch()` calls from inside the page. After a ~5 s one-time
warmup, each part lookup is ~200-300 ms. Nothing is cached: every call is live.

USAGE (client — auto-starts the daemon if it isn't running):

  python siteapi.py mcmaster search <query>       part-number / keyword search -> JSON
  python siteapi.py mcmaster product <partno>     full product JSON (specs, CAD, image)
  python siteapi.py mcmaster cadlinks <partno>    {format: url} for every CAD file
  python siteapi.py mcmaster cad <partno> [fmt]   download CAD (default STEP) -> browser-agent/files

  python siteapi.py serve                         run the daemon in the foreground
  python siteapi.py stop                          shut the daemon down
  python siteapi.py health                         is the daemon up?

Protolabs is NOT on this fast path — its portal hands out session-only cookies,
so it stays on Playwright browser automation (a real window you log into):
  python siteapi.py protolabs discover            open a browser, sign in, record
                                                  the portal's API traffic

It's a real HTTP API too — once the daemon is up you can curl it:
  curl http://127.0.0.1:8765/mcmaster/product/91290A115
  curl "http://127.0.0.1:8765/mcmaster/render?path=/washers/&scroll=1"
      rendered page HTML + the XHR calls the page made (endpoint discovery)
  curl "http://127.0.0.1:8765/mcmaster/raw?rel=/mv.../Search/WebSrchEng.aspx?..."
      one in-page fetch, raw body back
  POST /eval {path, scroll, js:[{code,arg}...]}
      run caller-supplied JS on the warm page — how `browse` reads McMaster
      without a cold profile of its own (see supplier.py read_via_daemon)

Only one process can hold the browser profile at a time, so stop the browser MCP
window (or other browser-agent runs) before serving. Set SITEAPI_PORT to change
the port.
"""

import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# SITEAPI_AGENT_DIR lets a secondary checkout (e.g. a git worktree) run this
# daemon against the primary checkout's browser-agent — its warm profile and
# logins.json — instead of starting a cold profile of its own.
AGENT_DIR = os.environ.get("SITEAPI_AGENT_DIR") or os.path.join(HERE, "browser-agent")
FILES_DIR = os.path.join(AGENT_DIR, "files")
STATE_DIR = os.path.join(AGENT_DIR, "state")
PORT = int(os.environ.get("SITEAPI_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"

MCM = "https://www.mcmaster.com"


# ===========================================================================
# Server side: one warm browser, in-page fetch per site.
# ===========================================================================

class SiteBrowser:
    """Holds one Playwright context with a page per origin, kept warm so the
    site's anti-bot session stays valid.

    Playwright's sync API is bound to the thread that created it, but the HTTP
    server dispatches each request on its own thread. So every browser operation
    is marshalled onto a single dedicated worker thread via a job queue; public
    methods just submit a closure and block for the result.
    """

    def __init__(self):
        self._jobs = queue.Queue()
        self._pages = {}          # origin -> Playwright Page
        self._warmed = set()      # origins whose warmup nav has run
        self._mv_cache = {}       # origin -> mv<digits> asset-version prefix
        self._ready = threading.Event()
        self._err = None
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        self._ready.wait()
        if self._err:
            raise self._err

    # Error messages that mean the user closed the Chromium window (or the
    # browser died). Closing the window is a supported way to put the daemon
    # to sleep: the next request relaunches the browser (~10 s warmup).
    _DEAD_BROWSER = ("has been closed", "target closed", "browser closed",
                     "browser has been disconnected", "connection closed")

    def _browser_dead(self, e):
        msg = str(e).lower()
        return any(s in msg for s in self._DEAD_BROWSER)

    def _relaunch(self):
        try:
            self._b.close()
        except Exception:
            pass
        # A just-closed window releases the profile lock after a few seconds.
        last = None
        for attempt in range(4):
            try:
                self._b = self._Browser(headless=False)
                break
            except Exception as e:
                last = e
                time.sleep(3 * (attempt + 1))
        else:
            raise last
        self._pages.clear()
        self._warmed.clear()
        self._mv_cache.clear()

    def _run(self):
        try:
            sys.path.insert(0, AGENT_DIR)
            from agent import Browser
            self._Browser = Browser
            self._b = Browser(headless=False)
        except Exception as e:  # startup failed
            self._err = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            fn, box = self._jobs.get()
            if fn is None:
                return
            try:
                box["result"] = fn()
            except Exception as e:
                if self._browser_dead(e):
                    try:
                        self._relaunch()
                        box["result"] = fn()  # retry once on a fresh browser
                    except Exception as e2:
                        box["error"] = e2
                else:
                    box["error"] = e
            finally:
                box["done"].set()

    def _submit(self, fn):
        box = {"done": threading.Event()}
        self._jobs.put((fn, box))
        box["done"].wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _page_for(self, origin):
        if origin not in self._pages:
            # reuse the context's first page for the first origin, new tabs after
            if not self._pages and self._b.page:
                pg = self._b.page
            else:
                pg = self._b._ctx.new_page()
            self._pages[origin] = pg
        return self._pages[origin]

    def _warm(self, origin, url):
        if origin in self._warmed:
            return
        pg = self._page_for(origin)
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(2)
        if origin == MCM:
            self._mcm_login_if_walled(pg)
        self._warmed.add(origin)

    def _mcm_login_if_walled(self, pg):
        """McMaster throws a 'To continue browsing, please log in' wall at
        automated browsers. Sign in with the mcmaster.com creds from
        browser-agent/logins.json (mirrors protolabs.py: secrets stay in that
        file, never printed). The wall renders in shadow DOM, so only
        Playwright locators see it — innerText does not.
        """
        try:
            if not pg.get_by_text("please log in").first.count():
                return
        except Exception:
            return
        with open(os.path.join(AGENT_DIR, "logins.json"), encoding="utf-8") as f:
            entry = json.load(f).get("mcmaster.com")
        if not entry:
            raise RuntimeError("mcmaster.com missing from browser-agent/logins.json")
        pw = pg.locator("input[type=password]").first
        email = pw.locator("xpath=preceding::input[@type='text' or @type='email'][1]")
        # Unhurried pacing: Akamai scores interaction timing, not just fingerprint.
        email.fill(entry["username"])
        time.sleep(0.8)
        pw.fill(entry["password"])
        time.sleep(0.8)
        pg.get_by_role("button", name="Log in", exact=True).first.click()
        try:
            pg.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)
        if pg.get_by_text("please log in").first.count():
            raise RuntimeError("mcmaster login did not clear the wall")

    _FETCH_JS = r"""
    async ({rel, hdrs, binary}) => {
      const t0 = performance.now();
      const r = await fetch(rel, {headers: hdrs||{}, credentials:"include"});
      if (binary) {
        if (r.status !== 200) return {status:r.status, b64:null};
        const buf = await r.arrayBuffer();
        let bin = ""; const bytes = new Uint8Array(buf);
        for (let i=0;i<bytes.length;i++) bin += String.fromCharCode(bytes[i]);
        return {status:r.status, b64: btoa(bin)};
      }
      const txt = await r.text();
      return {status:r.status, ms:Math.round(performance.now()-t0), body:txt};
    }"""

    def _fetch_raw(self, origin, warm_url, rel_url, headers=None, binary=False):
        """In-page fetch with one automatic recovery pass.

        Akamai flags the *scripted fetch* pattern before it flags real
        navigations (seen 2026-07-28: fetches 403 while page loads pass).
        On 403, perform a real navigation on the same page — its telemetry
        rehabilitates the session — then retry the fetch once.
        """
        def job():
            self._warm(origin, warm_url)
            pg = self._page_for(origin)
            arg = {"rel": rel_url, "hdrs": headers or {}, "binary": binary}
            res = pg.evaluate(self._FETCH_JS, arg)
            if res["status"] in (403, 429):
                pg.goto(warm_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    pg.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(2)
                pg.mouse.wheel(0, 600)
                time.sleep(1)
                res = pg.evaluate(self._FETCH_JS, arg)
                res["recovered"] = True
            return res
        return self._submit(job)

    def fetch_json(self, origin, warm_url, rel_url, headers=None):
        """Run fetch(rel_url) inside the page at `origin` and parse JSON."""
        res = self._fetch_raw(origin, warm_url, rel_url, headers)
        if res["status"] != 200:
            return {"_error": "upstream", "status": res["status"],
                    "body": res["body"][:400]}
        try:
            return json.loads(res["body"])
        except Exception:
            return {"_error": "not_json", "status": res["status"],
                    "body": res["body"][:400]}

    def fetch_text(self, origin, warm_url, rel_url, headers=None):
        """In-page fetch returning raw text (e.g. a server-rendered page)."""
        res = self._fetch_raw(origin, warm_url, rel_url, headers)
        return res.get("body", ""), res["status"]

    def fetch_bytes(self, origin, warm_url, rel_url):
        """Fetch a binary file (e.g. CAD) in-page, return raw bytes."""
        res = self._fetch_raw(origin, warm_url, rel_url, binary=True)
        if res["status"] != 200 or not res["b64"]:
            return None, res["status"]
        return base64.b64decode(res["b64"]), 200

    def render(self, origin, url, settle_s=2.0, scroll=False):
        """Navigate the origin's page to `url`, let the SPA paint, and return
        the rendered HTML plus the XHR/fetch URLs the page called. The XHR
        list is how you discover a site's own JSON endpoint for a page, to
        promote it to a fast fetch_json route later.

        scroll=True walks the page to the bottom first: long catalog pages
        lazy-render their lower sections, so an unscrolled read returns empty
        placeholders for everything below the fold.

        Runs as MANY small worker jobs, not one long one, with the waits on
        the calling thread — a scrolled render takes ~30 s and the worker is
        shared by every session on this machine, so quick product fetches
        must be able to interleave instead of queuing behind it."""
        state = {}

        def j_start():
            self._warm(origin, f"{origin}/")
            pg = self._page_for(origin)
            xhr = []
            handler = lambda r: (r.resource_type in ("xhr", "fetch")
                                 and xhr.append({"method": r.method, "url": r.url}))
            pg.on("request", handler)
            state.update(pg=pg, xhr=xhr, handler=handler)
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                pg.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

        self._submit(j_start)
        time.sleep(settle_s)
        try:
            if scroll:
                self._scroll_to_bottom(state["pg"])
        finally:
            def j_end():
                pg = state["pg"]
                pg.remove_listener("request", state["handler"])
                return {"url": pg.url, "html": pg.content(),
                        "xhr": state["xhr"]}
            result = self._submit(j_end)
        return result

    # The window is often not the scroller on SPA catalog pages — an inner
    # container holds the overflow (seen on McMaster: window.scrollBy is a
    # no-op there). Step whichever element actually scrolls.
    _SCROLL_JS = r"""
    () => {
      const cands = [document.scrollingElement,
                     ...document.querySelectorAll('*')]
        .filter(e => e && e.scrollHeight > e.clientHeight + 200);
      cands.sort((a, b) => b.scrollHeight - a.scrollHeight);
      const s = cands[0] || document.scrollingElement;
      s.scrollTop += s.clientHeight;
      return {len: document.body.innerText.length,
              atBottom: s.scrollTop + s.clientHeight + 4 >= s.scrollHeight};
    }"""

    def _scroll_to_bottom(self, pg):
        """Walk the page's real scroller to the bottom so lazy sections
        render. Fine-grained jobs with the sleeps on the calling thread, so
        other sessions' quick fetches interleave instead of queuing."""
        last, stable = -1, 0
        for _ in range(60):
            r = self._submit(lambda: pg.evaluate(self._SCROLL_JS))
            time.sleep(0.6)
            stable = stable + 1 if r["len"] == last else 0
            last = r["len"]
            if r["atBottom"] and stable >= 3:
                break

    def eval_on(self, origin, url, scripts, scroll=False, settle_s=2.0):
        """Navigate the warm page to `url`, optionally scroll it out, then
        evaluate each {code, arg} in `scripts` and return their results.
        This is how supplier.py's `browse` reads McMaster through the
        daemon's trusted session instead of a cold profile of its own —
        the extraction logic stays in supplier.py; this just lends the page."""
        def j_nav():
            self._warm(origin, f"{origin}/")
            pg = self._page_for(origin)
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                pg.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            return pg
        pg = self._submit(j_nav)
        time.sleep(settle_s)
        if scroll:
            self._scroll_to_bottom(pg)
        results = []
        for s in scripts:
            arg = s.get("arg")
            results.append(self._submit(
                lambda code=s["code"], arg=arg:
                    pg.evaluate(code, arg) if arg is not None
                    else pg.evaluate(code)))
        return {"url": self._submit(lambda: pg.url), "results": results}

    _BUNDLE_JS = r"""
    async (rels) => {
      const get = async rel => {
        const r = await fetch(rel, {headers: {"x-requested-with": "XMLHttpRequest"},
                                    credentials: "include"});
        return {status: r.status, body: await r.text()};
      };
      const [c, o, p] = await Promise.all(
        [get(rels.content), get(rels.order), get(rels.prsn)]);
      const out = {statuses: {content: c.status, order: o.status, prsn: p.status}};
      try { out.content = JSON.parse(c.body); } catch (e) { out.content = null; }
      try { out.order = JSON.parse(o.body); } catch (e) { out.order = null; }
      try {
        // ItmPrsnttn responses are a stream of 10-digit-length-prefixed
        // chunks (JSON and HTML mixed). Regex the pieces we need out of the
        // raw body instead of trusting the framing.
        const body = p.body;
        const tm = /"TitleTxt":"((?:[^"\\]|\\.)*)"/.exec(body);
        if (tm) out.title = JSON.parse('"' + tm[1] + '"');
        const at = body.indexOf("spec-table--pd");
        if (at >= 0) {
          const start = body.lastIndexOf("<table", at);
          const end = body.indexOf("</table>", at);
          if (start >= 0 && end > start) {
            const doc = new DOMParser().parseFromString(
              body.slice(start, end + 8), "text/html");
            const specs = {};
            for (const tr of doc.querySelectorAll("tr")) {
              const tds = tr.querySelectorAll("td");
              if (tds.length !== 2) continue;
              const k = tds[0].textContent.trim(), v = tds[1].textContent.trim();
              if (k && v) specs[k] = v;
            }
            out.specs = specs;
          }
        }
        out.notes = [...body.matchAll(
            /<p class="[^"]*copy[^"]*">([\s\S]{0,2000}?)<\/p>/g)]
          .map(m => new DOMParser().parseFromString(m[1], "text/html")
                      .body.textContent.trim())
          .filter(Boolean).slice(0, 8);
      } catch (e) { out.prsnError = String(e).slice(0, 160); }
      return out;
    }"""

    def fetch_product_bundle(self, origin, warm_url, rels):
        """Fetch product content + order info + item presentation in one
        parallel in-page round trip. Same 403 recovery as _fetch_raw."""
        def job():
            self._warm(origin, warm_url)
            pg = self._page_for(origin)
            res = pg.evaluate(self._BUNDLE_JS, rels)
            if any(s in (403, 429) for s in res["statuses"].values()):
                pg.goto(warm_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    pg.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(2)
                pg.mouse.wheel(0, 600)
                time.sleep(1)
                res = pg.evaluate(self._BUNDLE_JS, rels)
                res["recovered"] = True
            return res
        return self._submit(job)

    def mv_prefix(self, origin=MCM):
        """McMaster versions its API path as /mv<digits>/. Read it off the page,
        warming the origin first so there's real HTML to read. Cached: the
        version only changes on site deploys, and a stale value just makes the
        next fetch 404 (harmless — restart the daemon)."""
        if origin in self._mv_cache:
            return self._mv_cache[origin]
        def job():
            self._warm(origin, f"{origin}/")
            return self._page_for(origin).content()
        html = self._submit(job)
        m = re.search(r"/(mv\d+)/", html)
        if m:
            self._mv_cache[origin] = m.group(1)
        return m.group(1) if m else ""

    def close(self):
        def job():  # never raises, so a dead browser can't trigger a relaunch
            try:
                self._b.close()
            except Exception:
                pass
        try:
            self._submit(job)
        finally:
            self._jobs.put((None, None))


# --- McMaster request builders (mirror the site's own front-end calls) ------

def _mcm_events(partno):
    return urllib.parse.quote(json.dumps(
        [{"type": "ORDERINGMASTERPARTNUMBER", "selectionType": "SELECTION",
          "values": {"entities": partno, "preselectedComponentPartNumber": ""}}]))


def mcm_content(sb, partno):
    """ProductContent.aspx only: image + CAD list (1 upstream fetch)."""
    mv = sb.mv_prefix()
    rel = (f"/{mv}/WebParts/Navigate/ProductContent.aspx?partNumber={partno}"
           f"&clientNavigationEvents={_mcm_events(partno)}&features=rtrvcadviarps")
    return sb.fetch_json(MCM, f"{MCM}/{partno}/", rel,
                         headers={"x-requested-with": "XMLHttpRequest"})


def mcm_product(sb, partno):
    """Full product picture in one parallel round trip: title, price,
    delivery, spec table, family notes, CAD list, image.

    Discovered 2026-07-28 via the part page's own resource-timing entries
    (the service worker hides them from Playwright network events):
      ProductContent.aspx     -> image + CAD (all this endpoint has for
                                 family-table parts)
      ProductOrderInfo.aspx   -> price, unit, delivery, descriptions
      ItmPrsnttnWebPart.aspx  -> spec table + copy, as HTML inside JSON
                                 with a numeric length prefix
    """
    mv = sb.mv_prefix()
    ev = _mcm_events(partno)
    rels = {
        "content": (f"/{mv}/WebParts/Navigate/ProductContent.aspx"
                    f"?partNumber={partno}&clientNavigationEvents={ev}"
                    f"&features=rtrvcadviarps"),
        "order": (f"/{mv}/WebParts/OrderServer/ProductOrderInfo.aspx"
                  f"?partNumber={partno}&clientNavigationEvents={ev}"),
        "prsn": (f"/{mv}/WebParts/Navigate/ItmPrsnttnWebPart.aspx"
                 f"?partnbrtxt={partno}&componentpartnbrtxt="
                 f"&possiblecompnbrtxt=&attrcompitmids=&attrnm=&attrval="
                 f"&cntnridtxt=MainContent&proddtllnkclickedInd=false"
                 f"&printprsnttnInd=false&cssAlias=undefined"
                 f"&clientNavigationEvents={ev}"),
    }
    res = sb.fetch_product_bundle(MCM, f"{MCM}/{partno}/", rels)
    statuses = res.get("statuses", {})
    if all(s != 200 for s in statuses.values()):
        return {"_error": "upstream", "statuses": statuses}
    order = res.get("order") or {}
    pricing = order.get("pricingData") or {}
    content = res.get("content") or {}
    cad = (content.get("cadControlDat") or {}).get("AvailableCAD") or {}
    cad_files = {}
    for group in ("TwoDDownloads", "ThreeDDownloads"):
        for d in cad.get(group, []):
            cad_files[d["DisplayName"]] = d["FilePath"]
    out = {
        "partNumber": partno,
        "title": res.get("title") or " ".join(filter(None, [
            order.get("parentDescription"), order.get("suffixDescription")])),
        "price": pricing.get("price"),
        "priceLevels": pricing.get("priceLevels") or [],
        "unitOfMeasure": order.get("unitOfMeasure"),
        "delivery": order.get("deliveryMessage"),
        "specs": res.get("specs") or {},
        "notes": res.get("notes") or [],
        "cad": cad_files,
        "image": (content.get("displayImage") or {}).get("sourcePath"),
    }
    if res.get("recovered"):
        out["_recovered"] = True
    if res.get("prsnError"):
        out["_prsnError"] = res["prsnError"]
    bad = {k: s for k, s in statuses.items() if s != 200}
    if bad:
        out["_partial"] = bad
    return out


def mcm_search(sb, query):
    mv = sb.mv_prefix()
    q = urllib.parse.quote(query)
    rel = (f"/{mv}/Search/WebSrchEng.aspx?inpArgTxt={q}"
           f"&ignoreTranslationTooLong=true&useComponentPNSearch=true"
           f"&usePNDescSearch=true")
    return sb.fetch_json(MCM, f"{MCM}/", rel,
                         headers={"x-requested-with": "XMLHttpRequest"})


def mcm_cadlinks(sb, partno):
    data = mcm_content(sb, partno)
    if "_error" in data:
        return data
    cad = (data.get("cadControlDat") or {}).get("AvailableCAD") or {}
    out = {}
    for group in ("TwoDDownloads", "ThreeDDownloads"):
        for d in cad.get(group, []):
            out[d["DisplayName"]] = d["FilePath"]
    return out


# ===========================================================================
# HTTP daemon
# ===========================================================================

class Handler(BaseHTTPRequestHandler):
    sb = None  # set on the server

    def log_message(self, *a):
        pass  # quiet

    def _send(self, obj, code=200, raw=None, ctype="application/json"):
        if raw is not None:
            body = raw
        else:
            body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = urllib.parse.parse_qs(u.query)
        try:
            if parts == ["health"]:
                return self._send({"ok": True, "warmed": sorted(self.sb._warmed)})
            if parts[:1] == ["mcmaster"]:
                sub = parts[1] if len(parts) > 1 else ""
                if sub == "search":
                    return self._send(mcm_search(self.sb, q.get("q", [""])[0]))
                if sub == "product" and len(parts) > 2:
                    return self._send(mcm_product(self.sb, parts[2]))
                if sub == "cadlinks" and len(parts) > 2:
                    return self._send(mcm_cadlinks(self.sb, parts[2]))
                if sub == "cad" and len(parts) > 2:
                    return self._cad(parts[2], q.get("fmt", ["STEP"])[0])
                if sub == "raw":
                    rel = q.get("rel", [""])[0]
                    if not rel.startswith("/"):
                        return self._send({"error": "rel must start with /"}, 400)
                    body, status = self.sb.fetch_text(
                        MCM, f"{MCM}/", rel,
                        headers={"x-requested-with": "XMLHttpRequest"}
                        if q.get("xhr", ["0"])[0] == "1" else None)
                    return self._send(None, 200 if status == 200 else 502,
                                      raw=body.encode("utf-8"),
                                      ctype="text/plain; charset=utf-8")
                if sub == "render":
                    path = q.get("path", ["/"])[0]
                    scroll = q.get("scroll", ["0"])[0] not in ("0", "", "false")
                    return self._send(self.sb.render(MCM, MCM + path, scroll=scroll))
                if sub == "page":
                    body, status = self.sb.fetch_text(
                        MCM, f"{MCM}/", q.get("path", ["/"])[0])
                    if status != 200:
                        return self._send({"_error": "upstream",
                                           "status": status,
                                           "body": body[:400]}, 502)
                    return self._send(None, raw=body.encode("utf-8"),
                                      ctype="text/html; charset=utf-8")
                return self._send({"error": "unknown mcmaster route"}, 404)
            return self._send({"error": "not found", "path": u.path}, 404)
        except Exception as e:
            return self._send({"error": "exception", "detail": str(e)}, 500)

    def _cad(self, partno, fmt):
        links = mcm_cadlinks(self.sb, partno)
        if "_error" in links:
            return self._send(links, 502)
        want = [k for k in links if fmt.lower() in k.lower()]
        if not want:
            return self._send({"error": f"no {fmt} CAD", "available": list(links)}, 404)
        rel = urllib.parse.quote(links[want[0]])
        data, status = self.sb.fetch_bytes(MCM, f"{MCM}/{partno}/", rel)
        if data is None:
            return self._send({"error": "download failed", "status": status}, 502)
        os.makedirs(FILES_DIR, exist_ok=True)
        name = urllib.parse.unquote(links[want[0]].rsplit("/", 1)[-1])
        dest = os.path.join(FILES_DIR, name)
        with open(dest, "wb") as f:
            f.write(data)
        return self._send({"downloaded": dest, "bytes": len(data), "format": want[0]})

    def do_POST(self):
        if self.path == "/stop":
            self._send({"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif self.path == "/eval":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                q = json.loads(self.rfile.read(n) or b"{}")
                res = self.sb.eval_on(
                    MCM, MCM + q.get("path", "/"), q.get("js", []),
                    scroll=bool(q.get("scroll")),
                    settle_s=float(q.get("settle", 2.0)))
                self._send(res)
            except Exception as e:
                self._send({"error": "exception", "detail": str(e)}, 500)
        else:
            self._send({"error": "not found"}, 404)


def serve():
    sb = SiteBrowser()
    Handler.sb = sb
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"siteapi serving on {BASE} (pid {os.getpid()}) — Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sb.close()
        print("siteapi stopped")


# ===========================================================================
# Client side
# ===========================================================================

def _req(path, method="GET", timeout=90):
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _daemon_up():
    try:
        _req("/health", timeout=3)
        return True
    except Exception:
        return False


def ensure_daemon():
    if _daemon_up():
        return
    # Spawn `serve` as a detached background process.
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    log = open(os.path.join(STATE_DIR, "siteapi_daemon.log"), "a", encoding="utf-8") \
        if os.path.isdir(STATE_DIR) else subprocess.DEVNULL
    os.makedirs(STATE_DIR, exist_ok=True)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    print("starting siteapi daemon (first call warms the browser, ~5-8 s)...",
          file=sys.stderr)
    for _ in range(60):
        time.sleep(1)
        if _daemon_up():
            return
    raise SystemExit("daemon failed to start — see browser-agent/state/siteapi_daemon.log")


def emit(obj):
    print(json.dumps(obj, indent=1, default=str))


def client(args):
    cmd = args[0]
    if cmd == "mcmaster":
        if len(args) < 2:
            raise SystemExit(
                "usage: mcm search \"<query>\" | product <pn> | cadlinks <pn> "
                "| cad <pn> [STEP]")
        ensure_daemon()
        sub = args[1]
        if sub == "search":
            emit(_req("/mcmaster/search?q=" + urllib.parse.quote(args[2])))
        elif sub == "product":
            emit(_req("/mcmaster/product/" + args[2]))
        elif sub == "cadlinks":
            emit(_req("/mcmaster/cadlinks/" + args[2]))
        elif sub == "cad":
            fmt = args[3] if len(args) > 3 else "STEP"
            emit(_req(f"/mcmaster/cad/{args[2]}?fmt={urllib.parse.quote(fmt)}",
                      timeout=120))
        else:
            raise SystemExit(f"unknown mcmaster subcommand {sub!r}")
    elif cmd == "protolabs":
        raise SystemExit("Protolabs is driven by browser automation, not the fast "
                         "API — use the `quote` command (protolabs.py). See "
                         "SITEAPI.md for why its session cannot be persisted.")
    else:
        raise SystemExit(f"unknown command {cmd!r}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "serve":
        serve()
    elif args[0] == "stop":
        try:
            print(_req("/stop", method="POST", timeout=5))
        except Exception:
            print("daemon not running")
    elif args[0] == "health":
        print("up" if _daemon_up() else "down")
    else:
        client(args)


if __name__ == "__main__":
    main()
