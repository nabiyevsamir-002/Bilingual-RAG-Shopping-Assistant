#!/usr/bin/env python3
"""
Alinino.az məhsul scraper — Faza 1 (Məlumatların toplanması)
=============================================================

Sitemap-dan məhsul URL-lərini oxuyur, hər səhifədəki JSON-LD strukturlaşmış
datanı parse edir və təmiz məhsul qeydlərini `products.jsonl` + `products.json`
fayllarına yazır (istəyə bağlı olaraq PostgreSQL-ə də upsert edir).

Xüsusiyyətlər:
  • Etik scraping: sorğular arası gecikmə + jitter, retry, real User-Agent
  • JSON-LD (Product + BreadcrumbList) parse + HTML fallback
  • AZ / RU dil aşkarlanması (metadata kimi saxlanır)
  • RAG üçün hazır `embed_text` sahəsi
  • Resume: artıq yığılmış URL-ləri atlayır (təkrar işə salında)

İstifadə:
  python scraper.py --limit 800            # demo üçün 800 məhsul
  python scraper.py                          # tam kataloq (48k+)
  python scraper.py --limit 800 --workers 4  # 4 paralel işçi
  python scraper.py --postgres               # həm də Postgres-ə yaz

Yalnız `requests` tələb olunur (bax: requirements.txt). Postgres opsionaldır.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------------
# Konfiqurasiya
# ----------------------------------------------------------------------------
BASE = "https://alinino.az"
SITEMAP_URL = f"{BASE}/sitemap.xml"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "AlininoRAGBot/1.0 (+contact: your-email@example.com)"
)
OUT_DIR = Path(__file__).parent
JSONL_PATH = OUT_DIR / "products.jsonl"
JSON_PATH = OUT_DIR / "products.json"

# Postgres sxemi — scraper.py və sync.py arasında paylaşılır.
# `needs_embedding` = n8n indeksləmə workflow-u yalnız bunları embed edir.
# `deleted` = saytdan silinib, Qdrant-dan çıxarılmalıdır.
SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS products (
        sku             TEXT PRIMARY KEY,
        ad              TEXT,
        qiymet          NUMERIC,
        valyuta         TEXT,
        stok            TEXT,
        kateqoriya      TEXT,
        tesvir          TEXT,
        dil             TEXT,
        sekil           TEXT,
        url             TEXT,
        embed_text      TEXT,
        content_hash    TEXT,
        lastmod         DATE,
        needs_embedding BOOLEAN DEFAULT true,
        deleted         BOOLEAN DEFAULT false,
        updated_at      TIMESTAMPTZ DEFAULT now()
    );
    -- Köhnə cədvəllər üçün təhlükəsiz miqrasiya
    ALTER TABLE products ADD COLUMN IF NOT EXISTS content_hash    TEXT;
    ALTER TABLE products ADD COLUMN IF NOT EXISTS lastmod         DATE;
    ALTER TABLE products ADD COLUMN IF NOT EXISTS needs_embedding BOOLEAN DEFAULT true;
    ALTER TABLE products ADD COLUMN IF NOT EXISTS deleted         BOOLEAN DEFAULT false;
    CREATE INDEX IF NOT EXISTS idx_products_needs_embedding ON products (needs_embedding) WHERE needs_embedding;
    CREATE INDEX IF NOT EXISTS idx_products_deleted ON products (deleted) WHERE deleted;
"""

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ----------------------------------------------------------------------------
# HTTP sessiyası (retry + connection pool)
# ----------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "az,ru;q=0.9,en;q=0.8"})
    return s


# ----------------------------------------------------------------------------
# Sitemap
# ----------------------------------------------------------------------------
def fetch_sitemap_product_urls(session: requests.Session) -> list[str]:
    """sitemap.xml-dən bütün /product/ URL-lərini çıxarır (sitemap index-i də dəstəkləyir)."""
    log(f"[sitemap] yüklənir: {SITEMAP_URL}")
    resp = session.get(SITEMAP_URL, timeout=40)
    resp.raise_for_status()
    xml = resp.text

    # Sitemap index-dirsə, alt-sitemap-ları izlə
    sub_sitemaps = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>.*?</sitemap>", xml, re.DOTALL)
    urls: list[str] = []
    if sub_sitemaps:
        log(f"[sitemap] index tapıldı: {len(sub_sitemaps)} alt-sitemap")
        for sm in sub_sitemaps:
            try:
                r = session.get(sm.strip(), timeout=40)
                urls += re.findall(r"<loc>(https://alinino\.az/product/[^<]+)</loc>", r.text)
                time.sleep(0.5)
            except Exception as e:
                log(f"[sitemap] alt-sitemap xətası {sm}: {e}")
    else:
        urls = re.findall(r"<loc>(https://alinino\.az/product/[^<]+)</loc>", xml)

    # Dublikatları at, sırovu qoru
    seen, unique = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    log(f"[sitemap] {len(unique)} unikal məhsul URL-i tapıldı")
    return unique


