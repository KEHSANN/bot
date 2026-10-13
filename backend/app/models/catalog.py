from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdentityMixin


class ProductCategory(IdentityMixin, Base):
    __tablename__ = "product_categories"
    __table_args__ = (
        CheckConstraint("length(title) > 0", name="nonempty_title"),
        CheckConstraint("length(slug) > 0", name="nonempty_slug"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_product_categories_enabled_position", "enabled", "position"),
    )
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    title: Mapped[str] = mapped_column(String(256))
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="RESTRICT")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    position: Mapped[int] = mapped_column(Integer, server_default="0")


class Product(IdentityMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(title) > 0", name="nonempty_title"),
        CheckConstraint("length(slug) > 0", name="nonempty_slug"),
        CheckConstraint("length(service_type) > 0", name="nonempty_service_type"),
        CheckConstraint("inventory_mode IN ('manual','provisioned')", name="valid_inventory_mode"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="object_attributes"),
        Index("ix_products_category_enabled_position", "category_id", "enabled", "position"),
    )
    category_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_categories.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, server_default="")
    service_type: Mapped[str] = mapped_column(String(64))
    inventory_mode: Mapped[str] = mapped_column(String(16))
    economic: Mapped[bool] = mapped_column(Boolean, server_default="false")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class ProductPlan(IdentityMixin, Base):
    __tablename__ = "product_plans"
    __table_args__ = (
        UniqueConstraint("product_id", "slug"),
        CheckConstraint(
            "randomization_method = 'uniform_discrete'", name="valid_randomization_method"
        ),
        CheckConstraint("length(title) > 0 AND length(slug) > 0", name="nonempty_labels"),
        CheckConstraint("currency = 'IRT'", name="currency_irt"),
        CheckConstraint("price_minor >= 0", name="price_nonnegative"),
        CheckConstraint("duration_days IS NULL OR duration_days > 0", name="duration_positive"),
        CheckConstraint("traffic_bytes IS NULL OR traffic_bytes > 0", name="traffic_positive"),
        CheckConstraint("device_limit > 0", name="device_limit_positive"),
        CheckConstraint(
            "min_increment >= 0 AND max_increment >= min_increment", name="increment_range"
        ),
        CheckConstraint("increment_step > 0", name="increment_step_positive"),
        CheckConstraint(
            "CASE WHEN increment_step > 0 THEN "
            "min_increment % increment_step = 0 AND max_increment % increment_step = 0 "
            "ELSE false END",
            name="increment_aligned",
        ),
        CheckConstraint(
            "price_minor::numeric + max_increment::numeric <= 9223372036854775807",
            name="price_no_overflow",
        ),
        CheckConstraint("pricing_version > 0", name="pricing_version_positive"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="object_attributes"),
        Index("ix_product_plans_product_enabled", "product_id", "enabled"),
    )
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    slug: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(256))
    currency: Mapped[str] = mapped_column(String(3), server_default="IRT")
    price_minor: Mapped[int] = mapped_column(BigInteger)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    traffic_bytes: Mapped[int | None] = mapped_column(BigInteger)
    device_limit: Mapped[int] = mapped_column(Integer, server_default="1")
    min_increment: Mapped[int] = mapped_column(BigInteger, server_default="0")
    max_increment: Mapped[int] = mapped_column(BigInteger, server_default="0")
    increment_step: Mapped[int] = mapped_column(BigInteger, server_default="1")
    randomization_method: Mapped[str] = mapped_column(String(32), server_default="uniform_discrete")
    pricing_version: Mapped[int] = mapped_column(Integer, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
