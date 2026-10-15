# طراحی پایگاه داده و دفتر کل مالی (Database & Financial Ledger Design)

اسکیمای کامل پایگاه داده در فایل `backend/migrations/001_initial_schema.sql` تعریف شده است.

## قواعد کلیدی پایگاه داده

- **واحد پولی پایه (`IRT`)**: تمامی ستون‌های مالی (`balance_irt`, `amount_irt`, `base_price_irt`, `final_amount_irt`) از نوع `BIGINT` هستند و هیچ مقدار اعشاری (`FLOAT`/`DOUBLE`) در سیستم مجاز نیست.
- **تغییرناپذیری دفتر کل (`ledger_transactions` و `ledger_entries`)**: هیچ رکوردی در دفتر کل ویرایش یا حذف نمی‌شود. اصلاحات مالی یا لغو سفارش‌ها صرفاً از طریق تراکنش جبرانی (`refund_credit` یا `referral_reversal`) انجام می‌گیرند.
- **کلیدهای یکتای جلوگیری از تکرار (`idempotency_key`)**: جداول `ledger_transactions`، `payments`، `orders`، `provisioning_jobs` و `referral_rewards` دارای قید `UNIQUE (idempotency_key)` هستند.
