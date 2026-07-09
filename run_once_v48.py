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
)

from run_once_v45 import smart_money_engine
from run_once_v46 import evaluate_alerts
from run_once_v47 import session_context, news_guard_status, build_event, apply_guards


def macro_is_good(side, macro):
    dxy = str(macro.get("dxy_bias", "")).upper()
    us10y = str(macro.get("tnx_bias", "")).upper()

    good = []
    bad = []

    if side == "LONG":
        if dxy == "DOWN":
            good.append("DXY baissier, favorable à l’or")
        elif dxy == "UP":
            bad.append("DXY haussier contre le LONG")

        if us10y == "DOWN":
            good.append("US10Y baissier, favorable à l’or")
        elif us10y == "UP":
            bad.append("US10Y haussier contre le LONG")

    if side == "SHORT":
        if dxy == "UP":
            good.append("DXY haussier, pression baissière sur l’or")
        elif dxy == "DOWN":
            bad.append("DXY baissier contre le SHORT")

        if us10y == "UP":
            good.append("US10Y haussier, pression baissière sur l’or")
        elif us10y == "DOWN":
            bad.append("US10Y baissier contre le SHORT")

    return good, bad


def structure_confluence(side, m15a, h1a, h4a):
    good = []
    bad = []
    score = 0

    if side == "LONG":
        if h4a.get("bos_up"):
            score += 10
            good.append("H4 cassure haussière")
        if h1a.get("bos_up"):
            score += 12
            good.append("H1 cassure haussière")
        if m15a.get("bos_up"):
            score += 10
            good.append("M15 cassure haussière")
        if m15a.get("ema_up"):
            score += 8
            good.append("M15 repasse au-dessus EMA")
        if m15a.get("bull_sweep"):
            score += 12
            good.append("Sweep bas puis reprise acheteuse")
        if m15a.get("bottom_reject"):
            score += 8
            good.append("Mèche basse de rejet")

        if h1a.get("bos_down"):
            score -= 10
            bad.append("H1 structure baissière contre le LONG")
        if m15a.get("top_reject"):
            score -= 8
            bad.append("Rejet haut M15 contre le LONG")
        if m15a.get("bear_sweep"):
            score -= 8
            bad.append("Sweep haut contre le LONG")

    if side == "SHORT":
        if h4a.get("bos_down"):
            score += 10
            good.append("H4 cassure baissière")
        if h1a.get("bos_down"):
            score += 12
            good.append("H1 cassure baissière")
        if m15a.get("bos_down"):
            score += 10
            good.append("M15 cassure baissière")
        if m15a.get("ema_down"):
            score += 8
            good.append("M15 repasse sous EMA")
        if m15a.get("bear_sweep"):
            score += 12
            good.append("Sweep haut puis rejet vendeur")
        if m15a.get("top_reject"):
            score += 8
            good.append("Mèche haute de rejet")

        if h1a.get("bos_up"):
            score -= 10
            bad.append("H1 structure haussière contre le SHORT")
        if m15a.get("bottom_reject"):
            score -= 8
            bad.append("Rejet bas M15 contre le SHORT")
        if m15a.get("bull_sweep"):
            score -= 8
            bad.append("Sweep bas contre le SHORT")

    return score, good, bad


def session_confluence(session):
    name = session.get("name", "")
    good = []
    bad = []
    score = 0

    if name in ("LONDON", "NEW YORK OPEN", "NEW YORK"):
        score += 8
        good.append(f"Session active : {name}")

    if name == "ASIA":
        score -= 5
        bad.append("Session Asie : plus de ranges et faux signaux")

    if name == "OFF HOURS":
        score -= 8
        bad.append("Hors grosses sessions : liquidité plus faible")

    return score, good, bad


def impulse_confluence(event, imp):
    good = []
    bad = []
    score = 0

    if not imp:
        return score, good, bad

    regime = str(imp.get("regime", "")).upper()
    imp_score = float(imp.get("score", 0))

    if imp_score >= 75:
        score += 10
        good.append("Impulsion forte détectée")

    if "CHOP" in regime or "RANGE" in regime:
        score -= 6
        bad.append("Marché en range/chop : risque de faux départ")

    if "TREND" in regime or "DIRECTION" in regime:
        score += 6
        good.append("Régime plus directionnel")

    return score, good, bad


