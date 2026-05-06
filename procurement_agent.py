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

import re
import json
import threading
from typing import Optional
from google import genai
from google.genai import types

from analyst import DrugAlertPackage, HospitalRisk

# ── CONFIG ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyBcEeu5EgjkmYpDVy2ej1HUijEu8BdmlrE"
GEMINI_MODEL   = "gemma-4-26b-a4b-it"   # ← change model name here if needed

# Set False to force all drugs through the LLM (useful for testing parallel calls).
# Set True (default) to skip the LLM for trivially small gaps (gap < all MOQs).
MICRO_GAP_FAST_PATH = False
# ──────────────────────────────────────────────────────────────────────────────



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




def _hospitals_with_distributors_block(
    pkg:          DrugAlertPackage,
    hospital_ids: Optional[list] = None,
    bridge_map:   Optional[dict] = None,   # {hospital_id: bridge_units_needed}
) -> str:
    """
    Per-hospital section interleaved with that hospital's distributor options.
    Lean format — names kept for ID disambiguation, noise (price/reliability/city)
    stripped to reduce token bloat and focus LLM attention on allocation math.
    BELOW MIN ORDER shown as advisory — not a hard disqualification.
    """
    if hospital_ids is None:
        target_hids = [h.hospital_id for h in pkg.hospitals if h.requires_action]
    else:
        target_hids = hospital_ids

    # Build distributor meta lookup — only what the LLM needs
    dist_meta = {}
    for h in pkg.hospitals:
        for d in h.distributors:
            did = d["distributor_id"]
            if did not in dist_meta:
                dist_meta[did] = {
                    "name":          d["name"],
                    "current_stock": d["current_stock"],
                    "min_order":     d["min_order"],
                }

    # Global stock pool — gives LLM a reference starting point for scratchpad tracking
    pool_parts = []
    seen_dids  = set()
    for h in pkg.hospitals:
        if h.hospital_id not in target_hids:
            continue
        for d in h.distributors:
            did = d["distributor_id"]
            if did not in seen_dids:
                meta = dist_meta[did]
                pool_parts.append(f"{did}={meta['current_stock']:,}")
                seen_dids.add(did)

    lines = [
        "HOSPITALS AND DISTRIBUTOR OPTIONS",
        "(BELOW MIN ORDER = negotiation may be required — do not auto-disqualify.)",
        f"GLOBAL STOCK POOL: {' | '.join(pool_parts)}",
    ]

    for h in pkg.hospitals:
        if h.hospital_id not in target_hids:
            continue

        bridge_need = (bridge_map or {}).get(h.hospital_id, 0)
        lines.append(
            f"\n── {h.hospital_id} {h.hospital_name}"
            f" | Stockout: {h.days_until_stockout:.1f}d"
            f" | Bridge: {bridge_need:,} units ──"
        )

        if not h.distributors:
            lines.append("   No distributors available.")
            continue

        sorted_dists = sorted(h.distributors, key=lambda d: d["delivery_days"])
        for d in sorted_dists:
            did      = d["distributor_id"]
            meta     = dist_meta.get(did, {})
            delivery = d["delivery_days"]
            stockout = h.days_until_stockout

            timing   = "IN TIME      " if delivery < stockout else "AFTER STOCKOUT"
            min_ord  = meta.get("min_order", 0)
            stock    = meta.get("current_stock", 0)
            min_flag = "(BELOW MIN)" if stock < min_ord else "(MEETS MIN)"

            lines.append(
                f"  [{timing}] {did} {meta.get('name', did)}"
                f" | Stock: {stock:,}"
                f" | Min Order: {min_ord:,} {min_flag}"
            )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# PYTHON EXECUTION ENGINE — computes all numbers from LLM assignments
# ══════════════════════════════════════════════════════════════════════════════

