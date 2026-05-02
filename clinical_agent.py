"""
clinical_agent.py

Clinical Agent — pure lookup function, no LLM.

Receives a DrugAlertPackage (invoke_clinical=True) and returns a structured
substitution assessment built entirely from pre-loaded data.

What it does:
    1. Reads pkg.alternatives — already fetched from Neo4j by the Analyst
    2. Classifies each alternative as VIABLE or BLOCKED
         BLOCKED: shared_api_risk=True — alternative uses same disrupted API
         VIABLE:  shared_api_risk=False — safe to recommend
    3. Applies similarity tier logic to viable alternatives
         0.90–1.0  near_identical  — same drug different dose, straightforward
         0.70–0.89 viable          — good substitute, clinical caveats apply
         0.00–0.69 last_resort     — different mechanism, physician sign-off required
    4. Selects best alternative (highest similarity among viable)
    5. Lists affected hospitals ranked by urgency (days_until_stockout)
    6. Returns structured output for the Aggregator

No LLM call — all logic is deterministic from dataset values.
The substitution_notes field from alt_drug_map already contains
the clinical reasoning written at data generation time.

Fires only for HIGH_RISK drugs (invoke_clinical=True in DrugAlertPackage).
Runs in parallel with Procurement Agent — not after it.
"""

from dataclasses import dataclass, field
from typing import Optional
from analyst import DrugAlertPackage, HospitalRisk

# Similarity score tier boundaries
NEAR_IDENTICAL_MIN = 0.90
VIABLE_MIN         = 0.70
# anything below VIABLE_MIN is last_resort


# ── DATACLASS ──────────────────────────────────────────────────────────────────

@dataclass
class ClinicalAssessment:
    """
    Structured output of the Clinical Agent.
    Passed to Aggregator alongside Procurement Agent output.
    """
    drug_id:                   str
    drug_name:                 str
    criticality:               str

    # Core outcome
    substitution_viable:       bool
    no_alternative_reason:     Optional[str]   # set when substitution_viable=False

    # Best alternative (when viable)
    recommended_alt_id:        Optional[str]
    recommended_alt_name:      Optional[str]
    recommended_alt_criticality: Optional[str]
    similarity_score:          Optional[float]
    similarity_tier:           Optional[str]   # near_identical | viable | last_resort
    substitution_notes:        Optional[str]   # from alt_drug_map dataset

    # Physician approval required when:
    # - similarity_tier == last_resort (mechanism change)
    # - alternative criticality lower than primary drug criticality
    requires_physician_approval: bool

    # All alternatives classified
    viable_alternatives:       list  # [{alt_drug_id, alt_drug_name, similarity_score,
                                     #   similarity_tier, substitution_notes}]
    blocked_alternatives:      list  # [{alt_drug_id, alt_drug_name, reason}]

    # Affected hospitals sorted by urgency
    affected_hospitals:        list  # [{hospital_id, hospital_name, specialty_type,
                                     #   risk_level, days_until_stockout,
                                     #   shortage_probability}]

    # Flags
    critically_urgent_hospitals: list  # hospitals stocking out within 7 days
    system_wide_caveats:       list

    def to_dict(self) -> dict:
        return {
            "drug_id":                   self.drug_id,
            "drug_name":                 self.drug_name,
            "criticality":               self.criticality,
            "substitution_viable":       self.substitution_viable,
            "no_alternative_reason":     self.no_alternative_reason,
            "recommended_alt_id":        self.recommended_alt_id,
            "recommended_alt_name":      self.recommended_alt_name,
            "recommended_alt_criticality": self.recommended_alt_criticality,
            "similarity_score":          self.similarity_score,
            "similarity_tier":           self.similarity_tier,
            "substitution_notes":        self.substitution_notes,
            "requires_physician_approval": self.requires_physician_approval,
            "viable_alternatives":       self.viable_alternatives,
            "blocked_alternatives":      self.blocked_alternatives,
            "affected_hospitals":        self.affected_hospitals,
            "critically_urgent_hospitals": self.critically_urgent_hospitals,
            "system_wide_caveats":       self.system_wide_caveats,
        }


# ── HELPERS ────────────────────────────────────────────────────────────────────

CRITICALITY_ORDER = {"Life-Critical": 3, "High": 2, "Moderate": 1, "Low": 0}

def _similarity_tier(score: float) -> str:
    if score >= NEAR_IDENTICAL_MIN:
        return "near_identical"
    if score >= VIABLE_MIN:
        return "viable"
    return "last_resort"


def _requires_physician(
    tier:          str,
    alt_criticality: str,
    drug_criticality:str,
) -> bool:
    """
    Physician approval required when:
    - Similarity tier is last_resort (mechanism change)
    - Alternative has lower criticality than primary drug
      (downgrading to a weaker drug needs sign-off)
    """
    if tier == "last_resort":
        return True
    alt_rank  = CRITICALITY_ORDER.get(alt_criticality, 0)
    drug_rank = CRITICALITY_ORDER.get(drug_criticality, 0)
    return alt_rank < drug_rank


