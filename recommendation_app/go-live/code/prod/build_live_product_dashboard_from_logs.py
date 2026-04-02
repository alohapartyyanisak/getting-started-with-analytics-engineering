from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from build_mock_product_dashboard import compute_metrics, render_dashboard


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IN = ROOT / "data" / "mock_launch_analytics" / "product_analytics_logs.json"
DEFAULT_OUT_DIR = ROOT / "doc" / "launch_assets"


def load_gcloud_logging_export(path: Path) -> pd.DataFrame:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return pd.DataFrame()

    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]

    payload_rows = [row.get("jsonPayload", {}) for row in rows if isinstance(row.get("jsonPayload", {}), dict)]
    if not payload_rows:
        return pd.DataFrame()

    df = pd.DataFrame(payload_rows)
    props = pd.json_normalize(df["event_props"]).add_prefix("event_props.") if "event_props" in df.columns else pd.DataFrame(index=df.index)
    meta = pd.json_normalize(df["metadata"]).add_prefix("metadata.") if "metadata" in df.columns else pd.DataFrame(index=df.index)
    drop_cols = [col for col in ["event_props", "metadata"] if col in df.columns]
    df = pd.concat([df.drop(columns=drop_cols), props, meta], axis=1)
    df["event_ts_utc"] = pd.to_datetime(df["event_ts_utc"], utc=True, format="ISO8601")
    df["event_day"] = df["event_ts_utc"].dt.strftime("%Y-%m-%d")
    df["event_hour"] = df["event_ts_utc"].dt.hour
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a launch dashboard preview from Cloud Logging product_analytics exports.")
    parser.add_argument("--in", dest="in_path", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--traffic-source", default="")
    args = parser.parse_args()

    df = load_gcloud_logging_export(args.in_path)
    if df.empty:
        raise SystemExit(f"No rows found in {args.in_path}")

    df = df[df["event_type"] == "product_analytics"].copy()
    if args.traffic_source:
        df = df[df.get("traffic_source", "") == args.traffic_source].copy()
    if df.empty:
        raise SystemExit("No matching product_analytics rows after filtering.")

    metrics, daily, hourly = compute_metrics(df)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{args.traffic_source}" if args.traffic_source else ""
    out_png = args.out_dir / f"live_launch_product_dashboard{suffix}.png"
    out_pdf = args.out_dir / f"live_launch_product_dashboard{suffix}.pdf"
    out_json = args.out_dir / f"live_launch_product_dashboard_metrics{suffix}.json"
    out_csv = args.out_dir / f"live_launch_product_dashboard_daily{suffix}.csv"

    render_dashboard(metrics=metrics, daily=daily, hourly=hourly, out_png=out_png, out_pdf=out_pdf)
    out_json.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    daily.to_csv(out_csv, index=False)

    print(f"Dashboard PNG: {out_png}")
    print(f"Dashboard PDF: {out_pdf}")
    print(f"Metrics JSON: {out_json}")
    print(f"Daily CSV: {out_csv}")


if __name__ == "__main__":
    main()
