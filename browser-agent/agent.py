"""Playwright Browser core shared by the unified tools.

`protolabs.py` (quoting) and `siteapi.py` (McMaster) both import `Browser`
from here: a persistent-profile Chromium wrapper with settle/overlay handling
and element-indexed page reading. No model, no API keys — the callers script
every action themselves.
"""

from pathlib import Path

import sys

from playwright.sync_api import sync_playwright

# Windows consoles often default to cp1252, which can't print arrows/em-dashes
# from page titles or status lines.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# JS that tags interactive elements with data-ba-idx and returns their metadata.
COLLECT_ELEMENTS_JS = """
() => {
  const selectors = 'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [role="radio"], [role="checkbox"], [role="combobox"], [role="switch"], [onclick], [contenteditable="true"],' +
    // Custom dropdowns/comboboxes (Vue/React) render options as plain <li>/<div>
    // with framework-attached click handlers — no role, no onclick attribute, so
    // the generic selectors above miss them. Capture option-ish items inside things
    // that look like an open dropdown/listbox/menu so they get clickable indexes.
    ' [role="listbox"] li, [role="menu"] li, [class*="option" i], [class*="rich-select"] li, [class*="dropdown"] li, [class*="menu"] li, [class*="listbox"] li, [class*="select"] li';
  const els = Array.from(document.querySelectorAll(selectors));
  const visible = els.filter(el => {
    // File inputs are kept even when hidden: sites hide the real <input type=file>
    // behind a styled button, and upload_file needs to target it.
    if (el.tagName === 'INPUT' && el.type === 'file') return true;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  });
  return visible.slice(0, 300).map((el, i) => {
    el.setAttribute('data-ba-idx', String(i));
    const r = el.getBoundingClientRect();
    const label = (el.innerText || el.value || el.getAttribute('aria-label') ||
                   el.getAttribute('placeholder') || el.getAttribute('title') || el.alt || '')
                  .trim().replace(/\\s+/g, ' ').slice(0, 120);
    return {
      index: i,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || undefined,
      label: label,
      href: el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 200) : undefined,
      in_viewport: r.top < window.innerHeight && r.bottom > 0,
    };
  });
}
"""


def _xpath_lit(s: str) -> str:
    """Return an XPath string literal for `s`, handling embedded quotes via concat()."""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


COOKIE_BANNER_SELECTORS = [
    "#onetrust-accept-btn-handler",   # OneTrust (Protolabs marketing site and many others)
    ".cky-btn-accept",                # CookieYes (Protolabs buildit quote portal)
    "#truste-consent-button",         # TrustArc
    "#hs-eu-confirmation-button",     # HubSpot
    ".cc-allow",                      # cookieconsent.js
]
COOKIE_BUTTON_LABELS = ["Allow All", "Accept All", "Accept all cookies", "I Accept", "Got it"]


