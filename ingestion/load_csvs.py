"""Week 1 demo: load all Cost Explorer CSV exports and print summary stats."""
from pathlib import Path
import polars as pl

FOLDER = Path(__file__).parent


def load(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path, infer_schema_length=0)
    return df.rename({df.columns[0]: "label"})


def summarize(path: Path) -> None:
    df = load(path)
    cost_cols = [c for c in df.columns if c != "label"]
    total_col = next(c for c in cost_cols if c.strip().lower().startswith("total costs"))
    service_cols = [c for c in cost_cols if c != total_col]

    is_total_row = df["label"].str.to_lowercase().str.contains("total")
    date_rows = df.filter(~is_total_row)
    date_rows = date_rows.with_columns(
        [pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0) for c in cost_cols]
    ).with_columns(
        pl.col("label").str.to_date(format="%Y-%m-%d", strict=False).alias("_date")
    ).drop_nulls("_date")

    total_spend = date_rows[total_col].sum()
    date_min, date_max = date_rows["_date"].min(), date_rows["_date"].max()

    top3 = (
        date_rows.select(service_cols)
        .sum()
        .transpose(include_header=True, header_name="service", column_names=["cost"])
        .sort("cost", descending=True)
        .head(3)
    )

    print(f"\n=== {path.name} ===")
    print(f"Row count        : {df.height}")
    print(f"Columns ({len(df.columns)}): {df.columns}")
    print(f"Total spend      : ${total_spend:,.2f}")
    print(f"Date range       : {date_min} -> {date_max}")
    print("Top 3 by cost    :")
    for row in top3.iter_rows(named=True):
        print(f"   {row['service']:<35} ${row['cost']:,.2f}")


def main() -> None:
    csv_files = sorted(FOLDER.glob("*.csv"))
    if not csv_files:
        print("No CSV files found.")
        return
    for f in csv_files:
        try:
            summarize(f)
        except Exception as e:
            print(f"Failed to process {f.name}: {e}")


if __name__ == "__main__":
    main()
