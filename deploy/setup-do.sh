#!/usr/bin/env bash
# DigitalOcean droplet (Ubuntu 22.04/24.04, 1GB) üçün ilkin quraşdırma.
# İşə salma:  bash deploy/setup-do.sh
#
# Nə edir: Docker + Compose qurur, 2GB SWAP yaradır (1GB RAM üçün vacib),
#          firewall-da 22/80/443 açır.
set -euo pipefail

echo "==> [1/4] Sistem yenilənir + Docker qurulur"
sudo apt-get update -y
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" || true
sudo systemctl enable --now docker

echo "==> [2/4] 2GB SWAP yaradılır (1GB RAM-da OOM-un qarşısını alır)"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  # Swap-ı daha az aqressiv istifadə et (RAM-a üstünlük)
  sudo sysctl vm.swappiness=10
  grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
else
  echo "    swap artıq var, atlanır"
fi

echo "==> [3/4] Firewall (22/80/443)"
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

echo "==> [4/4] Python paketləri (data yükləmə + sync üçün)"
# Ubuntu 24.04 PEP 668 səbəbi ilə pip sistemə quraşdırmır → apt paketləri işlədirik
sudo apt-get install -y python3-requests python3-psycopg2

echo ""
echo "✓ Hazırdır. Yaddaş vəziyyəti:"
free -h
echo ""
echo "  1) 'docker' qrupu üçün bir dəfə çıxıb yenidən giriş edin (və ya: newgrp docker)."
echo "  2) deploy/.env doldurun → stack-i qaldırın (bax: deploy/README-do.md)."