def smart_map_confluence(event, sm):
    good = []
    bad = []
    score = 0

    if not sm:
        return score, good, bad

    if sm.get("alert"):
        score += 12
        good.append("Smart Money Map actif")

    if sm.get("zone_name"):
        good.append(f"Zone clé : {sm.get('zone_name')} {sm.get('zone_level')}")

    return score, good, bad


def news_confluence(guard):
    good = []
    bad = []
    score = 0

    if guard.get("active"):
        score -= 25
        bad.append("News USD proche/active : risque de mèche violent")
    else:
        score += 5
        good.append("Pas de news USD bloquante maintenant")

    return score, good, bad


def tier_from_score(score):
    if score >= 86:
        return "S TIER", "setup exceptionnel"
    if score >= 74:
        return "A TIER", "très bon setup"
    if score >= 60:
        return "B TIER", "scénario intéressant"
    return "C TIER", "bruit / prudence"


def build_brain(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard):
    if event["kind"] == "NONE":
        return event

    side = event.get("side", "NONE")

    base = float(event.get("score", 0))
    brain_score = min(base, 70)

    good_reasons = []
    bad_reasons = []

    s_score, s_good, s_bad = structure_confluence(side, m15a, h1a, h4a)
    brain_score += s_score
    good_reasons += s_good
    bad_reasons += s_bad

    m_good, m_bad = macro_is_good(side, macro)
    brain_score += len(m_good) * 8
    brain_score -= len(m_bad) * 8
    good_reasons += m_good
    bad_reasons += m_bad

    sess_score, sess_good, sess_bad = session_confluence(session)
    brain_score += sess_score
    good_reasons += sess_good
    bad_reasons += sess_bad

    imp_score, imp_good, imp_bad = impulse_confluence(event, imp)
    brain_score += imp_score
    good_reasons += imp_good
    bad_reasons += imp_bad

    sm_score, sm_good, sm_bad = smart_map_confluence(event, sm)
    brain_score += sm_score
    good_reasons += sm_good
    bad_reasons += sm_bad

    news_score, news_good, news_bad = news_confluence(guard)
    brain_score += news_score
    good_reasons += news_good
    bad_reasons += news_bad

    brain_score = max(0, min(100, round(brain_score, 1)))
    tier, tier_text = tier_from_score(brain_score)

    event["brain_score"] = brain_score
    event["tier"] = tier
    event["tier_text"] = tier_text
    event["good_reasons"] = good_reasons[:8]
    event["bad_reasons"] = bad_reasons[:6]

    good_txt = "\n".join([f"✅ {r}" for r in event["good_reasons"]]) or "Aucune confluence majeure."
    bad_txt = "\n".join([f"⚠️ {r}" for r in event["bad_reasons"]]) or "Aucun gros danger détecté."

    header = f"""<b>🧠 GROK GOLD V4.8 — CONFLUENCE BRAIN</b>

<b>Actif :</b> {SYMBOL}
<b>Signal :</b> {event.get('kind')} {side}
<b>Prix :</b> {round(float(event.get('price', 0)), 2)}

<b>Classement setup :</b> {tier}
<b>Qualité :</b> {tier_text}
<b>Brain score :</b> {brain_score}/100

<b>Confluences positives :</b>
{good_txt}

<b>Risques / contradictions :</b>
{bad_txt}
"""

    event["message"] = header + "\n" + event.get("message", "")

    # Filtre qualité : on évite d'envoyer les signaux faibles.
    if tier == "C TIER":
        if event["kind"] in ("FORMATION", "IMPULSE"):
            event["filtered"] = True
            event["filter_reason"] = "C TIER filtré : pas assez de confluence"
        elif event["kind"] == "TRADE":
            event["kind"] = "FORMATION"
            event["message"] += "\n\n<b>Décision V4.8 :</b> Trade déclassé en observation car confluence trop faible."

    return event


def main():
    print(f"GROK GOLD BOT V4.8 CONFLUENCE BRAIN - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")

    try:
        evaluate_alerts()
    except Exception as e:
        print(f"[V4.8] Évaluation V4.6 impossible: {e}")

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
        skip_reason = event.get("filter_reason", "Filtré V4.8")
        print(f"ALERTE FILTRÉE V4.8 | Type={event.get('kind')} | Side={event.get('side')} | Reason={skip_reason}")

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

    print("Mémoire V4.8 sauvegardée.")


if __name__ == "__main__":
    main()