# ----------------------------------------------------------------------------
# Parse
# ----------------------------------------------------------------------------
_LD_RE = re.compile(
    r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)


def _json_ld_blocks(html: str) -> list[dict]:
    out = []
    for raw in _LD_RE.findall(html):
        try:
            obj = json.loads(unescape(raw.strip()))
        except Exception:
            continue
        if isinstance(obj, list):
            out.extend(x for x in obj if isinstance(x, dict))
        elif isinstance(obj, dict):
            out.append(obj)
    return out


def _meta(html: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]*property="{re.escape(prop)}"[^>]*content="([^"]*)"', html)
    if not m:
        m = re.search(rf'<meta[^>]*content="([^"]*)"[^>]*property="{re.escape(prop)}"', html)
    return unescape(m.group(1)).strip() if m else ""


def _clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)          # HTML tag-larını sil
    s = unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def detect_lang(text: str) -> str:
    """Sadə heuristika: kiril hərfləri üstünlük təşkil edirsə → 'ru', əks halda 'az'."""
    if not text:
        return "az"
    cyr = len(re.findall(r"[а-яА-ЯёЁ]", text))
    lat = len(re.findall(r"[a-zA-ZəƏğĞşŞçÇöÖüÜıİ]", text))
    if cyr == 0 and lat == 0:
        return "az"
    return "ru" if cyr > lat else "az"


def parse_product(url: str, html: str) -> dict | None:
    blocks = _json_ld_blocks(html)
    product, crumbs = {}, []
    for b in blocks:
        t = b.get("@type")
        if t == "Product":
            product = b
        elif t == "BreadcrumbList":
            crumbs = [i.get("name", "") for i in b.get("itemListElement", [])]

    name = (product.get("name") or "").strip() or _meta(html, "og:title")
    description = _clean_text(product.get("description") or "") or _clean_text(_meta(html, "og:description"))

    offers = product.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    offer = offers[0] if offers else {}

    price = str(offer.get("price", "")).strip()
    currency = (offer.get("priceCurrency") or "AZN").strip()
    sku = str(offer.get("sku") or product.get("sku") or "").strip()
    availability = (offer.get("availability") or "").split("/")[-1]  # InStock / OutOfStock

    image = product.get("image") or _meta(html, "og:image")
    if isinstance(image, list):
        image = image[0] if image else ""

    # Kateqoriya: breadcrumb-dan (ilk "Əsas"/"Kataloq" və son məhsul adını at)
    category_parts = [c for c in crumbs if c and c.lower() not in ("əsas", "главная", "home")]
    if category_parts and category_parts[-1].strip() == name.strip():
        category_parts = category_parts[:-1]
    category = " > ".join(category_parts)

    if not name:  # keyfiyyətsiz qeyd — at
        return None

    lang = detect_lang(f"{name} {description}")

    # RAG üçün vahid sənəd mətni
    embed_text = (
        f"Məhsul: {name}\n"
        f"Kateqoriya: {category or '—'}\n"
        f"Qiymət: {price or '—'} {currency} | Stok: {availability or '—'}\n"
        f"Təsvir: {description or '—'}"
    )

    return {
        "sku": sku,
        "ad": name,
        "qiymet": _to_float(price),
        "valyuta": currency,
        "stok": availability,
        "kateqoriya": category,
        "tesvir": description,
        "dil": lang,
        "sekil": image,
        "url": url,
        "embed_text": embed_text,
        "content_hash": hashlib.md5(embed_text.encode("utf-8")).hexdigest(),
    }


def _to_float(s: str):
    try:
        return round(float(str(s).replace(",", ".")), 2)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Bir məhsulu yığ
# ----------------------------------------------------------------------------
def scrape_one(session: requests.Session, url: str, delay: float) -> dict | None:
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            log(f"[skip] {r.status_code} · {url}")
            return None
        rec = parse_product(url, r.text)
        # Etik gecikmə (jitter ilə)
        time.sleep(delay + random.uniform(0, delay))
        return rec
    except Exception as e:
        log(f"[err ] {url} → {e}")
        return None


# ----------------------------------------------------------------------------
# Resume dəstəyi
# ----------------------------------------------------------------------------
def load_done_urls() -> set[str]:
    done = set()
    if JSONL_PATH.exists():
        with JSONL_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["url"])
                except Exception:
                    pass
    return done


