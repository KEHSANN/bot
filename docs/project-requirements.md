# MASTER PROMPT — Production-Grade Telegram VPN Store

## Complete Implementation, Automated Testing, Referral Rewards, Dual Admin Panels, Security, Scalability, and Mandatory GitHub Publishing

You are a senior software architect, backend engineer, frontend engineer, DevOps engineer, security engineer, database engineer, and QA automation engineer working as one coordinated engineering team.

Your task is to design, implement, test, document, and prepare a production-grade Telegram VPN sales platform with a powerful Telegram bot, a complete web administration panel, a full-featured Telegram administration panel, wallet and payment systems, configurable products, inventory management, automated VPN provisioning, referral rewards, and operational monitoring.

This is a real software engineering project, not a prototype, tutorial, or superficial demo.

You must build a coherent, maintainable, secure, scalable, fully tested application. Do not merely produce a plan, architecture document, mockup, or code snippets. Inspect the existing repository, implement the actual software, run tests, fix failures, document the results, and publish completed implementation phases to GitHub.

---

# 1. Project Goals and Non-Negotiable Requirements

Build a Telegram-first VPN sales platform that supports:

- Selling predefined VPN configurations and subscriptions.
- Automatically provisioning VPN subscriptions through external provider APIs.
- Managing manually imported configurations and subscription inventory.
- Product categories, configurable plans, economic products, and multiple service types.
- User accounts and purchase history.
- A wallet for every user.
- Manual wallet deposits approved by administrators.
- Automatic deposits through supported payment gateways.
- Transactional purchases with strict balance and inventory consistency.
- Configurable pricing, including randomized price increments.
- Referral links and configurable rewards for both referrers and newly invited users.
- Referral milestone rewards that require administrator approval.
- A Telegram customer-facing bot.
- A complete web administration panel.
- A complete Telegram administration panel with operational feature parity with the web panel.
- Mandatory Telegram channel membership with configurable rules.
- Configurable messages, buttons, and user workflows.
- Customer support, coupons, campaigns, reporting, and audit logs.
- Provider management and secure secret storage.
- Comprehensive automated testing and continuous integration.
- Secure deployment, monitoring, backup, and recovery procedures.

## 1.1. Target Scale

Design for a target of **100,000 daily active users**.

Do not assume that this capacity is achieved merely because the application starts successfully.

Use appropriate architecture, database indexing, connection pooling, caching, queues, horizontal scaling, rate limiting, and load testing.

Measure and document actual performance. Never claim that the system supports 100,000 daily active users unless appropriate load tests and capacity analysis support that conclusion.

## 1.2. Language and User Experience

- Primary language: Persian.
- Telegram messages must support natural Persian text.
- The web administration interface must be Persian-first and RTL.
- Use a consistent, professional interface across the bot and web panel.
- Support Unicode correctly.
- Format monetary values, dates, numbers, and messages consistently.
- Make error messages understandable to ordinary users.
- Keep customer-facing interactions simple and fast.
- Provide administrators with detailed operational information without overwhelming ordinary customers.

## 1.3. Core Engineering Principles

- Financial correctness comes before convenience.
- Security must be designed into the architecture from the beginning.
- All financial and inventory operations must be concurrency-safe.
- Every externally retried operation must have an appropriate idempotency strategy.
- Do not duplicate business logic between the web panel and Telegram admin panel.
- Use database constraints in addition to application-level validation.
- Never trust data supplied by the client, Telegram callback data, payment callbacks, or external providers without validation.
- Do not fabricate successful test results, payment confirmations, provider responses, or deployment outcomes.
- Do not use fake implementations in production paths.
- Clearly separate production functionality from development-only mocks.

---

# 2. Mandatory Repository Inspection and Development Strategy

Before implementing anything:

1. Inspect the existing GitHub repository:
   `https://github.com/KEHSANN/bot`
2. Examine its branches, commit history, current files, project structure, README, dependency configuration, and existing implementation.
3. Identify whether the repository is empty, partially implemented, or already contains a working application.
4. Preserve existing functionality unless a documented, justified change is necessary.
5. Identify existing constraints and avoid blindly replacing the repository.
6. Create an implementation plan based on the actual repository state.
7. Record architectural decisions in `docs/architecture.md`.
8. Maintain a current implementation checklist in `docs/implementation-progress.md`.

Do not stop after creating a plan. Once the repository has been inspected and a safe baseline established, begin implementing the project.

If requirements conflict with existing code, document the conflict, choose the safest compatible approach, and proceed without silently discarding existing work.

---

# 3. Recommended Technology Stack

Use the following stack unless repository constraints or a demonstrably superior technical reason require a documented alternative.

## 3.1. Backend

- Python 3.12 or a currently supported compatible Python version.
- FastAPI for HTTP APIs.
- aiogram 3 for the Telegram bot.
- Pydantic v2 for schemas and validation.
- SQLAlchemy 2 with asynchronous database access.
- Alembic for database migrations.
- PostgreSQL as the primary database.
- Redis for caching, rate limiting, distributed coordination where appropriate, and queue infrastructure.
- A reliable background job system such as ARQ or another well-supported Redis-compatible solution.
- HTTPX for asynchronous external HTTP requests.
- Pytest and pytest-asyncio for automated backend tests.

Pin compatible dependency versions and maintain reproducible dependency installation.

## 3.2. Web Administration Panel

- Next.js.
- React.
- TypeScript with strict type checking.
- A consistent component system.
- Responsive layout for desktop, tablet, and mobile.
- Persian-first RTL layout.
- Server-side authorization for protected operations.
- Accessible forms, dialogs, tables, and navigation.
- Proper loading, empty, success, and error states.

Choose and document a suitable styling and form-validation stack.

## 3.3. Infrastructure

- Docker and Docker Compose for local development and reproducible deployment.
- PostgreSQL.
- Redis.
- A reverse proxy such as Nginx when appropriate.
- Separate scalable API, bot, and worker processes.
- Structured application logs.
- Health and readiness checks.
- Metrics and error monitoring.
- Automated database backups.
- CI workflows through GitHub Actions.

Do not introduce unnecessary microservices. Start with a modular monolith and independently scalable workers unless measured requirements justify a different architecture.

---

# 4. System Architecture

Use clear boundaries between business domains.

Recommended modules:

- `users`
- `authentication`
- `authorization`
- `admin`
- `catalog`
- `inventory`
- `pricing`
- `orders`
- `wallet`
- `ledger`
- `payments`
- `payment_gateways`
- `vpn_providers`
- `provisioning`
- `deliveries`
- `referrals`
- `membership`
- `messaging`
- `workflow_engine`
- `campaigns`
- `coupons`
- `support`
- `notifications`
- `reports`
- `settings`
- `audit`
- `observability`

Suggested repository structure:

