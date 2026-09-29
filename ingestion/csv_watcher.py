"""
Week 2 demo: watch `incoming/` for new Cost Explorer CSV exports and process
each one as it lands, based on report type.

Drop a file into `incoming/` (e.g. save-as from a Cost Explorer CSV download,
or copy one in) and this will pick it up, classify it, summarize it, and move
it to `processed/`.

Run: python csv_watcher.py
Stop: Ctrl+C
"""
import re
import shutil
import time
from pathlib import Path

import polars as pl
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

FOLDER = Path(__file__).parent
INCOMING = FOLDER / "incoming"
PROCESSED = FOLDER / "processed"

# How long a file's size must stay unchanged before we treat it as fully written.
STABLE_SECONDS = 1.0
STABLE_POLL_INTERVAL = 0.25
STABLE_TIMEOUT = 30.0

REPORT_TYPES = ["cost_by_service", "cost_by_region", "daily_spend", "budget_vs_actual", "reserved_usage"]


def classify(path: Path) -> str:
    name = path.stem.lower()
    if "daily" in name:
        return "daily_spend"
    if "region" in name:
        return "cost_by_region"
    if "budget" in name:
        return "budget_vs_actual"
    if "reserved" in name or re.search(r"\bri\b", name):
        return "reserved_usage"
    if "service" in name or "cost" in name:
        return "cost_by_service"

    # Filename gave no hint - sniff the header row instead.
    try:
        with open(path, encoding="utf-8-sig") as f:
            header = f.readline().lower()
    except OSError:
        return "unknown"
    if "region" in header:
        return "cost_by_region"
    if "service" in header:
        return "cost_by_service"
    return "unknown"


def wait_until_stable(path: Path) -> bool:
    """Block until the file's size stops changing (download/copy finished)."""
    deadline = time.monotonic() + STABLE_TIMEOUT
    last_size = -1
    stable_since = None
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STABLE_SECONDS:
                return True
        else:
            stable_since = None
            last_size = size
        time.sleep(STABLE_POLL_INTERVAL)
    return False


def summarize_pivot(path: Path) -> None:
    """Cost-by-service / cost-by-region / daily-spend share the same pivot shape
    used in load_csvs.py: first column is a label (service/region name or a date),
    remaining columns are cost-per-dimension plus a 'Total costs($)' column."""
    df = pl.read_csv(path, infer_schema_length=0)
    df = df.rename({df.columns[0]: "label"})
    cost_cols = [c for c in df.columns if c != "label"]
    total_col = next(c for c in cost_cols if c.strip().lower().startswith("total costs"))
    dim_cols = [c for c in cost_cols if c != total_col]

    is_total_row = df["label"].str.to_lowercase().str.contains("total")
    date_rows = df.filter(~is_total_row).with_columns(
        [pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0) for c in cost_cols]
    )

    total_spend = date_rows[total_col].sum()
    top3 = (
        date_rows.select(dim_cols)
        .sum()
        .transpose(include_header=True, header_name="dimension", column_names=["cost"])
        .sort("cost", descending=True)
        .head(3)
    )

    print(f"  Rows: {df.height}, Columns: {len(df.columns)}")
    print(f"  Total spend: ${total_spend:,.2f}")
    print("  Top 3:")
    for row in top3.iter_rows(named=True):
        print(f"    {row['dimension']:<35} ${row['cost']:,.2f}")


def summarize_daily(path: Path) -> None:
    """Daily spend has the same pivot shape as summarize_pivot, but its label
    column is a date rather than a service/region name - so report a date
    range and daily trend instead of just top-3 dimensions."""
    df = pl.read_csv(path, infer_schema_length=0)
    df = df.rename({df.columns[0]: "label"})
    cost_cols = [c for c in df.columns if c != "label"]
    total_col = next(c for c in cost_cols if c.strip().lower().startswith("total costs"))
    dim_cols = [c for c in cost_cols if c != total_col]

    is_total_row = df["label"].str.to_lowercase().str.contains("total")
    date_rows = df.filter(~is_total_row).with_columns(
        [pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0) for c in cost_cols]
    ).with_columns(
        pl.col("label").str.to_date(format="%Y-%m-%d", strict=False).alias("_date")
    ).drop_nulls("_date")

    total_spend = date_rows[total_col].sum()
    date_min, date_max = date_rows["_date"].min(), date_rows["_date"].max()
    avg_daily = date_rows[total_col].mean()
    peak = date_rows.sort(total_col, descending=True).head(1)
    peak_date, peak_cost = peak["_date"][0], peak[total_col][0]

    top3 = (
        date_rows.select(dim_cols)
        .sum()
        .transpose(include_header=True, header_name="service", column_names=["cost"])
        .sort("cost", descending=True)
        .head(3)
    )

    print(f"  Date range: {date_min} -> {date_max} ({date_rows.height} days)")
    print(f"  Total spend: ${total_spend:,.2f}")
    print(f"  Average daily spend: ${avg_daily:,.2f}")
    print(f"  Peak day: {peak_date} (${peak_cost:,.2f})")
    print("  Top 3 services over period:")
    for row in top3.iter_rows(named=True):
        print(f"    {row['service']:<35} ${row['cost']:,.2f}")


def summarize_generic(path: Path) -> None:
    """Budget-vs-actual and reserved-usage exports have a different shape than
    the service/region/daily pivots and no sample is on hand yet - just report
    shape/columns until the real format is known."""
    df = pl.read_csv(path, infer_schema_length=0)
    print(f"  Rows: {df.height}, Columns: {len(df.columns)}")
    print(f"  Columns: {df.columns}")


PROCESSORS = {
    "cost_by_service": summarize_pivot,
    "cost_by_region": summarize_pivot,
    "daily_spend": summarize_daily,
    "budget_vs_actual": summarize_generic,
    "reserved_usage": summarize_generic,
}


def process(path: Path) -> None:
    report_type = classify(path)
    print(f"\n=== {path.name} -> {report_type} ===")

    processor = PROCESSORS.get(report_type)
    if processor is None:
        print("  Unrecognized report type - skipping (left in incoming/).")
        return

    try:
        processor(path)
    except Exception as e:
        print(f"  Failed to process: {e}")
        return

    dest = PROCESSED / path.name
    shutil.move(str(path), str(dest))
    print(f"  Moved to {dest.relative_to(FOLDER)}")


class CSVHandler(FileSystemEventHandler):
    def __init__(self):
        self._seen = set()

    def _handle(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() != ".csv" or path in self._seen:
            return
        self._seen.add(path)
        if wait_until_stable(path):
            process(path)
        else:
            print(f"Gave up waiting for {path.name} to finish writing.")
        self._seen.discard(path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def main() -> None:
    INCOMING.mkdir(exist_ok=True)
    PROCESSED.mkdir(exist_ok=True)

    print(f"Watching {INCOMING} for new CSVs... (Ctrl+C to stop)")
    handler = CSVHandler()
    observer = Observer()
    observer.schedule(handler, str(INCOMING), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