def _classify_alternatives(pkg: DrugAlertPackage) -> tuple[list, list]:
    """
    Splits pkg.alternatives into viable and blocked lists.
    Blocked = shared_api_risk=True (alternative uses the same disrupted API).
    Each entry is enriched with similarity_tier.
    """
    viable  = []
    blocked = []

    for a in pkg.alternatives:
        raw_shared = a.get("shared_api_risk", False)
        # Handle Neo4j returning strings like "Yes" or "No" from the CSV
        if isinstance(raw_shared, str):
            shared = raw_shared.strip().lower() in ["yes", "true", "1"]
        else:
            shared = bool(raw_shared)
            
        score  = float(a.get("similarity_score", 0.0))
        tier   = _similarity_tier(score)

        entry = {
            "alt_drug_id":       a["alt_drug_id"],
            "alt_drug_name":     a["alt_drug_name"],
            "alt_criticality":   a.get("alt_criticality", "Unknown"),
            "similarity_score":  score,
            "similarity_tier":   tier,
            "substitution_notes":a.get("substitution_notes", ""),
        }

        if shared:
            blocked.append({
                **entry,
                "reason": (
                    f"Shares API source with disrupted node — "
                    f"supply of {a['alt_drug_name']} also at risk"
                ),
            })
        else:
            viable.append(entry)

    # Sort viable by similarity score descending — best first
    viable.sort(key=lambda x: x["similarity_score"], reverse=True)

    return viable, blocked


def _build_hospital_list(pkg: DrugAlertPackage) -> tuple[list, list]:
    """
    Builds sorted hospital list and identifies critically urgent ones.
    Critically urgent = stocks out within 7 days.
    Only includes HIGH_RISK hospitals (Clinical fires for these).
    """
    hospitals = []
    critical  = []

    high_risk = [h for h in pkg.hospitals if h.risk_level == "HIGH_RISK"]
    sorted_h  = sorted(high_risk, key=lambda h: h.days_until_stockout)

    for h in sorted_h:
        entry = {
            "hospital_id":          h.hospital_id,
            "hospital_name":        h.hospital_name,
            "specialty_type":       h.specialty_type,
            "risk_level":           h.risk_level,
            "days_until_stockout":  h.days_until_stockout,
            "shortage_probability": h.shortage_probability,
        }
        hospitals.append(entry)
        if h.days_until_stockout <= 7:
            critical.append(h.hospital_id)

    return hospitals, critical


def _build_caveats(
    pkg:           DrugAlertPackage,
    viable:        list,
    blocked:       list,
    best_alt:      Optional[dict],
    critical_hosps:list,
) -> list:
    """Assembles system-wide caveats from data — no LLM needed."""
    caveats = []

    if blocked:
        blocked_names = ", ".join(a["alt_drug_name"] for a in blocked)
        caveats.append(
            f"{blocked_names} {'is' if len(blocked)==1 else 'are'} blocked — "
            f"{'it shares' if len(blocked)==1 else 'they share'} the same disrupted "
            f"API source and cannot be relied upon as alternative supply."
        )

    if best_alt and best_alt["similarity_tier"] == "last_resort":
        caveats.append(
            f"{best_alt['alt_drug_name']} uses a different mechanism than "
            f"{pkg.drug_name}. Mandatory physician sign-off required before "
            f"switching any patient."
        )

    if best_alt and best_alt["similarity_tier"] == "near_identical":
        caveats.append(
            f"{best_alt['alt_drug_name']} is the same compound as {pkg.drug_name} "
            f"in a different dose/formulation. Verify dose equivalence before switching."
        )

    if critical_hosps:
        caveats.append(
            f"Critically urgent hospitals (stockout ≤7 days): "
            f"{', '.join(critical_hosps)}. "
            f"Substitution protocol must be initiated immediately."
        )

    if pkg.criticality == "Life-Critical":
        caveats.append(
            f"{pkg.drug_name} is Life-Critical. Any substitution must be "
            f"supervised by senior clinical staff. Do not switch without "
            f"physician approval even for near-identical alternatives."
        )

    return caveats


