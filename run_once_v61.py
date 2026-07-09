from datetime import datetime
import traceback

import run_once_v55
import run_once_v60

from run_once_v50 import send_error_report


LOTS = [0.01, 0.02, 0.03, 0.04, 0.05]


def fnum(x, default=0.0):
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def r2(x):
    return round(fnum(x), 2)


def recommend_lot(confidence, risk, session, news_guard):
    conf = fnum(confidence)
    risk = abs(fnum(risk, 8.0))

    if conf >= 82 and risk <= 6:
        lot = 0.05
    elif conf >= 75 and risk <= 8:
        lot = 0.04
    elif conf >= 68 and risk <= 10:
        lot = 0.03
    elif conf >= 58:
        lot = 0.02
    else:
        lot = 0.01

    # Réduction si conditions plus dangereuses
    if str(news_guard).lower() == "true":
        lot = max(0.01, lot - 0.02)

    if str(session).upper() in ("OFF HOURS", "ASIA", "N/A"):
        lot = max(0.01, lot - 0.01)

    # Réduction si stop large
    if risk >= 10:
        lot = max(0.01, lot - 0.01)

    # On force sur les tailles autorisées
    return min(LOTS, key=lambda x: abs(x - lot))


def order_type(action):
    if action == "BUY":
        return "BUY STOP"
    if action == "SELL":
        return "SELL STOP"
    return action


def format_pending_created_v61(p):
    action = p["action"]
    otype = order_type(action)
    lot = recommend_lot(p.get("confidence"), p.get("risk"), p.get("session"), p.get("news_guard"))

    emoji = "🟢" if action == "BUY" else "🔴"

    risk_note = "normal"
    if fnum(p.get("risk")) >= 10:
        risk_note = "stop large, lot réduit"
    if str(p.get("session")).upper() in ("OFF HOURS", "ASIA", "N/A"):
        risk_note = "horaire moins liquide, lot réduit"

    return f"""<b>📍 GROK GOLD V6.1 — ORDRE À PRÉPARER MAINTENANT</b>

<b>Action :</b> {emoji} {otype} XAUUSD
<b>Entrée :</b> {p['entry']}
<b>SL :</b> {p['sl']}
<b>TP1 :</b> {p['tp1']}
<b>TP2 :</b> {p['tp2']}
<b>TP3 :</b> {p['tp3']}

<b>Lot conseillé :</b> {lot}
<b>Confiance :</b> {p['confidence']}/100
<b>Risque :</b> {p['risk']} $
<b>Risque lecture :</b> {risk_note}

<b>Condition :</b>
{p['condition']}

<b>Contexte :</b>
DXY : {p['dxy']}
US10Y : {p['us10y']}
Session : {p['session']}
News Guard : {p['news_guard']}

<b>Lecture :</b>
Préparer l’ordre maintenant, avant que le mouvement parte.
Si le prix casse le niveau, l’ordre est prêt.
Si le prix ne casse pas, pas d’entrée.

<i>Ne pas entrer au marché si le prix est déjà trop éloigné. Le plan est basé sur l’entrée indiquée.</i>"""


def format_breakout_v61(p, price):
    action = p["action"]
    otype = order_type(action)
    lot = recommend_lot(p.get("confidence"), p.get("risk"), p.get("session"), p.get("news_guard"))

    emoji = "🟢" if action == "BUY" else "🔴"

    return f"""<b>🦅 GROK GOLD V6.1 — CASSURE DÉTECTÉE</b>

<b>Plan :</b> {emoji} {otype} XAUUSD
<b>Entrée prévue :</b> {p['entry']}
<b>Prix actuel :</b> {r2(price)}

<b>SL :</b> {p['sl']}
<b>TP1 :</b> {p['tp1']}
<b>TP2 :</b> {p['tp2']}
<b>TP3 :</b> {p['tp3']}

<b>Lot conseillé :</b> {lot}
<b>Confiance :</b> {p['confidence']}/100

<b>Lecture :</b>
Le niveau vient d’être cassé.
Si l’ordre était préparé, le plan est en activation.
Si tu n’es pas entré, éviter de courir après le prix : attendre retest vers {p['entry']}.

<i>Le bot surveille maintenant maintien/retest.</i>"""


def format_trade_activated_v61(p, price):
    action = p["action"]
    lot = recommend_lot(p.get("confidence"), p.get("risk"), p.get("session"), p.get("news_guard"))
    emoji = "🟢" if action == "BUY" else "🔴"

    return f"""<b>{emoji} {action} XAUUSD — PLAN ACTIVÉ</b>

<b>Entrée :</b> {p['entry']}
<b>Prix actuel :</b> {r2(price)}
<b>SL :</b> {p['sl']}
<b>TP1 :</b> {p['tp1']}
<b>TP2 :</b> {p['tp2']}
<b>TP3 :</b> {p['tp3']}

<b>Lot conseillé :</b> {lot}
<b>Confiance :</b> {p['confidence']}/100

<b>Pourquoi :</b>
Cassure du niveau surveillé + maintien du scénario.
DXY : {p['dxy']}
US10Y : {p['us10y']}
Session : {p['session']}

<b>Annulation :</b>
Plan invalide si le prix touche {p['sl']}.

<i>Plan automatique. Pas une garantie. Respecter SL et taille de lot.</i>"""


def main():
    print(f"GROK GOLD BOT V6.1 EARLY ORDER CARD + LOT MANAGER - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    run_once_v55.format_pending_created = format_pending_created_v61
    run_once_v55.format_breakout = format_breakout_v61
    run_once_v55.format_trade_activated = format_trade_activated_v61

    try:
        run_once_v60.main()
    except Exception as e:
        print("[V6.1] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
