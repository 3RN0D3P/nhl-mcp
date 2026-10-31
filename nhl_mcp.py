"""
NHL MCP — serveur MCP léger pour les stats de la LNH.
Source : API publique LNH (api-web.nhle.com), gratuite, sans clé.

Ajouter une stat plus tard :
  1. Trouver l'endpoint (voir la liste ENDPOINTS UTILES en bas).
  2. Copier un outil existant (@mcp.tool), changer l'URL et les champs retournés.
  3. Redémarrer le service : systemctl restart nhl-mcp
"""

import csv
import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastmcp import FastMCP

API = "https://api-web.nhle.com/v1"
SEARCH = "https://search.d3.nhle.com/api/v1/search/player"
TZ = ZoneInfo("America/Toronto")
CACHE_TTL = 30  # secondes (évite de marteler l'API pendant un match)
STATS = "https://api.nhle.com/stats/rest/en"  # API stats LNH (rapports par équipe)
MONEYPUCK = "https://moneypuck.com/moneypuck/playerData/seasonSummary"
MP_TTL = 3600  # MoneyPuck se met à jour une fois par jour : cache d'une heure

mcp = FastMCP("NHL")
_client = httpx.AsyncClient(timeout=10, headers={"User-Agent": "nhl-mcp/1.0"})
_cache: dict[str, tuple[float, object]] = {}


# ---------- Utilitaires ----------

