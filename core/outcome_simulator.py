"""
outcome_simulator.py

Pure-Python stock trajectory simulation — no LLM, runs in milliseconds.

Given a package's hospital_coverage and procurement option, computes
day-by-day stock levels for two scenarios:

  approved  — bridge order placed, stock reinforced on delivery day(s)
  rejected  — no intervention, stock drains until factory recovers

Both trajectories run from day 0 → recovery_days.

Used by /api/packages/{id}/outcome to feed the dashboard outcome chart.
"""


def _deliveries_by_hospital(option_orders: list) -> dict:
    """
    Extracts {hospital_id: [(delivery_day, units)]} from a procurement option.
    Multiple distributor fills for the same hospital are kept as separate tuples.
    """
    out = {}
    for order in (option_orders or []):
        for alloc in order.get("hospital_allocations", []):
            hid = alloc.get("hospital_id")
            day = int(alloc.get("delivery_days") or 0)
            qty = float(alloc.get("units_allocated") or 0)
            if hid and qty > 0:
                out.setdefault(hid, []).append((day, qty))
    return out


def _simulate_trajectory(
    current_stock: float,
    daily_rate:    float,
    recovery_days: int,
    deliveries:    list,   # [(day, units), ...]
) -> list:
    """
    Returns [{day, stock}, ...] from day 0 to recovery_days (inclusive).
    Deliveries are added at the start of their arrival day, before that day's consumption.
    Stock never goes below 0.
    """
    by_day = {}
    for (d, qty) in deliveries:
        by_day[d] = by_day.get(d, 0.0) + qty

    stock  = float(current_stock)
    points = [{"day": 0, "stock": round(stock)}]

    for day in range(1, recovery_days + 1):
        stock += by_day.get(day, 0.0)    # bridge arrives
        stock  = max(0.0, stock - daily_rate)
        points.append({"day": day, "stock": round(stock)})

    return points


def simulate(
    hospital_coverage: list,
    procurement:       dict,
    recovery_days:     int,
    option_key:        str = "option_a",
) -> dict:
    """
    Runs the outcome simulation for a full package.

    Args:
        hospital_coverage  list of hospital dicts from full_package
        procurement        procurement section from full_package
        recovery_days      disruption recovery window in days
        option_key         "option_a" or "option_b"

    Returns dict with shape:
    {
      option_key, recovery_days,
      hospitals: [
        {
          hospital_id, hospital_name,
          days_until_stockout, daily_rate, current_stock,
          trajectory_approved: [{day, stock}, ...],
          trajectory_rejected: [{day, stock}, ...],
          stockout_days_approved, stockout_days_rejected, hospital_days_saved,
        }, ...
      ],
      summary: {
        total_hospital_days_saved,
        hospitals_protected,
        hospitals_still_at_risk,
        total_gap_hospitals,
      }
    }
    """
    option_orders  = (procurement or {}).get(option_key) or []
    deliveries_map = _deliveries_by_hospital(option_orders)

    total_days_saved      = 0
    hospitals_protected   = 0
    hospitals_still_at_risk = 0
    results               = []

    for h in hospital_coverage:
        hid    = h.get("hospital_id")
        status = h.get("coverage_status", "NONE")

        # Hospitals covered by factory never hit stockout — skip
        if status == "COVERED_BY_FACTORY":
            continue

        stockout      = float(h.get("days_until_stockout") or 0)
        exposed_days  = max(0.0, recovery_days - stockout)

        # Hospital doesn't hit stockout before factory recovers — not a gap hospital
        if exposed_days <= 0:
            continue

        # Use prophet_forecast_30d for the daily rate — units_required is the bridge
        # need (a subset of the forecast) and would inflate the rate if used directly.
        forecast_30d = float(h.get("prophet_forecast_30d") or h.get("units_required") or 0)
        if forecast_30d <= 0:
            continue

        daily_rate    = forecast_30d / 30.0
        current_stock = stockout * daily_rate

        h_deliveries = deliveries_map.get(hid, [])

        traj_approved = _simulate_trajectory(current_stock, daily_rate, recovery_days, h_deliveries)
        traj_rejected = _simulate_trajectory(current_stock, daily_rate, recovery_days, [])

        # Exclude recovery_days itself — factory resumes on that day so stock=0
        # landing exactly on recovery day is not a real stockout.
        so_approved = sum(1 for p in traj_approved if p["stock"] == 0 and p["day"] < recovery_days)
        so_rejected = sum(1 for p in traj_rejected if p["stock"] == 0 and p["day"] < recovery_days)
        days_saved  = max(0, so_rejected - so_approved)

        total_days_saved += days_saved
        if days_saved > 0:
            hospitals_protected += 1
        if so_approved > 0:
            hospitals_still_at_risk += 1

        results.append({
            "hospital_id":            hid,
            "hospital_name":          h.get("hospital_name", hid),
            "days_until_stockout":    round(stockout, 1),
            "daily_rate":             round(daily_rate, 1),
            "current_stock":          round(current_stock),
            "trajectory_approved":    traj_approved,
            "trajectory_rejected":    traj_rejected,
            "stockout_days_approved": so_approved,
            "stockout_days_rejected": so_rejected,
            "hospital_days_saved":    days_saved,
        })

    results.sort(key=lambda x: x["days_until_stockout"])

    return {
        "option_key":    option_key,
        "recovery_days": recovery_days,
        "hospitals":     results,
        "summary": {
            "total_hospital_days_saved": total_days_saved,
            "hospitals_protected":       hospitals_protected,
            "hospitals_still_at_risk":   hospitals_still_at_risk,
            "total_gap_hospitals":       len(results),
        },
    }
