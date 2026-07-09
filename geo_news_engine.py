"""
Geo News Engine V4 - Fail-safe
Analyse GDELT pour détecter un biais géopolitique / risk-off.

Score positif = soutien potentiel à l'or
Score négatif = pression potentielle sur l'or

Si GDELT bloque ou répond lentement, le bot continue avec une géopolitique neutre.
"""

import requests
from config import settings


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = (3, 8)


RISK_OFF_KEYWORDS = [
    "war", "missile", "attack", "iran", "israel", "ukraine", "russia",
    "taiwan", "china", "red sea", "houthi", "sanction", "nuclear",
    "terror", "conflict", "escalation", "strike", "invasion",
    "geopolitical", "safe haven", "crisis", "military", "airstrike"
]

RISK_ON_KEYWORDS = [
    "ceasefire", "peace talks", "de-escalation", "agreement",
    "truce", "risk appetite", "markets rally", "peace deal"
]

FED_HAWKISH_KEYWORDS = [
    "hawkish fed", "rate hike", "higher for longer", "strong dollar",
    "hot inflation", "yields rise", "bond yields rise"
]

FED_DOVISH_KEYWORDS = [
    "rate cut", "dovish fed", "cooling inflation", "yields fall",
    "weaker dollar", "bond yields fall"
]


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_text(text: str) -> float:
    t = text.lower()
    score = 0.0

    for kw in RISK_OFF_KEYWORDS:
        if kw in t:
            score += 0.12

    for kw in RISK_ON_KEYWORDS:
        if kw in t:
            score -= 0.12

    for kw in FED_HAWKISH_KEYWORDS:
        if kw in t:
            score -= 0.10

    for kw in FED_DOVISH_KEYWORDS:
        if kw in t:
            score += 0.10

    return score


def fetch_gdelt_articles() -> list:
    query = (
        '(gold OR XAUUSD OR "safe haven" OR Iran OR Israel OR Ukraine OR Russia '
        'OR Taiwan OR China OR "Red Sea" OR Houthi OR sanction OR missile '
        'OR Fed OR inflation OR dollar OR yields)'
    )

    url = "https://api.gdeltproject.org/api/v2/doc/doc"

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": getattr(settings, "GEO_MAX_ARTICLES", 25),
        "timespan": f"{getattr(settings, 'GEO_LOOKBACK_HOURS', 6)}h",
        "sort": "hybridrel"
    }

    r = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    if r.status_code != 200:
        raise RuntimeError(f"GDELT error {r.status_code}: {r.text[:200]}")

    data = r.json()

    return data.get("articles", [])


def get_geo_snapshot() -> dict:
    try:
        articles = fetch_gdelt_articles()
    except Exception as e:
        return {
            "score": 0.0,
            "narrative": "Géopolitique neutre ou indisponible",
            "article_count": 0,
            "top_titles": [],
            "error": str(e)[:150]
        }

    total_score = 0.0
    titles = []

    for article in articles:
        title = article.get("title", "")
        domain = article.get("domain", "")

        text = f"{title} {domain}"
        total_score += score_text(text)

        if title and len(titles) < 5:
            clean_title = (
                title.replace("*", "")
                .replace("_", "")
                .replace("`", "")
                .strip()
            )
            titles.append(clean_title[:120])

    if articles:
        score = total_score / max(3, min(len(articles), 15))
    else:
        score = 0.0

    score = clamp(score)

    if score >= 0.35:
        narrative = "Risk-off géopolitique favorable à l'or"
    elif score <= -0.25:
        narrative = "Détente / risk-on défavorable à l'or"
    elif score > 0.10:
        narrative = "Léger soutien géopolitique"
    elif score < -0.10:
        narrative = "Légère pression géopolitique"
    else:
        narrative = "Géopolitique neutre"

    return {
        "score": round(float(score), 3),
        "narrative": narrative,
        "article_count": len(articles),
        "top_titles": titles
    }