```text
bot/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── bot/
│   │   ├── core/
│   │   ├── db/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── repositories/
│   │   ├── services/
│   │   ├── workers/
│   │   └── integrations/
│   ├── migrations/
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   ├── public/
│   └── package.json
├── deploy/
├── scripts/
├── docs/
├── .github/
│   └── workflows/
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

Adapt this structure when necessary, but preserve separation of concerns.

Business logic must reside in reusable services rather than Telegram handlers or frontend components.

Both administration interfaces must call the same backend services and enforce the same business rules.

---

# 5. Database Design

Use PostgreSQL as the source of truth.

Create normalized, indexed database models and explicit foreign-key relationships. Use appropriate unique constraints, check constraints, timestamps, and transaction boundaries.

The schema should cover at least the following entities.

## 5.1. Users and Administrators

- `users`
- `admin_users`
- `roles`
- `permissions`
- `role_permissions`
- `admin_role_assignments`
- `admin_sessions`, where applicable
- `user_status_history`

A user should have a stable internal ID and a unique Telegram identifier where available.

Support active, blocked, suspended, and other explicitly defined states.

Do not use a Telegram username as the permanent user identifier.

## 5.2. Products and Inventory

- `product_categories`
- `products`
- `product_plans` or an equivalent plan model
- `inventory_batches`
- `inventory_items`
- `inventory_import_jobs`
- `inventory_reservations`
- `provider_accounts`
- `provider_requests`

Products should support configurable names, descriptions, category, price, duration, traffic quota, device limits, service type, inventory mode, availability, and metadata.

Use flexible but validated product attributes when different VPN providers expose different capabilities.

## 5.3. Orders and Deliveries

- `orders`
- `order_items`
- `order_status_history`
- `deliveries`
- `idempotency_records`
- `outbox_events`
- `provisioning_jobs`

Preserve the original quoted price and relevant product details for each order.

## 5.4. Wallet and Financial Ledger

- `wallets`
- `ledger_transactions`
- `ledger_entries`, if using a double-entry ledger
- `wallet_adjustments`
- `wallet_withdrawals`, only if withdrawals are supported

Use integer amounts in the smallest explicitly defined application currency unit.

Ledger entries must be immutable. Corrections should use compensating transactions rather than editing historical entries.

## 5.5. Payments

- `payment_methods`
- `payments`
- `payment_events`
- `payment_verification_attempts`
- `refunds`
- `manual_deposit_requests`

Store external payment identifiers, amounts, currency, status, provider references, timestamps, and idempotency information.

Never store card security codes or other prohibited payment credentials.

## 5.6. Referrals

- `referral_codes`
- `referrals`
- `referral_reward_rules`
- `referral_rewards`
- `referral_milestone_requests`
- `referral_review_history`

Prevent duplicate referral attribution and duplicate reward issuance through database constraints and transactional logic.

## 5.7. Configuration and Communication

- `message_templates`
- `workflow_definitions`
- `workflow_steps`
- `workflow_buttons`
- `mandatory_channels`
- `membership_cache`
- `campaigns`
- `campaign_recipients`
- `notification_jobs`

## 5.8. Additional Administration

- `support_tickets`
- `support_messages`
- `coupons`
- `coupon_redemptions`
- `application_settings`
- `audit_logs`
- `admin_action_requests`
- `system_health_events`

You may adjust the exact schema based on the final architecture. Document important deviations.

Do not store critical business state only in Redis or process memory.

---

# 6. Currency and Financial Rules

The application currency is **IRT**.

Define and document exactly what one stored monetary unit represents. All API payloads, database records, gateway adapters, user interfaces, and admin interfaces must use the same documented convention.

Do not mix Iranian rials, tomans, and IRT amounts.

- Store money as integers, never floating-point values.
- Validate currency and amount at every integration boundary.
- Normalize gateway-specific amounts before recording them.
- Display prices consistently in Persian.
- Use explicit conversion functions only when required by a payment provider.
- Record the original provider amount and normalized internal amount when useful for reconciliation.
- Reject malformed, negative, overflowed, or inconsistent monetary values.
- Never silently reinterpret an amount supplied by an external gateway.

Implement automated tests for currency conversion and rounding rules.

---

# 7. Product Catalog and Pricing Engine

Administrators must be able to create, edit, disable, and organize products.

Support:

- Product categories.
- Economic or budget products.
- Multiple subscription durations.
- Different traffic limits.
- Different device limits.
- Manual inventory products.
- Automatically provisioned products.
- Configurable product descriptions and display order.
- Product availability and stock status.
- Optional promotional pricing.
- Product-specific restrictions.
- Configurable purchase limits.

## 7.1. Randomized Pricing

The pricing system must support a configurable randomized price increment.

For example:

- Base price: configurable.
- Minimum random increment: configurable.
- Maximum random increment: configurable.
- Randomization method and allowed increment precision: configurable.

Define the algorithm explicitly. Avoid generating arbitrary floating-point prices.

When a user requests a purchase quote:

1. Validate the product and eligibility.
2. Calculate the final price.
3. Store the resulting quote with its amount, currency, product reference, pricing-rule version, and expiration time if applicable.
4. Use that same quote throughout the purchase attempt.
5. Never generate a new random price because the user retries a callback, the bot restarts, or a network request is repeated.
6. Prevent the client from changing the quoted price.
7. Require a new quote only when the previous quote expires or the user explicitly starts a new purchase.

Do not use a new random increment to bypass duplicate-order or idempotency protection.

---

# 8. Telegram Customer Bot

Build a polished, intuitive Telegram bot in Persian.

## 8.1. Main Menu

Include configurable menu entries such as:

- خرید VPN
- محصولات اقتصادی
- دسته‌بندی محصولات
- کیف پول
- شارژ کیف پول
- سفارش‌های من
- لینک دعوت و پاداش‌ها
- راهنمای استفاده
- پشتیبانی
- قوانین و شرایط

Administrators must be able to configure menu visibility, labels, order, and destination where appropriate.

## 8.2. User Onboarding

On `/start`:

1. Create or update the user's profile safely.
2. Parse referral attribution if a valid referral parameter exists.
3. Check mandatory channel membership according to configured rules.
4. Present the appropriate onboarding or main-menu screen.
5. Avoid duplicating rewards, accounts, referrals, or other effects if `/start` is repeated.

Support users who return after a long period and users who interact through multiple bot sessions.

## 8.3. Product Purchase Flow

The purchase flow should be simple and reliable:

1. Browse categories.
2. Select a product.
3. View the product description and terms.
4. Obtain a server-generated price quote.
5. Confirm the purchase.
6. Check wallet balance and product eligibility.
7. Reserve or atomically claim inventory where necessary.
8. Deduct funds transactionally.
9. Deliver an existing configuration or initiate provider provisioning.
10. Display the result and order details.
11. Save the purchase in order history.

If the wallet balance is insufficient:

- Explain the shortage clearly.
- Show the required amount and current balance.
- Offer an immediate wallet recharge button.
- Preserve the intended product and quote when safely possible.
- Revalidate price validity and availability when the user returns.

Never charge the user twice because of repeated button clicks, callback retries, or network timeouts.

## 8.4. Order History

Users must be able to view:

- Previous orders.
- Product names.
- Purchase timestamps.
- Amount paid.
- Order status.
- Delivery status.
- Subscription expiration or quota information when available.
- Available support actions.

Paginate long histories.

## 8.5. Error Handling

Provide friendly Persian messages for:

- Temporary provider failures.
- Payment verification delays.
- Out-of-stock products.
- Expired quotes.
- Invalid referral links.
- Missing channel membership.
- Insufficient wallet balance.
- Duplicate actions.
- Temporary maintenance.
- Unexpected errors.

Never expose stack traces, secrets, internal URLs, or sensitive provider responses to customers.

---

# 9. Mandatory Telegram Channel Membership

Administrators must be able to configure one or more mandatory channels.

Support:

- Adding and removing channels.
- Public and supported private channel configurations.
- Join buttons.
- Membership verification.
- Configurable recheck intervals.
- A verification or retry button.
- Clear Persian explanations when membership is required.

## 9.1. Membership Caching

Do not perform an expensive Telegram membership request on every interaction.

Cache membership results for a configurable period, with a sensible default such as 24 hours.

Requirements:

- Cache key must identify both the user and the channel.
- Invalidate or refresh the cache when appropriate.
- Handle Telegram API failures without incorrectly treating an unknown membership state as verified.
- Define behavior when the Telegram bot lacks permission to inspect membership.
- Use bounded cache entries and suitable expiration.
- Allow administrators to force a recheck when necessary.

Use short-lived request deduplication or locking where it prevents many simultaneous identical membership checks.

Document the consistency trade-off created by the cache.

---

# 10. Wallet System

Every registered user must have a wallet.

Implement:

- Current balance.
- Wallet transaction history.
- Deposits.
- Purchase debits.
- Refunds.
- Referral credits.
- Administrative adjustments.
- Transaction statuses and references.
- Transaction details and timestamps.

## 10.1. Financial Invariants

- A user cannot spend more than the available balance.
- Concurrent purchases cannot create an unauthorized negative balance.
- Each financial operation must have a unique idempotency key or equivalent deduplication mechanism.
- Wallet mutations and their corresponding ledger entries must commit atomically.
- A transaction cannot be applied twice because a webhook or job is repeated.
- Administrative adjustments must require an explicit reason and generate an audit event.
- Historical ledger entries must never be silently overwritten or deleted.
- Balance calculations must be reconcilable against the ledger.
- Financial failures must roll back all incomplete database mutations.

Use appropriate row locking, conditional updates, or another verified concurrency-control technique.

Never rely solely on checking the balance in application memory before charging the wallet.

---

# 11. Manual Wallet Deposits

Support manual deposits through administrator-configured payment instructions.

Administrators can configure:

- Payment method name.
- Bank card or wallet information.
- Account holder or recipient description.
- Instructions.
- Minimum and maximum amounts.
- Whether the method is enabled.
- Optional payment proof requirements.
- Expected verification procedure.

A manual deposit flow should:

1. Let the user select an amount.
2. Display the configured instructions.
3. Create a pending deposit request.
4. Accept a payment reference or proof when required.
5. Notify authorized administrators.
6. Allow an administrator to approve or reject the request.
7. Record the decision and reason.
8. Credit the wallet only once after valid approval.
9. Notify the user of the result.

Manual deposits must never be credited merely because a user submits a screenshot or claims to have paid.

Prevent two administrators from approving the same deposit and issuing duplicate credits.

---

# 12. Automatic Payment Gateways

Create a payment-provider adapter interface so additional gateways can be added without rewriting the wallet system.

Support multiple configurable gateways where practical.

Each gateway adapter should implement operations such as:

- Create payment request.
- Return the payment URL or supported payment instructions.
- Verify payment status using the provider's authoritative API.
- Process authenticated callbacks or webhooks.
- Normalize provider statuses.
- Support refunds when the provider permits them.
- Reconcile uncertain transactions.

## 12.1. Payment Verification

Never trust a browser redirect or client-supplied success flag as proof of payment.

Before crediting a wallet:

- Verify the transaction with the provider.
- Validate the provider transaction ID.
- Validate amount and currency.
- Confirm the payment belongs to the correct internal request.
- Confirm that the transaction has not already been credited.
- Check that the payment is in an acceptable final state.
- Record the verification result.
- Use idempotent processing and transactional ledger updates.

Authenticate webhooks according to the provider's official mechanism. Validate signatures, timestamps, and replay protection where supported.

If a provider times out or returns an ambiguous result, mark the payment for reconciliation instead of guessing.

Use explicit payment states, for example:

`created`, `pending`, `verifying`, `succeeded`, `failed`, `expired`, `cancelled`, `refunded`, and `requires_review`.

Do not allow invalid or backward status transitions.

## 12.2. Provider Secrets

Store payment credentials securely outside source code.

Encrypt sensitive integration credentials at rest using a documented key-management strategy when persisted in the application database.

Do not display complete secrets in the web panel or Telegram admin panel.

Allow safe rotation and disablement of credentials.

---

# 13. VPN Configuration Inventory

The platform must support manually imported inventory as well as automatically provisioned subscriptions.

## 13.1. Manual Inventory

Administrators must be able to add configurations individually or import them in bulk.

Support configurable inventory types and validated fields for the chosen service format.

Examples may include supported URI-style configurations, subscription URLs, or provider-specific identifiers.

Do not assume that every arbitrary string is a valid configuration.

Apply validation appropriate to each supported format, without exposing secrets in logs.

## 13.2. Bulk Import

Provide a robust bulk-import system with:

- Paste-based import.
- File import where appropriate.
- Configurable delimiter: newline, comma, semicolon, or a custom supported separator.
- Preview before importing.
- Total-entry count.
- Valid-entry count.
- Invalid-entry count.
- Duplicate count.
- Clear error details per line or item.
- Duplicate detection within the current batch.
- Duplicate detection against existing inventory.
- Import summaries.
- Safe cancellation before commit.

Normalize content only for validation and duplicate detection according to the documented rules. Preserve the original deliverable when necessary.

Use a cryptographic hash of normalized content, plus database uniqueness constraints, to detect duplicates.

## 13.3. Atomic Import

Use transactional or explicitly staged import behavior.

The administrator must know whether the operation is atomic or partially accepted.

For the default atomic mode:

- Validate the entire batch.
- Preview all errors and duplicates.
- Require confirmation.
- Commit all accepted entries together.
- If the transaction fails, do not leave a partially imported batch.

If partial import is offered as a separate option, label it clearly and report exactly which entries were accepted or rejected.

Track import batch IDs, importing administrator, timestamps, counts, and results.

## 13.4. Inventory Assignment

An inventory item must not be delivered to two customers unless the product explicitly supports sharing.

Use row locking, conditional state transitions, unique constraints, or another concurrency-safe mechanism.

Track states such as:

`available`, `reserved`, `assigned`, `disabled`, and `invalid`.

Handle reservation expiration and recovery of abandoned reservations.

Do not expose configuration secrets in administrative logs, error messages, or analytics.

---

# 14. Automated VPN Provisioning

Create a common provider adapter interface.

Example operations:

- Validate provider configuration.
- Fetch available plans.
- Create a subscription.
- Extend a subscription.
- Retrieve subscription details.
- Revoke a subscription.
- Retrieve traffic usage and expiration, if supported.
- Reconcile provisioning status.

Implement real integrations only when the provider's official API documentation and credentials are available.

Include a clearly labeled fake provider for local development and automated tests.

Never silently use a fake provider in production.

## 14.1. Provisioning Reliability

External API calls may time out after the provider has already created a subscription.

Therefore:

- Do not blindly retry non-idempotent provisioning requests.
- Use provider-supported idempotency keys where available.
- Persist provisioning attempts and external request identifiers.
- Reconcile ambiguous outcomes using provider queries.
- Distinguish definite failure from unknown outcome.
- Keep slow external calls outside long-running database transactions.
- Use background jobs for operations that may take significant time.
- Retry transient failures with bounded exponential backoff and jitter.
- Send permanently failing operations to a reviewable dead-letter or failed-job workflow.

## 14.2. Order and Provisioning State

Track order, payment, inventory, and provisioning states independently where necessary.

For example:

- Order created.
- Payment or wallet charge confirmed.
- Inventory reserved or provisioning queued.
- Provisioning in progress.
- Delivery available.
- Delivery failed or requires reconciliation.

Use a saga or compensating-action strategy where operations span the database and external services.

If provisioning fails after the user has been charged, follow a documented recovery process:

- Retry safely if the outcome is known and retry is safe.
- Reconcile if the outcome is ambiguous.
- Refund or release funds through an idempotent compensating transaction when required.
- Notify the user and administrators.

Never refund and deliver twice due to a race condition.

---

# 15. Order Processing and Transactional Consistency

Implement a robust purchase orchestration service.

For a wallet-funded purchase, enforce the following logical guarantees:

1. Validate the quote, user, product, and eligibility.
2. Verify inventory or provisioning availability.
3. Prevent duplicate order creation.
4. Atomically reserve inventory when applicable.
5. Debit the wallet and record the corresponding ledger transaction in a database transaction.
6. Persist the order and durable work needed for delivery.
7. Commit the transaction.
8. Process external provisioning or delivery through an idempotent worker.
9. Update delivery state.
10. Notify the customer.
11. Reconcile failed or ambiguous operations.

The exact ordering may vary by product type, but financial and inventory invariants must always hold.

Use the transactional outbox pattern when it improves reliability between database commits and asynchronous job processing.

Never hold a database transaction open while waiting for a slow external provider.

Add recovery jobs for stuck orders, stale reservations, incomplete deliveries, and reconciliation-required payments.

---

# 16. Referral Links and Reward System

Implement a complete referral system with configurable rules.

Every eligible user can receive a unique referral link, for example:

`https://t.me/<bot_username>?start=ref_<opaque_referral_code>`

