"""
Download AWS Cost Explorer data as CSVs for the M1 ingestion pipeline demo.

Not executed by me - review the filters/permissions below, then run it yourself:
    pip install boto3 python-dotenv
    python cost_explorer_export.py --report-type cost_by_service --start 2026-08-01 --end 2026-08-31

Required IAM permissions (read-only) - exactly the calls this script makes,
nothing broader:
    ce:GetCostAndUsage, ce:GetDimensionValues, sts:GetCallerIdentity

Scoped down for now to cost-and-usage only. The budget_vs_actual and
reserved_usage report types (AWS Budgets / RI utilization APIs) are commented
out below - fetch_budget_rows(), fetch_reservation_rows(), and their branches
in main() - rather than deleted, so they're a straight uncomment away once
we're ready to demo those scenarios too. Nothing in this file will call
budgets:DescribeBudgets or ce:GetReservationUtilization until that happens.

Credentials: copy .env.aws.example to .env next to this file and fill in
your access key / secret key (and session token, if using temporary STS
creds). Demo/.env.example already exists for the Docker Compose pipeline's
own config (WATCH_FOLDER, ENCRYPTION_KEY, ...) - that's a different file
for a different purpose, don't merge them.
load_dotenv() below reads that file into the process environment on startup,
and boto3 picks up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
/ AWS_DEFAULT_REGION from there automatically - nothing else in this file
touches credentials. .env is never read if it doesn't exist, so --profile
(an entry in your real ~/.aws/credentials) still works exactly as before -
note that boto3 checks environment variables before a named profile, so if
.env is present it wins over --profile rather than the other way round.

Two real AWS API constraints shape the schema below - both worth knowing before
you point this at a live account:

1. GetCostAndUsage's GroupBy accepts at most 2 entries per call (AWS-enforced).
   You can't group by LINKED_ACCOUNT + SERVICE + USAGE_TYPE + REGION in one
   request. Whichever of those you don't pass via --group-by is resolved
   instead from an explicit --filter-* value, or (for the account) from
   sts.get_caller_identity() - correct for a single-account sandbox, not for
   consolidated billing across multiple linked accounts.

2. GetCostAndUsage is an aggregated-cost API, not a line-item export. It has
   no concept of "the Tags column" the way a CUR (Cost and Usage Report) does.
   The closest it offers is grouping by ONE tag key, which returns that key's
   distinct values as group labels - so --tag-key populates Tags as a single
   "Key=Value", never the multi-tag "Owner=alice;Project=finops" strings you
   see in the hand-built test fixtures. If you need real multi-tag exports,
   that means CUR-to-S3 or get_cost_and_usage_with_resources (resource-level
   data has to be opted into in the Cost Explorer console first), both out of
   scope here.

Active report types (GetCostAndUsage, M1 row schema):
    cost_by_service, cost_by_region, daily_spend, custom (your own
    --group-by / --filter-* / --granularity)

Commented out for now - see note above: budget_vs_actual, reserved_usage.

Use --dry-run to see exactly what would be sent to AWS - the printed kwargs
for whichever ce.get_cost_and_usage() call your flags build - without making
any network call at all. Good habit: run with --dry-run first, read the
printed request, then drop --dry-run once it looks right.
"""
import argparse
import csv
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

FOLDER = Path(__file__).parent
DEFAULT_OUTPUT_DIR = FOLDER / "incoming"

load_dotenv(FOLDER / ".env")  # no-op if .env doesn't exist

ALLOWED_GROUP_BY = ["LINKED_ACCOUNT", "SERVICE", "USAGE_TYPE", "REGION"]

M1_FIELDS = [
    "LinkedAccountId", "Service", "UsageType", "Region",
    "UsageStartDate", "BlendedCost", "UsageQuantity", "Tags",
]

# M1's schema (models.py CostLineItem) requires `service` and `usage_type`
# non-empty on every row - Region is optional. Since GetCostAndUsage caps
# GroupBy at 2 dimensions, every preset below groups by exactly those two
# required fields; there's no dimension slot left over for grouping by
# REGION too. "cost_by_region" instead means "service+usage_type, filtered
# to one region" - pair it with --filter-region, or every row's Region
# column comes back blank (harmless - M1 allows that - but defeats the
# preset's own name).
REPORT_PRESETS = {
    # report_type: (default group_by, default granularity)
    "cost_by_service": (["SERVICE", "USAGE_TYPE"], "MONTHLY"),
    "cost_by_region": (["SERVICE", "USAGE_TYPE"], "MONTHLY"),
    "daily_spend": (["SERVICE", "USAGE_TYPE"], "DAILY"),
    "custom": ([], "DAILY"),
}

