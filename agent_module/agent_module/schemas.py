from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class CostAnalysisResult(BaseModel):
    """Structured output schema for a single AWS cost line-item analysis."""

    service: str = Field(description="AWS service name this cost line item belongs to.")
    spend: float = Field(ge=0, description="Total spend for this line item, in the given currency.")
    currency: str = Field(description="ISO currency code, e.g. USD.")
    anomaly_flag: bool = Field(description="True if this spend looks anomalous versus typical usage.")
    summary_sentence: str = Field(description="One-sentence, business-friendly summary of this line item.")


class NormalizedCostRecord(BaseModel):
    """Canonical schema that Step 1 (normalise) maps a raw, messy upstream
    record into: consistent field names, real numeric types, and nulls only
    where a value is genuinely missing."""

    service: str = Field(description="Canonical AWS service name, e.g. 'Amazon EC2', 'Amazon S3', 'Amazon RDS'.")
    account_id: str = Field(description="AWS account ID.")
    region: Optional[str] = Field(default=None, description="AWS region, or null if missing in the raw record.")
    spend: float = Field(ge=0, description="Numeric spend amount, parsed from the raw record.")
    currency: str = Field(description="ISO currency code, e.g. USD.")
    period_start: str = Field(description="Billing period start date, YYYY-MM-DD.")
    period_end: str = Field(description="Billing period end date, YYYY-MM-DD.")
    resource_id: str = Field(description="Resource identifier, e.g. an EC2 instance ID.")
    environment: Optional[str] = Field(default=None, description="Environment tag if present in the raw record, else null.")
    usage_quantity: float = Field(description="Numeric usage quantity, parsed from the raw record.")
    usage_unit: str = Field(description="Unit for usage_quantity, e.g. Hours.")


class IntentClassification(BaseModel):
    """Step 3 (Week 5) output: which of the four fixed categories a user's
    request about an enriched record belongs to."""

    intent: Literal[
        "cost_anomaly",
        "budget_forecast",
        "optimisation_recommendation",
        "usage_report",
    ] = Field(description="Exactly one of the four fixed intent categories.")


class ToolHandlerResult(BaseModel):
    """Shared output schema for all four Week 5 tool handlers, so Week 6's
    self-correct step can validate every handler's output the same way."""

    finding: str = Field(description="The main finding or recommendation, one or two sentences, directly addressing the request.")
    confidence: float = Field(ge=0, le=1, description="Self-reported confidence in this finding, from 0 (unsure) to 1 (certain).")
    supporting_fact: str = Field(description="A specific number or fact from the record, tags, or pricing context that backs the finding.")
