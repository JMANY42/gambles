import pytest

from app import catalog, db, scenarios, sim


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "test.db")
    db.init_db()
    yield
    db.set_db_path(None)


def mk_market(ticker="HOUSEPA17-26-D", party="D", yes_bid=0.60, yes_ask=0.62, status="active",
              result=None, event="HOUSEPA17-26", **kw):
    m = {
        "ticker": ticker, "event_ticker": event, "series_ticker": "HOUSEPA17",
        "event_title": "PA-17 House winner?", "title": "t", "yes_sub_title": "Candidate",
        "category": "Elections", "chamber": "House", "state": "PA", "district": "17",
        "party": party, "is_midterm": 1, "status": status, "result": result,
        "yes_bid": yes_bid, "yes_ask": yes_ask,
        "no_bid": round(1 - yes_ask, 4), "no_ask": round(1 - yes_bid, 4),
        "last_price": yes_bid, "volume": 10, "open_interest": 5, "close_time": None,
    }
    m.update(kw)
    catalog.upsert_markets([m])
    return m


def new_pf(balance=1000.0):
    with db.tx() as conn:
        return sim.create_portfolio(conn, f"t{balance}", "Test", starting_balance=balance)


# --- fees ---

def test_kalshi_fee_matches_published_formula():
    # 0.07 * 100 * 0.5 * 0.5 = 1.75
    assert sim.kalshi_fee(100, 0.50) == 1.75
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounds UP to $0.02
    assert sim.kalshi_fee(1, 0.50) == 0.02
    # 0.07 * 10 * 0.9 * 0.1 = 0.063 -> $0.07
    assert sim.kalshi_fee(10, 0.90) == 0.07
    assert sim.kalshi_fee(0, 0.5) == 0.0


def test_contracts_for_budget_includes_fee():
    n = sim.contracts_for_budget(100, 0.50)
    assert n * 0.50 + sim.kalshi_fee(n, 0.50) <= 100
    assert (n + 1) * 0.50 + sim.kalshi_fee(n + 1, 0.50) > 100


# --- orders ---

def test_buy_yes_fills_at_ask_and_debits_cash():
    m = mk_market()
    pid = new_pf(1000)
    with db.tx() as conn:
        bet = sim.place_bet(conn, pid, m, "yes", contracts=100)
        pf = sim.get_portfolio(conn, pid)
    assert bet["entry_price"] == 0.62
    assert bet["cost"] == 62.0
    fee = sim.kalshi_fee(100, 0.62)
    assert bet["entry_fee"] == fee
    assert pf["cash"] == round(1000 - 62 - fee, 2)


def test_buy_no_uses_no_ask():
    m = mk_market(yes_bid=0.60, yes_ask=0.62)  # no_ask = 0.40
    pid = new_pf()
    with db.tx() as conn:
        bet = sim.place_bet(conn, pid, m, "no", contracts=10)
    assert bet["entry_price"] == 0.40


def test_insufficient_cash_rejected():
    m = mk_market()
    pid = new_pf(10)
    with db.tx() as conn, pytest.raises(sim.BetError, match="Insufficient"):
        sim.place_bet(conn, pid, m, "yes", contracts=100)


def test_cannot_bet_on_closed_market():
    m = mk_market(status="closed")
    pid = new_pf()
    with db.tx() as conn, pytest.raises(sim.BetError, match="not open"):
        sim.place_bet(conn, pid, m, "yes", contracts=1)


# --- settlement ---

def test_settlement_win_and_loss():
    m = mk_market()
    pid = new_pf(1000)
    with db.tx() as conn:
        yes = sim.place_bet(conn, pid, m, "yes", contracts=100)
        no = sim.place_bet(conn, pid, m, "no", contracts=50)
        cash_before = sim.get_portfolio(conn, pid)["cash"]
        settled = sim.settle_market(conn, {**m, "result": "yes"})
        assert settled == 2
        won = dict(conn.execute("SELECT * FROM bets WHERE id=?", (yes["id"],)).fetchone())
        lost = dict(conn.execute("SELECT * FROM bets WHERE id=?", (no["id"],)).fetchone())
        cash_after = sim.get_portfolio(conn, pid)["cash"]
    assert won["status"] == "won" and won["proceeds"] == 100.0
    assert won["pnl"] == round(100 - yes["cost"] - yes["entry_fee"], 2)
    assert lost["status"] == "lost" and lost["proceeds"] == 0.0
    assert lost["pnl"] == -round(no["cost"] + no["entry_fee"], 2)
    assert cash_after == round(cash_before + 100, 2)


