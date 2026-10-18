# استقرار روی Ubuntu 24.04

دو مسیر دارید: **Docker (پیشنهادی)** یا **نصب مستقیم روی سیستم**. هر دو روی
Ubuntu 24.04 LTS آزمایش شده‌اند. پلتفرم کامل تلگرامی است؛ تنها پورت باز، پورت
سلامت داخلی `8081` است که روی `127.0.0.1` می‌نشیند.

---

## ۰. پیش‌نیازها

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
```

- `git` برای دریافت کد
- `curl` برای بررسی سلامت سرویس

---

## ۱. دریافت کد

```bash
sudo mkdir -p /opt/vpnbot
sudo chown "$USER":"$USER" /opt/vpnbot
git clone https://github.com/KEHSANN/bot.git /opt/vpnbot
cd /opt/vpnbot
```

اگر مخزن خصوصی است، یک Personal Access Token بسازید و هنگام `clone` به‌جای
رمز عبور وارد کنید.

---

## ۲. ساخت فایل `.env`

```bash
cp .env.example .env
```

سه مقدار زیر **باید** عوض شوند:

| کلید | نحوه ساخت |
| --- | --- |
| `BOT_TOKEN` | از [@BotFather](https://t.me/BotFather) |
| `INVENTORY_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `FINGERPRINT_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |

```bash
nano .env
```

نکات مهم:

- `OWNER_TELEGRAM_IDS` را با شناسه عددی خودتان پر کنید (چند شناسه را با کاما
  جدا کنید). بدون آن برنامه در حالت production بالا نمی‌آید.
- `POSTGRES_PASSWORD` را هم در `.env` تنظیم کنید؛ Docker از همین مقدار
  استفاده می‌کند.
- `ENVIRONMENT=production` بماند.

```bash
chmod 600 .env
```

---

## ۳. مسیر A — Docker (پیشنهادی)

### نصب Docker

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
newgrp docker
```

سرویس‌ها:

| سرویس | وظیفه |
| --- | --- |
| `db` | PostgreSQL 16، فقط داخل شبکه کامپوز |
| `redis` | وضعیت FSM ویزاردها (اختیاری اما پیشنهادی) |
| `migrate` | اجرای یک‌باره migrationها، سپس خارج می‌شود |
| `bot` | polling تلگرام + endpointهای سلامت |
| `worker` | صف کارها: تحویل کانفیگ، همگانی، انقضای پرداخت |

### بالا آوردن

```bash
docker compose build
docker compose up -d
docker compose ps
```

`bot` باید `healthy` شود و `migrate` با کد `0` خارج شود.

### مایگریشن و داده اولیه

`migrate` خودکار اجرا می‌شود. نقش‌ها، صفحه‌ها و تنظیمات پیش‌فرض با اولین
دستور زیر ساخته می‌شوند:

```bash
docker compose run --rm bot vpnbot seed
docker compose run --rm bot vpnbot bootstrap-owner
```

هر دو idempotent هستند؛ اجرای دوباره چیزی را خراب نمی‌کند.

### لاگ‌ها

```bash
docker compose logs -f bot
docker compose logs -f worker
```

### به‌روزرسانی

```bash
cd /opt/vpnbot
git pull --ff-only
docker compose build
docker compose up -d
```

`migrate` قبل از بالا آمدن `bot` و `worker` اجرا می‌شود، پس ترتیب خودکار است.

---

## ۴. مسیر B — نصب مستقیم

### بسته‌های سیستمی

```bash
sudo apt install -y python3 python3-venv python3-pip postgresql redis-server
```

### ساخت دیتابیس

```bash
sudo -u postgres psql <<'SQL'
CREATE USER vpnbot WITH PASSWORD 'CHANGE_ME';
CREATE DATABASE vpnbot OWNER vpnbot;
SQL
```

سپس در `.env`:

```
DATABASE_URL=postgresql+asyncpg://vpnbot:CHANGE_ME@127.0.0.1:5432/vpnbot
REDIS_URL=redis://127.0.0.1:6379/0
```

Redis را محدود به loopback نگه دارید (در Ubuntu 24 به‌صورت پیش‌فرض همین‌طور
است) و اگر لازم شد در `/etc/redis/redis.conf` مقدار
`bind 127.0.0.1 -::1` را تأیید کنید.

### نصب برنامه

```bash
cd /opt/vpnbot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install .
.venv/bin/vpnbot migrate
.venv/bin/vpnbot seed
.venv/bin/vpnbot bootstrap-owner
```

### دو سرویس systemd

`/etc/systemd/system/vpnbot-bot.service`:

