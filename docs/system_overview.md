# LY Project — System Reference

> **Canonical reference for the LY Project.**
> An LLM agent or developer should read this end-to-end before making any changes.
> It covers the problem, every design decision and the reasoning behind it, and the precise technical map of every module.

---

## 1. What This Is

Hospitals depend on a supply chain to keep medicines stocked. When any part of that chain breaks — a factory disaster, a distributor going offline, a raw material shortage — hospitals run out of drugs and patients are harmed. No one has a real-time view of which hospitals will run out of which drugs when this happens. Decisions are made manually, slowly, and without data.

**What we built:** An automated decision-support system that, the moment a disruption is reported:
1. Traces which hospitals are at risk and calculates exactly how many days of stock they have left
2. Recommends an emergency procurement plan — which distributor, how many units, for which hospital
3. Suggests a clinical substitute if no procurement is possible
4. Shows everything on a dashboard for a human to approve or reject

**The system is not autonomous.** It never auto-executes anything. Agents propose, never decide. Every recommendation is queued for human approval. This is intentional — healthcare decisions must be explainable and accountable.

The supply chain breaks at three points:
- **Factory** goes down (strike, disaster, equipment failure) → can't produce the drug
- **Distributor** goes down (logistics failure, suspension) → drug exists but can't reach hospitals
- **API supply** is disrupted (raw material shortage, export ban) → factory has no ingredient to work with, even if the factory itself is running

---

## 2. Graph Database — Neo4j

The supply chain is stored as a graph in Neo4j — a database built for connected, relationship-heavy data. A relational database would need 4–5 JOINs to answer "which hospitals are at risk if Factory F001 goes down?" A graph traverses those hops in a single query. The structure also lets the GNN learn from connection patterns.

```
Credentials loaded from .env (see .env.example):
NEO4J_URI=neo4j://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your password>
```

### Graph size
- **57 nodes** — 5 Factories, 12 APIs, 20 Drugs, 10 Distributors, 10 Hospitals
- **1,939 relationships** total

### Node types and key properties

| Node | IDs | Key Properties |
|------|-----|---------------|
| `Factory` | F001–F005 | id, name, city, monthlyCapacity, reliabilityScore |
| `API` | A001–A012 | id, name, category, complexityFactor |
| `Drug` | D001–D020 | id, name, criticality, category, vulnerabilityScore, basePrice |
| `Distributor` | S001–S010 | id, name, city, reliabilityScore, pricingTier, deliverySpeed, vulnerabilityScore |
| `Hospital` | H001–H010 | id, name, city, specialtyType, avgDailyPatients |

### Relationship types and key properties

| Relationship | Count | Key Properties |
|-------------|-------|----------------|
| `(Factory)-[:PRODUCES_API]->(API)` | 17 | capacityShare, monthlyOutput |
| `(API)-[:COMPONENT_OF]->(Drug)` | 20 | yieldMultiplier |
| `(Factory)-[:PRODUCES_DRUG]->(Drug)` | 30 | derived shortcut (via API chain) |
| `(Drug)-[:STOCKS]->(Distributor)` | 164 | derived from DELIVERS_TO catalogue |
| `(Distributor)-[:DELIVERS_TO]->(Hospital)` | 1,640 | **drugId, currentStock, deliveryDays, pricePerUnit, minOrder** |
| `(Hospital)-[:NEEDS_DRUG]->(Drug)` | 200 | dailyDemand, currentUnits, daysOfStock |
| `(Drug)-[:ALTERNATIVE_TO]->(Drug)` | 32 | similarityScore, sharedApiRisk |

### Supply chain direction

```
API → supplied to → Factory → produces → Drug → distributed by → Distributor → delivered to → Hospital
```

APIs are raw chemical ingredients sourced and sent to factories. The factory formulates the API into the finished drug. Disrupting the API source stops production even if the factory itself is running.

`PRODUCES_DRUG` is a derived shortcut (Factory→Drug) used for fast graph traversal — the authoritative production path is `PRODUCES_API` + `COMPONENT_OF`. `capacityShare` is stored as a decimal: 0.72 = this factory controls 72% of global supply for that API.

### Critical data facts

- `DELIVERS_TO.currentStock` = distributor's total stock for a drug. Same value across all hospitals for the same distributor-drug pair. **This is the live value read by the pipeline** — during a session it is overridden by `session.db`, not read from Neo4j.
- `DELIVERS_TO.deliveryDays` varies per hospital (distance-based). `minOrder` and `pricePerUnit` are the same per distributor regardless of hospital.
- `DELIVERS_TO.drugId` is a property on the relationship — Drug and Distributor are not directly linked as nodes (hence the derived `STOCKS` relationship).
- `NEEDS_DRUG` is the correct relationship name for Hospital→Drug. `USES` does not exist in this graph — any Cypher query using `[:USES]` returns zero results silently.

### CSV Datasets (flat reference data)
Location: `./data/`

| File | Content |
|------|---------|
| `hospitals.csv` | Hospital metadata |
| `factories.csv` | Factory metadata |
| `drugs.csv` | Drug metadata (criticality, category, seasonality, basePrice) |
| `distributors.csv` | Distributor metadata |
| `apis.csv` | API metadata |
| `hospital_inventory.csv` | Hospital×Drug: daily_demand, current_units, days_of_stock |
| `hospital_drug_demand.csv` | Hospital×Drug demand (used for Neo4j seeding) |
| `distributor_catalogue.csv` | Full distributor×hospital×drug relationship data (stock, price, delivery, MOQ) |
| `demand_history.csv` | Historical daily demand per hospital per drug — Prophet training data |
| `disruption_taxonomy.csv` | **(single source of truth)** (node_type, event_type, severity) → (min_days, max_days) recovery |
| `api_drug_map.csv` | API → Drug component mappings with yieldMultiplier |
| `factory_api_map.csv` | Factory → API production mappings with capacityShare |
| `alt_drug_map.csv` | Drug → clinical alternative mappings with similarityScore, sharedApiRisk |

