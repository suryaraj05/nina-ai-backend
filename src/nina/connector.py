"""Drop-in FastAPI router for hybrid NINA merchant integrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .api_security import RateLimiter, verify_api_key
from .contract import load_agent
from .instructions import turn_to_instructions

if TYPE_CHECKING:
    from . import Nina


class QueryIn(BaseModel):
    message: str = ""
    transcript: str = ""
    sessionId: str
    page_context: dict[str, Any] | None = None
    session_hints: dict[str, Any] | None = None
    confirmed: bool = False
    replayQueued: bool = False


class NinaConnector:
    """Reusable router mounting /v1/query + /agent.json + /sdk assets."""

    def __init__(
        self,
        *,
        nina: "Nina",
        contract_path: str | Path,
        sdk_dir: str | Path,
        static_dir: str | Path | None = None,
        query_path: str = "/v1/query",
        report_path: str = "/v1/report-broken-selector",
        rate_limit: int = 60,
        rate_window: int = 60,
    ):
        self.nina = nina
        self.contract_path = Path(contract_path)
        self.sdk_dir = Path(sdk_dir)
        self.static_dir = Path(static_dir) if static_dir else None
        self.query_path = query_path
        self.report_path = report_path
        self._limiter = RateLimiter(max_requests=rate_limit, window_seconds=rate_window)
        self.contract = load_agent(self.contract_path)
        self.router = APIRouter()
        self._mount_routes()

    async def _api_guard(
        self,
        request: Request,
        x_nina_api_key: str | None,
        *,
        site_id: str | None,
        page_url: str | None,
    ) -> JSONResponse | None:
        client = request.client.host if request.client else "unknown"
        origin = request.headers.get("origin")
        ok_key, key_err = verify_api_key(
            x_nina_api_key,
            site_id=site_id,
            origin=origin,
            page_url=page_url,
            client_ip=client,
        )
        if not ok_key:
            return JSONResponse(status_code=401, content={"ok": False, "data": None, "error": key_err})
        ok_rate, rate_err = self._limiter.allow(f"{client}:{x_nina_api_key or 'anon'}")
        if not ok_rate:
            return JSONResponse(status_code=429, content={"ok": False, "data": None, "error": rate_err})
        return None

    def _mount_routes(self) -> None:
        @self.router.post(self.query_path)
        async def query(
            body: QueryIn,
            request: Request,
            x_nina_api_key: str | None = Header(default=None, alias="X-NINA-API-Key"),
        ):
            page_url = (body.page_context or {}).get("url")
            blocked = await self._api_guard(
                request,
                x_nina_api_key,
                site_id=self.contract.get("site", {}).get("id"),
                page_url=page_url,
            )
            if blocked:
                return blocked

            self.nina._core.config = {
                **(self.nina._core.config or {}),
                "_agentContract": self.contract,
                "_sessionHints": body.session_hints or {},
                "_pageId": (body.page_context or {}).get("pageId"),
                "_sessionAuthenticated": True,
            }
            envelope = await self.nina.chat(
                body.transcript or body.message,
                body.sessionId,
                replay_queued=body.replayQueued,
                confirmed=body.confirmed,
            )
            if envelope.get("ok") and envelope.get("data"):
                turn = dict(envelope["data"])
                if turn.get("intent") != "blocked":
                    turn["instructions"] = turn_to_instructions(
                        self.contract,
                        turn,
                        page_context=body.page_context,
                        session_hints=body.session_hints,
                        confirmed=body.confirmed,
                    )
                envelope = {**envelope, "data": turn}
            return envelope

        @self.router.post(self.report_path)
        async def report_broken_selector(payload: dict[str, Any]):
            # Connector keeps this endpoint for SDK compatibility.
            return {"ok": True, "data": {"accepted": True, "report": payload}}

        @self.router.get("/agent.json")
        async def agent_json():
            return self.contract

    def mount(self, app) -> None:
        app.include_router(self.router)
        app.mount("/sdk", StaticFiles(directory=self.sdk_dir), name="sdk")
        if self.static_dir:
            app.mount("/", StaticFiles(directory=self.static_dir, html=True), name="static")

