# پلتفرم جامع فروش VPN تلگرام | Production-Grade Telegram VPN Store

**نسخه کامل دوگانه (فارسی + English) | Bilingual Documentation (Persian & English)**

## 🇮🇷 بخش اول: مستندات کامل فارسی (معرفی ویژگی‌ها، معماری و نحوه اجرا)

### ۱. معرفی کلی پروژه
این پروژه یک پلتفرم کامل، مقیاس‌پذیر، امن و تست‌شده برای **فروش خودکار و دستی اشتراک‌ها و کانفیگ‌های VPN در تلگرام** است که به همراه **دو پنل مدیریت کاملاً هم‌تراز** (پنل وب راست‌به‌چپ فارسی با Next.js و پنل مدیریت تعاملی داخل تلگرام) طراحی و پیاده‌سازی شده است. تمام منطق‌های مالی، موجودی، قیمت‌گذاری، سیستم دعوت و دسترسی‌ها در لایه سرویس بک‌اند متمرکز هستند تا هیچ‌گونه دوگانگی یا خطای امنیتی بین وب و تلگرام رخ ندهد.

### ۲. ویژگی‌های کلیدی و تضمین‌های مهندسی

1. **موتور مالی دقیق و دفتر کل دوطرفه تغییرناپذیر (`IRT Wallet & Immutable Double-Entry Ledger`)**:
   - واحد پولی پایه سراسر سیستم **تومان (`IRT`)** به صورت عدد صحیح ۶۴ بیتی (`1 IRT = 10 IRR`) است و هیچ عدد اعشاری (`float`) در محاسبات مالی مجاز نیست.
   - تمامی تراکنش‌های کیف پول در جداول `ledger_transactions` و `ledger_entries` به صورت دوطرفه (بدهکار/بستانکار) ثبت می‌شوند و رکوردهای تاریخچه هرگز ویرایش یا حذف نمی‌گردند؛ اصلاحات یا لغوها صرفاً با تراکنش جبرانی (`refund_credit` یا `referral_reversal`) انجام می‌شوند.
   - استفاده از قفل تراکنشی (`BEGIN IMMEDIATE` / Row-Level Locking) و شرط اتمیک `WHERE balance_irt >= ?` مانع از ایجاد موجودی منفی در خریدهای هم‌زمان می‌شود.

2. **موتور قیمت‌گذاری تصادفی و پیش‌فاکتور پایدار (`PriceQuote`)**:
   - مدیر می‌تواند برای هر محصول یک قیمت پایه (`base_price_irt`)، بازه افزایش تصادفی (`min_random_increment_irt` تا `max_random_increment_irt`) و گام افزایش (`increment_step_irt`) تعریف کند.
   - هنگام استعلام قیمت توسط کاربر، یک پیش‌فاکتور پایدار (`PriceQuote`) با زمان انقضای مشخص در دیتابیس ذخیره می‌شود تا کلیک‌های مکرر کاربر، کندی شبکه یا تلاش مجدد هرگز باعث تولید قیمت تصادفی جدید یا دور زدن قفل تکرار سفارش نشود.

3. **مدیریت موجودی کانفیگ‌ها و درون‌ریزی دسته‌ای اتمیک (`Atomic Bulk Inventory Import`)**:
   - پشتیبانی از کانفیگ‌های دستی (`vless://`, `vmess://`, `trojan://`, `ss://` و لینک‌های اشتراک `https://`).
   - قابلیت درون‌ریزی دسته‌ای با جداکننده‌های قابل تنظیم (خط جدید، کاما، سمی‌کالن، پایپ)، محاسبه هش `SHA-256` روی محتوای نرمال‌شده برای تشخیص موارد تکراری (هم در داخل فایل ارسالی و هم در کل دیتابیس) و نمایش پیش‌نمایش خط‌به‌خط قبل از ثبت نهایی.
   - پشتیبانی از حالت **ثبت اتمیک (`atomic`)** (اگر حتی یک خط خطا یا تکراری داشته باشد کل بسته رد می‌شود) و حالت **ثبت جزئی (`partial`)**.
   - تمامی کانفیگ‌ها به صورت رمزنگاری‌شده (`enc_v1` با تگ احراز اصالت `HMAC-SHA256`) در دیتابیس نگهداری می‌شوند.

