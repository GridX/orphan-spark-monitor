#!/usr/bin/env python3
"""
Run the orphan finder and produce two consumable artifacts:

  1. orphaned_spark_instances.csv     (instance_id, name, job_id) — input to
     terminate_orphaned_instances.py
  2. orphaned_slack_message.txt       Slack-formatted summary to post in
     #engineering-all (or wherever) for owner confirmation before terminating.

The Slack message groups instances by (Name, lifecycle) so engineers can spot
their own jobs, and includes the Azkaban flow name + tagged exec id so the
killed exec is unambiguous (each Azkaban execution has its own fleet; the
JobID tag is per-execution, not per-flow — see README.md).

By default invokes ./orphaned_spark_instances.py --json to get fresh data.
Use --from-json PATH to skip the live run.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FINDER = os.path.join(HERE, "orphaned_spark_instances.py")
DEFAULT_CSV = os.path.join(HERE, "orphaned_spark_instances.csv")
DEFAULT_SLACK = os.path.join(HERE, "orphaned_slack_message.txt")


def run_finder(profile, region, hours):
    cmd = [sys.executable, FINDER, "--json",
           "--profile", profile, "--region", region, "--hours", str(hours)]
    print(f"Running: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(f"finder exited with {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"could not parse finder JSON output: {e}\n--- stdout head ---\n"
                 f"{proc.stdout[:500]}")


def write_csv(orphans, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["instance_id", "name", "job_id"])
        for x in orphans:
            w.writerow([x["InstanceId"], x["Name"], x["JobID"]])
    print(f"wrote {len(orphans)} rows -> {path}", file=sys.stderr)


def fmt_age(hours):
    return f"{hours/24:.1f}d" if hours >= 24 else f"{hours:.0f}h"


def build_slack(orphans, hours_threshold):
    """Group by (Name, lifecycle) and emit a Slack-formatted message."""
    g = defaultdict(lambda: {"ids": [], "jobs": set(), "flow": set(),
                              "status": set(), "env": set(), "stopped": []})
    for x in orphans:
        life = "spot" if x["Lifecycle"] == "spot" else "on-demand"
        k = (x["Name"], life)
        g[k]["ids"].append(x["InstanceId"])
        g[k]["jobs"].add(x["JobID"])
        g[k]["flow"].add(x.get("Flow") or "?")
        g[k]["status"].add(x["AzkabanStatus"])
        g[k]["env"].add(x["Environment"])
        g[k]["stopped"].append(x["StoppedHoursAgo"])

    od = sorted([(k, v) for k, v in g.items() if k[1] == "on-demand"],
                key=lambda kv: -max(kv[1]["stopped"]))
    spot = sorted([(k, v) for k, v in g.items() if k[1] == "spot"],
                  key=lambda kv: -max(kv[1]["stopped"]))

    def fmt_row(name, v):
        jobs = ",".join(sorted(v["jobs"]))
        if len(v["jobs"]) > 4:
            jobs = f"{len(v['jobs'])} jobs"
        return (f"• `{name}` — {len(v['ids'])} inst · "
                f"{','.join(sorted(v['env']))} · "
                f"flow `{','.join(sorted(v['flow']))}` · "
                f"job {jobs} · {'/'.join(sorted(v['status']))} "
                f"{fmt_age(max(v['stopped']))} ago")

    lines = [
        f":rotating_light: *Orphaned Spark EC2 instances* — their Azkaban "
        f"execution (the instance's `JobID` tag) is KILLED/FAILED/CANCELLED "
        f">{hours_threshold}h ago, but the instances are still running. "
        f"Planning to clean up — *please reply if any are still needed.*",
        "",
        f"*On-demand — {sum(len(v['ids']) for _, v in od)} instances across "
        f"{len(od)} names (will terminate):*",
    ]
    lines += [fmt_row(k[0], v) for k, v in od]
    lines += [
        "",
        f"*Spot — {sum(len(v['ids']) for _, v in spot)} instances across "
        f"{len(spot)} names (cancel fleet/spot request; they respawn if only "
        f"terminated):*",
    ]
    lines += [fmt_row(k[0], v) for k, v in spot]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-json", help="Use this JSON file instead of running the finder")
    p.add_argument("--profile", default="uat", help="AWS profile (default: uat)")
    p.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    p.add_argument("--hours", type=float, default=24.0,
                   help="Stopped-longer-than threshold in hours (default: 24)")
    p.add_argument("--csv", default=DEFAULT_CSV,
                   help=f"CSV output path (default: {DEFAULT_CSV})")
    p.add_argument("--slack", default=DEFAULT_SLACK,
                   help=f"Slack message output path (default: {DEFAULT_SLACK})")
    args = p.parse_args()

    if args.from_json:
        with open(args.from_json) as f:
            orphans = json.load(f)
        print(f"loaded {len(orphans)} orphans from {args.from_json}", file=sys.stderr)
    else:
        orphans = run_finder(args.profile, args.region, args.hours)
        print(f"finder returned {len(orphans)} orphans", file=sys.stderr)

    write_csv(orphans, args.csv)
    msg = build_slack(orphans, args.hours)
    with open(args.slack, "w") as f:
        f.write(msg + "\n")
    print(f"wrote Slack message -> {args.slack}", file=sys.stderr)

    # Echo a tiny summary on stdout so callers can pipe.
    od = sum(1 for x in orphans if x.get("Lifecycle") != "spot")
    sp = sum(1 for x in orphans if x.get("Lifecycle") == "spot")
    print(f"orphans={len(orphans)}  on-demand={od}  spot={sp}")


if __name__ == "__main__":
    main()
