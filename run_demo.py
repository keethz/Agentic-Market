"""Run a full negotiation locally.

  python run_demo.py                 # offline scripted agents
  python run_demo.py --brain gemini  # Gemini agents (needs GOOGLE_API_KEY)
  python run_demo.py --brain claude  # Claude agents (needs ANTHROPIC_API_KEY)
  python run_demo.py --auto-approve  # skip the interactive human approval prompts
  python run_demo.py --fallback      # buyer walks away from CloudCo -> next-ranked vendor (RetailIQ)
"""
import argparse
import json

from dotenv import load_dotenv

from negotiation import analytics
from negotiation.config import load_all
from negotiation.leak_scan import scan
from negotiation.models import Status
from negotiation.system import build


def show(m):
    body = json.loads(m.content)
    who = f"{m.sender} -> {m.recipient or 'ROOM'}"
    if body.get("kind") == "status":
        print(f"  [status] {body['status']}  {body.get('reason', '')}")
    elif body.get("kind") == "analytics":
        print("  [analytics recorded]")
    elif "terms" in body and body.get("terms"):
        t = body["terms"]
        print(f"{who:28} {body.get('type', body.get('kind')):16} ${t['unit_price']}/seat x{t['quantity']} "
              f"{t['contract_months']}mo net{t['payment_terms_days']} {t['sla_uptime']}% {t['support_tier']}"
              f"  | {body.get('rationale', '')}")
    else:
        print(f"{who:28} {body}")


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", choices=["scripted", "gemini", "claude"], default="scripted")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--fallback", action="store_true", help="simulate an impasse with the top vendor")
    args = ap.parse_args()

    if args.fallback:
        from negotiation.agent import ScriptedBrain
        from negotiation.config import load_private, load_public
        from negotiation.fallback import run_with_fallback
        from negotiation.models import Proposal, ProposalType

        class WalkAway(ScriptedBrain):
            def decide(self, incoming, feedback=None):
                return Proposal(type=ProposalType.walk_away, rationale="Not the right fit for us.")
        quit_ = {"company_a": {"company_b": WalkAway(load_public("company_b"), load_private("company_b"))}}
        attempts, coord, step = run_with_fallback("need-retailcorp-analytics", brain=args.brain, brains=quit_)
        for a in attempts:
            print(f"Attempt with {a.vendor_id}: {a.status} ({a.negotiation_id})")
        for n in step.notes:
            print(" ", n)
        if not coord:
            return print("No vendor reached agreement.")
        t = None
    else:
        t, coord, *_ = build(args.brain)
        coord.start()
        t.run_until_idle()
        for m in t.timeline:
            show(m)

    print("\nFinal status:", coord.status.value)
    if t:
        leaks = scan(t.timeline, load_all())
        print("Leak scan:", "CLEAN - no secrets in room timeline" if not leaks else leaks)

    def show_analytics():
        print("\n=== Deal analytics ===\n" + analytics.report(coord.analytics))
        print("\n=== Portfolio (all recorded deals) ===\n" + analytics.summary_report(analytics.summarize(coord.data_dir)))

    if coord.status != Status.pending_approval:
        return show_analytics()
    c = coord.contract
    print(f"\nContract {c.contract_id}  total ${c.total_value:,.2f}  hash {c.terms_hash[:12]}")
    for a in list(c.approvals):
        if coord.status != Status.pending_approval:
            break
        name = coord.parties[a.company_id].name
        if args.auto_approve:
            coord.approve(a.company_id, a.role, f"demo-{a.role}")
            continue
        ans = input(f"{name} / {a.role}: approve? [y/n] ").strip().lower()
        if ans == "y":
            coord.approve(a.company_id, a.role, input("  your name: ") or a.role)
        else:
            coord.reject(a.company_id, a.role, a.role, input("  reason: "))
    print("Contract status:", coord.contract.status.value, f"-> data/contracts/{c.contract_id}.json")
    show_analytics()


if __name__ == "__main__":
    main()