4. **تامین خودکار اشتراک (`Automated VPN Provisioning`) و مدیریت خطاهای مبهم**:
   - دارای آداپتورهای واقعی HTTP برای پنل‌های **مرزبان (`MarzbanHTTPProviderAdapter`)** و **سنایی (`ThreeXUIHTTPProviderAdapter`)** در فایل `backend/app/services/live_integrations.py` به همراه آداپتور محیط تست (`FakeVPNProviderAdapter`).
   - تولید نام کاربری یکتا و قطعی بر اساس `idempotency_key` سفارش؛ اگر درخواست ساخت اشتراک به دلیل کندی شبکه دچار Timeout شود ولی در پنل مرزبان ساخته شده باشد، وضعیت سفارش به `failed_Requires_reconciliation` می‌رود و در مرحله تطبیق (`reconcile`) بدون ساخت مجدد یا کسر پول اضافه، کانفیگ استعلام و تحویل کاربر می‌شود.
   - در صورت بروز خطای قطعی در سرویس‌دهنده (`ProvisioningDefiniteFailureError`)، مبلغ کسرشده به صورت خودکار و اتمیک از طریق تراکنش جبرانی به کیف پول کاربر بازگردانده می‌شود.

5. **واریز دستی کارت‌به‌کارت و درگاه‌های پرداخت خودکار (زرین‌پال و زیبال)**:
   - **واریز دستی**: ثبت درخواست واریز توسط کاربر به همراه کد پیگیری، بدون شارژ خودکار؛ بررسی توسط ادمین با قفل هم‌زمانی (`WHERE status = 'pending'`) که مانع از تایید هم‌زمان یک فیش توسط دو ادمین می‌شود.
   - **درگاه پرداخت خودکار**: آداپتورهای آماده برای **زرین‌پال (`ZarinpalHTTPGatewayAdapter`)** و **زیبال (`ZibalHTTPGatewayAdapter`)** با تبدیل خودکار تومان به ریال (`irt_to_irr`)، تایید سروربه‌سرور (`verify_payment`) و بررسی امضای دیجیتال Webhook (`HMAC-SHA256`) به همراه محافظت در برابر تکرار (`Replay Protection`).

6. **سیستم دعوت (`Referral`) و پاداش‌های چهارگانه (`N`, `W`, `P`, `M/K`)**:
   - لینک دعوت امن و غیرقابل حدس (`ref_<opaque_token>`)، جلوگیری قطعی از دعوت خود (`Self-Referral`) بر اساس `telegram_id` و عدم تغییر معرف اولیه در اجراهای بعدی `/start`.
   - **پاداش معرف (`N`)**: قابل تنظیم بر اساس ثبت‌نام، تایید عضویت در کانال یا اولین پرداخت.
   - **پاداش خوش‌آمدگویی کاربر جدید (`W`)**: واریز حداکثر یک‌بار به حساب کاربر دعوت‌شده پس از احراز شرایط.
   - **پاداش اولین پرداخت تاییدشده (`P`)**: واریز خودکار به معرف به محض اولین شارژ یا پرداخت موفق و تاییدشده کاربر دعوت‌شده (مقاوم در برابر Webhookهای تکراری).
   - **پاداش حدنصاب (`M` و `K`)**: با رسیدن تعداد دعوت‌های واجد شرایط به عدد `M`، یک درخواست پاداش به مبلغ `K` تومان در وضعیت `pending_review` ایجاد می‌شود و تنها پس از تایید مدیر (همراه با ثبت دلیل و لاگ ممیزی) به کیف پول واریز می‌گردد.

7. **عضویت اجباری در کانال با کش ۲۴ ساعته (`Membership Cache`) و قالب پیام‌های امن**:
   - بررسی عضویت کاربران در کانال‌های اجباری تلگرام با کش قابل تنظیم (پیش‌فرض ۲۴ ساعت بر اساس کلید `(user_id, channel_id)`) برای جلوگیری از محدودیت نرخ (`Rate Limit`) تلگرام در مقیاس ۱۰۰٬۰۰۰ کاربر فعال روزانه.
   - سیاست `Fail-Closed` در صورت خطای API تلگرام و موتور قالب پیام فارسی با لیست مجاز متغیرها (`allowed_placeholders`) بدون هیچ‌گونه اجرای کد (`eval`/`exec`).

