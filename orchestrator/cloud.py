"""Real environment: the run's cloud API (Exoscale binding), work status and grid API.

Only this run's endpoints are used.  The grid endpoint must live under the same
origin and run prefix as the cloud endpoint (same check as examples/python/grid.py).
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "python"))
import static   # noqa: E402  starter helpers: endpoint checks, discovery, signed client
import work     # noqa: E402  signed work-status call with 429 backoff

from .core import parse_ts   # noqa: E402

# Whether the starter's work.status that actually loaded accepts a read timeout.  An older
# helper (the one baked into the starter image) does not, and calling it with the keyword
# would raise TypeError on every tick until the error budget aborts the run.
_STATUS_TAKES_TIMEOUT = "timeout" in inspect.signature(work.status).parameters


class CloudEnv:
    def __init__(self, account: dict, grid_endpoint: str | None = None, grid_secret: str | None = None):
        self.account = account
        self.zones = static.discover(account)                 # slug -> zone API endpoint
        self.status_timeout = float(os.environ.get("GREENOPS_STATUS_TIMEOUT", "4"))
        self._local, self._types, self._tmpl = threading.local(), {}, {}
        self._lock = threading.Lock()
        self.grid_secret = grid_secret
        self.grid = None
        if grid_endpoint and grid_secret:
            cloud, grid = urlparse(account["endpoint"]), urlparse(grid_endpoint.rstrip("/"))
            prefix = cloud.path.split("/cloud/")[0]
            if (grid.scheme, grid.netloc, grid.path) != (cloud.scheme, cloud.netloc, prefix + "/grid/v4"):
                raise SystemExit("Electricity endpoint must belong to this cloud run")
            self.grid = grid_endpoint.rstrip("/")

    # ---------------------------------------------------------------- cloud
    def _c(self, zone):
        cache = self._local.__dict__.setdefault("clients", {})
        if zone not in cache:
            cache[zone] = static.client(self.account, self.zones[zone])
        return cache[zone]

    def warm(self):
        """Look up instance types and templates of every zone BEFORE the clock starts
        (virtual time is frozen until the first VM), so later creates need one call."""
        def one(zone):
            c = self._c(zone)
            types = {(t["family"], t["size"]): t["id"] for t in c.list_instance_types()["instance-types"]}
            tmpl = c.list_templates(visibility="public")["templates"][0]["id"]
            with self._lock:
                self._types[zone], self._tmpl[zone] = types, tmpl
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(one, self.zones))

    def status(self):
        # The simulator stalls on a minority of status reads (measured: median 2.8 s, but
        # roughly a quarter never answer).  At 720x a wall second is 12 virtual minutes, so the
        # stock 10 s timeout spends 2 virtual hours per stall with the control loop blocked and
        # nothing pausable.  Cut the loss: a stalled read is abandoned and retried next tick.
        #
        # The helper is resolved at runtime and is NOT ours in the submission image, which layers
        # orchestrator/ onto the starter image and keeps that image's examples/python.  Never
        # assume our signature is the one that loaded.
        if _STATUS_TAKES_TIMEOUT:
            return work.status(self.account, timeout=self.status_timeout)
        return work.status(self.account)

    def create(self, zone, profile, workload_id, name):
        c = self._c(zone)
        if zone not in self._types:
            self.warm()
        kwargs = {}
        if workload_id:
            binding = {"greenops": {"schema_version": "1.0", "workload_id": workload_id}}
            kwargs["user_data"] = base64.b64encode(json.dumps(binding).encode()).decode()
        try:
            op = c.create_instance(name=name, instance_type={"id": self._types[zone][(profile["family"], profile["size"])]},
                                   template={"id": self._tmpl[zone]}, disk_size=10, **kwargs)
            return op["reference"]["id"]
        except Exception:
            # Lost response? A deterministic name lets us recover instead of creating a duplicate.
            existing = [v for v in c.list_instances()["instances"] if v["name"] == name]
            if existing:
                return existing[0]["id"]
            raise

    def set_power(self, zone, vm_id, run: bool):
        c = self._c(zone)
        fn = getattr(c, "start_instance" if run else "stop_instance", None)
        if fn is not None:
            fn(id=vm_id)
            return
        # Fallback: PUT /instance/{id}:start|:stop through the same signed session.
        r = c.http_client.put(f"{self.zones[zone]}/instance/{vm_id}:{'start' if run else 'stop'}",
                              allow_redirects=False, timeout=20)
        r.raise_for_status()

    def delete(self, zone, vm_id):
        c = self._c(zone)
        try:
            c.delete_instance(id=vm_id)
        except Exception:
            if any(v["id"] == vm_id for v in c.list_instances()["instances"]):
                raise                                   # still there: real failure

    def adopt(self):
        rows = []
        for zone in self.zones:
            for v in self._c(zone).list_instances()["instances"]:
                if v["name"].startswith("orch-"):
                    rows.append({"id": v["id"], "zone": zone, "name": v["name"]})
        return rows

    # ----------------------------------------------------------------- grid
    def forecasts(self, areas):
        with ThreadPoolExecutor(max_workers=8) as ex:
            return dict(zip(areas, ex.map(self.forecast, areas)))

    def forecast(self, area):
        """Currently published 24 h forecast of THIS run; None if unavailable."""
        if not self.grid:
            return None
        try:
            r = requests.get(self.grid + "/carbon-intensity/forecast",
                             params={"zone": area, "horizonHours": 24, "temporalGranularity": "hourly"},
                             headers={"auth-token": self.grid_secret}, timeout=20, allow_redirects=False)
            r.raise_for_status()
            return [(parse_ts(p["datetime"]), float(p["carbonIntensity"])) for p in r.json()["forecast"]]
        except (requests.RequestException, KeyError, ValueError):
            return None
