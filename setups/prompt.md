 
1. File name - procurement_fully_llm.py
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

2. procurement_agent.py
     task = (
        f"TASK — BRIDGE ORDER DECISION\n"
        f"{len(gap_hospitals)} hospital(s) will run out before normal resupply arrives.\n"
        f"{allocation.get('allocation_note', '')}\n\n"
        "Reason through:\n"
        "  1. For EVERY gap hospital, assign the FASTEST available distributor\n"
        "     (lowest delivery_days), regardless of whether it arrives before or after stockout.\n"
        "     A late delivery is always better than no delivery for a hospital's recovery window.\n"
        "  2. Of distributors tied on speed, choose by: stock sufficiency for bridge_units_needed, then price.\n"
        "     If below min-order, flag for negotiation — still consider if best option.\n"
        "  3. HOSPITAL ALTERNATIVES: For a specific hospital, if the fastest distributor's\n"
        "     current_stock < bridge_units_needed AND a slower distributor has current_stock >= bridge_units_needed,\n"
        "     record the slower distributor in hospital_alternatives for that hospital only.\n"
        "     Use tradeoff_type: 'speed_vs_stock'.\n"
        "     Do NOT add an alternative if the fastest distributor fully covers bridge_units_needed.\n"
        "  4. DICEY CASE (whole-plan level only): if two entirely different assignment strategies\n"
        "     are genuinely close in merit for the system as a whole, set is_dicey_case=true\n"
        "     and provide option_b_assignments. Do not use this for single-hospital choices\n"
        "     (use hospital_alternatives for those instead).\n"
        "  5. Only put a hospital in hospitals_unserviceable if it has ZERO distributors listed above.\n"
        "     A distributor that arrives late is NOT unserviceable — assign it anyway.\n\n"
        "Respond with valid JSON only:\n"
        "{\n"
        '  "bridge_order_summary": "<2-3 sentences>",\n'
        '  "is_dicey_case": false,\n'
        '  "dicey_tradeoff": null,\n'
        '  "option_a_assignments": {"<hospital_id_1>": "<distributor_id>", "<hospital_id_2>": "<distributor_id>"},\n'
        '  "option_a_strategy": "<one sentence>",\n'
        '  "option_b_assignments": null,\n'
        '  "option_b_strategy": null,\n'
        '  "hospital_alternatives": {\n'
        '    "<hospital_id>": {\n'
        '      "alternative_distributor_id": "<distributor_id>",\n'
        '      "tradeoff_type": "speed_vs_stock",\n'
        '      "tradeoff": "<one sentence — e.g. arrives Xd later but fully covers Y units>"\n'
        '    }\n'
        '  },\n'
        '  "hospitals_unserviceable": [],\n'
        '  "caveats": ["<caveat 1>"],\n'
        '  "bridge_viable": true\n'
        "}\n\n"
        "If is_dicey_case is true: populate option_b_assignments with an alternative\n"
        "hospital→distributor mapping, set option_b_strategy and dicey_tradeoff.\n"
        "hospital_alternatives must be an empty object {} if no per-hospital alternatives exist."
    )
   
3. first iteration of the prompt combined call 1 and 2 whih we dont want but prompt for reference:
    task = (
        f"TASK — BRIDGE ORDER DECISION\n"
        f"{len(gap_hospitals)} hospital(s) will run out before the fastest\n"
        f"distributor can reach them. {allocation.get('allocation_note', '')}\n\n"
        "The section below titled HOSPITALS AND THEIR DISTRIBUTOR OPTIONS\n"
        "contains each gap hospital with its available distributors, delivery\n"
        "feasibility pre-computed, and stock/price information. Use this data.\n\n"
        "Reason through:\n"
        "  1. Per gap hospital: read its distributor options below.\n"
        "     Which distributors are marked ARRIVES IN TIME?\n"
        "  2. Does that distributor have sufficient stock for bridge_units_needed?\n"
        "     If below min-order, flag for negotiation — still consider if best option.\n"
        "  3. Consolidate: if multiple hospitals share same best distributor, combine.\n"
        "  4. Compute units_required (bridge_units_needed) vs units_allocated per hospital.\n"
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

