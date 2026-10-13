from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.money import MAX_AMOUNT
from app.security.schemas import JsonUUID, StepUpBody, StrictBody

Money = Annotated[int, Field(ge=0, le=MAX_AMOUNT)]


class Attributes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region: str | None = Field(default=None, max_length=64)
    transport: Literal["tcp", "ws", "grpc"] | None = None
    tls: bool | None = None


class CategoryBody(StepUpBody):
    kind: Literal["categories"]
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=256)
    parent_id: JsonUUID | None = None
    enabled: bool = False
    position: int = Field(default=0, ge=0, le=100000)


class ProductBody(StepUpBody):
    kind: Literal["products"]
    category_id: JsonUUID
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=10000)
    service_type: Literal[
        "vless", "vmess", "trojan", "shadowsocks", "wireguard", "openvpn", "subscription"
    ]
    inventory_mode: Literal["manual", "provisioned"]
    economic: bool = False
    enabled: bool = False
    position: int = Field(default=0, ge=0, le=100000)
    attributes: Attributes = Field(default_factory=Attributes)


class PlanBody(StepUpBody):
    kind: Literal["plans"]
    product_id: JsonUUID
    slug: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=256)
    price_minor: Money
    currency: Literal["IRT"] = "IRT"
    duration_days: int | None = Field(default=None, gt=0, le=36500)
    traffic_bytes: int | None = Field(default=None, gt=0, le=MAX_AMOUNT)
    device_limit: int = Field(default=1, gt=0, le=10000)
    min_increment: Money = 0
    max_increment: Money = 0
    increment_step: int = Field(default=1, gt=0, le=MAX_AMOUNT)
    randomization_method: Literal["uniform_discrete"] = "uniform_discrete"
    enabled: bool = False

    @model_validator(mode="after")
    def pricing(self) -> "PlanBody":
        if (
            self.max_increment < self.min_increment
            or self.price_minor + self.max_increment > MAX_AMOUNT
        ):
            raise ValueError("Invalid increment range or price overflow")
        if self.min_increment % self.increment_step or self.max_increment % self.increment_step:
            raise ValueError("Increment bounds must align with their step")
        return self


class QuoteBody(StrictBody):
    user_id: JsonUUID
    plan_id: JsonUUID
    request_key: JsonUUID


CatalogBody = Annotated[CategoryBody | ProductBody | PlanBody, Field(discriminator="kind")]
