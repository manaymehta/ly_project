# Pharmaceutical Supply Chain Disruption Intelligence System
# Project Context — Updated

---

## PROJECT OVERVIEW

A graph-based AI system that detects pharmaceutical supply chain disruptions,
scores their impact on hospitals, and generates procurement and clinical
recommendations for human review via a dashboard.

The system is reactive (fires on disruption input) and proactive (GNN
vulnerability scores surface structural risk before any disruption occurs).

Human-in-the-loop design: agents propose, never decide. Every recommendation
is queued for operator approval or rejection on the dashboard.

---

## TECH STACK

- Neo4j — heterogeneous graph database, primary data store
- Python — all backend logic
- Prophet — 200 pre-trained demand forecasting models (one per hospital-drug pair)
- Google Gemini API (google-genai SDK, model: gemini-2.5-flash) — Procurement Agent LLM only
- SQLite — review package storage and status tracking
- FastAPI — API layer
- Frontend — not decided yet, to be built after backend is complete

---

## DATA

### Graph: 57 nodes, 1939 relationships

Node types and counts:
- Factory:     5  (F001–F005)
- API:        12  (A001–A012)
- Drug:       20  (D001–D020)
- Distributor:10  (S001–S010)
- Hospital:   10  (H001–H010)

Relationship types:
- PRODUCES_API     (Factory→API)        17 edges  — capacityShare [0–1 decimal], monthlyOutput
- COMPONENT_OF     (API→Drug)           20 edges  — yieldMultiplier
- PRODUCES_DRUG    (Factory→Drug)       30 edges  — derived shortcut, no properties
- STOCKS           (Drug→Distributor)  164 edges  — derived from catalogue, stock > 0
- DELIVERS_TO      (Distributor→Hospital) 1640 edges — drugId, pricePerUnit, minOrder, deliveryDays, currentStock
- NEEDS_DRUG       (Hospital→Drug)     200 edges  — dailyDemand, monthlyDemand, currentUnits, daysOfStock
- ALTERNATIVE_TO   (Drug→Drug)          32 edges  — similarityScore, sharedApiRisk, notes

### Key data facts
- capacityShare stored as decimal (0.72 not 72) in Neo4j
- current_stock in DELIVERS_TO = distributor's total stock for that drug (same value across all hospital rows for same distributor-drug pair)
- delivery_days VARIES per hospital for same distributor
- min_order and price_per_unit same per distributor regardless of hospital
- DELIVERS_TO.drugId is a property on the relationship (Drug and Distributor not directly linked as nodes)
- STOCKS relationship was added specifically to connect Drug→Distributor for GNN traversal

### Node properties (all nodes)
Every node has: id, name, vulnerabilityScore, centralityScore, dependencyScore
These are set by the GNN component (gnn_centrality.py).

### CSV files (read at startup, not in Neo4j)
- hospital_inventory.csv — Daily Demand, Current Units, Days of Stock per hospital-drug
- hospital_drug_demand.csv — Daily Demand, Monthly Demand, Current Units, Days of Stock
- disruption_taxonomy.csv — 20 rows, Node Type, Event Type, Severity, Min Days, Max Days
- drugs.csv, hospitals.csv, distributors.csv, factories.csv — reference data
- seasonality_multiplier.csv, speciality_multiplier.csv — reference only

---

## SHORTAGE PROBABILITY FORMULA

shortage_probability = supply_loss_pct × demand_pressure × time_factor

All three components bounded [0, 1]. Maximum score = 1.0.

### supply_loss_pct
Fraction of global drug supply lost.
- Factory disruption: this_factory_output / global_total (per API, then converted to drug units via yieldMultiplier)
- API disruption: sum of offline factory outputs / global total. If no factory offline, full global = lost.
- Distributor disruption: forced to 1.0 (that channel is gone)

### demand_pressure
System-wide signal. Computed ONCE per drug, shared across all hospitals:
  total_system_forecast_30d (sum of Prophet forecasts for all affected hospitals)
  ─────────────────────────────────────────────────────────────────────────────
  drug_units_remaining (global remaining supply after disruption)

Capped at 1.0. When remaining = 0, demand_pressure = 1.0.

### time_factor
Per-hospital urgency. Uses Prophet-forecasted daily rate, not static daily demand:
  effective_daily = prophet_forecast_30d / 30
  days_until_stockout = current_units / effective_daily
  time_factor = 1 - (days_until_stockout / recovery_days), clamped [0, 1]

### Risk tiers
- NO_RISK:     score = 0.0
- LOW_RISK:    0 < score < 0.20
- MEDIUM_RISK: 0.20 ≤ score < 0.50
- HIGH_RISK:   score ≥ 0.50

---

## DISRUPTION TAXONOMY (20 entries)

Node Type | Event Type            | Severity | Recovery (midpoint)
Factory   | Disaster              | High     | 52d
Factory   | Disaster              | Medium   | 31d
Factory   | Equipment Failure     | High     | 34d
Factory   | Equipment Failure     | Medium   | 18d
Factory   | Equipment Failure     | Low      | 5d
Factory   | Strike                | High     | 45d
Factory   | Strike                | Medium   | 23d
Factory   | License Hold          | High     | 72d
Factory   | License Hold          | Medium   | 35d
Factory   | Raw Material Shortage | High     | 49d
Factory   | Raw Material Shortage | Medium   | 24d
API       | Raw Material Shortage | High     | 47d
API       | Supply Chain Failure  | High     | 55d
Distributor | Logistics Failure   | High     | 15d
Distributor | Logistics Failure   | Medium   | 6d
Distributor | Strike              | Medium   | 13d
Distributor | License Suspension  | High     | 40d
Distributor | Storage Failure     | High     | 26d
Distributor | Storage Failure     | Medium   | 11d
Distributor | Disaster            | High     | 32d

Fallback when (node_type, event_type, severity) not in taxonomy:
  High=21d, Medium=10d, Low=4d

---

## GNN COMPONENT (gnn_centrality.py)

Two scores computed per node:

centralityScore [0-1]:
  Betweenness centrality via Neo4j GDS, per-label normalised.
  Full graph projection: all 5 node types, all 6 relationship types, UNDIRECTED.

dependencyScore [0-1]:
  Custom irreplaceability score. Formula per node type:
  - Factory:     max(max_capacity_share, num_apis_produced / total_apis)
  - API:         base_dep × max_criticality_weight_of_downstream_drugs
                 base_dep = 1.0 if sole producer, else 1 - min_capacity_share
                 criticality weights: Life-Critical=1.0, High=0.75, Moderate=0.50, Low=0.25
  - Drug:        max(dependencyScore of source APIs)
  - Distributor: 1 - (drugs_carried / total_drugs)
  - Hospital:    0.5 uniform

vulnerabilityScore [0-1]:
  0.2 × centralityScore + 0.8 × dependencyScore

