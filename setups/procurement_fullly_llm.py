"""
procurement_agent.py

Procurement Agent — receives a DrugAlertPackage from the Analyst and
produces a structured procurement recommendation for human review.

Two execution paths based on supply loss scenario:

    TOTAL LOSS (supply_loss_pct = 1.0, drug_units_remaining = 0)
    ─────────────────────────────────────────────────────────────
    Single LLM call. LLM sees each hospital with its own distributor
    options interleaved — per-hospital reasoning, then consolidated orders.

    PARTIAL LOSS (0 < supply_loss_pct < 1.0, drug_units_remaining > 0)
    ───────────────────────────────────────────────────────────────────
    Two sequential LLM calls:
        Call 1 — ALLOCATION: which hospitals have a buffer gap before
                 normal resupply arrives? No distributor data — pure triage.
        Call 2 — BRIDGE ORDER: for gap hospitals, which distributor(s)
                 can bridge the shortfall within their stockout window?

Dicey case handling:
    When two distributor options are genuinely close (e.g. faster but below
    min-order vs slower but cheaper and meets min-order), the LLM sets
    is_dicey_case=true and returns option_a and option_b with tradeoffs.
    The dashboard operator decides which to approve.

Key data facts:
    current_stock  = distributor's total stock for this drug (same across hospitals)
    min_order      = same per distributor regardless of hospital
    price_per_unit = same per distributor regardless of hospital
    delivery_days  = VARIES per hospital — shown per-hospital in prompts
    BELOW MIN ORDER = advisory flag, not hard disqualification

Install:    pip install google-genai
"""

import json
from typing import Optional
from google import genai
from google.genai import types

from analyst import DrugAlertPackage, HospitalRisk

# ── CONFIG ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyBsWNU1nC6Ntn0H4CaYtAaudchc39E1etA"
GEMINI_MODEL   = "gemma-4-26b-a4b-it"   # ← change model name here if needed
# ──────────────────────────────────────────────────────────────────────────────

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = (
    "You are a pharmaceutical supply chain procurement specialist. "
    "You produce structured JSON recommendations for human review. "
    "Never auto-execute orders. Always flag caveats clearly. "
    "Respond with valid JSON only — no markdown fences, no preamble, "
    "no explanation outside the JSON."
)


# ══════════════════════════════════════════════════════════════════════════════
# CONTEXT BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _disruption_block(pkg: DrugAlertPackage) -> str:
    return (
        f"DISRUPTION\n"
        f"  Node:          {pkg.disrupted_name} ({pkg.disrupted_node})\n"
        f"  Type:          {pkg.disruption_type}\n"
        f"  Date:          {pkg.triggered_date}\n"
        f"  Recovery:      ~{pkg.recovery_days} days\n"
        f"  Supply lost:   {pkg.supply_loss_pct * 100:.0f}% of global supply\n"
        f"  Remaining:     {pkg.drug_units_remaining:,.0f} drug units/month "
        f"from other producers\n"
        f"  System demand: {pkg.system_total_forecast:,.0f} units forecast "
        f"over next 30 days"
    )


def _drug_block(pkg: DrugAlertPackage) -> str:
    return (
        f"DRUG AT RISK\n"
        f"  ID:          {pkg.drug_id}\n"
        f"  Name:        {pkg.drug_name}\n"
        f"  Criticality: {pkg.criticality}\n"
        f"  Category:    {pkg.category}\n"
        f"  API source:  {pkg.api_context.get('api_name', 'Unknown')}"
    )


def _hospitals_only_block(pkg: DrugAlertPackage) -> str:
    """
    Plain hospital list without distributor data.
    Used in Call 1 (Allocation) where distributor context is intentionally absent.
    """
    lines = ["AFFECTED HOSPITALS (ranked by urgency — most urgent first)"]
    for i, h in enumerate(pkg.hospitals, 1):
        if not h.requires_action:
            continue
        exposed = max(0.0, pkg.recovery_days - h.days_until_stockout)
        lines.append(
            f"  {i}. [{h.hospital_id}] {h.hospital_name} ({h.hospital_city}) | {h.specialty_type}\n"
            f"     Risk: {h.risk_level} | Stockout in: {h.days_until_stockout:.1f} days\n"
            f"     Exposed for: {exposed:.1f} days\n"
            f"     30-day demand forecast: {h.prophet_forecast_30d:,.0f} units"
        )
    return "\n".join(lines)


