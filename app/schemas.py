import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def normal_barcode(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{4,32}", text):
        raise ValueError("Barcode must contain 4-32 digits; leading zeros must be stored as text in Excel")
    return text


def valid_gtin(value: str) -> bool:
    if len(value) not in (8, 12, 13, 14) or not value.isdigit():
        return False
    return (
        sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(value[:-1]))) % 10
        == (-int(value[-1])) % 10
    )


class ProductInput(Strict):
    code: str | None = Field(default=None, max_length=80)
    barcode: str
    barcodes: list[str] = Field(default_factory=list, max_length=40)
    name: str = Field(min_length=1, max_length=400)
    brand: str = Field(default="", max_length=180)
    weight: str = Field(default="", max_length=80)
    unit: str = Field(default="EACH", max_length=40)
    active: bool = True

    @field_validator("barcode")
    @classmethod
    def check_barcode(cls, v):
        return normal_barcode(v)

    @field_validator("barcodes")
    @classmethod
    def check_barcodes(cls, v):
        return list(dict.fromkeys(normal_barcode(x) for x in v))

    @field_validator("code")
    @classmethod
    def check_code(cls, v):
        if not v:
            return None
        if v.startswith(("#", "=", "+", "@")):
            raise ValueError("Invalid internal code")
        return v


class Credentials(Strict):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value):
        value = value.strip().lower()
        if not value:
            raise ValueError("Username is required")
        return value


class UserInput(Credentials):
    password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "staff"] = "staff"


class UserUpdate(Strict):
    active: bool
    role: Literal["admin", "staff"]
    password: str | None = Field(default=None, min_length=12, max_length=128)


class PromoItem(Strict):
    product_id: int = Field(gt=0)
    price: Decimal = Field(gt=0, le=9999999.99, max_digits=9, decimal_places=2)
    previous_price: Decimal | None = Field(default=None, gt=0, le=9999999.99, max_digits=9, decimal_places=2)
    image_id: int | None = Field(default=None, gt=0)


class CampaignInput(Strict):
    name: str = Field(min_length=1, max_length=180)
    start_date: date
    end_date: date
    status: Literal["draft", "active", "archived"] = "draft"
    items: list[PromoItem] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def check_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("End date must follow start date")
        if len({p.product_id for p in self.items}) != len(self.items):
            raise ValueError("Product appears twice in campaign")
        return self


class LabelConfig(Strict):
    orientation: Literal["portrait", "landscape"] = "portrait"
    image_scale: float = Field(default=1, ge=0.5, le=1.0)
    image_x: float = Field(default=0, ge=-0.08, le=0.08)
    image_y: float = Field(default=0, ge=-0.08, le=0.08)
    red: str = Field(default="#C63830", pattern=r"^#[0-9a-fA-F]{6}$")
    logo_id: int | None = Field(default=None, gt=0)


class PrintInput(Strict):
    promotion_ids: list[int] = Field(min_length=1, max_length=1000)
    template: LabelConfig = Field(default_factory=LabelConfig)


class TemplateInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    config: LabelConfig