def test_settlement_is_idempotent():
    m = mk_market()
    pid = new_pf()
    with db.tx() as conn:
        sim.place_bet(conn, pid, m, "yes", contracts=10)
        assert sim.settle_market(conn, {**m, "result": "no"}) == 1
        assert sim.settle_market(conn, {**m, "result": "no"}) == 0


def test_sell_early_at_bid():
    m = mk_market(yes_bid=0.60, yes_ask=0.62)
    pid = new_pf(1000)
    with db.tx() as conn:
        bet = sim.place_bet(conn, pid, m, "yes", contracts=100)
    m2 = mk_market(yes_bid=0.80, yes_ask=0.82)
    with db.tx() as conn:
        sold = sim.sell_bet(conn, bet["id"], m2)
    exit_fee = sim.kalshi_fee(100, 0.80)
    assert sold["status"] == "sold" and sold["exit_price"] == 0.80
    assert sold["pnl"] == round(80 - exit_fee - 62 - bet["entry_fee"], 2)


# --- reporting ---

def test_summary_marks_open_positions_at_mid():
    m = mk_market(yes_bid=0.60, yes_ask=0.62)
    pid = new_pf(1000)
    with db.tx() as conn:
        bet = sim.place_bet(conn, pid, m, "yes", contracts=100)
    mk_market(yes_bid=0.70, yes_ask=0.72)  # price moves up; mid = 0.71
    with db.tx() as conn:
        s = sim.portfolio_summary(conn, pid)
    assert s["positions_value"] == 71.0
    assert s["unrealized_pnl"] == round(71 - 62 - bet["entry_fee"], 2)
    assert s["equity"] == round(s["cash"] + 71, 2)
    assert s["max_payout_if_all_win"] == 100.0


def test_summary_win_rate_and_groups():
    a = mk_market("SENATEPA-26-D", event="SENATEPA-26", chamber="Senate")
    b = mk_market("HOUSEPA17-26-R", party="R")
    pid = new_pf(1000)
    with db.tx() as conn:
        sim.place_bet(conn, pid, a, "yes", contracts=10)
        sim.place_bet(conn, pid, b, "yes", contracts=10)
        sim.settle_market(conn, {**a, "result": "yes"})
        sim.settle_market(conn, {**b, "result": "no"})
        s = sim.portfolio_summary(conn, pid)
    assert s["counts"]["won"] == 1 and s["counts"]["lost"] == 1
    assert s["win_rate_pct"] == 50.0
    assert {g["group"] for g in s["by_party"]} == {"D", "R"}
    assert s["total_pnl"] == round(s["equity"] - 1000, 2)


# --- catalog parsing ---

@pytest.mark.parametrize("ticker,expected", [
    ("CONTROLH", ("Control", None, "House")),
    ("CONTROLS", ("Control", None, "Senate")),
    ("SENATEPA", ("Senate", "PA", None)),
    ("SENATEPARTY-OH", ("Senate", "OH", None)),
    ("SENATEPARTYNES", ("Senate", "NE", "special")),
    ("KXSENATEAK", ("Senate", "AK", None)),
    ("GOVPARTYAZ", ("Governor", "AZ", None)),
    ("GOVPARTY-NH", ("Governor", "NH", None)),
    ("HOUSEPA17", ("House", "PA", "17")),
    ("HOUSENJ09", ("House", "NJ", "9")),
    ("HOUSEPARTY-MI07", ("House", "MI", "7")),
    ("HOUSETX18S", ("House", "TX", "18 (special)")),
])
def test_classify_series(ticker, expected):
    r = catalog.classify_series(ticker)
    assert (r["chamber"], r["state"], r["district"]) == expected


@pytest.mark.parametrize("ticker", ["KXSENATENHR", "KXGOVCANOMD", "RSENATESEATS", "KXREDWAVECOMBO", "SENATE"])
def test_classify_series_rejects_non_races(ticker):
    assert catalog.classify_series(ticker) is None


