r"""
Protolabs quoting — standalone Playwright driver, no model tokens.

Companion to mcmaster.py. Same shape: uses the browser-agent's Browser class and
its logins.json, prints JSON on stdout.

Modes:
  python protolabs.py serve                    hold ONE signed-in visible browser
                                               open and take commands over HTTP
                                               (see the daemon section below).
                                               Routes: /health /snapshot /session
                                               /act /materials /price /quote
                                               /lead /analysis /pdf /goto /eval
  python protolabs.py login                    sign in and report
  python protolabs.py quote <file.prt|.step>   convert + upload + ITAR + configure
                                               + DFM analysis + read price
  python protolabs.py probe [click ...]        dump page structure, optionally
                                               clicking through first
  python protolabs.py discover <file.step>     sign in, then PAUSE for you to
                                               drive the quote by hand while
                                               netspy records the endpoints

Credentials come from browser-agent/logins.json (protolabs.com) — never printed,
never hardcoded here.

WHY THIS IS BROWSER AUTOMATION AND NOT A siteapi ROUTE
------------------------------------------------------
See SITEAPI.md. The portal is a BFF + IdentityServer SPA issuing session-only
cookies: the auth cookie dies with the browser, so there is no session to persist
and no token the page ever sees. Every run needs its own sign-in. That is a
property of the site, not of this tool. Scripting removes the *tokens*, not the
*human* — budget one manual 2FA/CAPTCHA interception per run if the site asks.

PROFILE LOCK
------------
Only one process may hold a Chromium profile, so this uses its own
(browser-agent/profile-protolabs) rather than the shared one that siteapi,
mcmaster and the cad-web MCP browser contend for. Costs nothing here because the
Protolabs session never persists anyway. Override with PROTO_PROFILE.

TWO CLICK STRATEGIES ARE REQUIRED — using the wrong one fails silently
----------------------------------------------------------------------
* Material picker: must be a REAL Playwright click. This is a Vue SPA and it
  discards synthetic element.click(); the value appears to change, then reverts
  to "Make a selection".
* ITAR radios and the DFM "Done" button: must be JS clicks, because an overlay
  (.baseModal__message / .approval-spinner-container) intercepts real pointer
  events on top of them.

LEAVING THE CONFIGURE STEP
-------------------------
Take "Review Quote" whenever that button is usable, else "Request for Quote" —
branch on button_state(), NEVER on page text. "Request for Quote" is a permanent
button label on the configure page, so a text test reports a manual-RFQ part for
anything that has merely not finished pricing yet (this misfired on a part that
prices fine).

Review is where the price, lead times, DFM analysis and PDF all live. If Review
is reachable but still shows no price, fall back to the manual RFQ. Unpriced
parts render every figure as the placeholder "$—", so test with priced(), never
for a bare "$". 3-axis aluminium tops out at 559 x 356 x 95.3mm; 5-axis
envelopes are far smaller. Checkout is never clicked.

TWO WAYS TO DRIVE THIS
----------------------
quote() runs the whole flow blind and only reports at the end. That is fine
when the site behaves and opaque when it does not — a material label that does
not exist, a quantity that never applied, a button on a page it had already
left all surfaced as one empty result.

The step-wise API is the alternative: /session opens the quote and stops at
Configure, then /snapshot, /act, /materials and /price let the CALLER decide
each move, handing the page back every time. Sign-in stays automatic either
way. Because the caller runs its own loop, this costs no model API tokens.

Handing over the wheel means "checkout is never clicked" can no longer be a
property of the happy path, so _guard() enforces it on every action — along
with the ITAR save-as-default checkbox, which would answer an export-control
question for every future upload on the account.
"""

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.join(HERE, "browser-agent")
FILES_DIR = os.path.join(AGENT_DIR, "files")
sys.path.insert(0, AGENT_DIR)
from agent import Browser  # noqa: E402

HEADLESS = os.environ.get("PROTO_HEADLESS", "").strip().lower() in ("1", "true", "yes")

# Never let one browser operation monopolize the worker. Slow Protolabs pages
# are handled by retry/poll loops, so each individual Playwright wait or click
# gets at most ten seconds before control returns to the state machine.
UI_WAIT_MS = 10_000
POLL_SECONDS = 2


def pause(seconds):
    """Sleep in responsive chunks; no individual sleep may exceed 10 seconds."""
    remaining = max(0.0, float(seconds))
    while remaining:
        chunk = min(remaining, 10.0)
        time.sleep(chunk)
        remaining -= chunk


# --------------------------------------------------------------------------
# fast waits — fire the moment the page is ready, never the worst case
# --------------------------------------------------------------------------
#
# The old flow slept a fixed 1-5 seconds after every click and called
# Browser._settle(), which waits for networkidle — a state this SPA never
# reaches because it polls in the background forever, so every call burned its
# full 5s timeout. Together that added over a minute of dead time to a quote in
# which the page was actually ready in milliseconds. Every wait below is a
# condition: it returns as soon as the condition holds.

def settle(b, timeout_ms=3000):
    """domcontentloaded only. Never wait for networkidle on this SPA."""
    try:
        b.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass


def goto(b, url):
    """Navigate without Browser.navigate()'s networkidle settle."""
    b.page.goto(url, wait_until="domcontentloaded", timeout=30_000)


def wait_js(b, js, timeout=10, interval=0.25, arg=None):
    """Poll a JS predicate until truthy; its value, or None on timeout.

    Swallows evaluate errors: a click that starts navigation destroys the JS
    execution context mid-poll, which is progress, not failure.
    """
    deadline = time.time() + timeout
    while True:
        try:
            v = b.page.evaluate(js, arg) if arg is not None else b.page.evaluate(js)
            if v:
                return v
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def wait_visible(b, selector, timeout=10):
    """Event-driven wait for a visible element. True/False, never raises."""
    try:
        b.page.locator(selector).first.wait_for(
            state="visible", timeout=int(timeout * 1000))
        return True
    except Exception:
        return False


def wait_body(b, needle, timeout=10, interval=0.25):
    """Wait until the page text contains `needle`."""
    return bool(wait_js(
        b, "(n) => (document.body ? document.body.innerText : '').includes(n)",
        timeout, interval, arg=needle))
# Dedicated profile. Only one process may hold a Chromium profile, and the
# shared browser-agent profile is wanted by siteapi/mcmaster and the cad-web MCP
# browser. Protolabs' session is cookie-session-only and never persists, so an
# isolated profile costs nothing and lets Protolabs and McMaster run at once.
PROFILE = os.environ.get("PROTO_PROFILE") or os.path.join(AGENT_DIR, "profile-protolabs")

