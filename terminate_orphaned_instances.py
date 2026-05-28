#!/usr/bin/env python3
"""
Terminate the EC2 instances listed in a CSV
(default: ./orphaned_spark_instances.csv — the sibling file next to this script,
typically produced by generate_outputs.py).

CSV format: instance_id,name,job_id  (header required)

Safety:
  - Dry-run by default. Pass --yes to actually terminate.
  - Re-describes every instance from AWS and only terminates ones that:
      * still exist
      * are in 'running' state
      * still carry the same Name tag as the CSV (guards against drift)
  - Flags spot instances: terminating a spot member of an active fleet/request
    triggers replacement. Use --cancel-spot-requests to cancel the underlying
    SpotInstanceRequestIds first.
  - Final interactive confirmation unless --force is passed.

Usage:
  python3 terminate_orphaned_instances.py                  # dry-run
  python3 terminate_orphaned_instances.py --yes            # do it (with prompt)
  python3 terminate_orphaned_instances.py --yes --force    # do it, no prompt
  python3 terminate_orphaned_instances.py --yes --cancel-spot-requests
"""

import argparse
import csv
import os
import sys
from collections import defaultdict

import boto3

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "orphaned_spark_instances.csv")


def tags_to_dict(tags):
    return {t["Key"]: t["Value"] for t in (tags or [])}


def read_csv(path):
    rows = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        required = {"instance_id", "name", "job_id"}
        missing = required - set(r.fieldnames or [])
        if missing:
            sys.exit(f"ERROR: CSV {path} missing columns: {sorted(missing)}")
        for row in r:
            iid = (row.get("instance_id") or "").strip()
            if iid:
                rows.append({"instance_id": iid,
                             "name": (row.get("name") or "").strip(),
                             "job_id": (row.get("job_id") or "").strip()})
    return rows