8. **دو پنل مدیریت کامل (وب RTL + تلگرام) و کنترل دسترسی مبتنی بر نقش (`RBAC`)**:
   - تعریف ۷ نقش پیش‌فرض (`super_admin`, `operations_admin`, `finance_admin`, `inventory_admin`, `support_admin`, `marketing_admin`, `read_only_auditor`) و ۲۲ مجوز دقیق.
   - محافظت سیستمی از حذف یا غیرفعال‌سازی آخرین `super_admin` فعال.

### ۳. ساختار پوشه‌ها و فایل‌های پروژه

```text
bot_project/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   └── app.py                             # وب‌سرویس FastAPI (تمامی ۹ بخش پنل وب + وبهوک تلگرام)
│   │   ├── bot/
│   │   │   ├── telegram_bot.py                    # منطق ربات مشتری فارسی و پنل مدیریت کامل تلگرام
│   │   │   └── runner.py                          # پردازنده آپدیت‌های تلگرام (Polling و Webhook امن)
│   │   ├── core/
│   │   │   ├── config.py                          # تنظیمات محیطی و توابع واحد پولی IRT
│   │   │   └── security.py                        # رمزنگاری، هش رمز عبور، توکن نشست، SSRF و RBAC
│   │   ├── models/
│   │   │   └── database.py                        # مدیریت دیتابیس رابطه‌ای، مهاجرت خودکار و تراکنش ACID
│   │   └── services/
│   │       ├── rbac_audit.py                      # مدیریت کاربران، مدیران، نقش‌ها و لاگ ممیزی سانسورشده
│   │       ├── catalog_pricing.py                 # دسته‌بندی، محصولات، تخفیف و پیش‌فاکتور تصادفی پایدار
│   │       ├── inventory.py                       # مدیریت موجودی، تشخیص تکراری SHA-256 و درون‌ریزی اتمیک
│   │       ├── wallet_payments.py                 # کیف پول، دفتر کل دوطرفه، واریز دستی و درگاه پرداخت
│   │       ├── orders_provisioning_referrals.py   # سفارش‌ها، تامین اشتراک، دعوت چهارگانه، کش کانال و قالب‌ها
│   │       └── live_integrations.py               # آداپتورهای HTTP واقعی مرزبان، سنایی (3x-ui)، زرین‌پال و زیبال
│   ├── migrations/
│   │   └── 001_initial_schema.sql                 # اسکیمای کامل جداول پایگاه داده PostgreSQL / SQLite
│   └── tests/
│       └── test_full_platform.py                  # مجموعه کامل تست‌های واحد، یکپارچگی، هم‌زمانی و امنیت
├── frontend/
│   ├── package.json                               # تنظیمات پروژه Next.js + React + TypeScript
│   └── src/
│       ├── lib/api.ts                             # کلاینت ارتباط با API و فرمت‌دهی مبالغ فارسی
│       ├── components/AdminTabs.tsx               # کامپوننت‌های تعاملی تب‌های مدیریت وب
│       └── pages/index.tsx                        # صفحه اصلی پنل مدیریت وب فارسی و راست‌به‌چپ (RTL)
├── deploy/
│   ├── Dockerfile.backend                         # ایمیج داکر بک‌اند (Python 3.12 + Uvicorn)
│   └── Dockerfile.frontend                        # ایمیج داکر فرانت‌اند (Node 20 + Next.js)
├── scripts/
│   ├── backup_postgres.sh                         # اسکریپت پشتیبان‌گیری فشرده PostgreSQL با چک‌سام SHA-256
│   └── load_test_benchmark.py                     # بنچمارک بار و اندازه‌گیری ظرفیت و تاخیر (p50/p95/p99)
├── docs/                                          # مستندات کامل معماری، دیتابیس، امنیت، API، تست و عملیات
├── docker-compose.yml                             # راه‌اندازی کامل Postgres + Redis + Backend + Frontend
├── .env.example                                   # الگوی متغیرهای محیطی
└── README.md                                      # مستندات جامع دوگانه (فارسی و انگلیسی)
```