# Fields M1's CostLineItem requires non-empty. Anything not satisfied by
# GroupBy or an explicit --filter-* is silently blank in every output row -
# and M1 will reject every single one. See check_m1_compatibility() below.
M1_REQUIRED_FIELDS = ["SERVICE", "USAGE_TYPE"]


def build_session(profile: Optional[str]) -> boto3.Session:
    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def call_with_backoff(fn, *args, **kwargs):
    """CE's GetCostAndUsage/GetReservationUtilization throttle aggressively
    under concurrent use - a short exponential backoff on ThrottlingException
    is cheap insurance for a script that may be run alongside the console."""
    delay = 1.0
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ThrottlingException", "TooManyRequestsException") and attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def build_filter(
    account: Optional[str],
    region: Optional[str],
    service: Optional[str],
    usage_type: Optional[str],
    tag_key: Optional[str],
    tag_value: Optional[str],
) -> Optional[dict]:
    """Combine whichever --filter-* values were given into a CE Filter
    expression. A single clause is passed as-is (not wrapped in "And") -
    some CE validation is stricter about a 1-element And than about no
    wrapper at all."""
    clauses = []
    dim_filters = [
        ("LINKED_ACCOUNT", account),
        ("REGION", region),
        ("SERVICE", service),
        ("USAGE_TYPE", usage_type),
    ]
    for key, value in dim_filters:
        if value:
            clauses.append({"Dimensions": {"Key": key, "Values": [value]}})
    if tag_key and tag_value:
        clauses.append({"Tags": {"Key": tag_key, "Values": [tag_value]}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"And": clauses}


def paginate_cost_and_usage(ce, **kwargs) -> tuple:
    """Collect every ResultsByTime entry across NextPageToken pages.

    Returns (periods, call_count) - call_count is the real number of
    ce.get_cost_and_usage requests this took, so callers can report actual
    AWS usage rather than an assumption. Almost always 1 for a bounded date
    range grouped by <=2 dimensions; AWS decides when to hand back a
    NextPageToken, this function only follows it when given one."""
    periods = []
    call_count = 0
    token = None
    while True:
        if token:
            kwargs["NextPageToken"] = token
        resp = call_with_backoff(ce.get_cost_and_usage, **kwargs)
        call_count += 1
        periods.extend(resp["ResultsByTime"])
        token = resp.get("NextPageToken")
        if not token:
            return periods, call_count


def resolve_account_id(sts, explicit_account: Optional[str]) -> str:
    if explicit_account:
        return explicit_account
    return sts.get_caller_identity()["Account"]


def fetch_cost_rows(
    ce,
    sts,
    start: str,
    end: str,
    granularity: str,
    group_by: list,
    account: Optional[str],
    region: Optional[str],
    service: Optional[str],
    usage_type: Optional[str],
    tag_key: Optional[str],
    tag_value: Optional[str],
    dry_run: bool = False,
) -> list:
    if len(group_by) > 2:
        raise ValueError(f"GetCostAndUsage allows at most 2 GroupBy dimensions, got {group_by}")

    group_by_spec = [{"Type": "DIMENSION", "Key": k} for k in group_by]
    if tag_key:
        if len(group_by_spec) >= 2:
            raise ValueError("--tag-key adds a GroupBy entry too - drop one --group-by dimension to make room")
        group_by_spec.append({"Type": "TAG", "Key": tag_key})

    kwargs = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": granularity,
        "Metrics": ["BlendedCost", "UsageQuantity"],
    }
    if group_by_spec:
        kwargs["GroupBy"] = group_by_spec
    filt = build_filter(account, region, service, usage_type, tag_key, tag_value)
    if filt:
        kwargs["Filter"] = filt

    if dry_run:
        sts_calls = 0 if account else 1
        if account:
            print(f"[dry-run] account resolved from --filter-account: {account} (0 calls)")
        else:
            print("[dry-run] would call sts.get_caller_identity() (1 call)")
        print("[dry-run] would call ce.get_cost_and_usage() with: (1 call)")
        print(f"    {kwargs}")
        print("[dry-run] would only call ce.get_cost_and_usage() again if AWS returns a "
              "NextPageToken (large result sets) - not something this script decides on its own.")
        print(f"[dry-run] AWS calls for this run: {sts_calls} (sts) + 1 (ce.get_cost_and_usage) "
              f"= {sts_calls + 1} total, assuming a single page. No network calls actually made.")
        return []

    sts_calls = 0 if account else 1
    resolved_account = resolve_account_id(sts, account)

    periods, ce_calls = paginate_cost_and_usage(ce, **kwargs)

    rows = []
    for period in periods:
        usage_start = period["TimePeriod"]["Start"]

        entries = period["Groups"] if period.get("Groups") else [{"Keys": [], "Metrics": period["Total"]}]
        for entry in entries:
            keys = entry.get("Keys", [])
            metrics = entry["Metrics"]

            row = {
                "LinkedAccountId": resolved_account,
                "Service": service or "",
                "UsageType": usage_type or "",
                "Region": region or "",
                "UsageStartDate": usage_start,
                "BlendedCost": metrics["BlendedCost"]["Amount"],
                "UsageQuantity": metrics["UsageQuantity"]["Amount"],
                "Tags": "",
            }

            # Overlay whichever dimensions were actually grouped-by, in order.
            key_index = 0
            for dim in group_by:
                value = keys[key_index]
                key_index += 1
                if dim == "LINKED_ACCOUNT":
                    row["LinkedAccountId"] = value
                elif dim == "SERVICE":
                    row["Service"] = value
                elif dim == "USAGE_TYPE":
                    row["UsageType"] = value
                elif dim == "REGION":
                    row["Region"] = value
            if tag_key:
                # CE returns tag groups as "<TagKey>$<TagValue>" (or "$" suffix
                # for untagged resources) - reshape to the pipeline's Key=Value form.
                raw = keys[key_index]
                _, _, tag_val = raw.partition("$")
                row["Tags"] = f"{tag_key}={tag_val}" if tag_val else ""

            rows.append(row)

    print(f"[aws] calls made: {sts_calls} (sts.get_caller_identity) + {ce_calls} "
          f"(ce.get_cost_and_usage) = {sts_calls + ce_calls} total")
    return rows


