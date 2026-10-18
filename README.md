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
