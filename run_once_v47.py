from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import requests

from run_once_v41 import (
    SYMBOL,
    send_telegram,
    fetch_frames,
    get_tf,
    analyze,
    macro_filter,
    score_signal,
    format_trade,
    print_hold,
    cfg,
)

from run_once_v42 import detect_formation, format_formation
from run_once_v43 import fetch_m5_twelvedata, impulse_engine, format_impulse
from run_once_v44 import (
    load_state,
    save_state,
    should_send,
    register_sent_alert,
    append_history,
    update_tracking,
    now_utc,
)
from run_once_v45 import smart_money_engine, format_smart_map
from run_once_v46 import evaluate_alerts


NEWS_CACHE = Path(".bot_state/news_guard_cache.json")

NEWS_PRE_MIN = int(cfg("NEWS_PRE_MIN", "45"))
NEWS_POST_MIN = int(cfg("NEWS_POST_MIN", "30"))
NEWS_CACHE_HOURS = int(cfg("NEWS_CACHE_HOURS", "6"))
NEWS_GUARD_ENABLED = str(cfg("NEWS_GUARD_ENABLED", "True")).lower() == "true"
SESSION_GUARD_ENABLED = str(cfg("SESSION_GUARD_ENABLED", "True")).lower() == "true"

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

HIGH_KEYWORDS = [
    "CPI", "Core CPI", "PPI", "Core PPI", "NFP", "Non-Farm", "Nonfarm",
    "FOMC", "Federal Funds", "Fed Interest", "Powell", "Unemployment",
    "Average Hourly", "GDP", "Core PCE", "PCE Price", "ISM",
    "Retail Sales", "JOLTS", "ADP", "Consumer Confidence",
]


def session_context():
    now = datetime.now(timezone.utc)
    h = now.hour

    if 0 <= h < 7:
        return {
            "name": "ASIA",
            "risk": "range/pièges",
            "note": "Session Asie : priorité aux sweeps et fausses cassures. Trades validés plus prudents.",
        }

    if 7 <= h < 12:
        return {
            "name": "LONDON",
            "risk": "cassures/liquidité",
            "note": "Session Londres : bonne zone pour cassure du range asiatique et retests.",
        }

    if 12 <= h < 16:
        return {
            "name": "NEW YORK OPEN",
            "risk": "volatilité/news",
            "note": "Ouverture New York : gros mouvements possibles, attention news USD.",
        }

    if 16 <= h < 21:
        return {
            "name": "NEW YORK",
            "risk": "tendance/continuation",
            "note": "Session New York : continuation ou retournement après news.",
        }

    return {
        "name": "OFF HOURS",
        "risk": "liquidité faible",
        "note": "Hors grosses sessions : prudence, faux mouvements possibles.",
    }


def parse_ff_datetime(date_s, time_s):
    if not date_s or not time_s:
        return None

    time_s = str(time_s).strip()

    if "day" in time_s.lower() or time_s.lower() in ("", "tentative"):
        return None

    try:
        raw = f"{date_s} {time_s.upper().replace(' ', '')}"
        dt_ny = datetime.strptime(raw, "%m-%d-%Y %I:%M%p").replace(tzinfo=ZoneInfo("America/New_York"))
        return dt_ny.astimezone(timezone.utc)
    except Exception:
        return None


def is_relevant_news(event):
    country = str(event.get("country", "")).upper()
    impact = str(event.get("impact", "")).lower()
    title = str(event.get("title", ""))

    if country != "USD":
        return False

    if "high" not in impact and "red" not in impact:
        return False

    if any(k.lower() in title.lower() for k in HIGH_KEYWORDS):
        return True

    return True


def load_cached_news():
    if not NEWS_CACHE.exists():
        return None

    try:
        data = json.loads(NEWS_CACHE.read_text())
        cached_at = datetime.fromisoformat(data.get("cached_at", ""))
        age = datetime.now(timezone.utc) - cached_at

        if age.total_seconds() <= NEWS_CACHE_HOURS * 3600:
            return data.get("events", [])
    except Exception:
        return None

    return None