Written permanently to Neo4j. Dashboard reads for Vulnerability View tab.
Prediction engine score and GNN scores are completely separate signals.

---

## PIPELINE ARCHITECTURE

Sentinel → Prediction Engine → Analyst → [Procurement Agent ∥ Clinical Agent] → Aggregator → SQLite → Dashboard

### Sentinel (sentinel.py)
Entry point. Validates disruption input.
- Hard stops: invalid node_type (must be Factory/API/Distributor), node not in Neo4j, invalid severity
- Soft warnings: event+severity not in taxonomy (warns, uses fallback, continues), compound disruption (node already offline in session)
- Resolves node_name from Neo4j
- Calls prediction engine
- Returns DisruptionEvent dataclass:
  {node_type, node_id, event_type, severity, triggered_date, node_name,
   recovery_days, taxonomy_match, results (list), affected_count, processed_at, warnings}

### Prediction Engine (prediction_engine.py)
Called by Sentinel. Three-pass architecture:
  Pass 1: get_affected_pairs() — Neo4j graph traversal, builds list of (hospital, drug) pair dicts
  Pass 2: compute_drug_level_metrics() — runs Prophet for all pairs, aggregates system-wide demand_pressure per drug
  Pass 3: calculate_shortage_probability() — scores each pair using precomputed drug metrics

Session state (SimulationSession):
  - inventory: deep copy of BASE_DATA inventory, mutable
  - factory_status: {fid: "offline"/"online"}
  - Reset between scenarios, never mutates BASE_DATA

Key methods: deplete_inventory(), restock_inventory(), set_factory_offline(), is_factory_offline()

BASE_DATA loaded at startup from CSVs:
  drug_ref, hospital_ref, inventory_base, demand_ref (monthly demand fallback)

### Analyst (analyst.py)
Receives DisruptionEvent. Groups results by drug_id.
For each drug group:
  - Determines overall_risk_level (highest tier across hospitals)
  - invoke_procurement = any hospital requires_action (MEDIUM+)
  - invoke_clinical = any hospital is HIGH_RISK
  - Batch fetches from Neo4j: drug context + API, hospital metadata, distributor options, alternatives
  - Excludes disrupted distributor from DELIVERS_TO when disruption_type=Distributor
Returns List[DrugAlertPackage] sorted HIGH_RISK first.

DrugAlertPackage fields:
  drug_id, drug_name, criticality, category, vulnerability_score
  disruption_type, disrupted_node, disrupted_name, triggered_date, recovery_days
  supply_loss_pct, demand_pressure, drug_units_remaining, system_total_forecast
  overall_risk_level, invoke_procurement, invoke_clinical
  hospitals: List[HospitalRisk] sorted by days_until_stockout ascending
  api_context: {api_id, api_name, api_vulnerability_score}
  alternatives: [{alt_drug_id, alt_drug_name, alt_criticality, similarity_score,
                  shared_api_risk, substitution_notes}]

HospitalRisk fields:
  hospital_id, hospital_name, hospital_city, specialty_type, avg_daily_patients
  shortage_probability, risk_level, requires_action
  days_until_stockout, prophet_forecast_30d, time_factor
  distributors: list of {distributor_id, name, city, delivery_speed_class,
                          specialization, reliability_score, pricing_tier,
                          vulnerability_score, price_per_unit, min_order,
                          delivery_days, current_stock}
  NOTE: delivery_days varies per hospital. current_stock/price/min_order same across hospitals.

### Procurement Agent (procurement_agent.py)
LLM-based. Uses Gemini. Single file, two execution paths.

TOTAL_LOSS path (supply_loss_pct=1.0 or drug_units_remaining=0):
  Single Gemini call.
  Prompt structure:
    - Disruption block
    - Drug block
    - Per-hospital section with that hospital's distributors sorted by delivery speed.
      Each distributor shows: stock, price, min_order, delivery feasibility
      ("ARRIVES IN TIME" or "ARRIVES AFTER STOCKOUT" pre-computed per hospital)
      Min-order shown as advisory ("BELOW MIN ORDER — negotiation may be required")
      NOT as hard disqualification.
    - Alternatives block (viable vs blocked by shared API risk)
    - Task instructions
  LLM reasons per-hospital then consolidates orders.
  Output: option_a (primary), option_b (if is_dicey_case=True), hospital_allocations
  with units_required and units_allocated per hospital.

PARTIAL_LOSS path (drug_units_remaining > 0):
  Two sequential Gemini calls.
  Call 1 — ALLOCATION:
    Python pre-computes per hospital:
      min_delivery_days = fastest distributor to that hospital
      needs_bridge = days_until_stockout < min_delivery_days
      gap_days = min_delivery_days - days_until_stockout
      bridge_units = daily_demand × gap_days
    LLM reads pre-computed labels (NEEDS BRIDGE / covered), outputs hospitals_needing_bridge list.
  Call 2 — BRIDGE ORDER:
    Only for gap hospitals from Call 1.
    Same per-hospital distributor structure as single-call.
    LLM recommends bridge orders.

Dicey case: when two distributors are genuinely close in merit (faster but below
min-order vs slower but meets min-order), LLM sets is_dicey_case=True and returns
option_b alongside option_a. Dashboard operator decides which to approve.

Output keys always present:
  drug_id, drug_name, scenario (TOTAL_LOSS/PARTIAL_LOSS)
  parse_ok, call_count, procurement_viable
  is_dicey_case, dicey_tradeoff
  option_a: [{distributor_id, distributor_name, total_quantity, price_per_unit,
               hospital_allocations: [{hospital_id, units_required, units_allocated,
                                        delivery_days, coverage_note}],
               rationale}]
  option_b: null or same structure
  total_stock_gap, caveats, recommendation_summary
  For PARTIAL_LOSS also: allocation (Call 1 output), bridge_order (Call 2 output)

### Clinical Agent (clinical_agent.py)
Pure lookup function. NO LLM. Fully deterministic.
Fires when invoke_clinical=True (any hospital is HIGH_RISK).
Runs in parallel with Procurement Agent.

Three cases:
  1. No alternatives at all (e.g. D001 Insulin):
     substitution_viable=False. Returns immediately with escalation caveats.
  2. All alternatives blocked by shared_api_risk=True:
     substitution_viable=False. Returns with blocked list and reason.
  3. Viable alternatives exist (shared_api_risk=False):
     Selects best by highest similarity_score.
     Classifies by tier:
       near_identical (0.90+): same drug diff dose — dose equivalence caveat
       viable (0.70–0.89): good substitute with standard caveats
       last_resort (<0.70): different mechanism — physician sign-off required
     requires_physician_approval=True if tier=last_resort OR
       alternative criticality lower than primary drug criticality.