### ۴. راهنمای گام‌به‌گام نصب و اجرا (How to Run)

#### روش اول: استقرار کامل با Docker Compose (پیشنهادی برای سرور عملیاتی)
1. فایل متغیرهای محیطی را بسازید و کلیدهای امنیتی را مقداردهی کنید:
   ```bash
   cp .env.example .env
   ```
   مقادیر زیر را در `.env` تنظیم کنید:
   - `APP_ENV=production`
   - `ALLOW_FAKE_PROVIDERS=false`
   - `SECRET_KEY` و `ENCRYPTION_MASTER_KEY` (حداقل ۳۲ کاراکتر تصادفی امن)
   - `TELEGRAM_BOT_TOKEN` (توکن دریافتی از `@BotFather`)
   - `TELEGRAM_WEBHOOK_SECRET` (توکن مخفی برای تایید هدر وبهوک تلگرام)
2. سرویس‌ها را بیلد و اجرا کنید:
   ```bash
   docker compose up -d --build
   ```
   - وب‌سرویس بک‌اند و وبهوک روی پورت `8000` (`http://localhost:8000/healthz`) و پنل مدیریت وب روی پورت `3000` (`http://localhost:3000`) در دسترس خواهند بود.

#### روش دوم: اجرای مستقیم محلی (Local Development & Testing)
1. **اجرای وب‌سرویس بک‌اند (FastAPI)**:
   ```bash
   PYTHONPATH=. uvicorn backend.app.api.app:create_fastapi_app --factory --host 0.0.0.0 --port 8000 --reload
   ```
2. **ساخت اولین مدیر ارشد (`Super Administrator Bootstrap`)**:
   ```bash
   PYTHONPATH=. python3 -c "
   from backend.app.bot.telegram_bot import ApplicationContainer
   c = ApplicationContainer('vpnstore.db')
   admin = c.rbac.bootstrap_super_admin(telegram_id=123456789, username='admin', password='YourStrongPassword!123')
   print('Bootstrapped Super Admin:', admin)
   "
   ```
3. **اجرای پنل مدیریت وب (Next.js RTL)**:
   ```bash
   cd frontend
   npm install
   npm run dev
   ```
4. **اجرای مجموعه کامل تست‌های خودکار (Unit, Concurrency, Integration & Security)**:
   ```bash
   PYTHONPATH=. python3 backend/tests/test_full_platform.py
   ```
5. **اجرای بنچمارک بار و ظرفیت (`Load Test`)**:
   ```bash
   PYTHONPATH=. python3 scripts/load_test_benchmark.py
   ```

## 🌐 Part 2: Complete English Documentation (Features, Architecture & Execution Guide)

### 1. Overview
This repository contains a **Production-Grade Telegram VPN Store** engineered with a modular Python backend (`FastAPI`), a Persian customer-facing Telegram bot, a **full-parity Telegram Administration Panel**, and a **Persian RTL Next.js Web Administration Panel**. Both administrative interfaces invoke the exact same backend service layer and enforce strict server-side Role-Based Access Control (`RBAC`).

### 2. Core Features & Engineering Guarantees

- **Strict Integer IRT Currency & Immutable Double-Entry Financial Ledger**:
  - All monetary values are stored and processed as 64-bit integers in **Iranian Tomans (`IRT`, where `1 IRT = 10 IRR`)**. Floating-point monetary values are rejected at validation boundaries.
  - Every wallet credit or debit atomically records a transaction in `ledger_transactions` and balanced debit/credit rows in `ledger_entries`. Historical ledger entries are immutable; refunds and reward reversals use compensating ledger transactions.
  - Concurrency-safe conditional balance updates (`WHERE user_id = ? AND balance_irt >= ?`) guarantee that simultaneous purchase requests can never overdraft a wallet.

- **Configurable Randomized Pricing & Stable Quotes (`PriceQuote`)**:
  - Products support a configurable base price plus a bounded randomized step increment (`min_random_increment_irt`, `max_random_increment_irt`, `increment_step_irt`).
  - Generated quotes are persisted server-side with an expiration timestamp (`expires_at`). Callback retries, network timeouts, or bot restarts reuse the active quote rather than generating new random prices. Client-supplied price tampering is rejected.