Use secure, non-guessable referral codes. Do not expose sequential database IDs as referral secrets.

## 16.1. Referral Attribution

When a new user arrives through a referral link:

- Validate the code.
- Associate the new account with the referring user.
- Store attribution in a way that cannot be changed by ordinary users.
- Reject self-referrals.
- Prevent duplicate attribution.
- Prevent repeated `/start` commands from creating extra referrals.
- Do not replace a valid original referrer because a different link is opened later.
- Define how existing users and previously registered accounts are handled.
- Track referral creation, eligibility, qualification, rewards, and review status separately.

Use the Telegram user ID as the identity for self-referral detection, not a mutable username.

## 16.2. Reward Types

The system must support the following configurable reward types.

### A. Standard Referrer Reward — N

For each eligible person invited, the referrer can receive a configurable amount `N`.

Administrators must be able to configure:

- Reward amount.
- Whether the reward is enabled.
- Eligibility conditions.
- Whether the reward is issued after registration, membership verification, first qualifying payment, or another explicitly configured event.
- Reward limits and anti-abuse rules.

Make the triggering event explicit. Do not accidentally pay the same referral reward at both registration and purchase unless separate reward rules are deliberately configured.

### B. New User Joining Reward — W

A newly invited user can receive a configurable reward `W`.