Output keys (ClinicalAssessment.to_dict()):
  drug_id, drug_name, criticality
  substitution_viable, no_alternative_reason
  recommended_alt_id, recommended_alt_name, recommended_alt_criticality
  similarity_score, similarity_tier, substitution_notes
  requires_physician_approval
  viable_alternatives: [{alt_drug_id, alt_drug_name, similarity_score,
                          similarity_tier, substitution_notes}]
  blocked_alternatives: [{alt_drug_id, alt_drug_name, reason}]
  affected_hospitals: [{hospital_id, hospital_name, specialty_type,
                         risk_level, days_until_stockout, shortage_probability}]
  critically_urgent_hospitals: [hospital_ids with days_until_stockout ≤ 7]
  system_wide_caveats: [...]

### Aggregator (aggregator.py)
Merges both agent outputs into ReviewPackage dicts. Writes to SQLite.

Clinical suppression logic:
  Suppress clinical output when:
    - invoke_clinical=False (drug not HIGH_RISK), OR
    - procurement fully covers all hospitals (all coverage_status FULL or COVERED_BY_FACTORY)
  Rationale: if procurement solved the supply problem, substitution is unnecessary.
  Clinical Agent always runs (negligible cost — no LLM), Aggregator decides whether
  to surface its output to the operator.

Coverage computation:
  For each actionable hospital:
    units_required = prophet_forecast_30d
    units_acquired = sum of units_allocated from option_a hospital_allocations
    coverage_gap = max(0, units_required - units_acquired)
    coverage_status: FULL | PARTIAL | NONE | COVERED_BY_FACTORY

SQLite table: review_packages
  package_id (PRIMARY KEY: "{disrupted_node}-{drug_id}-{triggered_date}")
  disruption_node, disruption_event, disruption_severity
  drug_id, drug_name, criticality, overall_risk_level
  procurement_viable (0/1), clinical_suppressed (0/1), substitution_viable (0/1/NULL)
  status: pending_review / approved / rejected
  created_at, resolved_at (NULL until reviewed)
  procurement_action (JSON, filled on approval)
  clinical_action (JSON, filled on approval)
  full_package (full JSON blob — dashboard renders this)

ReviewPackage structure (stored in full_package):
  package_id, status, created_at, action_required, action_summary
  disruption: {node_id, node_name, disruption_type, event_type, severity,
               recovery_days, triggered_date}
  drug: {drug_id, drug_name, criticality, category, overall_risk_level,
         supply_loss_pct, demand_pressure, drug_units_remaining,
         system_total_forecast, vulnerability_score, api_name}
  hospital_coverage: [{hospital_id, hospital_name, hospital_city, specialty_type,
                        risk_level, shortage_probability, days_until_stockout,
                        units_required, units_acquired, coverage_gap,
                        coverage_pct, coverage_status}]
  procurement: {scenario, viable, call_count, is_dicey_case, dicey_tradeoff,
                recommendation_summary, option_a, option_b, total_stock_gap,
                caveats, allocation (PARTIAL only), bridge_order (PARTIAL only)}
  clinical: {suppressed, suppression_reason, substitution_viable,
             no_alternative_reason, recommended_alt_id, recommended_alt_name,
             recommended_alt_criticality, similarity_score, similarity_tier,
             substitution_notes, requires_physician_approval,
             viable_alternatives, blocked_alternatives,
             critically_urgent_hospitals, system_wide_caveats}

SQLite helper functions (used by FastAPI):
  get_pending_packages(db_path) → list of full_package dicts
  update_package_status(package_id, status, procurement_action, clinical_action)
  get_package_by_id(package_id) → dict

---

## WHAT IS NOT BUILT YET

- FastAPI layer (to be built)
- Frontend / Dashboard (to be built after FastAPI)
- factory_events_history.csv (deferred, validation only)
- GNN Components 2 and 3 (HeteroConv, GAT) — architecture designed, not implemented

---

## DESIGN PRINCIPLES TO MAINTAIN

1. Prediction engine score and GNN vulnerability score are SEPARATE signals. Never combine them into one number.

2. demand_pressure is system-wide per drug, not per hospital. All hospitals competing for the same remaining supply pool share this value.

3. Clinical Agent has no LLM. It is a deterministic lookup. Do not add an LLM call to it.

4. Procurement Agent uses two paths (single-call for total loss, two-call for partial loss). The two-call path pre-computes gap hospitals in Python — the LLM reads labels, not raw numbers.

5. BELOW MIN ORDER is an advisory flag in prompts, not a hard disqualification. The LLM can still recommend these distributors with a negotiation caveat.

6. Clinical suppression happens in the Aggregator, not in the Clinical Agent. The Clinical Agent always runs and always produces output.

7. Human always approves or rejects. Agents never auto-execute.

8. Session state is a deep copy of BASE_DATA. BASE_DATA is never mutated.

9. All Neo4j queries that fetch distributor options exclude the disrupted distributor when disruption_type=Distributor.

10. The STOCKS relationship (Drug→Distributor) exists specifically for GNN graph traversal. The prediction engine does not use it — it queries DELIVERS_TO directly.

---

## KNOWN ISSUES / DECISIONS MADE

- Hospital inventory was generated with inflated buffer days. Original data kept as-is (realistic for Indian tertiary hospitals per literature). Taxonomy recovery days were adjusted upward to make scenarios fire alerts.
- capacityShare stored as decimal in Neo4j (0.72, not 72). Injection script handles this.
- D017 Asthalin and D018 Ventorlin share A012 as their only API — both have shared_api_risk=True on each other's ALTERNATIVE_TO relationship. This is correct and intentional.
- D001 Insulin (Lantus) has no alternative. This is correct.
- S007 BioSupply India only stocks D001 (Insulin). Excluded from STOCKS relationships for other drugs.
- The comparison between single-call and two-call Procurement Agent paths is an ongoing evaluation. Single-call is default for TOTAL_LOSS, two-call for PARTIAL_LOSS.

---

## INSTRUCTIONS FOR CLAUDE CODE

Read this context fully before making any changes.
The backend pipeline (Sentinel → Analyst → Agents → Aggregator) is designed and partially built.
Your job is to continue from where the previous work left off.

Before writing any code:
1. Check what files currently exist
2. Check what each file currently does vs what this context says it should do
3. Identify gaps and discrepancies
4. Ask if anything is unclear before building

Do not change design decisions listed in DESIGN PRINCIPLES above without discussion.
Do not add LLM calls to the Clinical Agent.
Do not merge GNN scores into the shortage probability formula.

---

## REASONING AND INTENT BEHIND KEY DECISIONS

### Why Clinical Agent has no LLM
The basic lookup — does an alternative exist, is it blocked by shared API risk —
is pure deterministic logic from the data. The substitution_notes field in
alt_drug_map already contains the clinical reasoning written at data generation
time. The similarity score and tier already encode how safe the substitution is.
An LLM adds no value here and would only introduce inconsistency. The Clinical
Agent is a lookup function that surfaces pre-existing knowledge, not a reasoning
engine.