def _execute_order(
    assignments:     dict,           # {hospital_id: str | list[str]} from LLM
    pkg:             DrugAlertPackage,
    demand_override: dict = None,    # {hospital_id: units_needed} — for bridge orders
    unit_caps:       dict = None,    # {hospital_id: {distributor_id: int}} — from scratchpad extraction
) -> list:
    """
    Converts LLM hospital→distributor assignment(s) into a fully-computed order list.
    Supports both legacy single-string and new multi-fill list assignment formats.
    Global stock pool depletes in hospital urgency order (earliest stockout first).
    Exact-change rule: only draws what's needed, leaves the rest for later hospitals.
    If unit_caps is provided, uses those exact amounts instead of greedy allocation
    (honours the LLM's Universal Coverage capping decisions from the scratchpad).
    """
    if not assignments:
        return []

    # Build distributor meta + per-(hospital, distributor) delivery days
    dist_meta     = {}   # distributor_id → {name, city, current_stock, min_order, price}
    hosp_delivery = {}   # (hospital_id, distributor_id) → delivery_days
    for h in pkg.hospitals:
        for d in h.distributors:
            did = d["distributor_id"]
            hosp_delivery[(h.hospital_id, did)] = d["delivery_days"]
            if did not in dist_meta:
                dist_meta[did] = {
                    "name":          d["name"],
                    "city":          d["city"],
                    "current_stock": d["current_stock"],
                    "min_order":     d["min_order"],
                    "price_per_unit":d["price_per_unit"],
                }

    # Hospital demand, urgency and name lookups
    hosp_demand   = {}
    hosp_stockout = {}
    hosp_name     = {}
    for h in pkg.hospitals:
        hosp_name[h.hospital_id]     = h.hospital_name
        hosp_stockout[h.hospital_id] = h.days_until_stockout
        if demand_override and h.hospital_id in demand_override:
            hosp_demand[h.hospital_id] = demand_override[h.hospital_id]
        else:
            hosp_demand[h.hospital_id] = h.prophet_forecast_30d

    # Normalise: values can be a string (legacy) or list (multi-fill)
    normalised = {}
    for hid, val in assignments.items():
        if isinstance(val, list):
            normalised[hid] = [v for v in val if v]
        elif val:
            normalised[hid] = [val]
        else:
            normalised[hid] = []

    # Global shared stock pool — depleted across all hospitals
    remaining = {did: dist_meta[did]["current_stock"] for did in dist_meta}

    # Process hospitals in urgency order (earliest stockout first)
    sorted_hids = sorted(normalised.keys(), key=lambda hid: hosp_stockout.get(hid, 9999))

    # Accumulate per-distributor order records
    dist_orders = {}   # distributor_id → order_record

    for hid in sorted_hids:
        dist_list      = normalised[hid]
        units_needed   = round(hosp_demand.get(hid, 0))
        remaining_need = units_needed

        for did in dist_list:
            if remaining_need <= 0:
                break
            if did not in dist_meta:
                continue
            avail = remaining.get(did, 0)
            if avail <= 0:
                continue

            meta           = dist_meta[did]
            # Use scratchpad-extracted cap if available, else greedy exact-change
            if unit_caps and hid in unit_caps and did in unit_caps[hid]:
                units_from_did = min(unit_caps[hid][did], avail)   # cap from LLM scratchpad
            else:
                units_from_did = min(remaining_need, avail)         # greedy exact-change
            remaining[did] -= units_from_did
            remaining_need -= units_from_did

            delivery = hosp_delivery.get((hid, did))
            stockout = hosp_stockout.get(hid, 0)
            gap_days = round(max(0.0, (delivery or 0) - stockout), 1)

            # Min-order advisory for this specific allocation
            min_order     = meta.get("min_order", 0)
            current_stock = meta.get("current_stock", 0)
            if current_stock < min_order:
                dist_caveat = (
                    f"BELOW MIN ORDER: stock {current_stock:,} "
                    f"< min {min_order:,} — negotiation required"
                )
            elif units_from_did < min_order:
                dist_caveat = (
                    f"BELOW MIN ORDER: ordered {units_from_did:,} "
                    f"< min {min_order:,} — negotiation required"
                )
            else:
                dist_caveat = None

            coverage_note = (
                "ok" if remaining_need == 0
                else f"shortfall {remaining_need:,}"
            )

            alloc = {
                "hospital_id":    hid,
                "hospital_name":  hosp_name.get(hid, hid),
                "delivery_days":  delivery,
                "gap_days":       gap_days,
                "units_required": units_needed,
                "units_allocated":units_from_did,
                "coverage_note":  coverage_note,
            }

            if did not in dist_orders:
                dist_orders[did] = {
                    "distributor_id":       did,
                    "distributor_name":     f"{meta['name']} ({meta['city']})",
                    "total_quantity":       0,
                    "price_per_unit":       meta["price_per_unit"],
                    "hospital_allocations": [],
                }
            if dist_caveat and "distributor_caveat" not in dist_orders[did]:
                dist_orders[did]["distributor_caveat"] = dist_caveat

            dist_orders[did]["total_quantity"]       += units_from_did
            dist_orders[did]["hospital_allocations"].append(alloc)

    # Any hospital that was assigned but got 0 (stock exhausted) still needs a row
    allocated_hids = {
        alloc["hospital_id"]
        for order in dist_orders.values()
        for alloc in order["hospital_allocations"]
    }
    for hid in sorted_hids:
        if hid in allocated_hids:
            continue
        # Use first valid distributor in assignment list
        first_did = next((d for d in normalised[hid] if d in dist_meta), None)
        if not first_did:
            continue
        meta     = dist_meta[first_did]
        delivery = hosp_delivery.get((hid, first_did))
        stockout = hosp_stockout.get(hid, 0)
        gap_days = round(max(0.0, (delivery or 0) - stockout), 1)
        need     = round(hosp_demand.get(hid, 0))
        if first_did not in dist_orders:
            dist_orders[first_did] = {
                "distributor_id":       first_did,
                "distributor_name":     f"{meta['name']} ({meta['city']})",
                "total_quantity":       0,
                "price_per_unit":       meta["price_per_unit"],
                "hospital_allocations": [],
            }
        dist_orders[first_did]["hospital_allocations"].append({
            "hospital_id":    hid,
            "hospital_name":  hosp_name.get(hid, hid),
            "delivery_days":  delivery,
            "gap_days":       gap_days,
            "units_required": need,
            "units_allocated": 0,
            "coverage_note":  f"shortfall {need:,} (stock exhausted)",
        })

    return list(dist_orders.values())



