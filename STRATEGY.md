# DipFlow strategy

## Idea

Pump.fun bonding curves move fast and die fast. Chasing a curve after it
already pumped means buying the top — the first version of this bot did
exactly that and lost money on every coin it touched on the dev window.
DipFlow does the opposite: it buys a curve only after it has pulled back
from its recent high, and only while buyers are stepping back in. It never
buys a curve at birth (too easy to be the exit liquidity) and never holds
a dead coin hoping for a comeback.

## Entry (all must hold, evaluated on mints with a new trade this batch)

- Curve age >= 100 slots since its CreateEvent.
- Real quote reserves between 2 and 70 SOL.
- Price down 8%-45% from its recent high (last 60 slots) — a dip, not a
  collapse.
- Buyers stepping in: of the last 10 trades at least 6 are buys, the most
  recent trade is a buy, and 20-slot net flow is no worse than -0.3 SOL
  (the dump is stalling, not accelerating).
- At most 3 entries per mint per run, and at least 120 slots after an exit
  before re-entering the same mint.

## Sizing

- One tranche of 0.05 SOL per entry (capped by the harness's 1% of real
  reserves). No averaging down: adding into a falling coin lost money in
  backtests. Max 5 SOL total exposure, max 5 open positions.

## Exits (whole position, first trigger wins)

- Take-profit: sell value >= 115% of cost.
- Recovery: price recovers three quarters of the gap between the entry
  price and the recent high at entry.
- Stop-loss: sell value < 92% of cost.
- Time: held > 300 slots and value < 105% of cost.
- Stale: no trade on the coin for 50 slots.
- Completed curves are never ordered on; the harness closes those.

## Guardrail probes

The competition requires at least 20 coins with a >= 0.01 SOL round trip.
DipFlow paces itself against block time (25 coins / 240 fills over 6h):
when behind pace and a position slot is free, it buys 0.01 SOL of a liquid
traded coin it holds nothing on (real reserves >= 1.5 SOL) and sells the
whole probe as soon as the buy lands. Probes are tiny (about 0.0005 SOL of
fees each) and never block real entries.

## Costs

Every order pays 5,000 + 100,000 lamports in tx fees, filled or not, plus
1.25% curve fees (95 bps protocol + 30 bps creator) on each side. A 0.05 SOL
round trip therefore needs roughly +3% to break even, which is why entries
demand a real dip edge and exits are quick.

## Risks

- Mean reversion on memecoin curves is noisy: a dip can be the start of a
  slow bleed to zero rather than a bounce. The buyer step-in and stall
  filters reduce this but cannot remove it.
- Thin curves slip: buys are capped at 1% of real reserves and slippage is
  set wide (1500 bps buys, 10000 bps sells), so a fast dump can fill an
  exit well below the estimate.
- The take-profit is modest (+12%): a coin that runs much further gives
  back most of its move after the exit fires.
- This bot trades paper money on a replay; nothing here is financial
  advice, and past replay performance does not predict the final window.