---

## 3. The Dataset

Everything is **synthetic** — generated to match real Indian pharmaceutical supply chain structure. Real data is proprietary and not publicly available. The synthetic dataset is structured to reflect reality: factories with different capacity shares, regional distributors, varied hospital specialties, demand with realistic base rates. The system would work identically on real data — the architecture doesn't change.

### Hospitals (10)
Real Indian hospitals as reference points: AIIMS Delhi, Apollo Chennai, Fortis Gurgaon, etc. Each has a specialty focus, city, and average daily patient count.

### Drugs (20)
Span 5 disease categories: Diabetes, Cardiac, Antibiotics, Painkillers, CNS.

| Criticality | Meaning | Example |
|------------|---------|---------|
| Life-Critical | Patient dies without it | Lantus (Insulin Glargine) |
| High | Serious harm without it | Amoxil, Ciplox |
| Moderate | Manageable short-term | Glycomet, Paracetamol |
| Low | Inconvenient, not dangerous | Vitamins |

### Demand history
1 year of daily per-hospital per-drug demand (`data/demand_history.csv`) — used to train 200 Prophet models, one per (hospital, drug) pair.

### Prophet Models
Location: `ml/models/prophet_models/`
Format: `{hospital_id}_{drug_id}.pkl` — 200 files total.
Trained on `demand_history.csv` through 2024-12-31.
Generate with: `python scripts/train_prophet_models.py`

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser / React Dashboard                                      │
│  node_type, node_id, event_type, severity, month+day           │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  1. SENTINEL  (core/sentinel.py)                                │
│  • Validate inputs and resolve node in Neo4j                    │
│  • Look up recovery_days from disruption taxonomy CSV           │
│  • Compute supply_loss_pct                                      │
│  • Return DisruptionEvent dataclass                             │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. PREDICTION ENGINE  (core/prediction_engine.py)              │
│  • Neo4j graph traversal → all affected (hospital, drug) pairs  │
│  • Prophet ML demand forecast per pair                          │
│  • shortage_probability = supply_loss × demand × time_factor    │
│  • Risk classification: HIGH / MEDIUM / LOW / NO_RISK           │
│  • Owns SimulationSession (in-memory live inventory)            │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. ANALYST  (core/analyst.py)                                  │
│  • Group pairs by drug_id                                       │
│  • Build DrugAlertPackage per drug                              │
│  • Batch-fetch Neo4j context: hospital meta, distributor opts   │
│  • Override distributor stock from session.db if active  ──────────► session.db
│  • Return List[DrugAlertPackage]                                │
└───────────────┬───────────────────────────────┬─────────────────┘
                │                               │
                ▼                               ▼
┌──────────────────────────────┐   ┌──────────────────────────────┐
│  4. PROCUREMENT AGENT        │   │  5. CLINICAL AGENT           │
│  (core/procurement_agent.py) │   │  (core/clinical_agent.py)    │
│  • TOTAL LOSS → 1 LLM call   │   │  • HIGH_RISK drugs only      │
│  • PARTIAL LOSS → 2 LLM calls│   │  • Deterministic Python      │
│  • Micro-gap → fast-path     │   │  • Substitution viability    │
│  • Option A + Option B       │   │  • Physician sign-off flag   │
└──────────────┬───────────────┘   └──────────────┬───────────────┘
               │                                  │
               └──────────────┬───────────────────┘
                              │  (parallel via ThreadPoolExecutor)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  6. AGGREGATOR  (core/aggregator.py)                            │