def test_market_party_inference():
    assert catalog.market_party({"ticker": "GOVPARTYAZ-26-D"}) == "D"
    assert catalog.market_party({"ticker": "CONTROLS-2026-R"}) == "R"
    assert catalog.market_party({"ticker": "X-Y-Z", "custom_strike": {"Party": "Democratic"}}) == "D"
    assert catalog.market_party({"ticker": "SENATENE-26-DOSB", "yes_sub_title": "Dan Osborn"}) is None


def test_normalize_market_parses_dollar_strings():
    raw = {"ticker": "SENATEPA-26-D", "event_ticker": "SENATEPA-26", "status": "active",
           "yes_bid_dollars": "0.6200", "yes_ask_dollars": "0.6700", "no_bid_dollars": "0.3300",
           "no_ask_dollars": "0.3800", "last_price_dollars": "0.6500", "result": ""}
    m = catalog.normalize_market(raw, {"title": "PA Senate"}, {"chamber": "Senate", "state": "PA", "district": None})
    assert m["yes_ask"] == 0.67 and m["party"] == "D" and m["result"] is None and m["is_midterm"] == 1


# --- scenarios ---

def test_scenarios_bet_one_side_per_race():
    mk_market("SENATEPA-26-D", party="D", event="SENATEPA-26", yes_bid=0.6, yes_ask=0.62)
    mk_market("SENATEPA-26-R", party="R", event="SENATEPA-26", yes_bid=0.37, yes_ask=0.39)
    mk_market("HOUSEPA17-26-D", party="D", yes_bid=0.96, yes_ask=0.98)
    mk_market("HOUSEPA17-26-R", party="R", yes_bid=0.02, yes_ask=0.03)
    mk_market("HOUSEPA17-26-I", party="I", yes_bid=0.0, yes_ask=0.01)
    res = scenarios.seed_all()
    assert [r["bets_placed"] for r in res] == [2, 2]
    with db.tx() as conn:
        dem = db.rows(conn, "SELECT b.*, m.party FROM bets b JOIN markets m ON m.ticker=b.market_ticker "
                            "JOIN portfolios p ON p.id=b.portfolio_id WHERE p.slug='all-dem'")
        gop = db.rows(conn, "SELECT b.*, m.party FROM bets b JOIN markets m ON m.ticker=b.market_ticker "
                            "JOIN portfolios p ON p.id=b.portfolio_id WHERE p.slug='all-gop'")
    assert {b["party"] for b in dem} == {"D"} and {b["party"] for b in gop} == {"R"}
    assert all(b["side"] == "yes" for b in dem + gop)
    # Each bet spends close to (but never over) the $100 stake.
    assert all(90 <= b["cost"] + b["entry_fee"] <= 100 for b in dem + gop)
    # Re-seeding without reset is a no-op.
    assert all(r["status"] == "exists" for r in scenarios.seed_all())


def test_mark_price_falls_back_and_settles():
    assert sim.mark_price({"status": "closed", "last_price": 0.3, "yes_bid": 0, "yes_ask": 0}, "no") == 0.7
    assert sim.mark_price({"status": "finalized", "result": "yes"}, "yes") == 1.0
    assert sim.mark_price({"status": "finalized", "result": "yes"}, "no") == 0.0


def test_candles_to_points_uses_trade_then_mid():
    candles = [
        {"end_period_ts": 100, "price": {"close_dollars": "0.7400"},
         "yes_bid": {"close_dollars": "0.7300"}, "yes_ask": {"close_dollars": "0.7500"}, "volume_fp": "10"},
        # no trade this period -> bid/ask midpoint
        {"end_period_ts": 160, "price": {"close_dollars": None},
         "yes_bid": {"close_dollars": "0.7000"}, "yes_ask": {"close_dollars": "0.7200"}},
        # no trade and no quote -> skipped
        {"end_period_ts": 220, "price": {}, "yes_bid": {}, "yes_ask": {}},
    ]
    pts = catalog.candles_to_points(candles)
    assert [(p["t"], p["yes"]) for p in pts] == [(100, 0.74), (160, 0.71)]