QUOTE_URL = "https://buildit.protolabs.com/?lang=en-US&getaquote=true"
SIGNIN_HOST = "identity.protolabs.com"
SIGNIN_PATH = "/signin"
SIGNUP_PATH = "/signup"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def creds():
    """Read protolabs.com credentials from browser-agent/logins.json.

    Mirrors mcmaster.creds(). Secrets stay in that file so they never land in
    source control, a diff, or a terminal scrollback.
    """
    path = os.path.join(AGENT_DIR, "logins.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "protolabs.com" not in data:
        raise SystemExit(
            "no 'protolabs.com' entry in %s — add {\"username\":..., \"password\":...}"
            % path
        )
    c = data["protolabs.com"]
    return c["username"], c["password"]


# --------------------------------------------------------------------------
# step 1-2: sign in   (IMPLEMENTED)
# --------------------------------------------------------------------------

def at_signin(b):
    """True when the IdentityServer sign-in wall is showing.

    Host check first: the SPA redirects buildit -> identity, so the hostname is
    the reliable signal. The password field is the fallback for in-page renders.
    """
    try:
        url = b.page.url or ""
        if SIGNIN_HOST in url:
            return True
        # Past the wall. The buildit SPA keeps a password input in its DOM, so
        # the field-presence fallback below MUST NOT run here — treating it as
        # the sign-in wall makes login() time out on a page it already reached.
        if "buildit.protolabs.com" in url:
            return False
        return b.page.locator("input[type=password]").count() > 0
    except Exception:
        return False


def at_signup(b):
    """True when the account-REGISTRATION page is showing (not sign-in)."""
    try:
        return SIGNUP_PATH in (b.page.url or "").lower()
    except Exception:
        return False


def ensure_signin_form(b, timeout=60):
    """Guarantee we are on the sign-in form, never the sign-up form.

    A cold browser profile can land on /signup, which carries its own email and
    password fields — indistinguishable from sign-in if you only look for
    inputs. Registering an account is never an acceptable action for this tool,
    so this switches to sign-in and verifies before anything is typed.

    Clicks the page's own "Sign In" control first; falls back to rewriting
    /signup -> /signin in the URL (which preserves the OIDC returnUrl, so the
    redirect back into the quote flow still works).
    """
    page = b.page
    if not at_signup(b):
        return True

    emit({"step": "login", "note": "landed on the sign-up page; switching to sign-in "
                                   "(this tool never creates an account)"})
    try:
        page.get_by_text("Sign In", exact=True).first.click(timeout=15000)
        time.sleep(2)
    except Exception:
        pass

    if at_signup(b):
        page.goto(page.url.replace(SIGNUP_PATH, SIGNIN_PATH),
                  timeout=min(timeout * 1000, UI_WAIT_MS))
        time.sleep(2)

    b._settle()
    if at_signup(b):
        return False
    # Positive confirmation rather than "not signup": a real sign-in form.
    try:
        page.locator("input[type=password]").first.wait_for(
            state="visible", timeout=min(timeout * 1000, UI_WAIT_MS))
    except Exception:
        return False
    return SIGNIN_PATH in (page.url or "").lower() or "buildit" in (page.url or "")


def login(b, timeout=150):
    """Navigate to the quote entry point and sign in if challenged.

    Returns True once past the wall. Fills both fields from logins.json — this
    is the 'enter it automatically' step; no typing, no tokens.
    """
    goto(b, QUOTE_URL)
    settle(b)
    # The entry URL loads ON buildit first and only then bounces out to
    # identity when there is no session — so "am I on buildit?" means nothing
    # until the redirect settles. The two terminal states are: the identity
    # wall, or the buildit dashboard actually rendered ("New Quote" present).
    # Wait for whichever comes first; a bare host check here reported
    # "already authenticated" from the sign-in wall.
    wait_js(b, "() => location.host.includes('identity.protolabs.com')"
               " || (location.host.includes('buildit.protolabs.com')"
               "     && (document.body ? document.body.innerText : '')"
               "        .includes('New Quote'))", 45, 0.3)

    if not at_signin(b):
        if wait_body(b, "New Quote", 10):
            emit({"step": "login", "ok": True, "note": "already authenticated"})
            return True
        emit({"step": "login", "ok": False, "url": b.page.url,
              "hint": "neither the sign-in wall nor the dashboard appeared"})
        return False

    page = b.page

    # NEVER create an account. On a cold profile the IdentityServer SPA can land
    # on /signup, which also has an email + password pair — filling and
    # submitting it would attempt to register. Get onto the sign-in form first,
    # and refuse to type anything if we cannot.
    if not ensure_signin_form(b, timeout=timeout):
        emit({"step": "login", "ok": False, "url": page.url,
              "hint": "could not reach the sign-in form (page looks like signup). "
                      "Refusing to fill it — this tool never registers an account."})
        return False

    user, pw = creds()

    # Verified against the live page: the email field is <input type=text> (not
    # type=email), so target the password field first and the text input by
    # exclusion. Both live in a plain form, no shadow DOM.
    email_box = page.locator("input[type=text]").first
    pw_box = page.locator("input[type=password]").first

    email_box.wait_for(state="visible", timeout=min(timeout * 1000, UI_WAIT_MS))
    email_box.fill(user)
    pw_box.fill(pw)

    # Submit button is <button type=submit> labelled "Sign In".
    page.locator("button[type=submit]").first.click()

    # Wait for the destination, not for the absence of the wall. The auth chain
    # is /connect/authorize -> /handshake -> SPA boot and routinely runs past a
    # minute, so the budget here is the caller's full timeout — NOT the 10s
    # UI cap, which used to abort a sign-in that was still succeeding.
    try:
        page.wait_for_url("**buildit.protolabs.com**", timeout=timeout * 1000)
        settle(b)
        # SPA mount signal: the dashboard's New Quote control. Waiting on it
        # replaces the old fixed 3s sleep and also means start_quote() can
        # click immediately.
        wait_js(b, "() => (document.body ? document.body.innerText : '')"
                   ".includes('New Quote')", 30, 0.25)
        # Re-verify: the SPA can bounce back out to identity after the URL match
        # briefly passes, and reporting success from the signup page is how this
        # went unnoticed before.
        final = b.page.url or ""
        if at_signup(b) or "buildit.protolabs.com" not in final:
            emit({"step": "login", "ok": False, "url": final,
                  "hint": "redirect did not settle on buildit"})
            return False
        emit({"step": "login", "ok": True, "url": final})
        return True
    except Exception:
        pass

    # Still on the wall: wrong creds, or an interception (2FA / CAPTCHA /
    # "verify your email"). Report, do not retry — repeated auto-retries are how
    # accounts get locked.
    #
    # Distinguish the causes, because they need opposite fixes: a rejected
    # credential shows an error banner, whereas a broken selector leaves the
    # fields empty. Report field *lengths*, never contents.
    diag = {}
    try:
        diag["email_len"] = len(email_box.input_value() or "")
        diag["pw_len"] = len(pw_box.input_value() or "")
    except Exception as e:
        diag["field_read_error"] = str(e)[:200]
    try:
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        needles = ("incorrect", "invalid", "not recognized", "try again", "locked",
                   "verify", "code", "error", "unable")
        diag["page_messages"] = [
            ln.strip() for ln in body.splitlines()
            if ln.strip() and any(n in ln.lower() for n in needles)
        ][:8]
    except Exception as e:
        diag["body_read_error"] = str(e)[:200]

    if diag.get("pw_len", 0) == 0:
        diag["verdict"] = "fields empty — selector problem, not credentials"
    elif diag.get("page_messages"):
        diag["verdict"] = "fields filled and site responded — see page_messages"
    else:
        diag["verdict"] = "fields filled, no visible error — likely 2FA/CAPTCHA or slow redirect"

    emit({
        "step": "login",
        "ok": False,
        "url": b.page.url,
        "diagnostic": diag,
        "hint": "not retrying automatically — repeated attempts lock accounts.",
    })
    return False


# --------------------------------------------------------------------------
# step 3-6: quote   (STUBS — fill from a discover run)
# --------------------------------------------------------------------------

SERVICE_BTN = {
    "CNC Machining": "cncServiceCardBtn",
    "3D Printing": "3dpServiceCardBtn",
    "Injection Molding": "imServiceCardBtn",
    "Sheet Metal": "smServiceCardBtn",
}

PROJECT_NAME_MAX = 40


def project_name_for(path):
    """A fresh, unique project name derived from the part file.

    Timestamped so repeat quotes of the same part never collide, and clipped to
    the field's 40-character limit.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return ("%s-%s" % (stem[:PROJECT_NAME_MAX - len(stamp) - 1], stamp))[:PROJECT_NAME_MAX]


def start_quote(b, service="CNC Machining", project=None):
    """Dashboard -> new-quote modal -> NEW project -> service -> upload step.

    Always creates a fresh project rather than dropping the quote into whichever
    project happens to be preselected (the modal defaults to the most recent
    one, which silently mixes unrelated parts together).

    The project picker is a custom dropdown whose options live in a
    display:none popper, so the trigger must be opened first — clicking the
    option directly hits a hidden element and does nothing. Naming the project
    is required: "Continue to CAD Upload" stays disabled until the field is
    non-empty.

    Returns the new project's GUID (this is the /quotes/new/<GUID> segment — a
    PROJECT id, not a quote number; the quote number appears later as "Quote
    NNNN-NNN").
    """
    page = b.page
    project = project or "quote-%s" % time.strftime("%Y%m%d-%H%M%S")

    btn = SERVICE_BTN.get(service)
    if not btn:
        raise SystemExit("unknown service %r (known: %s)"
                         % (service, ", ".join(SERVICE_BTN)))

    # The dashboard may still be mounting right after login; wait for the
    # control rather than failing the click.
    wait_body(b, "New Quote", 30)
    page.get_by_text("New Quote", exact=True).first.click(timeout=UI_WAIT_MS)

    # Each click waits for what it causes: modal -> project dropdown -> name
    # field. No fixed sleeps; each wait returns the instant the control exists.
    wait_visible(b, "[t-sel=selectProject-selectItemBtn]", 10)
    page.locator("[t-sel=selectProject-selectItemBtn]").first.click(timeout=UI_WAIT_MS)
    wait_visible(b, "[t-sel=Project-newProject]", 5)
    page.locator("[t-sel=Project-newProject]").first.click(timeout=UI_WAIT_MS)

    name_box = page.locator("input[placeholder^='Examples:']").first
    name_box.wait_for(state="visible", timeout=UI_WAIT_MS)
    name_box.fill(project)          # real fill: Vue ignores synthetic input

    page.locator("[t-sel=%s]" % btn).first.click(timeout=UI_WAIT_MS)

    # Continue enables once name + service have both registered with Vue.
    wait_js(b, "() => { const e = document.querySelector("
               "'[t-sel=createNewQuoteDialog-actionBtn]');"
               " return !!(e && !e.disabled); }", 8, 0.15)
    action = page.locator("[t-sel=createNewQuoteDialog-actionBtn]").first
    if action.is_disabled():
        emit({"step": "start_quote", "ok": False,
              "hint": "Continue disabled — project name or service did not register"})
        return None
    action.click(timeout=UI_WAIT_MS)

    page.wait_for_url("**/quotes/new/**", timeout=15_000)
    settle(b)
    pid = page.url.rsplit("/", 1)[-1].split("?")[0]
    emit({"step": "start_quote", "ok": True, "project": project,
          "project_id": pid, "service": service})
    return pid


def upload_model(b, path, timeout=300, itar=None):
    """Attach the CAD file to the Dropzone input and advance to Configure.

    The input is display:none behind a styled dropzone; set_input_files works on
    hidden inputs, so no click-and-native-dialog dance is needed.

    Upload + server-side geometry analysis is the slow step — Protolabs meshes
    the solid and runs DFM before it will price anything. Budget minutes.
    """
    if not os.path.isfile(path):
        raise SystemExit("no such file: %s" % path)
    page = b.page
    inp = page.locator("input.dz-hidden-input").first
    inp.set_input_files(path, timeout=UI_WAIT_MS)
    emit({"step": "upload", "file": os.path.basename(path),
          "bytes": os.path.getsize(path), "note": "uploading + analysing"})

    # Wait for the button to stop saying "0 files". Tight poll: the register
    # usually lands in a few seconds and every extra poll interval is dead time.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = page.evaluate("() => document.body.innerText")
        except Exception:
            body = ""
        if "Continue with 0 files" not in body and "Continue with" in body:
            break
        time.sleep(0.5)
    else:
        emit({"step": "upload", "ok": False,
              "hint": "file never registered after %ss" % timeout})
        return False

    # State loop to Configure. A modal may intercept at any point (ITAR
    # attestation, DFM/size warning, unit confirmation) — read it before
    # touching anything; blind-clicking through an unread dialog is how you
    # accept terms you never saw. The ITAR dialog is answered whenever it
    # shows up, Continue is clicked whenever it is usable, and the loop ends
    # the moment the wizard URL reaches /configure.
    deadline = time.time() + 90
    continued = False
    while time.time() < deadline:
        if "/configure" in (page.url or ""):
            settle(b)
            emit({"step": "upload", "ok": True, "url": page.url})
            return True

        modal = read_modal(b)
        if modal.get("present"):
            if "ITAR" not in (modal.get("text") or "") or itar is None:
                emit({"step": "upload", "ok": False, "blocked_by_modal": modal,
                      "hint": "pass --itar=no or --itar=yes to answer this. It is an "
                              "export-control declaration; the caller must make it."})
                return False
            # Caller supplied the declaration explicitly. Answer only that, and
            # do NOT tick "save as my default" — a persisted export-control
            # default is the user's decision to make in their own settings.
            # JS click, not a Playwright click: .baseModal__message overlays
            # these radios and intercepts real pointer events.
            sel = ("isSubjectToExportRestrictionsRadioButton" if itar
                   else "notSubjectToExportRestrictionsRadioButton")
            page.evaluate("(s) => document.querySelector('[t-sel='+s+']').click()", sel)
            time.sleep(0.3)
            # Never tick saveAsDefaultITARChoiceCheckbox — persisting an
            # export-control answer account-wide is the user's call, not ours.
            page.evaluate(
                "() => document.querySelector('[t-sel=itarDialog-actionBtn]').click()")
            emit({"step": "itar", "declared": "yes" if itar else "no"})
            # Modal dismissal re-enables Continue; allow another click.
            continued = False
            wait_js(b, "() => { const m = document.querySelector('.baseModal__content');"
                       " return !m || !(m.offsetWidth || m.offsetHeight); }", 10, 0.2)
            continue

        if not continued:
            try:
                page.get_by_text("Continue with", exact=False).first.click(timeout=2000)
                continued = True
            except Exception:
                pass
        time.sleep(0.3)

    emit({"step": "upload", "ok": False, "url": page.url,
          "hint": "never reached /configure after Continue"})
    return False


def read_modal(b):
    """Return {present, text, buttons} for any dialog overlaying the page."""
    try:
        return b.page.evaluate(
            """() => {
                const m = document.querySelector('.baseModal__content')
                      || document.querySelector('#placeToPutDialogs .baseModal__content');
                if (!m || !(m.offsetWidth || m.offsetHeight)) return {present: false};
                return {
                    present: true,
                    text: (m.innerText || '').slice(0, 1500),
                    buttons: [...m.querySelectorAll('button,a')]
                             .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 15)
                };
            }"""
        )
    except Exception as e:
        return {"present": False, "error": str(e)[:200]}


ns = lambda t: " ".join((t or "").split())

# A real figure, e.g. "$1,163.74". The page shows "$—" as a placeholder while
# unpriced, so testing for a bare "$" reports success on an empty quote.
_MONEY = re.compile(r"\$\s?\d[\d,]*\.\d{2}")


def priced(body):
    return bool(_MONEY.search(body or ""))


def priced_figures(body):
    """True when an actual figure ROW has rendered — a line that IS a money
    value. priced() matches money anywhere, which includes the '- $986.58'
    deltas on the Network delivery rows; those render before the unit price
    does, so gating a price read on priced() reads a page of '$—' placeholders.
    """
    return any(l.strip().startswith("$") and _MONEY.search(l)
               for l in (body or "").splitlines())


def button_state(b, tsel):
    """{present, visible, disabled} for a t-sel button — the reliable signal.

    Do NOT branch on page text here: "Request for Quote" is a permanent button
    label on the configure page, so text matching reports a manual-RFQ part for
    anything that merely has not finished pricing yet.
    """
    try:
        return b.page.evaluate(
            """(s) => { const e = document.querySelector('[t-sel=' + s + ']');
                        return e ? {present: true, disabled: !!e.disabled,
                                    visible: !!(e.offsetWidth || e.offsetHeight)}
                                 : {present: false, disabled: true, visible: false}; }""",
            tsel)
    except Exception:
        return {"present": False, "disabled": True, "visible": False}


def usable(st):
    return bool(st.get("present") and st.get("visible") and not st.get("disabled"))


# --------------------------------------------------------------------------
# page snapshots — the caller drives, so every action hands the page back
# --------------------------------------------------------------------------
#
# The blind scripted flow broke the moment Protolabs deviated from it: an exact
# material label that does not exist ("Stainless Steel" — the picker only lists
# grades), a quantity that was accepted and never applied, and an RFQ fallback
# that clicked a button belonging to a page the script had already navigated
# away from. Each failure was invisible until the final JSON came back empty.
#
# A snapshot replaces that guesswork. Every action returns the page state and
# the caller picks the next move, so a page that does not look like the script
# expected is a visible fact rather than a silent stall.
#
# Deliberately NOT a screenshot: every control here is addressed by its t-sel
# attribute, which no image exposes, and pixels cost far more to ship.

_SNAP_JS = r"""(limit) => {
  const ns = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const lab = e => ns(e.innerText) || ns(e.getAttribute('aria-label')) || ns(e.value) || '';
  const sel = e => { try { return e.classList.contains('selected'); } catch (_) { return false; } };

  const controls = [], seen = new Set();
  for (const e of document.querySelectorAll('[t-sel], button, pl-button')) {
    const tsel = e.getAttribute('t-sel');
    const text = lab(e);
    if (!tsel && !text) continue;
    const key = tsel || 'txt:' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    const v = vis(e);
    if (!v && !tsel) continue;
    controls.push({
      tsel: tsel || null,
      label: text.slice(0, 70),
      tag: e.tagName.toLowerCase(),
      visible: v,
      disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
      selected: sel(e),
    });
  }

  const fields = [...document.querySelectorAll('input, select')].filter(vis).map(e => {
    let ctx = e.closest('div');
    for (let i = 0; i < 4 && ctx && ns(ctx.innerText).length < 40; i++) ctx = ctx.parentElement;
    return {
      tsel: e.getAttribute('t-sel'),
      type: (e.type || e.tagName).toLowerCase(),
      value: e.type === 'checkbox' ? e.checked : e.value,
      label: ns(ctx ? ctx.innerText : '').slice(0, 70),
    };
  });

  const delivery = [...document.querySelectorAll('button.delivery-option')]
    .map(e => ({ label: ns(e.innerText), selected: sel(e) }));

  const m = document.querySelector('.baseModal__content')
        || document.querySelector('#placeToPutDialogs .baseModal__content');
  const modal = (m && vis(m)) ? {
    present: true,
    text: ns(m.innerText).slice(0, 1200),
    buttons: [...m.querySelectorAll('button, pl-button, a')].filter(vis).map(e => ({
      tsel: e.getAttribute('t-sel'), label: ns(e.innerText).slice(0, 50),
      disabled: !!e.disabled })).slice(0, 15),
  } : { present: false };

  // Newlines are preserved: _parse_review() reads this line by line. Only runs
  // of blank lines are collapsed. Returned in full — the caller derives its
  // flags from the whole page and truncates only what it ships back.
  const text = (document.body.innerText || '').replace(/\n{3,}/g, '\n\n');
  return { url: location.href, text, controls, fields, delivery, modal };
}"""


def _is_busy(body, step):
    """True only while a PART tile is still being analysed.

    The account's quote list carries rows like "0 days ago  4594-505  ...
    Quote - Analyzing", so a bare "Analyzing" in body test reports busy on any
    page that happens to list a pending quote — the landing page included. The
    original check got away with it only because it ran solely on Configure.
    """
    if step not in ("upload", "configure", "review"):
        return False
    return any("Analyzing" in ns(l) and "Quote - " not in ns(l)
               for l in body.splitlines())


def _wizard_step(url):
    for s in ("upload", "configure", "review"):
        if "/" + s in (url or ""):
            return s
    return None


def snapshot(b, limit=6000):
    """The page, as the caller needs to see it to choose the next action."""
    try:
        s = b.page.evaluate(_SNAP_JS, limit)
    except Exception as e:
        return {"error": str(e)[:300]}
    # Derive from the WHOLE page, then truncate. Deriving from the shipped
    # excerpt makes every flag a function of `limit` — a short limit reported
    # "Analyzing" from a stray word near the top and missed the price further
    # down.
    body = s.get("text") or ""
    s["step"] = _wizard_step(s.get("url"))
    # A part tile reading "Analyzing" means the quote engine has not returned
    # yet; acting now is what produced half-configured quotes. Wait instead.
    s["busy"] = _is_busy(body, s["step"])
    s["priced"] = priced(body)
    s["dims"] = next((ns(l) for l in body.splitlines() if l.strip().startswith("X:")), None)
    if "Order Summary" in body or "Receive by" in body:
        s["review"] = _parse_review(body)
    s["truncated"] = len(body) > limit
    s["text"] = body[:limit]
    return s


# Actions the caller is never allowed to drive. Handing over the wheel means
# "checkout is never clicked" can no longer be guaranteed by the happy path, so
# it is enforced here — the single point every action passes through.
# saveAsDefaultITARChoice is blocked for the same reason it always was: it
# answers an export-control question for every future upload on the account.
_FORBIDDEN = ("checkout", "placeorder", "submitorder", "paynow", "payment",
              "saveasdefault")


def _guard(target):
    t = (target or "").lower().replace(" ", "").replace("-", "").replace("_", "")
    for f in _FORBIDDEN:
        if f in t:
            raise PermissionError(
                "refusing to interact with %r — ordering and persisted "
                "export-control defaults are blocked by design. This tool "
                "quotes; a human places orders." % target)


def act(b, kind, target=None, value=None, exact=True, settle_s=1):
    """Perform ONE action, then hand the page back.

    Click strategy matters and gets the two failure modes backwards if guessed:
    a real Playwright click fails LOUDLY when an overlay intercepts it, while a
    JS click on the Vue material picker fails SILENTLY (the value appears to
    change, then reverts). So 'click' tries real-first and falls back to JS on
    an exception, reporting which it used; 'js_click' forces the JS path for
    the controls known to sit under .baseModal__message overlays.
    """
    page = b.page
    used = kind
    if kind in ("click", "js_click", "click_text", "fill"):
        _guard(target)

    if kind == "wait":
        pause(max(0, int(value or 10)))
    elif kind == "goto":
        goto(b, target)
    elif kind == "click_text":
        page.get_by_text(target, exact=bool(exact)).first.click(timeout=UI_WAIT_MS)
    elif kind == "click":
        try:
            page.locator("[t-sel=%s]" % target).first.click(timeout=UI_WAIT_MS)
            used = "click(real)"
        except Exception:
            page.evaluate(
                """(s) => { const e = document.querySelector('[t-sel='+s+']');
                            if (!e) throw new Error('no such t-sel: '+s);
                            e.scrollIntoView(); e.click(); }""", target)
            used = "click(js-fallback)"
    elif kind == "js_click":
        page.evaluate(
            """(s) => { const e = document.querySelector('[t-sel='+s+']');
                        if (!e) throw new Error('no such t-sel: '+s);
                        e.scrollIntoView(); e.click(); }""", target)
    elif kind == "fill":
        page.locator("[t-sel=%s]" % target).first.fill(str(value), timeout=UI_WAIT_MS)
    elif kind == "set_qty":
        return {"action": kind, "ok": set_quantity(b, value), **snapshot(b)}
    else:
        return {"action": kind, "ok": False,
                "error": "unknown action: %s" % kind, **snapshot(b)}

    time.sleep(settle_s)
    settle(b)
    return {"action": used, "target": target, "ok": True, **snapshot(b)}


def set_quantity(b, qty, timeout=60):
    """Set the part quantity and verify it actually took.

    configure() has always accepted a qty and never applied it — it only echoed
    the number back in its log line, so every quote was priced at whatever the
    form happened to be showing. The field is a Vue-bound input[type=number]:
    assigning .value alone is discarded, so fall back to the native setter plus
    the events v-model listens for, then re-read to confirm.
    """
    page = b.page
    want = str(int(qty))
    try:
        page.locator("input[type=number]").first.fill(want, timeout=UI_WAIT_MS)
    except Exception:
        page.evaluate(
            """(q) => { const el = document.querySelector('input[type=number]');
                        if (!el) throw new Error('no quantity field');
                        const set = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        el.focus(); set.call(el, q);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.blur(); }""", want)

    deadline = time.time() + timeout
    while time.time() < deadline:
        got = page.evaluate(
            "() => { const e = document.querySelector('input[type=number]');"
            " return e ? e.value : null; }")
        if got == want:
            tile = ("Quantity %s" % want) in ns(
                page.evaluate("() => document.body.innerText"))
            emit({"step": "quantity", "ok": True, "qty": want,
                  "confirmed_on_tile": tile})
            return True
        time.sleep(0.3)

    emit({"step": "quantity", "ok": False, "wanted": want, "got": got,
          "hint": "field reverted — the SPA discarded the change"})
    return False


_MATERIALS_JS = """() => Object.fromEntries(
    [...document.querySelectorAll('[t-sel^=CATMAT-]')]
        .map(e => [(e.innerText||'').replace(/\\s+/g,' ').trim(), e.getAttribute('t-sel')])
        .filter(([k]) => k))"""


def materials(b, _opened=False):
    """{label: t-sel code} for every CNC material the picker offers.

    The options only exist in the DOM while the picker is open, so a caller
    that simply asks gets {} and no hint as to why. Open it and retry once.
    """
    found = b.page.evaluate(_MATERIALS_JS)
    if found or _opened:
        return found
    # Open it and retry. Prefer the [t-sel=material] handle over the "Make a
    # selection" text: that placeholder only shows on an unconfigured part, and
    # a part that already has a material shows the material name instead.
    # Real clicks only — this is the Vue picker that discards synthetic ones.
    # Short click timeouts: a missing control should cost 2 seconds, not 10.
    for attempt in (lambda: b.page.locator("[t-sel=material]").first.click(timeout=2000),
                    lambda: b.page.get_by_text("Make a selection", exact=False)
                            .first.click(timeout=2000)):
        try:
            attempt()
        except Exception:
            continue
        got = wait_js(b, _MATERIALS_JS, 4, 0.15)
        if got:
            return got
    # Still nothing: the picker will not open. On a part Protolabs has locked
    # for manual analysis this is expected, not a bug.
    return {}


def select_material(b, material="Aluminum 6061-T651/T6", select_all=True):
    """Set the material on the configure page. Returns whether it stuck.

    Split out of configure() so a caller can make this one decision on its own,
    without also committing to a quantity and a four-minute repricing wait.

    IMPORTANT — two different click strategies are required on this page, and
    using the wrong one fails silently:
      * Material picker: must be a REAL Playwright click. The SPA is Vue and
        ignores synthetic element.click() — the value appears to change and then
        reverts to "Make a selection".
      * ITAR radio (see upload_model): must be a JS click, because the modal's
        message div intercepts pointer events.
    """
    page = b.page
    if select_all:
        try:
            # 1.5s, not 10: a single-part quote has no Select All, and burning
            # the full UI timeout on its absence was one of the fixed stalls.
            page.get_by_text("Select All", exact=True).first.click(timeout=1500)
        except Exception:
            pass                       # a single-part quote has no Select All

    # The material control does not exist until the part tile finishes
    # mounting after upload — arriving on Configure and clicking immediately
    # finds nothing and reads exactly like a locked part. Wait for the handle
    # itself first (its appearance is the real "configure is ready" signal).
    wait_visible(b, "[t-sel=material]", 60)

    # Open the picker. "Make a selection" is only the placeholder on an
    # UNCONFIGURED part — one that already has a material shows the material
    # name instead — so go via the stable t-sel handle first. Success is the
    # options existing in the DOM, not the click having been dispatched.
    opened = False
    open_deadline = time.time() + 20
    while not opened and time.time() < open_deadline:
        for attempt in (lambda: page.locator("[t-sel=material]").first.click(timeout=3000),
                        lambda: page.get_by_text("Make a selection", exact=False)
                                .first.click(timeout=2000)):
            try:
                attempt()
            except Exception:
                continue
            if wait_js(b, _MATERIALS_JS, 3, 0.15):
                opened = True
                break
    if not opened:
        emit({"step": "material", "ok": False, "wanted": material,
              "hint": "picker would not open — expected on a part Protolabs "
                      "has locked for manual analysis"})
        return False

    try:
        page.get_by_text(material, exact=True).first.click(timeout=UI_WAIT_MS)
    except Exception as e:
        emit({"step": "material", "ok": False, "wanted": material,
              "hint": "not offered: %s" % str(e)[:120],
              "available": sorted(materials(b, _opened=True))[:40]})
        return False

    # Verified stuck when the material name renders outside the (now closed)
    # picker. The Vue revert failure shows up here as a timeout, not a sleep.
    wait_body(b, material, 8, 0.25)
    ok = ns(page.evaluate("() => document.body.innerText")).find(material) >= 0
    emit({"step": "material", "ok": ok, "material": material,
          "hint": None if ok else "did not stick — was it a real click?"})
    return ok


def wait_priced(b, timeout=0):
    """Report whether the quote engine has settled. Does NOT block by default.

    Pricing is async. The old version sat in a 420-second poll, which made a
    stalled page indistinguishable from a slow one and left the caller with no
    output for minutes. Now timeout=0 means "look once and tell me": the caller
    calls again if `busy` is true, reasoning between attempts instead of
    sleeping through them. Pass a timeout only for a deliberately unattended run.
    """
    page = b.page
    deadline = time.time() + max(0, timeout)
    body = page.evaluate("() => document.body.innerText")
    while time.time() < deadline:
        if "Analyzing" not in body and (
                priced(body) or usable(button_state(b, "requestButton"))):
            break
        time.sleep(1)
        body = page.evaluate("() => document.body.innerText")

    is_priced = priced(body)
    settled = "Analyzing" not in body
    out = {"priced": is_priced, "settled": settled, "busy": not settled,
           "dims": next((ns(l) for l in body.splitlines()
                         if l.strip().startswith("X:")), None),
           "rfq_only": bool(usable(button_state(b, "requestButton")) and not is_priced)}
    emit({"step": "priced", **out})
    return out


def configure(b, material="Aluminum 6061-T651/T6", qty=1, timeout=420):
    """Select all parts, set material and quantity, wait for repricing."""
    if not select_material(b, material):
        emit({"step": "configure", "ok": False, "hint": "material not set"})
        return False

    # Set the quantity BEFORE the repricing poll, or the price that comes back
    # belongs to a different quantity than the caller asked for.
    qty_ok = set_quantity(b, qty)
    st = wait_priced(b, timeout)

    emit({"step": "configure", "ok": True, "material": material, "qty": qty,
          "qty_applied": qty_ok, "dims": st["dims"],
          "priced_on_configure": st["priced"]})
    return qty_ok


def proceed_from_configure(b, timeout=90):
    """Leave the configure step: Review Quote if available, else Request for Quote.

    Branching on button availability rather than on page text — see
    button_state(). Review is always preferred: it is where the price, the lead
    times, the DFM analysis and the PDF all live.
    """
    page = b.page
    deadline = time.time() + timeout
    while time.time() < deadline:
        review = button_state(b, "reviewButton")
        if usable(review):
            page.locator("[t-sel=reviewButton]").first.click(timeout=UI_WAIT_MS)
            try:
                page.wait_for_url("**/review**", timeout=UI_WAIT_MS)
            except Exception:
                pass
            # Review is ready when its summary content renders, not four
            # seconds after the URL changed.
            wait_js(b, "() => { const t = document.body ? document.body.innerText : '';"
                       " return t.includes('Order Summary') || t.includes('Receive by'); }",
                    10, 0.25)
            emit({"step": "proceed", "via": "review", "url": page.url})
            return "review"

        request = button_state(b, "requestButton")
        if usable(request):
            emit({"step": "proceed", "via": "request",
                  "note": "Review Quote unavailable — submitting the manual RFQ."})
            return "rfq" if request_for_quote(b) else False
        time.sleep(1)

    emit({"step": "proceed", "ok": False,
          "hint": "neither Review Quote nor Request for Quote became available"})
    return False


def click_pl(page, tsel):
    """Click a <pl-button> custom element by its t-sel. Returns True if clicked.

    Three things make these buttons awkward, and all three have bitten this
    script:
      * They are custom elements whose real <button> lives in a shadow root —
        clicking the HOST does nothing, you must reach shadowRoot's button.
      * A real Playwright click is intercepted by an overlaying .summary-card /
        .approval-spinner-container.
      * Their labels are direct text nodes, not <span>s, so any span-based text
        lookup silently finds nothing and the caller waits forever.
    Addressing them by t-sel and clicking the shadow button avoids all three.
    """
    try:
        return bool(page.evaluate(
            """(s) => { const el = document.querySelector('[t-sel=' + s + ']');
                        if (!el || !(el.offsetWidth || el.offsetHeight)) return false;
                        const inner = el.shadowRoot && el.shadowRoot.querySelector('button');
                        if (inner && inner.disabled) return false;
                        (inner || el).click();
                        return true; }""", tsel))
    except Exception:
        return False


def request_for_quote(b, timeout=240):
    """Submit the manual RFQ for a part Protolabs won't auto-price.

    Standing instruction from the account holder: when a part comes back
    unpriced, submit the request rather than stopping. Protolabs replies by
    email within a few hours with a quote and manufacturing analysis.

    This sends a request to Protolabs staff — it is NOT an order, and checkout
    is still never clicked.

    Selector notes: "Request for Quote" appears twice as text (the part tile's
    status label comes first in the DOM), so match the BUTTON by t-sel rather
    than by text. Both this and the confirm dialog need JS clicks — overlays
    intercept real pointer events.
    """
    page = b.page
    try:
        page.evaluate(
            """() => { const b=document.querySelector('[t-sel=requestButton]');
                       if (!b) throw new Error('no requestButton');
                       b.scrollIntoView(); b.click(); }"""
        )
    except Exception as e:
        emit({"step": "rfq", "ok": False, "error": str(e)[:200]})
        return False

    # Confirm dialog: "Request Quote for N Part(s)".
    deadline = time.time() + timeout
    confirmed = False
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            hit = page.evaluate(
                """() => { const b=document.querySelector(
                             '[t-sel=requestAnalysisDialog-actionBtn]');
                           if (!b || b.disabled) return false; b.click(); return true; }"""
            )
        except Exception:
            hit = False
        if hit:
            confirmed = True
            break

    if not confirmed:
        emit({"step": "rfq", "ok": False,
              "hint": "confirm dialog never appeared or stayed disabled"})
        return False

    # Wait for the acknowledgement, then dismiss it.
    deadline = time.time() + timeout
    ack = None
    while time.time() < deadline:
        time.sleep(1)
        body = page.evaluate("() => document.body.innerText")
        if "Thank You" in body or "received your" in body:
            ack = next((ns(l) for l in body.splitlines() if "received your" in l), None)
            break
    page.evaluate(
        """() => { const b=[...document.querySelectorAll('button')]
                     .find(e => (e.innerText||'').trim()==='Got It');
                   if (b) b.click(); }"""
    )
    emit({"step": "rfq", "ok": bool(ack), "submitted": True,
          "acknowledgement": ack or "submitted; no confirmation text seen",
          "note": "Protolabs will email a quote and manufacturing analysis."})
    return bool(ack)


def _return_to_quote(b, quote_url, timeout=120):
    """Leave the DFM analysis app and land back on the quote. Verified.

    The old code clicked "Return to Quote" and then waited for the URL to stop
    containing dfm-ui, so a click that never landed was indistinguishable from
    a slow one: it burned the whole timeout and left the caller stranded on the
    analysis page, where delivery options read empty and every price field is
    null. Worse, the caller then read that as "Protolabs won't price this" and
    fell through to the manual RFQ.

    So: try the shadow-DOM button, then the visible text, then navigate straight
    back to the quote URL captured on the way in — and return whether we are
    actually off dfm-ui, not whether a click was dispatched.
    """
    page = b.page
    for via in ("to-quote", "text", "goto"):
        try:
            if via == "to-quote":
                if not click_pl(page, "to-quote"):
                    continue
            elif via == "text":
                page.get_by_text("Return to Quote", exact=False).first.click(
                    timeout=2000)
            else:
                if not quote_url or "dfm-ui" in quote_url:
                    continue
                page.goto(quote_url)
        except Exception:
            continue
        # A dispatched button that has not navigated within ten seconds is not
        # worth another minute; move promptly to the next fallback.
        deadline = time.time() + min(timeout, 10)
        while time.time() < deadline:
            if "dfm-ui" not in page.url:
                settle(b)
                # The quote page is usable when its content renders; don't
                # sleep a fixed three seconds on top of the navigation.
                wait_js(b, "() => { const t = document.body ? document.body.innerText : '';"
                           " return t.includes('Order Summary') || t.includes('Receive by')"
                           " || t.includes('Quote '); }", 8, 0.25)
                emit({"step": "return_to_quote", "ok": True, "via": via,
                      "url": page.url})
                return True
            time.sleep(0.4)
    emit({"step": "return_to_quote", "ok": False, "url": page.url})
    return False


def view_analysis(b, timeout=300):
    """Open the DFM analysis, read advisories, approve, and return to the quote.

    This is NOT cosmetic: until the advisories are acknowledged the quote reads
    "Your part(s) need your attention" and is not orderable. Clicking through
    flips it to "Ready to Order!".

    It records a named approval ("Approved By: <account holder>"). That is the
    point of the step. Standing instruction from the account holder: when the
    quote says the part needs attention, click through and approve — do not stop
    to ask. See analysis_needed().
    """
    page = b.page
    # /analysis is also the recovery route after an interrupted /finish.  If
    # the browser is already in the DFM app, searching for "View Analysis" can
    # never succeed and used to burn the full five-minute timeout.
    on_dfm = "dfm-ui" in page.url
    quote_url = None if on_dfm else page.url
    if not on_dfm:
        # Clicking View Analysis makes Protolabs create a server-side DFM
        # session before the browser navigates, and that has been MEASURED at
        # over 30 seconds. So: dispatch the click, then wait for the dfm-ui
        # URL with a generous budget, re-dispatching every 10s in case the
        # first click never took (idempotent — once navigation starts the
        # search finds nothing). Only a click that never dispatches at all
        # fails fast.
        _CLICK_ANALYSIS_JS = """() => {
              const wanted = /(view|review|manufacturing).*analysis|analysis.*(view|review)/i;
              const nodes = [...document.querySelectorAll(
                'button, a, [role=button], pl-button')];
              for (const host of nodes) {
                const target = host.shadowRoot?.querySelector('button') || host;
                const label = (target.innerText || host.innerText || '')
                  .replace(/\\s+/g, ' ').trim();
                const visible = !!(target.offsetWidth || target.offsetHeight ||
                                  target.getClientRects().length);
                if (visible && !target.disabled && wanted.test(label)) {
                  target.scrollIntoView({block: 'center'});
                  target.click();
                  return true;
                }
              }
              return false;
            }"""
        clicked = False
        last_error = None
        deadline = time.time() + min(timeout, 120)
        fail_fast = time.time() + 12          # only if the click NEVER lands
        next_click = 0.0
        while time.time() < deadline and "dfm-ui" not in page.url:
            if time.time() >= next_click:
                try:
                    if page.evaluate(_CLICK_ANALYSIS_JS):
                        clicked = True
                except Exception as e:
                    # A click that starts navigation commonly destroys the JS
                    # execution context. That is progress, not failure.
                    last_error = e
                    if "Execution context was destroyed" in str(e):
                        clicked = True
                next_click = time.time() + 10
            if not clicked and time.time() >= fail_fast:
                out = {"ok": False, "returned_to_quote": True, "url": page.url,
                       "hint": "View Analysis not clickable: %s" %
                               str(last_error or "no enabled analysis action")[:160]}
                emit({"step": "analysis", **out})
                return out
            pause(0.3)
        if "dfm-ui" not in page.url:
            out = {"ok": False, "returned_to_quote": True, "url": page.url,
                   "hint": "analysis click dispatched but never navigated "
                           "within %ss" % int(min(timeout, 120))}
            emit({"step": "analysis", **out})
            return out
        # Wait for the advisory UI itself, not just the URL: the route changes
        # before the bundle renders, and the advisory walk needs buttons.
        wait_js(b, "() => { const t = document.body ? document.body.innerText : '';"
                   " return t.includes('Done') || t.includes('Approved By')"
                   " || t.includes('Return to Quote'); }", 20, 0.3)

    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:
        body = ""
    advisories = [ns(l) for l in body.splitlines()
                  if l.strip() and ("unavailable" in l.lower() or "issue" in l.lower())]

    # Follow one visible enabled action at a time and immediately re-read the
    # resulting page. Navigation races are expected. Three idle paints end the
    # loop in seconds rather than waiting out the route timeout.
    actions = []
    already_approved = "Approved By" in body
    approved = already_approved
    idle = 0
    for _ in range(20 if not already_approved else 0):
        step = advisory_step(b)
        if step.get("clicked"):
            actions.append(step["clicked"])
            idle = 0
        else:
            idle += 1
        if step.get("approved"):
            approved = True
            break
        # Done may return straight to the quote without ever exposing an
        # "Approved By" paint in the DFM app. If the quote no longer asks for
        # analysis, the acknowledgement succeeded.
        if not step.get("on_dfm"):
            approved = not step.get("needs_analysis", True)
            break
        if idle >= 3:
            break
        pause(0.5)

    returned = _return_to_quote(b, quote_url, timeout)
    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:
        body = ""
    if returned and not analysis_needed(b):
        approved = True

    out = {"ok": bool(approved and returned), "approved": approved,
           "returned_to_quote": returned, "url": page.url,
           "actions_clicked": actions,
           "advisories": advisories[:8],
           "approved_line": next((ns(l) for l in body.splitlines()
                                  if "Approved By" in l), None)}
    emit({"step": "analysis", **out})
    return out


_NEEDS_ANALYSIS = re.compile(
    r"need(s)? your attention|please view the analysis|review and approve"
    r"|view the analysis|advisor(y|ies) require", re.I)


_ADVISORY_JS = """() => {
  const priority = ['Done', 'Accept', 'Approve', 'Next', 'Continue',
                    'View Details', 'View Analysis', 'Return to Quote'];
  const hosts = [...document.querySelectorAll('button, a, [role=button], pl-button')];
  const seen = [];
  for (const host of hosts) {
    const el = host.shadowRoot?.querySelector('button') || host;
    const label = (el.innerText || host.innerText || '').replace(/\\s+/g,' ').trim();
    if (!label) continue;
    const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    if (visible && !el.disabled) seen.push({el, label});
  }
  // Prefix match, not equality: Protolabs suffixes the quote number onto some
  // labels ("Return to Quote 7385-266"), and an exact match silently skips them.
  for (const wanted of priority) {
    const w = wanted.toLowerCase();
    const hit = seen.find(x => x.label.toLowerCase().startsWith(w));
    if (hit) { hit.el.scrollIntoView({block:'center'}); hit.el.click(); return {clicked: hit.label}; }
  }
  return {clicked: null, visible: seen.map(x => x.label).slice(0, 25)};
}"""


def advisory_step(b):
    """Click exactly ONE enabled action on the analysis flow, then return.

    The whole advisory walk used to happen inside one call behind three 300s
    poll loops, so a stalled click looked identical to a slow page and produced
    no output for minutes. This does one thing and hands the page straight back:
    the caller reads `clicked`, `approved` and `on_dfm`, decides, and calls
    again. Action and reasoning alternate; nothing waits.

    When nothing matched, `visible` lists the enabled labels actually on the
    page — which is the information needed to choose the next move.
    """
    page = b.page
    try:
        res = page.evaluate(_ADVISORY_JS) or {}
    except Exception as e:
        res = {"clicked": None, "error": str(e)[:160]}
    time.sleep(0.4)                     # one paint, not a poll
    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:
        body = ""
    out = {"clicked": res.get("clicked"), "visible": res.get("visible"),
           "error": res.get("error"), "url": page.url,
           "on_dfm": "dfm-ui" in page.url,
           "approved": "Approved By" in body or "Ready for Manufacturing" in body,
           "ready_to_order": "Ready to Order" in body,
           "needs_analysis": bool(_NEEDS_ANALYSIS.search(body))
                             and "Approved By" not in body}
    emit({"step": "advisory", **{k: out[k] for k in
                                 ("clicked", "on_dfm", "approved")}})
    return out


def analysis_needed(b):
    """Does the quote still want the DFM advisories acknowledged?

    Protolabs words this several ways ("Your part(s) need your attention.",
    "Please view the analysis"), so match loosely rather than on one string. A
    quote already carrying "Approved By" or reading "Ready to Order!" is done.
    """
    try:
        body = b.page.evaluate("() => document.body.innerText")
    except Exception:
        return False
    if "Approved By" in body or "Ready to Order" in body:
        return False
    return bool(_NEEDS_ANALYSIS.search(body))


def finish(b, lead="Standard", out_dir=None, timeout=300):
    """Take a priced quote the rest of the way, unattended.

    Approves the advisories if the quote is asking for it, selects the lead
    time, reads the price, and downloads the PDF. Never clicks Checkout.

    This exists so the tail of a quote needs no decisions: everything here is
    either already specified by the caller or dictated by the page.
    """
    steps = {}
    if analysis_needed(b):
        steps["analysis"] = view_analysis(b, timeout=timeout)
        if "dfm-ui" in (b.page.url or ""):     # late navigation — walk it now
            steps["analysis"] = view_analysis(b, timeout=timeout)
        if not steps["analysis"].get("returned_to_quote", True):
            emit({"step": "finish", "ok": False, "error": "stranded_on_analysis",
                  "url": b.page.url})
            return {"ok": False, "error": "stranded_on_analysis",
                    "url": b.page.url, **steps}
    else:
        steps["analysis"] = {"skipped": True, "note": "quote was not asking for it"}

    steps["lead"] = {"ok": select_lead_time(b, lead),
                     "options": [o["label"] for o in delivery_options(b)]}
    result = read_price(b) or {}
    ok = bool(result.get("unit_price") and _MONEY.search(result["unit_price"] or ""))
    result["pdf"] = download_pdf(b, out_dir or FILES_DIR) if ok else None
    emit({"step": "finish", "ok": ok, "pdf": result["pdf"]})
    return {"ok": ok, **steps, **result}


def delivery_options(b):
    """The lead-time rows: [{label, selected}].

    Prices shown on the non-selected rows are DELTAS relative to the selected
    one ("+ $803.99"), not absolute prices.
    """
    try:
        return b.page.evaluate(
            """() => [...document.querySelectorAll('button.delivery-option')].map(e => ({
                   label: (e.innerText || '').replace(/\\s+/g, ' ').trim(),
                   selected: (e.className || '').includes('selected'),
               }))"""
        )
    except Exception:
        return []


def select_lead_time(b, kind="Standard", timeout=240):
    """Select a delivery option — Protolabs defaults to the cheapest/slowest.

    Without this the quote silently reports a Protolabs Network price (weeks
    out) while the caller believes it asked for standard lead time. Must be a
    real Playwright click; this is the same Vue SPA that ignores synthetic ones.
    """
    page = b.page
    # The delivery rows render asynchronously — notably they are re-created
    # after returning from the DFM app. Asking immediately found 0 rows and
    # reported the option missing on a page that was about to show it.
    wait_js(b, "() => document.querySelectorAll('button.delivery-option').length > 0",
            15, 0.3)
    # Navigation-safe: an in-flight page change (e.g. the DFM app landing
    # late) destroys the JS context mid-evaluate. Retry briefly instead of
    # letting the exception kill the whole quote run.
    idx = None
    for _ in range(3):
        try:
            idx = page.evaluate(
                """(k) => [...document.querySelectorAll('button.delivery-option')]
                       .findIndex(e => (e.innerText || '').trim().startsWith(k))""", kind)
            break
        except Exception:
            time.sleep(1)
    if idx is None or idx < 0:
        emit({"step": "lead_time", "ok": False, "wanted": kind,
              "available": [o["label"] for o in delivery_options(b)]})
        return False

    row = page.locator("button.delivery-option").nth(idx)
    if "selected" in (row.get_attribute("class") or ""):
        emit({"step": "lead_time", "ok": True, "wanted": kind, "note": "already selected"})
        return True

    row.click(timeout=UI_WAIT_MS)
    # timeout=0 (the default) means: click, glance once, return. The caller
    # re-checks via /status rather than us blocking for four minutes.
    deadline = time.time() + max(0, timeout)
    while True:
        time.sleep(0.4)
        opts = delivery_options(b)
        sel = next((o for o in opts if o["selected"]), None)
        if sel and sel["label"].startswith(kind):
            emit({"step": "lead_time", "ok": True, "selected": sel["label"]})
            return True
        if time.time() >= deadline:
            break
    emit({"step": "lead_time", "ok": False, "wanted": kind,
          "options": [o["label"] for o in delivery_options(b)]})
    return False


def download_pdf(b, out_dir, timeout=120):
    """Save the quote PDF from the Review page next to the part file.

    The browser context is created with accept_downloads=True, so Playwright's
    expect_download captures it without touching the OS save dialog.
    """
    page = b.page
    try:
        os.makedirs(out_dir, exist_ok=True)
        # Target the BUTTON by t-sel: the visible label is a <p> inside it, and
        # get_by_text resolves to that <p>, which is not clickable.
        with page.expect_download(timeout=min(timeout * 1000, UI_WAIT_MS)) as info:
            page.locator("[t-sel=downloadPdf-button]").first.click(timeout=UI_WAIT_MS)
        dl = info.value
        name = os.path.basename(dl.suggested_filename or "protolabs-quote.pdf")
        target = os.path.join(out_dir, name)
        dl.save_as(target)
        size = os.path.getsize(target) if os.path.isfile(target) else 0
        emit({"step": "pdf", "ok": size > 0, "path": target, "bytes": size})
        return target if size > 0 else None
    except Exception as e:
        # Not fatal: an unpriced quote has no PDF to give.
        emit({"step": "pdf", "ok": False, "error": str(e)[:200]})
        return None


def _parse_review(body):
    """Pull price / lead time out of the review page's flat text."""
    lines = [ns(l) for l in body.splitlines() if ns(l)]

    def after(label, n=1, pred=None, exact=False):
        for i, l in enumerate(lines):
            low, want = l.lower(), label.lower()
            if low == want or (not exact and low.startswith(want)):
                for cand in lines[i + 1:i + 1 + n + 3]:
                    if pred is None or pred(cand):
                        return cand
        return None

    is_money = lambda s: s.startswith("$")

    def after_qty_row():
        """The "<n> Part(s) @" row — the label carries the quantity, so a
        literal "1 Part @" match silently finds nothing at any other qty."""
        for i, l in enumerate(lines):
            if re.match(r"^\d+\s+Parts?\s+@", l):
                for cand in lines[i + 1:i + 5]:
                    if is_money(cand):
                        return cand
        return None

    return {
        "quote": next((l for l in lines if l.startswith("Quote ")), None),
        "status": next((l for l in lines if "Ready to Order" in l
                        or "need your attention" in l), None),
        "standard_receive_by": after("Standard", pred=lambda s: "," in s and "$" not in s),
        "order_by": after("Order by:", pred=lambda s: "$" not in s),
        "quantity": next((l.split()[0] for l in lines
                          if re.match(r"^\d+\s+Parts?\s+@", l)), None),
        "unit_price": after_qty_row() or after("Quantity", pred=is_money),
        "subtotal": after("Subtotal", pred=is_money),
        # Exact label: "Shipping Address" appears earlier on the page and a
        # prefix match returned the first money after IT — the subtotal.
        "shipping": after("Shipping", pred=is_money, exact=True),
        # Order-summary total is the LAST money figure on the page; taking the
        # first one after "Order Summary" yields the subtotal instead.
        "total": (lambda ms: ms[-1] if ms else None)(
            [l for l in lines if is_money(l)]),
    }


