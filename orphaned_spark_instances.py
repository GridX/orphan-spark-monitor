#!/usr/bin/env python3
"""
Find orphaned spark-slave EC2 instances.

Lists running EC2 instances whose Name tag starts with "spark" and that carry a
JobID tag (the Azkaban execution id). For each, it looks up the execution in the
Azkaban instance matching the instance's Environment tag. If the Azkaban flow is
in a terminal "stopped" state (KILLED / FAILED / CANCELLED) and ended more than
THRESHOLD ago, but the EC2 instance is still running, the instance is reported as
orphaned.

Auth: Azkaban credentials (user/password) and base URL are read per-environment
from AWS Secrets Manager using the same AWS profile, so no manual creds needed.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import boto3
import requests
import urllib3

# Environment tag value -> Secrets Manager secret holding azkaban_url /
# azkaban_user / azkaban_password.
AZKABAN_SECRETS = {
    "prod": "prod/utility-operation/uo_service_db",
    "stage": "utility-operation/uo_service_db_stage",
    "uat": "uat/utility-operation/uo_service_db",
}

# Azkaban flow statuses that count as "killed or stopped"
STOPPED_STATUSES = {"KILLED", "FAILED", "CANCELLED"}

NAME_PREFIX = "spark"


class AzkabanError(RuntimeError):
    """Could not get a definitive status from Azkaban / Secrets Manager.

    Raised whenever we cannot confidently say whether a spark instance is
    orphaned. The finder treats this as fatal: it aborts without writing
    output, because a partial list would be unsafe input to the terminator.
    """


class AzkabanExecNotFound(AzkabanError):
    """Azkaban reported the execid as unknown. Non-fatal: skip the instance.

    Distinct from generic AzkabanError because a missing execid is a
    per-instance condition (the exec was purged, the tag is stale, etc.) —
    it doesn't invalidate our ability to audit the rest of the fleet.
    """


# Substrings (case-insensitive) in an Azkaban error body that mean
# "this execid doesn't exist" — anything else is treated as a hard failure.
EXEC_NOT_FOUND_HINTS = (
    "cannot find execution",
    "execution not found",
    "no execution",
    "no such execution",
    "doesn't exist",
)

# Live view of EC2 resources: source of PricePerHour (USD/hr) and launch time.
# NOTE: PricePerHour is only populated for on-demand instances; spot is blank.
LIVE_VIEW_URL = "http://live.internal.gridx.com/all"

# AWS billing convention: average hours in a month.
HOURS_PER_MONTH = 730


def tags_to_dict(tags):
    return {t["Key"]: t["Value"] for t in (tags or [])}


def list_spark_instances(session, region):
    """Running instances with Name=spark* and a JobID tag."""
    ec2 = session.client("ec2", region_name=region)
    paginator = ec2.get_paginator("describe_instances")
    filters = [
        {"Name": "tag:Name", "Values": [f"{NAME_PREFIX}*"]},
        {"Name": "tag-key", "Values": ["JobID"]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    instances = []
    for page in paginator.paginate(Filters=filters):
        for resv in page["Reservations"]:
            for inst in resv["Instances"]:
                instances.append(inst)
    return instances


def get_azkaban_config(session, region, env, cache):
    """Return (base_url, user, password) for an env from Secrets Manager, cached.

    Raises AzkabanError if the env is unknown or the secret can't be read —
    we cannot audit instances in that env, so callers must abort.
    """
    if env in cache:
        return cache[env]
    secret_id = AZKABAN_SECRETS.get(env)
    if not secret_id:
        raise AzkabanError(f"no Azkaban secret mapped for environment '{env}'")
    try:
        sm = session.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=secret_id)["SecretString"]
        d = json.loads(raw)
        cfg = ((d.get("azkaban_url") or "").rstrip("/"),
               d.get("azkaban_user"), d.get("azkaban_password"))
    except Exception as e:
        raise AzkabanError(
            f"could not load Azkaban creds for {env} ({secret_id}): {e}") from e
    if not all(cfg):
        raise AzkabanError(
            f"Azkaban secret '{secret_id}' missing url/user/password")
    cache[env] = cfg
    return cfg


def azkaban_login(base_url, user, password, verify):
    """Return a session.id for the Azkaban host. Raises AzkabanError on failure."""
    url = f"{base_url}/"
    try:
        resp = requests.post(
            url,
            data={"action": "login", "username": user, "password": password},
            headers={"X-Requested-With": "XMLHttpRequest"},
            verify=verify,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise AzkabanError(f"login to {base_url} failed: {e}") from e
    sid = data.get("session.id")
    if not sid:
        raise AzkabanError(
            f"login to {base_url} returned no session.id "
            f"(response: {data.get('error') or data})")
    return sid


def fetch_exec_flow(base_url, session_id, execid, verify):
    """Return the fetchexecflow JSON for an execid. Raises AzkabanError on failure.

    Azkaban can return HTTP 200 with `{"error": "..."}` (expired session,
    unknown execid, etc.) — those are treated as failures too, since we
    cannot determine the exec's status from such a response.
    """
    url = f"{base_url}/executor"
    params = {"ajax": "fetchexecflow", "session.id": session_id, "execid": execid}
    try:
        resp = requests.get(url, params=params, verify=verify, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise AzkabanError(
            f"fetchexecflow {base_url} execid={execid} failed: {e}") from e
    if "error" in data:
        err = str(data["error"])
        if any(h in err.lower() for h in EXEC_NOT_FOUND_HINTS):
            raise AzkabanExecNotFound(
                f"execid={execid} not known to {base_url}: {err}")
        raise AzkabanError(
            f"fetchexecflow {base_url} execid={execid}: {err}")
    return data


def ms_to_iso(ms):
    if not ms or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def fetch_spot_prices(session, region, sir_ids):
    """spot-instance-request-id -> price/hr (float) from the spot request.

    Uses ActualBlockHourlyPrice when present, else SpotPrice (the max-bid
    ceiling). Batched in chunks of 100.
    """
    out = {}
    ids = [i for i in sir_ids if i]
    if not ids:
        return out
    ec2 = session.client("ec2", region_name=region)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            resp = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=chunk)
        except Exception as e:
            print(f"  ! describe-spot-instance-requests failed: {e}", file=sys.stderr)
            continue
        for r in resp.get("SpotInstanceRequests", []):
            raw = r.get("ActualBlockHourlyPrice") or r.get("SpotPrice")
            try:
                out[r["SpotInstanceRequestId"]] = float(raw) if raw else None
            except (TypeError, ValueError):
                out[r["SpotInstanceRequestId"]] = None
    return out


def fetch_live_pricing():
    """instanceId -> {price_per_hour: float|None, launch_epoch: int|None, lifecycle}.

    Data comes from the Live View service. It is keyed by Cost Center at the top
    level, then by instance id. PricePerHour is blank for spot instances.
    """
    try:
        resp = requests.get(LIVE_VIEW_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ! live view fetch failed ({LIVE_VIEW_URL}): {e}", file=sys.stderr)
        return {}

    out = {}
    for group in data.values():
        if not isinstance(group, dict):
            continue
        for iid, fields in group.items():
            if not isinstance(fields, dict):
                continue
            price_raw = (fields.get("PricePerHour") or "").strip()
            try:
                price = float(price_raw) if price_raw else None
            except ValueError:
                price = None
            launch_raw = (fields.get("Duration") or "").strip()  # epoch seconds
            try:
                launch_epoch = int(float(launch_raw)) if launch_raw else None
            except ValueError:
                launch_epoch = None
            out[iid] = {
                "price_per_hour": price,
                "launch_epoch": launch_epoch,
                "lifecycle": fields.get("Lifecycle") or "on-demand",
            }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="uat", help="AWS profile (default: uat)")
    parser.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    parser.add_argument("--hours", type=float, default=1.0,
                        help="Stopped-longer-than threshold in hours (default: 1)")
    parser.add_argument("--verify-tls", action="store_true",
                        help="Verify Azkaban TLS certs (off by default: prod/stage "
                             "use self-signed certs)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Emit JSON instead of a table")
    args = parser.parse_args()

    verify = args.verify_tls
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    threshold_ms = args.hours * 3600 * 1000
    now_ms = time.time() * 1000

    session = boto3.Session(profile_name=args.profile)
    instances = list_spark_instances(session, args.region)
    print(f"Found {len(instances)} running spark* instances with a JobID tag.",
          file=sys.stderr)

    pricing = fetch_live_pricing()
    print(f"Loaded pricing for {len(pricing)} instances from live view.",
          file=sys.stderr)

    # Lazy per-env creds (Secrets Manager) + login + per-(env,execid) cache.
    azkaban_cfg = {}        # env -> (base_url, user, password)
    sessions = {}           # env -> session.id (or None if login failed)
    exec_flow_cache = {}    # (env, execid) -> fetchexecflow json

    def get_exec_flow(env, execid):
        key = (env, execid)
        if key in exec_flow_cache:
            return exec_flow_cache[key]
        base_url, user, password = get_azkaban_config(session, args.region, env, azkaban_cfg)
        if env not in sessions:
            sessions[env] = azkaban_login(base_url, user, password, verify)
        flow = fetch_exec_flow(base_url, sessions[env], execid, verify)
        exec_flow_cache[key] = flow
        return flow

    orphans = []
    for inst in instances:
        tags = tags_to_dict(inst.get("Tags"))
        env = (tags.get("Environment") or "").strip().lower()
        execid = tags.get("JobID")
        name = tags.get("Name")
        iid = inst["InstanceId"]

        if not env:
            sys.exit(
                f"ERROR: instance {iid} ({name}) has no Environment tag. "
                f"Cannot determine which Azkaban host to query. "
                f"Fix the tag or add the env to AZKABAN_SECRETS, then rerun.")
        if not execid:
            sys.exit(
                f"ERROR: instance {iid} ({name}) has no JobID tag. "
                f"Cannot audit. (Note: the EC2 filter requires a JobID tag, "
                f"so reaching this branch means tags changed mid-run — rerun.)")

        # The JobID tag is authoritative: each Azkaban execution launches its own
        # fleet tagged with its own exec id. Judge by THAT exec's status -- a
        # sibling execution of the same flow running does not save these instances.
        try:
            ef = get_exec_flow(env, execid)
        except AzkabanExecNotFound as e:
            # Per-instance: exec was purged or never existed in this Azkaban.
            # We can't confirm orphan, so skip without aborting the whole run.
            print(f"  ! skipping instance {iid} ({name}, env={env}): {e}",
                  file=sys.stderr)
            continue
        except AzkabanError as e:
            sys.exit(
                f"ERROR: cannot audit instance {iid} ({name}, env={env}, "
                f"jobid={execid}) — {e}\n"
                f"Aborting without writing output to avoid a partial/unsafe list. "
                f"Fix the issue (VPN? creds? Azkaban down?) and rerun.")
        status = ef.get("status")
        if status not in STOPPED_STATUSES:
            continue
        end_ms = ef.get("endTime") or 0
        if end_ms <= 0:
            end_ms = ef.get("updateTime") or 0
        if end_ms <= 0:
            # Terminal status with no timestamp is suspicious; surface it
            # rather than silently dropping the instance from consideration.
            print(f"  ! instance {iid} ({name}) exec={execid} status={status} "
                  f"has no endTime/updateTime — skipping (please investigate)",
                  file=sys.stderr)
            continue
        if (now_ms - end_ms) < threshold_ms:
            continue  # stopped too recently
        flow_id = ef.get("flowId")

        price_info = pricing.get(iid, {})
        price = price_info.get("price_per_hour")
        # Prefer live-view launch epoch; fall back to EC2 LaunchTime.
        launch_epoch = price_info.get("launch_epoch")
        if not launch_epoch and inst.get("LaunchTime"):
            launch_epoch = int(inst["LaunchTime"].timestamp())
        running_hours = round((now_ms / 1000 - launch_epoch) / 3600, 1) if launch_epoch else None
        monthly_cost = round(price * HOURS_PER_MONTH, 2) if price else None

        orphans.append({
            "InstanceId": iid,
            "Name": name,
            "Environment": env,
            "JobID": execid,
            "Flow": flow_id,
            "Lifecycle": inst.get("InstanceLifecycle") or "on-demand",
            "AzkabanStatus": status,
            "StoppedAt": ms_to_iso(end_ms),
            "StoppedHoursAgo": round((now_ms - end_ms) / 3600000, 1),
            "PricePerHour": price,
            "PriceSource": "live view" if price else None,
            "RunningHours": running_hours,
            "MonthlyCost": monthly_cost,
            "SpotRequestId": inst.get("SpotInstanceRequestId"),
            "Tags": tags,
        })

    # Live view doesn't price spot instances; fall back to the spot request.
    need = {o["SpotRequestId"] for o in orphans
            if not o["PricePerHour"] and o.get("SpotRequestId")}
    spot_prices = fetch_spot_prices(session, args.region, need)
    for o in orphans:
        if not o["PricePerHour"] and o.get("SpotRequestId"):
            p = spot_prices.get(o["SpotRequestId"])
            if p:
                o["PricePerHour"] = p
                o["MonthlyCost"] = round(p * HOURS_PER_MONTH, 2)
                o["PriceSource"] = "spot request"

    if args.as_json:
        print(json.dumps(orphans, indent=2))
        return

    if not orphans:
        print("\nNo orphaned instances found.")
        return

    print(f"\n{len(orphans)} orphaned instance(s) "
          f"(Azkaban {'/'.join(sorted(STOPPED_STATUSES))} > {args.hours}h ago, still running):\n")
    for o in orphans:
        if o["PricePerHour"]:
            price = f"${o['PricePerHour']:.4f}/hr ({o['PriceSource']})"
        else:
            price = "n/a"
        monthly = f"${o['MonthlyCost']:,.2f}/mo" if o["MonthlyCost"] else "n/a"
        age = f"{o['RunningHours']}h" if o["RunningHours"] is not None else "?"
        print(f"- {o['InstanceId']}  {o['Name']}")
        print(f"    env={o['Environment']}  jobid={o['JobID']}  lifecycle={o['Lifecycle']}  "
              f"status={o['AzkabanStatus']}  stopped={o['StoppedAt']} ({o['StoppedHoursAgo']}h ago)")
        print(f"    price={price}  running={age}  monthly_cost={monthly}")
        other = {k: v for k, v in o["Tags"].items()
                 if k not in ("Name", "Environment", "JobID")}
        if other:
            print("    tags: " + ", ".join(f"{k}={v}" for k, v in sorted(other.items())))

    od = [o for o in orphans if o["PriceSource"] == "live view" and o["PricePerHour"]]
    spot = [o for o in orphans if o["PriceSource"] == "spot request" and o["PricePerHour"]]
    unpriced = [o for o in orphans if not o["PricePerHour"]]

    od_monthly = sum(o["PricePerHour"] * HOURS_PER_MONTH for o in od)
    spot_hourly = sum(o["PricePerHour"] for o in spot)
    spot_daily = spot_hourly * 24
    # Accumulated to date = price/hr * hours alive since launch.
    spot_accrued = sum(o["PricePerHour"] * o["RunningHours"]
                       for o in spot if o["RunningHours"])

    print("\n" + "=" * 64)
    print("ORPHANED ON-DEMAND (price from live view)")
    print(f"  instances                       : {len(od)}")
    print(f"  MONTHLY SAVINGS if terminated   : ${od_monthly:,.2f}  (x{HOURS_PER_MONTH}h/mo)")
    print("")
    print("ORPHANED SPOT (price from spot-request max bid — upper bound)")
    print(f"  instances                       : {len(spot)}")
    print(f"  DAILY COST (going forward)      : ${spot_daily:,.2f}  (x24h)")
    print(f"  ACCUMULATED COST since launch   : ${spot_accrued:,.2f}")
    print("=" * 64)
    if unpriced:
        print(f"Note: {len(unpriced)} orphan(s) have no resolvable price and are excluded.")


if __name__ == "__main__":
    main()
