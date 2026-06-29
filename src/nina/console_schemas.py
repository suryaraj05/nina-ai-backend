"""Pydantic request/response models for the console API.

Pure data shapes for the FastAPI routes in ``console_app``. Kept separate so the
route layer reads as behavior, not field declarations, and so schemas can be
imported without pulling in the whole app.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OrgCreate(BaseModel):
    name: str
    ownerEmail: str | None = None


class SiteCreate(BaseModel):
    orgId: str
    name: str
    baseUrl: str
    currency: str = "USD"
    locales: list[str] = Field(default_factory=lambda: ["en"])
    markets: list[str] = Field(default_factory=list)
    allowedOrigins: list[str] = Field(default_factory=list)


class KeyIssueIn(BaseModel):
    siteId: str
    environment: str = "test"
    kind: str = "pk"


class KeyVerifyIn(BaseModel):
    apiKey: str
    siteId: str | None = None
    origin: str | None = None
    pageUrl: str | None = None
    clientIp: str | None = None


class CliTokenIn(BaseModel):
    orgId: str
    label: str = "default"


class WizardInitIn(BaseModel):
    orgName: str
    ownerEmail: str | None = None
    siteName: str
    baseUrl: str
    country: str = "IN"
    currency: str = "INR"
    languages: list[str] = Field(default_factory=lambda: ["en", "hi"])


class WizardApiConnectIn(BaseModel):
    siteId: str
    apiBaseUrl: str
    searchPath: str = "/api/v1/products/search"
    listCategoriesPath: str = "/api/v1/categories"


class WizardGenerateIn(BaseModel):
    configDir: str
    strict: bool = True
    probe: bool = False


class WizardValidateIn(BaseModel):
    agentPath: str
    strict: bool = True
    probe: bool = False


class RegistrarExportIn(BaseModel):
    siteId: str
    outputPath: str


class SeoSitemapIn(BaseModel):
    siteId: str
    sitemapUrl: str | None = None
    rawSitemapXml: str | None = None


class SeoEmbedHealthIn(BaseModel):
    siteUrl: str


class SiteContractIn(BaseModel):
    contract: dict[str, Any]


class SiteLlmConfigIn(BaseModel):
    llmConfig: dict[str, Any]


class MultiTenantQueryIn(BaseModel):
    message: str = ""
    transcript: str = ""
    sessionId: str
    page_context: dict[str, Any] | None = None
    session_hints: dict[str, Any] | None = None
    confirmed: bool = False
    replayQueued: bool = False


class OnboardingPackIn(BaseModel):
    siteId: str | None = None
    siteName: str | None = None
    baseUrl: str | None = None
    locales: list[str] = Field(default_factory=lambda: ["en"])
    markets: list[str] = Field(default_factory=list)
    allowedOrigins: list[str] = Field(default_factory=list)
    apiBaseUrl: str | None = None
    sitemapUrl: str | None = None
    rawSitemapXml: str | None = None
    capabilities: list[str] = Field(
        default_factory=lambda: ["search", "list_categories", "cart", "checkout"]
    )
    includeAuth: bool = False
    includeRisk: bool = False
    includeSkills: bool = True
