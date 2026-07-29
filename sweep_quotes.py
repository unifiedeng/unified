r"""
Sweep Protolabs quotes across test parts x materials x quantities.

One upload per part+material via the protolabs.py daemon, then walk the
quantity list on the Configure page. After each /qty the page keeps showing
the STALE price while the quote engine reprices async, so a read is only
trusted once the part tile confirms "Quantity N" and two consecutive polls
agree on the Per Part figure.

Steels (316 et al) do not instant-price on Configure — they go through the
RFQ route — so by default this sweeps aluminum only. Pass material keys as
argv[2] (comma-separated) to override.

Usage: python sweep_quotes.py [parts_csv] [materials_csv]
Results append incrementally to testparts/quotes/sweep_results.json.
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
OUT = os.path.join(HERE, "testparts", "quotes", "sweep_results.json")

PARTS = ["t1_block", "t2_pocket", "t3_holes",
         "t4_complex", "t5_sideholes", "t6_bigblock"]
MATERIALS = {"al6061": "Aluminum 6061-T651/T6",
             "ss316": "Stainless Steel 316/316L"}
DEFAULT_MATS = ["al6061"]
QTYS = [1, 5, 10, 25]

PER_PART = re.compile(r"Per Part\s*\n\$\s?(\d[\d,]*\.\d{2})")
TOTAL = re.compile(r"Parts Total\s*\n\$\s?(\d[\d,]*\.\d{2})")


def get(path, timeout=600):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def read_tile(snap):
    t = snap.get("text", "")
    per = PER_PART.search(t)
    tot = TOTAL.search(t)
    qty = re.search(r"Quantity (\d+)", t)
    return {
        "per_part": float(per.group(1).replace(",", "")) if per else None,
        "parts_total": float(tot.group(1).replace(",", "")) if tot else None,
        "tile_qty": int(qty.group(1)) if qty else None,
        "busy": snap.get("busy"),
        "rfq": "Request for Quote" in t and per is None,
    }


def settled_price(want_qty, max_s=150):
    """Poll until the tile shows want_qty and two consecutive reads agree."""
    t0, last = time.time(), None
    while time.time() - t0 < max_s:
        time.sleep(4)
        tile = read_tile(get("/snapshot", timeout=120))
        if tile["busy"] or tile["tile_qty"] != want_qty:
            last = None
            continue
        if tile["per_part"] is None:
            if tile["rfq"]:
                return tile
            continue
        if last is not None and last == tile["per_part"]:
            return tile
        last = tile["per_part"]
    return tile


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_results():
    if os.path.exists(OUT):
        with open(OUT) as f:
            return json.load(f)
    return {}


def save_results(res):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(res, f, indent=2)


def sweep_part(part, mat_key, res):
    step = os.path.join(HERE, "testparts", part + ".step")
    key = "%s|%s" % (part, mat_key)
    entry = res.get(key, {})
    have = entry.get("prices", {})
    if all(str(q) in have and "unit" in have[str(q)] for q in QTYS):
        log("skip %s (complete)" % key)
        return

    log("session upload: %s" % part)
    s = get("/session?path=%s&itar=no"
            % urllib.parse.quote(step.replace("\\", "/")))
    if s.get("error"):
        log("session error: %s" % s["error"])
        return
    m = get("/material?name=%s"
            % urllib.parse.quote(MATERIALS[mat_key]), timeout=300)
    if not m.get("ok"):
        log("MATERIAL FAILED for %s" % key)
        res.setdefault(key, {})["material_error"] = True
        save_results(res)
        return

    entry = res.setdefault(key, {"part": part, "material_key": mat_key,
                                 "material_label": MATERIALS[mat_key],
                                 "prices": {}})
    for q in QTYS:
        if str(q) in entry["prices"] and "unit" in entry["prices"][str(q)]:
            continue
        r = get("/qty?n=%d" % q, timeout=300)
        if not r.get("ok"):
            log("qty %d not applied for %s" % (q, key))
            entry["prices"][str(q)] = {"error": "qty_not_applied"}
            save_results(res)
            continue
        tile = settled_price(q)
        if tile.get("rfq"):
            entry["prices"][str(q)] = {"rfq_only": True}
            log("%s qty %d -> RFQ only" % (key, q))
        elif tile.get("per_part") is not None:
            entry["prices"][str(q)] = {"unit": tile["per_part"],
                                       "parts_total": tile["parts_total"]}
            log("%s qty %d -> $%.2f/part (total $%.2f)"
                % (key, q, tile["per_part"], tile["parts_total"] or -1))
        else:
            entry["prices"][str(q)] = {"error": "no_price",
                                       "tile": tile}
            log("%s qty %d -> NO PRICE (%s)" % (key, q, tile))
        save_results(res)


def main():
    parts = sys.argv[1].split(",") if len(sys.argv) > 1 else PARTS
    mats = sys.argv[2].split(",") if len(sys.argv) > 2 else DEFAULT_MATS
    res = load_results()
    # discard earlier stale-read data (identical prices across qtys)
    for k in list(res):
        prices = res[k].get("prices", {})
        units = [v.get("unit") for v in prices.values()
                 if isinstance(v, dict) and v.get("unit")]
        if len(units) >= 2 and len(set(units)) == 1:
            log("dropping suspect stale entry: %s" % k)
            del res[k]
    save_results(res)
    for part in parts:
        for mat_key in mats:
            try:
                sweep_part(part, mat_key, res)
            except Exception as e:  # noqa: BLE001
                log("ERROR %s|%s: %s" % (part, mat_key, e))
    log("sweep done")


if __name__ == "__main__":
    main()
