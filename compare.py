"""
סוכן השוואת סל קניות שבועי.

מוריד את קבצי השקיפות (סניפים, מחירים, מבצעים) של הרשתות שב-config.json,
מסנן לסניפים בערים שבהגדרות, מחשב את מחיר הסל מ-basket.csv בכל סניף,
ומייצר את docs/index.html (העמוד המתעדכן) ואת docs/data.json.

הרצה:
    python compare.py              # הורדה + חישוב
    python compare.py --no-download   # חישוב בלבד, על קבצים שכבר הורדו לתיקיית dumps
"""
import argparse
import asyncio
import csv
import datetime as dt
import gzip
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parent
DUMPS = ROOT / "dumps"
DOCS = ROOT / "docs"

CHAIN_NAMES = {
    "OSHER_AD": "אושר עד", "RAMI_LEVY": "רמי לוי", "SHUFERSAL": "שופרסל",
    "YOHANANOF": "יוחננוף", "VICTORY_NEW_SOURCE": "ויקטורי",
    "YAYNO_BITAN_AND_CARREFOUR": "יינות ביתן / קרפור", "HAZI_HINAM": "חצי חינם",
    "SUPER_YUDA": "סופר יודה", "TIV_TAAM": "טיב טעם",
    "MAHSANI_ASHUK_NEW_SOURCE": "מחסני השוק", "NETIV_HASED": "נתיב החסד",
}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- XML helpers

def read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(z.namelist()[0])
    return data


def parse_xml(path: Path):
    try:
        parser = etree.XMLParser(recover=True, huge_tree=True)
        return etree.fromstring(read_bytes(path), parser)
    except Exception as e:  # noqa: BLE001
        log(f"  ! לא הצלחתי לקרוא {path.name}: {e}")
        return None


def lname(el):
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.split("}")[-1].lower()


def kids(el):
    """child elements as {lowercase-name: text}"""
    out = {}
    for c in el:
        n = lname(c)
        if n and n not in out:
            out[n] = (c.text or "").strip()
    return out


def num(s, default=None):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return default


def norm_code(code):
    return str(code or "").strip().lstrip("0")


# ---------------------------------------------------------------- download

def chain_folder(chain_key):
    # one folder per chain inside dumps/, named after the chain key
    return DUMPS / chain_key


async def _scrape(chain_key, files_types, store_id=None):
    from il_supermarket_scarper.scrappers_factory import ScraperFactory
    from il_supermarket_scarper.utils import DiskFileOutput
    cls = getattr(ScraperFactory, chain_key).value
    folder = chain_folder(chain_key)
    scraper = cls(file_output=DiskFileOutput(storage_path=str(folder)))
    n = 0
    async for _ in scraper.scrape(files_types=files_types, store_id=store_id):
        n += 1
    return n


def download_stores(cfg):
    from il_supermarket_scarper import FileTypesFilters
    for chain in cfg["chains"]:
        log(f"סניפים: {CHAIN_NAMES.get(chain, chain)}")
        try:
            asyncio.run(_scrape(chain, FileTypesFilters.only_store()))
        except Exception as e:  # noqa: BLE001
            log(f"  ! נכשל: {e}")


def download_prices(cfg, stores):
    from il_supermarket_scarper import FileTypesFilters
    for s in stores:
        log(f"מחירים: {s['chain_name']} - {s['name']}")
        try:
            asyncio.run(_scrape(s["chain"], FileTypesFilters.all_full_files(),
                                store_id=int(s["store_id"])))
        except Exception as e:  # noqa: BLE001
            log(f"  ! נכשל: {e}")


# ---------------------------------------------------------------- stores

def store_in_area(k, cities, codes):
    """match on the City field only (it holds a name or a CBS city code).
    Addresses are not used: many towns have a 'שדרות ירושלים' street."""
    city = (k.get("city") or "").strip()
    if city and city.strip("0"):
        return norm_code(city) in codes or any(c in city for c in cities)
    return any(c in (k.get("storename") or "") for c in cities)


def find_local_stores(cfg):
    cities = [c.strip() for c in cfg["cities"]]
    codes = {norm_code(c) for c in cfg.get("city_codes", [])}
    stores = []
    for chain in cfg["chains"]:
        folder = chain_folder(chain)
        files = sorted((p for p in folder.rglob("*") if p.is_file() and "store" in p.name.lower()),
                       key=lambda p: p.name)
        if not files:
            log(f"  {CHAIN_NAMES.get(chain, chain)}: לא הורד קובץ סניפים")
            continue
        root = parse_xml(files[-1])  # newest stores file
        if root is None:
            continue
        seen, total, samples = set(), 0, set()
        for el in root.iter():
            k = kids(el)
            if "storeid" not in k or not ({"city", "address", "storename"} & k.keys()):
                continue
            total += 1
            if len(samples) < 12:
                samples.add(f'{k.get("city", "")}/{k.get("storename", "")}')
            if not store_in_area(k, cities, codes):
                continue
            sid = norm_code(k["storeid"])
            if not sid or sid in seen:
                continue
            seen.add(sid)
            stores.append({
                "chain": chain, "chain_name": CHAIN_NAMES.get(chain, chain),
                "store_id": sid, "name": k.get("storename", ""),
                "address": k.get("address", ""), "city": k.get("city", ""),
            })
        log(f"  {CHAIN_NAMES.get(chain, chain)}: {len(seen)} סניפים באזור מתוך {total}")
        if not seen:
            log(f"    דוגמאות עיר/סניף: {sorted(samples)}")
    return stores