class Browser:
    def __init__(self, headless: bool, profile_dir=None, attach_port=None, pace=0.0):
        self._pw = sync_playwright().start()
        self._attached = attach_port is not None
        self.pace = pace

        if self._attached:
            # Attach to a Chrome/Chromium the user started with
            # --remote-debugging-port. Their everyday profile comes along, so
            # the session carries the cookies, logins, and site standing that
            # normal browsing built up. A fresh automation profile has none of
            # that, which is why sites behind Akamai/Cloudflare wall it while
            # hand-driven browsing on the same machine sails through.
            # We drive that window; we never own or close it.
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{attach_port}"
            )
            if not self._browser.contexts:
                raise RuntimeError(
                    f"Connected to port {attach_port} but the browser has no open "
                    "context. Open a tab and retry."
                )
            self._ctx = self._browser.contexts[0]
            self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            return

        # Persistent profile: cookies and logins survive across runs, so the user
        # can log into a site (e.g. the Protolabs quote portal) once and stay in.
        profile_dir = profile_dir or Path(__file__).parent / "profile"
        launch_kwargs = dict(
            headless=headless,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            # Hide the navigator.webdriver automation flag — sites like McMaster-Carr
            # throw up login walls when they detect automation.
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            # Prefer the user's real Chrome install: its fingerprint trips far less
            # bot detection than Playwright's bundled Chromium.
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", **launch_kwargs
            )
        except Exception:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(profile_dir), **launch_kwargs
            )
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self):
        try:
            if self._attached:
                # Detach only. Closing the context would shut the window the
                # user opened, along with every other tab in it.
                self._browser.close()
            else:
                self._ctx.close()
        finally:
            self._pw.stop()

    def _dismiss_overlays(self):
        """Best-effort click on cookie-consent banners that block interaction."""
        for sel in COOKIE_BANNER_SELECTORS:
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=2000)
                    self.page.wait_for_timeout(400)
                    return
            except Exception:
                pass
        for label in COOKIE_BUTTON_LABELS:
            try:
                loc = self.page.get_by_role("button", name=label).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=2000)
                    self.page.wait_for_timeout(400)
                    return
            except Exception:
                pass

    def _settle(self):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # some pages never go network-idle; proceed with what we have
        self._dismiss_overlays()
        # Every state-changing action funnels through here, so this is the one
        # place that throttles how fast we walk a site. Sites that rate-limit
        # (McMaster's "Access has been restricted") score pages-per-minute, so
        # enforce pacing structurally rather than asking the model to remember.
        if self.pace:
            self.page.wait_for_timeout(int(self.pace * 1000))

    def navigate(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._settle()
        return f"Now at: {self.page.url} — {self.page.title()}"

    def read_page(self, filter_text: str = None) -> str:
        self._settle()
        elements = self.page.evaluate(COLLECT_ELEMENTS_JS)
        text = self.page.evaluate("() => document.body ? document.body.innerText : ''")
        text = " ".join(text.split())
        if len(text) > 6000:
            text = text[:6000] + " …[truncated]"
        if filter_text:
            f = filter_text.lower()
            elements = [
                el for el in elements
                if f in (el.get("label") or "").lower() or f in (el.get("href") or "").lower()
            ]
        lines = []
        for el in elements:
            desc = f"[{el['index']}] <{el['tag']}"
            if el.get("type"):
                desc += f' type={el["type"]}'
            desc += ">"
            if el.get("label"):
                desc += f" {el['label']!r}"
            if el.get("href"):
                desc += f" -> {el['href']}"
            if not el.get("in_viewport"):
                desc += " (off-screen)"
            lines.append(desc)
        header = f"INTERACTIVE ELEMENTS ({len(lines)}"
        header += f", filtered by {filter_text!r}):" if filter_text else "):"
        return (
            f"URL: {self.page.url}\nTitle: {self.page.title()}\n\n"
            f"PAGE TEXT:\n{text}\n\n"
            f"{header}\n" + "\n".join(lines)
        )

    def press_key(self, key: str) -> str:
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(300)
        return f"Pressed {key}. Now at: {self.page.url}"

    def _locator(self, index: int):
        loc = self.page.locator(f'[data-ba-idx="{index}"]')
        if loc.count() == 0:
            raise ValueError(
                f"No element with index {index}. The page changed — call read_page again."
            )
        return loc.first

    def click(self, index: int) -> str:
        loc = self._locator(index)
        try:
            loc.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            loc.click(timeout=10000)
        except Exception:
            # Some layouts (carousels, sticky overlays) never satisfy Playwright's
            # actionability checks — fall back to a direct DOM click.
            loc.evaluate("el => el.click()")
        self._settle()
        return f"Clicked element {index}. Now at: {self.page.url} — {self.page.title()}"

    def click_text(self, text: str, exact: bool = False, nth: int = 0) -> str:
        """Click an element by its visible text (Playwright text engine).

        Robust path for controls read_page can't index — custom dropdown options,
        menu items, etc. Matches the smallest element containing `text`.
        """
        loc = self.page.get_by_text(text, exact=exact)
        count = loc.count()
        if count == 0:
            # Fall back to a case-insensitive "contains" search over all elements.
            loc = self.page.locator(
                f"xpath=//*[contains(translate(normalize-space(.), "
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                f"{_xpath_lit(text.lower())})][not(.//*[contains(translate(normalize-space(.), "
                f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {_xpath_lit(text.lower())})])]"
            )
            count = loc.count()
        if count == 0:
            raise ValueError(
                f"No visible element with text {text!r}. Try read_page, a shorter substring, "
                f"or check the text is on screen."
            )
        target = loc.nth(min(nth, count - 1))
        try:
            target.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            target.click(timeout=8000)
        except Exception:
            target.evaluate("el => el.click()")
        self._settle()
        return (
            f"Clicked text {text!r} (match {min(nth, count - 1) + 1} of {count}). "
            f"Now at: {self.page.url} — {self.page.title()}"
        )

    def type_text(self, index: int, text: str, press_enter: bool = False) -> str:
        loc = self._locator(index)
        try:
            loc.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            # Some inputs (inside animated panels/modals) never satisfy Playwright's
            # visibility check even though they're fillable — don't hard-fail here.
            pass
        try:
            loc.fill(text, timeout=8000)
        except Exception:
            # Fallback for framework-controlled inputs that fill() can't act on:
            # set the value through the native setter and dispatch input/change so
            # React/Vue update their state (a plain el.value = ... is ignored by them).
            loc.evaluate(
                """(el, val) => {
                    el.focus();
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                text,
            )
        if press_enter:
            try:
                loc.press("Enter")
            except Exception:
                self.page.keyboard.press("Enter")
            self._settle()
        return f"Typed {text!r} into element {index}" + (
            f" and pressed Enter. Now at: {self.page.url}" if press_enter else "."
        )

    def select_option(self, index: int, option: str) -> str:
        loc = self._locator(index)
        try:
            loc.select_option(label=option, timeout=5000)
        except Exception:
            loc.select_option(value=option, timeout=5000)
        return f"Selected {option!r} in element {index}."

    def scroll(self, direction: str) -> str:
        delta = self.page.viewport_size["height"] - 100
        if direction == "up":
            delta = -delta
        self.page.mouse.wheel(0, delta)
        self.page.wait_for_timeout(300)
        return f"Scrolled {direction}."

    def go_back(self) -> str:
        self.page.go_back(wait_until="domcontentloaded", timeout=15000)
        self._settle()
        return f"Went back. Now at: {self.page.url} — {self.page.title()}"

    def screenshot(self) -> bytes:
        return self.page.screenshot(type="jpeg", quality=60)

    def upload_file(self, index: int, file_path) -> str:
        """Attach a local file to element `index` — a file input or a picker-opening button."""
        loc = self._locator(index)
        kind = loc.evaluate(
            "el => el.tagName.toLowerCase() + ':' + (el.getAttribute('type') || '').toLowerCase()"
        )
        if kind == "input:file":
            loc.set_input_files(str(file_path))
        else:
            # Not a file input — assume clicking it opens the OS file picker,
            # which Playwright intercepts as a file chooser.
            with self.page.expect_file_chooser(timeout=10000) as fc_info:
                loc.click()
            fc_info.value.set_files(str(file_path))
        self._settle()
        return f"Attached {file_path} to element {index}."

    def download_via_click(self, index: int, target_dir) -> str:
        """Click element `index` and capture the download it triggers."""
        with self.page.expect_download(timeout=30000) as dl_info:
            self._locator(index).click()
        download = dl_info.value
        name = Path(download.suggested_filename or "download.bin").name
        target = Path(target_dir) / name
        download.save_as(str(target))
        return f"Downloaded {name} -> {target} ({target.stat().st_size} bytes)"

    def fetch_url(self, url: str) -> tuple:
        """Fetch a URL directly (with the browser's cookies). Returns (filename, bytes)."""
        resp = self.page.request.get(url, timeout=60000)
        if not resp.ok:
            raise ValueError(f"GET {url} failed: HTTP {resp.status}")
        name = Path(url.split("?")[0].rstrip("/")).name or "download.bin"
        return name, resp.body()
