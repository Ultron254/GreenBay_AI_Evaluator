#!/bin/bash
# ===========================================================================
# GreenBay AI Evaluator — EC2 Server Setup Script
# Run once on a fresh Ubuntu 22.04 EC2 instance
# Usage: chmod +x setup-server.sh && sudo ./setup-server.sh
# ===========================================================================
set -euo pipefail

echo "=========================================="
echo "  GreenBay AI Evaluator — Server Setup"
echo "=========================================="

# --- 1. System Updates ---
echo "[1/7] Updating system packages..."
apt-get update -y && apt-get upgrade -y

# --- 2. Install Docker ---
echo "[2/7] Installing Docker..."
apt-get install -y ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add ubuntu user to docker group
usermod -aG docker ubuntu

echo "Docker version: $(docker --version)"
echo "Docker Compose version: $(docker compose version)"

# --- 3. Install Git ---
echo "[3/7] Installing Git..."
apt-get install -y git

# --- 4. Configure Firewall (UFW) ---
echo "[4/7] Configuring firewall..."
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP
ufw allow 443/tcp   # HTTPS
ufw --force enable
echo "Firewall rules:"
ufw status verbose

# --- 5. Configure SSH Hardening ---
echo "[5/7] Hardening SSH..."
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart sshd

# --- 6. Create App Directory ---
echo "[6/7] Setting up app directory..."
mkdir -p /opt/greenbay
chown ubuntu:ubuntu /opt/greenbay

# --- 7. Set up automatic security updates ---
echo "[7/7] Configuring automatic security updates..."
apt-get install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades

echo ""
echo "=========================================="
echo "  ✅ Server setup complete!"
echo "=========================================="
echo ""
echo "NEXT STEPS (run as 'ubuntu' user):"
echo ""
echo "  1. Clone the repo:"
echo "     cd /opt/greenbay"
echo "     git clone https://github.com/Ultron254/GreenBay_AI_Evaluator.git ."
echo ""
echo "  2. Copy and edit .env:"
echo "     cp .env.example .env"
echo "     nano .env"
echo ""
echo "  3. Generate self-signed SSL (for IP-based HTTPS):"
echo "     mkdir -p nginx/ssl"
echo "     openssl req -x509 -nodes -days 365 -newkey rsa:2048 \\"
echo "       -keyout nginx/ssl/server.key \\"
echo "       -out nginx/ssl/server.crt \\"
echo "       -subj '/CN=greenbay/O=GreenBay/C=KE'"
echo ""
echo "  4. Start all services:"
echo "     docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
echo ""
echo "  5. Check status:"
echo "     docker compose ps"
echo "     curl http://localhost:9100/health"
echo ""
