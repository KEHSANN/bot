# مستندات وب‌سرویس (HTTP REST API Documentation)

اپلیکیشن FastAPI در `backend/app/api/app.py` مسیرهای زیر را ارائه می‌دهد:

| متد | مسیر | مجوز مورد نیاز | توضیح |
| :--- | :--- | :--- | :--- |
| `GET` | `/healthz` | عمومی | بررسی سلامت سرویس و واحد پولی پایه (`IRT`) |
| `POST` | `/api/admin/login` | عمومی | احراز هویت مدیر وب و دریافت توکن نشست امضاشده |
| `GET` | `/api/admin/dashboard` | `orders.read` | دریافت آمار تفکیک‌شده مالی، سفارش‌ها و تعهدات کیف پول |
| `POST` | `/api/admin/categories` | `products.manage` | ایجاد دسته‌بندی محصول جدید |
| `POST` | `/api/admin/products` | `products.manage` | ایجاد محصول با تنظیمات قیمت‌گذاری تصادفی |
| `POST` | `/api/admin/inventory/preview` | `inventory.import` | پیش‌نمایش و خطایابی درون‌ریزی دسته‌ای کانفیگ‌ها |
| `POST` | `/api/admin/inventory/import` | `inventory.import` | ثبت اتمیک یا جزئی موجودی کانفیگ‌ها |
| `POST` | `/api/admin/deposits/{id}/review` | `payments.approve_manual` | تایید یا رد درخواست واریز دستی با ثبت دلیل |
| `POST` | `/api/admin/milestones/{id}/review` | `referrals.approve_milestone` | تایید یا رد پاداش حدنصاب دعوت (`K`) با ثبت دلیل |
| `POST` | `/api/admin/wallets/adjust` | `wallets.adjust` | اصلاح مدیریتی موجودی کیف پول با ثبت دلیل و کلید یکتا |
| `GET` | `/api/admin/audit-logs` | `audit.read` | مشاهده لاگ‌های ممیزی سانسورشده |
