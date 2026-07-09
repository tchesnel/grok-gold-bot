import pandas as pd
from pathlib import Path

LOG_FILE = Path("analysis_log.csv")

if not LOG_FILE.exists():
    print("❌ Aucun fichier analysis_log.csv trouvé.")
    print("Laisse tourner le bot avant d'analyser.")
    raise SystemExit

df = pd.read_csv(LOG_FILE)

if len(df) < 30:
    print(f"⚠️ Pas assez de données : {len(df)} lignes seulement.")
    print("Laisse tourner le bot plus longtemps.")
    raise SystemExit

df["price"] = pd.to_numeric(df["price"], errors="coerce")
df["global_score"] = pd.to_numeric(df["global_score"], errors="coerce")
df.dropna(subset=["price", "global_score"], inplace=True)

# Le bot analyse toutes les 5 minutes.
# 12 lignes = environ 1h
# 48 lignes = environ 4h
df["future_1h_price"] = df["price"].shift(-12)
df["future_4h_price"] = df["price"].shift(-48)

df["future_1h_return_pct"] = (df["future_1h_price"] / df["price"] - 1) * 100
df["future_4h_return_pct"] = (df["future_4h_price"] / df["price"] - 1) * 100

long_zones = df[df["global_score"] >= 68].copy()
short_zones = df[df["global_score"] <= 32].copy()
hold_zones = df[(df["global_score"] > 40) & (df["global_score"] < 60)].copy()

def summarize(name, data):
    print("\n" + "=" * 60)
    print(name)
    print("=" * 60)

    if len(data) == 0:
        print("Aucun cas.")
        return

    print("Nombre de cas :", len(data))
    print("Score moyen :", round(data["global_score"].mean(), 2))

    print("\nPerformance future 1h :")
    print("Moyenne :", round(data["future_1h_return_pct"].mean(), 3), "%")
    print("Médiane :", round(data["future_1h_return_pct"].median(), 3), "%")

    print("\nPerformance future 4h :")
    print("Moyenne :", round(data["future_4h_return_pct"].mean(), 3), "%")
    print("Médiane :", round(data["future_4h_return_pct"].median(), 3), "%")

    if name.startswith("LONG"):
        win_1h = (data["future_1h_return_pct"] > 0).mean() * 100
        win_4h = (data["future_4h_return_pct"] > 0).mean() * 100
    elif name.startswith("SHORT"):
        win_1h = (data["future_1h_return_pct"] < 0).mean() * 100
        win_4h = (data["future_4h_return_pct"] < 0).mean() * 100
    else:
        win_1h = None
        win_4h = None

    if win_1h is not None:
        print("\nWinrate direction 1h :", round(win_1h, 1), "%")
        print("Winrate direction 4h :", round(win_4h, 1), "%")

print("\n📊 ANALYSE DU BOT")
print("Lignes totales :", len(df))
print("Premier log :", df.iloc[0]["time"])
print("Dernier log :", df.iloc[-1]["time"])

summarize("LONG ZONE score >= 68", long_zones)
summarize("SHORT ZONE score <= 32", short_zones)
summarize("HOLD / NEUTRE score 40-60", hold_zones)

print("\n✅ Analyse terminée.")