│  • Merge procurement + clinical results per drug                │
│  • Compute hospital coverage: ALLOCATED / PARTIAL / ZERO        │
│  • Build full_package JSON blob                                 │
│  • Write to reviews.db              ───────────────────────────────► session.db
│                                         (apply_depletion on approval)
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  7. reviews.db  — review_packages table                         │
│  status: pending_review → approved / rejected                   │
│  Human reviewer approves/rejects via dashboard                  │
└─────────────────────────────────────────────────────────────────┘
```

### Input Parameters

```python
process_disruption(
    node_type  = "Factory" | "Distributor" | "API",
    node_id    = "F001"–"F005"   # Factories
               | "S001"–"S010"   # Distributors
               | "A001"–"A012",  # APIs

    # Valid event_type per node_type — enforced by Sentinel, mismatch raises SentinelError:
    # Factory:     Disaster | Equipment Failure | Strike | License Hold | Raw Material Shortage
    # Distributor: Logistics Failure | Strike | License Suspension | Storage Failure | Disaster
    # API:         Raw Material Shortage | Supply Chain Failure

    event_type     = <see above>,
    severity       = "High" | "Medium" | "Low",
    triggered_date = "YYYY-MM-DD"  # Year is synthetic — only month+day matters for Prophet
)
```

---

## 5. GNN Vulnerability Scoring

**File:** `ml/gnn_centrality.py` — run once at setup, not during live pipeline.

The GNN assigns every node a structural risk score before any disruption happens. This is the proactive part of the system — showing which nodes are inherently dangerous to lose, independent of any specific event.

### Three scores per node

**centralityScore** — how central is this node in the network?
Computed using Neo4j GDS betweenness centrality on the full graph (all 5 node types, all relationship types, undirected). Normalised per node type to [0, 1].

**dependencyScore** — how irreplaceable is this node?

| Node type | Formula |
|-----------|---------|
| Factory | `max(max_capacity_share, num_apis_produced / total_apis)` |
| API | `base_dep × max_criticality_weight_of_downstream_drugs` |
| Drug | `max(dependencyScore of its source APIs)` |
| Distributor | `1 − (drugs_carried / total_drugs)` |
| Hospital | 0.5 uniform |

Where `base_dep` for API = 1.0 if sole producer, else `1 − min_capacity_share`.
Criticality weights: Life-Critical=1.0, High=0.75, Moderate=0.50, Low=0.25.

**vulnerabilityScore** — the combined score written to Neo4j:
```
vulnerabilityScore = 0.2 × centralityScore + 0.8 × dependencyScore
```

Dependency is weighted 4× more than centrality because **irreplaceability matters more than network position** for supply chain risk. A factory that produces a niche drug for 2 hospitals is more dangerous to lose than a well-connected factory that has 3 substitutes.

These scores are written permanently to Neo4j and surfaced in the dashboard's GNN vulnerability graph toggle.

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `project_graph()` | public | Projects the supply graph into GDS memory |
| `run_betweenness()` | public | Runs betweenness centrality, normalises per label, writes `centralityScore` |
| `compute_dependency_scores()` | public | Scores each node's supply dependency weight |
| `compute_vulnerability_score()` | public | Combines centrality + dependency → `vulnerabilityScore` on each node |
| `print_report()` | public | Prints top vulnerable nodes per type |
| `spot_checks()` | public | Validates specific known-high-risk nodes have expected high scores |

---

## 6. Demand Forecasting & Risk Scoring

**File:** `core/prediction_engine.py`

### Why Prophet, not LSTM or ARIMA

Prophet is purpose-built for business time series with seasonality. It requires far less data than LSTM (which needs thousands of points), handles missing data gracefully, and is interpretable — you can see the trend and seasonal components separately. With 1 year of daily data per hospital-drug pair, Prophet is the right tool. ARIMA requires manual stationarity preprocessing and doesn't handle seasonality as cleanly. LSTM would need 2–3 years minimum and offers no interpretability benefit here.

### Three-pass architecture

**Pass 1 — `get_affected_pairs()`**
Neo4j graph traversal. Depending on the disruption node type:
- Factory → all drugs the factory produces via APIs → all hospitals that receive those drugs
- Distributor → all drugs that distributor delivers → hospitals it serves (excluding that distributor from future options)
- API → all factories that produce that API → all downstream drugs and hospitals

**Pass 2 — `compute_drug_level_metrics()`**
Runs Prophet for every affected (hospital, drug) pair. Aggregates system-wide demand pressure per drug:
```
demand_pressure = total_system_forecast_30d / drug_units_remaining
```
Capped at 1.0. If remaining = 0, demand_pressure = 1.0.

**Pass 3 — `calculate_shortage_probability()`**
Scores each individual (hospital, drug) pair using precomputed drug-level metrics.

### The shortage probability formula

```
shortage_probability = supply_loss_pct × demand_pressure × time_factor
```

All three components are bounded [0, 1]. Maximum score = 1.0.

**supply_loss_pct** — fraction of global drug supply lost:
- Factory disruption: `this_factory_output / global_total` (per API, converted via yieldMultiplier)
- API disruption: sum of offline factory outputs / global total. If no factory offline, full supply lost.
- Distributor disruption: forced to 1.0 — that channel is entirely gone

**demand_pressure** — system-wide, computed once per drug and shared across all hospitals:
```
demand_pressure = total_system_forecast_30d / drug_units_remaining   (capped at 1.0)
```

**time_factor** — per-hospital urgency:
```
time_factor = 1 - (days_until_stockout / recovery_days)   (clamped to [0, 1])
```
If a hospital stocks out on day 10 and recovery is day 30, time_factor = 0.67. If it never stocks out before recovery, time_factor ≤ 0 → clamped to 0 → NO_RISK.

**Days until stockout:**
```
effective_daily_rate = prophet_forecast_30d / 30
days_until_stockout  = current_units / effective_daily_rate
```

### Risk tiers

| Score | Tier | Action |
|-------|------|--------|
| 0.0 | NO_RISK | Nothing |
| 0 < score < 0.20 | LOW_RISK | Monitor only |
| 0.20 ≤ score < 0.50 | MEDIUM_RISK | Procurement review |
| score ≥ 0.50 | HIGH_RISK | Procurement + Clinical review |

### SimulationSession

The `SESSION` singleton (module-level in `prediction_engine.py`) manages mutable in-memory state across the pipeline:

```python
SESSION.inventory           # dict: (hosp_id, drug_id) → {current_units, daily_demand}
SESSION.factory_status      # dict: factory_id → "offline" / "online"
SESSION.reset()             # restore inventory to BASE_DATA baseline
SESSION.deplete_inventory() # deduct units from a hospital
SESSION.restock_inventory() # add units to a hospital
```

`SESSION.reset()` is called at the end of each test case in `test_runner.py`. `session_manager` persists `SESSION.inventory` to `session.db` so stock state survives HTTP requests.

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `SimulationSession` | class | Singleton holding mutable hospital inventory |
| `run_prediction_pipeline()` | **public entry point** | Graph traversal → Prophet forecasts → scoring → sorted risk list |
| `get_affected_pairs()` | public | Neo4j traversal per node type; returns all (hospital, drug) pairs |
| `compute_drug_level_metrics()` | public | Per-drug: system forecast, drug_units_remaining, demand_pressure |
| `calculate_shortage_probability()` | public | Scores one (hospital, drug) pair |
| `get_prophet_forecast()` | public | Loads pkl model, forecasts 30-day demand |
| `load_base_data()` | public | Reads CSVs into memory. Called once at startup. |
| `classify_risk()` | public | Converts probability score → HIGH/MEDIUM/LOW/NO_RISK |
| `load_taxonomy()` | public | Loads disruption_taxonomy.csv into dict at module startup |
| `BASE_DATA` | module-level dict | Loaded-once flat reference data |
| `SESSION` | module-level singleton | Live `SimulationSession` instance |

---

## 7. Stage 1 — Sentinel

**File:** `core/sentinel.py`

First stage of the pipeline. Validates the disruption input, confirms the node exists in Neo4j, looks up recovery time, and returns a `DisruptionEvent` dataclass that all downstream stages consume.

### Why taxonomy validation is a hard stop

Early versions let invalid event types through with a soft warning. The problem: if `node_type=Factory` is sent with `event_type=Logistics Failure` (which only applies to Distributors), the recovery_days lookup falls back to severity defaults and the system produces a plausible-looking but wrong result — the reviewer has no idea the event type was invalid. Making it a hard stop (`SentinelError`) forces the caller to send a valid combination. Errors surface at the boundary, not buried in downstream calculations.

### Hard stops vs soft warnings

**Hard stops (pipeline aborts):**
- Invalid node_type
- Node ID not found in Neo4j
- Invalid severity
- Event type not valid for this node_type (per taxonomy CSV)

**Soft warnings (pipeline continues):**
- Node already offline in current session (compound disruption — noted but not blocked)

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `DisruptionEvent` | dataclass | Carries all event metadata (node_id, event_type, severity, recovery_days, supply_loss_pct) through the pipeline |
| `SentinelError` | exception | Hard validation failure — pipeline stops |
| `process_disruption()` | **public entry point** | Validates, resolves node, looks up taxonomy, returns `DisruptionEvent` |
| `_validate_node_type()` | private | Checks node_type is Factory/Distributor/API |
| `_validate_severity()` | private | Checks severity is High/Medium/Low |
| `_validate_date()` | private | Validates date format, defaults to today |
| `_resolve_node()` | private | Confirms node exists in Neo4j, returns its name |
| `_check_taxonomy()` | private | Raises `SentinelError` if event_type not valid for node_type per taxonomy CSV |
| `_check_compound_disruption()` | private | Warns if node already offline (compound scenario) |

---

## 8. Stage 3 — Analyst

**File:** `core/analyst.py`

Receives the `DisruptionEvent` and organises prediction results into per-drug packages ready for the LLM agents.

For each drug:
- Groups all (hospital, drug) pairs
- Sets `overall_risk_level` = highest tier across all hospitals
- `invoke_procurement = True` if any hospital is MEDIUM_RISK or higher
- `invoke_clinical = True` if any hospital is HIGH_RISK
- Fetches from Neo4j: drug + API context, hospital metadata, distributor options, alternative drugs

**Key fields on `DrugAlertPackage`:**
`drug_id, drug_name, criticality, vulnerability_score, disruption details, supply_loss_pct, demand_pressure, drug_units_remaining, system_total_forecast, overall_risk_level, invoke_procurement, invoke_clinical, hospitals (List[HospitalRisk]), api_context, alternatives`

**Key fields on `HospitalRisk`:**
`hospital_id/name/city/specialty, shortage_probability, risk_level, requires_action, days_until_stockout, prophet_forecast_30d, time_factor, distributors (list with delivery_days, current_stock, min_order, price_per_unit)`

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `HospitalRisk` | dataclass | One hospital's risk data + available distributors |
| `DrugAlertPackage` | dataclass | Full context for one drug — passed to both agents |
| `analyse()` | **public entry point** | Groups results, builds packages, returns `List[DrugAlertPackage]` sorted HIGH first |
| `_build_drug_alert_package()` | private | Builds one DrugAlertPackage with all Neo4j context |
| `_fetch_distributor_options()` | private | Batch Neo4j query for distributor options. **Calls `session_manager.override_analyst_stock()`** when session active |
| `_fetch_hospital_context()` | private | Batch Neo4j query for hospital metadata |
| `_fetch_drug_context()` | private | Neo4j query for API vulnerability + clinical alternatives |
| `_determine_overall_risk()` | private | Returns highest risk tier across all hospitals |

---

## 9. Stage 4 — Procurement Agent

**File:** `core/procurement_agent.py`

Takes a `DrugAlertPackage`, builds a structured LLM prompt, calls Gemini, and returns Option A (Universal Coverage) and Option B (Ruthless Triage) allocations.

### Why an LLM, not linear programming

LP could find a mathematically optimal allocation number, but:
- LP gives a number, not a justification — reviewers can't understand *why* Hospital A was prioritised over B
- LP can't handle soft constraints like "below minimum order — flag for negotiation"
- LP can't identify and explain dicey tradeoffs between genuinely close options
- LP can't generate human-readable caveats

In healthcare, a reviewer needs to understand and trust the recommendation before approving it. The LLM produces a recommendation with a written justification, caveat flags, and dicey-case explanations. Mathematical optimality is sacrificed for transparency and accountability.

### The two-call architecture

**Step 1 — Python triage (no LLM)**
Python computes which hospitals need a bridge order and how many units:
```python
exposed_days  = recovery_days - days_until_stockout
bridge_units  = (prophet_forecast_30d / 30) × exposed_days
```
Hospitals that won't stock out before factory recovery are excluded.

**Step 2 — LLM bridge order (Call 1)**
The LLM receives the gap hospitals, their bridge needs, and distributor options. It must:
1. Write an `option_a_scratchpad` — tracking the global stock pool as it depletes
2. Assign distributors to hospitals (Option A: universal coverage)
3. Return `option_a_assignments: {hospital_id: [distributor_id, ...]}`
4. If a real tradeoff exists, also return `option_b_assignments` and set `is_dicey_case: true`

**Step 3 — Scratchpad extraction (Call 2)**
A second lightweight Gemini call extracts exact unit quantities from the Call 1 scratchpad as structured JSON.

**Why two calls, not one?**
The first call needs to reason freely — it writes a detailed scratchpad, assigns distributors, and narrates a justification. Doing both reasoning and strict JSON output in one call reliably produces malformed JSON because the model tries to reconcile narrative reasoning with schema constraints simultaneously. Separating them — Call 1 = think and assign, Call 2 = extract — is cleaner and more reliable.

**Why no regex fallback for Call 2?**
A regex fallback was tried and removed. It caused a catastrophic misread: the scratchpad uses notation like `S001 (6375)` to show opening stock. The regex treated `6375` as the unit cap for that distributor — a hospital received 6,375 units when it needed 406, draining the entire pool and leaving 6 hospitals with NONE status despite 103% system-wide coverage. If Call 2 fails, the correct response is to retry the LLM call, not parse it differently. Call 2 now retries up to 2 times (4s / 8s backoff). If all retries fail → `api_error: True` → dashboard shows a Retry button.

**Python's role in quantity computation:** The LLM only decides who gets which distributor. Python's `_execute_order()` computes all quantities — units allocated, delivery dates, gap days, costs. The LLM never does arithmetic that gets trusted directly.

### The dicey case

When two distributors are genuinely close in merit — e.g. faster delivery but below minimum order quantity vs slower but meets MOQ and is cheaper — the LLM returns both options and sets `is_dicey_case: true` with a written `dicey_tradeoff` explanation. The dashboard shows both and the reviewer chooses. This prevents the system from presenting a 51/49 ambiguous decision as if it were confident.

### LLM paths

**TOTAL LOSS path** (`supply_loss_pct = 1.0`)
- 1 LLM call
- All hospitals compete for the same distributor pool

**PARTIAL LOSS path** (`supply_loss_pct < 1.0`)
- Call 1: Bridge order (Option A + B assignments)
- Call 2: Unit cap extraction from scratchpad (retried up to 2× on failure)

**Micro-gap fast-path** (`MICRO_GAP_FAST_PATH = True`)
- Condition: `total_bridge_needed < min(all distributor MOQs)`
- Python resolves deterministically (no LLM) — picks highest-stock in-time distributor
- Eliminates 2–7 min LLM hangs for trivial cases where every option is below MOQ anyway

### Parallelism
- All actionable drugs run simultaneously via `ThreadPoolExecutor`
- 10-second stagger between LLM submissions to stay under 15 RPM (Gemini free tier)
- Each thread reuses one `genai.Client` via `threading.local()` — created once per worker thread, eliminating cold-start overhead
- Per-drug `try/except` — one failure prints `[✗]` and pipeline continues

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `run_procurement_agent()` | **public entry point** | Decides path (TOTAL/PARTIAL/micro-gap), calls LLM, returns procurement result dict |
| `_call_gemini()` | private | One LLM API call, reuses per-thread client via `threading.local()` |
| `_build_bridge_prompt()` | private | Assembles full LLM prompt |
| `_build_task()` | private | Task/instruction section of prompt |
| `_disruption_block()` | private | Disruption context block |
| `_drug_block()` | private | Drug metadata block |
| `_hospitals_with_distributors_block()` | private | Per-hospital section with nested distributor options |
| `_execute_order()` | private | Converts LLM assignment JSON into fully computed order list |
| `_parse_json()` | private | Robust JSON extraction (handles markdown fences, partial JSON) |
| `_strip_scratchpads()` | private | Removes scratchpad keys before final parse |
| `print_procurement_result()` | public | Pretty-prints Option A/B allocation tables |
| `MICRO_GAP_FAST_PATH` | config bool | `True` = skip LLM when total bridge < min MOQ |

---

## 10. Stage 5 — Clinical Agent

**File:** `core/clinical_agent.py`

Runs in parallel with the Procurement Agent. No LLM — fully deterministic Python. Fires when `invoke_clinical = True` (any hospital is HIGH_RISK).

Evaluates whether a clinical substitute exists, how similar it is, and whether a physician must approve the switch.

### Three outcomes

**1. No alternatives exist** (e.g. Insulin — nothing replaces it): `substitution_viable = False`. Escalation caveats returned.

**2. Alternatives exist but all share the same API risk:** Recommending a substitute that uses the same disrupted API is pointless. `substitution_viable = False`. Blocked list returned with reason.

**3. Viable alternative exists** (`shared_api_risk = False`): Best alternative by highest `similarity_score`:

| Tier | Score | Meaning |
|------|-------|---------|
| near_identical | ≥ 0.90 | Same drug, different dose/form — dose equivalence caveat |
| viable | 0.70–0.89 | Good substitute, standard caveats |
| last_resort | < 0.70 | Different mechanism — physician sign-off required |

`requires_physician_approval = True` if tier is `last_resort` OR if the alternative's criticality is lower than the primary drug.

### When clinical review is suppressed

If procurement fully covers all hospitals (all ALLOCATED), the Aggregator suppresses clinical review — no point suggesting a substitute when the original drug is being supplied.

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `ClinicalAssessment` | dataclass | Output: substitution_viable, recommended_alt, similarity_score, tier, physician_approval, caveats |
| `run_clinical_agent()` | **public entry point** | Evaluates alternatives, returns `ClinicalAssessment` |
| `_classify_alternatives()` | private | Splits alternatives into viable/blocked |
| `_requires_physician()` | private | Flags physician approval if tier is last_resort or alt is also at risk |
| `_build_hospital_list()` | private | Sorts hospitals by urgency |
| `_build_caveats()` | private | Assembles system-wide caveats |
| `_similarity_tier()` | private | Maps similarity score → near_identical / viable / last_resort |

---

## 11. Stage 6 — Aggregator

**File:** `core/aggregator.py`

Parallel execution coordinator. Submits all actionable drugs to a `ThreadPoolExecutor`, collects results, builds review packages, writes to SQLite.

### Coverage status — why ALLOCATED, not FULL

FULL would imply the hospital is completely protected — it is not. ALLOCATED means the system has assigned inventory to cover the bridge gap, but temporal risk remains: the drug still arrives after stockout if delivery days exceed days_until_stockout. ALLOCATED is honest — it communicates "an order has been placed to cover this" rather than "this hospital is safe". The Exposed column and colour coding in the drawer surface the remaining temporal risk.

### Coverage statuses

| Status | Meaning |
|--------|---------|
| `ALLOCATED` | units_acquired ≥ units_required — bridge fully assigned |
| `PARTIAL` | some units covered, not all |
| `ZERO` | assigned but stock exhausted — nothing allocated |
| `COVERED_BY_FACTORY` | hospital never needed a bridge (doesn't stock out before recovery) |

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `aggregate()` | **public entry point** | Runs agents in parallel, aggregates, writes to SQLite |
| `init_db()` | public | Creates `review_packages` table if not exists |
| `get_pending_packages()` | public | Returns all `pending_review` packages sorted by risk |
| `update_package_status()` | public | Marks package approved/rejected |
| `get_package_by_id()` | public | Fetches one package by package_id |
| `_build_review_package()` | private | Assembles the full `full_package` JSON blob |
| `_compute_hospital_coverage()` | private | Derives ALLOCATED/PARTIAL/ZERO per hospital from option_a allocations |
| `_build_action_summary()` | private | One-line text summary for dashboard card |
| `_write_to_sqlite()` | private | Inserts or replaces one package in SQLite |
| `_is_fully_covered()` | private | True if all actionable hospitals are ALLOCATED |

---

## 12. Outcome Simulator

**File:** `core/outcome_simulator.py` — no LLM, pure Python, runs in milliseconds.

Answers: *"What happens to hospital stock if we approve this order vs reject it?"*

For each gap hospital, simulates stock day-by-day from day 0 to recovery:
```python
daily_rate    = prophet_forecast_30d / 30
current_stock = days_until_stockout × daily_rate

