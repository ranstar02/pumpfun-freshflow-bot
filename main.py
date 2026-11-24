"""DipFlow — a mean-reversion dip bot for pump.fun bonding curves.

Strategy (agent-fun/replay/v2, Python):
  * Watch every curve's recent high (last 60 slots). Buy the dip: a curve
    that is down 8%-45% from its recent high, still holds 2-70 SOL of real
    reserves, and shows buyers stepping back in — of its last 10 trades at
    least 6 are buys, the most recent trade is a buy, and 20-slot net flow
    is no worse than -0.3 SOL (the dump is stalling, not accelerating).
  * One tranche of 0.05 SOL per entry, no averaging down (adding into a
    falling coin lost money in backtests). Max 5 SOL total exposure.
  * Exit the whole position on take-profit (+15% of cost), on recovering
    three quarters of the dip gap from entry to the recent high, on
    stop-loss (-8%), after 300 slots held without +5%, or when the coin
    goes 50 slots with no trade at all.
  * Guardrail probes: when the run is behind pace (25 coins / 240 fills
    over 6h of block time), buy 0.01 SOL of a liquid traded coin held
    nowhere and sell it as soon as the buy lands, so the run clears the
    20-coin round-trip floor even on a quiet tape.

Deterministic: no clock, environment or unseeded randomness; dicts are only
iterated in insertion order; money is integer lamports throughout.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- entry filter ---
HIGH_WINDOW = 60
DD_MIN = 0.08               # price down at least 8% from the recent high
DD_MAX = 0.45               # ... and at most 45% (a dip, not a collapse)
MIN_AGE_SLOTS = 100         # curve must be at least this old
MIN_REAL_SOL = 2 * SOL      # real quote reserves floor
MAX_REAL_SOL = 70 * SOL     # real quote reserves ceiling
FLOW_TRADES = 10            # last N trades examined for buyer step-in
MIN_BUYS = 6                # ... at least this many must be buys
FLOW_WINDOW = 20            # slots of tape the stall check looks back over
MIN_NET_SOL = -3 * SOL // 10  # 20-slot net flow no worse than -0.3 SOL
MAX_ENTRIES_PER_MINT = 3    # entries per mint over the whole run
REENTRY_COOLDOWN = 120      # slots to wait after an exit before re-entering

# --- sizing ---
TRANCHE = SOL // 20         # 0.05 SOL per tranche
MAX_TRANCHES = 1
MAX_INVEST = 3 * TRANCHE    # 0.15 SOL invested per position
MAX_EXPOSURE = 5 * SOL      # total invested across positions
MIN_BUY = SOL // 100        # 0.01 SOL: smallest buy worth sending
ADD_BAND_NUM = 88           # adds only while price within 88%-112% of entry
ADD_BAND_DEN = 100
ADD_MIN_DD = 0.05           # ... and the dip is still >= 5% off the high

# --- exits ---
TP_NUM = 115
TP_DEN = 100
RECOVER_SHARE_NUM = 3
RECOVER_SHARE_DEN = 4
STOP_NUM = 92
STOP_DEN = 100
MAX_HOLD_SLOTS = 300
TIME_EXIT_NUM = 105         # ... unless value >= 105% of cost
TIME_EXIT_DEN = 100
STALE_SLOTS = 50

# --- guardrail probes ---
PROBE_BUY = SOL // 100      # 0.01 SOL
PROBE_MIN_REAL = 3 * SOL // 2  # need >= 1.5 SOL real reserves (1% cap >= 0.015)
PACE_WINDOW_SEC = 21600     # 6 hours of block time
PACE_COINS = 25
PACE_FILLS = 240

CURVE_FEE_BPS = 125         # protocol 95 bps + creator 30 bps
BUY_SLIPPAGE_BPS = 1500
SELL_SLIPPAGE_BPS = 10000

# --- per-run state (one process = one run) ---
tape = {}             # mint -> [(slot, is_buy, sol_lamports, user, price)] slot-ordered
created_slot = {}     # mint -> slot of its CreateEvent
entry_count = {}      # mint -> entries sent
last_exit_slot = {}   # mint -> slot a position on it last closed
entry_slot = {}       # mint -> slot the open position first appeared
entry_price = {}      # mint -> decision price (SOL/token) of the first buy
entry_high = {}       # mint -> recent high (SOL/token) at first buy
last_buy_price = {}   # mint -> decision price of the last buy
tranches = {}         # mint -> tranches bought so far
probe_clients = {}    # client_id -> mint, probe buys still in flight
probe_pending = set()  # mints with a probe buy in flight
probe_to_sell = {}    # mint -> True (insertion order), probe buys landed, sell next
filled_buy_coins = set()  # mints with at least one filled buy
filled_buy_count = 0
first_block_time = None
last_block_time = None
known_positions = set()


def price_of(curve):
    """SOL per token from virtual reserves, 0.0 when unknown."""
    vt = curve.virtual_token_reserves
    vq = curve.virtual_quote_reserves
    if vt <= 0 or vq <= 0:
        return 0.0
    return vq / vt


def sell_value_lamports(tokens, curve, tx_fee):
    """What selling `tokens` now would pay, after the 1.25% curve fee and one tx fee."""
    vt = curve.virtual_token_reserves
    vq = curve.virtual_quote_reserves
    if tokens <= 0 or vt <= 0 or vq <= 0:
        return 0
    gross = (tokens * vq) // (vt + tokens)
    net = gross * (10000 - CURVE_FEE_BPS) // 10000
    return net - tx_fee


def prune_tape(mint, now_slot, keep):
    """Drop tape entries older than `keep` slots; return the kept list."""
    trades = tape.get(mint)
    if not trades:
        return []
    lo = now_slot - keep
    cut = 0
    while cut < len(trades) and trades[cut][0] < lo:
        cut += 1
    if cut:
        del trades[:cut]
    return trades


def dip_stats(mint, now_slot):
    """Dip/flow stats for a mint from its tape (pruned to HIGH_WINDOW).

    Returns None when the mint has no trades in the window.
    """
    trades = prune_tape(mint, now_slot, HIGH_WINDOW)
    if not trades:
        return None
    price_high = 0.0
    for t in trades:
        if t[4] > price_high:
            price_high = t[4]
    # last FLOW_TRADES trades: buyer step-in
    recent = trades[-FLOW_TRADES:]
    buys = 0
    for t in recent:
        if t[1]:
            buys += 1
    last_is_buy = trades[-1][1]
    # 20-slot net flow
    lo = now_slot - FLOW_WINDOW
    net20 = 0
    for t in trades:
        if t[0] >= lo:
            net20 += t[2] if t[1] else -t[2]
    return {
        "price_high": price_high,
        "buys": buys,
        "last_is_buy": last_is_buy,
        "net20": net20,
        "last_slot": trades[-1][0],
    }


def on_batch(state, batch):
    global filled_buy_count, first_block_time, last_block_time
    tx = state.tx_fee_lamports()
    slot = state.slot
    caps = state.hello["caps"]
    max_orders = caps["max_orders_per_batch"]
    max_positions = caps["max_open_positions"]

    # --- 1. fold this batch's events into the tape -----------------------
    traded_mints = []  # mints with a trade this batch, in event order
    traded_seen = set()
    for e in batch.get("events", []):
        bt = e.get("block_time")
        if type(bt) is int:
            if first_block_time is None or bt < first_block_time:
                first_block_time = bt
            if last_block_time is None or bt > last_block_time:
                last_block_time = bt
        mint = e.get("mint")
        kind = e.get("event")
        if kind == "CreateEvent":
            if mint not in created_slot:
                created_slot[mint] = int(e["slot"])
        elif kind == "TradeEvent":
            vt = int(e.get("virtual_token_reserves") or 0)
            vq = int(e.get("virtual_quote_reserves") or 0)
            sol = int(e.get("sol_amount") or 0)
            is_buy = bool(e.get("is_buy"))
            if vt > 0 and vq > 0 and sol > 0:
                tape.setdefault(mint, []).append(
                    (int(e["slot"]), is_buy, sol, e.get("user"), vq / vt)
                )
            if mint not in traded_seen:
                traded_seen.add(mint)
                traded_mints.append(mint)

    # --- 2. fold fills: count our own filled buys, track probes ---------
    for f in batch.get("fills", []):
        cid = f.get("client_id", "")
        if cid.startswith("_"):
            continue  # harness closes, not our orders
        status = f.get("status")
        mint = f.get("mint")
        if status == "filled" and f.get("side") == "buy":
            filled_buy_count += 1
            filled_buy_coins.add(mint)
            if cid in probe_clients:
                probe_pending.discard(mint)
                probe_to_sell[mint] = True
                del probe_clients[cid]
        elif status in ("rejected", "failed"):
            if cid in probe_clients:
                probe_pending.discard(mint)
                del probe_clients[cid]

    # --- 3. position lifecycle: note closes, stamp new positions --------
    current = set(state.positions)
    pending_buy_mints = set()
    pending_sell_mints = set()
    for p in state.pending.values():
        if p.order["side"] == "buy":
            pending_buy_mints.add(p.order["mint"])
        else:
            pending_sell_mints.add(p.order["mint"])
    for mint in list(known_positions):
        if mint not in current and mint not in pending_buy_mints:
            last_exit_slot[mint] = slot
            entry_slot.pop(mint, None)
            entry_price.pop(mint, None)
            entry_high.pop(mint, None)
            last_buy_price.pop(mint, None)
            tranches.pop(mint, None)
            probe_to_sell.pop(mint, None)
    known_positions.clear()
    known_positions.update(current)
    for mint in state.positions:
        if mint not in entry_slot:
            entry_slot[mint] = slot

    # --- 4. build orders: exits, probe sells, entries, adds, probe buy --
    orders = []
    ordered_buy_mints = set()
    free = state.free_cash_lamports()
    exposure = 0
    for h in state.positions.values():
        exposure += h.cost_lamports
    for p in state.pending.values():
        if p.order["side"] == "buy":
            exposure += int(p.order["quote_lamports"])
    slots_free = max_positions - state.committed_position_count()

    def send_buy(mint, amount):
        cid = state.client_id()
        orders.append(
            {
                "side": "buy",
                "client_id": cid,
                "mint": mint,
                "quote_lamports": amount,
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            }
        )
        ordered_buy_mints.add(mint)
        return cid

    def send_sell(mint, tokens):
        orders.append(
            {
                "side": "sell",
                "mint": mint,
                "token_amount": tokens,
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            }
        )

    # exits first
    for mint in list(state.positions):
        if len(orders) >= max_orders:
            break
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        if mint in pending_sell_mints:
            continue
        holding = state.positions[mint]
        cost = holding.cost_lamports
        value = sell_value_lamports(holding.token_amount, curve, tx)
        price_now = price_of(curve)
        take_profit = TP_DEN * value >= TP_NUM * cost
        recover = False
        eprice = entry_price.get(mint)
        ehigh = entry_high.get(mint)
        if eprice is not None and ehigh is not None and ehigh > eprice:
            target = eprice + (ehigh - eprice) * RECOVER_SHARE_NUM / RECOVER_SHARE_DEN
            recover = price_now >= target
        stop = STOP_DEN * value < STOP_NUM * cost
        held = slot - entry_slot.get(mint, slot)
        time_exit = held > MAX_HOLD_SLOTS and TIME_EXIT_DEN * value < TIME_EXIT_NUM * cost
        stats = dip_stats(mint, slot)
        stale = stats is not None and slot - stats["last_slot"] > STALE_SLOTS
        if take_profit or recover or stop or time_exit or stale:
            send_sell(mint, holding.token_amount)
            free -= tx

    # probe sells: a landed probe is sold whole at once
    for mint in list(probe_to_sell):
        if len(orders) >= max_orders:
            break
        probe_to_sell.pop(mint, None)
        if mint not in state.positions:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        if mint in pending_sell_mints:
            continue
        send_sell(mint, state.positions[mint].token_amount)
        free -= tx

    # entries: only on mints with a new trade this batch
    for mint in traded_mints:
        if len(orders) >= max_orders:
            break
        if mint in state.positions or mint in ordered_buy_mints:
            continue
        if mint in pending_buy_mints or mint in probe_pending or mint in probe_to_sell:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        created = created_slot.get(mint)
        if created is None:
            created = curve.created_slot if curve.created_slot is not None else slot
        if slot - created < MIN_AGE_SLOTS:
            continue
        if entry_count.get(mint, 0) >= MAX_ENTRIES_PER_MINT:
            continue
        last_exit = last_exit_slot.get(mint)
        if last_exit is not None and slot - last_exit < REENTRY_COOLDOWN:
            continue
        real = curve.real_quote_reserves
        if real < MIN_REAL_SOL or real > MAX_REAL_SOL:
            continue
        stats = dip_stats(mint, slot)
        if stats is None:
            continue
        price_now = price_of(curve)
        price_high = stats["price_high"]
        if price_now <= 0.0 or price_high <= 0.0:
            continue
        dip = 1.0 - price_now / price_high
        if dip < DD_MIN or dip > DD_MAX:
            continue
        if stats["buys"] < MIN_BUYS or not stats["last_is_buy"]:
            continue
        if stats["net20"] < MIN_NET_SOL:
            continue
        if slots_free <= 0:
            continue
        amount = min(TRANCHE, state.max_buy_lamports(mint))
        if amount < MIN_BUY:
            continue
        if exposure + amount > MAX_EXPOSURE:
            continue
        if free < amount + tx:
            continue
        send_buy(mint, amount)
        free -= amount + tx
        exposure += amount
        slots_free -= 1
        entry_count[mint] = entry_count.get(mint, 0) + 1
        last_buy_price[mint] = price_now
        entry_price[mint] = price_now
        entry_high[mint] = price_high
        tranches[mint] = 1

    # tranche adds into an open dip while it stays a dip
    for mint in list(state.positions):
        if len(orders) >= max_orders:
            break
        if mint in ordered_buy_mints or mint in probe_pending or mint in probe_to_sell:
            continue
        if mint in pending_buy_mints:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        if tranches.get(mint, 0) >= MAX_TRANCHES:
            continue
        holding = state.positions[mint]
        if holding.cost_lamports >= MAX_INVEST:
            continue
        price_now = price_of(curve)
        if price_now <= 0.0:
            continue
        eprice = entry_price.get(mint)
        if eprice is None:
            continue
        if price_now < eprice * ADD_BAND_NUM / ADD_BAND_DEN:
            continue
        if price_now > eprice * (2 * ADD_BAND_DEN - ADD_BAND_NUM) / ADD_BAND_DEN:
            continue
        stats = dip_stats(mint, slot)
        if stats is None:
            continue
        price_high = stats["price_high"]
        if price_high <= 0.0:
            continue
        dip = 1.0 - price_now / price_high
        if dip < ADD_MIN_DD:
            continue
        if stats["buys"] < MIN_BUYS or not stats["last_is_buy"]:
            continue
        amount = min(TRANCHE, state.max_buy_lamports(mint))
        if amount < MIN_BUY:
            continue
        if exposure + amount > MAX_EXPOSURE:
            continue
        if free < amount + tx:
            continue
        send_buy(mint, amount)
        free -= amount + tx
        exposure += amount
        last_buy_price[mint] = price_now
        tranches[mint] = tranches.get(mint, 0) + 1

    # guardrail probe: stay on pace for 25 coins / 240 fills over 6h
    probes_open = len(probe_pending) + len(probe_to_sell)
    if probes_open < 1 and slots_free > 0 and len(orders) < max_orders:
        pace = 0.0
        if (
            first_block_time is not None
            and last_block_time is not None
            and last_block_time > first_block_time
        ):
            elapsed = last_block_time - first_block_time
            pace = elapsed / PACE_WINDOW_SEC
            if pace > 1.0:
                pace = 1.0
        need_coins = -(-PACE_COINS * int(pace * PACE_WINDOW_SEC) // PACE_WINDOW_SEC) if pace > 0 else 0
        need_fills = -(-PACE_FILLS * int(pace * PACE_WINDOW_SEC) // PACE_WINDOW_SEC) if pace > 0 else 0
        if len(filled_buy_coins) < need_coins or filled_buy_count < need_fills:
            for mint in traded_mints:
                if mint in state.positions or mint in ordered_buy_mints:
                    continue
                if mint in pending_buy_mints or mint in probe_pending or mint in probe_to_sell:
                    continue
                curve = state.curves.get(mint)
                if curve is None or curve.complete:
                    continue
                if curve.real_quote_reserves < PROBE_MIN_REAL:
                    continue
                amount = min(PROBE_BUY, state.max_buy_lamports(mint))
                if amount < PROBE_BUY:
                    continue
                if exposure + amount > MAX_EXPOSURE:
                    continue
                if free < amount + tx:
                    continue
                cid = send_buy(mint, amount)
                probe_clients[cid] = mint
                probe_pending.add(mint)
                free -= amount + tx
                exposure += amount
                slots_free -= 1
                break

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