# ── New prompt builders (migrated from test_prompt_comparison.py) ─────────────


def _build_task(gap_hospitals: list) -> str:
    """Task section for the bridge order LLM call — multi-fill, dual-strategy."""
    sorted_hids  = [h["hospital_id"] for h in sorted(gap_hospitals, key=lambda h: h["days_until_stockout"])]
    total_needed = sum(h["bridge_units_needed"] for h in gap_hospitals)
    return (
        f"TASK — BRIDGE ORDER DECISION\n"
        f"Total bridge units needed: {total_needed:,} across {len(gap_hospitals)} hospitals.\n\n"
        f"Process hospitals in this urgency order (most urgent first): {', '.join(sorted_hids)}\n\n"
        f"STRATEGY DEFINITIONS (CRITICAL):\n"
        f"  - NO SHORTAGE (Abundant Supply): If Total Global Supply >= Total Demand, Option A and Option B will be IDENTICAL.\n"
        f"    Give every hospital 100% of their need. Do not reduce allocations. is_dicey_case is false.\n"
        f"  - OPTION A (Universal Coverage): If demand exceeds supply, do NOT let the bottom hospitals starve. "
        f"    Manually reduce the units assigned to top-priority hospitals to leave a 'Survival Reserve' in the global pool. "
        f"    Every hospital MUST receive at least a partial allocation.\n"
        f"  - OPTION B (Ruthless Triage): Process strictly top-down. Give top-priority hospitals 100% of their need "
        f"    until the global pool hits 0. Let the bottom hospitals starve if necessary.\n\n"
        f"STOCK RULE: Each distributor has ONE global stock pool. When you assign units to a hospital, "
        f"subtract them from the global pool. You can assign a distributor's remaining stock to a second hospital "
        f"even if it only partially covers the second hospital's need.\n\n"
        f"For each hospital (process strictly in the order above):\n"
        f"  1. Start with ARRIVES IN TIME distributors. If one has enough stock to fully cover the need, assign it.\n"
        f"  2. MULTI-FILL: If an ARRIVES IN TIME distributor's stock is too small (e.g. S009 with 146 units "
        f"     for a hospital needing thousands), assign it to bridge the immediate gap, AND ALSO assign an "
        f"     ARRIVES AFTER STOCKOUT distributor to cover the rest of the volume.\n"
        f"  3. If no in-time options exist, assign the ARRIVES AFTER STOCKOUT distributor with the most stock.\n"
        f"  4. Flag BELOW MIN ORDER situations in caveats — do not disqualify for this alone.\n\n"
        f"MATH RULES FOR MULTI-FILL:\n"
        f"  - Stop Condition: Stop adding distributors to a hospital the moment its cumulative assigned units >= its target (or full need).\n"
        f"  - Exact Change: Only draw exactly what you need to fill the gap. If a distributor has 5,000 units "
        f"    and you only need 1,000, subtract 1,000 and explicitly leave 4,000 in the global pool for other hospitals.\n"
        f"  - Minimum Order Check: Compare your 'exact change' assigned units to the distributor's minimum order requirement. "
        f"    If the assigned amount is less than the minimum order, you MUST flag it in the 'caveats' array.\n\n"
        f"Respond with valid JSON only. You MUST show your step-by-step math for Option A in the 'option_a_scratchpad' FIRST:\n"
        f"{{\n"
        f'  "option_a_scratchpad": {{"<HOSPITAL ID>": "<If shortage, state reduced target to save reserve. Explain Exact Change, Min Order check, and updated global stock here>"}},\n'
        f'  "bridge_order_summary": "<2-3 sentences>",\n'
        f'  "is_dicey_case": true or false,\n'
        f'  "dicey_tradeoff": "<description of the tradeoff, or \"N/A - Sufficient supply\" if is_dicey_case is false>",\n'
        f'  "option_a_assignments": {{"<HOSPITAL ID>":["<DISTRIBUTOR ID>", ...]}},\n'
        f'  "option_a_strategy": "<If shortage, output \'Universal Coverage: Reduced top allocations\'. If no shortage, output \'Abundant Supply: Fulfilled 100%\'>",\n'        f'  "option_b_scratchpad": {{"<HOSPITAL ID>": "<Explain Exact Change, Min Order check, and updated global stock here. Reduce targets ONLY if there is a shortage.>"}},\n'
        f'  "option_b_assignments": {{"<HOSPITAL ID>":["<DISTRIBUTOR ID>", ...]}},\n'
        f'  "option_b_strategy": "<If shortage, output \'Ruthless Triage: Guaranteed 100% for top urgency\'. If no shortage, output \'Abundant Supply: Fulfilled 100%\'>",\n'        f'  "hospitals_unserviceable": [],\n'
        f'  "caveats": ["<caveat>"]\n'
        f"}}\n"
    )