def read_price(b, timeout=300):
    """Advance to Review and return pricing, lead-time options and DFM notes.

    Review is a read-only step — it does NOT place an order. Checkout is
    deliberately never clicked by this script.
    """
    page = b.page
    if _wizard_step(page.url) != "review":
        # Only look for the button when NOT already on review — on the review
        # page it does not exist and the lookup used to burn its full timeout.
        try:
            page.get_by_text("Review Quote", exact=True).first.click(timeout=2000)
        except Exception:
            pass
    deadline = time.time() + timeout
    body = ""
    money_deadline = None
    while time.time() < deadline:
        if "dfm-ui" in (page.url or ""):
            # A late View Analysis navigation yanked the tab into the DFM app;
            # review text will never appear there. Bail immediately — the
            # caller recovers by walking the analysis — instead of polling out
            # the full timeout (this exact stall cost 300 s in testing).
            break
        try:
            body = page.evaluate("() => document.body.innerText")
        except Exception:
            body = ""                   # navigation mid-poll; look again
        if "Receive by" in body or "Order Summary" in body:
            # Review content is up — but every figure renders as the "$—"
            # placeholder while the engine reprices (e.g. right after the DFM
            # approval). Reading now yields a null quote for a part that
            # prices fine. Give real figures a bounded extra window.
            if priced_figures(body):
                break
            if money_deadline is None:
                money_deadline = time.time() + 75
            elif time.time() >= money_deadline:
                break
        time.sleep(0.5)

    out = {"url": page.url}
    out.update(_parse_review(body))
    # Report which delivery option the figures actually belong to. Without this
    # the price and the date can come from different rows.
    opts = delivery_options(b)
    out["delivery_options"] = [o["label"] for o in opts]
    out["selected_delivery"] = next(
        (o["label"] for o in opts if o["selected"]), None)
    emit({"step": "price", **out})
    return out


