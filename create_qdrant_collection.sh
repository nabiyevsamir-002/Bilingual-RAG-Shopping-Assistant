#!/usr/bin/env bash
# Qdrant-da `alinino_products` kolleksiyasını yaradır.
# Vektor ölçüsü embedding modelinə UYĞUN olmalıdır:
#   • models/gemini-embedding-001 → 3072  (STANDART — n8n node default-u budur)
#   • models/text-embedding-004   → 768   (daha ucuz; SIZE=768 + node-da modeli açıq seç)
#
# İstifadə:
#   normal (lokal, 24GB RAM):   ./create_qdrant_collection.sh
#   nazik (DO 1GB, az RAM):     ON_DISK=true QUANTIZE=true ./create_qdrant_collection.sh
#
# ON_DISK=true   → vektorlar + HNSW indeksi diskdə saxlanır (RAM az).
# QUANTIZE=true  → int8 kvantlaşdırma; kiçik vektorlar RAM-da, orijinallar diskdə.
set -euo pipefail

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
COLLECTION="${COLLECTION:-alinino_products}"
SIZE="${SIZE:-3072}"
ON_DISK="${ON_DISK:-false}"
QUANTIZE="${QUANTIZE:-false}"

echo "→ Qdrant: $QDRANT_URL | kolleksiya: $COLLECTION | ölçü: $SIZE | on_disk=$ON_DISK | quantize=$QUANTIZE"

BODY=$(python3 - "$SIZE" "$ON_DISK" "$QUANTIZE" <<'PY'
import json, sys
size = int(sys.argv[1]); on_disk = sys.argv[2] == "true"; quant = sys.argv[3] == "true"
body = {
    "vectors": {"size": size, "distance": "Cosine", "on_disk": on_disk},
    "optimizers_config": {"default_segment_number": 2},
}
if on_disk:
    body["hnsw_config"] = {"on_disk": True}
if quant:
    body["quantization_config"] = {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}}
print(json.dumps(body))
PY
)

curl -sS -X PUT "$QDRANT_URL/collections/$COLLECTION" \
  -H 'Content-Type: application/json' -d "$BODY"

echo -e "\n✓ Hazırdır. Yoxlama:  curl $QDRANT_URL/collections/$COLLECTION"
