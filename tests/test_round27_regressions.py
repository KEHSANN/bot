"""Round-27 regressions: TXID normalization bypass spellings,
explorer-link hash extraction, and the import parser fixes
(last-separator split, csv.Error conversion, length caps).

Split into its own module like rounds 25/26 (the main regressions
file is at the GitHub connector's single-argument push ceiling).
"""

from __future__ import annotations

import pytest
from tests.test_admin_regressions import _stock  # noqa: F401  (shared helper)

_HASH = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"


def test_txid_spellings_share_one_canonical_form() -> None:
    from vpnbot.services.payments import normalize_txid, valid_txid

    spellings = [
        _HASH,
        "0x" + _HASH,
        _HASH.upper(),
        "0X" + _HASH.upper(),
        # The round-27 bypass trio: separator removal used to run
        # AFTER a single 0x strip, so these stored as "0x<hash>" —
        # a different string for the same transfer.
        "_0x" + _HASH,
        "0_x" + _HASH,
        "0x0x" + _HASH,
        "0x-_" + _HASH,
        # Explorer links reduce to the same canonical hash.
        "https://tronscan.org/#/transaction/" + _HASH,
        "https://etherscan.io/tx/0x" + _HASH,
        "https://bscscan.com/tx/0x" + _HASH.upper() + "?utm=share",
        "tronscan.org/#/transaction/" + _HASH + "/",
    ]
    for spelling in spellings:
        normalized = normalize_txid(spelling)
        assert normalized == _HASH, spelling
        assert valid_txid(normalized)


def test_txid_link_junk_is_still_rejected() -> None:
    from vpnbot.services.payments import normalize_txid, valid_txid

    for junk in [
        "https://example.com/transaction/",          # no hash segment
        "https://example.com/tx?hash=" + _HASH,      # hash in query, not path
        "https://example.com/tx/not-a-hash",         # non-hex tail
        "just some text",
    ]:
        assert not valid_txid(normalize_txid(junk)), junk


async def test_txid_bypass_spelling_cannot_fund_twice(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import topups as topup_service
    from vpnbot.services.settings import CRYPTO_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900002801)
    await SettingsService(session).set(
        CRYPTO_SETTINGS,
        {"enabled": True, "asset": "USDT", "network": "TRC20", "address": "TTestAddress"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "txid27")

    # First use: a top-up paid with the 0x-prefixed spelling.
    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=50000, method=PaymentMethod.CRYPTO
    )
    await topup_service.submit_crypto_evidence(
        session, topup_id=topup.id, user_id=user.id, txid="0x" + _HASH
    )
    assert topup.txid == _HASH

    # Second use of the SAME transfer for an order — via a bypass
    # spelling AND via the explorer link — must both be refused.
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(
        session, order=order, method=PaymentMethod.CRYPTO
    )
    for spelling in ("_0x" + _HASH, "https://etherscan.io/tx/0x" + _HASH):
        try:
            await payment_service.submit_crypto_evidence(
                session, payment_id=payment.id, user_id=user.id, txid=spelling
            )
        except payment_service.PaymentError:
            pass
        else:
            raise AssertionError(f"TXID spelling {spelling[:12]}… funded twice")


def test_import_split_line_respects_config_and_link_commas() -> None:
    from vpnbot.services.import_service import _split_line

    config = "vless://u@h:443?alpn=h2,http/1.1#Server"
    # Comma inside the config's alpn AND a pair link: split at the
    # separator directly in front of the URL, not the first comma.
    assert _split_line(config + ",https://sub.example.com/sub") == (
        config,
        "https://sub.example.com/sub",
    )
    # A comma inside the link's own query belongs to the link.
    assert _split_line("vless://u@h:443,https://sub.example.com/api?type=vless,vmess") == (
        "vless://u@h:443",
        "https://sub.example.com/api?type=vless,vmess",
    )
    # Pipe pair with the ignored label column still splits clean.
    assert _split_line("vless://u@h:443|https://sub.example.com/x|label1") == (
        "vless://u@h:443",
        "https://sub.example.com/x",
    )
    # No pair link: the config survives whole, separators and all.
    assert _split_line(config) == (config, "")
    frag = "vless://u@h:443?alpn=h2,http/1.1#Germany | VIP"
    assert _split_line(frag) == (frag, "")


def test_import_oversized_csv_field_becomes_import_error() -> None:
    from vpnbot.services.import_service import ImportError, parse_records

    raw = "config,sub_url\n" + ("x" * 200_000) + ",https://s.example.com"
    with pytest.raises(ImportError):
        parse_records(raw)


def test_import_length_caps_reject_undeliverable_stock() -> None:
    from vpnbot.services.import_service import parse_records

    overlong_config = parse_records("vless://" + "a" * 5000 + "|https://sub.example.com/x")
    assert overlong_config[0].error and "طولانی" in overlong_config[0].error

    overlong_sub = parse_records("vless://ok|https://" + "a" * 1100)
    assert overlong_sub[0].error and "طولانی" in overlong_sub[0].error

    at_cap = parse_records("vless://" + "a" * 3400 + "|https://sub.example.com/x")
    assert at_cap[0].error is None