# ══════════════════════════════════════════════════════════════════════════════
# MAIN FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def run_clinical_agent(
    pkg:     DrugAlertPackage,
    verbose: bool = True,
) -> dict:
    """
    Main Clinical Agent entry point. Pure lookup — no LLM.

    Args:
        pkg:     DrugAlertPackage with invoke_clinical=True
        verbose: print progress

    Returns:
        ClinicalAssessment.to_dict() — structured output for Aggregator
    """
    if verbose:
        print(f"\n  [Clinical Agent] {pkg.drug_name} ({pkg.drug_id}) "
              f"[{pkg.criticality}]")

    viable, blocked = _classify_alternatives(pkg)
    hospitals, critical_hosps = _build_hospital_list(pkg)

    if verbose:
        print(f"    Viable alternatives:  {[a['alt_drug_name'] for a in viable]}")
        print(f"    Blocked alternatives: {[a['alt_drug_name'] for a in blocked]}")
        print(f"    HIGH_RISK hospitals:  {len(hospitals)}")
        print(f"    Critically urgent:    {critical_hosps}")

    # ── Case 1: No alternatives at all ────────────────────────────────────────
    if not pkg.alternatives:
        caveats = [
            f"{pkg.drug_name} ({pkg.criticality}) has no registered alternative "
            f"in the formulary. Supply procurement is the only resolution path.",
            "Escalate to senior clinical staff immediately.",
        ]
        assessment = ClinicalAssessment(
            drug_id=pkg.drug_id, drug_name=pkg.drug_name,
            criticality=pkg.criticality,
            substitution_viable=False,
            no_alternative_reason=(
                f"No alternative drug exists for {pkg.drug_name}."
            ),
            recommended_alt_id=None, recommended_alt_name=None,
            recommended_alt_criticality=None, similarity_score=None,
            similarity_tier=None, substitution_notes=None,
            requires_physician_approval=False,
            viable_alternatives=[], blocked_alternatives=[],
            affected_hospitals=hospitals,
            critically_urgent_hospitals=critical_hosps,
            system_wide_caveats=caveats,
        )
        if verbose:
            print(f"    → No alternative exists.")
        return assessment.to_dict()

    # ── Case 2: All alternatives blocked ──────────────────────────────────────
    if not viable and blocked:
        blocked_names = ", ".join(a["alt_drug_name"] for a in blocked)
        caveats = [
            f"All alternatives ({blocked_names}) share the same disrupted API "
            f"source and are also at risk. Substitution provides no safety margin.",
            "Supply procurement is the only resolution path.",
        ]
        assessment = ClinicalAssessment(
            drug_id=pkg.drug_id, drug_name=pkg.drug_name,
            criticality=pkg.criticality,
            substitution_viable=False,
            no_alternative_reason=(
                f"All known alternatives share the disrupted API source "
                f"({pkg.api_context.get('api_name', 'unknown')}) "
                f"and are also affected by this disruption."
            ),
            recommended_alt_id=None, recommended_alt_name=None,
            recommended_alt_criticality=None, similarity_score=None,
            similarity_tier=None, substitution_notes=None,
            requires_physician_approval=False,
            viable_alternatives=[],
            blocked_alternatives=[{
                "alt_drug_id":   a["alt_drug_id"],
                "alt_drug_name": a["alt_drug_name"],
                "reason":        a["reason"],
            } for a in blocked],
            affected_hospitals=hospitals,
            critically_urgent_hospitals=critical_hosps,
            system_wide_caveats=caveats,
        )
        if verbose:
            print(f"    → All alternatives blocked.")
        return assessment.to_dict()

    # ── Case 3: Viable alternatives exist ─────────────────────────────────────
    # Best alternative = highest similarity score among viable
    best = viable[0]
    tier = best["similarity_tier"]
    needs_physician = _requires_physician(
        tier, best["alt_criticality"], pkg.criticality)
    caveats = _build_caveats(pkg, viable, blocked, best, critical_hosps)

    assessment = ClinicalAssessment(
        drug_id=pkg.drug_id, drug_name=pkg.drug_name,
        criticality=pkg.criticality,
        substitution_viable=True,
        no_alternative_reason=None,
        recommended_alt_id=best["alt_drug_id"],
        recommended_alt_name=best["alt_drug_name"],
        recommended_alt_criticality=best["alt_criticality"],
        similarity_score=best["similarity_score"],
        similarity_tier=tier,
        substitution_notes=best["substitution_notes"],
        requires_physician_approval=needs_physician,
        viable_alternatives=viable,
        blocked_alternatives=[{
            "alt_drug_id":   a["alt_drug_id"],
            "alt_drug_name": a["alt_drug_name"],
            "reason":        a["reason"],
        } for a in blocked],
        affected_hospitals=hospitals,
        critically_urgent_hospitals=critical_hosps,
        system_wide_caveats=caveats,
    )

    if verbose:
        print(f"    → Recommended: {best['alt_drug_name']} "
              f"(sim={best['similarity_score']}, tier={tier})")
        print(f"    → Physician approval required: {needs_physician}")
        for c in caveats:
            print(f"    Caveat: {c}")

    return assessment.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from sentinel import process_disruption
    from analyst import analyse
    from prediction_engine import SESSION

    # Test 1: D001 Insulin — no alternative
    print("\n" + "="*62)
    print("TEST 1: D001 Lantus — no alternative exists")
    print("="*62)
    event1 = process_disruption("Factory", "F001", "Disaster", "High", "2024-08-15")
    pkgs1  = analyse(event1, verbose=False)
    insulin = next((p for p in pkgs1 if p.drug_id == "D001"), None)
    if insulin:
        if insulin.invoke_clinical:
            r1 = run_clinical_agent(insulin)
            print(f"  substitution_viable:  {r1['substitution_viable']}")
            print(f"  reason:               {r1['no_alternative_reason']}")
            print(f"  hospitals:            {len(r1['affected_hospitals'])}")
        else:
            print(f"  invoke_clinical=False ({insulin.overall_risk_level})")
    SESSION.reset()

    # Test 2: D017 Asthalin — all alternatives blocked (A012 disruption)
    print("\n" + "="*62)
    print("TEST 2: D017 Asthalin — all alternatives share A012 risk")
    print("="*62)
    event2   = process_disruption("API", "A012", "Supply Chain Failure", "High", "2024-01-15")
    pkgs2    = analyse(event2, verbose=False)
    asthalin = next((p for p in pkgs2 if p.drug_id == "D017"), None)
    if asthalin:
        if asthalin.invoke_clinical:
            r2 = run_clinical_agent(asthalin)
            print(f"  substitution_viable:  {r2['substitution_viable']}")
            print(f"  blocked:              {[a['alt_drug_name'] for a in r2['blocked_alternatives']]}")
            print(f"  reason:               {r2['no_alternative_reason']}")
            print(f"  critical hospitals:   {r2['critically_urgent_hospitals']}")
        else:
            print(f"  invoke_clinical=False ({asthalin.overall_risk_level})")
    SESSION.reset()

    # Test 3: D004 Amoxil — partial alternatives
    # D005 Augmentin + D019 Cephalexin blocked (share A004)
    # D006 Azithral viable (different API)
    # Amoxil is MEDIUM_RISK in Cipla Disaster — invoke_clinical=False
    # So force it via A004 Raw Material Shortage which gives higher scores
    print("\n" + "="*62)
    print("TEST 3: D004 Amoxil — partial alternatives (Azithral viable)")
    print("="*62)
    event3 = process_disruption("API", "A004", "Raw Material Shortage", "High", "2024-08-15")
    pkgs3  = analyse(event3, verbose=False)
    amoxil = next((p for p in pkgs3 if p.drug_id == "D004"), None)
    if amoxil:
        if amoxil.invoke_clinical:
            r3 = run_clinical_agent(amoxil)
            print(f"  substitution_viable:         {r3['substitution_viable']}")
            print(f"  recommended:                 {r3['recommended_alt_name']} "
                  f"(sim={r3['similarity_score']}, tier={r3['similarity_tier']})")
            print(f"  requires_physician_approval: {r3['requires_physician_approval']}")
            print(f"  viable:  {[a['alt_drug_name'] for a in r3['viable_alternatives']]}")
            print(f"  blocked: {[a['alt_drug_name'] for a in r3['blocked_alternatives']]}")
            print(f"  notes:   {r3['substitution_notes']}")
            for c in r3['system_wide_caveats']:
                print(f"  Caveat: {c}")
        else:
            print(f"  invoke_clinical=False ({amoxil.overall_risk_level})")
    SESSION.reset()

    # Test 4: D011 Amlip — last-resort alternative
    # D012 Amlosafe blocked (shares A009)
    # D015 Metolar viable but similarity=0.60 (last_resort — different mechanism)
    # Cardiac hospitals (H002, H005) should be in affected list
    print("\n" + "="*62)
    print("TEST 4: D011 Amlip — last-resort alternative (Metolar, sim=0.60)")
    print("="*62)
    event4 = process_disruption("Factory", "F004", "Disaster", "High", "2024-08-15")
    pkgs4  = analyse(event4, verbose=False)
    amlip  = next((p for p in pkgs4 if p.drug_id == "D011"), None)
    if amlip:
        if amlip.invoke_clinical:
            r4 = run_clinical_agent(amlip)
            print(f"  substitution_viable:         {r4['substitution_viable']}")
            print(f"  recommended:                 {r4['recommended_alt_name']} "
                  f"(sim={r4['similarity_score']}, tier={r4['similarity_tier']})")
            print(f"  requires_physician_approval: {r4['requires_physician_approval']}")
            print(f"  blocked: {[a['alt_drug_name'] for a in r4['blocked_alternatives']]}")
            print(f"  notes:   {r4['substitution_notes']}")
            for c in r4['system_wide_caveats']:
                print(f"  Caveat: {c}")
        else:
            print(f"  invoke_clinical=False ({amlip.overall_risk_level})")
    SESSION.reset()