def _build_bridge_prompt(pkg: DrugAlertPackage, gap_hospitals: list) -> str:
    """Assembles the full bridge-order LLM prompt from context blocks + task."""
    gap_hids   = [h["hospital_id"] for h in gap_hospitals]
    bridge_map = {h["hospital_id"]: h["bridge_units_needed"] for h in gap_hospitals}
    gap_lines  = [
        f"  {h['hospital_id']} ({h.get('hospital_name', '')}) | "
        f"Stockout in {h['days_until_stockout']}d | Bridge needed: {h['bridge_units_needed']:,} units"
        for h in gap_hospitals
    ]
    gap_section = "HOSPITALS NEEDING BRIDGE ORDER (Python-computed)\n" + "\n".join(gap_lines)
    return "\n\n".join([
        _disruption_block(pkg),
        _drug_block(pkg),
        gap_section,
        _hospitals_with_distributors_block(pkg, hospital_ids=gap_hids, bridge_map=bridge_map),
        _build_task(gap_hospitals),
    ])


# ══════════════════════════════════════════════════════════════════════════════
# LLM CALL + PARSER
# ══════════════════════════════════════════════════════════════════════════════

_thread_local = threading.local()

def _get_client() -> genai.Client:
    if not hasattr(_thread_local, 'client'):
        _thread_local.client = genai.Client(api_key=GEMINI_API_KEY)
    return _thread_local.client


def _call_gemini(prompt: str, max_tokens: int = 2048) -> str:
    import sys, pathlib
    from datetime import datetime

    _client = _get_client()   # one client per worker thread, reused across batches

    thread_name = threading.current_thread().name
    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=max_tokens,
                system_instruction=SYSTEM_INSTRUCTION,
            ),
        )
        return response.text or ""

    except Exception as e:
        ts  = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"[{ts}] [{thread_name}] _call_gemini ERROR\n"
            f"  Type   : {type(e).__name__}\n"
            f"  Message: {e}\n"
        )
        print(msg, file=sys.stderr, flush=True)
        log_path = pathlib.Path("raw_output/api_errors.log")
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as _lf:
            _lf.write(msg + "\n")
        raise



def _strip_scratchpads(text: str) -> str:
    """Remove option_a_scratchpad / option_b_scratchpad using brace-matched extraction."""
    result = text
    for field in ("option_a_scratchpad", "option_b_scratchpad"):
        m = re.search(f'"{field}"\\s*:', result)
        if not m:
            continue
        start = m.end()
        while start < len(result) and result[start] in ' \t\n\r':
            start += 1
        if start >= len(result) or result[start] != '{':
            continue
        depth, in_str, esc = 0, False, False
        i = start
        while i < len(result):
            c = result[i]
            if esc:
                esc = False
            elif c == '\\' and in_str:
                esc = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == '{':   depth += 1
                elif c == '}': depth -= 1
                if depth == 0:
                    end = i + 1
                    while end < len(result) and result[end] in ' \t':
                        end += 1
                    if end < len(result) and result[end] == ',':
                        end += 1
                    result = result[:m.start()] + result[end:]
                    break
            i += 1
    return result


