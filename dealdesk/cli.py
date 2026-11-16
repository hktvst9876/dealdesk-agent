"""dealdesk command line.

  dealdesk demo capacity            the spec's capacity-allocation test (engine only)
  dealdesk demo salt                the Salt worked numbers
  dealdesk validate <profile>       load a YAML/XLSX profile, show placeholders, fail on inconsistencies
  dealdesk template <profile> <out.xlsx>   write an Excel profile to fill in
  dealdesk deal <profile> "<situation>"    the staff quick deal (/deal) from the terminal
  dealdesk chat <profile>           chat as a customer in the terminal; you play staff too
  dealdesk run <profile>            run the Telegram bot (needs TELEGRAM_BOT_TOKEN)
  dealdesk metrics <profile>        this week's and last week's metrics and the auto-send gates
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _brain(args):
    if getattr(args, "rules", False):
        return None
    if os.environ.get("ANTHROPIC_API_KEY") or getattr(args, "claude", False):
        from .agent.claude_brain import ClaudeBrain

        return ClaudeBrain(model=getattr(args, "model", None))
    return None


def _now(args, profile) -> datetime:
    if getattr(args, "now", None):
        return datetime.fromisoformat(args.now)
    return datetime.now(ZoneInfo(profile.business.timezone)).replace(tzinfo=None, microsecond=0)


def _db(args, profile) -> str:
    return args.db or os.environ.get("DEALDESK_DB") or f"data/{profile.business.id}.db"


def cmd_demo(args) -> int:
    from .demo import format_capacity, format_salt

    if args.which == "capacity":
        for c in ([args.case] if args.case else range(1, 7)):
            print(format_capacity(c), end="\n\n")
    else:
        print(format_salt())
    return 0


def cmd_validate(args) -> int:
    from .profile.loader import load_profile

    try:
        p = load_profile(args.profile)
    except Exception as e:
        print(f"INVALID: {e}")
        return 1
    print(f"OK: {p.business.name} ({p.business.kind}), {len(p.inventory)} capacity types, {len(p.products)} products, "
          f"{len(p.perks)} perks, currency {p.business.currency}")
    print(f"Policy: minimum contribution {p.policy.min_contribution}, concession cap {p.policy.concession_cap}, "
          f"authority {dict(p.policy.authority)}")
    est = [r.id for r in p.inventory if r.estimate] + [x.id for x in p.products if x.estimate] + [x.id for x in p.perks if x.estimate]
    if p.placeholders or est:
        print("Replace before going live:")
        for x in p.placeholders:
            print(f"  - {x}")
        for x in est:
            print(f"  - {x} is marked as an estimate")
    return 0


def cmd_template(args) -> int:
    import yaml

    from .profile.loader import load_profile, resolve_profile_path
    from .profile.loader_xlsx import profile_dict_to_xlsx

    src = resolve_profile_path(args.profile)
    p = load_profile(src)
    out = profile_dict_to_xlsx(yaml.safe_load(src.read_text()), Path(args.out), p.resell_matrix)
    print(f"Wrote {out}")
    return 0


def cmd_deal(args) -> int:
    from .agent.turn import Agent
    from .ledger.store import Store
    from .profile.loader import load_profile

    p = load_profile(args.profile)
    agent = Agent(p, Store(_db(args, p) if args.db else ":memory:"), brain=_brain(args))
    print(agent.quick_deal(args.situation, _now(args, p), args.tier))
    return 0


def cmd_chat(args) -> int:
    from .agent.models import Inbound
    from .agent.turn import Agent
    from .agent.watchers import tick
    from .ledger.store import Store
    from .profile.loader import load_profile

    p = load_profile(args.profile)
    store = Store(args.db or ":memory:")
    if args.cakes is not None:
        store.set_signal(p.business.id, "stock", "cakes", args.cakes, _now(args, p))
    agent = Agent(p, store, brain=_brain(args))
    now = _now(args, p)
    print(f"Chatting with {p.business.name} as a customer ({'Claude' if agent.brain else 'offline rules'}). "
          f"Clock: {now:%a %d %b %H:%M}. Ctrl-D to quit.")
    print("Staff controls after each draft: Enter = Send, e = Edit, r = Reject. Type '/...' for staff commands.\n")
    n = 0
    while True:
        try:
            text = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not text:
            continue
        now = now.replace(second=0) if args.now else _now(args, p)
        if text.startswith("/"):
            from .staff import commands

            for r in commands.handle(agent, text, "Owner", "owner", now):
                print(f"[staff] {r if isinstance(r, str) else r.text}")
            continue
        n += 1
        res = agent.handle_customer(Inbound("terminal", "you", str(n), text, now, None))
        for o in res.outbound:
            if o.to == "staff":
                print("\n" + "\n".join(f"  | {line}" for line in o.text.splitlines()))
        if res.review_id and not res.autosent:
            choice = input("staff [Enter=send / e=edit / r=reject]> ").strip().lower()
            if choice == "e":
                new = input("new text> ")
                outs = agent.staff_edit(res.review_id, "You", new, now)
            elif choice == "r":
                outs = agent.staff_reject(res.review_id, "You", input("reason> ") or "rejected", now)
            else:
                outs = agent.staff_send(res.review_id, "You", now)
        else:
            outs = [o for o in res.outbound if o.to == "customer"]
        for o in outs:
            print(f"\n{p.business.name}> {o.text}\n" if o.to == "customer" else f"[staff] {o.text}")
        for o in tick(agent, now):
            print(f"[watcher] {o.text}")


def cmd_run(args) -> int:
    from .agent.turn import Agent
    from .channels.telegram import HttpTelegram, TelegramRunner
    from .ledger.store import Store
    from .profile.loader import load_profile

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = load_profile(args.profile)
    token = os.environ.get(p.channels.telegram.bot_token_env)
    if not token:
        print(f"Set {p.channels.telegram.bot_token_env} to your bot token from @BotFather.")
        return 2
    brain = _brain(args)
    if brain is None:
        logging.warning("No ANTHROPIC_API_KEY: running with the offline rules brain and template wording.")
    agent = Agent(p, Store(_db(args, p)), brain=brain)
    api = HttpTelegram(token)
    me = api.get_me()
    staff_chat = int(os.environ["DEALDESK_STAFF_CHAT_ID"]) if os.environ.get("DEALDESK_STAFF_CHAT_ID") else None
    runner = TelegramRunner(agent, api, staff_chat)
    print(f"Bot @{me.get('username')} is live for {p.business.name}.")
    if runner.staff_chat_id is None:
        print("Next: add the bot to your private staff group and send /setup there.")
    runner.run_forever()
    return 0


def cmd_metrics(args) -> int:
    from datetime import timedelta

    from .analytics.metrics import gate_status, week_metrics
    from .ledger.store import Store
    from .profile.loader import load_profile

    p = load_profile(args.profile)
    store = Store(_db(args, p))
    now = _now(args, p)
    week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    for start in (week, week - timedelta(days=7)):
        print("\n".join(week_metrics(store, p, start).lines(p.business.currency_symbol)), end="\n\n")
    for cls, (ok, why) in gate_status(store, p, now).items():
        print(f"{cls}: {'MET' if ok else 'not met'} - {why}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dealdesk", description="Financially responsible deal agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("which", choices=["capacity", "salt"])
    d.add_argument("--case", type=int)
    d.set_defaults(fn=cmd_demo)
    v = sub.add_parser("validate")
    v.add_argument("profile")
    v.set_defaults(fn=cmd_validate)
    t = sub.add_parser("template")
    t.add_argument("profile")
    t.add_argument("out")
    t.set_defaults(fn=cmd_template)
    for name, fn in (("deal", cmd_deal), ("chat", cmd_chat), ("run", cmd_run), ("metrics", cmd_metrics)):
        s = sub.add_parser(name)
        s.add_argument("profile")
        if name == "deal":
            s.add_argument("situation")
            s.add_argument("--tier", default="front_line")
        s.add_argument("--db")
        s.add_argument("--now", help="pretend local time, e.g. 2026-11-14T15:00")
        s.add_argument("--claude", action="store_true", help="use Claude (needs ANTHROPIC_API_KEY)")
        s.add_argument("--rules", action="store_true", help="offline rules only, even if a key is set")
        s.add_argument("--model", help="Claude model id (default claude-opus-5-5)")
        if name == "chat":
            s.add_argument("--cakes", type=int, help="cakes available today (kitchen checklist)")
        s.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
