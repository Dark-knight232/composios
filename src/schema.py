from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AppResearchRecord(BaseModel):
    """Strict structured output for a single app's developer/API metadata (Pass 1)."""

    app_name: str = Field(..., description="Name of the software application researched")
    category: Literal[
        "CRM & Sales",
        "DevTools & Infra",
        "Finance & Payments",
        "Productivity & Docs",
        "HR & Recruiting",
        "Marketing & Social",
        "Support & Ticketing",
        "Data & Analytics",
        "E-Commerce & ERP",
        "Security & Identity",
        "Other",
    ] = Field(..., description="App category")
    one_liner: str = Field(..., description="Max 15 words describing what the app does")
    auth_methods: list[
        Literal[
            "OAuth2",
            "API_Key",
            "Basic_Auth",
            "Bearer_Token",
            "Custom_Header",
            "JWT_Service_Account",
            "None_Public",
            "Unknown",
        ]
    ] = Field(..., description="Authentication methods supported by the API")
    access_model: Literal[
        "Self_Serve_Free",
        "Self_Serve_Paid_Or_Trial",
        "Admin_Approval_Required",
        "Partner_Or_Sales_Gated",
        "No_Public_API",
    ] = Field(..., description="How developers gain access to the API")
    access_model_notes: str = Field(
        ..., description="Specific details on rate limits, tier restrictions, or approval forms"
    )
    api_surface: list[Literal["REST", "GraphQL", "gRPC", "Webhooks", "SDK_Only", "None"]] = Field(
        ..., description="API surface styles exposed"
    )
    endpoint_breadth: Literal["Broad_50_Plus", "Moderate_15_To_50", "Narrow_Under_15", "None"] = Field(
        ..., description="Rough breadth of the public API surface"
    )
    existing_mcp: Literal["Official_MCP", "Community_MCP", "None_Found"] = Field(
        ..., description="Whether an MCP server already exists for this app"
    )
    existing_mcp_url: str | None = Field(None, description="URL of existing MCP server, if found")
    buildability_verdict: Literal["Buildable_Today", "Buildable_With_Workaround", "Blocked"] = Field(
        ..., description="Can an MCP integration be built today?"
    )
    primary_blocker: str | None = Field(
        None, description="Exact blocker when not Buildable_Today (e.g. 'Partner review required')"
    )
    evidence_urls: list[str] = Field(
        ..., description="Specific developer docs/API reference URLs found in search/extract outputs"
    )
    confidence_score: int = Field(..., ge=1, le=5, description="1-5; scores < 4 flag the row for Pass 2")
    pass_1_raw_notes: str = Field(..., description="Raw notes from Pass 1 research")

    @field_validator("one_liner")
    @classmethod
    def _one_liner_max_words(cls, v: str) -> str:
        if len(v.split()) > 15:
            raise ValueError("one_liner must be at most 15 words")
        return v

    @field_validator("evidence_urls")
    @classmethod
    def _evidence_urls_must_be_http(cls, v: list[str]) -> list[str]:
        for u in v:
            if not u.startswith(("http://", "https://")):
                raise ValueError(f"evidence_urls must be http(s) URLs, got: {u}")
        return v

    @field_validator("primary_blocker")
    @classmethod
    def _blocker_consistency(cls, v: str | None, info) -> str | None:
        verdict = info.data.get("buildability_verdict")
        if verdict != "Buildable_Today" and not v:
            raise ValueError("primary_blocker is required when buildability_verdict is not Buildable_Today")
        if verdict == "Buildable_Today":
            return None
        return v
