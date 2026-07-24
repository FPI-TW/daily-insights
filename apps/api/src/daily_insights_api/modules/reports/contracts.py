from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Locale = Literal["zh-TW", "zh-CN", "en"]
SUPPORTED_LOCALES: frozenset[str] = frozenset(("zh-TW", "zh-CN", "en"))
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]


class ContractModel(BaseModel):
    """Strict immutable DTO used at the report persistence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricValue(ContractModel):
    id: Identifier
    value: Decimal
    unit_code: Identifier


class ChartPoint(ContractModel):
    x: str = Field(min_length=1, max_length=100)
    value: Decimal | None


class ChartSeries(ContractModel):
    id: Identifier
    points: tuple[ChartPoint, ...]


class ChartData(ContractModel):
    id: Identifier
    unit_code: Identifier
    series: tuple[ChartSeries, ...]

    @model_validator(mode="after")
    def require_unique_series(self) -> Self:
        series_ids = [series.id for series in self.series]
        if len(series_ids) != len(set(series_ids)):
            raise ValueError("chart series ids must be unique")
        return self


class PublicationContent(ContractModel):
    """Locale-neutral application-owned values.

    Decimal is intentionally retained in memory and serialized as a JSON string
    by Pydantic. This avoids locale-specific formatting and binary float loss.
    """

    schema_version: Identifier
    market_code: Identifier
    as_of: date
    metrics: tuple[MetricValue, ...] = ()
    charts: tuple[ChartData, ...] = ()

    @model_validator(mode="after")
    def require_unique_element_ids(self) -> Self:
        element_ids = [metric.id for metric in self.metrics]
        element_ids.extend(chart.id for chart in self.charts)
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("metric and chart ids must be unique within a publication")
        return self


class LocalizedElementText(ContractModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2_000)
    unit_label: str | None = Field(default=None, max_length=100)
    series_labels: dict[Identifier, str] = Field(default_factory=dict)


class PresentationContract(ContractModel):
    schema_version: Identifier
    locale: Locale
    title: str = Field(min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=10_000)
    labels: dict[Identifier, LocalizedElementText]


class PublicationBundle(ContractModel):
    """One immutable value payload and its complete three-locale presentation."""

    content: PublicationContent
    presentations: dict[Locale, PresentationContract]

    @model_validator(mode="after")
    def validate_localized_references(self) -> Self:
        if set(self.presentations) != SUPPORTED_LOCALES:
            raise ValueError("presentations must contain exactly zh-TW, zh-CN, and en")

        metric_ids = {metric.id for metric in self.content.metrics}
        chart_series = {
            chart.id: {series.id for series in chart.series} for chart in self.content.charts
        }
        expected_element_ids = metric_ids | set(chart_series)

        for locale, presentation in self.presentations.items():
            if presentation.locale != locale:
                raise ValueError(f"presentation locale does not match key {locale}")
            if set(presentation.labels) != expected_element_ids:
                raise ValueError(f"{locale} labels must reference every metric and chart exactly")
            for metric_id in metric_ids:
                if presentation.labels[metric_id].series_labels:
                    raise ValueError(f"metric {metric_id} cannot define series labels")
            for chart_id, expected_series_ids in chart_series.items():
                if set(presentation.labels[chart_id].series_labels) != expected_series_ids:
                    raise ValueError(f"{locale} chart {chart_id} must label every series exactly")
        return self

    def content_for_storage(self) -> dict[str, object]:
        return self.content.model_dump(mode="json")

    def presentations_for_storage(self) -> dict[str, object]:
        return {
            locale: presentation.model_dump(mode="json")
            for locale, presentation in self.presentations.items()
        }