# ----------------------------------------------------------------------------
# Postgres (opsional)
# ----------------------------------------------------------------------------
def upsert_postgres(records: list[dict]) -> None:
    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError:
        log("[pg  ] psycopg2 qurulmayıb — Postgres yazısı atlandı (pip install psycopg2-binary)")
        return

    dsn = os.environ.get("PG_DSN", "postgresql://n8n:n8n@localhost:5432/alinino")
    log(f"[pg  ] qoşulur: {dsn.split('@')[-1]}")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    # Sxem — sync.py ilə paylaşılan tam struktur (delta sinxronizasiya sütunları daxil)
    cur.execute(SCHEMA_SQL)
    rows = [(
        r["sku"] or r["url"], r["ad"], r["qiymet"], r["valyuta"], r["stok"],
        r["kateqoriya"], r["tesvir"], r["dil"], r["sekil"], r["url"], r["embed_text"],
        r.get("content_hash"),
    ) for r in records]
    execute_values(cur, """
        INSERT INTO products
            (sku, ad, qiymet, valyuta, stok, kateqoriya, tesvir, dil, sekil, url, embed_text, content_hash)
        VALUES %s
        ON CONFLICT (sku) DO UPDATE SET
            ad=EXCLUDED.ad, qiymet=EXCLUDED.qiymet, valyuta=EXCLUDED.valyuta,
            stok=EXCLUDED.stok, kateqoriya=EXCLUDED.kateqoriya, tesvir=EXCLUDED.tesvir,
            dil=EXCLUDED.dil, sekil=EXCLUDED.sekil, url=EXCLUDED.url,
            embed_text=EXCLUDED.embed_text,
            -- yalnız məzmun dəyişibsə yenidən embedding üçün işarələ
            needs_embedding = (products.content_hash IS DISTINCT FROM EXCLUDED.content_hash),
            content_hash=EXCLUDED.content_hash,
            deleted=false,
            updated_at=now();
    """, rows)
    conn.commit()
    cur.close()
    conn.close()
    log(f"[pg  ] {len(rows)} qeyd upsert edildi")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Alinino.az məhsul scraper")
    ap.add_argument("--limit", type=int, default=0, help="Maksimum məhsul sayı (0 = hamısı)")
    ap.add_argument("--workers", type=int, default=3, help="Paralel işçi sayı (etik: ≤4)")
    ap.add_argument("--delay", type=float, default=0.7, help="Sorğular arası baza gecikmə (san)")
    ap.add_argument("--postgres", action="store_true", help="Postgres-ə də upsert et")
    ap.add_argument("--fresh", action="store_true", help="Əvvəlki nəticələri sil, sıfırdan başla")
    args = ap.parse_args()

    if args.fresh and JSONL_PATH.exists():
        JSONL_PATH.unlink()

    session = make_session()
    urls = fetch_sitemap_product_urls(session)

    done = load_done_urls()
    if done:
        log(f"[resume] {len(done)} məhsul artıq mövcuddur, atlanılır")
        urls = [u for u in urls if u not in done]

    if args.limit:
        urls = urls[: args.limit]

    total = len(urls)
    if total == 0:
        log("[done] yeni məhsul yoxdur.")
        _finalize(args)
        return

    log(f"[start] {total} məhsul yığılır · {args.workers} işçi · ~{args.delay}s gecikmə")
    t0 = time.time()
    collected = 0

    with JSONL_PATH.open("a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(scrape_one, session, u, args.delay): u for u in urls}
            for i, fut in enumerate(as_completed(futures), 1):
                rec = fut.result()
                if rec:
                    with _print_lock:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out.flush()
                    collected += 1
                if i % 25 == 0 or i == total:
                    rate = i / max(time.time() - t0, 1e-9)
                    log(f"[prog] {i}/{total} · yığıldı={collected} · {rate:.1f}/san")

    log(f"[done] {collected}/{total} məhsul yığıldı · {time.time()-t0:.0f}s")
    _finalize(args)


def _finalize(args) -> None:
    # JSONL → JSON massiv (n8n üçün rahat)
    records = []
    if JSONL_PATH.exists():
        with JSONL_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    JSON_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[write] {JSON_PATH.name} · {len(records)} qeyd")

    # Qısa statistika
    if records:
        az = sum(1 for r in records if r["dil"] == "az")
        ru = sum(1 for r in records if r["dil"] == "ru")
        instock = sum(1 for r in records if r["stok"] == "InStock")
        cats = len({r["kateqoriya"] for r in records if r["kateqoriya"]})
        log(f"[stat] dil: AZ={az} RU={ru} | stokda={instock} | unikal kateqoriya={cats}")

    if args.postgres and records:
        upsert_postgres(records)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n[stop] istifadəçi dayandırdı. Nəticələr saxlanılıb, təkrar işə salsanız davam edəcək.")
        sys.exit(130)
