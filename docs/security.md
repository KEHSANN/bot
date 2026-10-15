# مهندسی امنیت و مدل تهدید (Security Engineering & Threat Model)

## ۱. کنترل‌های امنیتی پیاده‌سازی‌شده

- **احراز هویت و کنترل دسترسی (`RBAC`)**: تمامی عملیات حساس در API وب و بات تلگرام در سمت سرور با `require_permission` و `require_telegram_admin_permission` بررسی می‌شوند.
- **محافظت مالی در برابر شرایط مسابقه (Race Conditions)**: کسر موجودی کیف پول با شرط اتمیک `WHERE user_id = ? AND balance_irt >= ?` و تخصیص موجودی کانفیگ با `WHERE id = ? AND status = 'available'` داخل تراکنش قفل‌شده انجام می‌شود.
- **رمزنگاری اطلاعات حساس در حالت سکون (Encryption at Rest)**: تمامی کانفیگ‌های VPN و کلیدهای سرویس‌دهندگان با کلید اصلی (`ENCRYPTION_MASTER_KEY`) و تگ احراز اصالت `HMAC-SHA256` رمزنگاری می‌شوند (`enc_v1:...`).
- **جلوگیری از SSRF و تزریق کد در قالب‌ها**: آدرس سرویس‌دهندگان خارجی با `validate_provider_url` در برابر IPهای داخلی و متادیتا بررسی می‌شود و قالب پیام‌ها تنها متغیرهای مجاز (`allowed_placeholders`) را جایگزین می‌کنند.
