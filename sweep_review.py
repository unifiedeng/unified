r"""
Re-price the open Protolabs quotes from the REVIEW page with lead=Standard.

The Configure page shows a price from whichever fulfillment channel the
quote engine happened to route the part to — Protolabs Factory (tile says
"Machining Tolerance: +/-0.005 in") or the cheaper partner Network (tile
says "Network Tolerance: ISO 2768-m") — so configure-page numbers mix two
incompatible price scales. The review page with "Standard" delivery is
always the Factory price, so that is what we record.

Per part x qty: goto configure -> set qty -> proceed -> (analysis once)
-> lead=Standard -> read price. Results go to review_results.json.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8766"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "testparts", "quotes", "review_results.json")

QUOTES = {  # part -> open quote configure URL. Fill from the daemon log
    # ($TEMP/proto.out.log "upload" records) after sweep_quotes.py has run:
    # "t1_block": "https://buildit.protolabs.com/quotes/<quote-id>/configure",
}
QTYS = [1, 5, 10, 25]
MONEY = re.compile(r"\$\s?(\d[\d,]*\.\d{2})")


def get(path, timeout=600):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def money(v):
    if not v:
        return None
    m = MONEY.search(str(v))
    return float(m.group(1).replace(",", "")) if m else None


def wait_qty_applied(q, max_s=90):
    t0 = time.time()
    while time.time() - t0 < max_s:
        snap = get("/snapshot", timeout=120)
        t = snap.get("text", "")
        m = re.search(r"Quantity (\d+)", t)
        if m and int(m.group(1)) == q and not snap.get("busy"):
            return True
        time.sleep(3)
    return False


def price_at(part, url, q, res, entry):
    get("/goto?url=%s" % urllib.parse.quote(url, safe=""), timeout=300)
    try:
        get("/click?text=Select%20All", timeout=120)   # bind the left panel
    except Exception:
        pass                                           # already selected
    r = get("/qty?n=%d" % q, timeout=300)
    if not r.get("ok"):
        log("%s qty %d: not applied" % (part, q))
        entry["prices"][str(q)] = {"error": "qty_not_applied"}
        return
    wait_qty_applied(q)
    p = get("/proceed", timeout=300)
    if p.get("needs_analysis") and not entry.get("analysis_done"):
        log("%s: running analysis approval" % part)
        a = get("/analysis", timeout=600)
        entry["analysis_done"] = bool(a.get("returned_to_quote"))
    ok = get("/lead?kind=Standard&timeout=60", timeout=300)
    pr = get("/price", timeout=600)
    unit = money(pr.get("unit_price"))
    entry["prices"][str(q)] = {
        "unit": unit,
        "subtotal": money(pr.get("subtotal")),
        "lead_ok": ok.get("ok"),
        "delivery": pr.get("selected_delivery"),
        "receive_by": pr.get("standard_receive_by"),
    }
    log("%s qty %d -> $%s (lead_ok=%s, %s)"
        % (part, q, unit, ok.get("ok"), pr.get("selected_delivery")))


def main():
    parts = sys.argv[1].split(",") if len(sys.argv) > 1 else list(QUOTES)
    res = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            res = json.load(f)
    for part in parts:
        entry = res.setdefault(part, {"prices": {}})
        for q in QTYS:
            if str(q) in entry["prices"] and entry["prices"][str(q)].get("unit"):
                continue
            try:
                price_at(part, QUOTES[part], q, res, entry)
            except Exception as e:  # noqa: BLE001
                log("ERROR %s qty %d: %s" % (part, q, e))
            with open(OUT, "w") as f:
                json.dump(res, f, indent=2)
    log("review sweep done")


if __name__ == "__main__":
    main()