def files_for_store(chain, store_id, kind):
    """newest PriceFull / PromoFull file of one store"""
    folder = chain_folder(chain)
    pat = re.compile(rf"{kind}\d*-(?:\d+-)?0*{store_id}-\d", re.I)
    files = [p for p in folder.rglob("*") if p.is_file() and pat.search(p.name)]
    if not files:
        return None
    return max(files, key=lambda p: re.findall(r"\d{12}", p.name)[-1] if re.findall(r"\d{12}", p.name) else p.name)


# ---------------------------------------------------------------- prices

def load_prices(path):
    items = {}
    root = parse_xml(path) if path else None
    if root is None:
        return items
    for el in root.iter():
        if lname(el) != "item":
            continue
        k = kids(el)
        code = norm_code(k.get("itemcode"))
        price = num(k.get("itemprice"))
        if not code or not price:
            continue
        items[code] = {
            "code": code, "name": k.get("itemname") or k.get("manufactureritemdescription", ""),
            "price": price, "weighted": k.get("bisweighted", "0") in ("1", "true"),
            "unit": k.get("unitofmeasure", ""),
        }
    return items


def load_promos(path):
    """{item_code: [promo,...]}"""
    promos = {}
    root = parse_xml(path) if path else None
    if root is None:
        return promos
    today = dt.date.today().isoformat()
    for el in root.iter():
        if lname(el) != "promotion":
            continue
        k = kids(el)
        end = (k.get("promotionenddate") or "9999")[:10]
        if end < today:
            continue
        club = k.get("clubid", "0").strip()
        p = {
            "desc": k.get("promotiondescription", ""),
            "min_qty": num(k.get("minqty"), 1) or 1,
            "price": num(k.get("discountedprice")),
            "end": end,
            "club": club not in ("", "0", "00"),
        }
        if not p["price"]:
            continue
        for item in el.iter():
            if lname(item) == "item":
                code = norm_code(kids(item).get("itemcode"))
                if code:
                    promos.setdefault(code, []).append(p)
    return promos


def promo_unit_price(promo, regular):
    """price per unit under a promotion (DiscountedPrice is usually the total for MinQty units)"""
    mq, dp = promo["min_qty"], promo["price"]
    if mq > 1 and dp > regular * 0.999:
        return dp / mq
    return dp


# ---------------------------------------------------------------- matching

STOP = {"ג'", "גרם", "ק\"ג", "ל'", "יח'", "ליטר", "מ\"ל", "(שק)", "ארוז"}


def tokens(name):
    t = re.sub(r"[()]", " ", name)
    return [w for w in t.split() if w not in STOP and not re.fullmatch(r"[\d.×x*%]+", w)]


def is_barcode(code):
    return len(code) >= 8 and not code.startswith("900")


def shares_word(a, b):
    wa = {w for w in tokens(a) if len(w) > 2}
    return any(w in b for w in wa)


def match_item(row, prices):
    """returns (item, how) or (None, None)"""
    code = norm_code(row["barcode"])
    if code in prices:
        return prices[code], "barcode"
    if is_barcode(code):
        return None, None
    # short code from the Osher Ad receipt.
    # 1) often it is the tail of the real barcode (e.g. 66318 -> 7290000066318)
    if len(code) >= 5:
        tails = [it for c, it in prices.items()
                 if len(c) >= 12 and c.endswith(code) and shares_word(row["name"], it["name"])]
        if len(tails) == 1:
            return tails[0], "barcode"
    # 2) fresh produce sold by weight: match on the exact produce name
    if row["unit"] != 'ק"ג':
        return None, None
    want = " ".join(tokens(row["name"]))
    cands = [it for it in prices.values() if it["weighted"]
             and (it["name"].strip() == want or it["name"].startswith(want + " "))]
    if not cands:
        return None, None
    return min(cands, key=lambda it: it["price"]), "name"


# ---------------------------------------------------------------- compute