### Why two-call path for Procurement Agent partial loss
Allocation and bridge order selection are fundamentally different reasoning tasks.
Call 1 (allocation) asks: which hospitals have a gap before the fastest distributor
can reach them? This is pure urgency triage — no distributor data needed.
Call 2 (bridge order) asks: given those specific gap hospitals, which distributor
covers them within their stockout window? This is procurement math — needs
distributor data but for a much smaller set of hospitals.
Separating them gives the LLM focused context for each task. A single call with
all 10 hospitals and all distributors causes the LLM to lose track of which
hospitals actually need emergency orders vs which are covered by remaining factory
supply.

### Why dicey case only fires in genuine non-obvious situations
The dicey case (is_dicey_case=True, option_b returned) is not for every
multi-distributor scenario. It fires only when two options are close enough
that the tradeoff is a genuine judgment call — e.g. one distributor is faster
but below minimum order (requires negotiation), another meets minimum order
but arrives later than the most urgent hospital's stockout. The human operator
decides which risk to take. If one option is clearly better on all dimensions,
the LLM picks it and option_b stays null.

### Why clinical suppression happens in Aggregator not Clinical Agent
Clinical Agent always runs because it has no LLM call and costs nothing.
The suppression decision — did procurement already solve the supply problem —
can only be made after seeing the Procurement Agent's output (units_allocated
vs units_required per hospital). Clinical Agent has no visibility into
Procurement Agent output when running in parallel. So the Aggregator, which
sees both outputs together, makes the suppression call. This keeps agents
independent and the pipeline parallel.

### Human-in-the-loop scope per disruption
One disruption event generates multiple review packages (one per affected drug).
The operator's decision load per disruption:
  1. Disruption confirmation (1 decision — valid event or dismiss)
  2. Procurement approvals (~1 per actionable drug, not per hospital)
  3. Clinical approvals (only for HIGH_RISK drugs where substitution is viable
     and not suppressed)
  4. LOW_RISK monitoring acknowledgement (1 click, no action)
Total: roughly 10-15 decisions for a major disruption like Cipla Disaster/High.
Agents propose, operator approves or rejects. Nothing executes automatically.

### S008 CriticalMed as express specialist
S008 CriticalMed Supplies exists specifically for Life-Critical emergency
situations. It carries only 4 drugs (D001, D011, D015, D017), has express
delivery speed class, but charges premium prices and has low stock (50-124
units for most drugs). The Procurement Agent must surface this tradeoff
explicitly: S008 is the fastest option but below minimum order for most
scenarios and cannot cover full system demand. For H009 (District Hospital
Nagpur) running out in 14.8 days with the next fastest distributor taking
11 days, S008's 5-day delivery is potentially the difference between
stockout and coverage — even if negotiation for below-min-order is required.
This is the prototypical dicey case the agent should flag.

### Single-call vs two-call comparison test intent
The comparison test (running the same drug through both paths) is not just
a correctness check. It is an evaluation of LLM attention quality — does
giving the LLM a focused smaller context (two calls) produce better reasoning
than one large call with everything? The hypothesis is that for partial loss
scenarios with many hospitals and distributors, two focused calls produce
more accurate hospital prioritisation and better gap quantification than one
large call. The test results should inform whether the two-call path is worth
the extra API call cost.

### Inventory buffer and taxonomy recovery days
Hospital inventory was originally generated with buffers too generous for
scenarios to fire (Life-Critical: ~40 days, vs Fire recovery of 22 days).
Rather than changing the inventory data (which was derived from realistic
Indian tertiary hospital procurement cycles per literature), the taxonomy
recovery days were adjusted upward so that meaningful disruption scenarios
produce non-zero time_factor values. This is the correct approach — the
inventory reflects reality, the taxonomy reflects how long different event
types actually take to recover.

### demand_pressure is system-wide per drug, not per hospital
All hospitals affected by the same drug disruption are competing for the
same remaining global supply pool. It makes no sense to compare one
hospital's 30-day forecast against the entire global remaining supply —
that would make demand_pressure near-zero for every individual hospital
even when the system as a whole is severely stressed. The system-wide
aggregation correctly captures whether remaining supply can meet total
demand across all affected hospitals.

### Prophet forecasted daily rate in time_factor
The static daily_demand from hospital_inventory.csv is a historical average.
Prophet's forecasted daily rate reflects seasonal patterns — in August
(monsoon peak) antibiotic demand is higher, so hospitals burn through stock
faster and time_factor is higher. Using Prophet's rate makes time_factor
seasonal-aware, which is the entire point of having 200 individual
hospital-drug models. Without this, August and April would produce identical
time_factor values for the same hospital-drug pair.

### STOCKS relationship purpose
The STOCKS relationship (Drug→Distributor) was added specifically to make
the supply chain graph fully connected for GNN betweenness centrality.
Without it, the manufacturing side (Factory→API→Drug) and the distribution
side (Distributor→Hospital) are disconnected subgraphs. STOCKS bridges them
so that paths like Factory→API→Drug→Distributor→Hospital are traversable
in one connected graph. The prediction engine does NOT use STOCKS — it
queries DELIVERS_TO directly with drugId as a property filter.

### Why PRODUCES_DRUG is kept despite being derivable
PRODUCES_DRUG (Factory→Drug) is derived from PRODUCES_API + COMPONENT_OF.
It was created as a convenience shortcut for fast drug discovery in the
prediction engine — instead of traversing Factory→API→Drug for every
disruption query, one hop Factory→Drug finds all affected drugs immediately.
It is NOT redundant in the context of GNN — it serves as the bridge connecting
the manufacturing side to the distribution side in the physical delivery path
(Factory→Drug→Distributor→Hospital via PRODUCES_DRUG and STOCKS).
In the GNN projection, both PRODUCES_API (API path) and PRODUCES_DRUG
(physical path) are included. Including both does not double-count because
they represent different semantic paths through the graph.

---

## DEMO SCENARIOS (primary)

### Scenario A — Cipla Factory Disaster / High / August (monsoon peak)
Cipla (F002) is the sole producer of A012 (Salbutamol) and the dominant
producer of A004 (Amoxicillin, 72%), A005 (Azithromycin, 71%), A006
(Ciprofloxacin, 75%). A disaster taking Cipla offline for ~52 days hits
multiple drugs simultaneously. August is monsoon peak — respiratory and
antibiotic demand is elevated. This produces:
  - Ventorlin (D018, High): HIGH_RISK across most hospitals
  - Asthalin (D017, Life-Critical): MEDIUM_RISK — deeper buffer
  - Amoxil (D004, Moderate): MEDIUM_RISK for thin-buffer hospitals
  - Multiple LOW_RISK drugs for monitoring
Procurement Agent faces genuine tradeoffs (S008 express vs S010 meets
min-order). Clinical Agent finds Ventorlin/Asthalin alternatives blocked
(both share A012).