def _parse_json(raw: str) -> tuple[dict, bool, str]:
    """Returns (parsed_dict, success, error_msg). Never raises."""
    if raw is None or not raw.strip():
        return {}, False, "LLM returned empty or None response"
    clean = raw.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()
    # First attempt: parse as-is
    try:
        return json.loads(clean), True, ""
    except (json.JSONDecodeError, IndexError):
        pass
    # Fallback: strip scratchpad fields (inline corrections can break json.loads)
    try:
        stripped = _strip_scratchpads(clean)
        result   = json.loads(stripped)
        result["_scratchpad_stripped"] = True
        return result, True, "scratchpad stripped"
    except (json.JSONDecodeError, IndexError) as e:
        return {}, False, str(e)


def _parse_error_result(
    drug_id:   str,
    drug_name: str,
    error_msg: str,
    raw_text:  str,
    call_num:  int,
) -> dict:
    return {
        "drug_id":                drug_id,
        "drug_name":              drug_name,
        "parse_ok":               False,
        f"raw_response_call{call_num}": raw_text,
        "parse_error":            error_msg,
        "recommendation_summary": f"Call {call_num} parse failed — see raw_response.",
        "hospital_priority_order":[],
        "option_a":               [],
        "option_b":               None,
        "is_dicey_case":          False,
        "caveats":                ["LLM response could not be parsed as JSON."],
        "procurement_viable":     False,
        "call_count":             call_num,
    }


# ══════════════════════════════════════════════════════════════════════════════
# RESULT PRINTER  (standalone — reads from result dict, safe to call anywhere)
# ══════════════════════════════════════════════════════════════════════════════

