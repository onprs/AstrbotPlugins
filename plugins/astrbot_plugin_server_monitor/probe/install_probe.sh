#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/astrbot-server-probe"
SERVICE_NAME="astrbot-server-probe"
HOST="0.0.0.0"
PORT="9810"
TOKEN=""
LOCATION=""
REMARK=""
USER_NAME="root"

usage() {
  cat <<'EOF'
Usage:
  sudo bash install_probe.sh --token TOKEN [options]

Options:
  --token TOKEN       Probe token. Required unless --allow-empty-token is used.
  --location TEXT     Location shown in AstrBot, for example Singapore.
  --remark TEXT       Remark shown in AstrBot, for example API ingress.
  --port PORT         Listen port. Default: 9810.
  --host HOST         Listen host. Default: 0.0.0.0.
  --user USER         systemd service user. Default: root.
  --install-dir DIR   Install directory. Default: /opt/astrbot-server-probe.
  --allow-empty-token Install without token. Not recommended.
  -h, --help          Show this help.

Example:
  sudo bash install_probe.sh \
    --token 'change-me-long-random-token' \
    --location 'Singapore' \
    --remark 'API ingress' \
    --port 9810
EOF
}

allow_empty_token=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)
      TOKEN="${2:-}"
      shift 2
      ;;
    --location)
      LOCATION="${2:-}"
      shift 2
      ;;
    --remark)
      REMARK="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --user)
      USER_NAME="${2:-}"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="${2:-}"
      shift 2
      ;;
    --allow-empty-token)
      allow_empty_token=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root, for example: sudo bash install_probe.sh --token TOKEN" >&2
  exit 1
fi

if [[ -z "$TOKEN" && "$allow_empty_token" -ne 1 ]]; then
  echo "--token is required. Use a long random token." >&2
  echo "For example: openssl rand -hex 24" >&2
  exit 1
fi

case "$PORT" in
  ''|*[!0-9]*)
    echo "--port must be a number." >&2
    exit 1
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PROBE="$SCRIPT_DIR/linux_probe.py"
if [[ ! -f "$SOURCE_PROBE" ]]; then
  echo "linux_probe.py not found beside install_probe.sh: $SOURCE_PROBE" >&2
  exit 1
fi

systemd_env_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

HOST_ENV="$(systemd_env_value "$HOST")"
PORT_ENV="$(systemd_env_value "$PORT")"
TOKEN_ENV="$(systemd_env_value "$TOKEN")"
LOCATION_ENV="$(systemd_env_value "$LOCATION")"
REMARK_ENV="$(systemd_env_value "$REMARK")"
INSTALL_DIR_ENV="$(systemd_env_value "$INSTALL_DIR")"

install -d -m 0755 "$INSTALL_DIR"
install -m 0755 "$SOURCE_PROBE" "$INSTALL_DIR/linux_probe.py"

cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=AstrBot Server Monitor Probe
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="ASTRBOT_PROBE_HOST=${HOST_ENV}"
Environment="ASTRBOT_PROBE_PORT=${PORT_ENV}"
Environment="ASTRBOT_PROBE_TOKEN=${TOKEN_ENV}"
Environment="ASTRBOT_PROBE_LOCATION=${LOCATION_ENV}"
Environment="ASTRBOT_PROBE_REMARK=${REMARK_ENV}"
ExecStart=/usr/bin/python3 ${INSTALL_DIR_ENV}/linux_probe.py
Restart=always
RestartSec=3
User=${USER_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "AstrBot server probe installed."
echo "Service: ${SERVICE_NAME}.service"
echo "Endpoint from AstrBot: http://<this-server-ip>:${PORT}/metrics"
if [[ -n "$TOKEN" ]]; then
  echo "Auth header: Authorization: Bearer ${TOKEN}"
fi
echo
echo "Check service:"
echo "  systemctl status ${SERVICE_NAME}.service --no-pager"
echo
echo "Local probe test:"
if [[ -n "$TOKEN" ]]; then
  echo "  curl -H 'Authorization: Bearer ${TOKEN}' http://127.0.0.1:${PORT}/metrics"
else
  echo "  curl http://127.0.0.1:${PORT}/metrics"
fi