### Scenario B — A012 Supply Chain Failure / High / January (winter peak)
Direct API-level disruption. A012 is sole-source (Cipla only). Winter
peak means Salbutamol demand is highest. Affects only D017 and D018
(both Life-Critical/High respiratory drugs). All 10 hospitals fire
MEDIUM_RISK or HIGH_RISK. Both alternatives are blocked (shared API risk).
No viable substitution — procurement is the only resolution path.
This is the most dramatic single-drug scenario.

### Scenario C — Distributor S003 Logistics Failure / Medium
Minor channel disruption. 6-day recovery. Most hospitals have buffer.
Only thin-buffer hospitals for Low/Moderate drugs surface as MEDIUM_RISK
(Brufen at H002 with 3.8 days stock vs 6-day recovery). Demonstrates
that the system correctly stays silent for non-critical disruptions
rather than flooding the operator with false alarms.

---

# ADDENDUM — WHAT HAS BEEN BUILT AND DECIDED SINCE THIS DOCUMENT WAS WRITTEN

This section is additive. Everything above remains valid. This section
documents all architectural decisions, implementations, and clarifications
made after the original context above was authored. Any future LLM or
developer should read BOTH sections together.

---

## BUILD STATUS UPDATE

| Component | Status | Notes |
|-----------|--------|-------|
| sentinel.py | ✅ Complete | Validates, resolves node, calls prediction engine |
| prediction_engine.py | ✅ Complete | Prophet forecasting, scoring, SimulationSession |
| analyst.py | ✅ Complete | DrugAlertPackage builder with session.db stock override |
| procurement_agent.py | ✅ Complete | Two-path LLM agent (TOTAL_LOSS / PARTIAL_LOSS) |
| clinical_agent.py | ✅ Complete | Deterministic lookup, no LLM |
| aggregator.py | ✅ Complete | Merges outputs, writes to reviews.db |
| gnn_centrality.py | ✅ Complete | GNN Component 1 — betweenness + dependency scores |
| session_manager.py | ✅ Complete | Simulation session lifecycle + stock depletion |
| dashboard_api.py | ✅ Complete | Full FastAPI layer — all endpoints live |
| dashboard.html | ❌ Not built | The only remaining major component |
| GNN Components 2 & 3 | ❌ Deferred | HeteroConv, GAT — architecture designed, not implemented |
| factory_events_history.csv | ❌ Deferred | Validation only, not needed for current build |

---

## LLM MODEL CHANGE

The original context specified `gemini-2.5-flash` as the Procurement Agent LLM.
The implementation uses `gemma-4-26b-a4b-it` via Google AI Studio free tier.

Config location: `procurement_agent.py`, line ~49:
```python
GEMINI_MODEL = "gemma-4-26b-a4b-it"   # ← change model name here if needed
```

The free tier limit is **15 requests per minute (RPM)**. The pipeline staggers
LLM calls across drugs using a 10-second inter-batch delay. Do not reduce this
without understanding the RPM impact — reducing it will cause 429 errors that
trigger tenacity exponential backoff, making the pipeline appear hung for minutes.

The google-genai SDK is used (not openai-compatible). Install: `pip install google-genai`.

---

## GRAPH RELATIONSHIP NAMES — AUTHORITATIVE LIST

The original context listed these. Confirming they are correct and all exist in Neo4j:

| Relationship | Exists in Neo4j | Used by |
|---|---|---|
| PRODUCES_API | ✅ | GNN, prediction engine (Factory disruption) |
| COMPONENT_OF | ✅ | GNN, prediction engine (API disruption) |
| PRODUCES_DRUG | ✅ | GNN, prediction engine (fast drug lookup), dashboard graph |
| STOCKS | ✅ | GNN traversal only — prediction engine never queries this |
| DELIVERS_TO | ✅ | Analyst, prediction engine, dashboard graph (deduplicated) |
| NEEDS_DRUG | ✅ | GNN — **this is the real name, not USES** |
| ALTERNATIVE_TO | ✅ | Clinical Agent, dashboard graph (dashed edges) |

CRITICAL: `USES` does not exist in Neo4j. The correct name is `NEEDS_DRUG`.
Any code using `USES` in a Cypher query will silently return zero results.

The GNN graph projection includes 6 of the 7 types (all except ALTERNATIVE_TO):
PRODUCES_API, COMPONENT_OF, PRODUCES_DRUG, STOCKS, DELIVERS_TO, NEEDS_DRUG
All projected UNDIRECTED for betweenness centrality.

ALTERNATIVE_TO is excluded from GNN because it connects Drug→Drug
and does not represent a supply chain structural path.

---

## SESSION MANAGEMENT ARCHITECTURE

A new module (`session_manager.py`) was built to enable state-aware simulation.
This is what makes the dashboard interactive — stock depletes as operators
approve procurement packages, rather than resetting to baseline on every disruption.

### Two-database design

**`reviews.db`** — Permanent audit log. Never reset or deleted.
- Initialized at FastAPI server startup via `aggregator.init_db()` called in the FastAPI lifespan event.
- Accumulates all `review_packages` rows from all sessions and all disruption runs permanently.
- This is the historical record. Do not delete it between sessions or demos.
- Table: `review_packages` — see Aggregator section in original context above for full schema.

**`session.db`** — Ephemeral simulation sandbox. Lives for the duration of one session.
- Created when `POST /api/session/start` is called.
- Seeded from Neo4j baseline data (`DELIVERS_TO.currentStock`) and `hospital_inventory.csv`.
- Tables:
  - `session_info` (session_id, started_at, is_active)
  - `distributor_stock` (distributor_id, hospital_id, drug_id, current_stock, baseline_stock, min_order, delivery_days, price_per_unit)
  - `hospital_inventory` (hospital_id, drug_id, current_units, daily_demand, baseline_units)
- Reset to Neo4j baseline when `POST /api/session/end` is called.
- Reseeded fresh on every `POST /api/session/start`.

### Session lifecycle
```
POST /api/session/start
  → session_manager.start_session()
  → Seeds distributor_stock from Neo4j DELIVERS_TO.currentStock
  → Seeds hospital_inventory from hospital_inventory.csv
  → session.db now live

[User triggers disruptions, approves/rejects packages]

POST /api/session/end
  → session_manager.end_session()
  → Resets distributor_stock + hospital_inventory to baseline in session.db
  → Marks session inactive (is_active = 0)
  → reviews.db untouched — all packages remain
```

### Analyst hook for session state
`analyst.py` checks `session_manager.is_active()` before building DrugAlertPackages.
If a session is active, it calls `session_manager.get_distributor_stock(distributor_id, drug_id)`
to override the Neo4j `DELIVERS_TO.currentStock` value with the live session.db value.

This means: after a procurement is approved and stock depletes, the next disruption
simulation for the same drug will see the correct reduced stock level — not the
original Neo4j baseline. This is the core of "simulation continuity".

