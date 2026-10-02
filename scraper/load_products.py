#!/usr/bin/env python3
"""
products.jsonl / products.json faylını Postgres-ə yükləyir — YENİDƏN YIĞMADAN.

Serverdə (Oracle) saytı təzədən scrape etmək əvəzinə, lokaldan köçürülmüş
məhsul faylını birbaşa Postgres-ə upsert etmək üçün.

İstifadə (serverdə):
  export PG_DSN="postgresql://n8n:GÜCLÜ_ŞİFRƏ@127.0.0.1:5432/alinino"
  python3 load_products.py

Sonra n8n-də `01-İndeksləmə` işə salınır → Qdrant-a embed olunur.
"""
from __future__ import annotations
import json
from scraper import upsert_postgres, JSONL_PATH, JSON_PATH, log


def main() -> None:
    path = JSONL_PATH if JSONL_PATH.exists() else JSON_PATH
    if not path.exists():
        log("[load] products.jsonl / products.json tapılmadı. Əvvəlcə faylı serverə köçürün.")
        return

    records = []
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    else:
        records = json.loads(path.read_text(encoding="utf-8"))

    log(f"[load] {path.name}-dan {len(records)} məhsul oxundu")
    if not records:
        return
    upsert_postgres(records)   # ON CONFLICT sku; needs_embedding avtomatik təyin olunur
    log("[load] tamamlandı. İndi n8n-də 01-İndeksləmə workflow-unu işə salın.")


if __name__ == "__main__":
    main()