def ensure_step(path):
    """Accept .prt or .step. NX parts are converted via step.cmd first.

    Protolabs' dropzone does accept .prt, but ".prt" is ambiguous between NX and
    Creo and the STEP path is the one that is actually proven end to end here,
    so convert rather than gamble on their importer.
    """
    if path.lower().endswith((".step", ".stp")):
        return path
    if not path.lower().endswith(".prt"):
        raise SystemExit("expected a .prt or .step file, got: %s" % path)

    out = os.path.splitext(path)[0] + ".step"
    if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        emit({"step": "convert", "reused": out})
        return out

    import subprocess
    emit({"step": "convert", "from": path, "to": out, "note": "running NX headless"})
    r = subprocess.run([os.path.join(HERE, "step.cmd"), path, out, "242"],
                       capture_output=True, text=True, shell=False)
    if not os.path.isfile(out):
        raise SystemExit("STEP export failed:\n%s\n%s" % (r.stdout[-800:], r.stderr[-800:]))
    emit({"step": "convert", "ok": True, "bytes": os.path.getsize(out)})
    return out


def quote(b, path, material="Aluminum 6061-T651/T6", qty=1, itar=False,
          analysis=True, lead="Standard", service="CNC Machining"):
    """End-to-end: convert -> upload -> ITAR -> configure -> analysis -> price.

    `service` picks the manufacturing process card (see SERVICE_BTN: CNC
    Machining, 3D Printing, Injection Molding, Sheet Metal). The configure page
    differs per service; the material label must be one that service offers.

    Stops at the price. Checkout is never clicked.
    """
    path = ensure_step(path)
    part_dir = os.path.dirname(os.path.abspath(path))
    qid = start_quote(b, service=service, project=project_name_for(path))
    if not qid:
        return None
    if not upload_model(b, path, itar=itar):
        return None
    if not configure(b, material=material, qty=qty):
        return None

    route = proceed_from_configure(b)
    if not route:
        return None

    if route == "rfq":
        out = {"project_id": qid, "file": os.path.basename(path),
               "manual_rfq_submitted": True, "unit_price": None,
               "note": "No instant price available — request submitted; Protolabs "
                       "replies by email within a few hours."}
        emit({"step": "quote", "ok": True, **out})
        return out

    # Review route: approve the DFM advisories first — until that is done the
    # quote reads "needs your attention" and is not orderable — then read the
    # price it settles on.
    if analysis:
        an = view_analysis(b) or {}
        # Race guard: a View Analysis click that "did not navigate" can still
        # land after view_analysis returns. If the browser is now in the DFM
        # app, run the walk again — view_analysis handles already-on-dfm.
        if "dfm-ui" in (b.page.url or ""):
            an = view_analysis(b) or {}
        if not an.get("returned_to_quote", True):
            # Advisories may well be approved, but we are still on the DFM page.
            # Reading price/lead time from here yields nulls that look exactly
            # like an unpriceable part, so stop and say what actually happened.
            out = {"project_id": qid, "file": os.path.basename(path),
                   "ok": False, "error": "stranded_on_analysis",
                   "url": b.page.url, "unit_price": None,
                   "advisories": an.get("advisories"),
                   "approved_line": an.get("approved_line"),
                   "note": "Advisories handled but the browser never returned to the "
                           "quote. Navigate back and re-read /lead and /price; do "
                           "not treat this as an unpriceable part."}
            emit({"step": "quote", **out})
            return out
    # Protolabs preselects the cheapest/slowest delivery option, so the lead
    # time must be chosen explicitly before the price is read.
    select_lead_time(b, lead)
    result = read_price(b)

    if "dfm-ui" in (b.page.url or ""):
        # The View Analysis navigation landed mid-read. Walk the advisories
        # now, return to the quote, and redo lead time + price.
        view_analysis(b)
        select_lead_time(b, lead)
        result = read_price(b)

    ok = bool(result and result.get("unit_price")
              and _MONEY.search(result["unit_price"] or ""))

    if not ok and _wizard_step(b.page.url) != "review":
        # An empty price only means "unpriceable" when read from the review
        # page. Anywhere else it means we lost our place — submitting an RFQ
        # here would file a manual request for a part that prices fine.
        out = {"project_id": qid, "file": os.path.basename(path),
               "ok": False, "error": "unpriced_off_review", "url": b.page.url,
               "unit_price": None,
               "note": "No price, but not on the review page — refusing to submit a "
                       "manual RFQ from here."}
        emit({"step": "quote", **out})
        return out

    if not ok:
        # Review was reachable but Protolabs still would not price it. Fall back
        # to the manual request rather than reporting an empty quote.
        emit({"step": "quote", "note": "Review gave no price — falling back to the "
                                       "manual RFQ."})
        if request_for_quote(b):
            out = {"project_id": qid, "file": os.path.basename(path),
                   "manual_rfq_submitted": True, "unit_price": None,
                   "note": "No instant price — request submitted; Protolabs replies "
                           "by email within a few hours."}
            emit({"step": "quote", "ok": True, **out})
            return out

    result["pdf"] = download_pdf(b, part_dir) if ok else None
    result["project_id"] = qid
    emit({"step": "quote", "ok": ok, "file": os.path.basename(path),
          "project_id": qid, "pdf": result["pdf"],
          "itar_declared": "yes" if itar else "no"})
    return result


