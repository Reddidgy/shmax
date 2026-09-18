#!/usr/bin/env bash
# One-time setup for coturn TURN server on the Oracle VPS.
# Run as root: sudo bash scripts/coturn/setup_coturn.sh
set -euo pipefail

DOMAIN="shmax.praxisos.dev"
CONF_SRC="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="/var/log/coturn"

echo "=== Installing coturn ==="
apt-get update -qq
apt-get install -y coturn

echo "=== Creating turnserver system user ==="
if ! id -u turnserver &>/dev/null; then
    useradd -r -s /usr/sbin/nologin turnserver
fi

echo "=== Generating TURN credentials ==="
TURN_USER="shmax"
TURN_PASS="$(openssl rand -hex 24)"

echo "=== Detecting network IPs ==="
PUBLIC_IP="158.101.177.72"
PRIVATE_IP="$(ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)"
echo "  Public IP:  ${PUBLIC_IP}"
echo "  Private IP: ${PRIVATE_IP}"

echo "=== Granting turnserver read access to Let's Encrypt certs ==="
setfacl -m u:turnserver:rx /etc/letsencrypt/live
setfacl -m u:turnserver:rx /etc/letsencrypt/archive
setfacl -m u:turnserver:r /etc/letsencrypt/archive/${DOMAIN}/privkey*.pem

echo "=== Writing turnserver.conf ==="
sed -e "s|__DOMAIN__|${DOMAIN}|g" \
    -e "s|__TURN_USER__|${TURN_USER}|g" \
    -e "s|__TURN_PASS__|${TURN_PASS}|g" \
    -e "s|__PUBLIC_IP__|${PUBLIC_IP}|g" \
    -e "s|__PRIVATE_IP__|${PRIVATE_IP}|g" \
    "${CONF_SRC}/turnserver.conf" > /etc/turnserver.conf
chown root:turnserver /etc/turnserver.conf
chmod 640 /etc/turnserver.conf

echo "=== Setting up log directory ==="
mkdir -p "${LOGDIR}"
chown turnserver:turnserver "${LOGDIR}"

echo "=== Installing logrotate config ==="
cp "${CONF_SRC}/coturn-logrotate" /etc/logrotate.d/coturn

echo "=== Enabling coturn ==="
sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn

echo "=== Starting coturn ==="
systemctl daemon-reload
systemctl enable coturn
systemctl restart coturn
systemctl status coturn --no-pager

echo ""
echo "================================================"
echo "  coturn is running!"
echo "================================================"
echo ""
echo "  TURN credentials (save these for .env):"
echo "    TURN_SERVER_URL=${DOMAIN}"
echo "    TURN_SERVER_USERNAME=${TURN_USER}"
echo "    TURN_SERVER_CREDENTIAL=${TURN_PASS}"
echo ""
echo "  STUN_SERVERS='[\"stun:${DOMAIN}:3478\",\"stun:stun.l.google.com:19302\"]'"
echo ""
echo "  Add these to /home/ubuntu/git/shmax/.env on the VPS."
echo ""
echo "  REMAINING MANUAL STEPS:"
echo "  1. Open ports in Oracle Cloud Security List:"
echo "     - UDP 3478  (STUN + TURN)"
echo "     - TCP 3478  (TURN TCP)"
echo "     - TCP 5349  (TURNS TLS)"
echo "     - UDP 49152-50175 (relay range)"
echo "  2. Open the same ports in OS firewall (iptables):"
echo "     sudo iptables -I INPUT -p udp --dport 3478 -j ACCEPT"
echo "     sudo iptables -I INPUT -p tcp --dport 3478 -j ACCEPT"
echo "     sudo iptables -I INPUT -p tcp --dport 5349 -j ACCEPT"
echo "     sudo iptables -I INPUT -p udp --dport 49152:50175 -j ACCEPT"
echo "     sudo netfilter-persistent save"
echo "  3. Add the TURN env vars to /home/ubuntu/git/shmax/.env"
echo "  4. Restart the Shmax API (kill the API PID; fetcher restarts it)"
echo "  5. Verify: turnutils_uclient -T -u ${TURN_USER} -w ${TURN_PASS} ${DOMAIN}"
echo ""
echo "  NOTE: coturn reads the Let's Encrypt cert directly. After the cert"
echo "  renews, restart coturn if it doesn't pick up the new cert on its own:"
echo "    sudo systemctl restart coturn"
echo "================================================"