```ini
[Unit]
Description=vpnbot Telegram bot
After=network-online.target postgresql.service redis-server.service
Wants=network-online.target

[Service]
Type=simple
User=vpnbot
Group=vpnbot
WorkingDirectory=/opt/vpnbot
EnvironmentFile=/opt/vpnbot/.env
ExecStart=/opt/vpnbot/.venv/bin/vpnbot run
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/vpnbot

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/vpnbot-worker.service` همان است با
`ExecStart=/opt/vpnbot/.venv/bin/vpnbot worker` و
`Description=vpnbot job worker`.

ساخت کاربر سرویس و فعال‌سازی:

```bash
sudo useradd --system --home /opt/vpnbot --shell /usr/sbin/nologin vpnbot
sudo chown -R vpnbot:vpnbot /opt/vpnbot
sudo systemctl daemon-reload
sudo systemctl enable --now vpnbot-bot vpnbot-worker
sudo systemctl status vpnbot-bot --no-pager
```

به‌روزرسانی در این حالت:

```bash
cd /opt/vpnbot
sudo -u vpnbot git pull --ff-only
sudo -u vpnbot .venv/bin/pip install .
sudo -u vpnbot .venv/bin/vpnbot migrate
sudo systemctl restart vpnbot-bot vpnbot-worker
```

---

## ۵. بررسی سلامت

```bash
curl -fsS http://127.0.0.1:8081/healthz
curl -fsS http://127.0.0.1:8081/readyz
```

`readyz` باید `{"status":"ok"}` با `database: ok` و `redis: ok` بدهد. اگر
Redis را غیرفعال کرده‌اید (`REDIS_ENABLED=false`) آن کلید اصلاً گزارش نمی‌شود.

در حالت Docker:

```bash
docker compose exec bot curl -fsS http://127.0.0.1:8081/readyz
```

---

## ۶. اولین ورود به پنل

1. در تلگرام ربات را باز کنید و `/start` بزنید.
2. با همان حسابی که در `OWNER_TELEGRAM_IDS` گذاشتید `/admin` بزنید.
3. از «تنظیمات» شماره کارت / آدرس کریپتو و متن‌ها را پر کنید.
4. از «محصولات» یک دسته، محصول و پلن بسازید.
5. از «انبار» و سپس «ورود کانفیگ» فایل کانفیگ‌ها را وارد کنید.

تا وقتی موجودی وارد نشده باشد، خریداری ثبت نمی‌شود؛ این عمدی است.

---

## ۷. پشتیبان‌گیری

Docker:

```bash
docker compose exec -T db pg_dump -U vpnbot vpnbot | gzip > "backup-$(date +%F).sql.gz"
```

نصب مستقیم:

```bash
sudo -u postgres pg_dump vpnbot | gzip > "backup-$(date +%F).sql.gz"
```

بازیابی:

```bash
gunzip -c backup-2026-01-01.sql.gz | docker compose exec -T db psql -U vpnbot -d vpnbot
```

`.env` را جداگانه و امن نگه دارید: **بدون `INVENTORY_ENCRYPTION_KEY` بکاپ
دیتابیس قابل استفاده نیست**، چون کانفیگ‌ها با همان کلید رمز شده‌اند.

---

## ۸. نکات امنیتی

- پورت `8081` را روی اینترنت باز نکنید؛ فقط loopback.
- `.env` باید `600` و مالک `vpnbot` باشد.
- هیچ‌گاه `INVENTORY_ENCRYPTION_KEY` را عوض نکنید مگر با ابزار چرخش کلید؛
  داده‌های قبلی دیگر خوانده نمی‌شوند.
- کانفیگ‌ها و لینک اشتراک فقط پس از تأیید دستی پرداخت برای خریدار ارسال
  می‌شوند؛ در لاگ‌ها هم نوشته نمی‌شوند.
- برای امنیت بیشتر می‌توانید `ufw` را فعال کنید و فقط SSH را باز بگذارید:
  ```bash
  sudo ufw allow OpenSSH
  sudo ufw enable
  ```

---

## ۹. عیب‌یابی سریع

| نشانه | بررسی |
| --- | --- |
| `bot` بالا نمی‌آید | `docker compose logs bot`؛ معمولاً `.env` ناقص یا `BOT_TOKEN` غلط است |
| `readyz` روی database خطا می‌دهد | پسورد `POSTGRES_PASSWORD` و وضعیت `db` |
| تحویل انجام نمی‌شود | `docker compose logs worker`؛ jobها در جدول `jobs` قابل مشاهده‌اند |
| پیام‌های همگانی گیر کرده | وضعیت campaign در پنل و `BROADCAST_RATE_PER_SECOND` |
