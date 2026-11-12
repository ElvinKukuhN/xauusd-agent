#!/usr/bin/env bash
# ============================================================
# setup_vps.sh - Setup otomatis oanda-xauusd-agent di Ubuntu (22.04 / 24.04)
#
# Yang dipasang:
#   1. Wine (WineHQ stable) + Xvfb (layar virtual) + x11vnc
#   2. Terminal MetaTrader 5 di dalam Wine
#   3. Python-Windows (embeddable) + library MetaTrader5 di dalam Wine
#   4. Service systemd: mt5-xvfb dan mt5-terminal (MT5 selalu hidup)
#   5. Virtual environment Python Linux untuk bot + dependency
#   6. Instruksi (atau pemasangan otomatis) cronjob
#
# Pemakaian:
#   chmod +x setup_vps.sh
#   ./setup_vps.sh                 # setup lengkap + instruksi cron
#   ./setup_vps.sh --install-cron  # sama, sekaligus memasang cronjob
#
# Skrip aman dijalankan ulang: langkah yang sudah selesai akan dilewati.
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
LOG_DIR="${PROJECT_DIR}/logs"

RUN_USER="$(id -un)"
RUN_HOME="${HOME}"
export WINEPREFIX="${WINEPREFIX:-${RUN_HOME}/.mt5wine}"
export WINEARCH=win64
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="   # lewati dialog instalasi Mono/Gecko
export DISPLAY=":99"

MT5_SETUP_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
MT5_EXE="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"
PY_VERSION="3.10.11"
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip"
PY_WIN_DIR="${WINEPREFIX}/drive_c/mt5py"
PY_WIN_CMD="C:/mt5py/python.exe"
GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"

CRON_MARKER="# oanda-xauusd-agent"
# Menit ke-1 setiap jam, Senin-Jumat. Menit 1 (bukan 0) memberi waktu candle
# H1 yang baru tutup tercatat di terminal.
CRON_LINE="1 * * * 1-5 cd ${PROJECT_DIR} && ${PYTHON_BIN} main.py >> ${LOG_DIR}/cron.log 2>&1 ${CRON_MARKER}"

INSTALL_CRON=0
if [[ "${1:-}" == "--install-cron" ]]; then
    INSTALL_CRON=1
fi

if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
else
    SUDO="sudo"
fi

step() { echo; echo "==> $*"; }

echo "Direktori proyek : ${PROJECT_DIR}"
echo "User             : ${RUN_USER}"
echo "WINEPREFIX       : ${WINEPREFIX}"

# ---------- 1. Paket sistem + WineHQ ----------
# shellcheck disable=SC1091
. /etc/os-release
CODENAME="${VERSION_CODENAME:-}"
if [[ "${ID:-}" != "ubuntu" || -z "${CODENAME}" ]]; then
    echo "ERROR: skrip ini untuk Ubuntu. Terdeteksi: ${PRETTY_NAME:-tidak dikenal}" >&2
    exit 1
fi

step "Menginstal paket dasar"
export DEBIAN_FRONTEND=noninteractive
${SUDO} apt-get update -y
${SUDO} apt-get install -y python3 python3-venv python3-pip wget unzip xvfb x11vnc ca-certificates cron

if ! command -v wine >/dev/null 2>&1; then
    step "Menginstal Wine (WineHQ stable) untuk Ubuntu ${CODENAME}"
    ${SUDO} dpkg --add-architecture i386
    ${SUDO} mkdir -pm755 /etc/apt/keyrings
    ${SUDO} wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
    ${SUDO} wget -qNP /etc/apt/sources.list.d/ \
        "https://dl.winehq.org/wine-builds/ubuntu/dists/${CODENAME}/winehq-${CODENAME}.sources"
    ${SUDO} apt-get update -y
    ${SUDO} apt-get install -y --install-recommends winehq-stable
else
    step "Wine sudah terpasang: $(wine --version)"
fi
WINE_BIN="$(command -v wine)"
WINESERVER_BIN="$(command -v wineserver)"

# ---------- 2. Layar virtual (service mt5-xvfb) ----------
step "Menyiapkan service layar virtual (mt5-xvfb)"
${SUDO} tee /etc/systemd/system/mt5-xvfb.service >/dev/null <<EOF
[Unit]
Description=Layar virtual Xvfb untuk MetaTrader 5
After=network.target

[Service]
User=${RUN_USER}
ExecStart=/usr/bin/Xvfb ${DISPLAY} -screen 0 1280x800x24 -nolisten tcp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
${SUDO} systemctl daemon-reload
${SUDO} systemctl enable --now mt5-xvfb.service
sleep 2

