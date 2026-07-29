"""
Fuel Theft Detection — cross-referencing audit
==============================================
Reproduces the analysis behind a Tableau dashboard I built at Charger Logistics
to detect potential fuel theft by cross-referencing fuel-card transactions
against GPS / telematics data.

Three independent checks run on every fuel-card transaction:

  1. MPG check       - miles driven (from automated GPS odometer) vs gallons
                       purchased, compared to the asset class's expected MPG.
                       Abnormally low MPG => more fuel bought than the truck
                       could have burned (possible siphoning / phantom gallons).
  2. Location check  - distance between the truck's GPS position at the
                       transaction timestamp and the pump location. A large gap
                       means the truck wasn't physically at the pump.
  3. Odometer check  - point-of-sale odometer vs the automated GPS odometer.
                       Static or artificially inflated entries are used to mask
                       excess purchases.

Transactions are scored, flagged with a reason, and rolled up into a per-driver
risk summary so investigators can target the worst profiles first.

Data here is synthetic. Usage:
    python fuel_theft_audit.py --txns data/fuel_transactions.csv \
        --vehicles data/vehicles.csv --outdir reports
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pandas as pd

# Thresholds (tunable) -------------------------------------------------------
MPG_TOLERANCE = 0.55       # flag if actual MPG < 55% of the asset's expected MPG
LOCATION_TOLERANCE_MI = 5.0  # flag if truck is > 5 miles from the pump
ODO_STATIC = True            # flag static / decreasing point-of-sale odometer
ODO_INFLATE_MI = 300         # flag if POS odometer overstates GPS by > 300 mi


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def prepare(txns: pd.DataFrame, vehicles: pd.DataFrame) -> pd.DataFrame:
    df = txns.merge(vehicles, on="vehicle_id", how="left")
    df = df.sort_values(["vehicle_id", "gps_odometer"]).reset_index(drop=True)
    # miles since this vehicle's previous fill, from the trusted GPS odometer
    df["miles_since_prev"] = df.groupby("vehicle_id")["gps_odometer"].diff()
    df["actual_mpg"] = df["miles_since_prev"] / df["gallons"]
    df["pump_distance_mi"] = df.apply(
        lambda r: haversine_mi(r["gps_lat"], r["gps_lon"],
                               r["pump_lat"], r["pump_lon"]),
        axis=1,
    )
    df["odo_gap_mi"] = df["pos_odometer"] - df["gps_odometer"]
    df["prev_pos_odo"] = df.groupby("vehicle_id")["pos_odometer"].shift()
    return df


def flag(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in df.iterrows():
        reasons = []
        if pd.notna(r["actual_mpg"]) and r["miles_since_prev"] > 0:
            if r["actual_mpg"] < r["expected_mpg"] * MPG_TOLERANCE:
                reasons.append(
                    f"Low MPG {r['actual_mpg']:.1f} vs expected "
                    f"{r['expected_mpg']:.1f}")
        if r["pump_distance_mi"] > LOCATION_TOLERANCE_MI:
            reasons.append(
                f"Truck {r['pump_distance_mi']:.0f} mi from pump")
        if ODO_STATIC and pd.notna(r["prev_pos_odo"]) and \
                r["pos_odometer"] <= r["prev_pos_odo"]:
            reasons.append("Static/decreasing POS odometer")
        if r["odo_gap_mi"] > ODO_INFLATE_MI:
            reasons.append(
                f"POS odometer inflated by {r['odo_gap_mi']:.0f} mi")

        if reasons:
            rows.append({
                "txn_id": r["txn_id"],
                "date": r["txn_date"],
                "vehicle_id": r["vehicle_id"],
                "driver_id": r["driver_id"],
                "asset_class": r["asset_class"],
                "gallons": r["gallons"],
                "total_cost": r["total_cost"],
                "flags": len(reasons),
                "reasons": "; ".join(reasons),
            })
    return pd.DataFrame(rows)


def driver_summary(flags: pd.DataFrame, outdir: Path) -> Path:
    path = outdir / "driver_risk_summary.md"
    if flags.empty:
        path.write_text("# Driver Risk Summary\n\nNo anomalies found.\n",
                        encoding="utf-8")
        return path
    g = (flags.groupby("driver_id")
         .agg(flagged_txns=("txn_id", "count"),
              total_flags=("flags", "sum"),
              exposure=("total_cost", "sum"))
         .sort_values("total_flags", ascending=False))
    lines = ["# Driver Risk Summary", "",
             "Drivers ranked by number of anomaly flags — investigate top first.",
             "",
             "| Driver | Flagged txns | Total flags | $ exposure |",
             "|--------|-------------:|------------:|-----------:|"]
    for drv, row in g.iterrows():
        lines.append(f"| {drv} | {int(row['flagged_txns'])} | "
                     f"{int(row['total_flags'])} | ${row['exposure']:,.2f} |")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Fuel theft cross-referencing audit.")
    ap.add_argument("--txns", default="data/fuel_transactions.csv")
    ap.add_argument("--vehicles", default="data/vehicles.csv")
    ap.add_argument("--outdir", default="reports")
    args = ap.parse_args()

    txns = pd.read_csv(args.txns)
    vehicles = pd.read_csv(args.vehicles)
    df = prepare(txns, vehicles)
    flags = flag(df)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    flags.to_csv(outdir / "flagged_transactions.csv", index=False)
    summary_path = driver_summary(flags, outdir)

    n_flagged = flags["txn_id"].nunique() if not flags.empty else 0
    exposure = flags["total_cost"].sum() if not flags.empty else 0.0
    print(f"Reviewed {len(df):,} fuel transactions across "
          f"{df['vehicle_id'].nunique()} vehicles.")
    print(f"Flagged {n_flagged} transactions "
          f"({flags['driver_id'].nunique() if not flags.empty else 0} drivers).")
    print(f"Cost exposure on flagged fills: ${exposure:,.2f}")
    print(f"  Flagged txns  -> {outdir/'flagged_transactions.csv'}")
    print(f"  Driver risk   -> {summary_path}")


if __name__ == "__main__":
    main()
