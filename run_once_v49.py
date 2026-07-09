from datetime import datetime

from run_once_v41 import (
    SYMBOL,
    send_telegram,
    fetch_frames,
    get_tf,
    analyze,
    macro_filter,
    score_signal,
    print_hold,
)

from run_once_v42 import detect_formation
from run_once_v43 import fetch_m5_twelvedata, impulse_engine
from run_once_v44 import (
    load_state,
    save_state,
    should_send,
    register_sent_alert,
    append_history,
    update_tracking,
    now_utc,
    parse_time,
)

from run_once_v45 import smart_money_engine
from run_once_v47 import session_context, news_guard_status, build_event, apply_guards
from run_once_v48 import build_brain


MAX_EVAL_REPORTS_PER_RUN = 2


def side_move(side, entry, price):
    if side == "LONG":
        return price - entry
    if side == "SHORT":
        return entry - price
    return 0.0


def format_eval_message(alert, price, move, age_min, label):
    side = alert.get("side", "NONE")
    kind = alert.get("kind", "ALERT")
    entry = float(alert.get("entry", alert.get("price_start", price)))
    score = alert.get("score", 0)
    max_fav = float(alert.get("max_favorable", 0))
    max_adv = float(alert.get("max_adverse", 0))

    if label == "HIT_1R":
        title = "✅ MOUVEMENT FAVORABLE"
        txt = "Le prix part dans le sens du scénario."
    elif label == "HIT_2R":
        title = "🚀 GROS MOUVEMENT FAVORABLE"
        txt = "Le scénario donne un très bon déplacement."
    elif label == "BAD_MOVE":
        title = "❌ SCÉNARIO INVALIDÉ"
        txt = "Le prix est parti contre le scénario."
    elif label == "FINAL_GOOD":
        title = "🏆 BILAN 4H : GOOD MOVE"
        txt = "Le scénario a donné un bon mouvement dans les 4 heures."
    elif label == "FINAL_BAD":
        title = "⚠️ BILAN 4H : BAD MOVE"
        txt = "Le scénario n’a pas bien réagi dans les 4 heures."
    else:
        title = "➖ BILAN 4H : NEUTRAL"
        txt = "Le scénario n’a pas donné assez de mouvement clair."

    return f"""<b>{title}</b>

<b>Actif :</b> {SYMBOL}
<b>Type alerte :</b> {kind}
<b>Sens :</b> {side}
<b>Score initial :</b> {score}/100

<b>Entrée scénario :</b> {round(entry, 2)}
<b>Prix actuel :</b> {round(price, 2)}
<b>Âge alerte :</b> {int(age_min)} min

<b>Mouvement actuel :</b> {round(move, 2)} $
<b>Max favorable :</b> {round(max_fav, 2)} $
<b>Max adverse :</b> {round(max_adv, 2)} $

<b>Lecture :</b>
{txt}

<i>Évaluation automatique V4.9 sans appel API supplémentaire.</i>"""


def evaluate_alerts_with_current_price(state, price):
    alerts = state.get("tracked_alerts", [])

    if not alerts:
        print("[V4.9] Aucun signal à évaluer.")
        return

    current = now_utc()
    reports_sent = 0

    for a in alerts:
        if reports_sent >= MAX_EVAL_REPORTS_PER_RUN:
            break

        if a.get("closed"):
            continue

        if a.get("side") not in ("LONG", "SHORT"):
            continue

        t = parse_time(a.get("time", ""))

        if t is None:
            continue

        age_min = (current - t).total_seconds() / 60

        if age_min < 10:
            continue

        entry = float(a.get("entry", a.get("price_start", price)))
        risk = abs(float(a.get("risk", 6.0)))
        risk = max(risk, 5.0)

        move = side_move(a.get("side"), entry, price)

        a["max_favorable"] = max(float(a.get("max_favorable", 0)), move)
        a["max_adverse"] = min(float(a.get("max_adverse", 0)), move)

        reports = set(a.get("reports_sent", []))
        label = None

        if move >= 2.0 * risk and "HIT_2R" not in reports:
            label = "HIT_2R"
        elif move >= 1.0 * risk and "HIT_1R" not in reports:
            label = "HIT_1R"
        elif move <= -1.0 * risk and "BAD_MOVE" not in reports:
            label = "BAD_MOVE"
        elif age_min >= 240 and "FINAL" not in reports:
            if a["max_favorable"] >= 1.0 * risk:
                label = "FINAL_GOOD"
                a["result"] = "GOOD_MOVE"
            elif a["max_adverse"] <= -1.0 * risk:
                label = "FINAL_BAD"
                a["result"] = "BAD_MOVE"
            else:
                label = "FINAL_NEUTRAL"
                a["result"] = "NEUTRAL"

            a["closed"] = True
            reports.add("FINAL")

        if label:
            print(f"[V4.9] Rapport envoyé: {label} | {a.get('kind')} {a.get('side')} | move={round(move, 2)}")
            ok = send_telegram(format_eval_message(a, price, move, age_min, label))

            if ok:
                reports.add(label)
                a["reports_sent"] = list(reports)
                reports_sent += 1

    print("[V4.9] Évaluation terminée sans appel API supplémentaire.")


def main():
    print(f"GROK GOLD BOT V4.9 DATA SAVER BRAIN - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
        print(f"[V4.9] Évaluation impossible: {e}")

    session = session_context()
    guard = news_guard_status()

    print(f"Session | {session['name']} | Risk={session['risk']}")
    print(f"NewsGuard | Active={guard.get('active')} | Source={guard.get('source')} | {guard.get('message')}")

    sig = score_signal(h4a, h1a, m15a, macro)

    imp = None
    sm = None
    formation = None

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
    event = build_brain(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard)
    event = apply_guards(event, guard, session)

    sent = False
    skip_reason = ""

    if event.get("filtered"):
        skip_reason = event.get("filter_reason", "Filtré V4.9")
        print(f"ALERTE FILTRÉE V4.9 | Type={event.get('kind')} | Side={event.get('side')} | Reason={skip_reason}")

    elif event["kind"] != "NONE":
        ok, reason = should_send(state, event)

        if ok:
            print(
                f"ALERTE ENVOYÉE | Type={event['kind']} | Side={event['side']} | "
                f"Tier={event.get('tier')} | BrainScore={event.get('brain_score')} | Reason={reason}"
            )
            sent = send_telegram(event["message"])

            if sent:
                register_sent_alert(state, event)
        else:
            skip_reason = reason
            print(f"ALERTE BLOQUÉE ANTI-SPAM | Type={event['kind']} | Side={event['side']} | Reason={reason}")

    append_history({
        "time": now_utc().isoformat(),
        "symbol": SYMBOL,
        "kind": event.get("kind", "NONE"),
        "side": event.get("side", "NONE"),
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

    print("Mémoire V4.9 sauvegardée.")


if __name__ == "__main__":
    main()