Administrators must be able to configure:

- Amount.
- Enablement.
- Qualification criteria.
- Whether channel membership is required.
- Whether the reward is immediate or delayed.
- Applicable eligibility and anti-abuse conditions.

Issue this reward at most once per eligible account.

### C. First Verified Payment Reward — P

When a referred user completes their first qualifying, successfully verified payment, the referrer may receive a configurable reward `P`.

Requirements:

- The payment must be verified through the configured payment verification mechanism.
- A failed, pending, cancelled, or unverified payment does not qualify.
- A duplicate payment webhook cannot issue duplicate rewards.
- Multiple successful payments by the same referred user must not trigger the first-payment reward repeatedly.
- Define whether refunded or reversed payments invalidate eligibility, and implement a documented compensation or review policy.
- Use a unique reward reference and transactional ledger entry.

### D. Referral Milestone Reward — M and K

Support a special reward requiring administrator approval.

Example:

- `M` = the configurable number of eligible referrals required to reach the milestone.
- `K` = the configurable reward amount or milestone bonus.

When a user reaches the configured milestone:

1. Calculate eligible referrals using a documented eligibility definition.
2. Create a milestone request.
3. Mark it as pending review.
4. Notify authorized administrators.
5. Display supporting statistics to reviewers.
6. Allow an authorized administrator to approve or reject the request.
7. Require a reason for rejection or manual override where appropriate.
8. Credit the reward only after valid approval.
9. Prevent repeated approval from crediting the reward twice.
10. Record the administrator, decision, timestamp, and audit event.

Make the threshold and reward configurable, including whether the milestone is one-time or repeatable.

Define whether each milestone uses total eligible referrals or referrals since the previous milestone.

Do not silently count unqualified or fraudulent accounts.

## 16.3. Referral Statistics for Users

Provide a referral page in the Telegram bot showing:

- Personal referral link.
- Copy/share action where supported.
- Total referrals.
- Eligible referrals.
- Pending referrals.
- Referrals with qualifying payments.
- Rewards earned.
- Rewards pending review.
- Reward history.
- Milestone progress.
- Referral terms.

Do not reveal another user's sensitive payment details.

## 16.4. Referral Administration

Both admin panels must support:

- Enable or disable the referral system.
- Configure reward `N`.
- Configure new-user reward `W`.
- Configure first-verified-payment reward `P`.
- Configure milestone threshold `M`.
- Configure milestone reward `K`.
- Configure qualification rules.
- Review individual referrals.
- Search and filter referrals.
- Inspect reward history.
- Approve or reject milestone requests.
- Detect suspicious patterns.
- Reverse an incorrect reward through a compensating ledger transaction with an audit trail.
- Export appropriate referral reports.

## 16.5. Referral Abuse Prevention

Implement safeguards for:

- Self-referrals.
- Duplicate accounts where detectable.
- Repeated reward claims.
- Replayed payment callbacks.
- Automated registration abuse.
- Suspicious referral bursts.
- Repeated reward reversal patterns.
- Manual reward manipulation.

Use rate limits, eligibility rules, audit events, and risk flags.

Do not claim that device fingerprinting or IP checks can perfectly identify a person. Treat signals as risk indicators rather than definitive proof.

Do not automatically punish legitimate users based only on an unreliable signal.

---

# 17. Configurable Messages and Workflow Engine

Administrators must be able to customize messages shown at important steps without editing source code.

Examples:

- Welcome message.
- Mandatory membership message.
- Main-menu introduction.
- Category description.
- Product details.
- Purchase confirmation.
- Insufficient-balance message.
- Deposit instructions.
- Pending-payment message.
- Payment success or failure.
- Delivery success.
- Delivery delay.
- Referral invitation.
- Referral reward notification.
- Milestone review result.
- Support response.
- Maintenance announcement.
- Campaign messages.

## 17.1. Message Templates

Support:

- Editable Persian text.
- Supported Telegram formatting.
- Validated placeholders.
- Preview before saving.
- Enable/disable controls.
- Version history or safe rollback where practical.

Use an explicit allowlist of template variables.

Never evaluate administrator-supplied text as Python, JavaScript, SQL, or arbitrary executable code.

Handle missing or malformed placeholders safely.

## 17.2. Buttons and Menus

Allow administrators to configure supported Telegram inline and reply keyboard buttons:

- Button text.
- Button action.
- Destination or internal workflow.
- Visibility.
- Display order.
- Relevant role or user conditions.

Validate destinations and callback actions.

Telegram keyboards do not support arbitrary button background colors. Do not promise custom button colors that the Telegram Bot API does not provide.

## 17.3. Workflow Engine

Implement reusable workflow definitions with validated steps, transitions, and actions.

Examples:

- Welcome → membership check → main menu.
- Product selection → quote → confirmation → payment or wallet purchase.
- Deposit request → pending verification → result.
- Referral milestone → pending review → approval or rejection.

Keep business rules in backend services. A workflow must not be able to bypass payment verification, authorization, or financial invariants.

---

# 18. Complete Web Administration Panel

Build a real administration application, not a collection of placeholder pages.

## 18.1. Dashboard

Display relevant metrics:

- Total users.
- Active users where reliably measurable.
- New registrations.
- Total orders.
- Successful orders.
- Pending orders.
- Failed deliveries.
- Revenue and deposits with clearly defined accounting semantics.
- Wallet liabilities and ledger reconciliation status where available.
- Referral statistics.
- Inventory availability.
- Provider health.
- Payment gateway health.
- Recent administrative activity.

Clearly distinguish gross payments, wallet deposits, order revenue, refunds, and other accounting categories. Do not present them as interchangeable metrics.

## 18.2. User Management

Provide:

- Search by Telegram ID and supported identifiers.
- Filters and pagination.
- User profile and registration date.
- Wallet balance.
- Order history.
- Payment history.
- Referral statistics.
- Account status.
- Administrative notes.
- Appropriate support history.

Sensitive actions such as blocking a user or adjusting a wallet must require the relevant permission and an audit record.

## 18.3. Product Management

Provide full CRUD operations, categories, product plans, pricing configuration, stock mode, availability, descriptions, and product-level settings.

Validate all values server-side.

## 18.4. Inventory Management

Provide:

- Manual insertion.
- Bulk import.
- Import preview.
- Duplicate detection.
- Batch history.
- Stock status.
- Inventory search.
- Assignment history.
- Failed-item inspection.
- Safe disabling or correction.
- Access-controlled viewing of sensitive configuration values.

## 18.5. Order Management

