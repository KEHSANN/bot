"""Catalog: categories, products, plans."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vpnbot.db.base import Base, TimestampMixin, enum_type
from vpnbot.db.enums import ButtonStyle, ProductType


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    products: Mapped[list[Product]] = relationship(back_populates="category", lazy="noload")


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    image_file_id: Mapped[str | None] = mapped_column(sa.String(256))
    product_type: Mapped[ProductType] = mapped_column(
        enum_type(ProductType), default=ProductType.CONFIG, nullable=False
    )
    #: Colour of this product's button in the customer shop list
    #: (Telegram native button styles; DEFAULT = no colour).
    button_style: Mapped[ButtonStyle] = mapped_column(
        enum_type(ButtonStyle), default=ButtonStyle.DEFAULT, nullable=False
    )
    specs: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False, index=True)
    sales_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    category: Mapped[Category | None] = relationship(back_populates="products", lazy="joined")
    plans: Mapped[list[Plan]] = relationship(
        back_populates="product", lazy="selectin", order_by="Plan.sort_order"
    )

    @property
    def is_on_sale(self) -> bool:
        return self.is_active and self.sales_enabled and self.archived_at is None


class Plan(Base, TimestampMixin):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    price_irt: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    duration_days: Mapped[int | None] = mapped_column(sa.Integer)
    traffic_gb: Mapped[int | None] = mapped_column(sa.Integer)
    device_limit: Mapped[int | None] = mapped_column(sa.Integer)
    specs: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    product: Mapped[Product] = relationship(back_populates="plans", lazy="noload")

    @property
    def is_on_sale(self) -> bool:
        return self.is_active and self.archived_at is None
