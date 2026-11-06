#!/usr/bin/env bash
# Host the 365 Health Check from an Android tablet or phone (Termux) and share it on the internet
# through a Cloudflare Quick Tunnel: a temporary https://....trycloudflare.com link, no account needed.
#
#   bash scripts/termux-host.sh                         # anyone with the link can open it
#   bash scripts/termux-host.sh you@company.com         # only these emails can open it (one-time PIN)
#   bash scripts/termux-host.sh 'a@x.com,b@y.com'       # several people, or '*@company.com'
#
# The link changes every time you start it. Quick Tunnels are for demos and testing, not customers.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

if ! python -c "import fastapi, uvicorn, jinja2, msal, dns, yaml" 2>/dev/null; then
  cat <<'EOF'
The app's Python packages aren't installed yet. Run these once (it can take 10-30 minutes), then try again:

  pkg install -y python rust clang binutils python-cryptography
  export ANDROID_API_LEVEL=$(getprop ro.build.version.sdk)
  pip install fastapi uvicorn jinja2 itsdangerous python-multipart msal requests dnspython pyyaml
EOF
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Installing cloudflared (Cloudflare's tunnel program)..."
  pkg install -y cloudflared
fi

# Keep a stable session key between restarts, so people stay signed in.
if [ -z "${TVX_SESSION_SECRET:-}" ]; then
  [ -s data/.session-secret ] || { mkdir -p data; python -c "import secrets; print(secrets.token_urlsafe(48))" > data/.session-secret; }
  TVX_SESSION_SECRET="$(cat data/.session-secret)"
  export TVX_SESSION_SECRET
fi

# Keep the CPU awake while hosting, so Android doesn't pause the app when the screen turns off.
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  --proxy-headers --forwarded-allow-ips 127.0.0.1 > app.log 2>&1 &
APP_PID=$!

cleanup() {
  kill "$APP_PID" 2>/dev/null || true
  command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
  echo "Stopped."
}
trap cleanup EXIT

app_ready() {
  python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=2)" 2>/dev/null
}

echo "Starting the app..."
for _ in $(seq 1 60); do
  app_ready && break
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "The app stopped while starting. Last lines of app.log:"
    tail -n 20 app.log
    exit 1
  fi
  sleep 1
done
app_ready || { echo "The app didn't start within a minute. See app.log."; exit 1; }

echo
echo "The app is running on this device: http://localhost:$PORT"
echo "Now opening a public link through Cloudflare. Look for the https://....trycloudflare.com address below."
echo "Keep Termux open. Press CTRL+C to stop hosting."
echo

ARGS=(tunnel --no-autoupdate --protocol http2 --url "http://127.0.0.1:$PORT")
if [ $# -gt 0 ]; then
  ARGS+=(--allowed-mail "$(IFS=,; echo "$*")")
fi
cloudflared "${ARGS[@]}"
