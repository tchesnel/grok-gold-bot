from datetime import datetime
import math
import json
from pathlib import Path

from run_once_v41 import (
    SYMBOL,
    send_telegram,
    fetch_frames,
    get_tf,
    analyze,
    macro_filter,
    score_signal,
    print_hold,
    cfg,
)

from run_once_v42 import detect_formation
from run_once_v43 import fetch_m5_twelvedata, impulse_engine
from run_once_v44 import (
    load_state,
    save_state,
    append_history,
    update_tracking,
    now_utc,
    parse_time,
)

from run_once_v45 import smart_money_engine
from run_once_v47 import session_context, news_guard_status, build_event, apply_guards
from run_once_v48 import build_brain
from run_once_v49 import evaluate_alerts_with_current_price
from run_once_v50 import maybe_send_status_report, send_error_report


TRADE_PLAN_MIN_CONF = float(cfg("TRADE_PLAN_MIN_CONF", "68"))
BIG_MOVE_MIN_SCORE = float(cfg("BIG_MOVE_MIN_SCORE", "70"))
PLAN_COOLDOWN_MIN = int(cfg("PLAN_COOLDOWN_MIN", "25"))


def fnum(x, default=0.0):
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def round_price(x):
    return round(fnum(x), 2)


def get_df_from_analysis(a):
    df = a.get("df")
    if df is None:
        return None

    try:
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
        return df
    except Exception:
        return None


def calc_atr(df, n=14, fallback=6.0):
    if df is None or len(df) < n + 2:
        return fallback

    try:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        tr = tr1.combine(tr2, max).combine(tr3, max)
        atr = float(tr.tail(n).mean())

        return max(atr, 3.0)
    except Exception:
        return fallback


def get_recent_levels(m15a, side, price, atr):
    df = get_df_from_analysis(m15a)

    prev_high = fnum(m15a.get("prev_12_high"), price + atr)
    prev_low = fnum(m15a.get("prev_12_low"), price - atr)

    if df is not None and len(df) >= 25:
        try:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            prev_high = float(high.iloc[-21:-1].max())
            prev_low = float(low.iloc[-21:-1].min())
        except Exception:
            pass

    if side == "LONG":
        return prev_high, prev_low

    return prev_low, prev_high


def detect_big_move(m5, m15a, h1a, h4a, macro, session, guard):
    df = None

    if m5 is not None:
        try:
            df = m5.copy()
            df.columns = [str(c).lower() for c in df.columns]
        except Exception:
            df = None

    if df is None:
        df = get_df_from_analysis(m15a)

    if df is None or len(df) < 25:
        return None

    try:
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        price = float(close.iloc[-1])
        c3 = float(close.iloc[-4])
        move3 = price - c3

        ranges = high - low
        avg_range = float(ranges.iloc[-21:-1].mean())
        avg_range = max(avg_range, 0.5)

        prev20_high = float(high.iloc[-21:-1].max())
        prev20_low = float(low.iloc[-21:-1].min())

        side = None
        level = None

        if move3 > max(5.0, avg_range * 2.2) and price > prev20_high:
            side = "LONG"
            level = prev20_high

        if move3 < -max(5.0, avg_range * 2.2) and price < prev20_low:
            side = "SHORT"
            level = prev20_low

        if side is None:
            return None

        score = 60
        score += min(20, abs(move3) / avg_range * 4)

        dxy = str(macro.get("dxy_bias", "")).upper()

        if side == "LONG" and dxy == "DOWN":
            score += 8
        if side == "SHORT" and dxy == "UP":
            score += 8

        if session.get("name") in ("LONDON", "NEW YORK OPEN", "NEW YORK"):
            score += 7

        if guard.get("active"):
            score -= 18

        score = max(0, min(100, round(score, 1)))

        if score < BIG_MOVE_MIN_SCORE:
            return None

        return {
            "kind": "BIG_MOVE",
            "side": side,
            "score": score,
            "price": price,
            "level": level,
            "move3": move3,
            "avg_range": avg_range,
            "message": "Big Move Detector actif",
        }

    except Exception as e:
        print(f"[V5.1] Big move detector impossible: {e}")
        return None


def confidence_from_event(event, guard, session):
    brain = fnum(event.get("brain_score"), None)

    if brain is None:
        brain = fnum(event.get("score"), 50)

    conf = brain

    if event.get("kind") == "TRADE":
        conf += 5

    if event.get("kind") == "BIG_MOVE":
        conf += 3

    if guard.get("active"):
        conf -= 15

    if session.get("name") == "ASIA":
        conf -= 5

    if session.get("name") == "OFF HOURS":
        conf -= 8

    return max(0, min(100, round(conf, 1)))