# --- Disabled for now: budget_vs_actual scenario (AWS Budgets API) ---------
# Scoped out per current focus: cost-and-usage only. Uncomment this function
# and its branch in main() (also disabled below) when ready to demo it.
# Calls budgets:DescribeBudgets - add that back to the IAM policy first.
#
# def fetch_budget_rows(budgets_client, account_id: str, dry_run: bool = False) -> list:
#     """AWS Budgets, not Cost Explorer - budgeted vs. actual isn't a cost-usage
#     concept, it's a separate service with its own API."""
#     if dry_run:
#         print(f"[dry-run] would call budgets_client.describe_budgets(AccountId={account_id!r}, MaxResults=100) (1 call)")
#         print("[dry-run] would only repeat if AWS returns a NextToken (>100 budgets).")
#         print("[dry-run] AWS calls for this run: 1 total, assuming <=100 budgets. No network calls actually made.")
#         return []
#
#     rows = []
#     paginator_token = None
#     while True:
#         kwargs = {"AccountId": account_id, "MaxResults": 100}
#         if paginator_token:
#             kwargs["NextToken"] = paginator_token
#         resp = call_with_backoff(budgets_client.describe_budgets, **kwargs)
#         for b in resp.get("Budgets", []):
#             spend = b.get("CalculatedSpend", {})
#             actual = spend.get("ActualSpend", {})
#             forecasted = spend.get("ForecastedSpend", {})
#             limit = b.get("BudgetLimit", {})
#             rows.append({
#                 "BudgetName": b.get("BudgetName"),
#                 "BudgetType": b.get("BudgetType"),
#                 "TimeUnit": b.get("TimeUnit"),
#                 "BudgetLimitAmount": limit.get("Amount"),
#                 "BudgetLimitUnit": limit.get("Unit"),
#                 "ActualSpendAmount": actual.get("Amount"),
#                 "ActualSpendUnit": actual.get("Unit"),
#                 "ForecastedSpendAmount": forecasted.get("Amount"),
#                 "ForecastedSpendUnit": forecasted.get("Unit"),
#             })
#         paginator_token = resp.get("NextToken")
#         if not paginator_token:
#             break
#     return rows
# --- end disabled: budget_vs_actual -----------------------------------------


