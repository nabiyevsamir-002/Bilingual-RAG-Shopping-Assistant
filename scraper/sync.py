#!/usr/bin/env python3
"""
Alinino.az inkremental sinxronizator — Faza 2 (Delta yenilənmə)
================================================================

Bütün kataloqu yenidən yığmaq ƏVƏZİNƏ, yalnız DƏYİŞƏNLƏRİ tapıb yeniləyir.
Sitemap-dakı `<lastmod>` tarixi + məzmun hash-ı ilə üç növ dəyişikliyi aşkarlayır:

  • YENİ məhsul     → sitemap-da var, bazada yox        → yığ + embed işarələ
  • DƏYİŞƏN məhsul  → lastmod yenilənib VƏ hash fərqli  → yığ + embed işarələ + köhnə Qdrant nöqtəsini sil
  • SİLİNƏN məhsul  → bazada var, sitemap-da yox         → Qdrant-dan sil + bazadan sil

Necə işləyir (məsuliyyət bölgüsü):
  sync.py  → sitemap fərqi + scraping + PostgreSQL vəziyyəti (+ Qdrant SİLMƏ)
  n8n (03) → yalnız `needs_embedding=true` olanları embed edib Qdrant-a INSERT edir

İstifadə:
  python sync.py --dry-run                 # heç nə yazmadan fərqi göstər (test)
  python sync.py --postgres --qdrant       # tam sinxron (Postgres + Qdrant silmə)
  python sync.py --postgres --qdrant --limit 500   # test üçün məhdud

Mühit dəyişənləri:
  PG_DSN, QDRANT_URL (default http://localhost:6333),
  QDRANT_COLLECTION (default alinino_products),
  QDRANT_SKU_KEY   (Qdrant payload-da sku açarı, default "metadata.sku")
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from scraper import make_session, scrape_one, log, SITEMAP_URL, SCHEMA_SQL, JSON_PATH

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "alinino_products")
QDRANT_SKU_KEY = os.environ.get("QDRANT_SKU_KEY", "metadata.sku")
PG_DSN = os.environ.get("PG_DSN", "postgresql://n8n:n8n@localhost:5432/alinino")


# ----------------------------------------------------------------------------
# Sitemap → {url: lastmod}
# ----------------------------------------------------------------------------
def fetch_sitemap_with_lastmod(session) -> dict[str, str | None]:
    log(f"[sitemap] yüklənir: {SITEMAP_URL}")
    resp = session.get(SITEMAP_URL, timeout=40)
    resp.raise_for_status()
    xml = resp.text
    result: dict[str, str | None] = {}

    def parse_urlset(text: str) -> None:
        for block in re.findall(r"<url>(.*?)</url>", text, re.DOTALL):
            loc = re.search(r"<loc>(https://alinino\.az/product/[^<]+)</loc>", block)
            if not loc:
                continue
            lm = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            result[loc.group(1)] = lm.group(1).strip()[:10] if lm else None

    subs = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>.*?</sitemap>", xml, re.DOTALL)
    if subs:
        log(f"[sitemap] index: {len(subs)} alt-sitemap")
        for sm in subs:
            try:
                r = session.get(sm.strip(), timeout=40)
                parse_urlset(r.text)
                time.sleep(0.4)
            except Exception as e:
                log(f"[sitemap] alt xəta {sm}: {e}")
    else:
        parse_urlset(xml)

    log(f"[sitemap] {len(result)} məhsul (lastmod ilə)")
    return result


# ----------------------------------------------------------------------------
# Qdrant silmə (REST, AI lazım deyil)
# ----------------------------------------------------------------------------
def qdrant_delete_skus(skus: list[str]) -> None:
    if not skus:
        return
    body = {"filter": {"must": [{"key": QDRANT_SKU_KEY, "match": {"any": skus}}]}}
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
            json=body, params={"wait": "true"}, timeout=30,
        )
        if r.status_code < 300:
            log(f"[qdrant] {len(skus)} köhnə nöqtə silindi")
        else:
            log(f"[qdrant] silmə xətası {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log(f"[qdrant] silmə bağlantı xətası: {e}")


# ----------------------------------------------------------------------------
# Postgres
# ----------------------------------------------------------------------------
def pg_connect():
    import psycopg2
    return psycopg2.connect(PG_DSN)


def load_db_state(conn) -> dict[str, dict]:
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)          # sxemi təmin et (idempotent)
    conn.commit()
    cur.execute(
        "SELECT url, sku, to_char(lastmod,'YYYY-MM-DD'), content_hash "
        "FROM products WHERE deleted = false;"
    )
    state = {url: {"sku": sku, "lastmod": lm, "content_hash": ch}
             for url, sku, lm, ch in cur.fetchall()}
    cur.close()
    return state


def upsert_products(conn, records: list[dict]) -> None:
    if not records:
        return
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    rows = [(
        r["sku"] or r["url"], r["ad"], r["qiymet"], r["valyuta"], r["stok"],
        r["kateqoriya"], r["tesvir"], r["dil"], r["sekil"], r["url"],
        r["embed_text"], r.get("content_hash"), r.get("lastmod"),
    ) for r in records]
    execute_values(cur, """
        INSERT INTO products
            (sku, ad, qiymet, valyuta, stok, kateqoriya, tesvir, dil, sekil, url, embed_text, content_hash, lastmod)
        VALUES %s
        ON CONFLICT (sku) DO UPDATE SET
            ad=EXCLUDED.ad, qiymet=EXCLUDED.qiymet, valyuta=EXCLUDED.valyuta, stok=EXCLUDED.stok,
            kateqoriya=EXCLUDED.kateqoriya, tesvir=EXCLUDED.tesvir, dil=EXCLUDED.dil, sekil=EXCLUDED.sekil,
            url=EXCLUDED.url, embed_text=EXCLUDED.embed_text,
            needs_embedding = (products.content_hash IS DISTINCT FROM EXCLUDED.content_hash),
            content_hash=EXCLUDED.content_hash, lastmod=EXCLUDED.lastmod,
            deleted=false, updated_at=now();
    """, rows)
    conn.commit()
    cur.close()


def backfill_lastmod(conn, pairs: list[tuple[str, str]]) -> int:
    """Toxunulmayan mövcud məhsullara lastmod baseline yazır (yığmadan)."""
    if not pairs:
        return 0
    from psycopg2.extras import execute_values
    cur = conn.cursor()
    execute_values(cur, """
        UPDATE products AS p SET lastmod = v.lastmod::date
        FROM (VALUES %s) AS v(url, lastmod)
        WHERE p.url = v.url AND p.lastmod IS NULL;
    """, pairs)
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def remove_deleted(conn, urls: list[str], use_qdrant: bool) -> list[str]:
    if not urls:
        return []
    cur = conn.cursor()
    cur.execute("SELECT sku FROM products WHERE url = ANY(%s);", (urls,))
    skus = [r[0] for r in cur.fetchall() if r[0]]
    if use_qdrant:
        qdrant_delete_skus(skus)
    cur.execute("DELETE FROM products WHERE url = ANY(%s);", (urls,))
    conn.commit()
    cur.close()
    return skus


# ----------------------------------------------------------------------------
# Yığım (paralel)
# ----------------------------------------------------------------------------
def scrape_urls(session, url_lastmod: list[tuple[str, str | None]], workers: int, delay: float) -> list[dict]:
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scrape_one, session, u, delay): (u, lm) for u, lm in url_lastmod}
        for i, fut in enumerate(as_completed(futs), 1):
            u, lm = futs[fut]
            rec = fut.result()
            if rec:
                rec["lastmod"] = lm
                out.append(rec)
            if i % 25 == 0 or i == len(futs):
                log(f"[scrape] {i}/{len(futs)}")
    return out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Alinino inkremental sinxronizator")
    ap.add_argument("--postgres", action="store_true", help="Postgres-ə yaz (real sinxron)")
    ap.add_argument("--qdrant", action="store_true", help="Silinən/dəyişən nöqtələri Qdrant-dan da sil")
    ap.add_argument("--dry-run", action="store_true", help="Heç nə yazma, yalnız fərqi göstər")
    ap.add_argument("--limit", type=int, default=0, help="Yığılacaq yeni/dəyişən məhsul limiti (test)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.6)
    args = ap.parse_args()

    dry = args.dry_run or not args.postgres
    session = make_session()

    # 1) Cari vəziyyət (sitemap) və məlum vəziyyət (baza/JSON)
    sitemap = fetch_sitemap_with_lastmod(session)

    conn = None
    if args.postgres:
        conn = pg_connect()
        db = load_db_state(conn)
    else:
        # Postgres olmadan: products.json məlum vəziyyət kimi (dry-run test)
        db = {}
        if JSON_PATH.exists():
            for r in json.loads(JSON_PATH.read_text(encoding="utf-8")):
                db[r["url"]] = {"sku": r.get("sku"), "lastmod": r.get("lastmod"),
                                "content_hash": r.get("content_hash")}
        log(f"[state] products.json-dan {len(db)} məlum məhsul (dry-run)")

    cur_urls, known = set(sitemap), set(db)
    new_urls = cur_urls - known
    deleted_urls = known - cur_urls
    both = cur_urls & known

    changed_candidates = [u for u in both
                          if db[u]["lastmod"] and sitemap[u] and sitemap[u] > db[u]["lastmod"]]
    backfill_pairs = [(u, sitemap[u]) for u in both if not db[u]["lastmod"] and sitemap[u]]

    log("─" * 56)
    log(f"[delta] YENİ={len(new_urls)}  DƏYİŞƏN(namizəd)={len(changed_candidates)}  "
        f"SİLİNƏN={len(deleted_urls)}  baseline-backfill={len(backfill_pairs)}")
    log("─" * 56)

    if dry:
        for u in list(new_urls)[:3]:
            log(f"  + yeni:    {u}")
        for u in list(deleted_urls)[:3]:
            log(f"  - silinən: {u}")
        log("[dry-run] heç nə yazılmadı. Real sinxron üçün: --postgres --qdrant")
        return

    # 2) Yeni + dəyişən namizədləri yığ
    to_scrape = [(u, sitemap[u]) for u in new_urls] + [(u, sitemap[u]) for u in changed_candidates]
    if args.limit:
        to_scrape = to_scrape[: args.limit]
    log(f"[scrape] {len(to_scrape)} məhsul yığılır...")
    records = scrape_urls(session, to_scrape, args.workers, args.delay)

    # 3) Həqiqətən dəyişənləri ayır (hash müqayisəsi ilə) — köhnə Qdrant nöqtələrini sil
    changed_skus = []
    for r in records:
        old = db.get(r["url"])
        if old and old.get("content_hash") and old["content_hash"] != r.get("content_hash"):
            changed_skus.append(r["sku"] or r["url"])
    if args.qdrant and changed_skus:
        qdrant_delete_skus(changed_skus)   # köhnə versiyanı sil ki, INSERT dublikat yaratmasın

    # 4) Postgres upsert (needs_embedding hash fərqinə görə avtomatik təyin olunur)
    upsert_products(conn, records)
    bf = backfill_lastmod(conn, backfill_pairs)

    # 5) Silinənləri təmizlə
    removed_skus = remove_deleted(conn, list(deleted_urls), args.qdrant)

    # 6) Hesabat
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM products WHERE needs_embedding AND NOT deleted;")
    pending = cur.fetchone()[0]
    cur.close()
    conn.close()

    log("═" * 56)
    log(f"[done] yığıldı={len(records)}  həqiqətən-dəyişən={len(changed_skus)}  "
        f"silindi={len(removed_skus)}  baseline={bf}")
    log(f"[done] Qdrant-a embed gözləyən (needs_embedding): {pending}")
    log("       → İndi n8n '03-Planlı Yenilənmə' workflow-u bunları embed edəcək.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n[stop] dayandırıldı.")
        sys.exit(130)