def build_trade_plan(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard, current_price):
    if event.get("kind") == "NONE":
        return None

    if event.get("filtered"):
        return None

    side = event.get("side")

    if side not in ("LONG", "SHORT"):
        return None

    action = "BUY" if side == "LONG" else "SELL"
    price = fnum(event.get("price"), current_price)

    df = get_df_from_analysis(m15a)
    atr = calc_atr(df, fallback=6.0)

    level = fnum(event.get("level"), price)
    confidence = confidence_from_event(event, guard, session)

    if confidence < TRADE_PLAN_MIN_CONF:
        print(f"[V5.1] Plan ignoré: confiance {confidence} < {TRADE_PLAN_MIN_CONF}")
        return None

    entry_type = "CONFIRMATION"
    entry = price

    if event.get("kind") == "TRADE":
        entry = fnum(event.get("entry"), fnum(sig.get("entry"), price))
        sl = fnum(event.get("sl"), fnum(sig.get("sl"), 0))
        risk = abs(entry - sl)

        if risk < 3:
            risk = max(atr * 1.15, 5.0)
            sl = entry - risk if side == "LONG" else entry + risk

        entry_type = "ENTRÉE VALIDÉE"

    elif event.get("kind") in ("FORMATION", "BIG_MOVE"):
        # Formation = pas forcément entrée au marché. On donne une entrée conditionnelle.
        if side == "LONG":
            entry = max(level, price)
        else:
            entry = min(level, price)

        risk = max(atr * 1.25, 5.0)

        prev_key, invalid_key = get_recent_levels(m15a, side, price, atr)

        if side == "LONG":
            sl = min(invalid_key - atr * 0.35, entry - risk)
        else:
            sl = max(invalid_key + atr * 0.35, entry + risk)

        entry_type = "ENTRÉE SI CASSURE / RETEST"

    elif event.get("kind") == "IMPULSE":
        # Si le move est déjà parti, on préfère une entrée sur pullback/retest.
        if side == "LONG":
            entry = min(price, level + atr * 0.15)
        else:
            entry = max(price, level - atr * 0.15)

        risk = max(atr * 1.3, 5.0)
        sl = entry - risk if side == "LONG" else entry + risk
        entry_type = "PULLBACK / RETEST"

    else:
        risk = max(atr * 1.25, 5.0)
        sl = price - risk if side == "LONG" else price + risk
        entry = price

    risk = abs(entry - sl)
    risk = max(risk, 4.0)

    if side == "LONG":
        tp1 = entry + risk * 1.0
        tp2 = entry + risk * 1.8
        tp3 = entry + risk * 2.8
        invalidation = sl
    else:
        tp1 = entry - risk * 1.0
        tp2 = entry - risk * 1.8
        tp3 = entry - risk * 2.8
        invalidation = sl

    reasons = []

    for r in event.get("good_reasons", [])[:6]:
        reasons.append(f"✅ {r}")

    for r in event.get("bad_reasons", [])[:4]:
        reasons.append(f"⚠️ {r}")

    if event.get("kind") == "BIG_MOVE":
        reasons.insert(0, "🚨 Gros mouvement détecté : accélération + cassure niveau court terme")

    if not reasons:
        reasons.append("✅ Score technique suffisant selon le moteur de confluence")

    return {
        "kind": "TRADE_PLAN",
        "source_kind": event.get("kind"),
        "side": side,
        "action": action,
        "entry_type": entry_type,
        "entry": round_price(entry),
        "sl": round_price(sl),
        "tp1": round_price(tp1),
        "tp2": round_price(tp2),
        "tp3": round_price(tp3),
        "risk": round_price(risk),
        "price": round_price(price),
        "confidence": confidence,
        "tier": event.get("tier", ""),
        "brain_score": event.get("brain_score", ""),
        "score": event.get("score", 0),
        "session": session.get("name", ""),
        "news_guard": guard.get("active", False),
        "reasons": reasons,
        "invalidation": round_price(invalidation),
    }


def plan_key(plan):
    bucket = round(fnum(plan.get("entry"), 0) / 2) * 2
    return f"{plan.get('kind')}:{plan.get('action')}:{bucket}"


