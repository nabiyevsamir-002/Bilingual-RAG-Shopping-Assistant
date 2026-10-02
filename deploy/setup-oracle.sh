#!/usr/bin/env bash
# Oracle Cloud (Ubuntu 22.04/24.04, ARM64) üçün ilkin quraşdırma.
# İşə salma:  bash deploy/setup-oracle.sh
#
# Nə edir: Docker + Compose qurur, firewall-da 80/443 açır, portları saxlayır.
# QEYD: Oracle VCN Security List-də də 80/443 ingress əl ilə açılmalıdır (bax README-deploy.md).
set -euo pipefail

echo "==> [1/4] Sistem yenilənir"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

echo "==> [2/4] Docker + Compose plugin qurulur"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" || true
sudo systemctl enable --now docker

echo "==> [3/4] Firewall: 80/443 açılır (Oracle Ubuntu default iptables REJECT-i keçmək üçün)"
sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
# Qaydaları reboot üçün saxla
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent netfilter-persistent
sudo netfilter-persistent save

echo "==> [4/4] Python paketləri (scraper/sync üçün) qurulur"
# Ubuntu 24.04 PEP 668 səbəbi ilə pip sistemə quraşdırmır → apt paketləri işlədirik
sudo apt-get install -y python3-requests python3-psycopg2

echo ""
echo "✓ Hazırdır."
echo "  1) 'docker' qrupu üçün bir dəfə çıxıb yenidən giriş edin (və ya: newgrp docker)."
echo "  2) deploy/.env faylını doldurun (DOMAIN, açarlar)."
echo "  3) Stack-i qaldırın:"
echo "       docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d"
echo ""
echo "  ⚠ Oracle veb konsolunda VCN → Security List → Ingress: 80 və 443 (0.0.0.0/0) açmağı UNUTMAYIN."