Provide:

- Search and filtering.
- Order details.
- Status history.
- Payment and delivery status.
- Provisioning attempts.
- Reconciliation actions.
- Safe retry controls.
- Refund or compensation actions where authorized.

Do not expose a generic button that blindly repeats an unsafe provisioning operation.

## 18.6. Wallet and Payment Administration

Provide:

- Wallet inspection.
- Ledger history.
- Manual deposit requests.
- Approve/reject actions.
- Automatic payment history.
- Gateway status.
- Verification details.
- Refund workflows.
- Reconciliation tools.
- Administrative adjustments with mandatory reasons.
- Duplicate payment detection.

## 18.7. Provider Management

Provide:

- Provider configuration.
- Connection status.
- Health checks.
- Supported plans.
- Request history.
- Error summaries.
- Safe credential updates and rotation.
- Enable/disable controls.

Never reveal complete secrets in forms, tables, logs, or API responses.

## 18.8. Referral Management

Implement all features in Section 16.

## 18.9. Membership and Messaging

Manage mandatory channels, cache settings, message templates, workflow definitions, menus, and buttons.

## 18.10. Campaigns and Support

Provide:

- Campaign creation and scheduling.
- Audience selection.
- Preview and test send.
- Delivery progress.
- Failure tracking.
- Rate-limited sending.
- Support tickets.
- Assignment to administrators.
- Status and priority.
- Internal notes.
- Response history.

Respect Telegram messaging limitations and user blocking behavior.

## 18.11. Roles, Permissions, and Settings

Provide role-based permissions and safe application settings management.

Use explicit permissions for financial actions, security settings, administrator management, secret rotation, and bulk messaging.

## 18.12. Reports and Audit Logs

Provide searchable, filterable reports with pagination and appropriate exports.

Audit logs must identify:

- Actor.
- Action.
- Target entity.
- Timestamp.
- Outcome.
- Relevant request or correlation ID.
- Reason where required.

Do not put passwords, payment credentials, bot tokens, private VPN configurations, or other secrets in audit logs.

---

# 19. Complete Telegram Administration Panel

The Telegram administration panel is a mandatory first-class interface.

**It must provide operational feature parity with the web administration panel.**

This does not mean reproducing the web layout pixel by pixel. It means that administrators must be able to perform the same authorized business operations through Telegram, using a practical conversational interface.

Do not implement only a few administrative commands and call the panel complete.

## 19.1. Administrator Authentication and Authorization

- Verify administrators using trusted Telegram user IDs and configured administrative identities.
- Apply role-based permissions to every command and callback.
- Never trust a callback merely because its data contains an admin-looking action.
- Recheck permissions on the server for every sensitive operation.
- Reject unauthorized users without exposing internal information.
- Record security-relevant attempts.
- Require additional confirmation for high-impact operations.

## 19.2. Dashboard

Provide paginated dashboard views with:

- Users.
- Orders.
- Deposits.
- Revenue metrics.
- Pending payments.
- Pending milestone approvals.
- Inventory stock.
- Failed deliveries.
- Provider health.
- Recent alerts.

## 19.3. User Management

Support:

- Search by Telegram ID and supported identifiers.
- Paginated user lists.
- User details.
- Wallet and order history.
- Referral information.
- Blocking and unblocking.
- Administrative notes.
- Authorized account actions.

## 19.4. Product Management

Support creating, editing, enabling, disabling, and organizing products and categories through multi-step forms.

Validate all submitted values.

## 19.5. Inventory Management

Support:

- Adding inventory.
- Starting a bulk import.
- Choosing a delimiter.
- Previewing imports.
- Reviewing invalid and duplicate entries.
- Confirming atomic imports.
- Inspecting batches.
- Searching inventory.
- Disabling invalid entries.
- Viewing import history.

For very large imports, accept a file or initiate a background job where supported. Do not attempt to place an unbounded inventory batch into a single Telegram message.

## 19.6. Order Management

Support searching, filtering, inspecting, and safely managing orders, delivery failures, and provisioning reconciliation.

## 19.7. Wallet and Payment Management

Support:

- Wallet inspection.
- Ledger history.
- Manual deposit approval and rejection.
- Automatic payment inspection.
- Refund or compensation workflows.
- Authorized wallet adjustments.
- Reconciliation tools.

High-impact operations must require confirmation and a reason where appropriate.

## 19.8. Provider Management

Support inspecting provider health, enabling or disabling integrations, and safely updating configuration.

Never send full secrets into Telegram messages.

## 19.9. Referral Management

Support referral rules, reward settings, referral search, reward inspection, suspicious activity review, and milestone approval/rejection.

## 19.10. Messaging and Membership

Support:

- Mandatory channel configuration.
- Membership cache settings.
- Editing and previewing messages.
- Editing supported buttons.
- Workflow configuration.
- Main-menu settings.
- Maintenance messages.

## 19.11. Campaigns, Support, and Coupons

Support campaign creation and monitoring, support ticket handling, coupon management, and usage reporting.

## 19.12. Administrators, Roles, and Permissions

Support managing administrators and role assignments, subject to the actor's permissions.

Do not allow an administrator to grant themselves higher privileges.

Protect the last active super-administrator from accidental removal or demotion.

## 19.13. Reports, Audit, and System Health

Support viewing operational reports, audit logs, queue status, failed jobs, database health indicators, and integration status.

Do not expose server secrets or raw sensitive environment variables.

## 19.14. Telegram UI Requirements

Use:

- Paginated inline-keyboard menus.
- Multi-step forms.
- Input validation.
- Back and cancel navigation.
- Confirmation screens.
- Clear success and failure responses.
- Safe handling of stale callbacks.
- Callback expiration or validation where appropriate.
- Rate limiting.
- Consistent Persian labels.

Long-running operations must run in background jobs and return status updates.

**Both the web panel and Telegram admin panel must call the same underlying services. Never duplicate financial or security-sensitive business logic inside Telegram handlers.**

---

# 20. Role-Based Access Control

Implement explicit roles and permissions.

Suggested roles:

- Super Administrator.
- Operations Administrator.
- Finance Administrator.
- Inventory Administrator.
- Support Administrator.
- Marketing Administrator.
- Read-Only Auditor.

Treat these as configurable examples, not hard-coded universal privileges.

Permissions must be enforced on the server.

Examples:

- `users.read`
- `users.manage`
- `products.read`
- `products.manage`
- `inventory.read`
- `inventory.import`
- `orders.read`
- `orders.manage`
- `wallets.read`
- `wallets.adjust`
- `payments.read`
- `payments.approve_manual`
- `payments.refund`
- `providers.manage`
- `referrals.read`
- `referrals.configure`
- `referrals.approve_milestone`
- `campaigns.manage`
- `support.manage`
- `admins.manage`
- `settings.manage`
- `audit.read`

Do not rely on hiding buttons in the frontend. Enforce authorization in every backend endpoint, service operation, Telegram command, and callback.

For especially sensitive operations, require step-up confirmation, a reason, and an immutable audit record.

---

# 21. Security Engineering

Security is a core project requirement, not a final polishing task.

Create a threat model and security documentation in `docs/security.md`.

## 21.1. Secrets and Configuration

- Keep production secrets out of source control.
- Use `.env.example` with placeholder values only.
- Add appropriate secret files and local environment files to `.gitignore`.
- Validate required configuration at startup.
- Fail safely when essential production configuration is missing.
- Never print secrets in logs.
- Encrypt persisted integration credentials using a documented strategy.
- Support credential rotation.
- Minimize secret access by component.

## 21.2. Web Authentication

Use a secure administrator authentication system appropriate to the deployment.

Requirements include:

- Strong authentication.
- Secure session management.
- Appropriate password hashing if passwords are used.
- Login rate limiting.
- Secure cookies.
- CSRF protection when applicable.
- Session expiration and revocation.
- Optional MFA where feasible.
- Secure password reset procedures.
- Protection against session fixation.

