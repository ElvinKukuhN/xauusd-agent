#!/usr/bin/env bash
# ============================================================
# entrypoint.sh - Menjalankan MT5 (Wine) + market API dalam satu container.
#
# Urutan:
#   1. Xvfb (layar virtual) di ${DISPLAY}
#   2. Instalasi pertama (hanya jika belum ada di volume /data):
#      Wine prefix, terminal MT5, Python-Windows + library MetaTrader5
#   3. x11vnc (opsional, ENABLE_VNC=1) untuk login MT5 pertama kali
#   4. Pengawas terminal MT5 (dijalankan ulang jika mati)
#   5. exec CMD (bawaan: python -m tools.market_api)
#
# Logika instalasi sama dengan setup_vps.sh; ubah keduanya bersamaan.
# ============================================================
set -euo pipefail

MT5_SETUP_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
MT5_EXE="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"
PY_VERSION="3.10.11"
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip"
PY_WIN_DIR="${WINEPREFIX}/drive_c/mt5py"
PY_WIN_CMD="C:/mt5py/python.exe"
GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"
VNC_PORT="${VNC_PORT:-5900}"

log() { echo "[entrypoint] $*"; }

# ---------- 1. Layar virtual ----------
DISPLAY_NUM="${DISPLAY#:}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
Xvfb "${DISPLAY}" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
for _ in $(seq 1 40); do
    [[ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]] && break
    sleep 0.5
done
if [[ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
    log "ERROR: Xvfb gagal berjalan di ${DISPLAY}"
    exit 1
fi

# ---------- 2. Instalasi pertama (disimpan di volume) ----------
mkdir -p "${WINEPREFIX}"

if [[ ! -d "${WINEPREFIX}/drive_c" ]]; then
    log "Membuat Wine prefix (1-2 menit)"
    wineboot -u
    wineserver -w
    wine reg add "HKEY_CURRENT_USER\\Software\\Wine" /v Version /t REG_SZ /d win10 /f >/dev/null
    wineserver -w
fi

if [[ ! -f "${MT5_EXE}" ]]; then
    log "Mengunduh dan menginstal MetaTrader 5 (beberapa menit)"
    TMP_DIR="$(mktemp -d)"
    wget -qO "${TMP_DIR}/mt5setup.exe" "${MT5_SETUP_URL}"
    wine "${TMP_DIR}/mt5setup.exe" /auto >/dev/null 2>&1 &
    for _ in $(seq 1 120); do
        [[ -f "${MT5_EXE}" ]] && break
        sleep 5
    done
    sleep 20   # beri waktu installer menyelesaikan penyalinan file
    wineserver -k || true
    rm -rf "${TMP_DIR}"
    if [[ ! -f "${MT5_EXE}" ]]; then
        log "ERROR: instalasi MT5 gagal (terminal64.exe tidak ditemukan). Restart container untuk mencoba lagi."
        exit 1
    fi
fi

if [[ ! -f "${PY_WIN_DIR}/python.exe" ]]; then
    log "Memasang Python ${PY_VERSION} (Windows, embeddable) di dalam Wine"
    mkdir -p "${PY_WIN_DIR}"
    wget -qO "${PY_WIN_DIR}/python-embed.zip" "${PY_EMBED_URL}"
    unzip -qo "${PY_WIN_DIR}/python-embed.zip" -d "${PY_WIN_DIR}"
    rm -f "${PY_WIN_DIR}/python-embed.zip"
    sed -i 's/^#[[:space:]]*import site/import site/' "${PY_WIN_DIR}"/python*._pth
fi

if ! wine "${PY_WIN_CMD}" -c "import MetaTrader5" >/dev/null 2>&1; then
    log "Memasang pip dan library MetaTrader5 di Python-Windows"
    wget -qO "${PY_WIN_DIR}/get-pip.py" "${GET_PIP_URL}"
    wine "${PY_WIN_CMD}" "C:/mt5py/get-pip.py" --no-warn-script-location
    wine "${PY_WIN_CMD}" -m pip install --no-warn-script-location MetaTrader5 \
        || wine "${PY_WIN_CMD}" -m pip install --no-warn-script-location "numpy<2" MetaTrader5
    wineserver -w || true
    if ! wine "${PY_WIN_CMD}" -c "import MetaTrader5" >/dev/null 2>&1; then
        log "ERROR: library MetaTrader5 gagal diimpor di Python-Windows."
        exit 1
    fi
fi
wineserver -k 2>/dev/null || true

# ---------- 3. VNC (opsional) ----------
if [[ "${ENABLE_VNC:-1}" == "1" ]]; then
    if [[ -n "${VNC_PASSWORD:-}" ]]; then
        x11vnc -storepasswd "${VNC_PASSWORD}" /data/.vncpass >/dev/null 2>&1
        VNC_AUTH=(-rfbauth /data/.vncpass)
    else
        VNC_AUTH=(-nopw)
    fi
    (
        while true; do
            x11vnc -display "${DISPLAY}" -rfbport "${VNC_PORT}" "${VNC_AUTH[@]}" \
                -forever -shared -quiet >/dev/null 2>&1 || true
            sleep 3
        done
    ) &
    log "VNC aktif di port ${VNC_PORT} (container)"
fi

# ---------- 4. Pengawas terminal MT5 ----------
# Cek proses, bukan menunggu exit: updater MT5 bisa menjalankan ulang terminal
# sebagai proses baru, dan menunggu exit akan memicu instance kedua.
(
    while true; do
        if ! pgrep -f terminal64.exe >/dev/null 2>&1; then
            log "Menjalankan terminal MT5"
            wine "${MT5_EXE}" >/dev/null 2>&1 &
        fi
        sleep 15
    done
) &

log "Siap. Menjalankan: $*"
exec "$@"
