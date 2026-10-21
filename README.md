# vpnbot — فروش VPN در تلگرام

پلتفرم فروش کانفیگ VPN به‌صورت **کاملاً تلگرامی**: یک ربات برای مشتری و یک
پنل مدیریت کامل داخل همان ربات. وب‌سایت، داشبورد جدا یا Mini App وجود ندارد.

- پایتون 3.11+ · aiogram 3 · PostgreSQL · SQLAlchemy 2 (async) · Alembic · Redis
- رابط کاربری فارسی، مبالغ به **تومان** و به‌صورت عدد صحیح
- پرداخت دستی: کارت‌به‌کارت و کریپتو؛ بدون درگاه و بدون بررسی زنجیره‌ای

## دو قاعده‌ای که کل سیستم روی آن‌ها بنا شده

1. **هر کانفیگ یک‌بار مصرف است.** هر رکورد انبار یک جفت جدانشدنی
   «کانفیگ + لینک اشتراک» است با چرخه
   `AVAILABLE → RESERVED → DELIVERED` (یا `DISABLED`). بازگشت از `DELIVERED`
   در سطح دیتابیس با trigger مسدود شده است، نه فقط در کد.
2. **تا تأیید دستی مدیر، هیچ چیزی تحویل نمی‌شود.** سرویس تحویل مستقل از
   پنل، وضعیت پرداخت را دوباره زیر قفل بررسی می‌کند و در صورت نبود تأیید،
   تحویل را رد می‌کند.

## اجرا

سریع‌ترین مسیر روی Ubuntu 24:

```bash
cp .env.example .env   # BOT_TOKEN و کلیدها را پر کنید
docker compose up -d
docker compose run --rm bot vpnbot seed
docker compose run --rm bot vpnbot bootstrap-owner
```

راهنمای کامل (Docker و نصب مستقیم، بکاپ، عیب‌یابی):
[docs/deployment-ubuntu.md](docs/deployment-ubuntu.md)

---

## نصب از صفر (مسیر پیشنهادی: Docker)

### گام ۰ — نصب Docker

روی Ubuntu 24.04:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
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

### گام ۱ — دریافت کد

```bash
sudo mkdir -p /opt/vpnbot
sudo chown "$USER":"$USER" /opt/vpnbot
git clone https://github.com/KEHSANN/bot.git /opt/vpnbot
cd /opt/vpnbot
```

### گام ۲ — ساخت و پر کردن `.env`

```bash
cp .env.example .env
nano .env
```

این مقدارها **حتماً** باید عوض شوند:

| کلید | از کجا / چطور |
| --- | --- |
| `BOT_TOKEN` | از [@BotFather](https://t.me/BotFather) |
| `OWNER_TELEGRAM_IDS` | شناسه عددی تلگرام خودتان (از @userinfobot)؛ چندتا با کاما |
| `POSTGRES_PASSWORD` | یک رمز قوی دلخواه (خودتان تعیین می‌کنید) |
| `INVENTORY_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `FINGERPRINT_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BOT_USERNAME` | یوزرنیم ربات بدون `@` |

بقیه مقدارها (از جمله `ENVIRONMENT=production`) همان پیش‌فرض بمانند.

```bash
chmod 600 .env
```

### گام ۳ — بالا آوردن سرویس‌ها

```bash
docker compose build
docker compose up -d
docker compose ps
```

سرویس `migrate` مایگریشن‌های دیتابیس را خودکار اجرا می‌کند و خارج می‌شود؛
`bot` باید به وضعیت `healthy` برسد.

### گام ۴ — داده اولیه و مالک

```bash
docker compose run --rm bot vpnbot seed
docker compose run --rm bot vpnbot bootstrap-owner
```

هر دو دستور idempotent هستند؛ اجرای دوباره‌شان چیزی را خراب نمی‌کند.

### گام ۵ — بررسی سلامت

```bash
curl -fsS http://127.0.0.1:8081/readyz
```

باید `{"status":"ok"}` با `database: ok` و `redis: ok` برگردد.

### گام ۶ — اولین ورود و راه‌اندازی فروشگاه

1. در تلگرام ربات را باز کنید و `/start` بزنید.
2. با همان حسابی که در `OWNER_TELEGRAM_IDS` گذاشتید، `/admin` بزنید.
3. در «تنظیمات» شماره کارت / آدرس کریپتو را پر کنید.
4. در «محصولات» یک دسته، محصول و پلن بسازید.
5. در «انبار ← ورود کانفیگ» فایل کانفیگ‌ها را وارد کنید.

تا وقتی موجودی وارد نشده باشد خریدی انجام نمی‌شود؛ این عمدی است.

> مسیر جایگزین بدون Docker (نصب مستقیم + systemd) در
> [docs/deployment-ubuntu.md](docs/deployment-ubuntu.md) آمده است.

---

## به‌روزرسانی امن (بدون آسیب به داده‌ها)

داده‌ها (کاربران، سفارش‌ها، کیف پول، انبار) داخل والیوم داکر `pgdata`
نگه‌داری می‌شوند، نه داخل کانتینرها. آپدیت کد، کانتینرها را بازسازی
می‌کند ولی به والیوم دست نمی‌زند — به شرطی که مراحل زیر را رعایت کنید.

### قبل از هر آپدیت: بکاپ

```bash
cd /opt/vpnbot
docker compose exec -T db pg_dump -U vpnbot vpnbot | gzip > "backup-$(date +%F-%H%M).sql.gz"
cp .env "env-backup-$(date +%F).bak"
```

بکاپ دیتابیس بدون `.env` قابل استفاده نیست: کانفیگ‌های انبار با
`INVENTORY_ENCRYPTION_KEY` داخل `.env` رمز شده‌اند. هر دو را کنار هم و
امن نگه دارید.

### آپدیت

```bash
cd /opt/vpnbot
git pull --ff-only
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:8081/readyz
docker compose logs bot --tail 20
```

- مایگریشن‌های جدید دیتابیس را سرویس `migrate` هنگام بالا آمدن،
  **خودکار و قبل از bot و worker** اعمال می‌کند؛ نیازی به دستور دستی
  نیست و ساختار داده‌های قبلی حفظ می‌شود.
- اجرای دوباره `vpnbot seed` لازم نیست و اگر اجرا شود هم آسیبی
  نمی‌زند: فقط کلیدهای تنظیماتی را که وجود ندارند اضافه می‌کند و
  تنظیماتی را که از پنل تغییر داده‌اید دست نمی‌زند.

### هرگز این کارها را نکنید

| کار | چرا |
| --- | --- |
| `docker compose down -v` | فلگ `-v` والیوم `pgdata` را **پاک می‌کند**؛ یعنی کل دیتابیس از بین می‌رود. برای توقف عادی فقط `docker compose down` (بدون `-v`) امن است، ولی برای آپدیت اصلاً نیازی به down نیست |
| پاک کردن یا از نو ساختن `.env` | توکن، رمز دیتابیس و کلید رمزنگاری انبار داخل آن است؛ از دست رفتنش یعنی قفل شدن روی داده‌های خودتان |
| تغییر `INVENTORY_ENCRYPTION_KEY` بعد از نصب اول | همه کانفیگ‌های ذخیره‌شده با کلید قبلی رمز شده‌اند و دیگر خوانده نمی‌شوند |
| تغییر `POSTGRES_PASSWORD` در `.env` به‌تنهایی | این رمز فقط موقع ساخت اولیه والیوم روی دیتابیس اعمال می‌شود؛ عوض کردنش در `.env` فقط اتصال ربات را خراب می‌کند. اگر مجبور به تغییرید، اول با `ALTER USER` داخل دیتابیس عوضش کنید، بعد `.env` را هماهنگ کنید |
| `git reset --hard` یا clone دوباره روی پوشه نصب | تغییرات محلی و `.env` را از بین می‌برد؛ آپدیت فقط با `git pull --ff-only` |

### اگر آپدیت خراب شد

کد را به کامیت قبلی برگردانید و دوباره بسازید؛ دیتابیس سر جایش است:

```bash
git log --oneline -5        # کامیت سالم قبلی را پیدا کنید
git checkout <commit>
docker compose build
docker compose up -d
```

اگر مایگریشن جدیدی اعمال شده بود و نسخه قبلی کد با آن ناسازگار بود،
بکاپ قبل از آپدیت را برگردانید (دستور بازیابی در
[docs/deployment-ubuntu.md](docs/deployment-ubuntu.md) بخش ۷).

## دستورها

| دستور | کار |
| --- | --- |
| `vpnbot migrate` | اعمال migrationها |
| `vpnbot seed` | ساخت نقش‌ها، صفحه‌های پیش‌فرض و تنظیمات |
| `vpnbot bootstrap-owner` | دادن نقش مالک به `OWNER_TELEGRAM_IDS` |
| `vpnbot run` | ربات (polling) + endpointهای سلامت |
| `vpnbot worker` | پردازش صف کارها |
| `vpnbot sweep` | یک‌بار انقضای پرداخت‌های قدیمی |

## ساختار

```
src/vpnbot/
  bot/          # dispatcher، middlewareها، هندلرهای مشتری و پنل
  services/     # منطق دامنه: کاتالوگ، انبار، سفارش، پرداخت، تحویل، کیف پول…
  jobs/         # worker صف پایدار
  db/           # مدل‌ها، enumها، engine
  api/          # /healthz و /readyz
alembic/        # migrationها، شامل triggerهای محافظ
tests/          # تست‌های واقعی روی PostgreSQL
```

## تست

تست‌ها روی PostgreSQL واقعی اجرا می‌شوند (نه mock) و خودشان دیتابیس تست را
می‌سازند، migration می‌زنند و در پایان حذف می‌کنند.

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/vpnbot_tests \
  .venv/bin/python -m pytest
```

## محیط

همه تنظیمات از متغیرهای محیطی خوانده می‌شوند؛ فهرست کامل در
[.env.example](.env.example). هیچ مقدار محرمانه‌ای در مخزن نیست.