def print_procurement_result(result: dict) -> None:
    """Pretty-print the full Option A / Option B tables from a result dict.

    Reads entirely from the result dict — no agent context needed.
    Safe to call from the aggregator after future.result().
    """
    COL = [8, 12, 10, 8, 8, 6, 10, 9]
    HDR = ["Hospital", "Distributor", "Full Need", "Remainin", "Given", "Gap d", "Coverage", "Min Ord?"]
    W   = sum(COL) + len(COL) * 5 + 1

    def _hdr(title):
        pad = max(0, W - len(title) - 2)
        print(f"\n  +{'-'*(W+2)}+")
        print(f"  | {title}{' '*pad} |")
        print(f"  +{'-'*(W+2)}+")

    def _row(*cols):
        parts = [str(c)[:w].ljust(w) for c, w in zip(cols, COL)]
        print("  | " + " | ".join(parts) + " |")

    def _div():
        print(f"  +{'-'*(W+2)}+")

    def _print_table(label, orders_list, strategy=""):
        if not orders_list:
            return
        title = f"{label}  --  {strategy}" if strategy else label
        _hdr(title)
        _row(*HDR)
        _div()

        cumulative = {}
        raw_allocs = []
        for order in orders_list:
            cav = order.get("distributor_caveat", "")
            did = order.get("distributor_id", "?")
            for alloc in order.get("hospital_allocations", []):
                raw_allocs.append((alloc, did, cav))

        seen_hids, allocs_by_hid = [], {}
        for item in raw_allocs:
            hid = item[0].get("hospital_id", "?")
            if hid not in allocs_by_hid:
                allocs_by_hid[hid] = []
                seen_hids.append(hid)
            allocs_by_hid[hid].append(item)

        all_allocs = []
        for hid in seen_hids:
            all_allocs.extend(allocs_by_hid[hid])

        prev_hid = None
        for alloc, did, cav in all_allocs:
            hid = alloc.get("hospital_id", "?")
            if prev_hid is not None and hid != prev_hid:
                print("  |" + " " * (W + 2) + "|")
            prev_hid = hid

            full_need        = alloc.get("units_required", 0)
            given_now        = alloc.get("units_allocated", 0)
            prev_given       = cumulative.get(hid, 0)
            remaining_before = max(0, full_need - prev_given)
            cumulative[hid]  = prev_given + given_now
            remaining_after  = max(0, full_need - cumulative[hid])

            gap  = f"{alloc.get('gap_days', 0):.1f}d"
            cov  = "ok" if remaining_after == 0 else f"-{remaining_after:,}"
            mord = "NEG" if cav else "ok"

            _row(hid, did, f"{full_need:,}", f"{remaining_before:,}",
                 f"{given_now:,}", gap, cov, mord)
        _div()

    # ── Summary line ──
    bridge     = result.get("bridge_order", {})
    gap_count  = len(result.get("allocation", {}).get("hospitals_needing_bridge", []))
    print(f"\n  Drug : {result.get('drug_name')} ({result.get('drug_id')})")
    print(f"  Gap hospitals : {gap_count}"
          f"  |  Stock gap : {result.get('total_stock_gap', 0):,} units"
          f"  |  Parse ok : {result.get('parse_ok')}"
          f"  |  Viable : {result.get('procurement_viable')}")
    if result.get("_scratchpad_stripped"):
        print("  [!] Scratchpad stripped during JSON parse — assignments still valid.")

    # ── Option A ──
    strat_a = bridge.get("option_a_strategy", "Option A")
    _print_table("OPTION A", result.get("option_a", []), strat_a)

    # ── Option B (dicey) ──
    if result.get("is_dicey_case"):
        print(f"\n  [!] DICEY CASE:")
        tradeoff = result.get("dicey_tradeoff", "")
        for line in (tradeoff or "").split(". "):
            if line.strip():
                print(f"      {line.strip()}.")
        strat_b = bridge.get("option_b_strategy", "Option B")
        _print_table("OPTION B", result.get("option_b") or [], strat_b)
    else:
        print("\n  [ok] Not a dicey case — single recommendation.")

    # ── Caveats ──
    caveats = result.get("caveats", [])
    if caveats:
        print(f"\n  {'─'*70}")
        print("  CAVEATS:")
        for c in caveats:
            print(f"    [!] {c}")
        print(f"  {'─'*70}")
    print()


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
        print(f"\n  [Procurement Agent] {pkg.drug_name} ({pkg.drug_id})")

    result = {"drug_id": pkg.drug_id, "drug_name": pkg.drug_name}

    # ── Two-call path (all scenarios) ─────────────────────────────────────────
    # ── Python triage (replaces LLM Call 1) ──────────────────────────────────
    # needs_bridge = True when the hospital runs out BEFORE the factory recovers.
    # All arithmetic was already pre-computed in the old Call 1 prompt — the LLM
    # was just reading the labels back. Python does it directly here.
    covers             = pkg.drug_units_remaining >= pkg.system_total_forecast
    factory_covers_str = "COVERS" if covers else "DOES NOT COVER"

    gap_hospitals  = []
    covered_hids   = []

    for h in pkg.hospitals:
        if not h.requires_action:
            continue
        needs_bridge = h.days_until_stockout < pkg.recovery_days
        if needs_bridge:
            exposed_days = max(0.0, pkg.recovery_days - h.days_until_stockout)
            bridge_units = round((h.prophet_forecast_30d / 30.0) * exposed_days)
            gap_hospitals.append({
                "hospital_id":         h.hospital_id,
                "hospital_name":       h.hospital_name,
                "days_until_stockout": round(h.days_until_stockout, 1),
                "bridge_units_needed": bridge_units,
            })
        else:
            covered_hids.append(h.hospital_id)

    # Build allocation dict
    gap_summary = ", ".join(
        f"{h['hospital_id']} ({h['days_until_stockout']}d)" for h in gap_hospitals
    )
    allocation = {
        "factory_covers_demand":        covers,
        "hospitals_needing_bridge":     [h["hospital_id"] for h in gap_hospitals],
        "hospitals_covered_by_factory": covered_hids,
        "allocation_summary": (
            f"Factory supply {factory_covers_str} total 30-day system demand. "
            f"{len(gap_hospitals)} hospital(s) require emergency bridge orders: "
            f"{gap_summary or 'none'}."
        ),
        "allocation_note": (
            f"Bridge required for {len(gap_hospitals)} hospital(s). "
            f"Factory supply {factory_covers_str.lower()} total demand."
        ),
    }

    result["allocation"] = allocation

    if verbose:
        print(f"    [Python triage] Gap hospitals: {len(gap_hospitals)}")
        print(f"    Factory covers demand: {covers}")
        for h in gap_hospitals:
            print(f"      BRIDGE \u2192 {h['hospital_id']} ({h['hospital_name']}) "
                  f"| stockout {h['days_until_stockout']}d "
                  f"| bridge units: {h['bridge_units_needed']}")
        if covered_hids:
            print(f"    Covered by factory: {covered_hids}")

    # No bridge needed \u2014 factory covers all hospitals through normal channels
    if not gap_hospitals:
        result.update({
            "parse_ok":               True,
            "call_count":             0,
            "bridge_order":           None,
            "recommendation_summary": allocation["allocation_summary"],
            "is_dicey_case":          False,
            "option_a":               [],
            "option_b":               None,
            "caveats": [
                "No emergency bridge order required \u2014 "
                "factory supply covers all hospitals through normal channels."
            ],
            "procurement_viable": True,
        })
        if verbose:
            print("    No bridge needed.")
        return result

    # \u2500\u2500 Micro-gap fast-path \u2014 TEMPORARILY DISABLED for parallel LLM call testing \u2500
    # Re-enable: change `if False:` back to:
    #   if all_moqs and total_bridge < min(all_moqs):
    total_bridge = sum(h.get("bridge_units_needed", 0) for h in gap_hospitals)
    all_moqs = [
        d["min_order"]
        for h in pkg.hospitals
        for d in h.distributors
        if h.hospital_id in {g["hospital_id"] for g in gap_hospitals}
    ]
    if MICRO_GAP_FAST_PATH and all_moqs and total_bridge < min(all_moqs):
        print(f"  [FAST-PATH] {pkg.drug_name}: bridge={total_bridge} < min_moq={min(all_moqs)} — skipping LLM")
        fast_assignments = {}
        fast_caveats = [
            f"Micro-gap fast-path: total bridge needed ({total_bridge:,} units) "
            f"is below the minimum order quantity of every available distributor "
            f"(smallest MOQ = {min(all_moqs):,}). Negotiation required for all options."
        ]
        for gh in gap_hospitals:
            hid = gh["hospital_id"]
            h_obj = next((h for h in pkg.hospitals if h.hospital_id == hid), None)
            if not h_obj or not h_obj.distributors:
                continue
            in_time  = [d for d in h_obj.distributors if d["delivery_days"] < h_obj.days_until_stockout]
            fallback = h_obj.distributors
            pool = sorted(in_time or fallback, key=lambda d: -d["current_stock"])
            if pool:
                best = pool[0]
                fast_assignments[hid] = [best["distributor_id"]]
                fast_caveats.append(
                    f"{hid}: assigned {best['distributor_id']} ({best['name']}) \u2014 "
                    f"ordered {gh['bridge_units_needed']:,} < MOQ {best['min_order']:,} \u2014 negotiation required."
                )
        demand_override = {h["hospital_id"]: h.get("bridge_units_needed", 0) for h in gap_hospitals}
        fast_option_a = _execute_order(fast_assignments, pkg, demand_override=demand_override)
        result.update({
            "parse_ok":             True,
            "procurement_viable":  True,
            "is_dicey_case":       False,
            "bridge_order_summary": (
                f"Micro-gap auto-resolve: {total_bridge:,} units needed across "
                f"{len(gap_hospitals)} hospital(s). All distributor MOQs exceed this \u2014 negotiation required."
            ),
            "option_a":  fast_option_a,
            "option_b":  None,
            "caveats":   fast_caveats,
        })
        return result
    # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    # Bridge order — LLM call (only reached if gap >= smallest MOQ)
    if verbose:
        print(f"    Bridge order for {len(gap_hospitals)} hospital(s)...")
    raw2 = _call_gemini(_build_bridge_prompt(pkg, gap_hospitals), max_tokens=4096)

    # Save Call 1 raw response immediately — per-drug file under raw_output/
    import pathlib
    raw_out_dir = pathlib.Path("raw_output")
    raw_out_dir.mkdir(exist_ok=True)
    _raw_log = raw_out_dir / f"{pkg.drug_id}_{pkg.drug_name.replace(' ', '_')}_llm.txt"
    with open(_raw_log, "w", encoding="utf-8") as _fp:
        _fp.write("=== CALL 1: Bridge Order (scratchpad + assignments) ===\n\n")
        _fp.write(raw2)

    bridge, ok2, err2 = _parse_json(raw2)

    if not ok2:
        result.update(
            _parse_error_result(
                pkg.drug_id, pkg.drug_name, err2, raw2, 2))
        result["allocation"] = allocation
        return result

    # LLM gives assignments {hospital_id: distributor_id}
    # Python computes all numeric allocations using bridge_units_needed as demand
    demand_override = {
        h["hospital_id"]: h.get("bridge_units_needed", 0)
        for h in gap_hospitals
    }
    a_assign = bridge.get("option_a_assignments") or {}
    b_assign = bridge.get("option_b_assignments") or {}

    # ── Call 2: lightweight scratchpad extraction ─────────────────────────────
    # Read the scratchpad text and extract {hospital_id: {distributor_id: units}}
    # for both options, so Python can honour the LLM's capping decisions.
    a_scratchpad = bridge.get("option_a_scratchpad", {})
    b_scratchpad = bridge.get("option_b_scratchpad", {})
    a_caps, b_caps = {}, {}
    if a_scratchpad or b_scratchpad:
        extract_prompt = (
        "You are a strict data extraction parser. Your ONLY job is to convert the final stated "
        "allocations from the text into a single JSON object.\n\n"
        "RULES:\n"
        "1. IGNORE ALL MATH AND REASONING (e.g., 'Need x', 'Remaining y', 'Total available').\n"
        "2. If there is a 'REVISED' or 'Final:' summary at the end of a text block, use ONLY that summary. "
        "If there is no summary, extract the final assigned values directly from each hospital's explanation.\n"
        "3. Be careful to distinguish assigned units from remaining global stock (e.g., in 'distributor id (146)... distributor id: 0', the assignment is 146, and 0 is the leftover stock).\n"
        "4. Omit distributors if the assigned value is 0.\n"
        "5. Output absolutely nothing but valid JSON.\n\n"
        "EXPECTED FORMAT:\n"
        "{\n"
        '  "option_a": {"<HOSPITAL_ID>": {"<DISTRIBUTOR_ID>": <integer>, ...}},\n'
        '  "option_b": {"<HOSPITAL_ID>": {"<DISTRIBUTOR_ID>": <integer>, ...}}\n'
        "}\n\n"
        f"--- OPTION A TEXT ---\n{json.dumps(a_scratchpad)}\n\n"
        f"--- OPTION B TEXT ---\n{json.dumps(b_scratchpad)}"
        )
        if verbose:
            print("    [Extracting caps from scratchpad...]")
        raw_extract = _call_gemini(extract_prompt, max_tokens=1024)
        with open(_raw_log, "a", encoding="utf-8") as _fp:
            _fp.write("\n\n=== CALL 2: Scratchpad Extraction (caps) ===\n\n")
            _fp.write(raw_extract)
        extracted, ok_e, _ = _parse_json(raw_extract)
        if ok_e:
            a_caps = extracted.get("option_a", {})
            b_caps = extracted.get("option_b", {})
            if verbose:
                print(f"    [Caps extracted: A={len(a_caps)} hospitals, B={len(b_caps)} hospitals]")
        else:
            if verbose:
                print("    [Scratchpad extraction failed — falling back to greedy allocation]")

    option_a = _execute_order(a_assign, pkg, demand_override, unit_caps=a_caps or None)
    option_b = _execute_order(b_assign, pkg, demand_override, unit_caps=b_caps or None) if b_assign else None



    # Compute total_stock_gap
    total_required = sum(round(h["bridge_units_needed"]) for h in gap_hospitals)
    total_acquired = sum(
        alloc["units_allocated"]
        for order in option_a
        for alloc in order["hospital_allocations"]
    )
    total_stock_gap = max(0, total_required - total_acquired)

    all_caveats = list(bridge.get("caveats", []))
    for order in option_a:
        if "distributor_caveat" in order:
            all_caveats.append(f"{order['distributor_name']}: {order['distributor_caveat']}")
    for order in option_a:
        for alloc in order["hospital_allocations"]:
            gd = alloc.get("gap_days", 0)
            if gd > 0:
                all_caveats.append(
                    f"ZERO STOCK GAP - {alloc['hospital_name']} ({alloc['hospital_id']}): "
                    f"{gd}d without supply (delivery day {alloc.get('delivery_days', '?')})."
                )

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
        "option_a":       option_a,
        "option_b":       option_b,
        "total_stock_gap":        total_stock_gap,
        "procurement_viable": bool(a_assign),
        "caveats":        all_caveats,
    })

    # -- Verbose summary --
    if verbose:
        print_procurement_result(result)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# TEST RUNNER
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from sentinel import process_disruption
    from analyst import analyse
    from prediction_engine import SESSION

    print("\n" + "="*62)
    print("TEST: D004 Amoxil — Cipla Disaster / High / August")
    print("  Partial loss (Lupin 28% remains)")
    print("="*62)

    event = process_disruption("Factory", "F002", "Disaster", "High", "2024-08-15")
    pkgs  = analyse(event, verbose=False)
    d004  = next((p for p in pkgs if p.drug_id == "D004"), None)

    if d004:
        result = run_procurement_agent(d004, verbose=True)
        # Dump full structured result to JSON for inspection
        import pathlib
        out_path = pathlib.Path("procurement_result.json")
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(result, fp, indent=2, ensure_ascii=False, default=str)
        print(f"  Full result saved → {out_path.resolve()}")
    else:
        print("  D004 not found in analysis results.")

    SESSION.reset()