# --- Disabled for now: reserved_usage scenario (RI utilization API) --------
# Scoped out per current focus: cost-and-usage only. Uncomment this function
# and its branch in main() (also disabled below) when ready to demo it.
# Calls ce:GetReservationUtilization - add that back to the IAM policy first.
#
# def fetch_reservation_rows(ce, start: str, end: str, granularity: str, dry_run: bool = False) -> list:
#     """Reserved Instance utilization - GetReservationUtilization, a distinct
#     CE endpoint from GetCostAndUsage with its own response shape."""
#     if dry_run:
#         print("[dry-run] would call ce.get_reservation_utilization() with: (1 call)")
#         print(f"    {{'TimePeriod': {{'Start': {start!r}, 'End': {end!r}}}, 'Granularity': {granularity!r}}}")
#         print("[dry-run] would only repeat if AWS returns a NextPageToken (large result sets).")
#         print("[dry-run] AWS calls for this run: 1 total, assuming a single page. No network calls actually made.")
#         return []
#
#     rows = []
#     token = None
#     while True:
#         kwargs = {
#             "TimePeriod": {"Start": start, "End": end},
#             "Granularity": granularity,
#         }
#         if token:
#             kwargs["NextPageToken"] = token
#         resp = call_with_backoff(ce.get_reservation_utilization, **kwargs)
#         for period in resp["UtilizationsByTime"]:
#             total = period["Total"]
#             rows.append({
#                 "PeriodStart": period["TimePeriod"]["Start"],
#                 "PeriodEnd": period["TimePeriod"]["End"],
#                 "UtilizationPercentage": total.get("UtilizationPercentage"),
#                 "PurchasedHours": total.get("PurchasedHours"),
#                 "UsedHours": total.get("UsedHours"),
#                 "UnusedHours": total.get("UnusedHours"),
#                 "NetRISavings": total.get("NetRISavings"),
#                 "OnDemandCostOfRIHoursUsed": total.get("OnDemandCostOfRIHoursUsed"),
#             })
#         token = resp.get("NextPageToken")
#         if not token:
#             break
#     return rows
# --- end disabled: reserved_usage -------------------------------------------


def list_dimension_values(ce, dimension: str, start: str, end: str, dry_run: bool = False) -> None:
    """Helper: print the exact values CE expects for a dimension filter, e.g.
    'Amazon Elastic Compute Cloud - Compute', not the friendly 'AmazonEC2'
    names used in the hand-built test fixtures."""
    if dry_run:
        print(f"[dry-run] would call ce.get_dimension_values(TimePeriod={{'Start': {start!r}, 'End': {end!r}}}, "
              f"Dimension={dimension!r}) (1 call)")
        print("[dry-run] AWS calls for this run: 1 total. No network call actually made.")
        return
    resp = call_with_backoff(
        ce.get_dimension_values,
        TimePeriod={"Start": start, "End": end},
        Dimension=dimension,
    )
    for v in resp["DimensionValues"]:
        print(v["Value"])
    print("[aws] calls made: 1 (ce.get_dimension_values)")


def check_m1_compatibility(
    group_by: list,
    service: "str | None",
    usage_type: "str | None",
) -> list:
    """Pure local check, no AWS/network involved: for each of M1's required
    fields, would every output row actually have a non-empty value? A field
    is populated either by being in --group-by (varies per row) or by an
    explicit --filter-* (constant per row) - anything else comes back "" on
    every row, and M1 rejects every row missing a required field. Returns a
    list of warning strings (empty list = compatible)."""
    resolved = {"SERVICE": bool(service), "USAGE_TYPE": bool(usage_type)}
    for dim in group_by:
        if dim in resolved:
            resolved[dim] = True

    warnings = []
    for field in M1_REQUIRED_FIELDS:
        if not resolved.get(field, False):
            warnings.append(
                f"'{field}' will be blank on every row - not in --group-by and no matching "
                f"--filter-* given. M1 requires this field non-empty, so every row this run "
                f"produces will be rejected. Fix: add '{field}' to --group-by (only if you have "
                f"a free GroupBy slot - max 2 total), or pass --filter-{field.lower().replace('_', '-')} <value>."
            )
    return warnings