- **VPN Configuration Inventory & Atomic Bulk Import**:
  - Validates configuration schemes (`vless://`, `vmess://`, `trojan://`, `ss://`, and `https://` subscription URLs).
  - Computes a `SHA-256` hash over normalized content to detect duplicates both within the uploaded batch and against existing database inventory.
  - Supports multi-delimiter parsing (`newline`, `,`, `;`, `|`), pre-commit line-by-line preview diagnostics, strict **Atomic Import Mode** (rolls back the entire batch if any invalid or duplicate entry exists), and **Partial Import Mode**.
  - Raw configurations and provider credentials are encrypted at rest (`enc_v1` authenticated stream encryption with `HMAC-SHA256` integrity tags).

- **Automated VPN Provisioning & Ambiguous Timeout Reconciliation**:
  - Includes production HTTP adapters (`MarzbanHTTPProviderAdapter` and `ThreeXUIHTTPProviderAdapter` in `backend/app/services/live_integrations.py`) alongside a development-only `FakeVPNProviderAdapter` (strictly blocked when `APP_ENV=production`).
  - Uses deterministic idempotency keys (`idempotency_key`) when calling external provider APIs outside database locks.
  - If an external provider times out after potentially creating a subscription, the job enters `ambiguous_reconcile` and queries the provider by its deterministic username before retrying, preventing duplicate subscription creation. Definite provider failures trigger an automatic, idempotent compensating wallet refund.

- **Manual Deposits & Automated Payment Gateways (`Zarinpal` & `Zibal`)**:
  - **Manual Deposits**: Customers submit deposit requests with tracking proofs; wallets are credited only after an authorized administrator approves the request under a concurrency-safe status transition (`WHERE status = 'pending'`).
  - **Automated Gateways**: Includes `ZarinpalHTTPGatewayAdapter` and `ZibalHTTPGatewayAdapter` with authoritative server-to-server payment verification, `IRR` to `IRT` normalization, and `HMAC-SHA256` webhook signature + timestamp replay verification.

- **4-Tier Referral & Milestone Approval System (`N`, `W`, `P`, `M & K`)**:
  - Uses cryptographically opaque referral codes (`ref_<token>`), blocks self-referrals by `telegram_id`, and prevents overwriting existing attribution on repeated `/start` commands.
  - Supports **Standard Referrer Reward (`N`)**, **New-User Joining Reward (`W`)**, **First Verified Payment Reward (`P`)**, and **Milestone Bonus (`M` eligible referrals -> `K` IRT bonus)** requiring explicit administrator review and approval.

- **Mandatory Channel Membership Caching & Safe Message Templates**:
  - Caches Telegram channel membership checks per `(user_id, channel_id)` (default 24h TTL) with single-flight lock deduplication and fail-closed error handling.
  - Renders customizable Persian message templates using a strict variable allowlist without `eval`/`exec` or format-string injection vulnerabilities.

### 3. Quick Start & Execution Commands

#### A. Run Automated Test Suite (Unit, Integration, Concurrency, Bot, API & Security)
```bash
PYTHONPATH=. python3 backend/tests/test_full_platform.py
```
All 10 test suites (including multi-threaded concurrency tests competing for a single inventory item, limited wallet balance, simultaneous manual deposit approvals, and live HTTP adapter verification) execute offline and pass (`OK`).

#### B. Run Load & Capacity Benchmark
```bash
PYTHONPATH=. python3 scripts/load_test_benchmark.py
```
Simulates 200 concurrent full user journeys (`/start` onboarding -> wallet credit -> randomized quote generation -> transactional purchase) across 16 worker threads and outputs throughput (`journeys_per_second`) and latency percentiles (`p50`, `p95`, `p99`).

#### C. Start Production Stack via Docker Compose
```bash
cp .env.example .env
# Edit .env with production secrets and set APP_ENV=production, ALLOW_FAKE_PROVIDERS=false
docker compose up -d --build
```

#### D. Database Backup & Checksum Verification
```bash
DATABASE_URL="postgresql://vpnstore:password@localhost:5432/vpnstore" ./scripts/backup_postgres.sh
```
