#!/usr/bin/env bash
# Generate a locally-trusted TLS cert for HomelabHQ using mkcert, so web push
# and PWA install work without browser warnings.
#
# Usage:
#   ./scripts/setup-mkcert.sh [host-or-ip ...]
#   e.g. ./scripts/setup-mkcert.sh 192.168.1.10 homelabhq.lan
#
# With no args it uses localhost/127.0.0.1 and tries to detect your LAN IP.
set -euo pipefail

if ! command -v mkcert >/dev/null 2>&1; then
  cat <<'EOF'
mkcert not found. Install it first:
  macOS:          brew install mkcert nss
  Debian/Ubuntu:  sudo apt install -y libnss3-tools
                  curl -JLO "https://dl.filippo.io/mkcert/latest?for=linux/amd64"
                  chmod +x mkcert-v*-linux-amd64
                  sudo mv mkcert-v*-linux-amd64 /usr/local/bin/mkcert
EOF
  exit 1
fi

CERT_DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$CERT_DIR"

# SAN entries for the cert. Always include loopback; add any args, else detect.
NAMES=(localhost 127.0.0.1 "$@")
if [ "$#" -eq 0 ]; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  [ -z "${IP:-}" ] && IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [ -n "${IP:-}" ]; then
    NAMES+=("$IP")
    echo "No hosts given; detected LAN IP $IP (pass hostnames/IPs as args to add more)."
  fi
fi

echo "Installing mkcert local CA (may prompt for sudo / keychain password)…"
mkcert -install

echo "Issuing cert for: ${NAMES[*]}"
mkcert -cert-file "$CERT_DIR/nm.crt" -key-file "$CERT_DIR/nm.key" "${NAMES[@]}"

cat <<EOF

✓ Cert written to:
    $CERT_DIR/nm.crt
    $CERT_DIR/nm.key

Next:
  1. In docker-compose.yml, uncomment the certs mount:  - ./certs:/certs:ro
  2. docker compose up -d --build
  3. Open https://<one of the names above>:8770 — no warning on this machine.

Trust it on phones / other devices (install the mkcert root CA):
    $(mkcert -CAROOT)/rootCA.pem
  iOS:     AirDrop/email rootCA.pem → install profile → Settings ▸ General ▸
           About ▸ Certificate Trust Settings → enable full trust.
  Android: Settings ▸ Security ▸ Encryption & credentials ▸ Install a certificate
           ▸ CA certificate.
EOF
