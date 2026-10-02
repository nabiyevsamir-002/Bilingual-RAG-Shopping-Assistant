# Deployment — DigitalOcean (nazik / $5 kredit)

Alinino RAG-i kiçik 1 GB droplet-də işlətmək. Taktika: Qdrant-ı **diskə + int8
kvantlaşdırmaya** keçiririk (RAM ~142 MB) + **2 GB swap**. Model dəyişmir (3072).

**Xərc:** $6/ay droplet (1 GB) → $5 kredit təxminən **3–4 həftə** çatır. Demo/pilot üçün.

---

## 0. Arxitektura
```
İnternet → Caddy (80/443, HTTPS) → n8n:5678 (daxili)
                                    ├── postgres  (127.0.0.1)
                                    └── qdrant    (127.0.0.1, disk + int8)
```

## 1. Droplet yarat
DigitalOcean → **Create → Droplets**:
- **Image:** Ubuntu 24.04
- **Plan:** Basic → Regular → **$6/ay (1 GB / 1 vCPU / 25 GB)**
  *(512 MB seçmə — n8n üçün azdır.)*
- **SSH açarı** əlavə et
- Public IP-ni qeyd et

> DO-da portlar defolt açıqdır (Oracle kimi əlavə VCN addımı yoxdur). `setup-do.sh` ufw qurur.

## 2. DNS
Domenin (və ya pulsuz **DuckDNS**) → `A` record → droplet-in public IP-si.

## 3. Faylları köçür (lokal Mac-dən)
Hazır arxivi göndər:
```bash
scp "/Users/samirnbiyev/Projects/alinino-rag-deploy.tar.gz" root@DROPLET_IP:~/
```
Serverə gir və aç:
```bash
ssh root@DROPLET_IP
```
```bash
tar xzf alinino-rag-deploy.tar.gz && cd Alinino.az-RAG && bash deploy/setup-do.sh
```
*(DO-da defolt istifadəçi `root`-dur. setup bitəndə `free -h` swap-ı göstərməlidir.)*

## 4. Sirləri doldur
```bash
cd ~/Alinino.az-RAG && cp deploy/.env.example deploy/.env
```
`deploy/.env` → `DOMAIN`, `N8N_ENCRYPTION_KEY` (`openssl rand -hex 24`),
`POSTGRES_PASSWORD` (`openssl rand -hex 16`).

## 5. Stack-i qaldır
```bash
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d
```

## 6. Qdrant kolleksiyası — NAZİK rejim (vacib!)
```bash
ON_DISK=true QUANTIZE=true ./create_qdrant_collection.sh
```

## 7. Datanı yüklə (yenidən scrape etmədən)
```bash
cd ~/Alinino.az-RAG/scraper
export PG_DSN="postgresql://n8n:SƏNİN_POSTGRES_PASSWORD@127.0.0.1:5432/alinino"
python3 load_products.py
```

## 8. n8n (brauzerdə `https://domenin`)
1. Owner hesabı yarat
2. 3 kredensial: **Gemini** (açar) · **Qdrant** (`http://qdrant:6333`, açar boş) ·
   **Postgres** (host `postgres`, DB `alinino`, user `n8n`, şifrə = .env-dəki `POSTGRES_PASSWORD`)
3. `01`, `02`, `03` import et → kredensialları bağla

## 9. İndeksləmə — 1 GB üçün BATCH ilə (təhlükəsiz)
47K-nı birdən embed etmək kiçik qutunu yükləyə bilər. Ona görə **hissə-hissə**:

`01` workflow-unun **«Postgres — məhsulları oxu»** node-unda sorğunu müvəqqəti dəyiş:
```sql
SELECT sku, ad, qiymet, valyuta, stok, kateqoriya, tesvir, dil, url, embed_text
FROM products WHERE needs_embedding = true AND deleted = false
LIMIT 8000;
```
Sonra `01`-i **6–7 dəfə** işə sal (hər dəfə 8000 embed olunub `needs_embedding=false` olur,
növbəti dəfə sonrakı 8000 gəlir). Yoxla:
```bash
curl -s http://localhost:6333/collections/alinino_products | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['points_count'])"
```
`points_count` 48480-ə çatanda bitib. (İstəsən LIMIT-i silib normal saxla.)

> RAM-ı izlə: `free -h` və `docker stats`. Swap işə düşsə də normaldır.

## 10. Test + Faza 2
- `02-Chat`-ı sına (AZ + RU).
- `03`-ü **Active** et + cron (bax: əsas README, Addım 8; PG_DSN-i güclü şifrə ilə).

---

## Baxım
- `docker compose -f deploy/docker-compose.prod.yml logs -f n8n`
- Backup: `docker compose -f deploy/docker-compose.prod.yml exec -T postgres pg_dump -U n8n alinino | gzip > ~/backup-$(date +%F).sql.gz`
- **Kredit bitəndə:** daha çox trafik/məhsul olsa $12 (2 GB) droplet-ə keç, və ya
  Hetzner CX22 (~€4/ay, 4 GB) — daha çox baş-boşluq.