def plan_should_send(state, plan):
    key = plan_key(plan)

    sent = state.get("last_trade_plans", {})
    last = sent.get(key)

    if last:
        t = parse_time(last)

        if t:
            age_min = (now_utc() - t).total_seconds() / 60

            if age_min < PLAN_COOLDOWN_MIN:
                return False, f"plan similaire déjà envoyé il y a {int(age_min)} min"

    return True, "nouveau trade plan"


def register_plan(state, plan):
    key = plan_key(plan)

    state.setdefault("last_trade_plans", {})
    state["last_trade_plans"][key] = now_utc().isoformat()

    # On l’ajoute aussi au suivi V4.9 pour évaluation automatique.
    state.setdefault("tracked_alerts", [])

    state["tracked_alerts"].append({
        "time": now_utc().isoformat(),
        "kind": "TRADE_PLAN",
        "side": plan.get("side"),
        "entry": plan.get("entry"),
        "risk": plan.get("risk"),
        "score": plan.get("confidence"),
        "source_kind": plan.get("source_kind"),
        "reports_sent": [],
        "closed": False,
    })

    # Nettoyage : on garde les 80 derniers suivis max.
    state["tracked_alerts"] = state["tracked_alerts"][-80:]


def format_trade_plan(plan):
    reasons_txt = "\n".join(plan["reasons"])

    news_txt = "ACTIVE — prudence news USD" if plan.get("news_guard") else "OK — pas de blocage news"

    return f"""<b>📍 GROK GOLD V5.1 — TRADE PLAN</b>

<b>Action :</b> {plan['action']} {SYMBOL}
<b>Type :</b> {plan['entry_type']}
<b>Source :</b> {plan['source_kind']}
<b>Confiance :</b> {plan['confidence']}/100
<b>Tier :</b> {plan.get('tier') or 'N/A'}

<b>Prix actuel :</b> {plan['price']}
<b>Entrée :</b> {plan['entry']}
<b>Stop Loss :</b> {plan['sl']}
<b>TP1 :</b> {plan['tp1']}
<b>TP2 :</b> {plan['tp2']}
<b>TP3 :</b> {plan['tp3']}

<b>Risque prix :</b> {plan['risk']} $
<b>Invalidation :</b> {plan['invalidation']}

<b>Session :</b> {plan.get('session')}
<b>News Guard :</b> {news_txt}

<b>Pourquoi :</b>
{reasons_txt}

<b>Règle :</b>
Si le prix ne respecte pas l’entrée ou casse l’invalidation, pas de trade.

<i>Signal d’analyse automatique. Pas une garantie de gain. À valider avec ton plan de trading.</i>"""