for each day:
    stock += bridge_delivery_if_today   # approved trajectory only
    stock  = max(0, stock - daily_rate)
```

Two trajectories per hospital: **approved** (stock reinforced on delivery day) and **rejected** (no intervention, drains to zero).

`hospital_days_saved = stockout_days_rejected − stockout_days_approved`

The outcome chart in the drawer shows both lines per hospital, with a vertical marker at factory recovery day and a summary pill showing total_hospital_days_saved.

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `simulate_outcome()` | **public entry point** | Returns two stock trajectories per hospital + total_hospital_days_saved |
| `_simulate_hospital()` | private | Day-by-day simulation for one hospital |

---

## 13. Session Management

**File:** `core/session_manager.py`

### Why SQLite, not in-memory

The FastAPI backend is stateless across HTTP requests. If stock depletion state were held in a Python dict, a server restart would wipe it. SQLite gives persistence across restarts, handles concurrent reads cleanly, and requires no separate database server. The tradeoff is file I/O overhead — acceptable for this workload (single user, approval actions are infrequent). Neo4j is never written to during a session — `session.db` is the sole source of truth for live stock.

### How a session works

1. `start_session()` — seeds `session.db` from Neo4j's `DELIVERS_TO.currentStock` + `SESSION.inventory`
2. Each time a procurement order is approved, `apply_depletion()` deducts that distributor's stock in `session.db`. The next drug that also needs that distributor will see the reduced availability. This simulates real-world stock depletion across sequential approvals.
3. `end_session()` — marks session inactive in `session.db`, calls `SESSION.reset()`. The file stays on disk as an audit trail.

`reviews.db` is never reset or deleted — it is the permanent record of every decision made in the system.

### Module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `start_session()` | **public** | Seeds `session.db` from Neo4j + SESSION |
| `end_session()` | **public** | Marks inactive, resets SESSION |
| `apply_depletion()` | **public** | On approval: deducts distributor stock, restocks hospital inventory, syncs SESSION |
| `override_analyst_stock()` | **public** | Called by `analyst._fetch_distributor_options()` — patches Neo4j values with live session values |
| `get_session_state()` | **public** | Returns depletion delta snapshot (baseline vs current) |
| `is_active()` | **public** | Returns True if active session row exists |
| `get_distributor_stock()` | **public** | Single stock lookup by (distributor_id, hospital_id, drug_id) |
| `get_all_distributor_stock()` | **public** | Full stock dict for batch override |
| `_neo4j_all_stock()` | private | Pulls all `DELIVERS_TO` stock data from Neo4j for seeding |
| `session.db` tables | SQLite | `session_info`, `distributor_stock`, `hospital_inventory` |

---

## 14. Dashboard

**Tech stack:** FastAPI (`dashboard_api.py`) + React 19 (Vite, Tailwind CSS, TanStack Query, React Flow)
**Frontend location:** `./frontend/` — `npm run dev` (Vite dev server proxies to FastAPI on port 8000)
**Run backend:** `uvicorn dashboard_api:app --reload --port 8000`
**Auto-docs:** `http://localhost:8000/docs`

