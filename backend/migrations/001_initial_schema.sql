-- 001_initial_schema.sql
-- Production-Grade Telegram VPN Store — Complete Relational Schema (IRT Integer Currency)

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(128),
    full_name VARCHAR(256) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked', 'suspended')),
    referred_by_user_id BIGINT REFERENCES users(id),
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by_user_id);

CREATE TABLE IF NOT EXISTS user_status_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    old_status VARCHAR(32) NOT NULL,
    new_status VARCHAR(32) NOT NULL,
    changed_by_admin_id BIGINT,
    reason TEXT NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(128) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    name VARCHAR(64) PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    code VARCHAR(64) PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_name VARCHAR(64) NOT NULL REFERENCES roles(name) ON DELETE CASCADE,
    permission_code VARCHAR(64) NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_name, permission_code)
);

CREATE TABLE IF NOT EXISTS admin_role_assignments (
    admin_id BIGINT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    role_name VARCHAR(64) NOT NULL REFERENCES roles(name) ON DELETE CASCADE,
    assigned_by_admin_id BIGINT,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (admin_id, role_name)
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    jti VARCHAR(64) PRIMARY KEY,
    admin_id BIGINT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    revoked INTEGER NOT NULL DEFAULT 0,
    expires_at BIGINT NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_categories (
    id BIGSERIAL PRIMARY KEY,
    slug VARCHAR(64) UNIQUE NOT NULL,
    title_fa VARCHAR(128) NOT NULL,
    description_fa TEXT NOT NULL,
    is_economic INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    category_id BIGINT NOT NULL REFERENCES product_categories(id),
    code VARCHAR(64) UNIQUE NOT NULL,
    title_fa VARCHAR(128) NOT NULL,
    description_fa TEXT NOT NULL,
    service_type VARCHAR(32) NOT NULL CHECK (service_type IN ('vless', 'vmess', 'trojan', 'Outline', 'subscription_url')),
    inventory_mode VARCHAR(32) NOT NULL CHECK (inventory_mode IN ('manual', 'automated_provider')),
    provider_code VARCHAR(64),
    base_price_irt BIGINT NOT NULL CHECK (base_price_irt >= 0),
    promo_price_irt BIGINT CHECK (promo_price_irt IS NULL OR promo_price_irt >= 0),
    min_random_increment_irt BIGINT NOT NULL DEFAULT 0 CHECK (min_random_increment_irt >= 0),
    max_random_increment_irt BIGINT NOT NULL DEFAULT 0 CHECK (max_random_increment_irt >= min_random_increment_irt),
    increment_step_irt BIGINT NOT NULL DEFAULT 100 CHECK (increment_step_irt > 0),
    duration_days INTEGER NOT NULL CHECK (duration_days > 0),
    traffic_gb INTEGER NOT NULL CHECK (traffic_gb > 0),
    device_limit INTEGER NOT NULL CHECK (device_limit > 0),
    max_purchases_per_user INTEGER NOT NULL DEFAULT 100,
    is_economic INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    pricing_version INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_quotes (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    product_id BIGINT NOT NULL REFERENCES products(id),
    base_amount_irt BIGINT NOT NULL,
    random_increment_irt BIGINT NOT NULL,
    discount_irt BIGINT NOT NULL DEFAULT 0,
    final_amount_irt BIGINT NOT NULL CHECK (final_amount_irt >= 0),
    currency VARCHAR(8) NOT NULL DEFAULT 'IRT' CHECK (currency = 'IRT'),
    pricing_version INTEGER NOT NULL,
    coupon_code VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'consumed', 'expired')),
    expires_at BIGINT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_user_product ON price_quotes(user_id, product_id, status);

CREATE TABLE IF NOT EXISTS inventory_batches (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id),
    imported_by_admin_id BIGINT NOT NULL REFERENCES admin_users(id),
    import_mode VARCHAR(32) NOT NULL CHECK (import_mode IN ('atomic', 'partial', 'single')),
    delimiter VARCHAR(16) NOT NULL,
    total_count INTEGER NOT NULL,
    valid_count INTEGER NOT NULL,
    invalid_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id),
    batch_id BIGINT REFERENCES inventory_batches(id),
    config_encrypted TEXT NOT NULL,
    content_hash VARCHAR(64) UNIQUE NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'reserved', 'assigned', 'disabled', 'invalid')),
    reserved_by_order_id VARCHAR(64),
    reserved_until BIGINT,
    assigned_to_order_id VARCHAR(64) UNIQUE,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inventory_product_status ON inventory_items(product_id, status);

