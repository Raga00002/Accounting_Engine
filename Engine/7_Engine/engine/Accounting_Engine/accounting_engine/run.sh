#!/usr/bin/env bash
# Accounting Engine launcher for Linux (Ubuntu x86_64 / aarch64, Raspberry Pi, ARM servers) and macOS.
#
#   ./run.sh                 start on http://localhost:8501 (also reachable from the LAN)
#   AE_PORT=8600 ./run.sh    use another port
#   AE_HTTPS=1 ./run.sh      serve over https:// with a self-signed certificate
#                            (needed for camera + microphone when you open the app from ANOTHER
#                             device – browsers only allow mic/camera on https:// or localhost)
#   ./run.sh --check         only verify Python + packages, do not start
set -euo pipefail
cd "$(dirname "$0")"

PORT="${AE_PORT:-8501}"
ADDRESS="${AE_ADDRESS:-0.0.0.0}"     # 0.0.0.0 = reachable from phones/laptops on the same network
PY="${AE_PYTHON:-python3}"

say()  { printf '\n==> %s\n' "$*"; }
die()  { printf '\n!!  %s\n' "$*" >&2; exit 1; }

# --- 1. Python ----------------------------------------------------------------------------------
command -v "$PY" >/dev/null 2>&1 || die "python3 not found. Ubuntu/Debian:  sudo apt install python3 python3-venv python3-pip"
"$PY" - <<'EOF' || die "Python 3.10 or newer is required (found: $("$PY" --version 2>&1)). Ubuntu 22.04+ ships 3.10+."
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

# --- 2. Virtual environment (Ubuntu marks the system Python as externally managed) -------------
if [ ! -x ".venv/bin/python" ]; then
    say "Creating virtual environment (.venv)…"
    if ! "$PY" -m venv .venv 2>/tmp/ae_venv_err.txt; then
        cat /tmp/ae_venv_err.txt >&2
        die "Could not create the virtual environment. On Ubuntu/Debian run:  sudo apt install python3-venv   and try again."
    fi
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
fi

# --- 3. Packages (re-run if a previous install was interrupted) --------------------------------
if ! .venv/bin/python -c "import streamlit, anthropic, openpyxl, rapidfuzz, pydantic, pandas, PIL, speech_recognition, cryptography, authlib" 2>/dev/null; then
    say "Installing packages (all are prebuilt wheels for x86_64 and aarch64 – no compiler needed)…"
    .venv/bin/python -m pip install -r requirements.txt || die "Package installation failed – check the internet connection and run again."
fi

if [ "${1:-}" = "--check" ]; then
    .venv/bin/python - <<'EOF'
import platform, sys, streamlit, anthropic, pandas, PIL, rapidfuzz
print(f"OK  python {sys.version.split()[0]}  {platform.machine()}  streamlit {streamlit.__version__}  anthropic {anthropic.__version__}")
EOF
    exit 0
fi

# --- 4. Configuration ---------------------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    say "Created .env – open it, paste your ANTHROPIC_API_KEY and run ./run.sh again:"
    echo "    nano $(pwd)/.env"
    exit 1
fi

# --- 5. Optional HTTPS (self-signed) -----------------------------------------------------------
SSL_ARGS=()
if [ "${AE_HTTPS:-0}" = "1" ]; then
    mkdir -p certs
    if [ ! -f certs/cert.pem ] || [ ! -f certs/key.pem ]; then
        command -v openssl >/dev/null 2>&1 || die "openssl is needed to create the certificate:  sudo apt install openssl"
        HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
        say "Creating a self-signed certificate for $(hostname) ${HOST_IP:+/ $HOST_IP} (valid 10 years)…"
        openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
            -keyout certs/key.pem -out certs/cert.pem \
            -subj "/CN=$(hostname)" \
            -addext "subjectAltName=DNS:$(hostname),DNS:localhost,IP:127.0.0.1${HOST_IP:+,IP:$HOST_IP}" >/dev/null 2>&1
    fi
    SSL_ARGS=(--server.sslCertFile certs/cert.pem --server.sslKeyFile certs/key.pem)
    SCHEME=https
else
    SCHEME=http
fi

# --- 6. Start -----------------------------------------------------------------------------------
export STREAMLIT_SERVER_SHOW_EMAIL_PROMPT=false
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
export STREAMLIT_SERVER_HEADLESS=true            # never try to open a browser (works over SSH / headless boards)

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Accounting Engine starting"
echo "    on this machine : ${SCHEME}://localhost:${PORT}"
[ -n "$HOST_IP" ] && echo "    from the LAN    : ${SCHEME}://${HOST_IP}:${PORT}"
[ "$SCHEME" = "http" ] && echo "    (camera/mic from another device need HTTPS:  AE_HTTPS=1 ./run.sh)"
echo "    stop with Ctrl+C"

exec .venv/bin/python -m streamlit run app.py \
    --server.address "$ADDRESS" --server.port "$PORT" --server.headless true \
    ${SSL_ARGS[@]+"${SSL_ARGS[@]}"}