def probe(b, clicks=()):
    """Dump page structure: file inputs, buttons, headings.

    Optional `clicks` is a list of visible button/link texts to click in order
    before dumping (e.g. "New Quote"), so the quote flow can be walked without a
    human driving the UI.
    """
    time.sleep(3)
    b._settle()
    page = b.page
    for label in clicks:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=UI_WAIT_MS)
            time.sleep(4)
            b._settle()
        except Exception as e:
            emit({"step": "click", "label": label, "ok": False, "error": str(e)[:200]})
            break
    out = {"url": page.url, "title": page.title()}
    try:
        out["file_inputs"] = page.evaluate(
            """() => [...document.querySelectorAll('input[type=file]')].map(e => ({
                name: e.name, id: e.id, accept: e.accept,
                hidden: !(e.offsetWidth || e.offsetHeight),
                cls: (e.className || '').slice(0, 80)
            }))"""
        )
        out["buttons"] = page.evaluate(
            """() => [...document.querySelectorAll('button,a[role=button]')]
                .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 60)
                .slice(0, 40)"""
        )
        out["headings"] = page.evaluate(
            """() => [...document.querySelectorAll('h1,h2,h3')]
                .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 20)"""
        )
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        out["text_head"] = body[:1200]
    except Exception as e:
        out["error"] = str(e)[:300]
    emit(out)
    return out


