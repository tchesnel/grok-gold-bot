from datetime import datetime

from run_once_v41 import (
    SYMBOL,
    MIN_SCORE,
    send_telegram,
    fetch_frames,
    get_tf,
    analyze,
    macro_filter,
    score_signal,
    format_trade,
    print_hold,
)


def format_formation(side, sig):
    m15 = sig["m15"]
    macro = sig["macro"]

    if side == "SHORT":
        emoji = "🟡"
        score = sig["short_score"]
        confirmation = round(m15["prev_12_low"], 2)
        invalidation = round(m15["prev_30_high"], 2)
        reasons = sig.get("short_reasons", [])
        title = "SHORT EN FORMATION"
        trigger = f"Attendre cassure claire sous {confirmation}"
    else:
        emoji = "🟡"
        score = sig["long_score"]
        confirmation = round(m15["prev_12_high"], 2)
        invalidation = round(m15["prev_30_low"], 2)
        reasons = sig.get("long_reasons", [])
        title = "LONG EN FORMATION"
        trigger = f"Attendre cassure claire au-dessus de {confirmation}"

    reasons_txt = "\n".join([f"• {r}" for r in reasons[:8]]) or "• Setup en construction"

    return f"""<b>{emoji} GROK GOLD V4.2 — {title}</b>

<b>Actif :</b> {SYMBOL}
<b>Prix actuel :</b> {sig['price']}
<b>Score formation :</b> {score}/100

<b>Ce n’est PAS encore un trade validé.</b>

<b>Confirmation attendue :</b>
{trigger}

<b>Invalidation :</b>
{invalidation}

<b>Macro :</b>
DXY : {macro.get('dxy_bias')}
US10Y : {macro.get('tnx_bias')}

<b>Raisons :</b>
{reasons_txt}

<i>Le bot surveille une opportunité. Attendre confirmation avant entrée.</i>"""


def detect_formation(sig):
    if sig["side"] != "HOLD":
        return None

    m15 = sig.get("m15", {})
    h1 = sig.get("h1", {})
    macro = sig.get("macro", {})

    long_score = sig.get("long_score", 0)
    short_score = sig.get("short_score", 0)

    long_building = (
        long_score >= 28
        and (
            m15.get("bull_sweep")
            or h1.get("bull_sweep")
            or m15.get("bottom_reject")
            or m15.get("ema_reclaim_up")
            or macro.get("dxy_bias") == "DOWN"
        )
    )

    short_building = (
        short_score >= 28
        and (
            m15.get("bear_sweep")
            or h1.get("bear_sweep")
            or m15.get("top_reject")
            or m15.get("ema_reclaim_down")
            or macro.get("dxy_bias") == "UP"
        )
    )

    if short_building and short_score >= long_score:
        return "SHORT"

    if long_building and long_score > short_score:
        return "LONG"

    return None


def main():
    print(f"GROK GOLD BOT V4.2 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")
    print(f"Min score trade validé: {MIN_SCORE}")

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

    sig = score_signal(h4a, h1a, m15a, macro)

    if sig["side"] != "HOLD":
        print(
            f"Prix: {sig['price']} | Action: {sig['side']} | Score: {sig['score']} | "
            f"Entry: {sig['entry']} | SL: {sig['sl']} | TP1: {sig['tp1']}"
        )
        send_telegram(format_trade(sig))
        return

    print_hold(sig)

    formation = detect_formation(sig)

    if formation:
        print(f"Formation détectée: {formation}")
        send_telegram(format_formation(formation, sig))
    else:
        print("Aucune formation assez propre pour alerte Telegram.")


if __name__ == "__main__":
    main()
