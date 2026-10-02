# Deployment — Oracle Cloud Always Free (production)

Alinino RAG sistemini əbədi pulsuz Oracle serverində HTTPS ilə yerləşdirmək.
Nəticə: `https://sənin-domenin` ünvanında canlı n8n + chatbot.

**Niyə Oracle:** Always Free tier 4 ARM core / 24 GB RAM verir — tam 48K@3072
indeksi rahat daşıyır, xərc €0. (Bütün Docker image-ləri ARM64 dəstəkləyir.)

---

## 0. Arxitektura (təhlükəsizlik)

```
İnternet → Caddy (80/443, HTTPS) → n8n:5678 (yalnız daxili)
                                     ├── postgres  (yalnız 127.0.0.1)
                                     └── qdrant    (yalnız 127.0.0.1)
```
Yalnız 80/443 açıqdır. Postgres/Qdrant internetdən görünmür.

---

## 1. Oracle instansiyası yarat

1. cloud.oracle.com → **Compute → Instances → Create Instance**
2. **Image:** Ubuntu 22.04 (və ya 24.04)
3. **Shape:** `VM.Standard.A1.Flex` (Ampere ARM, Always Free).
   Başlanğıc üçün **1–2 OCPU / 6–12 GB** kifayətdir (48K@3072 üçün 6 GB bəs edir)
   və tutum tapmaq daha asandır. *(«Out of capacity» çıxsa, başqa Availability
   Domain və ya region seç.)*
4. **SSH açarı** əlavə et (öz public açarını yüklə).
5. Yaradıldıqdan sonra **Public IP**-ni qeyd et.

## 2. VCN Firewall (Oracle konsolu) — VACİB

Instances → instansiyaya klik → **Virtual Cloud Network → Security Lists →
Default Security List → Add Ingress Rules**:

| Source CIDR | Protokol | Dest Port |
|-------------|----------|-----------|
| 0.0.0.0/0 | TCP | 80 |
| 0.0.0.0/0 | TCP | 443 |

*(22 artıq açıqdır. Bu addım olmadan sayt açılmayacaq.)*

## 3. DNS — domeni IP-yə yönəlt

- Öz domenin varsa: `A` record → serverin public IP-si.
- Pulsuz variant: **DuckDNS** (duckdns.org) → subdomen yarat (məs.
  `alinino-demo.duckdns.org`) → IP-ni ora yaz.

## 4. Faylları serverə köçür (lokal Mac-dən)

```bash
scp -r "/Users/samirnbiyev/Projects/Alinino.az-RAG" ubuntu@SERVER_IP:~/
```

## 5. Serverdə quraşdırma

```bash
ssh ubuntu@SERVER_IP
cd ~/Alinino.az-RAG
bash deploy/setup-oracle.sh
```
Bitəndə bir dəfə çıxıb yenidən gir (docker qrupu üçün): `exit` → `ssh ubuntu@SERVER_IP`

## 6. Sirləri doldur

```bash
cd ~/Alinino.az-RAG
cp deploy/.env.example deploy/.env
```
`deploy/.env`-i redaktə et:
- `DOMAIN` = sənin domenin
- `N8N_ENCRYPTION_KEY` = `openssl rand -hex 24` nəticəsi
- `POSTGRES_PASSWORD` = `openssl rand -hex 16` nəticəsi

## 7. Stack-i qaldır

```bash
cd ~/Alinino.az-RAG
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d
```
Bir-iki dəqiqə sonra Caddy avtomatik HTTPS sertifikatı alır.

## 8. Qdrant kolleksiyası (3072)

```bash
cd ~/Alinino.az-RAG
./create_qdrant_collection.sh
```

## 9. Datanı yüklə (yenidən scrape etmədən)

Lokalda yığdığın `products.jsonl` artıq serverə köçürülüb (Addım 4). Postgres-ə yüklə:

```bash
cd ~/Alinino.az-RAG/scraper
export PG_DSN="postgresql://n8n:SƏNİN_POSTGRES_PASSWORD@127.0.0.1:5432/alinino"
python3 load_products.py
```

## 10. n8n-i qur (brauzerdə)

`https://sənin-domenin` aç:
1. Owner hesabı yarat (lokal, pulsuz)
2. **3 kredensial** (Credentials → Add):
   - **Google Gemini** → API açarın
   - **Qdrant** → URL `http://qdrant:6333`, açar boş
   - **Postgres** → Host `postgres`, DB `alinino`, User `n8n`, **Şifrə = .env-dəki POSTGRES_PASSWORD**, Port `5432`
3. `n8n/01`, `02`, `03` workflow-larını import et → node-lara kredensialları bağla
4. **01**-i işə sal → Qdrant-a embed olunur. Yoxla:
   ```bash
   curl -s http://localhost:6333/collections/alinino_products | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['points_count'])"
   ```
5. **02-Chat**-ı sına (AZ + RU sualları)

## 11. Faza 2 — avtomatik yenilənmə

- `03` workflow-unu **Active** et.
- Host cron (hər 6 saat):
  ```bash
  ( crontab -l 2>/dev/null; echo "0 */6 * * * cd ~/Alinino.az-RAG/scraper && PG_DSN='postgresql://n8n:SƏNİN_ŞİFRƏ@127.0.0.1:5432/alinino' /usr/bin/python3 sync.py --postgres --qdrant >> ~/sync.log 2>&1" ) | crontab -
  ```

---

## Baxım və backup

- **Loglar:** `docker compose -f deploy/docker-compose.prod.yml logs -f n8n`
- **Yenidən başlatma:** `docker compose -f deploy/docker-compose.prod.yml restart`
- **Backup (Postgres):**
  ```bash
  docker compose -f deploy/docker-compose.prod.yml exec -T postgres pg_dump -U n8n alinino | gzip > ~/backup-$(date +%F).sql.gz
  ```
- **Qdrant snapshot:** `curl -X POST http://localhost:6333/collections/alinino_products/snapshots`

## Xərc

Oracle Always Free = **€0/ay** (əbədi). Domen öz domenindirsə illik ~€10,
DuckDNS istifadə etsən €0. Gemini pulsuz tier. **Cəmi ≈ €0.**