Do not assume that a hidden URL is authentication.

## 21.3. API Security

- Validate all request bodies and parameters.
- Use strict schemas.
- Enforce authorization server-side.
- Add rate limits.
- Apply request size limits.
- Configure CORS explicitly.
- Avoid mass assignment.
- Protect against SQL injection.
- Protect against insecure direct object references.
- Validate file uploads.
- Use parameterized queries and ORM-safe patterns.
- Avoid exposing internal exception details.
- Apply timeouts to external requests.

## 21.4. Telegram Security

- Validate Telegram user identity for administrative operations.
- Validate callback ownership and state.
- Prevent replay of sensitive actions.
- Use expiring, server-validated confirmation tokens where appropriate.
- Never trust callback payloads as the source of monetary amounts or permissions.
- Validate webhook requests according to Telegram's supported security mechanisms.
- Use a configured webhook secret token when operating with webhooks.
- Do not expose the bot token in URLs, logs, frontend code, or public repository files.

## 21.5. Payment Security

- Verify payment status through the authoritative gateway.
- Validate amount, currency, transaction ID, and payment ownership.
- Use idempotent event processing.
- Protect against webhook replay.
- Prevent duplicate credits.
- Record verification results.
- Reconcile uncertain transactions.
- Do not accept client-side payment success as authoritative.

## 21.6. Financial Integrity

- Use database transactions.
- Use unique constraints.
- Use concurrency-safe balance updates.
- Make ledger entries immutable.
- Audit administrative adjustments.
- Use compensating transactions for corrections.
- Add reconciliation checks for wallet balances, payments, refunds, and order charges.

## 21.7. Provider and VPN Configuration Security

VPN configuration strings and subscription URLs may contain credentials.

- Restrict access to sensitive configurations.
- Avoid logging raw configuration values.
- Avoid returning inventory secrets in general-purpose reports.
- Protect exports.
- Use redaction in exception messages.
- Avoid leaking provider credentials to Telegram users or unauthorized administrators.
- Validate provider API endpoints to reduce SSRF risk.
- Restrict outbound network access where practical.
- Apply provider request timeouts and bounded response sizes.

## 21.8. Browser and Frontend Security

- Avoid unsafe HTML rendering.
- Sanitize any permitted rich-text content.
- Use a restrictive security policy where compatible.
- Protect sensitive administrative actions against CSRF and XSS.
- Do not store long-lived administrative secrets in browser storage.
- Validate server-side even when frontend validation exists.
- Ensure that private API responses are not publicly cached.

## 21.9. File Upload Security

For inventory imports and report uploads:

- Limit file size.
- Validate file format and content.
- Do not trust file extensions alone.
- Reject unsupported encodings or malformed records safely.
- Prevent path traversal.
- Store uploaded files outside publicly served directories.
- Restrict access to import artifacts.
- Clean up temporary files.
- Do not execute uploaded content.

## 21.10. Abuse Prevention

Implement rate limits and abuse controls for:

- Login attempts.
- Wallet operations.
- Payment creation.
- Referral registration.
- Reward claims.
- Inventory imports.
- Administrative actions.
- Campaign broadcasts.
- Support message submission.
- Provider API requests.

Use configurable thresholds and observable risk signals.

## 21.11. Security Testing

Include tests for:

- Unauthorized access.
- Privilege escalation.
- IDOR.
- CSRF where applicable.
- XSS protections.
- SQL injection protections.
- Invalid webhook signatures.
- Replayed webhooks.
- Duplicate payment credits.
- Concurrent wallet spending.
- Duplicate referral rewards.
- Self-referrals.
- Unauthorized milestone approval.
- Secret exposure in API responses and logs.
- Malicious or malformed import files.
- Unsafe retry behavior.

Document residual risks rather than claiming perfect security.

---

# 22. Scalability and Performance

Design for high concurrency and measured scaling.

## 22.1. Database

- Add indexes based on query patterns.
- Use pagination rather than unbounded list queries.
- Avoid N+1 queries.
- Configure connection pooling.
- Keep transactions short.
- Use appropriate isolation and locking.
- Monitor slow queries.
- Use database constraints to enforce invariants.
- Test migration safety.

## 22.2. Redis and Caching

Use Redis for suitable temporary data, membership caching, rate limiting, and coordination.

Do not treat Redis as the permanent source of truth for wallet balances, payments, orders, or referrals.

Define cache invalidation and expiration behavior.

## 22.3. Background Workers

Use separate workers for:

- Provider provisioning.
- Payment reconciliation.
- Notifications.
- Campaigns.
- Inventory processing.
- Referral qualification.
- Stuck-order recovery.
- Scheduled cleanup.
- Reporting jobs.

Implement bounded retries, exponential backoff, idempotency, monitoring, and failure inspection.

## 22.4. Telegram API Constraints

- Respect Telegram rate limits.
- Use bounded concurrency.
- Handle retry-after responses.
- Prevent broadcast jobs from overwhelming the bot.
- Paginate large administrative datasets.
- Avoid blocking handlers with slow external calls.
- Use background jobs for long-running tasks.

## 22.5. Performance Measurement

Add structured metrics for:

- Request latency.
- Error rates.
- Database latency.
- Queue depth.
- Worker throughput.
- Provider latency and failures.
- Payment verification delays.
- Inventory import duration.
- Telegram API failures.
- Referral reward processing.

Set explicit performance targets before final load testing and report actual measured results.

---

# 23. Automated Testing — Mandatory

Testing is part of the implementation, not an optional future task.

Create a complete automated test suite and run it throughout development.

## 23.1. Backend Unit Tests

Use pytest and pytest-asyncio.

Test:

- Pricing calculations.
- Random price increment boundaries.
- Quote persistence and reuse.
- Currency normalization.
- Wallet balance rules.
- Ledger immutability.
- Purchase eligibility.
- Inventory validation.
- Duplicate detection.
- Referral attribution.
- Referral reward rules.
- Payment status transitions.
- Permission checks.
- Membership cache behavior.
- Workflow template validation.
- Provider error classification.
- Idempotency behavior.

## 23.2. Integration Tests

Use isolated test infrastructure with PostgreSQL and Redis.

Test:

- Database migrations.
- Repository operations.
- Transaction rollback.
- Wallet credit and debit.
- Concurrent purchases.
- Inventory reservation.
- Duplicate payment webhooks.
- Manual deposit approval.
- Payment gateway verification.
- Provider provisioning.
- Retry and reconciliation workflows.
- Background job processing.
- Referral reward issuance.
- Milestone approval.
- Audit event creation.
- Authorization across admin interfaces.

Mock external services at the integration boundary when necessary, and clearly distinguish mocked tests from tests against real sandbox environments.

## 23.3. Telegram Bot Tests

Test:

- `/start`.
- User registration.
- Referral attribution.
- Repeated `/start`.
- Mandatory channel membership.
- Membership cache hits and misses.
- Product browsing.
- Price quotes.
- Purchase confirmation.
- Insufficient balance.
- Duplicate callback delivery.
- Order history.
- Manual deposits.
- Automatic payment notifications.
- Referral dashboard.
- Reward notifications.
- Error handling.

For administrative operations, test every major category and permission boundary.

## 23.4. Web Frontend Tests

Use appropriate tools such as Vitest and React Testing Library.

Test:

- Rendering and navigation.
- RTL behavior.
- Forms and validation.
- API error states.
- Loading states.
- Pagination and filtering.
- Role-based visibility.
- Wallet and payment workflows.
- Referral management.
- Inventory import preview.
- Confirmation dialogs.
- Accessibility of essential controls.

Frontend visibility tests do not replace backend authorization tests.

## 23.5. End-to-End Tests

Use Playwright or a suitable equivalent.

Cover at least these complete workflows:

1. Register or identify a user.
2. Browse a product.
3. Receive a stable price quote.
4. Recharge a wallet through a simulated verified payment.
5. Purchase a manually stocked VPN configuration.
6. Complete a purchase that uses a fake provider in a test environment.
7. Inspect order history and delivery.
8. Import inventory through the administration interface.
9. Approve a manual deposit.
10. Attribute a referral.
11. Issue a new-user reward.
12. Issue a referrer reward after a qualifying verified payment.
13. Submit and approve a milestone request.
14. Perform equivalent core administrative workflows through Telegram.
15. Confirm that unauthorized users cannot perform administrative actions.

## 23.6. Concurrency Tests

Test simultaneous operations, not just sequential ones.

Mandatory scenarios:

- Two purchases competing for one inventory item.
- Multiple purchases competing for a limited wallet balance.
- Duplicate delivery jobs.
- Duplicate payment callbacks.
- Multiple workers processing the same event.
- Simultaneous approval of one deposit.
- Simultaneous approval of one referral milestone.
- Repeated reward-triggering events.
- Repeated idempotency keys.

Verify the final database state, ledger, inventory state, and user-visible results.

## 23.7. Security Tests

Implement the security scenarios in Section 21.11.

Add static analysis, dependency checks, and secret scanning to CI where practical.

## 23.8. Coverage Requirements

Set a backend coverage target of at least 85% overall and aim for at least 95% coverage of critical financial and referral logic where practical.

Coverage percentages alone do not establish correctness.

Report:

- Actual coverage.
- Tests executed.
- Tests passed.
- Tests failed.
- Skipped tests and reasons.
- Known gaps.
- External services that were mocked.
- Tests requiring credentials that were unavailable.

Never lower a coverage threshold merely to hide missing tests.

## 23.9. Continuous Integration

Create GitHub Actions workflows that run appropriate checks on pull requests and relevant pushes:

- Dependency installation.
- Formatting.
- Linting.
- Static type checking.
- Backend unit tests.
- Integration tests with PostgreSQL and Redis.
- Frontend tests.
- Build verification.
- Migration checks.
- Security and dependency scanning.
- Secret scanning where available.

Do not claim CI passed until the workflow has actually completed successfully.

---

# 24. Load Testing and Capacity Validation

Create reproducible load-test scenarios using an appropriate tool such as Locust, k6, or an equivalent.

Test realistic user journeys rather than only sending trivial health-check requests.

Include scenarios for:

- User onboarding.
- Product browsing.
- Cached membership checks.
- Price quote generation.
- Wallet balance reads.
- Concurrent purchases.
- Payment status polling.
- Referral statistics.
- Administrative searches.
- Inventory imports.
- Background notifications.

Test expected peak concurrency, gradual ramp-up, and sustained operation.

Monitor:

- Latency percentiles.
- Throughput.
- Error rate.
- Database connections.
- Database query latency.
- Redis behavior.
- Queue backlog.
- Worker utilization.
- Memory and CPU.
- Provider timeouts.

Identify bottlenecks and document tuning steps.

Do not run destructive load tests against production payment gateways, real customer data, or production providers without explicit authorization.

Use synthetic data and sandbox integrations.

The target is to design for 100,000 daily active users, but report the tested workload and actual measured capacity rather than treating the target as an established fact.

---

# 25. Observability, Operations, and Recovery

Implement:

- Structured logs.
- Request and job correlation IDs.
- Health endpoints.
- Readiness checks.
- Metrics.
- Error monitoring integration points.
- Background-job status inspection.
- Provider health checks.
- Payment reconciliation reports.
- Stuck-order detection.
- Inventory reconciliation.
- Wallet ledger reconciliation.
- Referral reward reconciliation.

Logs must be useful without exposing secrets or private VPN configurations.

## 25.1. Backups

Document and configure an appropriate PostgreSQL backup strategy.

Include:

- Backup scheduling.
- Retention.
- Access restrictions.
- Encryption where appropriate.
- Restoration procedures.
- Verification through restore tests.

Do not claim backups are recoverable until a restoration test has succeeded.

## 25.2. Recovery Procedures

Document recovery for:

- Failed migrations.
- Database restoration.
- Redis loss.
- Stuck background jobs.
- Ambiguous provider requests.
- Failed VPN deliveries.
- Incorrect wallet credits.
- Duplicate payment events.
- Incorrect referral rewards.
- Compromised integration credentials.

Prefer explicit recovery procedures and compensating transactions over manual database edits.

---

# 26. Configuration and Deployment

Provide:

- `.env.example` with placeholders only.
- Dockerfiles.
- Docker Compose configuration.
- Database migration commands.
- Seed data for local development.
- Development startup instructions.
- Test commands.
- Production deployment instructions.
- Reverse-proxy configuration where appropriate.
- Health checks.
- Worker startup instructions.
- Backup instructions.
- Upgrade and rollback guidance.

Validate production configuration at startup.

Use separate configurations for development, test, staging, and production.

Do not use development credentials or a fake payment gateway in production.

Do not hard-code production URLs, bot tokens, payment credentials, provider credentials, or administrator secrets.

Document how to configure:

- Telegram bot token.
- Telegram webhook or polling mode.
- Initial super-administrator.
- PostgreSQL.
- Redis.
- Payment gateways.
- VPN providers.
- Currency conventions.
- Mandatory channels.
- Pricing rules.
- Referral rules.
- Logging and monitoring.

Do not create an insecure default administrator password.

Provide a safe initial administrator bootstrap procedure.

---

# 27. Documentation and Developer Experience

Create and maintain:

- `README.md`
- `docs/architecture.md`
- `docs/database.md`
- `docs/security.md`
- `docs/api.md`
- `docs/deployment.md`
- `docs/testing.md`
- `docs/operations.md`
- `docs/implementation-progress.md`

Document:

- Architecture and module boundaries.
- Important database models.
- Payment and wallet invariants.
- Order and provisioning state machines.
- Referral qualification and reward rules.
- Telegram and web administration permissions.
- Environment variables.
- Local setup.
- Migrations.
- Test execution.
- Load testing.
- Deployment.
- Backup and recovery.
- Known limitations.

Keep documentation synchronized with the actual implementation.

Do not document unimplemented features as completed.

---

# 28. Mandatory GitHub Workflow

**GitHub repository: `https://github.com/KEHSANN/bot`**

GitHub publishing is mandatory throughout the project lifecycle, not just at the end.

## 28.1. Initial Inspection

Before making changes:

- Inspect the remote repository.
- Check the current branch and commit history.
- Review existing files and uncommitted work.
- Identify branch protection and repository conventions.
- Confirm that Git authentication and required permissions are available.
- Preserve existing history and functionality.

Do not overwrite existing work blindly.

## 28.2. Mandatory Phase-by-Phase Publishing

Divide implementation into independently verifiable phases.

After each phase, follow this exact cycle:

**Complete → Test → Review → Commit → Push → Verify → Report**

Do not wait until the entire project is finished before pushing changes.

After every completed phase:

1. Finish the phase's implementation.
2. Run its relevant automated tests.
3. Fix failures introduced by the phase.
4. Review the Git diff.
5. Check for unintended changes.
6. Check that no secrets, tokens, credentials, production configuration, or sensitive generated data are included.
7. Update relevant documentation.
8. Commit the completed changes with a clear, meaningful message.
9. Push the commit to `https://github.com/KEHSANN/bot`.
10. Verify that the push succeeded and the remote branch points to the expected commit.
11. Record the commit hash, test results, and current status in `docs/implementation-progress.md`.
12. Report the result before beginning the next phase.

Do not claim that a push succeeded without verifying it.

## 28.3. Suggested Implementation Phases

Adjust the phase boundaries to match the existing repository, but cover all requirements.