def write_csv(rows: list, fieldnames: list, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def default_date_range() -> tuple:
    today = date.today()
    start_of_month = today.replace(day=1)
    return start_of_month.isoformat(), today.isoformat()


def parse_args():
    default_start, default_end = default_date_range()

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # "budget_vs_actual" and "reserved_usage" removed from choices while
    # those scenarios are commented out above - add them back to this list
    # (and uncomment the matching branch in main()) together.
    p.add_argument("--report-type", choices=list(REPORT_PRESETS), default="cost_by_service")
    p.add_argument("--start", default=default_start, help="YYYY-MM-DD, inclusive (default: 1st of this month)")
    p.add_argument("--end", default=default_end, help="YYYY-MM-DD, exclusive per AWS convention (default: today)")
    p.add_argument("--granularity", choices=["DAILY", "MONTHLY"], default=None,
                   help="Overrides the report-type's default granularity")
    p.add_argument("--group-by", nargs="*", choices=ALLOWED_GROUP_BY, default=None,
                   help=f"Up to 2 of {ALLOWED_GROUP_BY}. Overrides the report-type default.")
    p.add_argument("--filter-account", help="LinkedAccountId to filter/populate (default: your caller identity)")
    p.add_argument("--filter-region", help="e.g. us-east-1")
    p.add_argument("--filter-service", help="Exact CE service name, e.g. 'Amazon Elastic Compute Cloud - Compute'")
    p.add_argument("--filter-usage-type", help="e.g. BoxUsage:t3.medium")
    p.add_argument("--tag-key", help="Group by this tag key; populates the Tags column as 'key=value'")
    p.add_argument("--tag-value", help="Filter to this tag value (requires --tag-key)")
    p.add_argument("--profile", help="AWS named profile (default: standard credential chain)")
    p.add_argument("--output", help="Output CSV path (default: incoming/<report_type>_<timestamp>.csv)")
    p.add_argument("--list-dimension-values", metavar="DIMENSION",
                   help="Instead of exporting, print CE's valid values for this dimension "
                        "(e.g. SERVICE, REGION) and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Print exactly what would be sent to AWS and exit - makes zero network calls")
    return p.parse_args()


def main():
    args = parse_args()
    session = build_session(args.profile)
    ce = session.client("ce")  # client() builds a local object only - no network call yet

    if args.list_dimension_values:
        list_dimension_values(ce, args.list_dimension_values, args.start, args.end, dry_run=args.dry_run)
        return

    sts = session.client("sts")

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    output_path = Path(args.output) if args.output else DEFAULT_OUTPUT_DIR / f"{args.report_type}_{timestamp}.csv"

    # --- Disabled for now: budget_vs_actual / reserved_usage branches ------
    # if args.report_type == "budget_vs_actual":
    #     budgets_client = session.client("budgets")
    #     if args.dry_run:
    #         account_id = args.filter_account or "<resolved via sts.get_caller_identity() - skipped in dry-run>"
    #     else:
    #         account_id = resolve_account_id(sts, args.filter_account)
    #     rows = fetch_budget_rows(budgets_client, account_id, dry_run=args.dry_run)
    #     fieldnames = ["BudgetName", "BudgetType", "TimeUnit", "BudgetLimitAmount", "BudgetLimitUnit",
    #                   "ActualSpendAmount", "ActualSpendUnit", "ForecastedSpendAmount", "ForecastedSpendUnit"]
    #
    # elif args.report_type == "reserved_usage":
    #     granularity = args.granularity or "MONTHLY"
    #     rows = fetch_reservation_rows(ce, args.start, args.end, granularity, dry_run=args.dry_run)
    #     fieldnames = ["PeriodStart", "PeriodEnd", "UtilizationPercentage", "PurchasedHours",
    #                   "UsedHours", "UnusedHours", "NetRISavings", "OnDemandCostOfRIHoursUsed"]
    # --- end disabled --------------------------------------------------------

    # Only path active right now: cost-and-usage (cost_by_service /
    # cost_by_region / daily_spend / custom). Re-introduce the `if/elif` above
    # this block once budget_vs_actual / reserved_usage come back.
    preset_group_by, preset_granularity = REPORT_PRESETS[args.report_type]
    group_by = args.group_by if args.group_by is not None else preset_group_by
    granularity = args.granularity or preset_granularity

    m1_warnings = check_m1_compatibility(group_by, args.filter_service, args.filter_usage_type)
    if m1_warnings:
        print("=" * 70)
        print("M1 COMPATIBILITY WARNING - this run will produce rows M1 rejects:")
        for w in m1_warnings:
            print(f"  - {w}")
        print("=" * 70)

    rows = fetch_cost_rows(
        ce, sts,
        start=args.start, end=args.end, granularity=granularity, group_by=group_by,
        account=args.filter_account, region=args.filter_region,
        service=args.filter_service, usage_type=args.filter_usage_type,
        tag_key=args.tag_key, tag_value=args.tag_value,
        dry_run=args.dry_run,
    )
    fieldnames = M1_FIELDS

    if args.dry_run:
        return  # nothing was fetched - nothing to write

    write_csv(rows, fieldnames, output_path)
    print(f"Wrote {len(rows)} row(s) to {output_path}")


if __name__ == "__main__":
    main()