CREATE TABLE IF NOT EXISTS wallets (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    balance_irt BIGINT NOT NULL DEFAULT 0 CHECK (balance_irt >= 0),
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    tx_type VARCHAR(64) NOT NULL CHECK (tx_type IN ('deposit_manual', 'deposit_gateway', 'purchase_debit', 'refund_credit', 'referral_reward', 'referral_reversal', 'admin_adjustment')),
    amount_irt BIGINT NOT NULL,
    balance_after_irt BIGINT NOT NULL CHECK (balance_after_irt >= 0),
    currency VARCHAR(8) NOT NULL DEFAULT 'IRT' CHECK (currency = 'IRT'),
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    reference_type VARCHAR(64) NOT NULL,
    reference_id VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    actor_admin_id BIGINT,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger_transactions(user_id, created_at);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    transaction_id VARCHAR(64) NOT NULL REFERENCES ledger_transactions(id),
    account_code VARCHAR(64) NOT NULL,
    direction VARCHAR(16) NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount_irt BIGINT NOT NULL CHECK (amount_irt > 0),
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_methods (
    code VARCHAR(64) PRIMARY KEY,
    title_fa VARCHAR(128) NOT NULL,
    method_type VARCHAR(32) NOT NULL CHECK (method_type IN ('manual_card', 'gateway')),
    card_number VARCHAR(64),
    account_holder VARCHAR(128),
    instructions_fa TEXT NOT NULL,
    min_amount_irt BIGINT NOT NULL CHECK (min_amount_irt > 0),
    max_amount_irt BIGINT NOT NULL CHECK (max_amount_irt >= min_amount_irt),
    is_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS manual_deposit_requests (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    method_code VARCHAR(64) NOT NULL REFERENCES payment_methods(code),
    amount_irt BIGINT NOT NULL CHECK (amount_irt > 0),
    proof_reference TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by_admin_id BIGINT REFERENCES admin_users(id),
    review_reason TEXT,
    ledger_tx_id VARCHAR(64) REFERENCES ledger_transactions(id),
    created_at BIGINT NOT NULL,
    reviewed_at BIGINT
);

CREATE TABLE IF NOT EXISTS payments (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    gateway_code VARCHAR(64) NOT NULL,
    amount_irt BIGINT NOT NULL CHECK (amount_irt > 0),
    provider_amount_irr BIGINT NOT NULL CHECK (provider_amount_irr > 0),
    currency VARCHAR(8) NOT NULL DEFAULT 'IRT',
    provider_authority VARCHAR(128) UNIQUE,
    provider_tx_id VARCHAR(128) UNIQUE,
    status VARCHAR(32) NOT NULL CHECK (status IN ('created', 'pending', 'verifying', 'succeeded', 'failed', 'expired', 'cancelled', 'refunded', 'requires_review')),
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    ledger_tx_id VARCHAR(64) REFERENCES ledger_transactions(id),
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    product_id BIGINT NOT NULL REFERENCES products(id),
    quote_id VARCHAR(64) UNIQUE NOT NULL REFERENCES price_quotes(id),
    amount_irt BIGINT NOT NULL CHECK (amount_irt >= 0),
    currency VARCHAR(8) NOT NULL DEFAULT 'IRT',
    status VARCHAR(32) NOT NULL CHECK (status IN ('created', 'paid', 'provisioning', 'delivered', 'failed_Requires_reconciliation', 'refunded')),
    delivery_status VARCHAR(32) NOT NULL CHECK (delivery_status IN ('pending', 'reserved_inventory', 'provisioning_queued', 'delivered', 'failed', 'compensated')),
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    ledger_tx_id VARCHAR(64) REFERENCES ledger_transactions(id),
    refund_ledger_tx_id VARCHAR(64) REFERENCES ledger_transactions(id),
    delivered_config_encrypted TEXT,
    provider_sub_id VARCHAR(128),
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);

CREATE TABLE IF NOT EXISTS provisioning_jobs (
    id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) UNIQUE NOT NULL REFERENCES orders(id),
    provider_code VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL CHECK (status IN ('queued', 'in_progress', 'succeeded', 'ambiguous_reconcile', 'dead_letter', 'compensated')),
    external_request_id VARCHAR(128),
    last_error TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_accounts (
    code VARCHAR(64) PRIMARY KEY,
    title_fa VARCHAR(128) NOT NULL,
    adapter_type VARCHAR(32) NOT NULL CHECK (adapter_type IN ('marzban', 'fake_dev')),
    endpoint_url TEXT NOT NULL,
    credentials_encrypted TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    health_status VARCHAR(32) NOT NULL DEFAULT 'healthy',
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_codes (
    code VARCHAR(64) PRIMARY KEY,
    user_id BIGINT UNIQUE NOT NULL REFERENCES users(id),
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    id BIGSERIAL PRIMARY KEY,
    referrer_user_id BIGINT NOT NULL REFERENCES users(id),
    referred_user_id BIGINT UNIQUE NOT NULL REFERENCES users(id),
    referral_code VARCHAR(64) NOT NULL REFERENCES referral_codes(code),
    status VARCHAR(32) NOT NULL DEFAULT 'attributed' CHECK (status IN ('attributed', 'eligible', 'qualified_payment', 'flagged_suspicious')),
    risk_flag TEXT,
    created_at BIGINT NOT NULL,
    qualified_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id, status);

CREATE TABLE IF NOT EXISTS referral_reward_rules (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled INTEGER NOT NULL DEFAULT 1,
    referrer_reward_n_irt BIGINT NOT NULL DEFAULT 15000,
    referrer_n_trigger VARCHAR(32) NOT NULL DEFAULT 'membership_verified' CHECK (referrer_n_trigger IN ('registration', 'membership_verified', 'first_payment')),
    new_user_reward_w_irt BIGINT NOT NULL DEFAULT 10000,
    new_user_w_requires_membership INTEGER NOT NULL DEFAULT 1,
    first_payment_reward_p_irt BIGINT NOT NULL DEFAULT 25000,
    milestone_threshold_m INTEGER NOT NULL DEFAULT 5 CHECK (milestone_threshold_m > 0),
    milestone_reward_k_irt BIGINT NOT NULL DEFAULT 100000,
    milestone_repeatable INTEGER NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_rewards (
    id VARCHAR(64) PRIMARY KEY,
    referral_id BIGINT REFERENCES referrals(id),
    recipient_user_id BIGINT NOT NULL REFERENCES users(id),
    reward_type VARCHAR(32) NOT NULL CHECK (reward_type IN ('N_referrer', 'W_new_user', 'P_first_payment', 'K_milestone')),
    amount_irt BIGINT NOT NULL CHECK (amount_irt > 0),
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'credited' CHECK (status IN ('credited', 'reversed')),
    ledger_tx_id VARCHAR(64) NOT NULL REFERENCES ledger_transactions(id),
    reversal_ledger_tx_id VARCHAR(64) REFERENCES ledger_transactions(id),
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_milestone_requests (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    milestone_index INTEGER NOT NULL DEFAULT 1,
    threshold_m INTEGER NOT NULL,
    eligible_count_snapshot INTEGER NOT NULL,
    reward_k_irt BIGINT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending_review' CHECK (status IN ('pending_review', 'approved', 'rejected')),
    reviewed_by_admin_id BIGINT REFERENCES admin_users(id),
    review_reason TEXT,
    reward_id VARCHAR(64) REFERENCES referral_rewards(id),
    created_at BIGINT NOT NULL,
    reviewed_at BIGINT,
    UNIQUE (user_id, milestone_index)
);

CREATE TABLE IF NOT EXISTS mandatory_channels (
    channel_id VARCHAR(128) PRIMARY KEY,
    title_fa VARCHAR(128) NOT NULL,
    join_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    cache_ttl_seconds INTEGER NOT NULL DEFAULT 86400
);

CREATE TABLE IF NOT EXISTS membership_cache (
    user_id BIGINT NOT NULL REFERENCES users(id),
    channel_id VARCHAR(128) NOT NULL REFERENCES mandatory_channels(channel_id) ON DELETE CASCADE,
    is_member INTEGER NOT NULL,
    checked_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    PRIMARY KEY (user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS message_templates (
    key VARCHAR(64) PRIMARY KEY,
    title_fa VARCHAR(128) NOT NULL,
    body_fa TEXT NOT NULL,
    allowed_placeholders TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_buttons (
    id BIGSERIAL PRIMARY KEY,
    menu_key VARCHAR(64) NOT NULL,
    button_text_fa VARCHAR(128) NOT NULL,
    action_key VARCHAR(64) NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    is_visible INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS coupons (
    code VARCHAR(64) PRIMARY KEY,
    discount_irt BIGINT NOT NULL CHECK (discount_irt > 0),
    min_order_irt BIGINT NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 100,
    used_count INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    subject VARCHAR(256) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'closed')),
    priority VARCHAR(32) NOT NULL DEFAULT 'normal',
    assigned_admin_id BIGINT REFERENCES admin_users(id),
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_messages (
    id BIGSERIAL PRIMARY KEY,
    ticket_id VARCHAR(64) NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    sender_type VARCHAR(32) NOT NULL CHECK (sender_type IN ('user', 'admin', 'internal_note')),
    sender_id BIGINT NOT NULL,
    body TEXT NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id VARCHAR(64) PRIMARY KEY,
    title_fa VARCHAR(128) NOT NULL,
    body_fa TEXT NOT NULL,
    audience_filter VARCHAR(64) NOT NULL DEFAULT 'all_active',
    status VARCHAR(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'running', 'completed', 'cancelled')),
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_by_admin_id BIGINT NOT NULL REFERENCES admin_users(id),
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    actor_admin_id BIGINT,
    action VARCHAR(128) NOT NULL,
    target_type VARCHAR(64) NOT NULL,
    target_id VARCHAR(128) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    reason TEXT,
    correlation_id VARCHAR(64) NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
