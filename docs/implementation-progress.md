# وضعیت پیاده‌سازی

آخرین به‌روزرسانی: 2026-10-18

## تکمیل‌شده

### زیرساخت
- تنظیمات از محیط (`config.py`) با اعتبارسنجی کلید Fernet و حداقل طول secret
- engine و session factory آسنکرون (`db/session.py`)
- مدل‌ها: کاربر/مدیر/نقش، کاتالوگ، انبار، سفارش/پرداخت، کیف پول، معرف،
  پشتیبانی، محتوا، همگانی، کارها، رویدادهای ممیزی
- Alembic با migration اولیه (۲۳ جدول) و دو trigger محافظ:
  - `trg_audit_events_immutable` — ممیزی فقط افزودنی
  - `trg_inventory_item_no_reuse` — بازگشت از DELIVERED/DISABLED ممنوع

### دامنه
- `money.py` — تومان صحیح، ارقام فارسی، تبدیل ریال، بدون float
- `jalali.py` — تاریخ شمسی با تهران +03:30
- `security/crypto.py` — Fernet + fingerprint با HMAC keyed
- سرویس‌ها: کاتالوگ، انبار (`FOR UPDATE SKIP LOCKED`)، سفارش، پرداخت
  (تأیید/رد idempotent)، تحویل سه‌فازی، کیف پول (دفتر کل idempotent),
  معرف، همگانی، پشتیبانی، تنظیمات، صفحه‌ها، ورود دسته‌ای، حسابرسی

### ربات
- middlewareها: DbSession → UserLoader → AdminLoader → Throttle
- هندلرهای مشتری: منو، فروشگاه، پرداخت، سفارش‌ها، کیف پول، دعوت، پشتیبانی
- پنل مدیریت: داشبورد، کاتالوگ، انبار، **ورود دسته‌ای**، سفارش‌ها، پرداخت‌ها،
  پشتیبانی، همگانی، ویرایشگر صفحه/دکمه، تنظیمات، مدیران، کیف پول، ممیزی
- `bot/app.py` — dispatcher، storage با fallback، دستورها، هندلر خطا

### پردازش و اجرا
- `jobs/worker.py` — claim با SKIP LOCKED، شش نوع job، backoff نمایی،
  بازیابی jobهای گیرکرده، محدودیت نرخ همگانی
- `api/app.py` — `/healthz`، `/readyz`، `/version`
- `cli.py` — `migrate`، `seed`، `bootstrap-owner`، `run`، `worker`، `sweep`

### تست (۳۶ تست، همه پاس روی PostgreSQL واقعی)
- `test_money.py` — پارس و قالب‌بندی مبالغ
- `test_inventory_invariants.py` — یک‌بارمصرف بودن، triggerها، رزرو هم‌زمان
- `test_fulfillment_flow.py` — مسیر کامل پرداخت دستی تا تحویل، idempotency،
  نبود موجودی، رد پرداخت، تکرار TXID

### استقرار
- `Dockerfile`، `docker-compose.yml` (db/redis/migrate/bot/worker)
- `docs/deployment-ubuntu.md` — Docker و systemd
- `README.md`، `.env.example`

## باگ‌هایی که تست‌ها پیدا کردند و رفع شد
- `orders.create_order` به تابع ناموجود `inventory.stock_for_plan` اشاره می‌کرد؛
  **همه خریدها خطا می‌دادند**. به `catalog.stock_for_plan` منتقل شد.
- `payments.start_payment` روی رابطه lazy دست می‌گذاشت و در async خطای
  `MissingGreenlet` می‌داد. به کوئری صریح تغییر کرد.
- `reserve_for_order` پس از `UPDATE`، شیء کهنه را از identity map می‌خواند.
  اکنون با `populate_existing` دوباره خوانده می‌شود.
- بررسی تکرار TXID به حروف بزرگ/کوچک حساس بود. اکنون case-insensitive است.

## باقی‌مانده / بدهی فنی
- `wallet.apply` در `IntegrityError` کل تراکنش را rollback می‌کند؛ بهتر است
  `SAVEPOINT` باشد (فقط مسیر رقابت نادر).
- `inventory.item_expired` placeholder است؛ منقضی‌شدن زمان‌دار پلن پیاده نشده.
- هندلر تکراری `/cancel` در `customer/menu.py`.
- `settings_manager._prompt` آرگومان بی‌استفاده `index` دارد.
- تست هم‌زمانی فقط دو رزرو موازی را پوشش می‌دهد؛ بار بالاتر تست نشده.
- پوشش تست برای هندلرهای تلگرام (نیاز به bot fake) وجود ندارد.