- Phase 1: Repository inspection, architecture, development setup, and CI foundation.
- Phase 2: Database foundation, migrations, configuration, and common infrastructure.
- Phase 3: Authentication, RBAC, security foundations, and audit logging.
- Phase 4: Product catalog, pricing engine, and stable quotes.
- Phase 5: Inventory management and atomic bulk import.
- Phase 6: Wallet, immutable ledger, transactional purchases, and order management.
- Phase 7: Manual deposits and payment gateway adapters.
- Phase 8: VPN provider adapters, provisioning workers, and delivery recovery.
- Phase 9: Telegram customer bot and configurable user workflows.
- Phase 10: Referral attribution, reward rules, milestone approvals, and anti-abuse controls.
- Phase 11: Web administration panel.
- Phase 12: Complete Telegram administration panel with feature parity.
- Phase 13: Campaigns, coupons, support, reports, and operational tooling.
- Phase 14: Comprehensive unit, integration, end-to-end, concurrency, and security tests.
- Phase 15: Load testing, performance improvements, observability, and recovery procedures.
- Phase 16: Deployment documentation, production-readiness review, and final verification.

A phase is complete only when its implementation and relevant checks have actually been performed.

## 28.4. Git Safety

- Use a suitable development branch or feature-branch workflow.
- Preserve the repository's established branching conventions.
- Never force-push or rewrite shared history without explicit authorization.
- Do not delete existing branches or tags without authorization.
- Do not bypass branch protection.
- Do not disable failing tests to make a commit pass.
- Use focused commits with understandable messages.
- Add appropriate entries to `.gitignore`.
- Keep `.env` and secret files out of Git.
- Provide `.env.example` with placeholder values only.
- Never commit live tokens, credentials, payment secrets, provider secrets, private customer configurations, or production database contents.

If a phase has a blocking failure, document the failure clearly. If a safe checkpoint is appropriate, commit and push that checkpoint without falsely labeling the phase as passing. Do not proceed as if the phase succeeded when critical correctness or security problems remain.

If authentication, permissions, branch protection, or network problems prevent a push, report the exact blocker and the local commit hash. Never fabricate a successful remote push.

## 28.5. Required Progress Report After Each Phase

Report:

- Phase name.
- Implementation summary.
- Files and major components changed.
- Tests executed.
- Actual test results.
- Known failures or limitations.
- Security concerns discovered.
- Commit hash.
- Branch name.
- Push verification result.
- Next planned phase.

Maintain `docs/implementation-progress.md` with a checklist, test status, commit hashes, and unresolved issues.

## 28.6. Final Repository Verification

At project completion:

- Verify that all intended implementation work is committed and pushed.
- Verify that the remote repository contains the latest changes.
- Confirm that required documentation and deployment files are present.
- Confirm that the final test results reflect actual execution.
- Provide the repository URL.
- Provide the final branch and latest commit hash.
- Summarize test results and load-test findings.
- Document any unimplemented requirements or known risks.

The final GitHub repository must be:

`https://github.com/KEHSANN/bot`

**Non-negotiable rule: after every implementation phase, commit and push the completed work before moving on to the next phase.**

---

# 29. Implementation Order and Dependency Management

Follow a dependency-aware implementation order.

Do not build a visually complete frontend on top of a fundamentally incorrect financial backend.

Prioritize:

1. Repository inspection and architecture.
2. Database and migrations.
3. Security, authentication, and permissions.
4. Financial invariants and immutable ledger.
5. Orders, pricing, and inventory.
6. Payment verification.
7. Provider provisioning and delivery recovery.
8. Customer Telegram bot.
9. Referral rewards and milestone approvals.
10. Web administration panel.
11. Complete Telegram administration panel.
12. Campaigns, coupons, support, and reports.
13. Comprehensive tests.
14. Load and security validation.
15. Deployment and operational documentation.

The interfaces can be developed incrementally alongside the backend, but must never bypass the underlying business rules.

Do not leave security, testing, or financial correctness until the end.

---

# 30. Definition of Done

The project is not complete until the following requirements have been implemented and verified.

## Functional Requirements

- [ ] Persian Telegram customer bot.
- [ ] Product catalog and categories.
- [ ] Economic products and configurable plans.
- [ ] Configurable pricing and stable randomized quotes.
- [ ] Wallet and immutable financial ledger.
- [ ] Manual deposit workflow.
- [ ] Verified automatic payment workflow.
- [ ] Order management and history.
- [ ] Manual inventory entry.
- [ ] Bulk inventory import with preview and duplicate detection.
- [ ] Atomic inventory import mode.
- [ ] Automated provider provisioning through implemented adapters.
- [ ] Safe handling of ambiguous provisioning outcomes.
- [ ] Mandatory channel membership.
- [ ] Configurable membership caching.
- [ ] Configurable messages and supported buttons.
- [ ] Referral links and immutable attribution.
- [ ] Standard referrer rewards.
- [ ] New-user rewards.
- [ ] First verified-payment rewards.
- [ ] Milestone rewards requiring administrator approval.
- [ ] Referral anti-abuse controls.
- [ ] Complete web administration panel.
- [ ] Complete Telegram administration panel with operational feature parity.
- [ ] Role-based permissions.
- [ ] Support and campaign workflows.
- [ ] Reports and audit logs.
- [ ] Operational monitoring and recovery tools.

## Quality Requirements

- [ ] Database migrations work from a clean database.
- [ ] Backend unit tests run successfully.
- [ ] Integration tests run successfully.
- [ ] Telegram bot tests run successfully.
- [ ] Frontend tests run successfully.
- [ ] Essential end-to-end workflows pass.
- [ ] Critical concurrency tests pass.
- [ ] Security tests have been executed and findings documented.
- [ ] CI workflows are configured and actual results reported.
- [ ] Load tests have been executed and their actual results documented.
- [ ] No known critical financial consistency bug remains.
- [ ] No known critical authorization vulnerability remains unaddressed.
- [ ] No production secrets are committed to Git.
- [ ] Documentation reflects the real implementation.
- [ ] Deployment and recovery procedures are documented.
- [ ] All completed phases have been committed and pushed to GitHub.

## Final Reporting Requirements

At completion, provide:

1. GitHub repository URL.
2. Final branch and latest commit hash.
3. Implemented feature summary.
4. Database and deployment architecture summary.
5. Test commands and actual results.
6. Coverage report.
7. Load-test results and measured capacity.
8. Security checks performed and remaining risks.
9. Deployment instructions.
10. Any remaining incomplete items and blockers.

Do not claim that the application is production-ready merely because it builds or starts.

Do not claim 100,000 daily active users are supported unless test evidence justifies the claim.

Do not claim real payment or VPN provider integrations work unless they have been implemented and verified against appropriate documentation or sandbox environments.

Do not conceal failed tests or incomplete functionality.

---

# 31. Execution Instructions

Begin by inspecting `https://github.com/KEHSANN/bot`.

Then:

1. Summarize the existing repository state.
2. Identify any blockers, existing conventions, and important architectural decisions.
3. Establish the first implementation phase.
4. Implement the first phase rather than stopping at planning.
5. Run the relevant tests.
6. Review the changes.
7. Commit and push the completed phase to GitHub.
8. Verify the push and report the commit hash.
9. Update `docs/implementation-progress.md`.
10. Continue to the next phase using the same mandatory cycle.

If a required API credential or external service is unavailable, implement the correct adapter interface and test it using a clearly identified fake or sandbox implementation. Document what still requires credentials. Do not silently substitute a fake integration in production.

If a requirement is technically ambiguous, choose a safe, maintainable default, document the decision, and continue where possible. Ask a question only when the missing information materially blocks a safe implementation.

Do not stop after producing architecture diagrams, documentation, database schemas, placeholder pages, or a development plan.

Do not leave essential buttons unimplemented.

Do not simulate payment success in production.

Do not bypass authorization.

Do not claim tests passed unless they actually passed.

Do not claim GitHub publishing succeeded unless the remote push was verified.

**Start now: inspect the repository, establish the baseline, implement the first phase, test it, and push the completed phase to `https://github.com/KEHSANN/bot`. Continue incrementally until every feasible requirement is implemented, verified, documented, and published.**
