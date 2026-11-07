# Agentic Sales: agent-to-agent B2B negotiation on Band

Two AI agents negotiate a B2B SaaS deal for their companies. A neutral **Deal Coordinator**
enforces the rules, and **humans approve** the deal before it becomes binding. Everything runs
live in a [Band](https://band.ai) room.

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
| `@Deal Coordinator status` | Shows the deal status and who has approved |
| `@Deal Coordinator analytics` | Shows this deal's metrics and portfolio totals |
| `@Deal Coordinator approve <role>` | Approves as that role (several lines allowed in one message) |
| `@Deal Coordinator reject <role> <reason>` | Rejects the contract |

Approver roles in the sample configs: `Sales Manager`, `VP Sales` (CloudCo) and
`Procurement Lead`, `CFO` (RetailCorp).

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
| `negotiation/analytics.py` | Per-deal metrics and portfolio aggregates |
| `run_demo.py` / `run_band.py` | Local demo / live Band runtime |
| `data/` | Audit log, contract records and analytics (gitignored) |

## Configuration (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | none | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Falls back to other free Flash models when busy |
| `AGENT_BRAIN` | `gemini` | `gemini`, `claude` or `scripted` |
| `ANTHROPIC_API_KEY`, `AGENT_MODEL` | none, `claude-opus-5-5` | For `--brain claude` |
| `POLICY_API_KEY` | none | Protects `policy_api.py` |
