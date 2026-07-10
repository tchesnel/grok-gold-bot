from datetime import datetime
from pathlib import Path
import json
import time
import traceback

import run_once_v41
import run_once_v43
import run_once_v51
import run_once_v62
import hybrid_free_data as hfd

from run_once_v50 import send_error_report
from hybrid_free_data import DataStaleError


def patch_data_sources():
    # On sauvegarde les vraies fonctions Twelve Data AVANT de les remplacer
    hfd.ORIGINAL_TWELVE_FETCH_FRAMES = run_once_v41.fetch_frames
    hfd.ORIGINAL_TWELVE_FETCH_M5 = run_once_v43.fetch_m5_twelvedata

    # Patch moteur principal
    run_once_v41.fetch_frames = hfd.fetch_hybrid_all
    run_once_v41.macro_filter = hfd.hybrid_macro_filter

    # Patch moteur utilisé par V5.1 / V6.x
    run_once_v51.fetch_frames = hfd.fetch_hybrid_all
    run_once_v51.macro_filter = hfd.hybrid_macro_filter
    run_once_v51.fetch_m5_twelvedata = hfd.fetch_hybrid_m5

    # Patch impulse M5
    run_once_v43.fetch_m5_twelvedata = hfd.fetch_hybrid_m5

    print("[V6.4] Data source patchée: TWELVE PRIMARY + FINNHUB FALLBACK")


def notify_stale_data_once(error):
    p = Path(".bot_state/stale_data_notify.json")
    p.parent.mkdir(parents=True, exist_ok=True)

    now = time.time()
    last = 0

    try:
        if p.exists():
            last = float(json.loads(p.read_text()).get("last", 0))
    except Exception:
        last = 0

    if now - last < 15 * 60:
        return

    msg = (
        "⚠️ GROK GOLD — DATA INDISPONIBLE\n\n"
        "Twelve/Finnhub/Yahoo ne donnent pas de donnée assez fraîche.\n\n"
        f"Détail : {error}\n\n"
        "Sécurité activée : aucun trade précis ne sera envoyé tant que les données ne sont pas fiables."
    )

    run_once_v41.send_telegram(msg)
    p.write_text(json.dumps({"last": now}))


def main():
    print(f"GROK GOLD BOT V6.4 TWELVE PRIMARY + FINNHUB FALLBACK - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        patch_data_sources()
        run_once_v62.main()

    except DataStaleError as e:
        print(f"[V6.4] DATA UNAVAILABLE: {e}")
        notify_stale_data_once(e)
        return

    except Exception as e:
        print("[V6.4] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