def _hospitals_with_distributors_block(
    pkg:          DrugAlertPackage,
    hospital_ids: Optional[list] = None,
) -> str:
    """
    Per-hospital section interleaved with that hospital's distributor options.
    Each hospital shows urgency data then its distributors sorted by delivery speed.
    Delivery feasibility pre-computed per hospital per distributor.
    BELOW MIN ORDER shown as advisory — not a hard disqualification.
    """
    if hospital_ids is None:
        target_hids = [h.hospital_id for h in pkg.hospitals if h.requires_action]
    else:
        target_hids = hospital_ids

    # Build distributor meta lookup — stock/price/min-order same across hospitals
    dist_meta = {}
    for h in pkg.hospitals:
        for d in h.distributors:
            did = d["distributor_id"]
            if did not in dist_meta:
                dist_meta[did] = {
                    "name":                d["name"],
                    "city":                d["city"],
                    "delivery_speed_class":d["delivery_speed_class"],
                    "reliability_score":   d["reliability_score"],
                    "pricing_tier":        d["pricing_tier"],
                    "current_stock":       d["current_stock"],
                    "min_order":           d["min_order"],
                    "price_per_unit":      d["price_per_unit"],
                }

    lines = [
        "HOSPITALS AND THEIR DISTRIBUTOR OPTIONS",
        "(Each hospital lists distributors sorted by delivery speed.)",
        "(BELOW MIN ORDER = negotiation may be required — do not auto-disqualify.)",
        "(current_stock = distributor total stock for this drug, not per-hospital.)",
    ]

    for h in pkg.hospitals:
        if h.hospital_id not in target_hids:
            continue

        exposed = max(0.0, pkg.recovery_days - h.days_until_stockout)
        lines.append(
            f"\n── {h.hospital_id} {h.hospital_name} ({h.hospital_city}) ──\n"
            f"   Specialty: {h.specialty_type}\n"
            f"   Risk: {h.risk_level} | Stockout in: {h.days_until_stockout:.1f} days\n"
            f"   Exposed for: {exposed:.1f} days | "
            f"Units needed (30d): {h.prophet_forecast_30d:,.0f}"
        )

        if not h.distributors:
            lines.append("   No distributors available.")
            continue

        # Sort distributors by delivery days ascending for this hospital
        sorted_dists = sorted(h.distributors, key=lambda d: d["delivery_days"])
        lines.append("   Distributor options (fastest first):")

        for d in sorted_dists:
            did      = d["distributor_id"]
            meta     = dist_meta.get(did, d)
            delivery = d["delivery_days"]
            stockout = h.days_until_stockout

            # Delivery feasibility
            if delivery < stockout:
                feasibility = (
                    f"ARRIVES IN TIME ({delivery}d < stockout {stockout:.1f}d)"
                )
            else:
                feasibility = (
                    f"ARRIVES AFTER STOCKOUT ({delivery}d > stockout {stockout:.1f}d)"
                )

            # Min-order advisory
            if meta["current_stock"] >= meta["min_order"]:
                order_note = f"meets min-order ({meta['min_order']:,} units)"
            else:
                order_note = (
                    f"BELOW MIN ORDER — stock {meta['current_stock']:,} "
                    f"< min {meta['min_order']:,} — negotiation may be required"
                )

            lines.append(
                f"     {did} {meta['name']} ({meta['city']})"
                f" | Speed: {meta['delivery_speed_class']}"
                f" | Reliability: {meta['reliability_score']}\n"
                f"       Stock: {meta['current_stock']:,} units"
                f" | Price: Rs {meta['price_per_unit']}/unit"
                f" | {order_note}\n"
                f"       {feasibility}"
            )

    return "\n".join(lines)


