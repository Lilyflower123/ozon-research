# ozon_parse_saved_json.py (improved)
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

OZON_HOST = "https://www.ozon.ru"

# only treat strings as links if they look like URLs
def looks_like_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("/") or s.startswith("http://") or s.startswith("https://") or s.startswith("//")

PRODUCT_PATH_RE = re.compile(r"/(product|context/detail)/", re.I)

PRICE_RE = re.compile(r"(\d[\d\s\u00a0]{0,12})\s*₽")
REVIEWS_RU_RE = re.compile(r"(\d[\d\s\u00a0]{0,12})\s*(?:отзыв|отзыва|отзывов)", re.I)
# rating: prefer 3.0-5.0
RATING_DEC_RE = re.compile(r"\b([3-5][\.,]\d)\b")
RATING_INT_RE = re.compile(r"\b([3-5])\b")

CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]")


def clean_text(s: str) -> str:
    s = s.lstrip("\ufeff")
    s = CONTROL_CHARS_RE.sub("", s)
    s = s.strip()
    i1 = s.find("{")
    i2 = s.find("[")
    starts = [i for i in (i1, i2) if i != -1]
    if starts:
        s = s[min(starts):]
    return s


def safe_json_loads(text: str) -> Optional[Any]:
    if not text:
        return None
    t = clean_text(text)
    if not t:
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def maybe_parse_json_string(v: Any, depth: int = 3) -> Any:
    cur = v
    for _ in range(depth):
        if isinstance(cur, str):
            obj = safe_json_loads(cur)
            if obj is None:
                return cur
            cur = obj
        else:
            return cur
    return cur


def headers_to_dict(hlist: Any) -> Dict[str, str]:
    out = {}
    if isinstance(hlist, list):
        for h in hlist:
            if isinstance(h, dict):
                k = str(h.get("name", "")).lower().strip()
                v = str(h.get("value", "")).strip()
                if k:
                    out[k] = v
    return out


def decode_body_from_har_entry(entry: dict) -> Tuple[str, int, str]:
    """
    Return (request_url, status, decoded_text) from HAR entry.
    Handles base64 + gzip/br where possible.
    """
    req_url = (entry.get("request") or {}).get("url") or ""
    resp = entry.get("response") or {}
    status = int(resp.get("status") or 0)

    content = (resp.get("content") or {})
    text = content.get("text") or ""
    if not text:
        return req_url, status, ""

    encoding = (content.get("encoding") or "").lower().strip()
    resp_headers = headers_to_dict(resp.get("headers"))
    cenc = resp_headers.get("content-encoding", "").lower()

    if encoding == "base64":
        try:
            raw = base64.b64decode(text)
        except Exception:
            return req_url, status, ""
    else:
        raw = text.encode("utf-8", "ignore")

    if raw and ("gzip" in cenc or raw.startswith(b"\x1f\x8b")):
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass

    if raw and "br" in cenc:
        try:
            import brotli
            raw = brotli.decompress(raw)
        except Exception:
            pass

    try:
        return req_url, status, raw.decode("utf-8", "ignore") if raw else ""
    except Exception:
        return req_url, status, ""


def normalize_url(link: str) -> str:
    if not link:
        return ""
    link = link.strip()
    if link.startswith("//"):
        return "https:" + link
    if link.startswith("/"):
        return OZON_HOST + link
    return link


@dataclass
class Product:
    title: str
    url: str
    price: str = ""
    rating: str = ""
    reviews: str = ""


def iter_all_dicts(root: Any) -> Iterable[Dict[str, Any]]:
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            for v in cur.values():
                stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                stack.append(v)


def iter_widgetstates_objects(root: Any) -> Iterable[Any]:
    for d in iter_all_dicts(root):
        ws = d.get("widgetStates")
        if isinstance(ws, dict):
            for v in ws.values():
                yield maybe_parse_json_string(v, depth=3)


def collect_string_leaves(x: Any, limit: int = 3000) -> List[str]:
    out: List[str] = []
    stack = [x]
    while stack and len(out) < limit:
        cur = stack.pop()
        if isinstance(cur, str):
            s = cur.strip()
            if s:
                out.append(s)
        elif isinstance(cur, dict):
            for v in cur.values():
                stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                stack.append(v)
    return out


def guess_title(strings: List[str]) -> str:
    # hard block obvious UA / tech strings
    ua_bad = ("Mozilla/5.0", "AppleWebKit", "Windows NT", "Chrome/", "Safari/")
    bad_tokens = ("ozon", "добав", "в корз", "₽", "отзыв", "скид", "завтра", "руб")

    cands = []
    for s in strings:
        ss = s.replace("\u00a0", " ").strip()
        if any(b in ss for b in ua_bad):
            continue
        if len(ss) < 8 or len(ss) > 220:
            continue
        if "http" in ss or "/product/" in ss or "/context/detail" in ss:
            continue
        low = ss.lower()
        if any(t in low for t in bad_tokens):
            continue
        if not re.search(r"[A-Za-zА-Яа-я]", ss):
            continue
        cands.append(ss)

    if not cands:
        return ""
    cands.sort(key=lambda t: (len(t), t.count(" ")), reverse=True)
    return cands[0]


def guess_price(strings: List[str]) -> str:
    for s in strings:
        m = PRICE_RE.search(s.replace("\u00a0", " "))
        if m:
            return m.group(0).strip()
    return ""


