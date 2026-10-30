from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class PropertyListing(BaseModel):
    """
    Standardized property listing entity extracted from marketplace DOM.
    Pure domain representation independent of storage or transport format (SRP compliant).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    listing_id: int = Field(gt=0, description="Unique listing identifier")
    url: str = Field(min_length=5, description="Canonical listing URL")
    title: str = Field(min_length=2, description="Listing title or headline")
    price: float = Field(gt=0, description="Current listing price in target currency")
    currency: str = Field(
        default="USD", max_length=5, description="ISO currency code (USD, EUR, GBP)"
    )
    area_sqm: float = Field(gt=0, description="Usable floor area in square meters")
    rooms: int = Field(default=1, ge=0, description="Number of residential rooms")
    floor: int = Field(default=1, description="Floor index of the property")
    total_floors: int = Field(default=1, description="Total building stories")
    posted_at: datetime | None = None
    views_total: int | None = Field(default=0, ge=0)
    views_today: int | None = Field(default=0, ge=0)
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_floor_boundary(self) -> "PropertyListing":
        if self.floor > 0 and self.total_floors > 0 and self.floor > self.total_floors:
            # If floor exceeds total_floors, normalize total_floors to floor rather than rejecting
            object.__setattr__(self, "total_floors", self.floor)
        return self

    @computed_field
    @property
    def price_per_sqm(self) -> float:
        """Computed unit economics metric."""
        return round(self.price / self.area_sqm, 2) if self.area_sqm > 0 else 0.0


class PriceAnomalySignal(BaseModel):
    """
    Generated alert when price drops significantly below historical threshold.
    """

    model_config = ConfigDict(frozen=True)

    listing_id: int
    url: str
    old_price: float
    new_price: float
    drop_pct: float
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def absolute_savings(self) -> float:
        return round(self.old_price - self.new_price, 2)