def discover(b, path):
    """Sign in, then hand the browser over so you can drive one quote by hand.

    Run netspy.py against this session to capture the post-auth JSON traffic
    that the existing netlog_protolabs*.json files are missing entirely.
    """
    emit({
        "step": "discover",
        "note": "signed in — now drive one quote manually, end to end: upload %s, "
                "pick material, set qty, read the price. Every endpoint you touch "
                "gets recorded. Press Enter here when finished." % path,
    })
    try:
        input()
    except EOFError:
        pause(600)


# --------------------------------------------------------------------------

_LAST_EMIT = [time.time()]


def emit(obj):
    """Print a step record. `dt` is seconds since the previous record — the
    field to scan when a run feels slow: whichever step carries the big dt is
    the stall."""
    now = time.time()
    obj = {"dt": round(now - _LAST_EMIT[0], 1), **obj}
    _LAST_EMIT[0] = now
    print(json.dumps(obj, indent=2), flush=True)


# --------------------------------------------------------------------------
# daemon — one browser, held open, driven over HTTP
# --------------------------------------------------------------------------
#
# Protolabs' session dies with the browser, so every separate `python
# protolabs.py ...` invocation used to mean a fresh window and a fresh sign-in.
# This holds ONE signed-in browser open for the whole working session, the way
# siteapi.py does for McMaster. Always non-headless.
#
# Playwright's sync API is not thread-safe, so this is a single-threaded
# HTTPServer: handlers run on the same thread that created the browser.

