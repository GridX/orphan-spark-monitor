# orphan-spark

Tooling to find and clean up **orphaned Spark EC2 instances** at GridX: EC2
instances launched for an Azkaban job that has since been KILLED / FAILED /
CANCELLED, but the instances are still running (and still costing money).

This folder is the canonical home for the audit. Nothing here depends on any
other repo.

---

## The problem in one paragraph

Each Azkaban execution of a Spark flow launches its **own** fleet of EC2
instances. Every instance is tagged with `JobID = <Azkaban execution id>` at
launch — that tag is **authoritative and per-execution**, not per-flow. When
the execution ends (success or otherwise), the fleet is supposed to be torn
down. Sometimes it isn't, and the instances keep running for hours or weeks.
A single flow can therefore have multiple concurrent executions, each with
its own fleet — a sibling execution being live does **not** save the
instances of a stopped one. Detection must judge each instance by its **own
tagged exec id**, never by flow-level state.

(This was learned the hard way: an earlier version of the finder gated on
"is the flow currently running?" and wrongly spared 280 genuinely-orphaned
instances of a KILLED exec, just because a sibling exec was alive.)

---

## Files

| File | What it does |
|---|---|
| `orphaned_spark_instances.py` | **Finder.** Lists running EC2 instances with `Name=spark*` and a `JobID` tag, looks each up in Azkaban (per the instance's `Environment` tag), and flags those whose tagged exec is in a stopped state >1h ago. Adds price per hour (on-demand from live view, spot from the spot request) and computes savings/cost metrics. JSON via `--json`. |
| `generate_outputs.py` | **Reporter.** Runs the finder, then writes two artifacts: a `instance_id,name,job_id` CSV (input to the terminator) and a Slack-formatted message grouped by instance Name + lifecycle (for #engineering-all confirmation). |
| `terminate_orphaned_instances.py` | **Cleanup.** Reads the CSV, re-verifies each row from AWS (still exists, still running, Name tag still matches), then terminates. Dry-run by default. Spot-aware: warns about fleet respawn and can cancel `SpotInstanceRequestId`s with `--cancel-spot-requests`. |
| `orphaned_spark_instances.csv` | (Generated) The kill list. |
| `orphaned_slack_message.txt` | (Generated) Slack post body. |

---

## Standard workflow

```bash
cd ~/work/orphan-spark

# 1. Find + report
python3 generate_outputs.py
#   -> updates orphaned_spark_instances.csv and orphaned_slack_message.txt
#   -> echoes a one-line summary

# 2. Paste the Slack message and wait for owner confirmation
cat orphaned_slack_message.txt   # copy into Slack

# 3. After confirmation: dry-run, then terminate
python3 terminate_orphaned_instances.py            # dry-run, lists targets
python3 terminate_orphaned_instances.py --yes      # prompts then terminates
# For spot fleets (cancel underlying request so they don't respawn):
python3 terminate_orphaned_instances.py --yes --cancel-spot-requests
```

---

## External dependencies

All AWS calls go through the `uat` profile (account 743512984079, region
`us-west-2`). The single AWS account hosts instances tagged `prod` / `stage`
/ `uat`; the **`Environment` tag** on each instance, not the AWS account,
determines which Azkaban host to query.

- **EC2 inventory + on-demand price/hr**: `http://live.internal.gridx.com/all`
  (internal HTTP service, JSON keyed by Cost Center → instance id). The
  `PricePerHour` field is **blank for spot** — fall back to the spot request.
  The `Duration` field is the launch epoch (seconds), not an elapsed time.

- **Spot price**: read the instance's `SpotInstanceRequestId` (an instance
  field, not a tag) and call `describe-spot-instance-requests` → `SpotPrice`.
  Note this is the **max bid**, often set ≈ on-demand price; the actual
  market price is typically lower, so spot $ figures are an upper bound.

- **Azkaban**: three hosts, one per logical env:

  | Environment tag | Azkaban host | TLS |
  |---|---|---|
  | `prod`  | `azkaban.internal.gridx.com`       | self-signed |
  | `stage` | `azkaban-stage.internal.gridx.com` | self-signed |
  | `uat`   | `azkaban.uat.gridx.com`            | valid CA    |

  Credentials and base URL are stored in **AWS Secrets Manager** (read via the
  same `uat` profile), under JSON keys `azkaban_user`, `azkaban_password`,
  `azkaban_url`:

  | Environment tag | Secret id |
  |---|---|
  | `prod`  | `prod/utility-operation/uo_service_db` |
  | `stage` | `utility-operation/uo_service_db_stage` |
  | `uat`   | `uat/utility-operation/uo_service_db` |

  Because prod/stage use self-signed certs, the finder defaults to **TLS
  verification off** (pass `--verify-tls` to enforce). Login is the standard
  Azkaban flow: `POST <base_url>/` with `action=login,username,password`
  returning `session.id`; then `GET <base_url>/executor?ajax=fetchexecflow&execid=<n>`
  to read an execution's status / endTime.

---

## Detection rules (precise)

An instance is reported as orphaned iff **all** of the following hold:

1. It is in `running` state.
2. Its `Name` tag starts with `spark` and it has a `JobID` tag.
3. The Azkaban execution identified by `JobID` (looked up in the host
   selected by the `Environment` tag) has status in
   `{KILLED, FAILED, CANCELLED}`.
4. That execution's `endTime` (or `updateTime` if `endTime <= 0`) is more
   than the threshold (default 1h, `--hours` to override) in the past.

The finder also annotates each orphan with `Flow` (Azkaban flow id derived
from the execution) and price/running-time fields used by the cost summary.

---

## What NOT to do

- **Do not gate on flow-level state.** The Azkaban `getRunning` endpoint
  reports running execs of a flow. Using it to skip orphans of *other*
  execs of the same flow is wrong — see the "problem in one paragraph"
  section above. Per-execid status is the only correct signal.
- **Do not assume "spot instance still running" means the fleet leaked
  forever.** Spot fleets churn (instances replaced every few hours).
  Terminate the instances *and* cancel the spot request, or they'll
  respawn within minutes (use `--cancel-spot-requests`).
- **Do not write secrets to the repo.** Creds come from Secrets Manager at
  runtime; never bake them into config or env files committed here.

---

## Tunables worth knowing

- `generate_outputs.py --hours N` → propagates to the finder; raise this
  (e.g. 24) to surface only *clearly stale* fleets and avoid false alarms
  on freshly-killed flows that may get a new run.
- `terminate_orphaned_instances.py --skip-name-check` → bypasses the
  Name-tag drift check. Don't use this unless you know why you're
  bypassing it.
- All three scripts accept `--profile` / `--region` if the deployment ever
  moves out of `uat` / `us-west-2`.
