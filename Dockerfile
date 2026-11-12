# ============================================================
# Image xauusd-agent: MT5 (Wine + Xvfb) + market API / bot dalam satu container.
# Bot memanggil tools/mt5_bridge.py lewat Wine sebagai subprocess, jadi bot dan
# terminal MT5 harus berada di container yang sama.
#
# Instalasi MT5 + Python-Windows dilakukan saat container pertama kali jalan
# (docker/entrypoint.sh) dan disimpan di volume /data, sehingga login MT5
# tetap ada walau container dibuat ulang.
# ============================================================
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    WINEARCH=win64 \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES="mscoree,mshtml=" \
    WINEPREFIX=/data/wine \
    DISPLAY=:99 \
    MT5_DISPLAY=:99 \
    MT5_PYTHON_CMD="wine C:/mt5py/python.exe" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates wget gnupg unzip xvfb x11vnc procps tini python3 python3-venv \
    && mkdir -pm755 /etc/apt/keyrings \
    && wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -qNP /etc/apt/sources.list.d/ \
        https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources \
    && apt-get update \
    && apt-get install -y --install-recommends winehq-stable \
    && rm -rf /var/lib/apt/lists/*

# User non-root uid 1000 (image ubuntu sudah punya user "ubuntu" dengan uid itu).
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash mt5 \
    && mkdir -p /data /app/logs /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown -R mt5:mt5 /data /app

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=mt5:mt5 . /app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# Jaga-jaga jika file ter-checkout dengan CRLF di Windows.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

USER mt5
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8787 5900

# Instalasi pertama MT5 bisa makan 5-10 menit, jadi start-period dibuat panjang.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15m --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('MARKET_API_PORT','8787'), timeout=5)"

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "tools.market_api"]
