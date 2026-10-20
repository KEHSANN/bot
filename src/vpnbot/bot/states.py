"""FSM states for customer flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CheckoutStates(StatesGroup):
    choosing_method = State()
    awaiting_receipt = State()
    awaiting_txid = State()
    confirming = State()


class WalletStates(StatesGroup):
    """Wallet top-up: amount first, then evidence for the chosen method."""

    entering_amount = State()
    choosing_method = State()
    awaiting_receipt = State()
    awaiting_txid = State()


class SupportStates(StatesGroup):
    awaiting_message = State()


class AdminStates(StatesGroup):
    """Admin wizard inputs (each entry sets the expected free-text input)."""

    product_create = State()
    product_edit = State()
    plan_create = State()
    plan_edit = State()
    category_create = State()
    import_upload = State()
    broadcast_compose = State()
    settings_value = State()
    screen_message = State()
    screen_button = State()
    admin_grant = State()
    support_reply = State()
    payment_note = State()
    rejection_reason = State()
    wallet_adjust = State()
    broadcast_confirm = State()
