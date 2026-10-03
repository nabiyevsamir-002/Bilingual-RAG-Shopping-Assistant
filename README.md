# Alinino RAG — Faza 1: Scraper + n8n Workflow

Alinino.az kataloqu üçün RAG chatbot prototipinin kodu və quraşdırma təlimatı. Repozitoriya işlək sistemin istehsal mühitində yayımlandığını və ya bütün kataloqun indeksləndiyini təsdiqləmir.
Bu paket üç hissədən ibarətdir:

| Hissə | Fayl | Nə edir |
|-------|------|---------|
| **Scraper** | `scraper/scraper.py` | Məhsulları toplamaq, təmizləmək və Postgres-ə yazmaq üçün nəzərdə tutulub; emal olunan say ayrıca ölçülməlidir |
| **Sinxronizator** | `scraper/sync.py` | Yalnız dəyişənləri tapıb yeniləyir (delta) |
| **İnfrastruktur** | `docker-compose.yml` | n8n + Postgres + Qdrant-ı bir əmrlə qaldırır |
| **Workflow-lar** | `n8n/*.json` | İndeksləmə + Chat + Planlı yenilənmə |

Arxitektura: `Scraper → PostgreSQL → n8n (embed) → Qdrant` və
`İstifadəçi → n8n AI Agent → Qdrant → Gemini → cavab`.

---

## 0. Tələblər

- **Docker** və **Docker Compose**
- **Python 3.10+**
- **Google Gemini API açarı** — pulsuz: <https://aistudio.google.com/apikey>

---

## Hazırkı vəziyyət və yoxlama meyarı

Bu repozitoriyada scraper, delta sinxronizatoru, Docker Compose konfiqurasiyası və n8n workflow JSON faylları var. İctimai repozitoriyada tamamlanmış crawl nəticəsi, Qdrant indeksi, işə salma jurnalı və ya sual-cavab keyfiyyəti ölçümü yoxdur. Buna görə **48k+ indekslənmiş məhsul**, istehsalda işləyən chatbot və ölçülmüş nəticə iddiası edilmir.

Yoxlamaq üçün aşağıdakı demo quraşdırmasını işə salın, Postgres-də yazılan sətirlərin və Qdrant-da indekslənən obyektlərin sayını ölçün, sonra Azərbaycan və rus dillərində bir neçə sualı mənbə keçidləri ilə yoxlayın. Nəticələri yalnız həmin ölçmələrdən sonra qeyd edin. Workflow-ların importu, credentials və model bağlantısı ayrıca qurulmalıdır.

## 1. İnfrastrukturu qaldır

```bash
cd alinino-rag
docker compose up -d
```

- n8n → <http://localhost:5678>
- Qdrant dashboard → <http://localhost:6333/dashboard>

---

## 2. Məhsulları yığ (Scraper)

```bash
cd scraper
pip install -r requirements.txt
pip install psycopg2-binary          # Postgres-ə yazmaq üçün

# Demo üçün 800 məhsul (Postgres-ə də yazır):
python scraper.py --limit 800 --postgres

# Tam kataloqu toplamaq üçün (müddət və məhsul sayı mənbədən asılıdır):
python scraper.py --postgres
```

Çıxış: `products.jsonl`, `products.json` və Postgres-də `products` cədvəli.
Prosesi dayandırsanız, təkrar işə salında **qaldığı yerdən davam edir**.

> **Qeyd:** `--postgres` işlətməsəniz, data yalnız JSON fayllarına yazılır.
> İndeksləmə workflow-u Postgres-dən oxuyur, ona görə demo üçün `--postgres` tövsiyə olunur.

---

## 3. Qdrant kolleksiyasını yarat

Standart embedding modeli `gemini-embedding-001` → **3072 ölçü**, Cosine
(n8n Gemini node-unun default-u budur, ona görə standart bunu tutur).

```bash
cd ..
chmod +x create_qdrant_collection.sh
./create_qdrant_collection.sh
```

> **Daha ucuz variant (768 ölçü):** `SIZE=768 ./create_qdrant_collection.sh`
> və hər iki workflow-un `Embeddings Google Gemini` node-unda modeli AÇIQ
> şəkildə `models/text-embedding-004` seçin. Yaddaşa 4× qənaət, keyfiyyət
> bir az aşağı. **Vacib:** kolleksiya ölçüsü ilə node modeli həmişə uyğun olmalıdır.

---

## 4. n8n-də kredensialları qur

<http://localhost:5678> aç, sahib hesabı yarat, sonra **3 kredensial** əlavə et
(*Credentials → Add*):

| Kredensial | Tip | Dəyərlər |
|-----------|-----|----------|
| **Google Gemini API** | `Google Gemini(PaLM) API` | API açarını yapışdır |
| **Qdrant API** | `Qdrant API` | URL: `http://qdrant:6333` · açar: boş (lokal) |
| **Postgres** | `Postgres` | Host: `postgres` · DB: `alinino` · İstifadəçi: `n8n` · Şifrə: `n8n` · Port: `5432` |

> ⚠️ **Vacib:** n8n Docker konteynerinin içindən baxıldığı üçün host adları
> `qdrant` və `postgres`-dir (`localhost` DEYİL). Scraper isə host maşından
> işlədiyi üçün `localhost` istifadə edir.

---

## 5. Workflow-ları import et

*Workflows → Import from File*:

1. `n8n/01-ingestion-workflow.json` — **İndeksləmə** (ilkin tam yükləmə)
2. `n8n/02-chat-rag-workflow.json` — **Sual-Cavab (RAG)**
3. `n8n/03-scheduled-sync-workflow.json` — **Planlı Yenilənmə** (Faza 2, aşağı bax)