Distributors whose session.db stock is 0 are excluded from the analyst results entirely
(the Analyst filters zero-stock distributors from the available options list).

### Stock depletion on approval
When the operator approves a procurement package via `POST /api/packages/{package_id}/review`:
1. dashboard_api.py reads the approved `option_a` or `option_b` from the full_package JSON
2. Calls `session_manager.apply_depletion(option, drug_id)`
3. `apply_depletion` iterates hospital_allocations, subtracts `units_allocated` from
   the relevant distributor's `current_stock` in `session.db`
4. Updates both SQLite (session.db) and the in-memory state simultaneously

---

## FASTAPI LAYER — COMPLETE ENDPOINT REFERENCE

File: `dashboard_api.py`
Run: `uvicorn dashboard_api:app --reload` (or `python dashboard_api.py`)
Port: 8000
Docs: http://localhost:8000/docs (auto-generated Swagger UI)

### Startup behaviour
FastAPI lifespan event runs at server start:
- Calls `aggregator.init_db(REVIEWS_DB)` — creates reviews.db if not present, no-op if already exists.

### Endpoints

**Graph / Topology**
```
GET  /api/graph/nodes         → vis.js graph payload: { nodes, edges, legend }
GET  /api/graph/vulnerability → same topology + GNN scores per node
```

**Session lifecycle**
```
POST /api/session/start  → { success, session_id }
POST /api/session/end    → { success }
GET  /api/session/state  → { is_active, session_id, distributor_stock[], hospital_inventory[] }
```

**Disruption pipeline** (synchronous, blocks 3–10 min)
```
POST /api/session/run-disruption
  Body: { node_type, node_id, event_type, severity, month, day }
  → Runs full pipeline: Sentinel → Analyst → Agents → Aggregator → reviews.db
  → Returns: { success, triggered_date, total_packages, actionable, affected_drug_ids, db }
```

**Review packages**
```
GET  /api/packages           → list of all pending full_package dicts from reviews.db
GET  /api/packages/{id}      → one full_package dict
POST /api/packages/{id}/review
  Body: { action: "approve_a" | "approve_b" | "reject" }
  → Updates status in reviews.db
  → If approving: calls session_manager.apply_depletion() to deplete session stock
```

### Pipeline execution inside run-disruption
The endpoint is synchronous and blocking. It runs in a ThreadPoolExecutor
with BATCH_SIZE=2: two drugs processed in parallel at a time, then the next
two, and so on. This is a deliberate concurrency cap to manage LLM rate limits.

One disruption event that affects 5 actionable drugs takes roughly:
  ceil(5/2) = 3 batches × (LLM call time per drug) = 3–10 minutes total

**Frontend must show a loading/in-progress state during this call.**
The UI must NOT let the user trigger another disruption while one is running —
this would cause concurrent writes to session.db and corrupt the simulation state.
Disable the Disrupt button while the pipeline is active.

---

## GRAPH VISUALIZATION DECISIONS

For the dashboard's node graph (vis.js Network):

### Layout: HIERARCHICAL, not force-directed
Use:
```js
layout: {
  hierarchical: {
    direction: 'LR',
    sortMethod: 'directed',
    levelSeparation: 200,
    nodeSpacing: 80
  }
}
physics: { enabled: false }
```

Why: Neo4j has 1,939 total edges. Force-directed layouts are unstable with
dense many-to-many subgraphs (DELIVERS_TO: 1,640 edges). The supply chain IS
hierarchical (API — Factory — Drug — Distributor — Hospital), so hierarchical
layout is semantically correct AND performant.

### What /api/graph/nodes returns (after deduplication)

The endpoint reduces 1,939 raw Neo4j edges to ~200 for visualization:

| Relationship shown | Edges shown | Why |
|---|---|---|
| PRODUCES_API | 17 | All shown |
| COMPONENT_OF | 20 | All shown |
| PRODUCES_DRUG | 30 | All shown (shortcut, useful for visual) |
| STOCKS | ~164 deduplicated | One per (drug, distributor) pair |
| DELIVERS_TO | ~100 deduplicated | One per (distributor, hospital) pair — not 1,640 |
| ALTERNATIVE_TO | 32 | Shown as dashed lines with `shared_risk` flag |
| NEEDS_DRUG | 0 (hidden) | 200 edges would clutter, semantic info already in node tooltips |

### Node disruptability
- Disruptable (show Disrupt button on click): `Factory`, `API`, `Distributor`
- Display-only (informational only): `Drug`, `Hospital`

Each node in the response carries `disruptable: true/false`. The frontend
uses this flag — never hardcode which node types are disruptable.

### ALTERNATIVE_TO edges
Returned with `dashes: true` and `shared_risk: bool`.
The frontend should render blocked alternatives (shared_risk=true) in a
different colour or style to convey that the substitution is not viable
for that specific disruption scenario.

---

## MICRO_GAP_FAST_PATH

Located at top of `procurement_agent.py`:
```python
MICRO_GAP_FAST_PATH = False
```

When `True`: skips the LLM for drugs where the supply gap is smaller than
the minimum order quantity of every available distributor. In such cases
the answer is trivially "no procurement action needed" and an LLM call
adds nothing. Default is `False` (always calls LLM) for correctness
and benchmarking. Set to `True` in production to save API calls.

---

## PIPELINE INTERNAL FLOW CLARIFICATION

The Sentinel does NOT just validate — it also calls the prediction engine
internally and embeds the results in the DisruptionEvent dataclass:

```
process_disruption() → DisruptionEvent
  DisruptionEvent.results = List[ScoredPair]
  DisruptionEvent.affected_count = int
```

The Analyst receives this DisruptionEvent and reads `.results` directly.
It does NOT call the prediction engine again. The pipeline from
dashboard_api.py's perspective is:

```python
event    = process_disruption(...)           # Sentinel + Prediction Engine inside
packages = analyse(event)                    # Analyst reads event.results
# Then Procurement + Clinical in parallel
# Then Aggregator
```

This means the prediction engine is always invoked as part of Sentinel,
never called standalone by the dashboard. Do not add a separate prediction
engine call step in any new code.

---

## KNOWN GOTCHAS ACCUMULATED DURING DEVELOPMENT

1. **Tenacity silent retries**: google-genai SDK retries 429 errors with exponential
   backoff up to 60s per sleep. A "hung" drug thread is almost always a tenacity
   retry in progress, not a crash. Errors only surface after all 5 retry attempts fail.

2. **USES does not exist in Neo4j**. The correct relationship name is `NEEDS_DRUG`.
   Any Cypher query using `[:USES]` returns zero results silently.

3. **`DELIVERS_TO.currentStock` = distributor's TOTAL stock for that drug**, not
   per-hospital. The same stock value appears across all hospital rows for the same
   distributor-drug pair. Do not sum it across hospital rows.

