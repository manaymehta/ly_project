import pandas as pd
import numpy as np
import os
import pickle
from prophet import Prophet
import warnings
warnings.filterwarnings("ignore")

# ── Load datasets ──────────────────────────────────────────────────────────────
demand_history = pd.read_csv("datasets/demand_history.csv")
drugs          = pd.read_csv("datasets/drugs.csv")

demand_history.columns = demand_history.columns.str.strip()
drugs.columns          = drugs.columns.str.strip()

# ── Output directory for saved models ─────────────────────────────────────────
# Each model saved as: models/H001_D001.pkl
# Loaded on demand during prediction — not all loaded simultaneously
os.makedirs("prophet_models", exist_ok=True)

# ── Drug category lookup ───────────────────────────────────────────────────────
# Chronic drugs (Diabetes, Cardiovascular) are flat year-round
# Acute drugs (Antibiotic, Painkiller, Respiratory) have seasonal patterns
# Prophet uses this to configure seasonality strength per model
chronic_categories = {"Diabetes", "Cardiovascular"}
drug_category      = drugs.set_index("Drug ID")["Category"].to_dict()

# ── Get all unique hospital-drug pairs ────────────────────────────────────────
pairs = demand_history[["Hospital ID", "Drug ID"]].drop_duplicates()
total = len(pairs)
print(f"Training {total} Prophet models (one per hospital-drug pair)")
print(f"Saving to: prophet_models/")
print(f"Estimated time: 2-5 minutes\n")

# ── Train one model per pair ───────────────────────────────────────────────────
trained   = 0
failed    = 0
skipped   = 0

for _, pair in pairs.iterrows():
    hosp_id = pair["Hospital ID"]
    drug_id = pair["Drug ID"]
    category = drug_category.get(drug_id, "Antibiotic")

    # Filter to this specific pair
    subset = demand_history[
        (demand_history["Hospital ID"] == hosp_id) &
        (demand_history["Drug ID"]     == drug_id)
    ][["Date", "Units Demanded"]].copy()

    # Prophet requires columns named ds and y
    subset = subset.rename(columns={"Date": "ds", "Units Demanded": "y"})
    subset["ds"] = pd.to_datetime(subset["ds"], dayfirst=True)
    subset = subset.sort_values("ds").reset_index(drop=True)

    # Need minimum 2 non-zero rows to fit
    if len(subset) < 10 or subset["y"].sum() == 0:
        skipped += 1
        continue

    try:
        # ── Prophet configuration ──────────────────────────────────────────────
        # yearly_seasonality=True: learns the annual seasonal cycle
        #   — this is what captures monsoon/winter/summer patterns
        #   — with one year of data Prophet fits Fourier terms to the full cycle
        #   — forecasting beyond Dec 31 continues the curve naturally into Jan+
        #
        # daily_seasonality=False: no day-of-week patterns (hospital demand
        #   doesn't vary by weekday for chronic/acute drugs at this level)
        #
        # weekly_seasonality=False: same reasoning
        #
        # seasonality_mode:
        #   chronic drugs → additive: seasonal variation is a fixed amount
        #   acute drugs   → multiplicative: seasonal variation scales with level
        #   (antibiotic demand in a large hospital varies more in absolute terms
        #    during monsoon than in a small hospital — multiplicative captures this)

        is_chronic = category in chronic_categories

        model = Prophet(
            yearly_seasonality  = True,
            weekly_seasonality  = False,
            daily_seasonality   = False,
            seasonality_mode    = "additive" if is_chronic else "multiplicative",
            seasonality_prior_scale = 5.0 if is_chronic else 10.0,
            # Lower uncertainty interval — we want point forecasts not wide bands
            interval_width      = 0.80,
        )

        model.fit(subset)

        # Save model as pickle file
        model_path = f"prophet_models/{hosp_id}_{drug_id}.pkl"
        with open(model_path, "wb") as f:
            pickle.dump(model, f)

        trained += 1

        if trained % 20 == 0:
            print(f"  {trained}/{total} models trained...")

    except Exception as e:
        print(f"  WARNING: Failed {hosp_id}_{drug_id} — {e}")
        failed += 1