# Hentikan terminal lama (jika ada) selama instalasi agar tidak bentrok.
${SUDO} systemctl stop mt5-terminal.service 2>/dev/null || true

# ---------- 3. Wine prefix ----------
if [[ ! -d "${WINEPREFIX}/drive_c" ]]; then
    step "Membuat Wine prefix (bisa memakan 1-2 menit)"
    wineboot -u
    "${WINESERVER_BIN}" -w
fi
"${WINE_BIN}" reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d win10 /f >/dev/null
"${WINESERVER_BIN}" -w

# ---------- 4. MetaTrader 5 ----------
if [[ ! -f "${MT5_EXE}" ]]; then
    step "Mengunduh dan menginstal MetaTrader 5 (beberapa menit)"
    TMP_DIR="$(mktemp -d)"
    wget -qO "${TMP_DIR}/mt5setup.exe" "${MT5_SETUP_URL}"
    "${WINE_BIN}" "${TMP_DIR}/mt5setup.exe" /auto &
    for _ in $(seq 1 120); do
        [[ -f "${MT5_EXE}" ]] && break
        sleep 5
    done
    sleep 20   # beri waktu installer menyelesaikan penyalinan file
    "${WINESERVER_BIN}" -k || true
    rm -rf "${TMP_DIR}"
    if [[ ! -f "${MT5_EXE}" ]]; then
        echo "ERROR: instalasi MT5 gagal (terminal64.exe tidak ditemukan)." >&2
        echo "       Jalankan ulang skrip ini; jika tetap gagal, pasang manual lewat VNC." >&2
        exit 1
    fi
else
    step "MetaTrader 5 sudah terpasang."
fi

# ---------- 5. Python-Windows + library MetaTrader5 ----------
if [[ ! -f "${PY_WIN_DIR}/python.exe" ]]; then
    step "Memasang Python ${PY_VERSION} (Windows, embeddable) di dalam Wine"
    mkdir -p "${PY_WIN_DIR}"
    wget -qO "${PY_WIN_DIR}/python-embed.zip" "${PY_EMBED_URL}"
    unzip -qo "${PY_WIN_DIR}/python-embed.zip" -d "${PY_WIN_DIR}"
    rm -f "${PY_WIN_DIR}/python-embed.zip"
    # Aktifkan site-packages agar pip dan library bisa dipakai.
    sed -i 's/^#[[:space:]]*import site/import site/' "${PY_WIN_DIR}"/python*._pth
fi

if ! "${WINE_BIN}" "${PY_WIN_CMD}" -c "import MetaTrader5" >/dev/null 2>&1; then
    step "Memasang pip dan library MetaTrader5 di Python-Windows"
    wget -qO "${PY_WIN_DIR}/get-pip.py" "${GET_PIP_URL}"
    "${WINE_BIN}" "${PY_WIN_CMD}" "C:/mt5py/get-pip.py" --no-warn-script-location
    "${WINE_BIN}" "${PY_WIN_CMD}" -m pip install --no-warn-script-location MetaTrader5
    "${WINESERVER_BIN}" -w || true
    if ! "${WINE_BIN}" "${PY_WIN_CMD}" -c "import MetaTrader5" >/dev/null 2>&1; then
        echo "ERROR: library MetaTrader5 gagal diimpor di Python-Windows." >&2
        echo "       Coba: wine ${PY_WIN_CMD} -m pip install \"numpy<2\" MetaTrader5" >&2
        exit 1
    fi
else
    step "Library MetaTrader5 sudah terpasang di Python-Windows."
fi
"${WINESERVER_BIN}" -k 2>/dev/null || true

# ---------- 6. Service terminal MT5 ----------
step "Menyiapkan service terminal MT5 (mt5-terminal)"
${SUDO} tee /etc/systemd/system/mt5-terminal.service >/dev/null <<EOF
[Unit]
Description=Terminal MetaTrader 5 (Wine) untuk oanda-xauusd-agent
After=network-online.target mt5-xvfb.service
Wants=network-online.target
Requires=mt5-xvfb.service

[Service]
User=${RUN_USER}
Environment=HOME=${RUN_HOME}
Environment=WINEPREFIX=${WINEPREFIX}
Environment=WINEARCH=win64
Environment=WINEDEBUG=-all
Environment=DISPLAY=${DISPLAY}
ExecStart=${WINE_BIN} "${MT5_EXE}"
ExecStopPost=-${WINESERVER_BIN} -k
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
${SUDO} systemctl daemon-reload
${SUDO} systemctl enable --now mt5-terminal.service

# ---------- 7. Virtual environment bot (Python Linux) ----------
if [[ ! -x "${PYTHON_BIN}" ]]; then
    step "Membuat virtual environment di ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