Hər workflow-da qırmızı işarəli node-lara klik edib **kredensialları seç**
(import zamanı avtomatik bağlanmır).

### İndekslə
`01-İndeksləmə` workflow-unu aç → **Test workflow** (və ya Execute) düyməsi.
Bütün məhsullar vektorlaşıb Qdrant-a yazılacaq. Yoxlama:

```bash
curl http://localhost:6333/collections/alinino_products | python3 -m json.tool
```

`points_count` məhsul sayına bərabər olmalıdır.

### Söhbət et
`02-Chat` workflow-unu aç → aşağıdakı **Chat** düyməsi ilə test et:

- «20 manatdan ucuz uşaq kitabı varmı?»
- «есть ли книги про космос?»
- «kitab lampası neçəyədir?»

---

## 6. Import sonrası yoxlama siyahısı

n8n versiyaları arasında kiçik fərqlər ola bilər. Import-dan sonra:

- [ ] Hər AI node-da **kredensial** seçilib
- [ ] Chat və embedding node-larında **model** dropdown-u doludur
      (boşdursa, əl ilə seç: `models/gemini-2.5-flash`, `models/gemini-embedding-001`)
- [ ] Embedding modeli **hər iki** workflow-da eynidir
- [ ] Qdrant kolleksiyasının ölçüsü embedding ölçüsünə uyğundur (3072 = gemini-embedding-001)

---

## 7. Tez-tez rast gəlinən problemlər

| Problem | Səbəb / Həll |
|---------|--------------|
| `Wrong vector dimension` | Kolleksiya ölçüsü ≠ embedding ölçüsü. Kolleksiyanı düzgün SIZE ilə yenidən yarat. |
| Qdrant node «connection refused» | Host `localhost` yox, `http://qdrant:6333` olmalıdır. |
| Postgres node qoşulmur | Host `postgres`, konteynerlər eyni şəbəkədədir. |
| Gemini «quota exceeded» | Pulsuz limit (250 sorğu/gün). Bir az gözlə və ya ödənişli tier / self-host bge-m3. |
| Chat cavab vermir | `01-İndeksləmə` işləyibmi? Qdrant-da `points_count > 0` olmalıdır. |
| Scraper bloklanır | `--delay` dəyərini artır (məs. `--delay 1.5`), `--workers` azalt. |

---

## 8. Faza 2 — Avtomatik Yenilənmə (delta sinxronizasiya)

Bilik bazasını canlı saxlayan iki komponent:

- **`scraper/sync.py`** (host) — sitemap-dakı `<lastmod>` + məzmun hash-ı ilə
  **yeni / dəyişən / silinən** məhsulları tapır. Bütün 48k-nı yenidən yığmır —
  yalnız fərqi. Postgres-i işarələyir (`needs_embedding=true`), silinənləri
  Qdrant-dan çıxarır.
- **`n8n/03-scheduled-sync-workflow.json`** — `Schedule Trigger` (hər 6 saat)
  ilə yalnız işarələnmiş (`needs_embedding=true`) məhsulları embed edib Qdrant-a
  yazır və işarəni təmizləyir.

### Necə işləyir

```
sync.py (host, cron)  →  Postgres flag-ları  →  n8n 03 (embed)  →  Qdrant
   ↑ sitemap fərqi                                   ↑ hər 6 saat
```

### Quraşdırma

**1.** Əvvəlcə tam bazanı yığın (bir dəfə):
```bash
python scraper.py --postgres
```

**2.** `sync.py`-ı test edin (heç nə yazmadan fərqi göstərir):
```bash
python sync.py --dry-run
```

**3.** Real sinxronu host cron-a əlavə edin (hər 6 saat):
```bash
# crontab -e
0 */6 * * * cd /path/to/Alinino.az-RAG/scraper && /usr/bin/python3 sync.py --postgres --qdrant >> sync.log 2>&1
```

**4.** n8n-də `03-scheduled-sync` workflow-unu import edin, kredensialları qoşun
və yuxarı sağdan **Active** edin.

### Qeydlər / yoxlama nöqtələri

- `sync.py --qdrant` köhnə/silinən nöqtələri Qdrant REST API ilə silir. Qdrant
  payload-da sku açarının adını yoxlayın (dashboard-da bir nöqtəyə baxın); adətən
  `metadata.sku`-dur. Fərqlidirsə: `QDRANT_SKU_KEY=... python sync.py ...`.
- İlk `sync` çalışması mövcud məhsullara yalnız **lastmod baseline** yazır
  (yenidən yığmadan); sonrakılar həqiqi dəyişiklikləri aşkarlayır.
- `sync.py` scraper.py ilə eyni kodu (parse, dil aşkarlama) təkrar istifadə edir.

---

## 9. Növbəti addımlar (Faza 3+)

- **Metadata filtrləri:** qiymət/kateqoriya üzrə dəqiq + semantik axtarış
- **Reranker:** nəticə dəqiqliyini artırmaq üçün Cohere Rerank
- **Sayta widget:** Chat Trigger-in embed kodunu Alinino saytına yerləşdir
- **Keyfiyyət:** qızıl test dəsti ilə avtomatik retrieval accuracy ölçmə
- **Rəsmi feed:** Alinino-dan API/webhook → scraping tamamilə aradan qalxır

Tam memarlıq və xərc planı üçün əsas təqdimat sənədinə bax.