def _alternatives_block(pkg: DrugAlertPackage) -> str:
    if not pkg.alternatives:
        return "ALTERNATIVE DRUGS\n  None available."
    lines = ["ALTERNATIVE DRUGS"]
    for a in pkg.alternatives:
        shared = a.get("shared_api_risk", False)
        risk_note = (
            "WARNING: SHARED API RISK — uses same disrupted API. "
            "Supply also at risk — do not rely on this as fallback."
            if shared
            else "Safe — uses different API source."
        )
        lines.append(
            f"  {a['alt_drug_id']} {a['alt_drug_name']} [{a['alt_criticality']}]\n"
            f"    Similarity: {a['similarity_score']} | {risk_note}\n"
            f"    Notes: {a['substitution_notes']}"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# PATH 1 — SINGLE CALL (total supply loss)
# ══════════════════════════════════════════════════════════════════════════════

def _build_single_call_prompt(pkg: DrugAlertPackage) -> str:
    task = (
        "TASK\n"
        "All supply of this drug from the disrupted source is lost.\n"
        "Distributors hold existing stock to bridge the gap until supply recovers.\n\n"
        "Each hospital section above lists its distributors sorted by speed with\n"
        "delivery feasibility pre-computed for that hospital specifically.\n"
        "BELOW MIN ORDER = negotiation may be required — do not auto-disqualify.\n"
        "Consider these distributors if they offer better speed or cost.\n\n"
        "Reason through ALL of the following in order:\n"
        "  1. HOSPITAL PRIORITY: Rank all actionable hospitals by urgency\n"
        "     (days_until_stockout ascending). Note which will run out before\n"
        "     any distributor can reach them.\n"
        "  2. PER-HOSPITAL ORDER: For each hospital, identify the best distributor\n"
        "     considering in order: delivery feasibility (arrives before stockout),\n"
        "     then stock sufficiency for that hospital's 30d forecast, then price.\n"
        "  3. CONSOLIDATION: If multiple hospitals share the same best distributor,\n"
        "     combine into one order. Split orders are fine when hospitals need\n"
        "     different distributors.\n"
        "  4. COVERAGE: For each order compute:\n"
        "       units_required = hospital's 30-day forecast\n"
        "       units_allocated = what you allocate to that hospital\n"
        "     Flag any hospital where units_allocated < units_required.\n"
        "  5. DICEY CASE: If two distributors are genuinely close in merit\n"
        "     (e.g. one faster but below min-order, another slower but meets\n"
        "     min-order and is cheaper), set is_dicey_case=true and populate\n"
        "     both option_a and option_b with full order details and tradeoffs.\n"
        "     Only flag as dicey when the choice is genuinely non-obvious.\n"
        "  6. ALTERNATIVE DRUG: If shared_api_risk is True for all alternatives,\n"
        "     state clearly no viable drug alternative exists.\n"
        "  7. CAVEATS: Anything the reviewer must know before approving.\n\n"
        "Respond with valid JSON only:\n"
        "{\n"
        '  "recommendation_summary": "<2-3 sentences>",\n'
        '  "is_dicey_case": false,\n'
        '  "dicey_tradeoff": null,\n'
        '  "hospital_priority_order": [\n'
        '    {\n'
        '      "rank": 1,\n'
        '      "hospital_id": "...",\n'
        '      "hospital_name": "...",\n'
        '      "days_until_stockout": ...,\n'
        '      "units_required_30d": ...,\n'
        '      "urgency_note": "<one sentence>"\n'
        '    }\n'
        '  ],\n'
        '  "option_a": [\n'
        '    {\n'
        '      "distributor_id": "...",\n'
        '      "distributor_name": "...",\n'
        '      "total_quantity": ...,\n'
        '      "price_per_unit": ...,\n'
        '      "hospital_allocations": [\n'
        '        {\n'
        '          "hospital_id": "...",\n'
        '          "hospital_name": "...",\n'
        '          "delivery_days": ...,\n'
        '          "units_required": ...,\n'
        '          "units_allocated": ...,\n'
        '          "coverage_note": "<ok or shortfall amount>"\n'
        '        }\n'
        '      ],\n'
        '      "rationale": "<one sentence>"\n'
        '    }\n'
        '  ],\n'
        '  "option_b": null,\n'
        '  "total_stock_gap": ...,\n'
        '  "alternative_drug_assessment": "<one sentence>",\n'
        '  "caveats": ["<caveat 1>"],\n'
        '  "procurement_viable": true\n'
        "}\n\n"
        "If is_dicey_case is true: populate option_b with same structure as option_a,\n"
        "and set dicey_tradeoff to one sentence explaining what the human must decide.\n"
        "option_a is always the primary recommendation."
    )
    return "\n\n".join([
        _disruption_block(pkg),
        _drug_block(pkg),
        _hospitals_with_distributors_block(pkg),
        _alternatives_block(pkg),
        task,
    ])


# ══════════════════════════════════════════════════════════════════════════════
# PATH 2 — TWO CALLS (partial supply loss)
# ══════════════════════════════════════════════════════════════════════════════

def _build_allocation_prompt(pkg: DrugAlertPackage) -> str:
    """
    Call 1 — Allocation triage.
    No distributor data — purely about which hospitals have a gap
    before normal resupply can arrive through standard channels (~14 days).

    Pre-computes per hospital in Python before the LLM sees the prompt:
      - min_delivery_days: fastest any distributor can reach that hospital
      - needs_bridge: stock runs out before the fastest distributor arrives
      - bridge_units: daily_demand × (min_delivery_days - days_until_stockout)
        → covers exactly the gap from stockout to first possible delivery
    LLM reads pre-computed labels — does not recalculate.
    """
    covers = pkg.drug_units_remaining >= pkg.system_total_forecast

    # Pre-compute min delivery days per hospital from distributor data
    hosp_min_delivery: dict[str, int] = {}
    for h in pkg.hospitals:
        if not h.requires_action:
            continue
        hosp_min_delivery[h.hospital_id] = (
            min(d["delivery_days"] for d in h.distributors)
            if h.distributors else 999
        )

    # Build labelled hospital section — all maths done here, not by the LLM
    hosp_lines = []
    for h in pkg.hospitals:
        if not h.requires_action:
            continue
        min_del       = hosp_min_delivery.get(h.hospital_id, 999)
        daily_demand  = h.prophet_forecast_30d / 30.0
        exposed_days  = max(0.0, pkg.recovery_days - h.days_until_stockout)
        bridge_units  = round(daily_demand * exposed_days)
        needs_bridge  = h.days_until_stockout < pkg.recovery_days
        hosp_lines.append(
            f"  [{h.hospital_id}] {h.hospital_name} | "
            f"Stockout in {h.days_until_stockout:.1f}d | "
            f"Recovery in {pkg.recovery_days}d | "
            f"Fastest distributor: {min_del}d | "
            f"{'NEEDS BRIDGE — runs out before factory recovers' if needs_bridge else 'covered — stock lasts full recovery'} | "
            f"Exposed window: {exposed_days:.1f}d | Bridge units: {bridge_units:,}"
        )
    hosp_section = "HOSPITAL BRIDGE ANALYSIS\n" + "\n".join(hosp_lines)

    covers_str = "COVERS" if covers else "DOES NOT COVER"
    task = (
        "TASK — ALLOCATION DECISION\n"
        f"The disrupted source is partially offline. Remaining factory supply:\n"
        f"  {pkg.drug_units_remaining:,.0f} units/month from other producers\n"
        f"  {pkg.system_total_forecast:,.0f} units total 30-day system demand\n"
        f"  Factory supply {covers_str} total demand.\n\n"
        "A hospital NEEDS BRIDGE if its current stock runs out before the factory\n"
        f"recovers (~{pkg.recovery_days} days). The exposed window and bridge units\n"
        "are already computed above — use them directly.\n\n"
        "Reason through:\n"
        "  1. Which hospitals are marked NEEDS BRIDGE? List them.\n"
        "  2. For each NEEDS BRIDGE hospital, use the pre-computed bridge_units directly.\n"
        "     Do NOT recalculate — use the figure shown in the analysis above.\n"
        "  3. Rank by severity (largest bridge_units first).\n\n"
        "Respond with valid JSON only:\n"
        "{\n"
        '  "allocation_summary": "<2-3 sentences>",\n'
        f'  "factory_covers_demand": {str(covers).lower()},\n'
        '  "hospitals_needing_bridge": [\n'
        '    {\n'
        '      "hospital_id": "...",\n'
        '      "hospital_name": "...",\n'
        '      "days_until_stockout": ...,\n'
        '      "fastest_delivery_days": ...,\n'
        '      "gap_days": ...,\n'
        '      "bridge_units_needed": ...,\n'
        '      "priority": 1\n'
        '    }\n'
        '  ],\n'
        '  "hospitals_covered_by_factory": ["H001", "H002"],\n'
        '  "allocation_note": "<one sentence>"\n'
        "}"
    )
    return "\n\n".join([
        _disruption_block(pkg),
        _drug_block(pkg),
        hosp_section,
        task,
    ])


def _build_bridge_order_prompt(
    pkg:        DrugAlertPackage,
    allocation: dict,
) -> str:
    """
    Call 2 — Bridge order.
    Focused only on gap hospitals identified by Call 1.
    Uses per-hospital distributor block for focused context.
    """
    gap_hospitals = allocation.get("hospitals_needing_bridge", [])
    gap_hids      = [h["hospital_id"] for h in gap_hospitals]

    if not gap_hids:
        return ""

    gap_lines = []
    for h in gap_hospitals:
        gap_lines.append(
            f"  {h['hospital_id']} ({h.get('hospital_name', '')}) | "
            f"Stockout in {h['days_until_stockout']} days | "
            f"Bridge needed: {h.get('bridge_units_needed', '?')} units"
        )
    gap_section = (
        "HOSPITALS NEEDING BRIDGE ORDER (from allocation — Call 1)\n" +
        "\n".join(gap_lines)
    )

    task = (
        f"TASK — BRIDGE ORDER DECISION\n"
        f"{len(gap_hospitals)} hospital(s) will run out before normal resupply arrives.\n"
        f"{allocation.get('allocation_note', '')}\n\n"
        "Reason through:\n"
        "  1. Per gap hospital: which distributors arrive before stockout?\n"
        "  2. Does that distributor have sufficient stock for bridge_units_needed?\n"
        "     If below min-order, flag for negotiation — still consider if best option.\n"
        "  3. Consolidate: if multiple hospitals share same best distributor, combine.\n"
        "  4. units_allocated = max(bridge_units_needed, min_order), capped at current_stock.\n"
        "     If min_order > current_stock, use current_stock and flag shortfall.\n"
        "  5. DICEY CASE: if two distributors are genuinely close in merit for a hospital,\n"
        "     set is_dicey_case=true and provide option_a and option_b.\n"
        "  6. Flag any hospital that cannot be served in time by any distributor.\n\n"
        "Respond with valid JSON only:\n"
        "{\n"
        '  "bridge_order_summary": "<2-3 sentences>",\n'
        '  "is_dicey_case": false,\n'
        '  "dicey_tradeoff": null,\n'
        '  "option_a": [\n'
        '    {\n'
        '      "distributor_id": "...",\n'
        '      "distributor_name": "...",\n'
        '      "total_quantity": ...,\n'
        '      "price_per_unit": ...,\n'
        '      "hospital_allocations": [\n'
        '        {\n'
        '          "hospital_id": "...",\n'
        '          "hospital_name": "...",\n'
        '          "delivery_days": ...,\n'
        '          "units_required": ...,\n'
        '          "units_allocated": ...,\n'
        '          "coverage_note": "<ok or shortfall>"\n'
        '        }\n'
        '      ],\n'
        '      "rationale": "<one sentence>"\n'
        '    }\n'
        '  ],\n'
        '  "option_b": null,\n'
        '  "hospitals_unserviceable": [],\n'
        '  "total_bridge_cost_estimate": ...,\n'
        '  "caveats": ["<caveat 1>"],\n'
        '  "bridge_viable": true\n'
        "}\n\n"
        "If is_dicey_case is true: populate option_b and set dicey_tradeoff."
    )
    return "\n\n".join([
        _disruption_block(pkg),
        _drug_block(pkg),
        gap_section,
        _hospitals_with_distributors_block(pkg, hospital_ids=gap_hids),
        task,
    ])


# ══════════════════════════════════════════════════════════════════════════════
# LLM CALL + PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _call_gemini(prompt: str, max_tokens: int = 2048) -> str:
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=max_tokens,
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )
    return response.text or ""


