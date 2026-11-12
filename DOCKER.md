# Menjalankan xauusd-agent dengan Docker

Satu container `xauusd-market-api` berisi terminal MT5 (Wine + Xvfb) dan
`tools/market_api.py`. Workflow n8n (container `n8n`, network `stack_default`)
mengambil data dari container ini lalu memanggil Gemini dan Telegram sendiri.

```
[n8n]  --HTTP-->  [xauusd-market-api]
                  Xvfb + Wine + MT5 + market_api.py   (volume mt5-data = /data)
```

Pakai salah satu saja: **n8n** atau **cron + main.py**. Jika keduanya jalan, laporan terkirim dua kali.

## 1. Upload project ke VPS

Dari Git Bash di PC (tanpa `venv/`, `logs/`, `.git/`):

```bash
rsync -av --exclude venv --exclude logs --exclude .git --exclude __pycache__ ./ elvin@IP_VPS:~/xauusd-agent/
```

## 2. Isi `.env` di VPS

```bash
cd ~/xauusd-agent && nano .env && chmod 600 .env
```

Wajib untuk market API: `MARKET_API_TOKEN` (buat dengan `openssl rand -hex 32`).
Disarankan: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_SYMBOL`.
`WINEPREFIX`, `MT5_PYTHON_CMD`, `MARKET_API_HOST/PORT` diabaikan karena ditimpa `docker-compose.yml`.

## 3. Build dan jalankan

```bash
docker compose up -d --build
docker compose logs -f
```

Jalan pertama kali memasang Wine prefix, MT5 dan Python-Windows ke volume (5-10 menit).
Tunggu sampai log menampilkan `Market API mendengarkan di http://0.0.0.0:8787`.

## 4. Login MT5 pertama kali (lewat VNC)

Jika `MT5_LOGIN/PASSWORD/SERVER` sudah diisi, langkah ini biasanya tidak perlu.

1. Di PC, buat terowongan SSH dan biarkan terbuka:
   `ssh -L 5900:localhost:5900 elvin@IP_VPS`
2. Buka UltraVNC ke `localhost:5900`.
3. Di MT5: File > Login to Trade Account, centang "Save password".
   Pastikan simbol emas ada di Market Watch.

Login tersimpan di volume `mt5-data`, jadi tetap ada setelah container dibuat ulang.
Setelah itu VNC boleh dimatikan: `ENABLE_VNC=0` di `.env`, lalu `docker compose up -d`.

## 5. Tes

```bash
curl -s http://127.0.0.1:8787/health
curl -s -H "X-API-Key: TOKEN_ANDA" http://127.0.0.1:8787/snapshot | head -c 500
docker compose exec xauusd-market-api python -m tools.oanda_client H1
docker compose exec xauusd-market-api python main.py --dry-run
```

## 6. Sambungkan n8n

Di node HTTP Request workflow n8n:

- URL: `http://xauusd-market-api:8787/snapshot`
- Header: `X-API-Key` = nilai `MARKET_API_TOKEN`

Nama `xauusd-market-api` bisa dipakai karena container ini ikut network `stack_default`.
Jika n8n dipindah ke network lain, ubah `networks.n8n.name` di `docker-compose.yml`.

## Perawatan

```bash
docker compose restart                  # restart MT5 + API
docker compose up -d --build            # setelah kode diubah
docker compose down                     # stop (volume login MT5 tetap ada)
docker volume rm xauusd-agent_mt5-data  # HAPUS instalasi + login MT5 (mulai dari nol)
```