### Dashboard flow

1. **Start Session** → `session_manager.start_session()`; all panels unlock
2. **Supply Chain Graph** → React Flow column layout (Factory | API | Drug | Distributor | Hospital). Click any disruptable node to open the disruption panel.
3. **GNN Vulnerability Graph** → toggle on same area. Nodes coloured green→red by `vulnerabilityScore`.
4. **Disruption Panel** → select event_type + severity; fires full pipeline. Disrupt button disabled during run (prevents concurrent state corruption).
5. **Cascade Animation** → graph animates disruption wave outward layer by layer using `affected_drug_ids` from pipeline response.
6. **Review Queue** → drug risk cards: risk tier, coverage pills, days until stockout, disruption duration.
7. **Package Detail Drawer** → system totals row; hospital coverage table (Need / Get / Stock Lasts / Exposed / Arrives In / Coverage Status); Option A + B allocations; per-option caveats toggle (⚠ N caveat(s)); collapsible Agent Reasoning (LLM `recommendation_summary`); clinical guidance; approve/reject.
8. **Heatmap** → Hospital × Drug coverage matrix (ALLOCATED/PARTIAL/ZERO/NONE) across all drugs in disruption.
9. **Depletion View** → distributor stock baseline vs current after approvals.
10. **End Session** → `session_manager.end_session()` → stock resets; `reviews.db` untouched.