4. **`DELIVERS_TO.deliveryDays` VARIES per hospital**. price_per_unit and min_order
   are the same regardless of hospital. Never average deliveryDays across hospitals.

5. **reviews.db accumulates across sessions.** The frontend must not assume it starts
   empty. Filter by `created_at` timestamp or display all packages with their status.

6. **session.db is empty until start_session() is called.** The tables exist (created
   by init_dbs.py) but contain no rows until the session is seeded from Neo4j.

7. **run-disruption is synchronous and blocking.** The HTTP response does not return
   until the entire pipeline (Sentinel → Aggregator) completes. This takes 3–10 minutes.
   The frontend must handle this with appropriate UX (loading state, no second submit).

8. **capacityShare is stored as a decimal** (0.72, not 72). The GNN and prediction
   engine treat it as a fraction [0,1]. Do not multiply by 100.

9. **Session state is shared across the entire running process.** `prediction_engine.SESSION`
   is a module-level singleton. If run-disruption is called twice concurrently, both
   calls mutate the same SimulationSession. This is a race condition. The frontend
   must serialize disruption calls (disable Disrupt while a run is in progress).

10. **test_runner.py writes to named test DBs, not reviews.db.** Running tests
    will never pollute the dashboard's review queue.

11. **S008 CriticalMed is the dicey case distributor.** It stocks only D001, D011,
    D015, D017. Express delivery but premium price and low stock (50–124 units).
    The Procurement Agent is expected to flag this as is_dicey_case=True when
    S008 is the fastest option but below minimum order.

12. **D001 Insulin has no alternative.** Clinical Agent immediately returns
    substitution_viable=False with no_alternative_reason="No alternatives in graph."

13. **D017 and D018 alternatives are always blocked.** Both drugs share A012 as
    their sole API, so every ALTERNATIVE_TO relationship between them has
    sharedApiRisk=True. When an A012 or Cipla (F002) disruption occurs, the
    Clinical Agent will always return substitution_viable=False for both.

---

## DASHBOARD USER JOURNEY (intended flow for frontend)

1. **Page load**: Call `GET /api/graph/nodes` → render vis.js hierarchical graph.
   Disruptable nodes (Factory, API, Distributor) show a visual affordance.

2. **Start session**: User clicks "Start Simulation" → `POST /api/session/start`.
   Store session_id in UI state. Graph now shows live simulation is active.

3. **Select a node**: User clicks a disruptable node on the graph.
   Side panel opens showing: node name, node type, available event types and severities
   from the disruption taxonomy, date picker (month + day).

4. **Trigger disruption**: User fills the form and clicks "Disrupt".
   `POST /api/session/run-disruption` — show loading spinner.
   Disable all Disrupt buttons while pipeline runs.

5. **Review packages appear**: When the call returns, call `GET /api/packages`
   to fetch newly created review_packages. Display as cards, one per affected drug.
   Each card shows: drug name, risk level, procurement recommendation, clinical assessment.

6. **Approve/Reject**: For each card:
   - If is_dicey_case=True: show Option A vs Option B. User picks one.
   - `POST /api/packages/{id}/review` with action "approve_a", "approve_b", or "reject"
   - On approval: session stock depletes automatically (backend handles this).
   - Card status updates to approved/rejected.

7. **Subsequent disruptions**: The graph now reflects depleted stock. A second
   disruption for the same drug will see reduced distributor availability.

8. **End session**: User clicks "End Simulation" → `POST /api/session/end`.
   Session stock resets to Neo4j baseline. reviews.db retains all packages.

---

## WHAT IS NOT BUILT (as of this addendum)

- **`dashboard.html`** — The only remaining major component. All backend APIs
  are live and tested. The dashboard needs to be built as a client-side web page
  that calls the FastAPI endpoints listed above. No backend changes are expected.
  Recommended libraries: vis.js (graph), vanilla JS or lightweight framework.

- **GNN Components 2 and 3** — HeteroConv message-passing (Component 2) and
  Graph Attention Network (Component 3) — architecture designed, not implemented.
  These are research extensions, not required for the functional dashboard.

- **Concurrency guard on run-disruption** — The current implementation has no
  server-side lock preventing concurrent pipeline runs. This must be handled
  client-side: disable the Disrupt UI element while a run is in progress.

---

# ADDENDUM 2 — OPERATIONAL DETAILS, CORRECTIONS, AND EDGE CASES

---

## HARDCODED CREDENTIALS AND CONFIG — EVERY LOCATION

Neo4j credentials are **hardcoded in 6 separate files**. All use the same values.
If the database credentials change, all 6 must be updated:

| File | Lines | What it is |
|---|---|---|
| `analyst.py` | 28–30 | Module-level Neo4j driver (instantiated at import) |
| `sentinel.py` | 44–46 | Module-level Neo4j driver |
| `prediction_engine.py` | 48–50 | Module-level Neo4j driver |
| `session_manager.py` | 39–41 | Used in `_neo4j_all_stock()` for session seeding |
| `gnn_centrality.py` | 66–68 | Module-level driver, opened at script run |
| `dashboard_api.py` | 362–364 | Used in `/api/graph/nodes` and `/api/graph/vulnerability` |

Values (current):
```
NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"
```

Gemini API key — hardcoded in `procurement_agent.py` line ~48:
```python
GEMINI_API_KEY = "AIzaSyBcEeu5EgjkmYpDVy2ej1HUijEu8BdmlrE"
```

---

## CORRECTIONS TO THE ORIGINAL CONTEXT

### package_id format
The original context states:
  `package_id = "{disrupted_node}-{drug_id}-{triggered_date}"`

The actual implementation in `aggregator.py` line 271 is:
  `package_id = "{disrupted_node}-{drug_id}-{event_type}-{triggered_date}"`

The `event_type` is included. The original context was wrong.
Example: `"F002-D017-Disaster-2026-08-15"`

### aggregator.py default DB_PATH
The module-level default at the top of `aggregator.py` is:
  `DB_PATH = "disruption_reviews.db"`

This legacy default is never used in practice. `dashboard_api.py` always
passes `db_path=REVIEWS_DB` (which resolves to `reviews.db`) explicitly.
If calling `aggregator.aggregate()` or `aggregator.init_db()` directly
without a `db_path` argument, it will write to `disruption_reviews.db`
instead of `reviews.db`. Always pass `db_path` explicitly.

---

## PROJECT SETUP REQUIREMENTS

### Neo4j
- Must be running locally before ANY pipeline module can function.
- Neo4j Desktop or Community Edition. Port 7687 (default Bolt).
- The graph must have been populated (via the scripts in `setups/`) before use.
- `gnn_centrality.py` must have been run at least once to populate
  `vulnerabilityScore`, `centralityScore`, `dependencyScore` on all nodes.
  Until then, `/api/graph/vulnerability` returns 0.0 for all scores (coalesce fallback).

