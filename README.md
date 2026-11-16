# DealDesk: a financially responsible deal agent for any business

DealDesk answers customers on **Telegram** within minutes and gives them a good deal on the spot (a group booking, a round of drinks, a bigger order, a bridal party) **without giving away margin the business can't afford**. Claude reads and writes the messages. A deterministic engine does every calculation and decides what is allowed. Staff approve every message in a private Telegram group until each kind of decision has earned automatic sending.

It is built from *Build Spec v2: Financially Responsible Sales Negotiation Agent* (Salt Beach Club) and generalised so that any business is just a profile: bars, restaurants, venues, salons, wholesalers and shops.

```
dealdesk demo salt        # the spec's worked numbers, to the dollar
dealdesk demo capacity    # the spec's capacity-allocation test
dealdesk chat neighbourhood_bar --now 2026-11-17T16:30   # be the customer; you are staff too
```

---

## How it decides (the economics)

One set of formulas for every business. Only *what is scarce* changes.

| | Formula | Meaning |
|---|---|---|
| Contribution | `C = R - V - K` | revenue - variable cost to serve - true cost of what is given away |
| Opportunity cost | `O` | what the deal uses up is worth in its next-best use |
| Incremental value | `C - O` | is the deal better than not doing it? |
| Minimum price | `V + max(O, M)` | the lowest price that may ever be sent (M = business minimum per deal) |
| Expected contribution | `E = p*C + (1-p)*O` | the ranking number; p = chance the customer books on these terms |
| Break-even close rate | `p* = (E_std - O) / (C - O)` | how much a concession must lift the close rate to pay for itself |

**Opportunity cost depends on what the business runs out of:**

| Scarce thing | Example businesses | Opportunity cost |
|---|---|---|
| Seats, rooms, time slots | beach club, bar, restaurant, karaoke, hotel | chance a regular customer would have taken it x their contribution. Zero if an equivalent unit is free |
| Stock | bar and kitchen stock, retail, wholesale | Replenishable stock: chance the unit would sell at full price before the restock x the margin. Perishable stock: the purchase is sunk, so the cost is what it would otherwise fetch (often just salvage) |
| People or machine hours | salons, trades, agencies | hours displaced x the cheaper of lost work or overtime |
| Cash | anyone offering payment terms | amount x cost of capital x days / 365, plus expected bad debt |

**Profit-and-loss adaptation.** Put this month's P&L snapshot in the profile (`finance.pnl`). The agent then works out contribution margin, break-even, pace against target, concession budget and cash runway, and adjusts only inside bounds the owner sets:

* Behind pace: the minimum contribution on quiet capacity is relaxed, by up to the owner's cap. Busy slots never change.
* Real costs above the configured ratio: prices are worked out with the higher cost, and the owner is warned.
* Concession budget for the period used up: every concession needs a manager.
* Cash runway short: payment terms are priced at a stressed cost of capital and deposits are preferred.

The hard floors never move: price >= variable cost + opportunity cost; nothing outside the perk catalogue; the cumulative concession cap; the sender's authority.

### What the engine teaches (from the test suite)

* **Salt**: 15% off the minimum spend looks cheap but costs S$119 of contribution and needs a 15-point jump in close rate to pay back. The engine keeps the quote and offers the cake instead. The VIP room ranks first on expected contribution with no concession at all.
* **Bar**: "S$300 minimum spend and the sharing platter is on us" beats "15% off the bill" on a quiet Tuesday. It lifts spend; the discount just gives margin away on drinks the table would have bought anyway. On a busy Friday the bill discount is blocked outright.
* **Salon**: the same bridal party can have 15% off on a quiet Tuesday. On a full Saturday it gets a perk only, because every Saturday hour displaces a paying regular.
* **Wholesale**: discounting scarce gin loses the margin of the full-price sales it displaces. A short-dated keg costs only its salvage value, so it is the cheapest thing to give. 30-day terms cost about S$89 on a S$4.5k order and need a manager.

---

## The agentic workflow

Every customer message runs the same steps. Claude does steps 1 and 6 (understanding and wording). Code does everything with numbers or side effects.