def guess_reviews(strings: List[str]) -> str:
    for s in strings:
        m = REVIEWS_RU_RE.search(s.replace("\u00a0", " "))
        if m:
            return m.group(1).strip()
    return ""


def guess_rating(strings: List[str]) -> str:
    # prefer decimal ratings 3.0-5.0
    for s in strings:
        if "₽" in s or "руб" in s.lower():
            continue
        m = RATING_DEC_RE.search(s.replace(",", "."))
        if m:
            return m.group(1).replace(",", ".")
    # fallback integer 3-5
    for s in strings:
        if "₽" in s or "руб" in s.lower():
            continue
        m = RATING_INT_RE.search(s)
        if m:
            return m.group(1)
    return ""


def extract_products_from_payload(root: Any) -> List[Product]:
    products: List[Product] = []
    seen: set[str] = set()

    def add(url: str, title: str, price: str, rating: str, reviews: str):
        if not url or not looks_like_url(url):
            return
        if not PRODUCT_PATH_RE.search(url):
            return
        url2 = normalize_url(url)
        key = url2.split("?")[0]
        if key in seen:
            return
        if not title:
            return
        seen.add(key)
        products.append(Product(title=title, url=url2, price=price, rating=rating, reviews=reviews))

    def scan_dict(d: Dict[str, Any]):
        strings = collect_string_leaves(d, limit=1200)
        # links: only strings that look like URLs AND contain product path
        links = []
        for s in strings:
            if looks_like_url(s) and PRODUCT_PATH_RE.search(s):
                links.append(s)
        links = list(dict.fromkeys(links))
        if len(links) != 1:
            return

        title = guess_title(strings)
        price = guess_price(strings)
        rating = guess_rating(strings)
        reviews = guess_reviews(strings)
        add(links[0], title, price, rating, reviews)

    for d in iter_all_dicts(root):
        scan_dict(d)

    for ws_obj in iter_widgetstates_objects(root):
        if isinstance(ws_obj, (dict, list)):
            for d in iter_all_dicts(ws_obj):
                scan_dict(d)

    return products


def merge_keep_order(lists: List[List[Product]], topn: int = 15) -> List[Product]:
    out: List[Product] = []
    seen = set()
    for lst in lists:
        for p in lst:
            key = p.url.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
            if len(out) >= topn:
                return out
    return out


def parse_har_collect_all(path: Path, debug: bool = False) -> Tuple[List[Product], List[Tuple[int, str]]]:
    har_obj = safe_json_loads(path.read_text("utf-8", errors="ignore"))
    if not isinstance(har_obj, dict):
        return [], []

    entries = har_obj.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        return [], []

    per_entry_products: List[List[Product]] = []
    dbg: List[Tuple[int, str]] = []

    for e in entries:
        req_url, status, body = decode_body_from_har_entry(e)
        if status != 200:
            continue
        # only keep the real data endpoint
        if "entrypoint-api.bx/page/json/v2" not in req_url:
            continue
        obj = safe_json_loads(body)
        if obj is None:
            continue
        products = extract_products_from_payload(obj)
        if products:
            per_entry_products.append(products)
            dbg.append((len(products), req_url))

    merged = merge_keep_order(per_entry_products, topn=15)

    if debug:
        print("[debug] per-entry extracted counts (top 6):")
        for n, u in sorted(dbg, key=lambda x: x[0], reverse=True)[:6]:
            print(" ", n, u[:160])
        print("[debug] merged unique =", len(merged))
        if merged:
            print("[debug] sample:")
            for p in merged[:3]:
                print("  -", p.title[:60], "|", p.price, "|", p.rating, "|", p.url[:80])

    return merged, dbg


def parse_json_single(path: Path) -> List[Product]:
    obj = safe_json_loads(path.read_text("utf-8", errors="ignore"))
    if obj is None:
        return []
    return extract_products_from_payload(obj)[:15]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshots_dir", nargs="?", default="snapshots")
    ap.add_argument("--out", default="ozon_fitolamp_top15_by_node.csv")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    snap = Path(args.snapshots_dir)
    files = sorted([p for p in snap.iterdir() if p.is_file() and p.suffix.lower() in (".har", ".json", ".txt")])
    if not files:
        raise SystemExit("snapshots/ 下没有 .har/.json/.txt 文件")

    rows: List[Dict[str, str]] = []
    best_by_node: Dict[str, List[Product]] = {}

    for fp in files:
        node = fp.stem
        if fp.suffix.lower() == ".har":
            products, _ = parse_har_collect_all(fp, debug=args.debug)
        else:
            products = parse_json_single(fp)

        print(f"[ok] {node}: extracted_products={len(products)} ({fp.name})")
        if not products:
            continue

        # keep the longer list if multiple files share same node
        prev = best_by_node.get(node, [])
        if len(products) > len(prev):
            best_by_node[node] = products

    for node, products in best_by_node.items():
        for i, p in enumerate(products[:15], start=1):
            rows.append({
                "node": node,
                "rank": str(i),
                "title": p.title,
                "price": p.price,
                "rating": p.rating,
                "reviews": p.reviews,
                "url": p.url,
            })

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["node", "rank", "title", "price", "rating", "reviews", "url"])
        w.writeheader()
        w.writerows(rows)

    print("Saved:", out)
    print("Rows:", len(rows))


if __name__ == "__main__":
    main()
