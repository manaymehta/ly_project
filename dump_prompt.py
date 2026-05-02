import sys
from sentinel import process_disruption
from analyst import analyse
from procurement_agent import _build_bridge_prompt

# 1. Re-run the exact same data load the test block does
event = process_disruption("Factory", "F002", "Disaster", "High", "2024-08-15")
pkgs  = analyse(event, verbose=False)
d004  = next((p for p in pkgs if p.drug_id == "D004"), None)

# 2. Extract the exact gap_hospitals list the triage logic creates
gap_hospitals = []
for h in d004.hospitals:
    if h.requires_action and h.days_until_stockout < d004.recovery_days:
        exposed_days = max(0.0, d004.recovery_days - h.days_until_stockout)
        bridge_units = round((h.prophet_forecast_30d / 30.0) * exposed_days)
        gap_hospitals.append({
            "hospital_id":         h.hospital_id,
            "hospital_name":       h.hospital_name,
            "days_until_stockout": round(h.days_until_stockout, 1),
            "bridge_units_needed": bridge_units,
        })

# 3. Build the exact prompt string
prompt_text = _build_bridge_prompt(d004, gap_hospitals)

# 4. Save to file
with open("exact_llm_prompt.txt", "w", encoding="utf-8") as f:
    f.write(prompt_text)

print("Prompt dumped to exact_llm_prompt.txt")
