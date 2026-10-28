"""Builds panel_catalog.json (the extra subscriptions sold in the bot and the
Shop) from a scrape of the supplier panel's Products list, and writes the
matching block into index.html so both always carry the same ids and prices.

  python tools/panel_catalog.py build <scraped_products.json>   # -> panel_catalog.json
  python tools/panel_catalog.py html                            # panel_catalog.json -> index.html
  python tools/panel_catalog.py check                           # exit 1 if index.html is out of date

The scrape is a list of {id, name, months, price} (price = what the panel
charges). Our price is that plus MARKUP.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "panel_catalog.json"
INDEX_PATH = ROOT / "index.html"
MARKUP = 20.0
START, END = "/* PANEL-CATALOG:START */", "/* PANEL-CATALOG:END */"

# Products the bot already sells under another name (panel name -> bot item).
ALREADY_IN_BOT = {
    "Dynamed": "dynamed", "DynaMedEX": "dynamedex",
    "Amboss": "amboss_*", "Amboss 30/12/2024": "amboss_*",
    "Best practice BMJ": "bmj_best_practice", "Learning BMJ": "bmj_learning",
    "VisualDx": "visualdx",
    "Lexidrug (webproxy)": "lexicomp", "Lexidrug App DEC/2026": "lexicomp",
    "Lexidrug App MAY/2027": "lexicomp", "Lexidrug App SEP/2027": "lexicomp",
    "Access Medicine": "accessmedicine",
    "BoardVitals": "boardvitals", "BoardVitals VIP": "boardvitals",
    "ClinicalKey (Direct)": "clinicalkey", "ClinicalKey (WebProxy)": "clinicalkey",
    "ScienceDirect (Type 1)": "sciencedirect_noai", "ScienceDirect (Type 2)+AI": "sciencedirect_ai",
    "StatDX (WebProxy)": "statdx", "NEJM": "nejm", "JAMAevidence -Mhmedical": "jama_evidence",
    "Scopus": "scopus", "Embase": "embase",
    "Springer Journals": "springerlink_journals", "Springer Journals (WebProxy)": "springerlink_journals",
    "Merative Micromedex (IBM) - webproxy+app": "micromedex", "Merative Micromedex (IBM)-APPs": "micromedex",
    "Sanford Guide (APP)": "sanford_guide", "Sanford Guide (Webproxy)": "sanford_guide",
}
# Left out on purpose, with the reason.
SKIPPED = {
    "Open": "two products share this name and each duration appears twice — unclear what it is",
}


def duration_label(months: int) -> str:
    if months == 12:
        return "1 Year"
    return f"{months} Month" + ("" if months == 1 else "s")


def build(scrape_path: str):
    rows = json.loads(Path(scrape_path).read_text())
    groups = defaultdict(list)
    for r in rows:
        groups[r["name"].strip()].append(r)
    products, uptodate, already, skipped = [], [], {}, {}
    for name in sorted(groups, key=str.lower):
        if "uptodate" in name.lower():
            uptodate.append(name)
            continue
        if name in ALREADY_IN_BOT:
            already[name] = ALREADY_IN_BOT[name]
            continue
        if name in SKIPPED:
            skipped[name] = SKIPPED[name]
            continue
        seen, variants = set(), []
        for r in sorted(groups[name], key=lambda r: (r["months"], -r["id"])):
            if r["months"] in seen:
                continue
            seen.add(r["months"])
            variants.append({
                "id": f"pn{r['id']}", "months": r["months"], "label": duration_label(r["months"]),
                "panel_price": r["price"], "price": round(r["price"] + MARKUP, 2),
            })
        products.append({"name": name, "variants": variants})
    CATALOG_PATH.write_text(json.dumps({
        "markup": MARKUP,
        "source": "supplier panel Products list",
        "products": products,
        "excluded": {"uptodate": uptodate, "already_in_bot": already, "skipped": skipped},
    }, indent=1, ensure_ascii=False) + "\n")
    n_var = sum(len(p["variants"]) for p in products)
    print(f"{len(products)} products / {n_var} priced durations written to {CATALOG_PATH.name}; "
          f"excluded {len(uptodate)} UpToDate, {len(already)} already in the bot, {len(skipped)} skipped")


def _block() -> str:
    data = json.loads(CATALOG_PATH.read_text())
    compact = [[p["name"], [[v["id"], v["label"], v["price"]] for v in p["variants"]]] for p in data["products"]]
    return f"{START}\nconst PANEL_CATALOG = {json.dumps(compact, ensure_ascii=False, separators=(',', ':'))};\n{END}"


def sync_html(check_only=False) -> bool:
    html = INDEX_PATH.read_text()
    pat = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if not pat.search(html):
        raise SystemExit("index.html has no PANEL-CATALOG block")
    new = pat.sub(lambda m: _block(), html)
    if check_only:
        return new == html
    INDEX_PATH.write_text(new)
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        build(sys.argv[2])
    elif cmd == "html":
        sync_html()
        print("index.html updated")
    elif cmd == "check":
        ok = sync_html(check_only=True)
        print("index.html matches panel_catalog.json" if ok else "index.html is OUT OF DATE — run: python tools/panel_catalog.py html")
        sys.exit(0 if ok else 1)
    else:
        raise SystemExit(__doc__)
