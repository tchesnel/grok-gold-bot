from datetime import datetime
import traceback

import run_once_v41
import run_once_v43
import run_once_v51
import run_once_v62

from run_once_v50 import send_error_report
from hybrid_free_data import fetch_hybrid_all, fetch_hybrid_m5, hybrid_macro_filter


def patch_data_sources():
    # Patch moteur principal
    run_once_v41.fetch_frames = fetch_hybrid_all
    run_once_v41.macro_filter = hybrid_macro_filter

    # Patch moteur utilisé par V5.1 / V6.x
    run_once_v51.fetch_frames = fetch_hybrid_all
    run_once_v51.macro_filter = hybrid_macro_filter
    run_once_v51.fetch_m5_twelvedata = fetch_hybrid_m5

    # Patch impulse M5
    run_once_v43.fetch_m5_twelvedata = fetch_hybrid_m5

    print("[V6.3] Data source patchée: HYBRID FREE ENGINE")


def main():
    print(f"GROK GOLD BOT V6.3 HYBRID FREE DATA ENGINE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        patch_data_sources()
        run_once_v62.main()
    except Exception as e:
        print("[V6.3] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