```
Customer (Telegram)
  │
  0 INTAKE      code    drop duplicates (idempotency key); load the conversation record; start the reply clock
  1 UNDERSTAND  Claude  date, time, party size, occasion, seating, basket, intent, blocker
                        (price | fee | occasion | fit | risk | process | timing); one question only if it changes the offer
  2 GROUND      code    re-read calendar, holds, staff signals (/busy /stock), prices, perk stock, P&L posture: never from memory
  3 PROPOSE     code    placement first (requested slot, nearest free slot, other session, package), then the standard
                        quote + up to two catalogue concessions aimed at the blocker: no-cost fix > perk > fee > price
  4 SCORE       engine  R, C, O, C-O, minimum price, p, E, break-even, concession cost, cumulative cost, authority needed
  5 DECIDE      engine  covers O > minimum contribution > busy-session rule > beats standard > cumulative cap > authority
                        = keep the quote | offer an approved concession | suggest an alternative | escalate
                        side effects (hold, link, callback, escalation) only when the engine says sendable
  6 WRITE       Claude  words from the engine's brief only; a checker compares every number, perk, time and hold
                        with the brief; one rewrite, then a safe template
  7 GATE        staff   review card in the staff group: Send / Edit / Reject (edits and rejections are the training signal);
                        escalations go to a manager with figures, assumptions and a deadline; if nobody answers in time,
                        the best approved offer goes out rather than silence
  │
Customer gets one message: the plan for their group, the number, one next step
```

Background watchers release expired holds (holds are never extended silently), enforce escalation and callback deadlines, nudge staff when a reply is overdue, and purge message text on the retention schedule. Outcomes are logged, and the agent proposes recalibrated close rates for the owner to approve. It never changes its own limits.

**Why one agent and not a swarm:** one accountable loop with a fixed order of steps is easy to audit. The model never calls a side-effecting tool; code does, after the engine permits it.

---

## Run it on Telegram

You need: a computer or small server that stays on, Python 3.11+ (or Docker), a Telegram bot token, and ideally an Anthropic API key. Without the key the bot uses an offline understanding layer and template wording. It works, but it is less fluent.

1. **Create the bot.** In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. **Create the staff group.** Make a private Telegram group for your team and add the bot. In BotFather, send `/setprivacy`, pick your bot and choose *Disable*, so the bot can read staff replies in the group.
3. **Install and configure.**
   ```bash
   pip install .
   cp .env.example .env    # then put your tokens in .env (never in chat or in git)
   export $(grep -v '^#' .env | xargs)
   dealdesk validate salt_beach_club
   ```
4. **Start it.**
   ```bash
   dealdesk run salt_beach_club
   ```
   or with Docker:
   ```bash
   docker build -t dealdesk .
   docker run -d --restart unless-stopped --env-file .env -e PROFILE=salt_beach_club -v "$PWD/data:/app/data" dealdesk
   ```
5. In the staff group send `/setup`. Each staff member sends `/iam <name>` (names from the profile's `staff:` list, which holds each person's tier).
6. Message the bot from your own phone as a customer. The draft appears in the staff group; press **Send**.

## Set up your own business

A business is one profile, with no code changes. Copy one of these and edit it, or fill in the Excel template:

| Profile | Shows |
|---|---|
| `profiles/salt_beach_club.yaml` | the spec: minimum-spend beds, VIP package, corkage, cake, matrix-based resale chance |
| `profiles/neighbourhood_bar.yaml` | food and drink deals: minimum spend + perk, perishable vs scarce stock, busy nights, P&L posture |
| `profiles/retail_wholesale.yaml` | basket orders, volume discounts, short-dated stock, payment terms |
| `profiles/salon_services.yaml` | stylist hours as the scarce resource |
| `profiles/karaoke_demo.yaml` | the spec's capacity-allocation test |
| `profiles/business_profile_template.xlsx` | the same structure as a spreadsheet (`dealdesk template <profile> out.xlsx` makes one from any profile) |

`dealdesk validate <profile>` rejects inconsistent profiles: for example, a perk the front line may give that costs more than front-line authority, manager authority below front-line authority, or references to unknown fees or products. It also lists every placeholder that still needs real numbers.

## Staff commands (in the staff group)

