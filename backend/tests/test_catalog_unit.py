import secrets
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.catalog.schemas import PlanBody, ProductBody, QuoteBody
from app.core.money import MAX_AMOUNT


def plan_data() -> dict:
    return dict(
        kind="plans",
        product_id=str(uuid4()),
        slug="plan",
        title="پلن",
        price_minor=10000,
        current_password=secrets.token_urlsafe(28),
        reason="security_response",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"price_minor": True},
        {"price_minor": 1.5},
        {"price_minor": "100"},
        {"price_minor": -1},
        {"currency": "IRR"},
        {"pricing_version": 99},
        {"price_minor": MAX_AMOUNT, "max_increment": 1},
        {"min_increment": 20, "max_increment": 10},
        {"min_increment": 1, "increment_step": 10},
        {"max_increment": 12, "increment_step": 10},
        {"increment_step": 0},
        {"randomization_method": "client"},
        {"duration_days": 0},
        {"device_limit": False},
        {"enabled": "true"},
    ],
)
def test_strict_plan_rejects_invalid_pricing(change: dict) -> None:
    with pytest.raises(ValidationError):
        PlanBody.model_validate(plan_data() | change)


def test_catalog_defaults_and_private_attribute_rejection() -> None:
    assert not PlanBody.model_validate(plan_data()).enabled
    body = plan_data() | dict(
        kind="products", category_id=str(uuid4()), service_type="vless", inventory_mode="manual"
    )
    for key in ("product_id", "price_minor"):
        body.pop(key)
    assert not ProductBody.model_validate(body).enabled
    with pytest.raises(ValidationError):
        ProductBody.model_validate(body | {"attributes": {"password": "forbidden"}})
    with pytest.raises(ValidationError):
        QuoteBody.model_validate(
            dict(
                user_id=str(uuid4()), plan_id=str(uuid4()), request_key=str(uuid4()), amount_minor=1
            )
        )
