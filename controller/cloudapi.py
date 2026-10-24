"""Cloud control plane: zone discovery and VM lifecycle.

Wraps the official Python Exoscale binding. Every HTTP call is pinned to the one
configured run by RunOnlyAdapter, so a bad endpoint fails loudly instead of
reaching the real cloud or another run.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .endpoints import Account, check_cloud_endpoint

USER_DATA_SCHEMA = "1.0"
DISK_SIZE_GIB = 10


def workload_binding(workload_id: str) -> str:
    """Base64 user-data that binds a VM to a workload. The simulator reads this."""
    payload = {"greenops": {"schema_version": USER_DATA_SCHEMA, "workload_id": workload_id}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class RunOnlyAdapter(requests.adapters.HTTPAdapter):
    """Refuses any request outside the configured run's URL prefix."""

    def __init__(self, root: str):
        self.root = urlparse(root)
        super().__init__()

    def send(self, request, **kwargs):
        target = urlparse(request.url)
        prefix = self.root.path.rsplit("/v2", 1)[0] + "/"
        if ((target.scheme, target.netloc) != (self.root.scheme, self.root.netloc)
                or not target.path.startswith(prefix)):
            raise ValueError("Request escaped the configured cloud run")
        return super().send(request, **kwargs)


@dataclass
class Instance:
    id: str
    name: str
    state: str
    zone: str


class ZoneClient:
    """Instance operations within one cloud zone."""

    def __init__(self, account: Account, zone_slug: str, endpoint: str):
        from exoscale.api.v2 import Client

        self.zone_slug = zone_slug
        self.endpoint = check_cloud_endpoint(endpoint, account.endpoint)
        self._client = Client(account.key, account.secret, url=self.endpoint)
        guard = RunOnlyAdapter(account.endpoint)
        self._client.http_client.mount("http://", guard)
        self._client.http_client.mount("https://", guard)
        self._client.http_client.max_redirects = 0
        self._template_id: str | None = None
        self._type_ids: dict[tuple[str, str], str] = {}

    def instances(self) -> list[Instance]:
        return [Instance(v["id"], v["name"], v.get("state", "unknown"), self.zone_slug)
                for v in self._client.list_instances()["instances"]]

    def template_id(self) -> str:
        if self._template_id is None:
            self._template_id = self._client.list_templates(visibility="public")["templates"][0]["id"]
        return self._template_id

    def instance_type_id(self, family: str, size: str) -> str:
        """Resolve the run's own instance-type id; catalog ids are not assumed live."""
        wanted = (family, size)
        if wanted not in self._type_ids:
            match = next(t for t in self._client.list_instance_types()["instance-types"]
                         if t["family"] == family and t["size"] == size)
            self._type_ids[wanted] = match["id"]
        return self._type_ids[wanted]

    def create_instance(self, name: str, family: str, size: str, workload_id: str) -> str:
        """Create one VM bound to a workload. Returns its id; admission is enough."""
        operation = self._client.create_instance(
            name=name,
            instance_type={"id": self.instance_type_id(family, size)},
            template={"id": self.template_id()},
            disk_size=DISK_SIZE_GIB,
            user_data=workload_binding(workload_id),
        )
        return operation["reference"]["id"]

    def delete_instance(self, instance_id: str) -> None:
        self._client.delete_instance(id=instance_id)

    def stop_instance(self, instance_id: str) -> None:
        """Stop keeps all progress and staged input, and costs no electricity."""
        self._client.stop_instance(id=instance_id)

    def start_instance(self, instance_id: str) -> None:
        """Resume skips boot and input staging already completed."""
        self._client.start_instance(id=instance_id)


class Cloud:
    """Run-scoped cloud facade with one cached client per zone."""

    def __init__(self, account: Account):
        self.account = account
        self._endpoints: dict[str, str] | None = None
        self._clients: dict[str, ZoneClient] = {}

    def zone_endpoints(self) -> dict[str, str]:
        if self._endpoints is None:
            discovery = ZoneClient(self.account, "discovery", self.account.endpoint)
            zones = discovery._client.list_zones()["zones"]
            self._endpoints = {
                z["name"]: check_cloud_endpoint(z["api-endpoint"], self.account.endpoint)
                for z in zones}
        return self._endpoints

    def zone(self, zone_slug: str) -> ZoneClient:
        if zone_slug not in self._clients:
            endpoints = self.zone_endpoints()
            if zone_slug not in endpoints:
                raise ValueError(f"Zone {zone_slug} is not offered by this run")
            self._clients[zone_slug] = ZoneClient(self.account, zone_slug, endpoints[zone_slug])
        return self._clients[zone_slug]

    def all_instances(self) -> list[Instance]:
        return [vm for slug in self.zone_endpoints() for vm in self.zone(slug).instances()]