DAEMON_PORT = int(os.environ.get("PROTO_PORT", "8766"))
# Bump when routes change, so a client can tell it is talking to a daemon that
# was started from older code and restart it rather than 404.
DAEMON_VERSION = 5
_B = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        b = _B
        one = lambda k, d="": q.get(k, [d])[0]
        try:
            if u.path == "/health":
                # Actually touch the page. Reporting ok purely because the
                # process is alive is how a daemon whose browser had been closed
                # kept answering "ok" and burned a full quote run before failing
                # with "Target page, context or browser has been closed".
                alive, url, why = False, None, None
                if b is not None:
                    try:
                        url = b.page.evaluate("() => location.href")
                        alive = True
                    except Exception as e:  # noqa: BLE001
                        why = str(e)[:200]
                return self._send({"ok": alive, "version": DAEMON_VERSION,
                                   "url": url, "dead_browser": why})
            if u.path == "/text":
                txt = b.page.evaluate("() => document.body.innerText")
                return self._send({"url": b.page.url, "text": txt[:5000]})
            if u.path == "/probe":
                clicks = [c for c in one("clicks").split("|") if c]
                return self._send(probe(b, clicks))
            if u.path == "/goto":
                goto(b, unquote(one("url")))
                settle(b)
                return self._send({"url": b.page.url})
            if u.path == "/modal":
                return self._send(read_modal(b))
            if u.path == "/click":
                b.page.get_by_text(
                    one("text"), exact=one("exact") == "1"
                ).first.click(timeout=UI_WAIT_MS)
                time.sleep(0.5)
                settle(b)
                return self._send({"clicked": one("text"), "url": b.page.url})
            if u.path == "/start":
                return self._send({"quote_id": start_quote(b)})
            if u.path == "/upload":
                # ITAR default is "no" per the account holder's standing
                # instruction; echoed in the response so it is never silent.
                itar = one("itar", "no").lower() in ("yes", "y", "1", "true")
                ok = upload_model(b, one("path"), itar=itar)
                return self._send({"ok": ok, "itar_declared": "yes" if itar else "no"})
            if u.path == "/quote":
                itar = one("itar", "no").lower() in ("yes", "y", "1", "true")
                return self._send(quote(b, one("path"),
                                        material=one("material", "Aluminum 6061-T651/T6"),
                                        qty=int(one("qty", "1")), itar=itar,
                                        lead=one("lead", "Standard"),
                                        service=one("service", "CNC Machining")))
            # ---- interactive configure: one decision per call ----------------
            # /session hands the page back at Configure; these drive it from
            # there without committing to the whole batch sequence.
            if u.path == "/material":
                name = one("name") or one("material") or "Aluminum 6061-T651/T6"
                return self._send({"ok": select_material(b, name),
                                   "material": name, **snapshot(b)})
            if u.path == "/qty":
                n = int(one("n") or one("qty") or "1")
                return self._send({"ok": set_quantity(b, n), "qty": n,
                                   **snapshot(b)})
            if u.path == "/reprice":
                # timeout defaults to 0 — look once and return. Call again if
                # `busy`; never sit in a poll loop on the caller's behalf.
                return self._send({**wait_priced(b, int(one("timeout", "0"))),
                                   **snapshot(b)})
            if u.path == "/advisory":
                # One click per call. Loop from the caller, reasoning between.
                return self._send(advisory_step(b))
            if u.path == "/status":
                # Cheap page read: where am I, is it busy, does it want the
                # analysis. No clicks, no waiting.
                try:
                    body = b.page.evaluate("() => document.body.innerText")
                except Exception:
                    body = ""
                return self._send({
                    "url": b.page.url, "step": _wizard_step(b.page.url),
                    "on_dfm": "dfm-ui" in b.page.url,
                    "busy": "Analyzing" in body,
                    "priced": priced(body),
                    "approved": "Approved By" in body,
                    "ready_to_order": "Ready to Order" in body,
                    "needs_analysis": analysis_needed(b),
                    "delivery": delivery_options(b)})
            if u.path == "/proceed":
                route = proceed_from_configure(b)
                return self._send({"ok": bool(route), "route": route,
                                   "needs_analysis": analysis_needed(b),
                                   **snapshot(b)})
            if u.path == "/finish":
                # Advisory approval, lead time, price and PDF in one call. The
                # account holder's standing instruction is to approve rather
                # than stop and ask, so this never prompts.
                return self._send(finish(b, lead=one("lead", "Standard"),
                                         out_dir=one("dir") or None))
            if u.path == "/price":
                return self._send({**read_price(b), "page": snapshot(b, 3000)})
            if u.path == "/analysis":
                return self._send(view_analysis(b))
            if u.path == "/lead":
                ok = select_lead_time(b, one("kind", "Standard"),
                                      timeout=int(one("timeout", "0")))
                return self._send({"ok": ok, "options": delivery_options(b)})
            if u.path == "/pdf":
                return self._send({"path": download_pdf(b, one("dir") or FILES_DIR)})
            if u.path == "/snapshot":
                return self._send(snapshot(b, int(one("limit", "6000"))))
            if u.path == "/session":
                # The mechanical opening: sign-in already happened at serve(),
                # so this starts a draft, uploads, answers ITAR, and hands the
                # page back at Configure — where the real decisions start.
                itar = one("itar", "no").lower() in ("yes", "y", "1", "true")
                path = ensure_step(one("path"))
                qid = start_quote(b, service=one("service", "CNC Machining"),
                                  project=project_name_for(path))
                ok = upload_model(b, path, itar=itar) if qid else False
                out = {"quote_id": qid, "ok": ok, "file": path,
                       "itar_declared": "yes" if itar else "no"}
                emit({"step": "session", **out})
                return self._send({**out, **snapshot(b)})
            if u.path == "/act":
                return self._send(act(b, one("kind"), one("target") or None,
                                      one("value") or None,
                                      exact=one("exact", "1") == "1"))
            if u.path == "/materials":
                return self._send(materials(b))
            if u.path == "/eval":
                return self._send({"result": b.page.evaluate(unquote(one("js")))})
            if u.path == "/stop":
                self._send({"stopping": True})
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0))).start()
                return
            return self._send({"error": "unknown route: %s" % u.path}, 404)
        except PermissionError as e:
            return self._send({"error": str(e), "blocked": True}, 403)
        except Exception as e:
            # Ship the page with the error. A bare message is what made the
            # earlier failures so opaque — the caller could not see that the
            # button it wanted was on a page it had already left.
            err = {"error": str(e)[:600]}
            try:
                err["page"] = snapshot(b, 3000)
            except Exception:  # noqa: BLE001
                pass
            return self._send(err, 500)


