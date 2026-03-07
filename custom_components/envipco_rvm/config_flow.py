from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
from datetime import date
from io import StringIO
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .const import EP_BASE


class EnvipcoApiError(Exception):
    """Generic API error."""


@dataclass
class EnvipcoRvmApiClient:
    session: aiohttp.ClientSession
    username: str
    password: str

    _api_key: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _request_text(self, url: str) -> tuple[int, str]:
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return resp.status, await resp.text()

    async def _request_json(self, url: str) -> tuple[int, Any]:
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = await resp.text()
            return resp.status, data

    async def login(self) -> str:
        query = urlencode({"username": self.username, "password": self.password})
        url = f"{EP_BASE}/login?{query}"
        status, data = await self._request_json(url)
        if status != 200 or not isinstance(data, dict) or "ApiKey" not in data:
            raise EnvipcoApiError(f"Login failed: HTTP {status} - {data}")
        self._api_key = str(data["ApiKey"])
        return self._api_key

    async def get_api_key(self) -> str:
        async with self._lock:
            if self._api_key:
                return self._api_key
            return await self.login()

    async def _ensure_key_and_retry_json(self, url_builder) -> Any:
        api_key = await self.get_api_key()
        url = url_builder(api_key)
        status, data = await self._request_json(url)
        if status in (303, 304):
            async with self._lock:
                self._api_key = None
                api_key = await self.login()
            url = url_builder(api_key)
            status, data = await self._request_json(url)
        if status != 200:
            raise EnvipcoApiError(f"HTTP {status}: {data}")
        return data

    async def _ensure_key_and_retry_csv(self, url_builder) -> list[dict[str, str]]:
        api_key = await self.get_api_key()
        url = url_builder(api_key)
        status, text = await self._request_text(url)
        if status in (303, 304):
            async with self._lock:
                self._api_key = None
                api_key = await self.login()
            url = url_builder(api_key)
            status, text = await self._request_text(url)
        if status != 200:
            raise EnvipcoApiError(f"HTTP {status}: {text}")
        return [row for row in csv.DictReader(StringIO(text))]

    async def rvm_stats(self, rvms: list[str], for_date: date) -> dict[str, Any]:
        def build(api_key: str) -> str:
            params: list[tuple[str, str]] = [("apiKey", api_key), ("rvmDate", for_date.isoformat())]
            params.extend(("rvms", rvm_id) for rvm_id in rvms or [])
            return f"{EP_BASE}/rvmStats?{urlencode(params)}"

        data = await self._ensure_key_and_retry_json(build)
        if isinstance(data, dict):
            return data.get("rvmData", {}) or {}
        return {}

    async def rejects(self, rvms: list[str], start: date, end: date, include_acceptance: bool = True) -> list[dict[str, str]]:
        def build(api_key: str) -> str:
            params: list[tuple[str, str]] = [
                ("apiKey", api_key),
                ("startDate", start.isoformat()),
                ("endDate", end.isoformat()),
            ]
            if include_acceptance:
                params.append(("acceptance", "yes"))
            params.extend(("rvms", rvm_id) for rvm_id in rvms or [])
            return f"{EP_BASE}/rejects?{urlencode(params)}"

        return await self._ensure_key_and_retry_csv(build)


    async def site_data(self, site_id: str) -> dict[str, Any]:
        def build(api_key: str) -> str:
            params = [("apiKey", api_key), ("siteId", str(site_id))]
            return f"{EP_BASE}/siteData?{urlencode(params)}"

        data = await self._ensure_key_and_retry_json(build)
        return data if isinstance(data, dict) else {}

    async def rvms(self) -> list[str]:
        data = await self.rvm_stats(rvms=[], for_date=date.today())
        if isinstance(data, dict):
            return sorted([str(k).strip() for k in data.keys() if str(k).strip()])
        return []