def save_cached_news(events):
    NEWS_CACHE.parent.mkdir(exist_ok=True)

    data = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }

    NEWS_CACHE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def fetch_news_events():
    cached = load_cached_news()

    if cached is not None:
        return cached, "cache"

    try:
        r = requests.get(FF_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        js = r.json()

        events = []

        for e in js:
            if not is_relevant_news(e):
                continue

            dt = parse_ff_datetime(e.get("date"), e.get("time"))

            if dt is None:
                continue

            events.append({
                "title": e.get("title", "USD High Impact News"),
                "country": e.get("country", "USD"),
                "impact": e.get("impact", "High"),
                "utc": dt.isoformat(),
            })

        events = sorted(events, key=lambda x: x["utc"])
        save_cached_news(events)
        return events, "live"

    except Exception as ex:
        print(f"[NEWS] Impossible de charger le calendrier: {ex}")

        if NEWS_CACHE.exists():
            try:
                data = json.loads(NEWS_CACHE.read_text())
                return data.get("events", []), "old-cache"
            except Exception:
                pass

    return [], "none"


def news_guard_status():
    if not NEWS_GUARD_ENABLED:
        return {
            "active": False,
            "source": "disabled",
            "event": None,
            "message": "News guard désactivé.",
        }

    events, source = fetch_news_events()
    current = datetime.now(timezone.utc)

    nearest_upcoming = None

    for e in events:
        try:
            t = datetime.fromisoformat(e["utc"])
        except Exception:
            continue

        start = t - timedelta(minutes=NEWS_PRE_MIN)
        end = t + timedelta(minutes=NEWS_POST_MIN)

        if start <= current <= end:
            minutes = int((t - current).total_seconds() / 60)

            return {
                "active": True,
                "source": source,
                "event": e,
                "minutes_to_event": minutes,
                "message": f"News USD active: {e['title']}",
            }

        if t > current and nearest_upcoming is None:
            nearest_upcoming = e

    return {
        "active": False,
        "source": source,
        "event": nearest_upcoming,
        "message": "Pas de news USD bloquante maintenant.",
    }


def format_news_guard_message(original_event, guard, session):
    side = original_event.get("side", "NONE")
    kind = original_event.get("kind", "ALERT")
    score = original_event.get("score", 0)
    price = original_event.get("price", 0)

    e = guard.get("event") or {}
    title = e.get("title", "News USD importante")
    utc_time = e.get("utc", "heure inconnue")

    return f"""<b>🛡️ GROK GOLD V4.7 — NEWS GUARD ACTIF</b>

<b>Actif :</b> {SYMBOL}
<b>Prix :</b> {price}
<b>Signal détecté :</b> {kind} {side}
<b>Score :</b> {score}/100

<b>Décision :</b>
Le bot bloque le signal fort et passe en mode observation.

<b>Raison :</b>
News USD proche ou active : {title}
Heure UTC : {utc_time}

<b>Session :</b>
{session['name']} — {session['risk']}

<i>Pas d’entrée validée pendant la zone news. Attendre stabilisation/retest après la volatilité.</i>"""


def format_session_guard_message(original_event, session):
    side = original_event.get("side", "NONE")
    kind = original_event.get("kind", "ALERT")
    score = original_event.get("score", 0)
    price = original_event.get("price", 0)

    return f"""<b>🕒 GROK GOLD V4.7 — SESSION GUARD</b>

<b>Actif :</b> {SYMBOL}
<b>Prix :</b> {price}
<b>Signal détecté :</b> {kind} {side}
<b>Score :</b> {score}/100

<b>Session :</b>
{session['name']} — {session['risk']}

<b>Décision :</b>
Le bot garde le scénario en observation, mais ne le transforme pas en trade agressif.

<b>Note :</b>
{session['note']}

<i>Attendre confirmation claire, retest ou ouverture de session plus liquide.</i>"""


def apply_guards(event, guard, session):
    if event["kind"] == "NONE":
        return event

    # News : on bloque TRADE et IMPULSE. Formation reste possible, mais avec warning.
    if guard.get("active"):
        if event["kind"] in ("TRADE", "IMPULSE"):
            return {
                "kind": "FORMATION",
                "side": event.get("side", "NONE"),
                "score": event.get("score", 0),
                "price": event.get("price", 0),
                "level": event.get("level", event.get("entry", event.get("price", 0))),
                "message": format_news_guard_message(event, guard, session),
            }

        if event["kind"] == "FORMATION":
            event["message"] += "\n\n<b>🛡️ News Guard :</b> prudence, news USD proche ou active. Pas d’entrée agressive."
            return event

    if not SESSION_GUARD_ENABLED:
        return event

    # Session Asie : si trade pas exceptionnel, on le transforme en scénario.
    if session["name"] == "ASIA" and event["kind"] == "TRADE" and float(event.get("score", 0)) < 86:
        return {
            "kind": "FORMATION",
            "side": event.get("side", "NONE"),
            "score": event.get("score", 0),
            "price": event.get("price", 0),
            "level": event.get("entry", event.get("price", 0)),
            "message": format_session_guard_message(event, session),
        }

    # Hors session : on évite les impulsions moyennes.
    if session["name"] == "OFF HOURS" and event["kind"] == "IMPULSE" and float(event.get("score", 0)) < 82:
        return {
            "kind": "FORMATION",
            "side": event.get("side", "NONE"),
            "score": event.get("score", 0),
            "price": event.get("price", 0),
            "level": event.get("level", event.get("price", 0)),
            "message": format_session_guard_message(event, session),
        }

    return event


def build_event(sig, imp, sm, formation, current_price):
    if sig["side"] != "HOLD":
        return {
            "kind": "TRADE",
            "side": sig["side"],
            "score": sig.get("score", 0),
            "price": sig.get("price", current_price),
            "entry": sig.get("entry", sig.get("price", current_price)),
            "sl": sig.get("sl", 0),
            "risk": sig.get("risk", 6.0),
            "message": format_trade(sig),
        }

    if imp and imp.get("alert"):
        side = imp.get("side", "NONE")
        level = imp.get("prev_20_high") if side == "LONG" else imp.get("prev_20_low")

        return {
            "kind": "IMPULSE",
            "side": side,
            "score": imp.get("score", 0),
            "price": imp.get("price", current_price),
            "tf": imp.get("tf", ""),
            "level": level or imp.get("price", current_price),
            "message": format_impulse(imp),
        }

    if sm and sm.get("alert"):
        return {
            "kind": "FORMATION",
            "side": sm["side"],
            "score": sm["score"],
            "price": sm["price"],
            "level": sm["confirmation"],
            "message": format_smart_map(sm),
        }

    if formation:
        m15 = sig["m15"]
        level = m15["prev_12_low"] if formation == "SHORT" else m15["prev_12_high"]
        score = sig["short_score"] if formation == "SHORT" else sig["long_score"]

        return {
            "kind": "FORMATION",
            "side": formation,
            "score": score,
            "price": sig.get("price", current_price),
            "level": level,
            "message": format_formation(formation, sig),
        }

    return {
        "kind": "NONE",
        "side": "NONE",
        "score": 0,
        "price": current_price,
        "message": "",
    }


def main():
    print(f"GROK GOLD BOT V4.7 NEWS GUARD + SESSIONS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")

    try:
        evaluate_alerts()
    except Exception as e:
        print(f"[V4.7] Évaluation V4.6 impossible: {e}")

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
    event = apply_guards(event, guard, session)

    sent = False
    skip_reason = ""

    if event["kind"] != "NONE":
        ok, reason = should_send(state, event)

        if ok:
            print(f"ALERTE ENVOYÉE | Type={event['kind']} | Side={event['side']} | Reason={reason}")
            sent = send_telegram(event["message"])

            if sent:
                register_sent_alert(state, event)
        else:
            skip_reason = reason
            print(f"ALERTE BLOQUÉE ANTI-SPAM | Type={event['kind']} | Side={event['side']} | Reason={reason}")

    append_history({
        "time": now_utc().isoformat(),
        "symbol": SYMBOL,
        "kind": event["kind"],
        "side": event["side"],
        "price": current_price,
        "score": event.get("score", 0),
        "sent": sent,
        "skip_reason": skip_reason,
        "long_score": sig.get("long_score", ""),
        "short_score": sig.get("short_score", ""),
        "long_gate": sig.get("long_gate", ""),
        "short_gate": sig.get("short_gate", ""),
        "dxy": macro.get("dxy_bias", ""),
        "us10y": macro.get("tnx_bias", ""),
        "m15_bull_sweep": m15a.get("bull_sweep", ""),
        "m15_bear_sweep": m15a.get("bear_sweep", ""),
        "m15_bos_up": m15a.get("bos_up", ""),
        "m15_bos_down": m15a.get("bos_down", ""),
        "m15_top_reject": m15a.get("top_reject", ""),
        "m15_bottom_reject": m15a.get("bottom_reject", ""),
    })

    save_state(state)

    print("Mémoire V4.7 sauvegardée.")


if __name__ == "__main__":
    main()
