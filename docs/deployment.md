# راهنمای استقرار و پیکربندی (Deployment Guide)

## ۱. راه‌اندازی با Docker Compose

1. فایل `.env.example` را به `.env` کپی کرده و مقادیر واقعی `SECRET_KEY`، `ENCRYPTION_MASTER_KEY` و `TELEGRAM_BOT_TOKEN` را قرار دهید.
2. در محیط عملیاتی (`APP_ENV=production`) حتماً `ALLOW_FAKE_PROVIDERS=false` تنظیم شود.
3. سرویس‌ها را اجرا کنید:

```bash
docker compose up -d --build
```