### FastAPI Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/db-list` | GET | List available review DBs |
| `/api/packages` | GET | All packages from a DB, sorted HIGH→LOW (supports `?since=` for session scoping) |
| `/api/packages/{id}` | GET | Full package detail |
| `/api/packages/{id}/action` | POST | Approve Option A/B or Reject — updates SQLite, calls apply_depletion on approval |
| `/api/packages/{id}/outcome` | GET | Run outcome simulation for this package |
| `/api/packages/{id}/retry-procurement` | POST | Re-run LLM if previously errored |
| `/api/stats` | GET | Aggregate counts for session bar (supports `?since=`) |
| `/api/heatmap` | GET | Hospital × Drug coverage matrix |
| `/api/distributors` | GET | Distributor stress (assigned vs stock) |
| `/api/session/start` | POST | Start simulation session |
| `/api/session/end` | POST | End session |
| `/api/session/state` | GET | Depletion delta snapshot |
| `/api/session/run-disruption` | POST | **Triggers full pipeline** — synchronous, blocks 3–10 min. Returns `affected_drug_ids`. |
| `/api/graph/nodes` | GET | All nodes + edges for React Flow graph |
| `/api/graph/vulnerability` | GET | Same topology + GNN scores |

### `dashboard_api.py` module reference

| Component | Type | Purpose |
|-----------|------|---------|
| `stats()` | route GET `/api/stats` | Aggregate counts: total, high_risk, pending, dicey, zero_hospitals |
| `packages()` | route GET `/api/packages` | All package summaries from a DB |
| `package_detail()` | route GET `/api/packages/{id}` | Full package JSON |
| `heatmap()` | route GET `/api/heatmap` | Hospital × Drug coverage matrix |
| `package_action()` | route POST `/api/packages/{id}/action` | Approve/Reject |
| `resolve_db()` | helper | Returns path to named DB or latest DB |