`/deal <situation>` gives the deal plus the four-line reasoning, for staff eyes only ("table of 6 tonight, want gin").
`/busy <date> [session]` / `/quiet` / `/normal` set demand signals. `/stock cakes 3` and `/onhand <product> <n>` set live stock.
`/book <resource> <n> <date> <HH:MM>-<HH:MM>` records phone or walk-in bookings. `/confirm <conversation>` turns a hold into a booking once the card or payment is in.
`/done <callback>`, `/outcome <conversation> booked|lost|resold [spend] [guests]`, `/holds`, `/queue`, `/pnl`, `/metrics`, `/gates`.
Owner only: `/autosend <class> on|off`, `/calibration`, `/approve <key> <value>`.

## Rollout (from the spec)

Weeks 1-2: measure the baseline. Weeks 3-6: review mode, where every message is a draft. After that, auto-send is switched on per decision class, in order, each only after its gate has been met for two weeks running:

1. *Keep the quote*: more than 85% of drafts accepted as-is, and no factual errors.
2. *Offer an approved concession*: more than 80% accepted, concession cost inside the cap every time, and at least 30 outcomes to measure close rates.
3. *Suggest an alternative*: more than 80% accepted.
4. *Escalate*: always human.

`/gates` shows the status; only the owner can switch auto-send on.

## Decisions and deviations from the spec

| Spec | This build | Why |
|---|---|---|
| WhatsApp Business API | Telegram | your choice; long polling, no public server needed |
| SevenRooms adapter | built-in calendar (bookings, holds, staff signals) | your choice; the adapter slot remains (`adapters:` in the profile) |
| Lightspeed POS | prices and costs in the profile; `/onhand` for live stock | no POS access; refresh costs from the POS quarterly |
| VIP row (S$509, 24.7%) | reproduced exactly | the only consistent reading is that the VIP area has its own opportunity cost of ~S$302.50, which is set as a labelled placeholder until the workbook confirms it |
| Welcome drink: front line up to 10 guests | front line up to 7 | 10 x S$14 = S$140 breaks the spec's own S$100 front-line rule; the validator rejects it |
| Case 7: 4-bottle waiver escalates | it escalates only when profitable | at the default 50% close rate it needs 74.7% to pay back, so it is declined with a 1-bottle counter. With a calibrated 80% it escalates, as the spec intends |
| Case 11: Sat 20:39 is out of hours | separate reservations-desk hours | the spec's staffed hours run until 22:00 |
| Floor-discount "true cost S$0" | contribution given up (S$119) | matches the spec's worked table and its authority logic |
| Workflow | a fixed six-step workflow; Claude uses structured outputs | stronger than tool-calling for the guarantee that nothing is promised before the engine permits it |

## Placeholders to replace before going live

Run `dealdesk validate <profile>`. For Salt these are:

* actual unit counts
* real COGS and perk costs from the POS
* the S$55 / S$45 spend cases
* regular contribution per bed and the P(resell) matrix (`profiles/salt_deal_policy.csv`)
* reservations-desk hours
* the cancellation window
* the paylink URL
* who holds manager authority on weekend evenings

The P(resell) matrix and close rates are guesses at launch. Every offer shows its break-even close rate so a manager can sanity-check it, and `/calibration` proposes updates from logged outcomes.

## Privacy and guardrails

The ledger stores what the engine needs: headcount, dates, offers, outcomes and the chat id. Message text is kept only for the model's context and is purged after 30 days (adjust for your PDPA retention). Card details are never handled; the agent sends the business's own paylink. The checker blocks drafts that:

* invent numbers, holds, urgency or perks
* say "not possible"
* mention margins, probabilities or policy

Concessions are driven by blocker, lead time and utilisation, never by who the customer is.

## Project layout

```
dealdesk/
  profile/   schema (validation on load), YAML and Excel loaders
  engine/    revenue, contribution, opportunity cost (slots, stock, hours, cash), evaluation and decision
  policy/    eligibility, holds, callbacks, escalation deadlines, staffed hours
  finance/   P&L posture, calibration proposals
  ledger/    SQLite: append-only offers, concessions, holds, bookings, reviews, escalations, events
  agent/     six-step turn, candidates, brief, checker, Claude brain, offline brain, template writer, watchers
  channels/  Telegram (HTTP) and an in-memory Telegram for tests
  staff/     review and escalation cards, staff commands
  analytics/ weekly metrics, rollout gates
tests/       spec numbers, 14 Salt acceptance cases, other businesses, checker, Claude (mocked), Telegram end to end
```

```bash
pip install -e '.[test]' && pytest -q
```
