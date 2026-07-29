# Site-specific notes (loaded into the agent's system prompt — edit freely)

## McMaster-Carr (mcmaster.com)
- Search-driven site: go straight to the search box on the home page and search for
  the part (e.g. "M4 socket head cap screw"), don't browse category links.
- It's a single-page app: after searching or clicking, always read_page again.
- Catalog/results pages have hundreds of links — use read_page with a `filter`
  (e.g. filter "socket head") instead of reading everything.
- Product tables: each row's part number is a link to the detail page. Prices are
  shown per package in the table.
- To get a CAD model or spec sheet, open the specific part's detail page.
- McMaster often shows a "To continue browsing, please log in" wall to automated
  browsers. Use get_login("mcmaster.com") and log in — the session then persists.
- Downloading CAD (STEP/etc.) — the product-detail pane is rendered in SHADOW DOM,
  so read_page (light-DOM/innerText) shows only nav+footer and the login wall text
  is also invisible to innerText. Use Playwright locators (they pierce open shadow
  roots). The standalone `mcmaster.py` script does this end to end:
    * login: fill `input[type=password]` + preceding `input[type=text]`, submit;
      detect logged-out via `get_by_text("Log in to view")` (shadow-piercing).
    * cut-to-length rails (e.g. 47065T101) disable the CAD button until a Length is
      picked in the `.SpecSrch_Attribute` (has_text "Length") table — click a value
      cell first; picking 1 ft turned SKU 47065T101 into 47065T411.
    * CAD control: `button[aria-label="Select CAD file type"]` (aria-disabled until a
      concrete SKU); click it, then click role=option "3-D STEP" (opts: 3-D
      Solidworks/STEP/Parasolid/SAT/IGES/PDF, 2-D PDF/DWG/DXF/Solidworks/EDRW).
    * the option click does NOT download; a "Download" control appears — wrap the
      Download click in `page.expect_download()`. Files are AP203 (SolidWorks-authored).
  Verified parts: 47065T411 (1"x1" T-slot rail, 1 ft) + 47065T236 (1" corner bracket).
- McMaster rate-limits aggressive browsing. Keep full-page navigations to a minimum:
  use the search box and in-page clicks rather than loading many category URLs.
  If "Access has been restricted" appears, STOP immediately and tell the user to
  wait 15-30 minutes — retrying extends the block.
- **Flagged-profile symptom (seen 2026-07-28):** if ALL `/mv*/` XHRs 403 with an
  Akamai comment blob — including the site's OWN calls (TokenAuthorization,
  Suggest, even the masthead phone widget) while full-page navs still render —
  the profile's `_abck` sensor cookie is poisoned. Waiting does NOT fix it (30+
  min made no difference). Fix: a FRESH Chromium profile gets a clean sensor and
  works immediately, even anonymously. A known-good logged-in copy lives at
  `browser-agent\profile-mcm-fresh` (siteapi still points at `profile\`; swap if
  the old one is still dead).
- **No CAD for containers:** tote boxes / tubs / bins have no CAD models at all
  (verified logged-in across 6 families incl. 5124T35, 4315T4, 4867T4, 40365T8,
  4659T8, 4387T51; a screw like 91290A115 shows the full CAD menu in the same
  session). Don't hunt for the CAD button on these — it isn't login-gated, it
  just doesn't exist.
- siteapi.py now auto-logs-in during McMaster warmup (`_mcm_login_if_walled`,
  mirrors protolabs: creds from logins.json, human-paced fills).
- **Filter facets are virtualized — reading page text truncates them silently.**
  Only ~18 options sit in the DOM at a time; the rest load as the facet's own
  container scrolls. There is no ellipsis and no "show more", so the short list
  looks complete: standard-washers Material appeared to end at "Rubber", hiding
  Steel, Stainless Steel, Titanium, Wool Felt (18 shown, 22 real); rods showed
  18 of 43. Scroll the facet container until the option set stops growing —
  `supplier.py --facet Material` does this. Smell test: an alphabetical facet
  that ends before "S" is truncated, not short.
- **Category pages differ in kind.** Raw Materials categories list *materials*
  as tiles (rods: Steel 3,426, Aluminum 950, ... 30 tiles). Fastener categories
  list *product types* instead (standard-washers: O-Rings, Lock Washers, Shims,
  58 tiles) and only expose materials through the Material facet. Answer
  "what's it sold in" from tiles for raw stock, from the facet for fasteners.
- **`/products/washers/` is cleaning equipment** (parts washers, pressure
  washers, brushes). Fastener washers are `/products/standard-washers/`.
- **What actually trips the blocks is request cadence, not fingerprint.** A
  scripted navigation with zero mouse movement reads a category page fine; a
  burst of clicks through several categories gets walled regardless of browser.
  Three browsers (Playwright Chromium, the in-app pane, real Chrome) all
  behaved identically on this. Keep to a few page loads per question.
- **Cold profiles get the login wall on deep links.** A brand-new profile
  navigating straight to a category is walled even headed and even after a
  home-page warmup hop; a profile with a little history is not. For McMaster,
  prefer `mcm`/siteapi (warm, logged-in daemon) over `browse`.

## Protolabs (protolabs.com)
- The marketing site (www.protolabs.com) is mostly informational. Clicking "Get
  Instant Quote" jumps to the quote portal at **buildit.protolabs.com**.
- Quoting requires login. The portal session sometimes expires between runs, so you
  may be redirected to identity.protolabs.com/signin. Call get_login("protolabs.com"),
  then read_page (unfiltered — the email/password inputs have no visible label text,
  so a filtered read shows 0 elements). The form is just: <input type=text> (email),
  <input type=password>, <button type=submit> "Sign In". Fill and submit.
- Reusing an existing quote is fine and faster: the portal home lists "Recent
  Activity"; open the project → the quote → it lands on the Review page. If the CAD
  file is already uploaded (e.g. a prior "Model2 Quote"), you can skip re-uploading.
- To upload fresh: from the portal home click "Get a new quote". This opens the
  "Start a new quote" panel IN PLACE (the URL stays buildit.protolabs.com/ — it's an
  expanding section, not a new page; scroll down to see it). Do it in this order, then
  move on — don't loiter here:
  1. Project: either click "+ Create a New Project" and type a name into the
     "Name your project" box (type_text works even though the field reports as not
     visible), OR just click an existing project (e.g. "Model2 Quote") to skip naming.
  2. Click the service: "CNC Machining".
  3. Click "Continue to CAD Upload".
  4. On the upload step, use upload_file with the <input type=file> (read_page lists it
     even if hidden) to attach the CAD file (.step/.prt/.stl). Wait for it to process,
     then it lands on Configure.
- CONFIGURE PAGE — the flow that used to get stuck:
  1. First SELECT THE PART: click the part card (or "Select All"). Only then does the
     Configure panel (Quantity / Material / Finish / Threading) appear. Before this the
     part shows "Not Orderable" and there is no Material field — that's expected.
  2. Click the Material field ("Make a selection"). It's a custom "rich-select" combobox,
     NOT a <select> — select_option will fail with "not a <select> element". After
     clicking it, read_page with a filter like "Aluminum" or "6061"; the options now
     list as clickable elements (e.g. "Aluminum 6061-T651/T6") — click the index. If for
     some reason an option has no index, use **click_text "Aluminum 6061-T651/T6"**.
  3. Set Quantity if needed (default 1). Finish/Threading can stay default.
- Then click "Review". The quote takes a few seconds to compute; read_page again. A
  DFM note like "Sharp internal corners with minimum tool radius" is only a warning,
  not a blocker — the part still prices.
- The per-part price appears directly on the Configure and Review pages (e.g.
  "Per Part $283.88" / "Network Part @ $283.88"). Once you can read it, that IS the
  answer — report it and stop. Do NOT keep clicking around to re-confirm it.
- While the quote computes, the Configure page may show only a bare "$" placeholder for
  a few seconds. Don't sit on Configure re-reading it — click "Review", which is where
  the resolved price AND the lead-time (delivery-date) options appear together.
- "View Analysis" (the DFM/Design Review page) is OPTIONAL — it only lists advisories
  and lets you download the quote PDF. It is NOT required to get the price, so skip it
  unless the user asks for the analysis or a downloadable PDF quote. If you do open it,
  finish it (click "Done" / "Return to Quote") rather than leaving it half-done.
- LEAD TIME on the Review page is shown as **delivery-date options with upcharges**,
  e.g. "Fri Jul 24 +$72.46", "Wed Jul 29 +$44.43", "Tue Aug 11" (standard, included).
  There are no fixed "1 week / 2 week" labels — map the requested lead time to the
  nearest delivery date (check today's date first if unsure) and pick that row. The
  Order Summary then shows Subtotal / Shipping / Total. Report the part price, the
  chosen delivery date, and the total.
- Note: parts made via "Protolabs Network" (a marketplace) tend to have longer minimum
  lead times than Protolabs' in-house CNC. If no option is as fast as requested, report
  the fastest available rather than claiming the requested one exists.
- NX-native .prt uploads FAIL to process ("Thumbnail Error" + warning icon on the file
  card, Continue stays at "0 files") — export to STEP first (step.cmd in C:\code\unified)
  and upload the .step.
- Right after upload an "ITAR or Export Restrictions" dialog blocks everything: pick
  No (radio) then Done. The upload does NOT auto-advance — wait for the button to read
  "Continue with 1 files" and click it to reach Configure.
- The Review button in the wizard is the element [t-sel="reviewLink"]; it stays
  disabled until a material is chosen.
- Changing Quantity on Review shows "N Parts @ $" with the price blank for a long time
  (Network-priced parts may take minutes to recompute) — don't wait on it for a quick
  per-part answer; quote qty 1 and report that.
- Cookie banners: OneTrust (marketing site) and CookieYes (.cky-btn-accept, the portal)
  are auto-dismissed by the harness; if some other overlay blocks clicks, press Escape
  or find its close/X button.