---

## 15. Data Schemas

### SQLite `review_packages` table

**Flat columns:**
```
package_id, disruption_node, disruption_event, disruption_severity,
drug_id, drug_name, criticality, overall_risk_level,
procurement_viable, clinical_suppressed, substitution_viable,
status, created_at, resolved_at, procurement_action, clinical_action, full_package
```

**`full_package` JSON structure:**
```
{
  disruption:        { node_id, node_name, type, event_type, severity, recovery_days, date }

  drug:              { drug_id, name, criticality, supply_loss_pct, demand_pressure,
                       drug_units_remaining, system_total_forecast, vulnerability_score }

  hospital_coverage: [ { hospital_id, hospital_name, city, specialty_type,
                          shortage_probability, days_until_stockout,
                          units_required, units_acquired, coverage_gap,
                          coverage_pct, coverage_status } ]
                     NOTE: units_required = LLM bridge_required when available;
                     falls back to prophet_forecast_30d. Computed as daily_demand × exposed_days,
                     not the raw 30-day forecast.

  procurement:       { scenario, viable, is_dicey_case, dicey_tradeoff,
                        recommendation_summary, total_stock_gap, caveats,
                        option_a: [ { distributor_id, distributor_name,
                                      total_quantity, price_per_unit,
                                      hospital_allocations: [ { hospital_id,
                                        delivery_days, gap_days,
                                        units_required, units_allocated,
                                        coverage_note } ],
                                      distributor_caveat } ],
                        option_b: [ same structure ] }

  clinical:          { suppressed, substitution_viable, recommended_alt_id/name,
                        similarity_score, requires_physician_approval,
                        viable_alternatives, blocked_alternatives }
}
```

---

## 16. How a Full Run Works — Step by Step

**Disruption:** Factory F001 (Biocon Biologics) | Disaster | High | 14 September 2026

1. **Sentinel** → looks up "Factory / Disaster / High" in taxonomy → 52 days recovery. Queries Neo4j: F001 uses API A001, which is a component of Drug D001 (Lantus). 10 hospitals receive Lantus from distributors. Returns `DisruptionEvent`.

2. **Prediction Engine** → loads 10 Prophet models. Forecasts 30-day demand. Checks session inventory. Computes days_until_stockout per hospital. H006: 16.6d, H009: 36.7d, H010: 41.0d. The other 7 hospitals have >52 days of stock.

3. **Analyst** → scores each pair. H006 → 0.681 → HIGH_RISK. H009 → 0.295 → MEDIUM_RISK. H010 → 0.212 → MEDIUM_RISK. 7 hospitals → NO_RISK. Packages into one D001 Lantus alert. `invoke_procurement = True`, `invoke_clinical = True`.

4. **Procurement Agent** → Python triage: 3 gap hospitals. H006 needs 406 bridge units (16.6d→52d gap × 11.5/day), H009 needs 46, H010 needs 22. Calls Gemini. LLM assigns S001→H006, S008→H009, S009→H010. Call 2 extracts unit caps. Python executes orders. Abundant supply → all hospitals 100%. Two below-MOQ caveats flagged.

5. **Clinical Agent** (parallel) → Insulin has no viable substitute (no `ALTERNATIVE_TO` relationships with `sharedApiRisk=False`). Returns `substitution_viable=False`.

6. **Aggregator** → H006=ALLOCATED, H009=ALLOCATED, H010=ALLOCATED. Clinical suppressed (procurement covers all). Writes package to `db/reviews.db` with status `pending_review`.

7. **Dashboard** → reviewer opens drawer, sees outcome chart: all 3 hospitals fully protected (0 days exposed with order, 35/15/11 days exposed without). Action summary: "Procurement covers all hospitals". Clicks **Approve A**. Status → `approved`. S001 depleted 406 units in session.

---