### Python environment
- Conda environment: `ml_env`
- Key packages: `neo4j`, `google-genai`, `prophet`, `pandas`, `fastapi`, `uvicorn`, `tenacity`

### Prophet models
- Location: `./prophet_models/` — 200 pkl files
- Naming: `{hospital_id}_{drug_id}.pkl` — e.g. `H001_D004.pkl`
- If a model file is missing, the prediction engine raises an exception for that pair and skips it.
- Pre-trained. NOT regenerated at runtime. Must exist before any run.

### The `setups/` directory
Contains the Neo4j data injection scripts that populated the graph from CSVs.
These are setup-only — not used at runtime. Re-run only if Neo4j needs wiping and repopulation.

---

## RUNTIME CSV DEPENDENCIES

Only two CSVs are read during actual pipeline execution at runtime:

| CSV | Read by | When | Why |
|---|---|---|---|
| `hospital_inventory.csv` | `prediction_engine.py` | At module import (startup) | Loads BASE_DATA.inventory_base, seeds SimulationSession |
| `disruption_taxonomy.csv` | `sentinel.py` | At module import (startup) | Loads taxonomy dict for recovery_days lookup |

All other CSVs are setup/reference data only — NOT read at runtime:
- `demand_history.csv` (73,200 rows) — Prophet training data only
- `distributor_catalogue.csv` — used to build Neo4j graph; session_manager reads DIRECTLY from Neo4j
- `distributor_drug_stock.csv`, `distributor_drug_prices.csv` — reference only
- `drugs.csv`, `hospitals.csv`, `factories.csv`, `distributors.csv` — all data lives in Neo4j now
- `factory_api_map.csv`, `api_drug_map.csv`, `alt_drug_map.csv` — used to build Neo4j graph only
- `city_distance.csv` — used to derive DELIVERS_TO.deliveryDays; now in Neo4j

---

## SESSION STATE RESPONSE STRUCTURE

`GET /api/session/state` returns:
```json
{
  "is_active": true,
  "session_id": "uuid-string",
  "started_at": "2026-08-15T10:00:00",
  "distributor_stock": [
    {
      "distributor_id": "S001", "hospital_id": "H001", "drug_id": "D004",
      "current_stock": 1200.0, "baseline_stock": 1200.0,
      "min_order": 50.0, "delivery_days": 3.0, "price_per_unit": 4.5
    }
  ],
  "hospital_inventory": [
    {
      "hospital_id": "H001", "drug_id": "D004",
      "current_units": 450.0, "daily_demand": 15.0, "baseline_units": 450.0
    }
  ]
}
```

If no session is active: `{ "is_active": false }`

`start_session()` raises `RuntimeError` if a session is already active.
Frontend should check `is_active` before calling `POST /api/session/start`.

---

## COVERAGE STATUS — EXACT MEANINGS

From `aggregator._compute_hospital_coverage()`:

| Status | Meaning |
|---|---|
| `FULL` | units_acquired >= units_required — order covers this hospital's need |
| `PARTIAL` | 0 < units_acquired < units_required — partial coverage, gap remains |
| `NONE` | units_acquired == 0 — no procurement order covers this hospital |
| `COVERED_BY_FACTORY` | PARTIAL_LOSS only — remaining factory supply covers this hospital; no order needed |

Frontend colour guidance: FULL/COVERED_BY_FACTORY → green, PARTIAL → amber, NONE → red.

Clinical suppression triggers when ALL actionable hospitals are FULL or COVERED_BY_FACTORY.

---

## CLINICAL AGENT — EDGE CASE FIELDS

### `requires_physician_approval`
True when `similarity_tier == "last_resort"` (score < 0.70) OR
alternative drug criticality is lower than primary drug criticality.
Frontend must display a prominent warning. Operator must route to physician before implementation.

### `critically_urgent_hospitals`
List of hospital_ids where `days_until_stockout ≤ 7`.
Frontend should highlight these hospitals with an emergency indicator (red badge, "CRITICAL" label).

### `similarity_tier` values
- `near_identical` (≥ 0.90): same drug, different dose — dose equivalence caveat
- `viable` (0.70–0.89): good substitute, standard caveats
- `last_resort` (< 0.70): different mechanism — physician sign-off required

### `system_wide_caveats`
List of plain-English strings. Always surface these in the clinical section of the card.

---

## PROCUREMENT AGENT — EDGE CASE FIELDS

### `parse_ok`
True if LLM returned valid JSON. False if parsing failed.
When False: treat `procurement_viable` as False. Show "LLM response could not be parsed" error state.

### `procurement_viable`
Can be False even when `parse_ok = True` — means LLM found no viable procurement path
(all distributors out of stock, all blocked, etc.).
Frontend: show "No procurement action available" and do not render option_a/option_b.

### `dicey_tradeoff`
Plain English tradeoff description when `is_dicey_case = True`.
Example: "S008 arrives in 5 days but requires negotiation for below-min-order. S010 meets
min-order but arrives in 11 days — after H009's 8-day stockout window."
Show this text prominently when presenting Option A vs Option B.

### `call_count`
Number of LLM calls made (1 for TOTAL_LOSS, 2 for PARTIAL_LOSS, 0 if micro-gap fast-path).

### `hospitals_covered_by_factory` (PARTIAL_LOSS only)
Hospital_ids in the allocation dict that don't need bridge orders.
These hospitals are covered by remaining factory supply. Do not flag them as unaddressed.

---

## ACTION SUMMARY AND ACTION REQUIRED

`full_package["action_summary"]` — pre-built plain-English one-liner from
`aggregator._build_action_summary()`. Use directly as review card subtitle.
Examples:
- `"Procurement covers all hospitals | no viable substitution"`
- `"2 hospital(s) uncovered | substitution available: Ventolin | dicey case — two options presented"`
- `"Procurement not viable | physician sign-off required"`

`full_package["action_required"]` — True if any hospital has `requires_action=True`
(MEDIUM+ risk). False for LOW_RISK packages which are monitoring alerts, not actionable cards.

---

## FILES THAT CAN BE DELETED (cleanup)

These were created during development and are no longer needed:

| File | Reason |
|---|---|
| `count_edges.py` | One-off script to count graph edges. Done. |
| `init_dbs.py` | One-off DB initialization. DBs exist; server auto-inits reviews.db on startup. |
| `dump_prompt.py` | Debug script for inspecting LLM prompts. |
| `scratch_dump_prompt.py` | Same, scratch version. |
| `inspect_components.py` | Debug utility for Neo4j inspection. |
| `inspect_db.py` | Debug utility for SQLite inspection. |
| `procurement_result.json` | Leftover output dump from a test run. |

Core files to keep: `aggregator.py`, `analyst.py`, `clinical_agent.py`,
`dashboard_api.py`, `gnn_centrality.py`, `prediction_engine.py`,
`procurement_agent.py`, `sentinel.py`, `session_manager.py`, `test_runner.py`