def _parse_json(raw: str) -> tuple[dict, bool, str]:
    """Returns (parsed_dict, success, error_msg). Never raises."""
    if raw is None or not raw.strip():
        return {}, False, "LLM returned empty or None response"
    try:
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip()), True, ""
    except (json.JSONDecodeError, IndexError) as e:
        return {}, False, str(e)


def _parse_error_result(drug_id, drug_name, scenario, err, raw, call_num) -> dict:
    return {
        "drug_id":                drug_id,
        "drug_name":              drug_name,
        "scenario":               scenario,
        "parse_ok":               False,
        "parse_error":            err,
        f"raw_response_call{call_num}": raw,
        "recommendation_summary": f"Call {call_num} parse failed — see raw_response.",
        "hospital_priority_order":[],
        "option_a":               [],
        "option_b":               None,
        "is_dicey_case":          False,
        "alternative_drug_assessment": "N/A",
        "caveats":                ["LLM response could not be parsed as JSON."],
        "procurement_viable":     False,
        "call_count":             call_num,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AGENT FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def run_procurement_agent(
    pkg:     DrugAlertPackage,
    verbose: bool = True,
) -> dict:
    """
    Main Procurement Agent entry point.
    Routes to single-call (total loss) or two-call (partial loss) path.

    Returns structured recommendation dict for the Aggregator.
    Keys always present: drug_id, drug_name, scenario, procurement_viable,
                         parse_ok, call_count, caveats, is_dicey_case,
                         option_a (primary), option_b (if dicey)
    """
    is_total_loss = pkg.supply_loss_pct >= 1.0 or pkg.drug_units_remaining == 0
    scenario      = "TOTAL_LOSS" if is_total_loss else "PARTIAL_LOSS"

    if verbose:
        print(f"\n  [Procurement Agent] {pkg.drug_name} ({pkg.drug_id}) | {scenario}")

    result = {"drug_id": pkg.drug_id, "drug_name": pkg.drug_name, "scenario": scenario}

    # ── PATH 1: Single call — total supply loss ────────────────────────────────
    if is_total_loss:
        if verbose:
            print("    → Single-call path")

        raw    = _call_gemini(_build_single_call_prompt(pkg))
        parsed, ok, err = _parse_json(raw)

        if not ok:
            return _parse_error_result(
                pkg.drug_id, pkg.drug_name, scenario, err, raw, 1)

        result.update(parsed)
        result["parse_ok"]   = True
        result["call_count"] = 1

    # ── PATH 2: Two calls — partial supply loss ────────────────────────────────
    else:
        if verbose:
            print("    → Two-call path (allocation → bridge order)")

        # Call 1: Allocation
        if verbose:
            print("    Call 1: Allocation...")
        raw1 = _call_gemini(_build_allocation_prompt(pkg), max_tokens=1024)
        allocation, ok1, err1 = _parse_json(raw1)

        if not ok1:
            return _parse_error_result(
                pkg.drug_id, pkg.drug_name, scenario, err1, raw1, 1)

        result["allocation"] = allocation
        gap_hospitals = allocation.get("hospitals_needing_bridge", [])

        # ── Safety guard for hospital IDs ────────────────────────────────────
        real_ids = {h.hospital_id for h in pkg.hospitals}
        for gh in gap_hospitals:
            if gh.get("hospital_id", "") not in real_ids:
                if verbose:
                    print(f"    WARNING: Unrecognised hospital_id '{gh.get('hospital_id', '')}' "
                          f"returned by Call 1 — excluded from distributor block.")

        if verbose:
            print(f"    Call 1 done. Gap hospitals: {len(gap_hospitals)}")

        # No bridge needed — factory covers all
        if not gap_hospitals:
            result.update({
                "parse_ok":               True,
                "call_count":             1,
                "bridge_order":           None,
                "recommendation_summary": allocation.get("allocation_summary", ""),
                "is_dicey_case":          False,
                "option_a":               [],
                "option_b":               None,
                "caveats": [
                    "No emergency bridge order required — "
                    "factory supply covers all hospitals through normal channels."
                ],
                "procurement_viable": True,
            })
            if verbose:
                print("    No bridge needed.")
            return result

        # Call 2: Bridge order
        if verbose:
            print(f"    Call 2: Bridge order for {len(gap_hospitals)} hospital(s)...")
        raw2 = _call_gemini(
            _build_bridge_order_prompt(pkg, allocation), max_tokens=2048)
        bridge, ok2, err2 = _parse_json(raw2)

        if not ok2:
            result.update(
                _parse_error_result(
                    pkg.drug_id, pkg.drug_name, scenario, err2, raw2, 2))
            result["allocation"] = allocation
            return result

        result.update({
            "parse_ok":               True,
            "call_count":             2,
            "bridge_order":           bridge,
            "recommendation_summary": (
                allocation.get("allocation_summary", "") + " " +
                bridge.get("bridge_order_summary", "")
            ).strip(),
            "is_dicey_case":  bridge.get("is_dicey_case", False),
            "dicey_tradeoff": bridge.get("dicey_tradeoff"),
            "option_a":       bridge.get("option_a", []),
            "option_b":       bridge.get("option_b"),
            "procurement_viable": bridge.get("bridge_viable", False),
            "caveats":        bridge.get("caveats", []),
        })

    # ── Verbose summary ────────────────────────────────────────────────────────
    if verbose:
        summary = result.get("recommendation_summary", "")
        print(f"    Summary: {summary[:120]}...")

        orders = result.get("option_a", [])
        print(f"    Orders (option_a): {len(orders)}")
        for o in orders:
            for alloc in o.get("hospital_allocations", []):
                print(f"      {alloc.get('hospital_id')} — "
                      f"need={alloc.get('units_required')} "
                      f"get={alloc.get('units_allocated')} "
                      f"({alloc.get('coverage_note', '')})")

        if result.get("is_dicey_case"):
            print(f"    ⚠ DICEY CASE: {result.get('dicey_tradeoff')}")
            opt_b = result.get("option_b", [])
            print(f"    Option B orders: {len(opt_b) if opt_b else 0}")

        alt = result.get("alternative_drug_assessment")
        if alt:
            print(f"    Alternative: {alt}")

        for c in result.get("caveats", []):
            print(f"    Caveat: {c}")

        print(f"    viable={result.get('procurement_viable')} "
              f"parse_ok={result.get('parse_ok')} "
              f"calls={result.get('call_count')}")
        print("==============================================================")
        print("")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from sentinel import process_disruption
    from analyst import analyse
    from prediction_engine import SESSION

    # ── Test case selection ────────────────────────────────────────────────────
    # D017 Asthalin (A012 Supply Chain Failure, Jan — winter peak):
    #   Total loss, Life-Critical, 10 hospitals all actionable.
    #   Multiple distributors with real tradeoffs:
    #     S009: fastest (5d) but only 50 units stock — below min-order
    #     S001: large stock (943) but below min-order (1000)
    #     S010: meets min-order (1298 stock, 1000 min) but slow (11d)
    #   Shared API risk on its only alternative (Ventorlin).
    #   Hard case — genuine dicey situation likely.
    #
    # D004 Amoxil (Cipla Disaster, Aug — monsoon peak):
    #   Partial loss (Lupin 28% remains). H008 (5.9d), H004 (7d),
    #   H001 (9.6d) need bridge orders. Multiple feasible distributors.
    #   Tests two-call path with real choices.
    #
    # Comparison test: D017 Asthalin forced through two-call path
    # to compare reasoning quality vs single-call.

    # ── TEST 1: Single-call — Asthalin D017 (hard case) ──────────────────────
    '''print("\n" + "="*62)
    print("TEST 1: Single-call — D017 Asthalin")
    print("  A012 Supply Chain Failure / High / January (winter peak)")
    print("  Life-Critical | 10 hospitals | shared API risk on alternative")
    print("="*62)

    event1  = process_disruption(
        "API", "A012", "Supply Chain Failure", "High", "2024-01-15")
    pkgs1   = analyse(event1, verbose=False)
    d017_p1 = next((p for p in pkgs1 if p.drug_id == "D017"), None)

    if d017_p1:
        r1 = run_procurement_agent(d017_p1)
        print(f"\n  viable={r1.get('procurement_viable')} "
              f"dicey={r1.get('is_dicey_case')} "
              f"calls={r1.get('call_count')}")
        for o in r1.get("option_a", []):
            print(f"  Order: {o.get('distributor_name')} "
                  f"qty={o.get('total_quantity')}")
            for alloc in o.get("hospital_allocations", []):
                print(f"    {alloc.get('hospital_id')}: "
                      f"need={alloc.get('units_required')} "
                      f"get={alloc.get('units_allocated')} "
                      f"({alloc.get('coverage_note','')})")
        if r1.get("is_dicey_case"):
            print(f"  DICEY: {r1.get('dicey_tradeoff')}")
            if r1.get("option_b"):
                print(f"  Option B: {r1['option_b']}")
        print(f"  Alternative: {r1.get('alternative_drug_assessment')}")
        for c in r1.get("caveats", []):
            print(f"  Caveat: {c}")
    SESSION.reset()'''

    # ── TEST 2: Two-call — Amoxil D004 (partial loss) ─────────────────────────
    print("\n" + "="*62)
    print("TEST 2: Two-call — D004 Amoxil")
    print("  Cipla Disaster / High / August | Partial loss (Lupin 28% remains)")
    print("  H008 (5.9d), H004 (7d), H001 (9.6d) need bridge orders")
    print("="*62)

    event2  = process_disruption(
        "Factory", "F002", "Disaster", "High", "2024-08-15")
    pkgs2   = analyse(event2, verbose=False)
    d004_p2 = next((p for p in pkgs2 if p.drug_id == "D004"), None)

    if d004_p2:
        r2 = run_procurement_agent(d004_p2)
        print(f"\n  viable={r2.get('procurement_viable')} "
              f"calls={r2.get('call_count')}")
        print(f"  Summary: {r2.get('recommendation_summary','')[:120]}...")
        bridge = r2.get("bridge_order", {}) or {}
        if "total_bridge_cost_estimate" in bridge:
            print(f"  Est. Cost: Rs {bridge.get('total_bridge_cost_estimate'):,}")

        alloc = r2.get("allocation", {})
        print(f"\n  [Call 1] Factory covers demand: {alloc.get('factory_covers_demand')}")
        print(f"  Gap hospitals: {len(alloc.get('hospitals_needing_bridge',[]))}")
        for h in alloc.get("hospitals_needing_bridge", []):
            print(f"    {h.get('hospital_id')} — "
                  f"gap={h.get('gap_days')}d  bridge={h.get('bridge_units_needed')} units")

        print(f"\n  ── Option A ({len(r2.get('option_a',[]))} order(s)) ──")
        for o in r2.get("option_a", []):
            print(f"    [{o.get('distributor_id')}] {o.get('distributor_name')} "
                  f"| qty={o.get('total_quantity')} | Rs {o.get('price_per_unit')}/unit")
            for ah in o.get("hospital_allocations", []):
                print(f"      {ah.get('hospital_id')}: "
                      f"need={ah.get('units_required')} "
                      f"get={ah.get('units_allocated')} ({ah.get('coverage_note','')})")
            print(f"      Rationale: {o.get('rationale','')}")

        if r2.get("is_dicey_case"):
            print(f"\n  ⚠ DICEY: {r2.get('dicey_tradeoff')}")
            opt_b = r2.get("option_b") or []
            if opt_b:
                print(f"  ── Option B ({len(opt_b)} order(s)) ──")
                for o in opt_b:
                    print(f"    [{o.get('distributor_id')}] {o.get('distributor_name')} "
                          f"| qty={o.get('total_quantity')} | Rs {o.get('price_per_unit')}/unit")
                    for ah in o.get("hospital_allocations", []):
                        print(f"      {ah.get('hospital_id')}: "
                              f"need={ah.get('units_required')} "
                              f"get={ah.get('units_allocated')} ({ah.get('coverage_note','')})")
                    print(f"      Rationale: {o.get('rationale','')}")

        unserv = bridge.get("hospitals_unserviceable", [])
        if unserv:
            print(f"\n  ❌ UNSERVICEABLE HOSPITALS: {', '.join(unserv)}")

        print()
        for c in r2.get("caveats", []):
            print(f"  ⚡ {c}")

    SESSION.reset()

    # ── TEST 3: Comparison — D017 Asthalin through two-call path ──────────────
    '''print("\n" + "="*62)
    print("TEST 3: COMPARISON — D017 Asthalin forced through TWO-CALL path")
    print("  Same drug/disruption as Test 1 — compare reasoning depth")
    print("="*62)

    event3  = process_disruption(
        "API", "A012", "Supply Chain Failure", "High", "2024-01-15")
    pkgs3   = analyse(event3, verbose=False)
    d017_p3 = next((p for p in pkgs3 if p.drug_id == "D017"), None)

    if d017_p3:
        # Force partial-loss path by making remaining supply non-zero
        orig_remaining = d017_p3.drug_units_remaining
        orig_loss      = d017_p3.supply_loss_pct
        d017_p3.drug_units_remaining = 1.0
        d017_p3.supply_loss_pct      = 0.99
        print("  [Forced to two-call path for comparison]")

        r3 = run_procurement_agent(d017_p3)

        # Restore
        d017_p3.drug_units_remaining = orig_remaining
        d017_p3.supply_loss_pct      = orig_loss

        print(f"\n  viable={r3.get('procurement_viable')} "
              f"calls={r3.get('call_count')}")
        alloc3  = r3.get("allocation", {})
        print(f"  Gap hospitals: "
              f"{len(alloc3.get('hospitals_needing_bridge',[]))}")
        for o in r3.get("option_a", []):
            for alloc_h in o.get("hospital_allocations", []):
                print(f"  Bridge → {alloc_h.get('hospital_id')}: "
                      f"need={alloc_h.get('units_required')} "
                      f"get={alloc_h.get('units_allocated')}")
        if r3.get("is_dicey_case"):
            print(f"  DICEY: {r3.get('dicey_tradeoff')}")

        print("\n  ── COMPARISON ──")
        print(f"  Test 1 (single): "
              f"{len(r1.get('option_a',[]))} order(s) | "
              f"dicey={r1.get('is_dicey_case')}")
        print(f"  Test 3 (two):    "
              f"{len(r3.get('option_a',[]))} order(s) | "
              f"dicey={r3.get('is_dicey_case')}")
        print("  Review full output above to compare reasoning depth.")'''

    SESSION.reset()