## 17. Technology Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Supply chain graph | Neo4j | Graph traversal for multi-hop supply chain queries in a single query |
| Demand forecasting | Prophet (Meta) | Purpose-built for seasonal time series; works with 1 year of data; interpretable |
| Vulnerability scoring | GNN (PyTorch Geometric) | Learns structural importance from graph topology |
| LLM reasoning | Google Gemini via AI Studio | Explainable allocation decisions, dicey case handling, human-readable caveats |
| Backend API | FastAPI (Python) | Fast, async, type-safe |
| Frontend | React 19 + Vite | Modern component-based UI |
| Graph visualisation | React Flow | Interactive supply chain graph with custom column layout |
| Charts | Recharts | Declarative charting for outcome simulation |
| Styling | Tailwind CSS | Consistent dark theme |
| Database | SQLite | Lightweight, no server, full audit trail; persistent across HTTP requests |

---

## 18. Build Status

| Component | File | Status |
|-----------|------|--------|
| Event validation & taxonomy | `core/sentinel.py` | ✅ Complete |
| Demand forecasting & risk scoring | `core/prediction_engine.py` | ✅ Complete |
| DrugAlertPackage assembly | `core/analyst.py` | ✅ Complete |
| LLM procurement recommendations | `core/procurement_agent.py` | ✅ Complete |
| Clinical substitution logic | `core/clinical_agent.py` | ✅ Complete |
| Parallel orchestration & SQLite write | `core/aggregator.py` | ✅ Complete |
| Stock trajectory simulation | `core/outcome_simulator.py` | ✅ Complete |
| Simulation session & depletion DB | `core/session_manager.py` | ✅ Complete |
| FastAPI backend (all endpoints) | `dashboard_api.py` | ✅ Complete |
| GNN vulnerability scoring | `ml/gnn_centrality.py` | ✅ Complete |
| React dashboard — session & graph | `frontend/` | ✅ Complete |
| React dashboard — disruption trigger | `frontend/` | ✅ Complete |
| React dashboard — cascade animation | `frontend/` | ✅ Complete |
| React dashboard — GNN vulnerability graph | `frontend/` | ✅ Complete |
| React dashboard — review queue & drawer | `frontend/` | ✅ Complete |
| React dashboard — heatmap & depletion | `frontend/` | ✅ Complete |

> [!WARNING]
> `POST /api/session/run-disruption` is **synchronous and blocks for 3–10 minutes** (LLM calls + 10s stagger). Do not reduce the 10s stagger — it exists to stay under Gemini's 15 RPM free-tier limit.

---

## 19. Test Scenarios

| # | Node | Event | Sev | DB | Key drugs |
|---|------|-------|-----|-----|----------|
| 1 | F002 Cipla | Strike | High | test_F002_Strike_High.db | Ventorlin, Asthalin, Brufen |
| 2 | F001 Biocon | Disaster | High | test_F001_Disaster_High.db | Lantus and F001 portfolio |
| 3 | A012 | Supply Chain Failure | High | test_A012_SCF_High.db | All A012-dependent drugs |
| 4 | A004 | Raw Material Shortage | High | test_A004_RMS_High.db | All A004-dependent drugs |
| 5 | F004 | Disaster | High | test_F004_Disaster_High.db | Storvas, Amaryl, Atorva, Glycomet |
| 6 | S003 | Logistics Failure | Medium | test_S003_LogFail_Medium.db | Brufen, Levoflox, Storvas |

Run with: `python tests/test_runner.py` — writes to named DBs, never pollutes `reviews.db`.

---

## 20. Known Gotchas

- **Tenacity silent retries:** The `google-genai` SDK retries 429 errors with exponential backoff (up to 60s per sleep). A "hung" drug thread is almost always tenacity retrying, not a crash. Errors only surface after all 5 retry attempts fail.
- **Distributor stock source:** The live stock values used by `analyst.py` come from `DELIVERS_TO.currentStock` in Neo4j (or `session.db` if a session is active). Not from any CSV.
- **Hospital inventory source:** `prediction_engine.SESSION.inventory` is the runtime source, loaded from `hospital_inventory.csv` at startup. `session_manager` persists it to `session.db`. Do not read `hospital_inventory.csv` for live values during a session.
- **Year in dates:** Pass any year — Prophet patterns are month/day based. The year only affects how many periods Prophet forecasts past its 2024-12-31 training cutoff.
- **MICRO_GAP_FAST_PATH:** `True/False` at top of `procurement_agent.py`. Set `False` to force LLM calls for all cases (useful for benchmarking).
- **`reviews.db` is a permanent audit log.** Never reset or delete it between sessions. Initialized automatically at FastAPI startup via `aggregator.init_db()`. All disruption runs accumulate here permanently.
- **`session.db` and `reviews.db` are runtime-generated.** Neither is in the repo. Their absence is normal. `session.db` is created on `POST /api/session/start`. `reviews.db` is created on first disruption run.
- **Node ID format:** Factories = `F001`–`F005`, Distributors = `S001`–`S010`, APIs = `A001`–`A012`. These come from Neo4j `.id` properties and are included in `/api/graph/nodes` response.
- **Graph rendering — column layout, not force-directed.** Neo4j has 1,939 total edges. `/api/graph/nodes` returns ~137 structural edges (17 Factory→API, 20 API→Drug, 100 Distributor→Hospital deduplicated, 0 NEEDS_DRUG — Hospital→Drug hidden to avoid 200-edge clutter). The React Flow frontend uses `applyColumnLayout` with no physics.
- **`NEEDS_DRUG` not `USES`:** `USES` does not exist in this graph. Any Cypher query using `[:USES]` returns zero results silently.
- **STOCKS vs SUPPLIED_BY:** The cascade animation in `SupplyChainGraph.jsx` references edge label `SUPPLIED_BY` for the Drug→Distributor hop. The actual Neo4j relationship is `STOCKS`. A mismatch here silently breaks the distributor layer of the cascade for distributor disruptions.
- **Do not reduce the 10s stagger.** The pipeline staggers LLM calls by 10 seconds to stay under Gemini's 15 RPM free-tier limit. Reducing it causes 429 errors that trigger tenacity retries and make the pipeline appear hung.
- **`test_runner.py` writes to named DBs.** Running test cases never pollutes the dashboard's review queue.
