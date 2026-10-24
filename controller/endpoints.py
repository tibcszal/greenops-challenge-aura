"""Run-scoped endpoint handling.

Every request must stay inside the one run this controller was given. Guards here
exist so a misconfigured endpoint cannot reach the real Exoscale cloud or another
run's data, which the competition rules forbid.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

CLOUD_SUFFIX = "/cloud/v2"


def check_cloud_endpoint(value: str, reference: str | None = None) -> str:
    """Validate a cloud discovery or zone endpoint, optionally against a reference run."""
    url = urlparse(value)
    if (url.scheme not in {"http", "https"} or not url.hostname
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("Expected a challenge run URL")
    parts = url.path.strip("/").split("/")
    if len(parts) not in {4, 5} or parts[0] != "runs" or parts[2] != "cloud" or parts[-1] != "v2":
        raise ValueError("Cloud endpoint must be /runs/{id}/cloud[/zone]/v2")
    if url.hostname == "exoscale.com" or url.hostname.endswith(".exoscale.com"):
        raise ValueError("Use challenge endpoints only")
    if reference:
        ref = urlparse(reference)
        if ((url.scheme, url.netloc) != (ref.scheme, ref.netloc)
                or parts[1] != ref.path.strip("/").split("/")[1]):
            raise ValueError("Discovered endpoint escaped the configured run")
    return value


@dataclass(frozen=True)
class Account:
    """One run's cloud credentials, plus the optional grid secret."""
    key: str
    secret: str
    endpoint: str
    grid_endpoint: str | None = None
    grid_secret: str | None = None

    @property
    def run_prefix(self) -> str:
        """Scheme, host and /runs/{id} shared by this run's cloud, work and grid APIs."""
        url = urlparse(self.endpoint)
        return f"{url.scheme}://{url.netloc}" + url.path.split("/cloud/")[0]

    @property
    def run_id(self) -> str:
        return urlparse(self.endpoint).path.strip("/").split("/")[1]

    @property
    def work_endpoint(self) -> str:
        return self.run_prefix + "/work/v1"

    def resolved_grid_endpoint(self) -> str:
        """The run's grid endpoint, refusing any URL outside this run."""
        expected = self.run_prefix + "/grid/v4"
        if self.grid_endpoint and self.grid_endpoint.rstrip("/") != expected:
            raise ValueError("Electricity endpoint must belong to this cloud run")
        return expected


def load_account(config_path: str | Path = "greenops-exo.json") -> Account:
    """Read run credentials from the downloaded CLI config, or from the environment.

    Organizer reruns supply EXOSCALE_API_KEY, EXOSCALE_API_SECRET and
    GREENOPS_CLOUD_ENDPOINT instead of the config file.
    """
    path = Path(config_path)
    if path.exists():
        config = json.loads(path.read_text())
        account = next(a for a in config["accounts"] if a["name"] == config["defaultaccount"])
        key, secret, endpoint = account["key"], account["secret"], account["endpoint"]
    else:
        try:
            key = os.environ["EXOSCALE_API_KEY"]
            secret = os.environ["EXOSCALE_API_SECRET"]
            endpoint = os.environ["GREENOPS_CLOUD_ENDPOINT"]
        except KeyError as missing:
            raise ValueError(
                f"No {path} and no {missing.args[0]} in the environment") from None
    check_cloud_endpoint(endpoint)
    return Account(key, secret, endpoint,
                   os.environ.get("GREENOPS_GRID_ENDPOINT"),
                   os.environ.get("GREENOPS_GRID_SECRET"))
