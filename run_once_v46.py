from datetime import datetime

from run_once_v41 import SYMBOL, send_telegram, fetch_frames, get_tf, analyze
from run_once_v44 import load_state, save_state, now_utc, parse_time
from run_once_v45 import main as v45_main


MAX_EVAL_REPORTS_PER_RUN = 3


def get_current_price():
    frames = fetch_frames()
    m15 = get_tf(frames, "M15")

    if m15 is None:
        return None

    m15a = analyze(m15, "M15")
    return float(m15a["close"])


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

<i>Évaluation automatique V4.6. Cela sert à améliorer le scoring du bot avec le temps.</i>"""


def evaluate_alerts():
    state = load_state()
    alerts = state.get("tracked_alerts", [])

    if not alerts:
        print("[V4.6] Aucun signal à évaluer.")
        return

    price = get_current_price()

    if price is None:
        print("[V4.6] Prix actuel indisponible, évaluation ignorée.")
        return

    current = now_utc()
    reports_sent_this_run = 0

    for a in alerts:
        if reports_sent_this_run >= MAX_EVAL_REPORTS_PER_RUN:
            break

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
            print(f"[V4.6] Rapport envoyé: {label} | {a.get('kind')} {a.get('side')} | move={round(move, 2)}")
            ok = send_telegram(format_eval_message(a, price, move, age_min, label))

            if ok:
                reports.add(label)
                a["reports_sent"] = list(reports)
                reports_sent_this_run += 1

    save_state(state)
    print("[V4.6] Évaluation terminée.")


def main():
    print(f"GROK GOLD BOT V4.6 SIGNAL EVALUATOR - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        evaluate_alerts()
    except Exception as e:
        print(f"[V4.6] Évaluation impossible: {e}")

    v45_main()


if __name__ == "__main__":
    main()