def describe_all(ec2, instance_ids):
    """instance_id -> instance dict (or None if not found)."""
    out = {iid: None for iid in instance_ids}
    if not instance_ids:
        return out
    ids = list(instance_ids)
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        paginator = ec2.get_paginator("describe_instances")
        try:
            for page in paginator.paginate(InstanceIds=chunk):
                for resv in page["Reservations"]:
                    for inst in resv["Instances"]:
                        out[inst["InstanceId"]] = inst
        except ec2.exceptions.ClientError as e:
            # AWS returns InvalidInstanceID.NotFound when one+ in the batch is gone.
            # Fall back to describing them one at a time so missing ones don't poison
            # the whole batch.
            if "InvalidInstanceID.NotFound" in str(e):
                for iid in chunk:
                    try:
                        r = ec2.describe_instances(InstanceIds=[iid])
                        for resv in r["Reservations"]:
                            for inst in resv["Instances"]:
                                out[inst["InstanceId"]] = inst
                    except ec2.exceptions.ClientError:
                        out[iid] = None
            else:
                raise
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default=DEFAULT_CSV,
                        help=f"CSV to process (default: {DEFAULT_CSV})")
    parser.add_argument("--profile", default="uat", help="AWS profile (default: uat)")
    parser.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    parser.add_argument("--yes", action="store_true",
                        help="Actually terminate. Without this it's a dry-run.")
    parser.add_argument("--force", action="store_true",
                        help="Skip the interactive confirmation prompt (use with --yes).")
    parser.add_argument("--cancel-spot-requests", action="store_true",
                        help="Also cancel SpotInstanceRequestIds for spot instances "
                             "before terminating (prevents fleet respawn).")
    parser.add_argument("--skip-name-check", action="store_true",
                        help="Don't require the live Name tag to match the CSV "
                             "(use carefully).")
    args = parser.parse_args()

    rows = read_csv(args.csv)
    if not rows:
        print(f"No rows in {args.csv}. Nothing to do.")
        return
    print(f"Read {len(rows)} row(s) from {args.csv}")

    session = boto3.Session(profile_name=args.profile)
    ec2 = session.client("ec2", region_name=args.region)
    print(f"AWS profile={args.profile}  region={args.region}")

    by_id = {r["instance_id"]: r for r in rows}
    live = describe_all(ec2, list(by_id.keys()))

    to_terminate = []        # list of instance dicts
    spot_to_cancel = set()   # SpotInstanceRequestIds
    skipped = defaultdict(list)  # reason -> [(iid, csv_name)]

    for iid, csv_row in by_id.items():
        inst = live.get(iid)
        if inst is None:
            skipped["not found (already gone?)"].append((iid, csv_row["name"]))
            continue
        state = inst.get("State", {}).get("Name")
        if state != "running":
            skipped[f"state={state} (not running)"].append((iid, csv_row["name"]))
            continue
        live_tags = tags_to_dict(inst.get("Tags"))
        live_name = live_tags.get("Name", "")
        if not args.skip_name_check and live_name != csv_row["name"]:
            skipped[f"name mismatch (live='{live_name}')"].append((iid, csv_row["name"]))
            continue
        # Defense-in-depth: also verify the live JobID tag matches the CSV.
        # The CSV is built from the finder's audit; if the tag has changed
        # since then, the orphan reasoning no longer applies to this instance.
        live_jobid = live_tags.get("JobID", "")
        if not args.skip_name_check and csv_row["job_id"] and live_jobid != csv_row["job_id"]:
            skipped[f"jobid mismatch (live='{live_jobid}' csv='{csv_row['job_id']}')"].append(
                (iid, csv_row["name"]))
            continue
        to_terminate.append(inst)
        if inst.get("InstanceLifecycle") == "spot" and inst.get("SpotInstanceRequestId"):
            spot_to_cancel.add(inst["SpotInstanceRequestId"])

    # ---- Plan output ----
    spot = [i for i in to_terminate if i.get("InstanceLifecycle") == "spot"]
    od = [i for i in to_terminate if i.get("InstanceLifecycle") != "spot"]
    print("\n" + "=" * 60)
    print(f"PLAN  ({'LIVE' if args.yes else 'DRY-RUN'})")
    print("=" * 60)
    print(f"  will terminate : {len(to_terminate)}  (on-demand={len(od)}, spot={len(spot)})")
    if spot and not args.cancel_spot_requests:
        print(f"  WARNING: {len(spot)} spot instance(s) belong to "
              f"{len(spot_to_cancel)} spot request(s). They will likely respawn "
              f"unless you also pass --cancel-spot-requests.")
    if args.cancel_spot_requests:
        print(f"  will cancel spot requests : {len(spot_to_cancel)}")
    if skipped:
        print(f"  skipped : {sum(len(v) for v in skipped.values())}")
        for reason, items in skipped.items():
            print(f"    - {reason}: {len(items)}")

    if not to_terminate and not spot_to_cancel:
        print("\nNothing to do.")
        return

    # ---- Listing (so the user can see exactly what's targeted) ----
    print("\nTargets:")
    for inst in to_terminate:
        name = tags_to_dict(inst.get("Tags")).get("Name", "?")
        life = inst.get("InstanceLifecycle") or "on-demand"
        print(f"  - {inst['InstanceId']}  {name}  ({life})")

    if not args.yes:
        print("\n(dry-run only — pass --yes to actually terminate)")
        return

    # ---- Confirmation ----
    if not args.force:
        try:
            ans = input(f"\nProceed to terminate {len(to_terminate)} instance(s)"
                        + (f" and cancel {len(spot_to_cancel)} spot request(s)"
                           if args.cancel_spot_requests and spot_to_cancel else "")
                        + " in '" + args.profile + "'? Type 'yes' to confirm: ").strip()
        except EOFError:
            ans = ""
        if ans.lower() != "yes":
            print("Aborted.")
            return

    # ---- Cancel spot requests first (so fleet doesn't immediately replace) ----
    if args.cancel_spot_requests and spot_to_cancel:
        ids = sorted(spot_to_cancel)
        print(f"\nCancelling {len(ids)} spot request(s)...")
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                resp = ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=chunk)
                for r in resp.get("CancelledSpotInstanceRequests", []):
                    print(f"  cancelled {r['SpotInstanceRequestId']} -> {r['State']}")
            except Exception as e:
                print(f"  ! cancel-spot batch failed: {e}", file=sys.stderr)

    # ---- Terminate in batches of 1000 ----
    ids = [i["InstanceId"] for i in to_terminate]
    print(f"\nTerminating {len(ids)} instance(s)...")
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        try:
            resp = ec2.terminate_instances(InstanceIds=chunk)
            for t in resp.get("TerminatingInstances", []):
                print(f"  {t['InstanceId']}: "
                      f"{t['PreviousState']['Name']} -> {t['CurrentState']['Name']}")
        except Exception as e:
            print(f"  ! terminate batch failed: {e}", file=sys.stderr)

    print("\nDone.")


if __name__ == "__main__":
    main()
