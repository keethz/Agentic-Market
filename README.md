# Agentic Market: Autonomous B2B Discovery and Negotiation

Two AI agents negotiate a B2B SaaS deal for their companies. A neutral **Deal Coordinator**
enforces the rules, and **humans approve** the deal before it becomes binding. Everything runs
live in a [Band](https://band.ai) room.


Product pitch : https://drive.google.com/file/d/1QD-Stp3ILlxCC6_Jm3eigqFS7QKFxdLB/view?usp=sharing 
| Agent | Represents | Goal |
|---|---|---|
| **CloudCo Sales** | Seller (analytics platform) | Maximize revenue, protect its lowest acceptable terms |
| **RetailCorp Procurement** | Buyer (retailer, 400 stores) | Minimize cost, protect its budget and requirements |
| **Deal Coordinator** | Neutral, deterministic (no LLM) | Relay messages, enforce rules, detect agreement, write the contract, request approval |
| **Human approvers** | Both companies | Approve or reject before the deal is final |

```
 CloudCo Sales ──@mention──▶ Deal Coordinator ──@mention──▶ RetailCorp Procurement
 (Gemini + guardrails)          │   (rules, no LLM)          (Gemini + guardrails)
                                │
                    Policy Service (secret limits → pass/fail only)
                                │
         approval request ──▶ humans in the room ──▶ FINALIZED contract (data/contracts/)
```

## How a deal is finalized
1. **Both agents explicitly agree to identical terms:** one side accepts the other's offer, and the
   proposer confirms with the same terms. Near-identical terms are rejected.
2. **All constraints are satisfied:** the Policy Service checks both companies' private limits and
   non-negotiable terms.
3. **The coordinator writes a structured contract** with a SHA-256 hash of the terms.
4. **Every required human approver approves.** Approvers depend on deal size (e.g. CFO above $35k).
   One rejection stops the deal.

### Contract PDF
When **every** required approver has approved, the coordinator renders the final contract to
`data/contracts/<contract_id>.pdf`. The PDF shows the parties, the agreed terms with the total value,
the conditions, and each approver with their decision and timestamp. In Band
the PDF is uploaded to the room automatically; `@Deal Coordinator contract` sends it again. No PDF is
made for rejected or partly approved contracts, and it never contains private limits. See
[samples/sample_contract.pdf](samples/sample_contract.pdf).

Only the coordinator can relay messages between companies, change status, declare agreement,
request approval, or create the contract.

## How secrets stay secret
Band shows humans in a room the full timeline, so **no secret ever goes into a message**.
- Each company's limits, targets and objectives live only in `configs/private/<company>.yaml`
  (gitignored) and in that agent's own process.
- The coordinator never sees raw limits. It asks the **Policy Service**, which answers
  `ok` or reason codes like `OUT_OF_RANGE`, never numbers.
- Offers outside the *other* side's range are still relayed, because rejecting them would reveal
  that side's limits. Both sides' limits are checked only when a deal is accepted.
- **Guardrails** block any outgoing message that breaks the agent's own limits or whose rationale
  mentions secret numbers or words like "budget", "floor" or "walk-away". The agent then retries.
- **Leak scan** (`negotiation/leak_scan.py`) checks the whole timeline for secrets after a run.

## Marketplace: registry, matchmaking and consent
Sales agents don't message every buyer. Instead:
1. **Registry** (`configs/registry/`). Sellers publish product listings: capabilities, integrations,
   industries, company sizes, regions, certifications, SLA and deal history. Buyers publish structured
   needs: must-haves, nice-to-haves, seats, intent, timeline and a deliberately wide **public budget band**.
   Buyers never publish their true maximum budget; it stays in `configs/private/`.
2. **Matchmaker** (`negotiation/matchmaker.py`). It filters first on category, region, required
   capabilities, integrations, certifications and minimum SLA. It then scores the vendors that pass on
   capability and integration coverage, semantic fit, industry, company size, geography, buying intent
   and past deal performance. Semantic fit uses offline word overlap by default, or Gemini embeddings
   with `MATCH_SEMANTIC=gemini`. Every match lists **why** it was recommended (✓) and what is missing (✗).
3. **Consent before outreach** (`negotiation/outreach.py`). A seller can only send an outreach request
   when it is eligible for the need. It cannot send duplicates and is limited to 3 pending requests.
   The buyer agent then applies its **private** consent policy (`configs/consent/`): accept automatically
   above a score, send borderline matches to a human, decline blocked vendors or when too many
   conversations are open. Room messages give no policy details. The coordinator won't `start` a
   negotiation until the buyer has accepted.

| Command | What it does |
|---|---|
| `@Deal Coordinator needs` | Lists open buyer needs (public fields only) |
| `@Deal Coordinator matches` | Lists ranked vendors for the need, with explanations |
| `@Deal Coordinator opportunities CloudCo` | Shows needs that a seller is eligible for |
| `@Deal Coordinator outreach <vendor>` | Seller asks for consent to negotiate |
| `@Deal Coordinator consent <id> yes\|no` | Buyer's human decision on a borderline request |

## Vendor fallback
If a negotiation ends in **IMPASSE** (round limit, an agent walks away, or the buyer's human sends
`walkaway`), the coordinator moves on to the **next-ranked eligible vendor** for the same need:
1. It skips vendors already tried and vendors with no connected negotiating agent.
2. It asks the buyer for consent again. An automatic decline skips the vendor; a borderline
   score pauses until `@Deal Coordinator consent <id> yes|no`.
3. It adds the new seller to the room and opens a fresh negotiation, with its own contract,
   approvals, PDF and analytics.
4. When no vendors are left, it says so and the need stays open.

The sample registry ranks CloudCo first and RetailIQ second. RetailIQ has its own private limits
(`configs/private.example/vendor_retailiq.yaml`) and approvers (Account Executive, plus Sales Director
above $50k). To enable it in Band, register a remote agent named **RetailIQ Sales** and add its
credentials under `vendor_retailiq:` in `agent_config.yaml`. Locally: `.venv/bin/python run_demo.py --fallback`.

## Negotiation analytics
At the end of every deal (agreement or impasse), and again when humans finalize or reject it, the
coordinator computes metrics, saves them to `data/analytics/<negotiation>.json` and posts them in
the room.

| Metric | Meaning |
|---|---|
| Rounds | Offers and counters exchanged |
| Initial vs final price | Seller's opening, buyer's opening, and final price per seat |
| Buyer savings | (seller's opening price − final price) × final seats × final term |
| Seller discount | % off the seller's opening price |
| Price split | Where the deal landed in the opening gap (0% = buyer's open, 100% = seller's) |
| Time to agreement | Seconds from opening to identical acceptance |
| Rejected offers | Offers the other side countered instead of accepting |
| Policy violations | Proposals blocked by the coordinator (Policy Service) plus proposals blocked by each agent's own guardrails |
| Secret-leak attempts | Outgoing messages blocked because they mentioned confidential information |
| Walk-away rate | Share of negotiations ending with a walk-away (portfolio level) |

Portfolio totals across all deals: `@Deal Coordinator analytics` in Band, or
`.venv/bin/python -m negotiation.analytics` locally. Agents report only *counts* of blocked
attempts, never what was blocked, so the analytics are safe to post publicly.

## Quick start

Requires Python 3.11+.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                                 # set GOOGLE_API_KEY (free at aistudio.google.com)
cp -r configs/private.example configs/private        # each company's secret limits
```

### Run locally (no Band)
```bash
.venv/bin/python run_demo.py                          # scripted agents, offline
.venv/bin/python run_demo.py --brain gemini           # Gemini agents
.venv/bin/python run_demo.py --brain gemini --auto-approve
```

### Run on Band
1. Register three remote agents at app.band.ai → **Agents → Connect remote agent**:
   `CloudCo Sales`, `RetailCorp Procurement`, `Deal Coordinator`.
2. `cp agent_config.example.yaml agent_config.yaml`, then paste each agent's ID and API key.
3. Start all three agents:
   ```bash
   .venv/bin/python run_band.py          # brain from AGENT_BRAIN (default: gemini)
   ```
4. In Band, create a room, add **Deal Coordinator**, and send commands:

| Command | What it does |
|---|---|
| `@Deal Coordinator start` | Opens a negotiation (adds both company agents to the room) |
| `@Deal Coordinator walkaway [reason]` | Buyer ends the current negotiation; triggers vendor fallback |
| `@Deal Coordinator contract` | Re-sends the finalized contract PDF |
| `@Deal Coordinator status` | Shows the deal status and who has approved |
| `@Deal Coordinator analytics` | Shows this deal's metrics and portfolio totals |
| `@Deal Coordinator approve <role>` | Approves as that role (several lines allowed in one message) |
| `@Deal Coordinator reject <role> <reason>` | Rejects the contract |

Approver roles in the sample configs: `Sales Manager`, `VP Sales` (CloudCo) and
`Procurement Lead`, `CFO` (RetailCorp).

### Dashboard
```bash
.venv/bin/python dashboard.py          # http://localhost:8050
```
A read-only page that refreshes every 5 seconds. It shows totals (negotiations, deals closed, agreement
rate, average time to agreement and to close, closed value), a per-seller-agent table, time-to-agreement
bars, and every negotiation with a link to its contract PDF. It reads only `data/` and shows no
private limits.

### Tests
```bash
.venv/bin/python -m pytest -q
```
The tests cover turn order, near-identical acceptances, two-sided confirmation, agents breaking
their own limits, offers relayed without leaking limits, walk-away, approvals and rejections,
guardrail leak detection, analytics (violations, leaks, walk-aways, portfolio summary), and a full negotiation through a simulated Band room.

## Project layout
| Path | Purpose |
|---|---|
| `configs/public/` | Public company profiles (safe to show) |
| `configs/private.example/` | Example private limits, objectives and approval rules (copy to `configs/private/`) |
| `negotiation/coordinator.py` | Deterministic coordinator: routing, rules, state machine, agreement, contract, approvals |
| `negotiation/policy_service.py` | Limit checks and approver rules; `policy_api.py` serves them over HTTP with an API key |
| `negotiation/agent.py` | `NegotiationAgent` with `GeminiBrain`, `ClaudeBrain` and `ScriptedBrain` |
| `negotiation/guardrails.py` | Own-limit checks and secret-leak filter |
| `negotiation/band_runtime.py` | The three Band agents as custom adapters |
| `negotiation/band_format.py` | Readable Band messages with an embedded JSON payload |
| `negotiation/leak_scan.py` | Scans a timeline for leaked secrets |
| `negotiation/registry.py`, `matchmaker.py`, `outreach.py` | Registry, ranked matching with explanations, consent-gated outreach |
| `negotiation/fallback.py` | Next-vendor fallback after an impasse, through consent |
| `negotiation/contract_pdf.py` | Renders finalized contracts to PDF |
| `negotiation/analytics.py` | Per-deal metrics and portfolio aggregates |
| `dashboard.py`, `dashboard.html`, `negotiation/dashboard_data.py` | Read-only negotiations dashboard |
| `run_demo.py` / `run_band.py` | Local demo / live Band runtime |
| `data/` | Audit log, contract records and analytics (gitignored) |

## Configuration (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | none | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Falls back to other free Flash models when busy |
| `AGENT_BRAIN` | `gemini` | `gemini`, `claude` or `scripted` |
| `ANTHROPIC_API_KEY`, `AGENT_MODEL` | none, `claude-opus-5-5` | For `--brain claude` |
| `MATCH_SEMANTIC` | offline | `gemini` to use Gemini embeddings for matching |
| `POLICY_API_KEY` | none | Protects `policy_api.py` |
