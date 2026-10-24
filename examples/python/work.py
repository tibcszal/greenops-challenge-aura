"""Read-only batched telemetry, using the same cloud key and EXO2 HMAC.
The status endpoint never grants clock controls or future electricity data.
"""
import base64
import hashlib
import hmac
import time
from urllib.parse import urlparse
import requests
from static import check_endpoint, client, discover, save


def status(account, timeout=10):
    check_endpoint(account["endpoint"])
    endpoint = account["endpoint"].removesuffix("/cloud/v2") + "/work/v1/status"
    expires = str(int(time.time()) + 60)
    path = urlparse(endpoint).path
    signature = base64.b64encode(hmac.new(account["secret"].encode(),
        "\n".join(["GET " + path, "", "", "", expires]).encode(), hashlib.sha256).digest()).decode()
    response = requests.get(endpoint, headers={"Authorization":
        f'EXO2-HMAC-SHA256 credential={account["key"]},expires={expires},signature={signature}'},
        timeout=timeout, allow_redirects=False)
    if response.status_code == 429:
        time.sleep(min(10, max(1, int(response.headers.get("Retry-After", "1")))))
        return None
    response.raise_for_status()
    return response.json()


def wait_and_cleanup(account, plan, state_path):
    import json
    zones = discover(account)
    wanted = {p["workload_id"] for p in plan["placements"]}
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        current = status(account)
        if current is None:
            continue
        print(json.dumps(current))
        delivered = {w["id"] for w in current["workloads"] if w["final_output_delivered"]}
        # Receipt IDs scope cleanup to this orchestrator, including retries.
        state = json.loads(state_path.read_text())
        if state["endpoint"] != account["endpoint"]:
            raise ValueError("Receipt belongs to another run")
        for workload in current["workloads"]:
            if workload["id"] not in delivered:
                continue
            ids = {vm["vm_id"] for vm in workload["assignments"]}
            for row in list(state["instances"]):
                if row["id"] in ids:
                    client(account, zones[row["zone"]]).delete_instance(id=row["id"])
                    state["instances"].remove(row)
                    save(state_path, state)
        if wanted <= delivered:
            return
        if current["state"] in {"COMPLETED", "INVALIDATED", "UNAVAILABLE"}:
            raise ValueError("Run ended before all planned final outputs were delivered")
        time.sleep(1)
    raise TimeoutError("Polling exceeded 30 minutes")