def serve():
    """Launch a visible browser, sign in, and hold it open for HTTP commands."""
    global _B
    kw = {"headless": False}  # never headless — standing instruction
    if PROFILE:
        kw["profile_dir"] = PROFILE
    _B = Browser(**kw)
    # A persistent Chrome profile can restore tabs from a previous/stale
    # daemon. Reuse one page and close every other restored tab before login;
    # otherwise each restart can leave extra about:blank tabs visible.
    pages = list(_B._ctx.pages)
    if pages:
        keep = next(
            (p for p in pages if "protolabs.com" in (p.url or "").lower()),
            pages[0],
        )
        _B.page = keep
        for page in pages:
            if page != keep:
                page.close()
    login(_B)
    emit({"step": "serve", "port": DAEMON_PORT,
          "note": "browser stays open until GET /stop"})
    HTTPServer(("127.0.0.1", DAEMON_PORT), Handler).serve_forever()


def _parse_quote_flags(args):
    """--material/--qty/--lead/--service/--itar plus bare [material] [qty]."""
    opts = {"material": "Aluminum 6061-T651/T6", "qty": "1",
            "lead": "Standard", "service": "CNC Machining", "itar": "no"}
    positional = []
    for a in args:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            if k not in opts:
                raise SystemExit("unknown flag --%s" % k)
            opts[k] = v
        else:
            positional.append(a)
    if positional:
        opts["material"] = positional[0]
    if len(positional) > 1:
        opts["qty"] = positional[1]
    return opts


def client(path, args):
    """Agent-agnostic entry point: ensure the daemon, run ONE quote, print JSON.

    This is what quote.cmd (on PATH) runs. Any model, agent or plain shell
    gets the whole capability from one command — no Playwright, no skill
    files, no MCP on the calling side. Works from any folder because every
    path involved is absolute.

    Auto-starts the daemon hidden+detached when it is not running, and
    restarts it when it predates DAEMON_VERSION, so a stale daemon can never
    silently serve old behavior.
    """
    import subprocess
    from urllib.request import urlopen
    from urllib.parse import urlencode, quote as urlq

    base = "http://127.0.0.1:%d" % DAEMON_PORT

    def get(route, timeout):
        with urlopen(base + route, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def health():
        try:
            return get("/health", 5)
        except Exception:
            return None

    h = health()
    if h and h.get("ok") and h.get("version", 0) < DAEMON_VERSION:
        emit({"step": "client", "note": "daemon is version %s < %s — restarting it"
                                        % (h.get("version"), DAEMON_VERSION)})
        try:
            get("/stop", 5)
        except Exception:
            pass
        time.sleep(2)
        h = None
    if h and not h.get("ok"):
        # The daemon process is alive but its browser is gone (a human closed
        # the Chromium window, or Chrome crashed). It MUST be stopped before a
        # replacement starts: it still holds port %d, and on Windows a second
        # HTTPServer binds the same port via SO_REUSEADDR while the zombie
        # keeps receiving the requests — the exact failure this comment is
        # written on.
        emit({"step": "client", "note": "daemon answers but its browser is dead "
                                        "— stopping it before starting fresh",
              "detail": (h.get("dead_browser") or "")[:120]})
        try:
            get("/stop", 5)
        except Exception:
            pass
        time.sleep(2)
        h = None

    if not (h and h.get("ok")):
        log_dir = os.environ.get("TEMP") or HERE
        outlog = open(os.path.join(log_dir, "proto.out.log"), "ab")
        errlog = open(os.path.join(log_dir, "proto.err.log"), "ab")
        # DETACHED + no window: the daemon must outlive this client (an
        # interrupted agent must not kill the browser mid-quote) and must not
        # flash a blank console. The Chromium window stays visible — serve()
        # hardcodes headless False.
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"],
                         cwd=HERE, stdout=outlog, stderr=errlog,
                         creationflags=flags, close_fds=True)
        emit({"step": "client", "note": "daemon starting; sign-in usually ~15s"})
        deadline = time.time() + 240
        while time.time() < deadline:
            h = health()
            if h and h.get("ok"):
                break
            time.sleep(2)
        else:
            raise SystemExit("daemon never became healthy — see %s"
                             % os.path.join(log_dir, "proto.err.log"))

    opts = _parse_quote_flags(args)
    q = urlencode({"path": os.path.abspath(path), "material": opts["material"],
                   "qty": opts["qty"], "lead": opts["lead"],
                   "service": opts["service"], "itar": opts["itar"]},
                  quote_via=urlq)
    # Generous ceiling: .prt conversion (~1-2 min cold) + quote (~75s typical).
    result = get("/quote?" + q, 900)
    if not isinstance(result, dict):
        # quote() returns null when a step fails before producing a result.
        result = {"ok": False,
                  "error": "quote flow failed before pricing — the failing "
                           "step is the last record in %TEMP%\\proto.out.log"}
    print(json.dumps(result, indent=2))
    return 0 if (result.get("unit_price") or result.get("manual_rfq_submitted")) else 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "login"

    if mode == "serve":
        return serve()

    if mode == "client":
        if len(sys.argv) < 3:
            raise SystemExit(
                "usage: protolabs.py client <file.prt|.step> [--material=..] "
                "[--qty=N] [--lead=Standard] [--service='CNC Machining'] [--itar=no]")
        if not os.path.isfile(sys.argv[2]):
            raise SystemExit("no such file: %s" % sys.argv[2])
        return sys.exit(client(sys.argv[2], sys.argv[3:]))

    # Validate the file before launching anything: start_quote() creates a real
    # draft in the account, so a bad path must not get that far.
    if mode in ("upload", "quote"):
        if len(sys.argv) < 3:
            raise SystemExit("usage: protolabs.py %s <file.step> [material] [qty]" % mode)
        if not os.path.isfile(sys.argv[2]):
            raise SystemExit("no such file: %s" % sys.argv[2])

    kw = {"headless": HEADLESS}
    if PROFILE:
        kw["profile_dir"] = PROFILE
    b = Browser(**kw)
    try:
        if not login(b):
            return
        if mode == "login":
            return
        if mode == "probe":
            probe(b, clicks=sys.argv[2:])
        elif mode == "upload":
            itar = None
            for a in sys.argv[3:]:
                if a.startswith("--itar="):
                    itar = a.split("=", 1)[1].strip().lower() in ("yes", "y", "true", "1")
            start_quote(b)
            if upload_model(b, sys.argv[2], itar=itar):
                probe(b)
        elif mode == "discover":
            discover(b, sys.argv[2] if len(sys.argv) > 2 else "")
        elif mode == "quote":
            if len(sys.argv) < 3:
                raise SystemExit(
                    "usage: protolabs.py quote <file.prt|.step> [--material=..] "
                    "[--qty=N] [--lead=Standard] [--service='CNC Machining'] "
                    "[--itar=no]")
            opts = _parse_quote_flags(sys.argv[3:])
            quote(b, sys.argv[2], material=opts["material"],
                  qty=int(opts["qty"]), lead=opts["lead"],
                  service=opts["service"],
                  itar=opts["itar"].strip().lower() in ("yes", "y", "true", "1"))
        else:
            raise SystemExit("unknown mode: %s" % mode)
    finally:
        b.close()


if __name__ == "__main__":
    main()