print(f"\nDone.")
print(f"  Trained:  {trained}")
print(f"  Skipped:  {skipped}")
print(f"  Failed:   {failed}")
print(f"  Models saved in: prophet_models/")

# ── Verification: test forecast for 3 key pairs ───────────────────────────────
print("\n── Forecast Verification ──")

def load_and_forecast(hosp_id, drug_id, future_date_str):
    model_path = f"prophet_models/{hosp_id}_{drug_id}.pkl"
    if not os.path.exists(model_path):
        return None
    with open(model_path, "rb") as f:
        model = pickle.load(f)
    future = model.make_future_dataframe(periods=90, freq="D")
    forecast = model.predict(future)
    target = forecast[forecast["ds"] == future_date_str][["ds", "yhat"]].values
    return round(target[0][1]) if len(target) > 0 else None

# Check 1: AIIMS Delhi Amoxicillin — August should be higher than April
aug_forecast  = load_and_forecast("H001", "D004", "2025-08-15")
apr_forecast  = load_and_forecast("H001", "D004", "2025-04-15")
if aug_forecast and apr_forecast:
    print(f"\nAIIMS Delhi Amoxicillin forecast:")
    print(f"  August 15:  {aug_forecast} units/day (monsoon peak)")
    print(f"  April 15:   {apr_forecast} units/day (summer trough)")
    print(f"  Seasonal pattern learned — {'PASS' if aug_forecast > apr_forecast else 'FAIL'}")

# Check 2: Nizam's Insulin — January and August should be similar (flat)
jan_insulin = load_and_forecast("H006", "D001", "2025-01-15")
aug_insulin = load_and_forecast("H006", "D001", "2025-08-15")
if jan_insulin and aug_insulin:
    diff_pct = abs(jan_insulin - aug_insulin) / jan_insulin * 100
    print(f"\nNizam's Institute Insulin forecast:")
    print(f"  January 15: {jan_insulin} units/day")
    print(f"  August 15:  {aug_insulin} units/day")
    print(f"  Difference: {diff_pct:.1f}% (expect <15% for flat chronic drug)")
    print(f"  Flat year-round — {'PASS' if diff_pct < 15 else 'FAIL'}")

# Check 3: AIIMS Salbutamol Inhaler — January higher than May (winter peak)
jan_salb = load_and_forecast("H001", "D017", "2025-01-15")
may_salb = load_and_forecast("H001", "D017", "2025-05-15")
if jan_salb and may_salb:
    print(f"\nAIIMS Delhi Salbutamol Inhaler forecast:")
    print(f"  January 15: {jan_salb} units/day (winter peak)")
    print(f"  May 15:     {may_salb} units/day (summer trough)")
    print(f"  Winter peak confirmed — {'PASS' if jan_salb > may_salb else 'FAIL'}")

# Check 4: Year-end continuity — December and January should both be elevated
# for respiratory drugs (winter spans both months)
dec_salb = load_and_forecast("H001", "D017", "2025-12-15")
jan_salb2 = load_and_forecast("H001", "D017", "2026-01-15")
if dec_salb and jan_salb2:
    print(f"\nYear-end continuity check (Salbutamol Inhaler):")
    print(f"  December 2025: {dec_salb} units/day")
    print(f"  January 2026:  {jan_salb2} units/day")
    print(f"  Both elevated for winter — {'PASS' if dec_salb > may_salb and jan_salb2 > may_salb else 'FAIL'}")

print("\n── How to use these models in prediction pipeline ──")
print("When a disruption is triggered on a specific date:")
print("  1. Load prophet_models/{hosp_id}_{drug_id}.pkl")
print("  2. Call model.make_future_dataframe(periods=30)")
print("  3. Call model.predict(future)")
print("  4. Sum forecast['yhat'] for next 30 days = monthly demand estimate")
print("  5. Feed into Demand Pressure = monthly demand ÷ remaining supply")