async def _get(url: str, params: dict | None = None):
    key = url + str(sorted((params or {}).items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    r = await _client.get(url, params=params, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


def _saison_mp(saison: int | None) -> int:
    """MoneyPuck identifie une saison par son année de début (2026 = 2026-27)."""
    if saison:
        return saison
    now = datetime.now(TZ)
    return now.year if now.month >= 9 else now.year - 1


async def _mp(fichier: str, saison: int | None = None) -> list[dict]:
    """Télécharge un CSV MoneyPuck (skaters, goalies, teams, lines) avec cache d'une heure."""
    url = f"{MONEYPUCK}/{_saison_mp(saison)}/regular/{fichier}.csv"
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < MP_TTL:
        return hit[1]
    r = await _client.get(url, follow_redirects=True)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    _cache[url] = (time.time(), rows)
    return rows


def _f(row: dict, *cols, defaut=0.0) -> float:
    """Lit la première colonne présente parmi plusieurs noms possibles."""
    for c in cols:
        v = row.get(c)
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                pass
    return defaut


def _txt(v):
    """Les noms LNH arrivent souvent sous la forme {"default": "Nom", "fr": "..."}."""
    if isinstance(v, dict):
        return v.get("fr") or v.get("default") or ""
    return v or ""


async def _trouver_joueur(nom: str) -> dict:
    """Résout un nom (ou un ID) en joueur actif."""
    if str(nom).isdigit():
        return {"id": int(nom)}
    res = await _get(SEARCH, {"culture": "en-us", "limit": 10, "q": nom, "active": "true"})
    if not res:
        res = await _get(SEARCH, {"culture": "en-us", "limit": 10, "q": nom})
    if not res:
        raise ValueError(f"Aucun joueur trouvé pour « {nom} »")
    p = res[0]
    return {
        "id": int(p["playerId"]),
        "nom": p.get("name"),
        "equipe": p.get("teamAbbrev") or p.get("lastTeamAbbrev"),
        "position": p.get("positionCode"),
        "autres_resultats": [f'{x.get("name")} ({x.get("teamAbbrev")})' for x in res[1:4]],
    }


# ---------- Équipes : noms, surnoms et villes -> abréviation ----------
# Les petits modèles (ex. Ollama dans Home Assistant) passent souvent « Canadiens »
# ou « Habs » au lieu de MTL. On accepte tout ça et on refuse clairement l'inconnu.

EQUIPES = {
    "ANA": ["anaheim", "ducks"],
    "BOS": ["boston", "bruins"],
    "BUF": ["buffalo", "sabres"],
    "CAR": ["carolina", "caroline", "hurricanes", "canes"],
    "CBJ": ["columbus", "blue jackets", "jackets"],
    "CGY": ["calgary", "flames"],
    "CHI": ["chicago", "blackhawks", "hawks"],
    "COL": ["colorado", "avalanche", "avs"],
    "DAL": ["dallas", "stars"],
    "DET": ["detroit", "red wings", "wings"],
    "EDM": ["edmonton", "oilers"],
    "FLA": ["florida", "floride", "panthers"],
    "LAK": ["los angeles", "la", "kings"],
    "MIN": ["minnesota", "wild"],
    "MTL": ["montreal", "canadiens", "canadien", "habs", "ch", "le ch", "glorieux", "tricolore"],
    "NJD": ["new jersey", "devils", "nj"],
    "NSH": ["nashville", "predators", "preds"],
    "NYI": ["islanders", "new york islanders", "isles"],
    "NYR": ["rangers", "new york rangers"],
    "OTT": ["ottawa", "senators", "senateurs", "sens"],
    "PHI": ["philadelphia", "philadelphie", "flyers"],
    "PIT": ["pittsburgh", "penguins", "pingouins", "pens"],
    "SEA": ["seattle", "kraken"],
    "SJS": ["san jose", "sharks", "sj"],
    "STL": ["st louis", "saint louis", "blues"],
    "TBL": ["tampa", "tampa bay", "lightning", "bolts", "tb"],
    "TOR": ["toronto", "maple leafs", "leafs"],
    "UTA": ["utah", "mammoth", "utah mammoth", "utah hockey club"],
    "VAN": ["vancouver", "canucks"],
    "VGK": ["vegas", "las vegas", "golden knights", "knights", "vgk"],
    "WPG": ["winnipeg", "jets"],
    "WSH": ["washington", "capitals", "caps"],
}
# Codes MoneyPuck différents de ceux de la LNH
MP_CODES = {"L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL"}


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace(".", " ").replace("-", " ").replace("'", " ")
    for mot in ("les ", "le ", "the "):
        if s.startswith(mot) and s.strip() not in ("le ch",):
            s = s[len(mot):]
    return " ".join(s.split())


_ALIAS = {}
for _code, _noms in EQUIPES.items():
    _ALIAS[_norm(_code)] = _code
    for _n in _noms:
        _ALIAS[_norm(_n)] = _code
_ALIAS["le ch"] = _ALIAS["ch"] = "MTL"


def _equipe(nom: str) -> str:
    """'Canadiens', 'CH', 'Habs', 'Montréal', 'mtl' -> 'MTL'. Erreur claire si inconnue."""
    brut = (nom or "").strip().upper()
    if brut in MP_CODES:
        return MP_CODES[brut]
    n = _norm(nom or "")
    if n in _ALIAS:
        return _ALIAS[n]
    # « Canadiens de Montréal », « Toronto Maple Leafs »… : on cherche un alias dans le texte
    trouve = {c for a, c in _ALIAS.items() if len(a) > 3 and f" {a} " in f" {n} "}
    if len(trouve) == 1:
        return trouve.pop()
    raise ValueError(
        f"Équipe inconnue : « {nom} ». Utilise une abréviation LNH : " + ", ".join(EQUIPES)
    )


def _code_mp(code: str | None) -> str:
    """Code d'équipe MoneyPuck -> code LNH."""
    code = (code or "").upper()
    return MP_CODES.get(code, code)


def _aujourdhui() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


async def _match_equipe(equipe: str, date: str | None = None) -> dict | None:
    data = await _get(f"{API}/score/{date or _aujourdhui()}")
    equipe = _equipe(equipe)
    for g in data.get("games", []):
        if equipe in (g["awayTeam"].get("abbrev"), g["homeTeam"].get("abbrev")):
            return g
    return None


def _ligne_joueur(p: dict) -> dict:
    return {
        "nom": _txt(p.get("name")),
        "no": p.get("sweaterNumber"),
        "pos": p.get("position"),
        "buts": p.get("goals", 0),
        "passes": p.get("assists", 0),
        "points": p.get("points", 0),
        "tirs": p.get("sog", p.get("shots", 0)),
        "toi": p.get("toi"),
        "mises_en_echec": p.get("hits"),
        "tirs_bloques": p.get("blockedShots"),
        "plus_minus": p.get("plusMinus"),
    }


# ---------- Outils ----------

@mcp.tool
async def chercher_joueur(nom: str) -> dict:
    """Trouve un joueur LNH par nom (ex. 'bolduc', 'hutson'). Retourne son ID, équipe et position."""
    return await _trouver_joueur(nom)


@mcp.tool
async def stats_joueur(nom: str) -> dict:
    """Stats de la saison en cours d'un joueur (buts, passes, points, +/-, tirs, TOI moyen)
    et ses 5 derniers matchs. Accepte un nom ou un ID."""
    j = await _trouver_joueur(nom)
    d = await _get(f"{API}/player/{j['id']}/landing")
    saison = (d.get("featuredStats") or {}).get("regularSeason", {}).get("subSeason", {})
    return {
        "joueur": f'{_txt(d.get("firstName"))} {_txt(d.get("lastName"))}',
        "equipe": d.get("currentTeamAbbrev"),
        "position": d.get("position"),
        "numero": d.get("sweaterNumber"),
        "saison": (d.get("featuredStats") or {}).get("season"),
        "stats_saison": saison,
        "5_derniers_matchs": [
            {
                "date": g.get("gameDate"),
                "adversaire": g.get("opponentAbbrev"),
                "buts": g.get("goals"),
                "passes": g.get("assists"),
                "tirs": g.get("shots"),
                "toi": g.get("toi"),
            }
            for g in d.get("last5Games", [])
        ],
    }


@mcp.tool
async def matchs_du_jour(date: str | None = None) -> list[dict]:
    """Matchs d'une journée (défaut : aujourd'hui, heure de l'Est) avec score, état et période.
    date au format AAAA-MM-JJ."""
    data = await _get(f"{API}/score/{date or _aujourdhui()}")
    out = []
    for g in data.get("games", []):
        debut = g.get("startTimeUTC")
        if debut:
            debut = datetime.fromisoformat(debut.replace("Z", "+00:00")).astimezone(TZ).strftime("%H:%M")
        out.append({
            "game_id": g.get("id"),
            "match": f'{g["awayTeam"].get("abbrev")} @ {g["homeTeam"].get("abbrev")}',
            "score": f'{g["awayTeam"].get("score", 0)}-{g["homeTeam"].get("score", 0)}',
            "etat": g.get("gameState"),  # FUT, PRE, LIVE, CRIT, FINAL, OFF
            "debut_est": debut,
            "periode": (g.get("periodDescriptor") or {}).get("number"),
            "temps_restant": (g.get("clock") or {}).get("timeRemaining"),
        })
    return out


@mcp.tool
async def boxscore(game_id: int) -> dict:
    """Feuille de match complète : buts, passes, tirs, temps de glace (TOI) de chaque joueur."""
    d = await _get(f"{API}/gamecenter/{game_id}/boxscore")
    stats = d.get("playerByGameStats", {})
    out = {"score": f'{d["awayTeam"].get("abbrev")} {d["awayTeam"].get("score", 0)} - '
                     f'{d["homeTeam"].get("score", 0)} {d["homeTeam"].get("abbrev")}',
           "etat": d.get("gameState")}
    for cote in ("awayTeam", "homeTeam"):
        t = stats.get(cote, {})
        out[d[cote].get("abbrev")] = [
            _ligne_joueur(p) for p in t.get("forwards", []) + t.get("defense", [])
        ]
        out[f'{d[cote].get("abbrev")}_gardiens'] = [{
            "nom": _txt(g.get("name")),
            "partant": g.get("starter"),
            "toi": g.get("toi"),
            "tirs_recus": g.get("shotsAgainst"),
            "arrets": g.get("saves"),
            "buts_alloues": g.get("goalsAgainst"),
            "sv_pct": g.get("savePctg"),
            # Format "arrêts/tirs". L'API LNH nomme la situation du point de vue de l'adversaire :
            # powerPlay = l'adversaire est en avantage (son équipe est en désavantage).
            "arrets_par_situation": {
                "egalite": g.get("evenStrengthShotsAgainst"),
                "equipe_en_desavantage": g.get("powerPlayShotsAgainst"),
                "equipe_en_avantage": g.get("shorthandedShotsAgainst"),
            },
            "decision": g.get("decision"),
        } for g in t.get("goalies", []) if g.get("toi") not in (None, "00:00")]
    return out


@mcp.tool
async def joueur_ce_soir(nom: str, date: str | None = None) -> dict:
    """Ligne de stats d'un joueur dans le match de son équipe aujourd'hui (ou à une date) :
    temps de glace, buts, passes, tirs. Fonctionne en direct pendant le match."""
    j = await _trouver_joueur(nom)
    if "equipe" not in j:
        d = await _get(f"{API}/player/{j['id']}/landing")
        j["equipe"] = d.get("currentTeamAbbrev")
    g = await _match_equipe(j["equipe"], date)
    if not g:
        return {"joueur": j.get("nom"), "message": f'{j["equipe"]} ne joue pas ce jour-là.'}
    if g.get("gameState") in ("FUT", "PRE"):
        return {"joueur": j.get("nom"), "message": "Le match n'est pas commencé.", "game_id": g["id"]}
    d = await _get(f"{API}/gamecenter/{g['id']}/boxscore")
    for cote in ("awayTeam", "homeTeam"):
        t = d.get("playerByGameStats", {}).get(cote, {})
        for p in t.get("forwards", []) + t.get("defense", []) + t.get("goalies", []):
            if p.get("playerId") == j["id"]:
                return {"match": f'{d["awayTeam"]["abbrev"]} @ {d["homeTeam"]["abbrev"]}',
                        "etat": d.get("gameState"), **_ligne_joueur(p)}
    return {"joueur": j.get("nom"), "message": "Joueur absent de la feuille de match (blessé ou retranché)."}


@mcp.tool
async def classement(division: str | None = None) -> list[dict]:
    """Classement LNH actuel. Filtre optionnel : division (Atlantic, Metropolitan, Central, Pacific)."""
    d = await _get(f"{API}/standings/now")
    out = []
    for t in d.get("standings", []):
        if division and division.lower() not in (t.get("divisionName", "")).lower():
            continue
        out.append({
            "equipe": _txt(t.get("teamAbbrev")),
            "division": t.get("divisionName"),
            "pj": t.get("gamesPlayed"),
            "v": t.get("wins"), "d": t.get("losses"), "dp": t.get("otLosses"),
            "pts": t.get("points"),
            "serie": f'{t.get("streakCode", "")}{t.get("streakCount", "")}',
        })
    return out


@mcp.tool
async def calendrier_equipe(equipe: str) -> list[dict]:
    """Matchs de la semaine pour une équipe. equipe : abréviation (MTL, TOR...) ou nom
    (Canadiens, CH, Habs, Leafs...)."""
    d = await _get(f"{API}/club-schedule/{_equipe(equipe)}/week/now")
    return [{
        "game_id": g.get("id"),
        "date": g.get("gameDate"),
        "match": f'{g["awayTeam"].get("abbrev")} @ {g["homeTeam"].get("abbrev")}',
        "etat": g.get("gameState"),
    } for g in d.get("games", [])]


@mcp.tool
async def meneurs(categorie: str = "goals", limite: int = 10) -> list[dict]:
    """Meneurs de la LNH cette saison. Catégories : goals, assists, points, plusMinus,
    goalsPp, goalsSh, toi, faceoffLeaders."""
    d = await _get(f"{API}/skater-stats-leaders/current", {"categories": categorie, "limit": limite})
    return [{
        "joueur": f'{_txt(p.get("firstName"))} {_txt(p.get("lastName"))}',
        "equipe": p.get("teamAbbrev"),
        "valeur": p.get("value"),
    } for p in d.get(categorie, [])]


# ---------- Unités spéciales ----------

def _saison_lnh() -> str:
    a = _saison_mp(None)
    return f"{a}{a + 1}"


def _rang(rows: list[dict], cle: str, team_id, plus_haut_meilleur=True) -> int | None:
    tri = sorted(rows, key=lambda r: r.get(cle) or 0, reverse=plus_haut_meilleur)
    for i, r in enumerate(tri, 1):
        if r.get("teamId") == team_id:
            return i
    return None


@mcp.tool
async def unites_speciales(equipe: str) -> dict:
    """Unités spéciales d'une équipe : avantage numérique (AN, power play, PP) et
    désavantage numérique (DN, penalty kill, PK) pour la saison — format buts/occasions,
    pourcentage et rang LNH, plus le dernier match joué.
    equipe : abréviation (MTL, TOR...) ou nom (Canadiens, CH, Habs, Leafs...)."""
    equipe = _equipe(equipe)
    expr = f"seasonId={_saison_lnh()} and gameTypeId=2"
    equipes = (await _get(f"{STATS}/team")).get("data", [])
    team = next((t for t in equipes if t.get("triCode") == equipe), None)
    if not team:
        raise ValueError(f"Équipe inconnue : {equipe}")
    tid = team["id"]

    pp = (await _get(f"{STATS}/team/powerplay", {"cayenneExp": expr, "limit": 50})).get("data", [])
    pk = (await _get(f"{STATS}/team/penaltykill", {"cayenneExp": expr, "limit": 50})).get("data", [])
    a = next((r for r in pp if r.get("teamId") == tid), {})
    d = next((r for r in pk if r.get("teamId") == tid), {})

    out = {
        "equipe": equipe,
        "nom": team.get("fullName"),  # ex. « Montréal Canadiens » : évite qu'un petit modèle croie à une autre équipe
        "avantage_numerique": {
            "format": f'{a.get("powerPlayGoalsFor", 0)}/{a.get("ppOpportunities", 0)}',
            "pct": round((a.get("powerPlayPct") or 0) * 100, 1),
            "rang": _rang(pp, "powerPlayPct", tid),
        },
        "desavantage_numerique": {
            "format": f'{d.get("timesShorthanded", 0) - d.get("ppGoalsAgainst", 0)}/{d.get("timesShorthanded", 0)}',
            "buts_alloues": d.get("ppGoalsAgainst"),
            "pct": round((d.get("penaltyKillPct") or 0) * 100, 1),
            "rang": _rang(pk, "penaltyKillPct", tid),
        },
    }

    # Dernier match terminé : AN de chaque équipe (format "1/3")
    try:
        cal = await _get(f"{API}/club-schedule-season/{equipe}/now")
        fini = [g for g in cal.get("games", []) if g.get("gameState") in ("OFF", "FINAL")]
        if fini:
            g = fini[-1]
            rr = await _get(f"{API}/gamecenter/{g['id']}/right-rail")
            stats = {s.get("category"): s for s in rr.get("teamGameStats", [])}
            an = stats.get("powerPlay", {})
            dom = g["homeTeam"]["abbrev"] == equipe
            adv = g["awayTeam"]["abbrev"] if dom else g["homeTeam"]["abbrev"]
            out["dernier_match"] = {
                "date": g.get("gameDate"),
                "adversaire": adv,
                "an": an.get("homeValue" if dom else "awayValue"),
                "an_adversaire": an.get("awayValue" if dom else "homeValue"),
            }
    except Exception as e:  # le dernier match est un bonus, pas bloquant
        out["dernier_match"] = f"indisponible ({e.__class__.__name__})"
    return out


# ---------- Expected goals (MoneyPuck) ----------
# Situations : all, 5on5, 5on4 (avantage numérique), 4on5 (désavantage), other

def _ligne_xg(r: dict) -> dict:
    toi_min = _f(r, "icetime") / 60
    gp = _f(r, "games_played") or 1
    xg = _f(r, "I_F_xGoals")
    buts = _f(r, "I_F_goals")
    return {
        "joueur": r.get("name"),
        "equipe": r.get("team"),
        "pos": r.get("position"),
        "situation": r.get("situation"),
        "pj": int(gp),
        "toi_moy": f"{int(toi_min / gp)}:{int((toi_min / gp % 1) * 60):02d}",
        "buts": int(buts),
        "xg": round(xg, 2),
        "buts_moins_xg": round(buts - xg, 2),  # + = chanceux/finisseur, - = malchanceux
        "tirs": int(_f(r, "I_F_shotsOnGoal")),
        "xg_par_60": round(xg / toi_min * 60, 2) if toi_min else None,
        "xg_pct_sur_glace": round(_f(r, "onIce_xGoalsPercentage") * 100, 1),
        "xgf_sur_glace": round(_f(r, "OnIce_F_xGoals"), 2),
        "xga_sur_glace": round(_f(r, "OnIce_A_xGoals"), 2),
        "game_score": round(_f(r, "gameScore"), 2),
    }


@mcp.tool
async def xg_joueur(nom: str, situation: str = "all", saison: int | None = None) -> dict:
    """Expected goals d'un joueur (MoneyPuck) : xG, buts vs xG, xG/60, xG% sur glace.
    situation : all, 5on5, 5on4, 4on5. saison : année de début (2026 = 2026-27)."""
    j = await _trouver_joueur(nom)
    for r in await _mp("skaters", saison):
        if str(r.get("playerId")) == str(j["id"]) and r.get("situation") == situation:
            return {**_ligne_xg(r), "source": "MoneyPuck.com"}
    return {"message": f"Pas de données MoneyPuck pour {j.get('nom', nom)} ({situation})."}


@mcp.tool
async def meneurs_xg(tri: str = "xg", situation: str = "all", equipe: str | None = None,
                     min_pj: int = 5, limite: int = 10) -> list[dict]:
    """Classement des patineurs selon MoneyPuck. tri : xg, xg_par_60, buts_moins_xg,
    xg_pct_sur_glace, game_score. Filtre optionnel par équipe (MTL, Canadiens, Leafs...)."""
    equipe = _equipe(equipe) if equipe else None
    lignes = [
        _ligne_xg(r) for r in await _mp("skaters")
        if r.get("situation") == situation
        and _f(r, "games_played") >= min_pj
        and (not equipe or _code_mp(r.get("team")) == equipe)
    ]
    lignes.sort(key=lambda x: x.get(tri) or 0, reverse=True)
    return lignes[:limite]


@mcp.tool
async def xg_equipes(situation: str = "5on5") -> list[dict]:
    """Expected goals par équipe (MoneyPuck), triés par xG% : xGF, xGA, buts vs attendus."""
    out = []
    for r in await _mp("teams"):
        if r.get("situation") != situation:
            continue
        xgf, xga = _f(r, "xGoalsFor"), _f(r, "xGoalsAgainst")
        out.append({
            "equipe": r.get("team"),
            "pj": int(_f(r, "games_played")),
            "xg_pct": round(_f(r, "xGoalsPercentage") * 100, 1),
            "xgf": round(xgf, 1),
            "xga": round(xga, 1),
            "bp": int(_f(r, "goalsFor")),
            "bc": int(_f(r, "goalsAgainst")),
            "bp_moins_xgf": round(_f(r, "goalsFor") - xgf, 1),
            "bc_moins_xga": round(_f(r, "goalsAgainst") - xga, 1),
        })
    out.sort(key=lambda x: x["xg_pct"], reverse=True)
    return out


@mcp.tool
async def gsax_gardiens(min_pj: int = 3, limite: int = 15, situation: str = "all") -> list[dict]:
    """Gardiens classés par buts sauvés au-dessus des attentes (GSAx, MoneyPuck).
    GSAx positif = le gardien arrête plus que la qualité des tirs le prédit."""
    out = []
    for r in await _mp("goalies"):
        if r.get("situation") != situation or _f(r, "games_played") < min_pj:
            continue
        xg, ga = _f(r, "xGoals"), _f(r, "goals")
        tirs = _f(r, "ongoal")
        out.append({
            "gardien": r.get("name"),
            "equipe": r.get("team"),
            "pj": int(_f(r, "games_played")),
            "gsax": round(xg - ga, 2),
            "xga": round(xg, 2),
            "bc": int(ga),
            "sv_pct": round(1 - ga / tirs, 3) if tirs else None,
        })
    out.sort(key=lambda x: x["gsax"], reverse=True)
    return out[:limite]


# ---------- Blessés (MoneyPuck) ----------

MP_BLESSES = "https://moneypuck.com/moneypuck/playerData/playerNews"
BLESSES_TTL = 1800  # 30 min
STATUTS_BLESSURE = {
    "DTD": "Jour à jour",
    "O": "Out",
    "IR": "Réserve des blessés",
    "IR-LT": "Réserve longue durée",
    "IR-NR": "Réserve (hors alignement)",
}


def _entier(v) -> int | None:
    """'2.0' -> 2 ; vide ou -999 (inconnu chez MoneyPuck) -> None."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return None if n <= -999 else int(n)


async def _blesses_mp() -> tuple[list[dict], str | None]:
    url = f"{MP_BLESSES}/current_injuries.csv"
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < BLESSES_TTL:
        return hit[1]
    r = await _client.get(url, follow_redirects=True)
    r.raise_for_status()
    rows = []
    for row in csv.DictReader(io.StringIO(r.text)):
        retour = row.get("dateOfReturn") or None
        if retour and retour.startswith("2099"):
            retour = None  # date de retour inconnue
        statut = row.get("playerInjuryStatus") or ""
        rows.append({
            "joueur": row.get("playerName"),
            "equipe": _code_mp(row.get("teamCode")),
            "pos": row.get("position"),
            "blessure": row.get("yahooInjuryDescription") or "Non précisé",
            "statut": statut,
            "statut_fr": STATUTS_BLESSURE.get(statut, statut),
            "retour_prevu": retour,
            "matchs_a_manquer": _entier(row.get("gamesStillToMiss")),
            "matchs_manques": _entier(row.get("gamesMissedSoFar")),
            "dernier_match": row.get("lastGameDate") or None,
        })
    try:
        m = await _client.get(f"{MP_BLESSES}/injury_update_time.txt", follow_redirects=True)
        maj = m.text.strip() if m.status_code == 200 else None
    except httpx.HTTPError:
        maj = None
    _cache[url] = (time.time(), (rows, maj))
    return rows, maj


@mcp.tool
async def blesses(equipe: str | None = None, statut: str | None = None) -> dict:
    """Joueurs blessés LNH (MoneyPuck) : blessure, statut (DTD, O, IR, IR-LT, IR-NR),
    retour prévu et matchs encore à manquer.
    equipe : abréviation (MTL, TOR...) ou nom (Canadiens, CH, Habs...) ; sans équipe = toute la ligue.
    statut : filtre optionnel (ex. DTD = incertains pour le prochain match)."""
    rows, maj = await _blesses_mp()
    code = _equipe(equipe) if equipe else None
    if code:
        rows = [r for r in rows if r["equipe"] == code]
    if statut:
        rows = [r for r in rows if r["statut"] == statut.upper()]
    rows = sorted(rows, key=lambda r: (r["equipe"] or "", r["retour_prevu"] or "9999"))
    return {"equipe": code or "toute la ligue", "mise_a_jour": maj, "nb": len(rows), "blesses": rows}


# ---------- Démarrage ----------

if __name__ == "__main__":
    secret = os.environ.get("NHL_MCP_SECRET", "")
    path = f"/{secret}/mcp" if secret else "/mcp"
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8765)), path=path)


# ENDPOINTS UTILES pour ajouter des stats plus tard (préfixe API) :
#   /gamecenter/{id}/play-by-play     événements (buts, tirs) -> animation de but
#   /gamecenter/{id}/landing          résumé du match, trois étoiles
#   /player/{id}/game-log/now         stats match par match de la saison
#   /goalie-stats-leaders/current     meneurs gardiens (savePctg, goalsAgainstAverage, wins)
#   /roster/{EQUIPE}/current          alignement d'une équipe
#   /club-stats/{EQUIPE}/now          stats de tous les joueurs d'une équipe
#   /schedule/now                     calendrier de la ligue (semaine)
#
# MoneyPuck (MONEYPUCK/{année}/regular/{fichier}.csv) :
#   skaters, goalies, teams, lines   -> déjà branchés sauf "lines" (xG par trio / paire)
#   Ouvrir le CSV une fois pour voir toutes les colonnes : il y en a plus de 150
#   (high danger shots, rebonds, xG sans rebond, etc.). Ajouter une colonne = une ligne dans _ligne_xg.