def main():
    print(f"GROK GOLD BOT V5.1 TRADE PLAN SIGNAL CARD - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")

    state = load_state()

    frames = fetch_frames()

    m15 = get_tf(frames, "M15")
    h1 = get_tf(frames, "H1")
    h4 = get_tf(frames, "H4")

    if m15 is None or h1 is None or h4 is None:
        raise RuntimeError("Données insuffisantes M15/H1/H4.")

    m15a = analyze(m15, "M15")
    h1a = analyze(h1, "H1")
    h4a = analyze(h4, "H4")
    macro = macro_filter()

    current_price = float(m15a["close"])

    update_tracking(state, current_price)

    try:
        evaluate_alerts_with_current_price(state, current_price)
    except Exception as e:
        print(f"[V5.1] Évaluation impossible: {e}")

    session = session_context()
    guard = news_guard_status()

    print(f"Session | {session['name']} | Risk={session['risk']}")
    print(f"NewsGuard | Active={guard.get('active')} | Source={guard.get('source')} | {guard.get('message')}")

    sig = score_signal(h4a, h1a, m15a, macro)

    imp = None
    sm = None
    formation = None
    m5 = None

    if sig["side"] != "HOLD":
        print(
            f"TRADE CANDIDAT | Prix: {sig['price']} | Action: {sig['side']} | "
            f"Score: {sig['score']} | Entry: {sig['entry']} | SL: {sig['sl']} | TP1: {sig['tp1']}"
        )
    else:
        print_hold(sig)

        m5 = fetch_m5_twelvedata()

        if m5 is not None:
            imp = impulse_engine(m5, "M5", m15a, h1a, h4a, macro)
        else:
            print("[M5] Fallback impulse engine sur M15.")
            imp = impulse_engine(m15a["df"], "M15", m15a, h1a, h4a, macro)

        print(
            f"Impulse | Alert={imp.get('alert')} | Side={imp.get('side')} | "
            f"Score={imp.get('score')} | Regime={imp.get('regime')} | "
            f"RangeATR={imp.get('range_ratio')} | Speed3={imp.get('speed_3')} | "
            f"ATRExpansion={imp.get('atr_expansion')}"
        )

        sm = smart_money_engine(m15a, h1a, h4a, macro)

        print(
            f"SmartMap | Alert={sm.get('alert')} | Side={sm.get('side')} | "
            f"Score={sm.get('score')} | Zone={sm.get('zone_name')} {sm.get('zone_level')} | "
            f"Confirm={sm.get('confirmation')} | Invalid={sm.get('invalidation')}"
        )

        if not imp.get("alert") and not sm.get("alert"):
            formation = detect_formation(sig)

            if formation:
                print(f"Formation détectée: {formation}")
            else:
                print("Aucune alerte assez propre.")

    event = build_event(sig, imp, sm, formation, current_price)

    # Si le moteur classique ne voit rien, on tente un Big Move Detector.
    if event.get("kind") == "NONE":
        big = detect_big_move(m5, m15a, h1a, h4a, macro, session, guard)

        if big:
            event = big
            print(
                f"BIG MOVE détecté | Side={big['side']} | Score={big['score']} | "
                f"Move3={round_price(big.get('move3'))} | Level={round_price(big.get('level'))}"
            )

    event = build_brain(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard)
    event = apply_guards(event, guard, session)

    plan = build_trade_plan(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard, current_price)

    sent = False
    skip_reason = ""

    if plan:
        ok, reason = plan_should_send(state, plan)

        if ok:
            print(
                f"TRADE PLAN ENVOYÉ | Action={plan['action']} | Entry={plan['entry']} | "
                f"SL={plan['sl']} | TP1={plan['tp1']} | Conf={plan['confidence']} | Reason={reason}"
            )
            sent = send_telegram(format_trade_plan(plan))

            if sent:
                register_plan(state, plan)
        else:
            skip_reason = reason
            print(f"TRADE PLAN BLOQUÉ | Reason={reason}")

    else:
        if event.get("filtered"):
            skip_reason = event.get("filter_reason", "Filtré V5.1")
            print(f"PAS DE PLAN | Signal filtré | {skip_reason}")
        elif event.get("kind") == "NONE":
            skip_reason = "aucun setup exploitable"
            print("PAS DE PLAN | Aucun setup exploitable")
        else:
            skip_reason = "confiance insuffisante ou conditions incomplètes"
            print(f"PAS DE PLAN | {skip_reason}")

    append_history({
        "time": now_utc().isoformat(),
        "symbol": SYMBOL,
        "kind": "TRADE_PLAN" if plan else event.get("kind", "NONE"),
        "source_kind": plan.get("source_kind", "") if plan else event.get("kind", ""),
        "side": plan.get("side", event.get("side", "NONE")) if plan else event.get("side", "NONE"),
        "action": plan.get("action", "") if plan else "",
        "entry": plan.get("entry", "") if plan else "",
        "sl": plan.get("sl", "") if plan else "",
        "tp1": plan.get("tp1", "") if plan else "",
        "tp2": plan.get("tp2", "") if plan else "",
        "tp3": plan.get("tp3", "") if plan else "",
        "confidence": plan.get("confidence", "") if plan else "",
        "price": current_price,
        "score": event.get("score", 0),
        "brain_score": event.get("brain_score", ""),
        "tier": event.get("tier", ""),
        "sent": sent,
        "skip_reason": skip_reason,
        "long_score": sig.get("long_score", ""),
        "short_score": sig.get("short_score", ""),
        "long_gate": sig.get("long_gate", ""),
        "short_gate": sig.get("short_gate", ""),
        "dxy": macro.get("dxy_bias", ""),
        "us10y": macro.get("tnx_bias", ""),
        "session": session.get("name", ""),
        "news_guard": guard.get("active", ""),
        "m15_bull_sweep": m15a.get("bull_sweep", ""),
        "m15_bear_sweep": m15a.get("bear_sweep", ""),
        "m15_bos_up": m15a.get("bos_up", ""),
        "m15_bos_down": m15a.get("bos_down", ""),
        "m15_top_reject": m15a.get("top_reject", ""),
        "m15_bottom_reject": m15a.get("bottom_reject", ""),
    })

    save_state(state)
    print("Mémoire V5.1 sauvegardée.")

    maybe_send_status_report()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[V5.1] ERREUR: {e}")
        send_error_report(e)
        raise
