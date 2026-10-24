"""Electricity data for this run only.

Competition rules: all electricity information used for decisions must come from
this run's own grid endpoint, restricted to what is published at the run's current
simulated time. No external feed, no other run, no cached history from a previous
attempt. Requests cannot advance the clock.

Not used by the phase 1 planner. This is the hook a carbon-aware planner reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import requests

from .endpoints import Account

TIMEOUT_SECONDS = 20
ALLOWED_HORIZONS = (6, 24)


@dataclass(frozen=True)
class CarbonPoint:
    datetime: datetime
    carbon_intensity: Decimal

    @classmethod
    def parse(cls, raw: dict) -> "CarbonPoint":
        return cls(datetime.fromisoformat(raw["datetime"].replace("Z", "+00:00")),
                   Decimal(str(raw["carbonIntensity"])))


class Grid:
    """Read-only client for one run's electricity API."""

    def __init__(self, account: Account):
        if not account.grid_secret:
            raise ValueError("Set GREENOPS_GRID_SECRET to read this run's electricity data")
        self.endpoint = account.resolved_grid_endpoint()
        self._headers = {"auth-token": account.grid_secret}
        self._session = requests.Session()

    def _get(self, path: str, params: dict) -> dict:
        response = self._session.get(self.endpoint + path, params=params,
                                     headers=self._headers, timeout=TIMEOUT_SECONDS,
                                     allow_redirects=False)
        response.raise_for_status()
        return response.json()

    def latest(self, area: str) -> CarbonPoint:
        """Most recently published observation; it lags the simulated clock."""
        return CarbonPoint.parse(self._get("/carbon-intensity/latest", {"zone": area}))

    def forecast(self, area: str, horizon_hours: int = 24) -> list[CarbonPoint]:
        """Forecast vintage currently issued for this run. 6 or 24 hours only."""
        if horizon_hours not in ALLOWED_HORIZONS:
            raise ValueError(f"horizonHours must be one of {ALLOWED_HORIZONS}")
        payload = self._get("/carbon-intensity/forecast",
                            {"zone": area, "horizonHours": horizon_hours})
        return [CarbonPoint.parse(p) for p in payload["forecast"]]

    def history(self, area: str) -> list[CarbonPoint]:
        """The most recent 24 published hours for this area."""
        payload = self._get("/carbon-intensity/history", {"zone": area})
        return [CarbonPoint.parse(p) for p in payload["history"]]

    def renewable_level(self, area: str) -> str:
        """Latest renewable level: low, moderate or high. Not a percentage."""
        payload = self._get("/renewable-percentage-level/latest", {"zone": area})
        return payload["data"][0]["level"]