def compute(cfg, basket, stores):
    results = []
    for s in stores:
        prices = load_prices(files_for_store(s["chain"], s["store_id"], "pricefull"))
        if not prices:
            continue
        promos = load_promos(files_for_store(s["chain"], s["store_id"], "promofull"))
        lines, total, total_regular, missing_est, found = [], 0.0, 0.0, 0.0, 0
        for row in basket:
            qty = float(row["qty"])
            weekly = row["frequency"] == "weekly"
            item, how = match_item(row, prices)
            line = {"name": row["name"], "qty": qty, "unit": row["unit"], "weekly": weekly,
                    "ref": float(row["last_price"])}
            if item:
                reg = item["price"]
                unit_price, promo_txt, club_txt = reg, "", ""
                for p in promos.get(item["code"], []):
                    up = promo_unit_price(p, reg)
                    if up >= reg:
                        continue
                    if p["club"]:
                        club_txt = p["desc"]
                        continue
                    if qty >= p["min_qty"] and up < unit_price:
                        unit_price, promo_txt = up, p["desc"]
                cost = unit_price * qty
                line.update(found=True, how=how, item=item["name"], price=round(unit_price, 2),
                            cost=round(cost, 2), promo=promo_txt, club=club_txt)
                if weekly:
                    total += cost
                    total_regular += reg * qty
                    found += 1
            else:
                line.update(found=False, cost=round(line["ref"] * qty, 2))
                if weekly:
                    missing_est += line["ref"] * qty
            lines.append(line)
        n_weekly = sum(1 for r in basket if r["frequency"] == "weekly")
        results.append({**s, "coverage": round(found / n_weekly, 3), "found": found,
                        "n_items": n_weekly, "total_found": round(total, 2),
                        "saved_by_promos": round(total_regular - total, 2),
                        "missing_estimate": round(missing_est, 2),
                        "total": round(total + missing_est, 2), "lines": lines,
                        "_prices": prices, "_promos": promos})
    ranked = sorted([r for r in results if r["coverage"] >= cfg["min_coverage"]], key=lambda r: r["total"])
    partial = sorted([r for r in results if r["coverage"] < cfg["min_coverage"]], key=lambda r: -r["coverage"])
    return ranked, partial


def other_brand_deals(cfg, basket, ranked, top=3):
    """strong promotions on similar products from other brands, in the cheapest stores"""
    basket_codes = {norm_code(r["barcode"]) for r in basket}
    deals = []
    for s in ranked[:top]:
        prices, promos = s["_prices"], s["_promos"]
        for row, line in zip(basket, s["lines"]):
            if not line.get("found"):
                continue
            key = tokens(row["name"])[:1]
            if not key:
                continue
            for code, plist in promos.items():
                if code in basket_codes or code not in prices:
                    continue
                it = prices[code]
                if key[0] not in it["name"].split():
                    continue
                for p in plist:
                    if p["club"]:
                        continue
                    up = promo_unit_price(p, it["price"])
                    off = 100 * (1 - up / it["price"])
                    if off >= cfg["good_deal_percent"] and up < line["price"]:
                        deals.append({"store": f'{s["chain_name"]} {s["name"]}', "instead_of": row["name"],
                                      "item": it["name"], "deal": p["desc"], "unit_price": round(up, 2),
                                      "your_price": line["price"], "off": round(off), "until": p["end"]})
    seen, out = set(), []
    for d in sorted(deals, key=lambda d: -d["off"]):
        k = (d["store"], d["item"])
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out[:15]


# ---------------------------------------------------------------- output

def write_outputs(cfg, ranked, partial, deals, stores_count):
    now = dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    strip = lambda r: {k: v for k, v in r.items() if not k.startswith("_")}  # noqa: E731
    data = {"updated": now, "cities": cfg["cities"], "stores_checked": stores_count,
            "ranked": [strip(r) for r in ranked], "partial": [strip(r) for r in partial[:10]],
            "deals": deals}
    DOCS.mkdir(exist_ok=True)
    hist_path = DOCS / "history.json"
    try:
        history = json.loads(hist_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    if ranked:
        history.append({"date": dt.date.today().isoformat(), "best": ranked[0]["total"],
                        "store": f'{ranked[0]["chain_name"]} {ranked[0]["name"]}'})
        history = history[-26:]
        hist_path.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    data["history"] = history
    (DOCS / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    template = (ROOT / "page_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    log(f"נכתב: {DOCS / 'index.html'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    with open(ROOT / "basket.csv", encoding="utf-8") as f:
        basket = list(csv.DictReader(f))

    if not args.no_download:
        download_stores(cfg)
    stores = find_local_stores(cfg)
    log(f"נמצאו {len(stores)} סניפים באזור")
    if not stores:
        log("לא נמצאו סניפים. ייתכן שההורדה נחסמה (חלק מהרשתות חוסמות גישה מחוץ לישראל).")
        sys.exit(1)
    if not args.no_download:
        download_prices(cfg, stores)
    ranked, partial = compute(cfg, basket, stores)
    deals = other_brand_deals(cfg, basket, ranked)
    if ranked:
        best = {l["name"]: l for l in ranked[0]["lines"] if l["weekly"] and l.get("found")}
        for r in ranked + partial:
            mine = {l["name"]: l for l in r["lines"] if l["weekly"] and l.get("found")}
            com = set(best) & set(mine)
            r["same_items"] = {"n": len(com), "here": round(sum(mine[n]["cost"] for n in com), 2),
                               "best": round(sum(best[n]["cost"] for n in com), 2)}
    write_outputs(cfg, ranked, partial, deals, len(stores))
    if ranked:
        b = ranked[0]
        log(f"הזול ביותר: {b['chain_name']} {b['name']} - ₪{b['total']:.0f}")


if __name__ == "__main__":
    main()
