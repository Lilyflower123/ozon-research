import json
import csv
import time
from urllib.parse import unquote, urlparse
from curl_cffi import requests

NODE_URLS = {
    "N1_E27_BULB": "/search/?text=фитолампа e27",
    "N2_E27_PETAL_FOLD": "/search/?text=фитолампа лепестки e27",
    "N3_LINEAR_T5_T8": "/search/?text=фитолампа линейная t5",
    "N4_CLIP_GOOSENECK": "/search/?text=фитолампа на прищепке таймер",
    "N5_FLOOR_STAND": "/search/?text=фитолампа напольная для растений",
    "N6_QUANTUM_BOARD": "/search/?text=квантум борд фитолампа",
}

TOPN = 15
SLEEP_SEC = 1.2
DEBUG = False

BASES = [
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
    "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
    "https://api.ozon.ru/composer-api.bx/page/json/v2",
]

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
    "referer": "https://www.ozon.ru/",
    "x-o3-app-name": "ozonapp_android",
    "x-o3-app-version": "17.48.0(2528)",
}

def normalize_node_path(p: str) -> str:
    p = (p or "").strip()
    p = unquote(p)
    if p.startswith("http://") or p.startswith("https://"):
        u = urlparse(p)
        path = u.path or "/"
        if u.query:
            path = path + "?" + u.query
        return path if path.startswith("/") else "/" + path
    return p if p.startswith("/") else "/" + p

def deep_iter(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from deep_iter(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from deep_iter(x)

def get_price(d: dict):
    if isinstance(d.get("finalPrice"), (str, int, float)):
        return d.get("finalPrice")
    if isinstance(d.get("price"), (str, int, float)):
        return d.get("price")
    pv2 = d.get("priceV2") or {}
    if isinstance(pv2, dict):
        for k in ("price", "finalPrice", "discountedPrice", "originalPrice"):
            if isinstance(pv2.get(k), (str, int, float)):
                return pv2.get(k)
    return None

def get_rating(d: dict):
    if isinstance(d.get("rating"), (int, float, str)):
        return d.get("rating")
    if isinstance(d.get("score"), (int, float, str)):
        return d.get("score")
    rv2 = d.get("ratingV2") or {}
    if isinstance(rv2, dict):
        for k in ("rating", "value", "score"):
            if isinstance(rv2.get(k), (int, float, str)):
                return rv2.get(k)
    return None

def get_reviews(d: dict):
    for k in ("reviewsCount", "feedbackCount", "commentsCount", "count"):
        v = d.get(k)
        if isinstance(v, (int, float, str)):
            return v
    rv2 = d.get("ratingV2") or {}
    if isinstance(rv2, dict):
        v = rv2.get("count")
        if isinstance(v, (int, float, str)):
            return v
    return ""

def get_url(d: dict):
    u = d.get("link") or d.get("url")
    if isinstance(u, str) and u:
        return u
    action = d.get("action") or {}
    if isinstance(action, dict):
        u2 = action.get("link") or action.get("url")
        if isinstance(u2, str) and u2:
            return u2
    return ""

def pick_products(page_json: dict):
    widget_states = page_json.get("widgetStates", {}) or {}
    decoded = []
    for v in widget_states.values():
        if isinstance(v, str) and v.strip().startswith("{"):
            try:
                decoded.append(json.loads(v))
            except Exception:
                pass

    products = []
    for block in decoded:
        for d in deep_iter(block):
            title = d.get("title") or d.get("name")
            if not isinstance(title, str) or not title.strip():
                continue
            price = get_price(d)
            rating = get_rating(d)
            if price is None or rating is None:
                continue
            products.append({
                "title": title.strip(),
                "price": str(price).strip(),
                "rating": str(rating).strip(),
                "reviews": str(get_reviews(d)).strip(),
                "url": (get_url(d) or "").strip(),
            })

    seen = set()
    uniq = []
    for p in products:
        key = p["url"] or p["title"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq

def fetch_page_json(sess: requests.Session, node_path: str):
    for base in BASES:
        try:
            r = sess.get(
                base,
                params={"url": node_path},
                headers=HEADERS,
                impersonate="chrome120",
                timeout=30,
            )
            if DEBUG:
                print("[try]", r.status_code, r.url)
            if r.status_code == 200 and (r.text.strip().startswith("{") or "application/json" in (r.headers.get("content-type","").lower())):
                return r.json()
        except Exception as e:
            if DEBUG:
                print("[err]", base, e)
    return None

def main():
    sess = requests.Session()
    out_rows = []

    for node, raw_path in NODE_URLS.items():
        time.sleep(SLEEP_SEC)
        node_path = normalize_node_path(raw_path)

        data = fetch_page_json(sess, node_path)
        if not data:
            print(f"[warn] {node} failed. (still blocked: likely IP-level / Cloudflare)")
            continue

        items = pick_products(data)[:TOPN]
        if not items:
            print(f"[warn] {node} got 0 items (need adjust parser)")
            continue

        for i, p in enumerate(items, start=1):
            url = p["url"]
            if url.startswith("/"):
                url = "https://www.ozon.ru" + url
            out_rows.append({
                "node": node,
                "rank": i,
                "title": p["title"],
                "price": p["price"],
                "rating": p["rating"],
                "reviews": p["reviews"],
                "url": url,
            })

    out_file = "ozon_fitolamp_top15_by_node.csv"
    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["node","rank","title","price","rating","reviews","url"])
        w.writeheader()
        w.writerows(out_rows)

    print("Saved:", out_file)
    print("Rows:", len(out_rows))

if __name__ == "__main__":
    main()