else
    step "Virtual environment sudah ada, dipakai ulang."
fi
"${PYTHON_BIN}" -m pip install --upgrade pip --quiet
"${PYTHON_BIN}" -m pip install -r "${PROJECT_DIR}/requirements.txt" --quiet

# ---------- 8. File .env & folder log ----------
mkdir -p "${LOG_DIR}"
if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
    cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
    echo "==> File .env dibuat dari .env.example. WAJIB diisi sebelum menjalankan agen."
fi
chmod 600 "${PROJECT_DIR}/.env"

# ---------- 8b. Market API untuk n8n (opsional) ----------
# Service ini hanya dibutuhkan jika laporan dijalankan dari workflow n8n.
step "Menulis service systemd xauusd-market-api"
${SUDO} tee /etc/systemd/system/xauusd-market-api.service >/dev/null <<EOF
[Unit]
Description=XAU/USD market API (data MT5 + berita) untuk n8n
After=network-online.target mt5-terminal.service
Wants=network-online.target mt5-terminal.service

[Service]
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=HOME=${RUN_HOME}
ExecStart=${PYTHON_BIN} -m tools.market_api
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
${SUDO} systemctl daemon-reload
if grep -Eq '^MARKET_API_TOKEN=.{24,}$' "${PROJECT_DIR}/.env" \
        && ! grep -q '^MARKET_API_TOKEN=isi_' "${PROJECT_DIR}/.env"; then
    ${SUDO} systemctl enable xauusd-market-api.service
    ${SUDO} systemctl restart xauusd-market-api.service
    echo "Market API aktif. Cek: curl -s http://127.0.0.1:8787/health"
else
    echo "MARKET_API_TOKEN belum diisi di .env; service xauusd-market-api belum diaktifkan."
fi

# ---------- 9. Cronjob ----------
if [[ "${INSTALL_CRON}" -eq 1 ]]; then
    step "Memasang cronjob"
    ( crontab -l 2>/dev/null | grep -vF "${CRON_MARKER}" || true; echo "${CRON_LINE}" ) | crontab -
    echo "Cronjob terpasang. Cek dengan: crontab -l"
fi

cat <<EOF

============================================================
 SETUP SELESAI
============================================================
1. Isi kredensial (Gemini, Telegram, dan akun MT5):
     nano ${PROJECT_DIR}/.env

2. LOGIN MT5 PERTAMA KALI (sekali saja, lewat VNC):
   a. Di VPS, buka akses VNC sementara (hanya lewat localhost):
        x11vnc -display ${DISPLAY} -localhost -nopw -forever
   b. Di PC Anda, buat terowongan SSH (terminal baru):
        ssh -L 5900:localhost:5900 ${RUN_USER}@IP_VPS_ANDA
   c. Buka VNC viewer (mis. TigerVNC/RealVNC) ke: localhost:5900
   d. Di MT5: File > Login to Trade Account, isi login, password, dan
      server OANDA (sama seperti di web terminal), centang "Save password".
      Jika server tidak muncul: File > Open an Account, cari "OANDA".
   e. Pastikan XAUUSD ada di Market Watch, lalu tutup VNC viewer dan
      hentikan x11vnc dengan Ctrl+C.

3. Uji pengambilan data dari MT5:
     cd ${PROJECT_DIR} && ${PYTHON_BIN} -m tools.oanda_client H1

4. Uji agen tanpa mengirim ke Telegram, lalu uji penuh:
     cd ${PROJECT_DIR} && ${PYTHON_BIN} main.py --dry-run
     cd ${PROJECT_DIR} && ${PYTHON_BIN} main.py

5. Pasang cronjob (setiap jam, Senin-Jumat):
   Otomatis : ./setup_vps.sh --install-cron
   Manual   : jalankan "crontab -e" lalu tambahkan baris berikut:

${CRON_LINE}

   Catatan: jadwal cron memakai zona waktu VPS (cek: timedatectl).

   Memakai workflow n8n? JANGAN pasang cron ini (laporan akan terkirim dua kali).
   Sebagai gantinya isi MARKET_API_TOKEN di .env (openssl rand -hex 32), lalu:
     sudo systemctl enable --now xauusd-market-api
     curl -s -H "X-API-Key: <token>" http://127.0.0.1:8787/snapshot | head -c 300

6. Perawatan:
     systemctl status mt5-terminal        # status terminal MT5
     sudo systemctl restart mt5-terminal  # restart jika data macet
     tail -f ${LOG_DIR}/agent.log         # log bot
     tail -f ${LOG_DIR}/cron.log          # log cron
============================================================
EOF
