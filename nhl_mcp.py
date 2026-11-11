"""
NHL MCP — serveur MCP léger pour les stats de la LNH.
Source : API publique LNH (api-web.nhle.com), gratuite, sans clé.

Ajouter une stat plus tard :
  1. Trouver l'endpoint (voir la liste ENDPOINTS UTILES en bas).
  2. Copier un outil existant (@mcp.tool), changer l'URL et les champs retournés.
  3. Redémarrer le service : systemctl restart nhl-mcp
"""

import csv
import functools
import inspect
import io
import json
import re
import os
import time
from datetime import datetime, timedelta
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

def _outil_texte(fn):
    """Fait renvoyer à un outil du texte brut : la phrase « reponse » d'abord, les détails ensuite.
    Avec un dict, Home Assistant passe au modèle un JSON échappé en double (content +
    structuredContent) que Qwen3 4B lisait mal (« Non » alors que deja_affronte=true)."""
    @functools.wraps(fn)
    async def w(*a, **k):
        d = await fn(*a, **k)
        reponse = re.sub(r"\b(" + "|".join(NOMS) + r")\b", lambda m: NOMS[m.group(1)], d.pop("reponse", ""))
        return f"{reponse}\n\nDétails : {json.dumps(d, ensure_ascii=False, separators=(',', ':'))}"
    del w.__wrapped__
    w.__signature__ = inspect.signature(fn).replace(return_annotation=str)
    w.__annotations__ = {**fn.__annotations__, "return": str}
    return w


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


async def _chercher(q: str) -> list:
    """Joueurs actifs d'abord, puis retraités : sans ça, « Martin St-Louis » tombait sur un
    joueur actif prénommé Martin au lieu du retraité."""
    actifs = await _get(SEARCH, {"culture": "en-us", "limit": 10, "q": q, "active": "true"}) or []
    tous = await _get(SEARCH, {"culture": "en-us", "limit": 10, "q": q}) or []
    vus = {r.get("playerId") for r in actifs}
    return actifs + [r for r in tous if r.get("playerId") not in vus]


def _ressemblance(nom: str, candidat: str) -> float:
    """Ressemblance 0-1 entre le nom demandé et un nom de joueur (nom de famille seul si un seul mot)."""
    from difflib import SequenceMatcher
    q, c = _norm(nom), _norm(candidat)
    if " " not in q.strip():
        c = c.split(" ")[-1]
    return SequenceMatcher(None, q, c).ratio()


async def _chercher_nom(nom: str) -> list:
    """Recherche tolérante aux fautes : un nom dicté ou tapé vite (« slafkovski », « Cole Caulfield »)
    est réessayé sans accents, avec y/i interchangés, avec une lettre en moins, puis en raccourcissant
    la fin du mot. Les candidats sont classés par ressemblance avec le nom demandé : l'API LNH renvoie
    parfois n'importe quel « Cole » pour un nom de famille mal orthographié. Avec un nom de famille seul,
    un joueur du CH à une lettre près passe devant un homonyme exact (Caulfield -> Cole Caufield)."""
    dernier = _norm(nom).split(" ")[-1]
    seul = " " not in nom.strip()
    base = [nom, _norm(nom), dernier]
    if dernier.endswith("i"):
        base.append(dernier[:-1] + "y")
    if dernier.endswith("y"):
        base.append(dernier[:-1] + "i")
    phases = [
        (base, True),
        ([dernier[:i] + dernier[i + 1:] for i in range(len(dernier))], False),  # une lettre en trop
        ([dernier[:n] for n in range(len(dernier) - 1, 3, -1)], False),          # fin mal entendue
    ]
    vus, candidats = set(), {}

    def score(r):
        return _ressemblance(nom, r.get("name", ""))

    def trouve(ch_requis):
        bons = [r for r in candidats.values() if score(r) >= 0.85]
        return bool(bons) and (not ch_requis or any(r.get("teamAbbrev") == "MTL" for r in bons))

    for i, (essais, tous) in enumerate(phases):
        for q in essais:
            if not q or q in vus or len(q) < 4:
                continue
            vus.add(q)
            res = await _chercher(q) if tous else await _get(SEARCH, {"culture": "en-us", "limit": 10, "q": q, "active": "true"})
            for r in res or []:
                candidats.setdefault(r.get("playerId"), r)
            if not seul and trouve(False):
                break
        # Nom de famille seul : après la 1re phase, on ne s'arrête que si un joueur du CH ressemble ;
        # après les variantes à une lettre près, tout bon candidat suffit.
        if trouve(seul and i == 0):
            break

    def rang(r):
        s = score(r)
        return (seul and s >= 0.85 and r.get("teamAbbrev") == "MTL", s)
    return sorted(candidats.values(), key=rang, reverse=True)


async def _trouver_joueur(nom: str) -> dict:
    """Résout un nom (ou un ID) en joueur actif."""
    if str(nom).isdigit():
        return {"id": int(nom)}
    try:
        code = _equipe(nom)
    except ValueError:
        code = None
    if code and _norm(nom) in _ALIAS:
        raise ValueError(f"« {nom} » est une équipe ({code}), pas un joueur. Pour savoir quel joueur de "
                         f"{code} est le plus susceptible de tirer, marquer ou compter, utilise "
                         f"tendances_joueurs(equipe=\"{code}\").")
    res = await _chercher_nom(nom)
    if not res:
        raise ValueError(f"Aucun joueur trouvé pour « {nom} »")
    p = res[0]
    # Nom de famille seul et plusieurs joueurs du même nom (ex. Fowler) : on privilégie le CH,
    # l'équipe suivie à la maison. Les autres restent listés dans autres_resultats.
    if " " not in nom.strip():
        memes = [x for x in res if _norm(x.get("name", "")).split(" ")[-1] == _norm(nom)]
        if len(memes) > 1:
            p = next((x for x in memes if x.get("teamAbbrev") == "MTL"), p)
            res = [p] + [x for x in res if x is not p]
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
    "PIT": ["pittsburgh", "penguins", "pingouins", "pens", "pgh"],
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
# Noms dits dans les phrases « reponse » (le modèle 4B ne reliait pas « MTL » au « CH » demandé)
NOMS = {"ANA": "Ducks", "BOS": "Bruins", "BUF": "Sabres", "CAR": "Hurricanes", "CBJ": "Blue Jackets",
        "CGY": "Flames", "CHI": "Blackhawks", "COL": "Avalanche", "DAL": "Stars", "DET": "Red Wings",
        "EDM": "Oilers", "FLA": "Panthers", "LAK": "Kings", "MIN": "Wild", "MTL": "Canadiens",
        "NJD": "Devils", "NSH": "Predators", "NYI": "Islanders", "NYR": "Rangers", "OTT": "Sénateurs",
        "PHI": "Flyers", "PIT": "Penguins", "SEA": "Kraken", "SJS": "Sharks", "STL": "Blues",
        "TBL": "Lightning", "TOR": "Maple Leafs", "UTA": "Mammoth", "VAN": "Canucks",
        "VGK": "Golden Knights", "WPG": "Jets", "WSH": "Capitals"}
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
    # Début de nom : « pen », « pingou », « canad »… (un petit modèle invente parfois un code)
    if len(n) >= 3:
        trouve = {c for a, c in _ALIAS.items() if a.startswith(n)}
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


DATES_RELATIVES = {"aujourd hui": 0, "aujourdhui": 0, "ce soir": 0, "today": 0, "tonight": 0,
                   "hier": -1, "hier soir": -1, "yesterday": -1, "avant hier": -2,
                   "demain": 1, "demain soir": 1, "tomorrow": 1}


def _date(date: str | None) -> str:
    """AAAA-MM-JJ, « hier », « aujourd'hui », « demain »… -> AAAA-MM-JJ.
    Refuse une date loin de la saison : les petits modèles inventent souvent l'année."""
    auj = datetime.now(TZ).date()
    if not date:
        return auj.isoformat()
    n = _norm(str(date))
    if n in DATES_RELATIVES:
        return (auj + timedelta(days=DATES_RELATIVES[n])).isoformat()
    try:
        d = datetime.strptime(str(date).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Date invalide « {date} ». Utilise AAAA-MM-JJ, « hier », « aujourd'hui » "
                         f"ou « demain ». Aujourd'hui : {auj.isoformat()}.")
    if abs((d - auj).days) > 300:
        raise ValueError(f"Date « {date} » hors de la saison en cours. Aujourd'hui : {auj.isoformat()}. "
                         "Utilise plutôt « hier », « aujourd'hui » ou « demain ».")
    return d.isoformat()


def _heure(utc: str | None) -> str | None:
    if not utc:
        return None
    return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(TZ).strftime("%H:%M")


def _jj_mm(date: str) -> str:
    return datetime.strptime(date[:10], "%Y-%m-%d").strftime("%d/%m")


async def _match_equipe(equipe: str, date: str | None = None) -> dict | None:
    data = await _get(f"{API}/score/{_date(date)}")
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


@mcp.tool(output_schema=None)
@_outil_texte
async def alignement(equipe: str = "MTL", numero: int | None = None, position: str | None = None) -> dict:
    """Alignement actuel d'une équipe avec les numéros de chandail. « Qui porte le numéro 63 du CH ? »,
    « Qui sont les gardiens du Canadien ? » -> cet outil. numero : un numéro de chandail précis.
    position : attaquants, defenseurs ou gardiens. Ne jamais deviner un numéro de mémoire."""
    code = _equipe(equipe)
    d = await _get(f"{API}/roster/{code}/current")
    groupes = {"attaquants": "forwards", "defenseurs": "defensemen", "gardiens": "goalies"}
    joueurs = []
    for fr, en in groupes.items():
        for p in d.get(en, []):
            joueurs.append({
                "no": p.get("sweaterNumber"),
                "nom": f'{_txt(p.get("firstName"))} {_txt(p.get("lastName"))}',
                "pos": p.get("positionCode"),
                "groupe": fr,
            })
    joueurs.sort(key=lambda j: j["no"] or 999)
    if numero is not None:
        j = next((j for j in joueurs if j["no"] == int(numero)), None)
        reponse = (f'Le numéro {numero} des {NOMS.get(code, code)} est {j["nom"]} ({j["pos"]}).' if j
                   else f"Personne ne porte le numéro {numero} chez les {NOMS.get(code, code)} en ce moment.")
        return {"reponse": reponse, "joueur": j}
    if position:
        cle = _norm(position)
        cle = next((k for k in groupes if k.startswith(cle[:4])), None)
        if cle:
            joueurs = [j for j in joueurs if j["groupe"] == cle]
    reponse = f"Alignement des {NOMS.get(code, code)} : " + ", ".join(f'#{j["no"]} {j["nom"]}' for j in joueurs) + "."
    return {"reponse": reponse, "joueurs": joueurs}


@mcp.tool(output_schema=None)
@_outil_texte
async def stats_joueur(nom: str) -> dict:
    """Stats de la saison en cours d'un joueur (buts, passes, points, +/-, tirs, TOI moyen)
    et ses 5 derniers matchs. Accepte un nom ou un ID. Le total de la saison est dans la phrase
    de réponse ; les 5 derniers matchs sont des lignes de match, pas des totaux."""
    j = await _trouver_joueur(nom)
    d = await _get(f"{API}/player/{j['id']}/landing")
    saison = (d.get("featuredStats") or {}).get("regularSeason", {}).get("subSeason", {})
    qui = f'{_txt(d.get("firstName"))} {_txt(d.get("lastName"))}'
    if saison:
        reponse = (f'{qui} ({d.get("currentTeamAbbrev")}), saison en cours : {saison.get("goals", 0)} buts, '
                   f'{saison.get("assists", 0)} passes, {saison.get("points", 0)} points en '
                   f'{saison.get("gamesPlayed", 0)} matchs ({saison.get("plusMinus", 0):+}).')
    else:
        reponse = f"{qui} n'a pas encore joué cette saison."
    return {
        "reponse": reponse,
        "joueur": f'{_txt(d.get("firstName"))} {_txt(d.get("lastName"))}',
        "equipe": d.get("currentTeamAbbrev"),
        "position": d.get("position"),
        "numero": d.get("sweaterNumber"),
        "saison": (d.get("featuredStats") or {}).get("season"),
        "stats_saison": saison,
        "derniers_matchs_un_par_un": [
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


@mcp.tool(output_schema=None)
@_outil_texte
async def matchs_du_jour(date: str | None = None) -> dict:
    """Matchs et scores d'une journée (défaut : aujourd'hui, heure de l'Est) : score, état, période.
    date : « hier », « aujourd'hui », « demain » ou AAAA-MM-JJ. Pour « les scores d'hier soir »,
    passe date="hier". Le champ « reponse » contient la réponse prête à dire."""
    jour = _date(date)
    data = await _get(f"{API}/score/{jour}")
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
    morceaux = []
    for m in out:
        ext, dom = m["match"].split(" @ ")
        se, sd = m["score"].split("-")
        if m["etat"] in ("OFF", "FINAL"):
            morceaux.append(f"{ext} {se}, {dom} {sd} (final)")
        elif m["etat"] in ("LIVE", "CRIT"):
            morceaux.append(f"{ext} {se}, {dom} {sd} (en cours, {m['periode']}e période)")
        else:
            morceaux.append(f"{ext} à {dom} à {m['debut_est']}")
    reponse = (f"{len(out)} match(s) le {_jj_mm(jour)} : " + " ; ".join(morceaux) + ".") if out \
        else f"Aucun match le {_jj_mm(jour)}."
    return {"date": jour, "reponse": reponse, "matchs": out}


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
    """Ligne de stats d'UN joueur nommé dans le match de son équipe aujourd'hui (ou à une date) :
    temps de glace, buts, passes, tirs. Fonctionne en direct pendant le match. nom = un joueur,
    jamais une équipe. Pour « qui a le plus de chances de... » dans une équipe : tendances_joueurs."""
    date = _date(date)
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


@mcp.tool(output_schema=None)
@_outil_texte
async def classement(division: str | None = None, equipe: str | None = None) -> dict:
    """Classement LNH actuel. Filtres optionnels : division (Atlantic, Metropolitan, Central,
    Pacific) ou equipe (MTL, Canadiens, CH...) pour les points et le rang d'une seule équipe.
    Le champ « reponse » contient la réponse prête à dire."""
    d = await _get(f"{API}/standings/now")
    code = _equipe(equipe) if equipe else None
    if code:
        for t in d.get("standings", []):
            if _txt(t.get("teamAbbrev")) == code:
                return {"reponse": (f'{code} : {t.get("points")} points en {t.get("gamesPlayed")} matchs '
                                    f'({t.get("wins")} victoires, {t.get("losses")} défaites, '
                                    f'{t.get("otLosses")} défaites en prolongation), '
                                    f'{t.get("divisionSequence")}e de la division {t.get("divisionName")}, '
                                    f'{t.get("leagueSequence")}e dans la LNH.'),
                        "equipe": code, "pts": t.get("points"), "pj": t.get("gamesPlayed"),
                        "rang_division": t.get("divisionSequence"), "rang_lnh": t.get("leagueSequence")}
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
    reponse = ", ".join(f'{i}. {t["equipe"]} {t["pts"]} pts' for i, t in enumerate(out[:8], 1))
    return {"reponse": ("Classement : " + reponse + ".") if out else "Aucune équipe trouvée.",
            "note": "v = victoires, d = défaites, dp = défaites en prolongation (pas de matchs nuls)",
            "classement": out}


@mcp.tool(output_schema=None)
@_outil_texte
async def calendrier_equipe(equipe: str) -> dict:
    """Matchs de CETTE SEMAINE seulement pour une équipe. Ne couvre pas le reste de la
    saison : pour savoir si deux équipes se sont déjà affrontées, utilise face_a_face.
    equipe : abréviation (MTL, TOR...) ou nom (Canadiens, CH, Habs, Leafs...)."""
    eq = _equipe(equipe)
    d = await _get(f"{API}/club-schedule/{eq}/week/now")
    matchs = [{
        "game_id": g.get("id"),
        "date": g.get("gameDate"),
        "heure_est": _heure(g.get("startTimeUTC")),
        "match": f'{g["awayTeam"].get("abbrev")} @ {g["homeTeam"].get("abbrev")}',
        "etat": g.get("gameState"),
    } for g in d.get("games", [])]
    morceaux = []
    for m in matchs:
        ext, dom = m["match"].split(" @ ")
        adv = dom if ext == eq else ext
        lieu = "à domicile" if dom == eq else f"chez {dom}"
        morceaux.append(f'le {_jj_mm(m["date"])}' + (f' à {m["heure_est"]}' if m["heure_est"] else "")
                        + f" contre {adv} ({lieu})")
    reponse = (f"{eq} joue {len(matchs)} match(s) cette semaine : " + " ; ".join(morceaux) + ".") \
        if matchs else f"{eq} ne joue pas cette semaine."
    return {"equipe": eq, "reponse": reponse, "matchs": matchs}


@mcp.tool(output_schema=None)
@_outil_texte
async def dernier_match(equipe: str) -> dict:
    """Résultat du dernier match joué par une équipe (score, adversaire, date) et son prochain
    match. « Le score du CH hier / jeudi ? » -> cet outil. Le champ « reponse » est prêt à dire."""
    eq = _equipe(equipe)
    cal = await _get(f"{API}/club-schedule-season/{eq}/now")
    games = [g for g in cal.get("games", []) if g.get("gameType") in (2, 3)]
    fini = [g for g in games if g.get("gameState") in ("OFF", "FINAL")]
    a_venir = [g for g in games if g.get("gameState") not in ("OFF", "FINAL")]
    out = {"equipe": eq}
    phrases = []
    if fini:
        g = fini[-1]
        dom, ext = g["homeTeam"], g["awayTeam"]
        nous, eux = (dom, ext) if dom.get("abbrev") == eq else (ext, dom)
        gagne = (nous.get("score") or 0) > (eux.get("score") or 0)
        fin = (g.get("gameOutcome") or {}).get("lastPeriodType", "REG")
        fin_txt = {"OT": " en prolongation", "SO": " en tirs de barrage"}.get(fin, "")
        lieu = "à domicile" if dom.get("abbrev") == eq else f'chez {dom.get("abbrev")}'
        verbe = "a battu" if gagne else "a perdu contre"
        hs, bs = sorted([nous.get("score", 0), eux.get("score", 0)], reverse=True)
        phrases.append(f'Le {_jj_mm(g["gameDate"])}, {eq} {verbe} {eux.get("abbrev")} {hs}-{bs}{fin_txt} ({lieu}).')
        out["dernier"] = {"game_id": g.get("id"), "date": g.get("gameDate"),
                          "score": f'{ext.get("abbrev")} {ext.get("score")} - {dom.get("score")} {dom.get("abbrev")}',
                          "victoire": gagne}
    if a_venir:
        g = a_venir[0]
        dom, ext = g["homeTeam"], g["awayTeam"]
        adv = ext.get("abbrev") if dom.get("abbrev") == eq else dom.get("abbrev")
        lieu = "à domicile" if dom.get("abbrev") == eq else f'chez {dom.get("abbrev")}'
        h = _heure(g.get("startTimeUTC"))
        phrases.append(f'Prochain match : le {_jj_mm(g["gameDate"])}' + (f" à {h}" if h else "")
                       + f" contre {adv} ({lieu}).")
        out["prochain"] = {"game_id": g.get("id"), "date": g.get("gameDate"), "heure_est": h, "adversaire": adv}
    return {"reponse": " ".join(phrases) or f"Aucun match trouvé pour {eq}.", **out}


@mcp.tool(output_schema=None)
@_outil_texte
async def face_a_face(equipe: str, adversaire: str) -> dict:
    """À UTILISER dès que la question mentionne deux équipes : « X a-t-il déjà joué /
    affronté Y cette année ? », fiche contre Y, prochain match contre Y. Couvre toute
    la saison : matchs joués (score, gagnant), fiche victoires-défaites, matchs à venir.
    Le champ « reponse » contient la réponse prête à dire. equipe/adversaire : abréviation (MTL, PIT...) ou nom
    (Canadiens, CH, Penguins, Pingouins, Leafs...)."""
    eq, adv = _equipe(equipe), _equipe(adversaire)
    cal = await _get(f"{API}/club-schedule-season/{eq}/now")
    joues, a_venir = [], []
    v = d = dp = 0
    for g in cal.get("games", []):
        if g.get("gameType") != 2:  # saison régulière seulement
            continue
        dom, ext = g["homeTeam"], g["awayTeam"]
        if adv not in (dom.get("abbrev"), ext.get("abbrev")):
            continue
        lieu = f'{ext.get("abbrev")} @ {dom.get("abbrev")} ({dom.get("abbrev")} à domicile)'
        if g.get("gameState") in ("OFF", "FINAL"):
            nous = dom if dom.get("abbrev") == eq else ext
            eux = ext if nous is dom else dom
            gagne = (nous.get("score") or 0) > (eux.get("score") or 0)
            fin = (g.get("gameOutcome") or {}).get("lastPeriodType", "REG")
            if gagne:
                v += 1
            elif fin in ("OT", "SO"):
                dp += 1
            else:
                d += 1
            joues.append({
                "date": g.get("gameDate"),
                "match": lieu,
                "score": f'{ext.get("abbrev")} {ext.get("score", 0)} - {dom.get("score", 0)} {dom.get("abbrev")}',
                "gagnant": nous.get("abbrev") if gagne else eux.get("abbrev"),
                "fin": {"REG": "temps réglementaire", "OT": "prolongation", "SO": "tirs de barrage"}.get(fin, fin),
            })
        else:
            debut = g.get("startTimeUTC")
            if debut:
                debut = datetime.fromisoformat(debut.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
            a_venir.append({"date_heure_est": debut or g.get("gameDate"), "match": lieu})
    # Phrase prête à lire : les petits modèles (4B) contredisaient parfois le booléen.
    if joues:
        res = []
        for j in joues:
            jour = datetime.strptime(j["date"], "%Y-%m-%d").strftime("%d/%m")
            # « MTL 3 - 2 TOR » -> score du gagnant d'abord, lieu explicite
            ext, se, _, sd, dom = j["score"].split(" ")
            haut, bas = (se, sd) if j["gagnant"] == ext else (sd, se)
            fin = f' en {j["fin"]}' if j["fin"] != "temps réglementaire" else ""
            res.append(f'victoire de {j["gagnant"]} {haut}-{bas}{fin} le {jour}, chez {dom}')
        phrase = (f"Oui, {eq} a déjà affronté {adv} {len(joues)} fois cette saison : "
                  + " ; ".join(res) + ".")
    else:
        phrase = f"Non, {eq} n'a pas encore affronté {adv} cette saison."
    if a_venir:
        dom = a_venir[0]["match"].split(" @ ")[1].split(" ")[0]
        quand = a_venir[0]["date_heure_est"]
        phrase += f" Prochain match : le {_jj_mm(quand)} à {quand[11:16]}, chez {dom}."
    return {
        "reponse": phrase,
        "equipe": eq,
        "adversaire": adv,
        "deja_affronte_cette_saison": bool(joues),
        "fiche": f"{v}-{d}-{dp} (victoires-défaites-défaites en prolongation) pour {eq}",
        "matchs_joues": joues,
        "matchs_a_venir": a_venir,
    }


# Catégorie des meneurs -> (champ de /club-stats, libellé dit à voix haute)
_MENEURS_CLUB = {
    "goals": ("goals", "buts"), "assists": ("assists", "passes"), "points": ("points", "points"),
    "plusMinus": ("plusMinus", "+/-"), "goalsPp": ("powerPlayGoals", "buts en avantage numérique"),
    "goalsSh": ("shorthandedGoals", "buts en désavantage numérique"),
    "toi": ("avgTimeOnIcePerGame", "temps de glace moyen"),
    "faceoffLeaders": ("faceoffWinPctg", "% de mises en jeu gagnées"),
    "penaltyMins": ("penaltyMinutes", "minutes de pénalité"),
    "shots": ("shots", "tirs"),
    "gameWinningGoals": ("gameWinningGoals", "buts gagnants"),
}
# Catégories absentes de /skater-stats-leaders : classement LNH via l'API stats (skater/summary)
_MENEURS_STATS = {"penaltyMins": "penaltyMinutes", "shots": "shots", "gameWinningGoals": "gameWinningGoals"}
_MENEURS_SYN = {"buts": "goals", "but": "goals", "passes": "assists", "passe": "assists",
                "aides": "assists", "pts": "points", "point": "points", "differentiel": "plusMinus",
                "+/-": "plusMinus", "temps de glace": "toi", "mises en jeu": "faceoffLeaders",
                "pim": "penaltyMins", "penalites": "penaltyMins", "penalite": "penaltyMins",
                "minutes de penalite": "penaltyMins", "penaltyminutes": "penaltyMins",
                "tirs": "shots", "tir": "shots", "lances": "shots", "lancers": "shots",
                "buts gagnants": "gameWinningGoals", "gwg": "gameWinningGoals"}


@mcp.tool(output_schema=None)
@_outil_texte
async def meneurs(categorie: str = "points", limite: int = 5, equipe: str | None = None) -> dict:
    """Meneurs cette saison, de toute la LNH ou d'UNE équipe. Pour « les meneurs du Canadien »,
    passe equipe="MTL" (Canadiens, CH, Habs... acceptés). Catégories : points, goals (buts),
    assists (passes), plusMinus, goalsPp, goalsSh, toi, faceoffLeaders, penaltyMins
    (PIM, minutes de pénalité), shots (tirs, lancers), gameWinningGoals (buts gagnants).
    Le champ « reponse » contient la réponse prête à dire."""
    cat = _MENEURS_SYN.get(_norm(categorie or "points"), categorie or "points")
    if cat not in _MENEURS_CLUB:
        return {"reponse": f"Catégorie inconnue : « {categorie} ». Choix : points, buts, passes, "
                           "plus-moins, buts en avantage numérique, buts en désavantage numérique, "
                           "temps de glace, mises en jeu, minutes de pénalité, tirs, buts gagnants."}
    champ, libelle = _MENEURS_CLUB[cat]
    limite = max(1, min(int(limite or 5), 20))

    if equipe:
        eq = _equipe(equipe)
        club = await _get(f"{API}/club-stats/{eq}/now")
        joueurs = [p for p in club.get("skaters", []) if (p.get("gamesPlayed") or 0) > 0]
        if cat == "faceoffLeaders":  # sans minimum, un joueur à 1 mise en jeu sur 1 serait premier
            joueurs = [p for p in joueurs if p.get("positionCode") == "C"]
        joueurs.sort(key=lambda p: p.get(champ) or 0, reverse=True)
        lignes = []
        for p in joueurs[:limite]:
            v = p.get(champ)
            if cat == "toi":
                v = _mmss(v)
            elif cat == "faceoffLeaders" and v is not None:
                v = f"{round(100 * v, 1)} %"
            lignes.append({"joueur": f'{_txt(p.get("firstName"))} {_txt(p.get("lastName"))}', "valeur": v})
        portee = f"Meneurs des {eq}"
    elif cat in _MENEURS_STATS:
        cle = _MENEURS_STATS[cat]
        d = await _get(f"{STATS}/skater/summary", {
            "isAggregate": "false", "isGame": "false", "start": 0, "limit": limite,
            "sort": json.dumps([{"property": cle, "direction": "DESC"}]),
            "cayenneExp": f"seasonId={_saison_lnh()} and gameTypeId=2"})
        lignes = [{"joueur": r.get("skaterFullName"),
                   "equipe": (r.get("teamAbbrevs") or "").split(",")[-1].strip() or None,
                   "valeur": r.get(cle)} for r in d.get("data", [])]
        portee = "Meneurs de la LNH"
    else:
        d = await _get(f"{API}/skater-stats-leaders/current", {"categories": cat, "limit": limite})
        lignes = [{"joueur": f'{_txt(p.get("firstName"))} {_txt(p.get("lastName"))}',
                   "equipe": p.get("teamAbbrev"), "valeur": p.get("value")} for p in d.get(cat, [])]
        portee = "Meneurs de la LNH"

    if not lignes:
        return {"reponse": f"{portee} : aucune donnée pour l'instant."}
    morceaux = [f'{l["joueur"]}' + (f' ({l["equipe"]})' if l.get("equipe") else "") + f' {l["valeur"]}'
                for l in lignes]
    return {"reponse": f"{portee} en {libelle} : " + ", ".join(morceaux) + ".",
            "categorie": cat, "meneurs": lignes}


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
            "buts_marques": a.get("powerPlayGoalsFor", 0),
            "occasions": a.get("ppOpportunities", 0),
            "pct": round((a.get("powerPlayPct") or 0) * 100, 1),
            "rang": _rang(pp, "powerPlayPct", tid),
        },
        "desavantage_numerique": {
            # Champs explicites : un petit modèle lisait « 43/52 » comme 43 buts alloués.
            "punitions_tuees": d.get("timesShorthanded", 0) - d.get("ppGoalsAgainst", 0),
            "fois_en_desavantage": d.get("timesShorthanded", 0),
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
                "avantage_numerique_buts_sur_occasions": an.get("homeValue" if dom else "awayValue"),
                "desavantage_numerique_buts_alloues_sur_occasions": an.get("awayValue" if dom else "homeValue"),
            }
    except Exception as e:  # le dernier match est un bonus, pas bloquant
        out["dernier_match"] = f"indisponible ({e.__class__.__name__})"
    return out


# ---------- Jalons (milestones) ----------

def _alleger(x, max_liste: int = 5, profondeur: int = 0):
    """Réduit une réponse JSON brute : listes coupées, champs vides et liens retirés.
    Utile pour les endpoints LNH peu documentés (EDGE, jalons) dont les clés changent."""
    if profondeur > 6:
        return None
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if v in (None, "", [], {}) or any(m in k.lower() for m in ("url", "logo", "headshot", "image")):
                continue
            v = _alleger(v, max_liste, profondeur + 1)
            if v not in (None, "", [], {}):
                out[k] = _txt(v) if isinstance(v, dict) and set(v) <= {"default", "fr", "cs", "de", "es", "fi", "sk", "sv"} else v
        return out
    if isinstance(x, list):
        return [_alleger(v, max_liste, profondeur + 1) for v in x[:max_liste]]
    return x


async def _id_equipe(code: str) -> int | None:
    equipes = (await _get(f"{STATS}/team")).get("data", [])
    return next((t["id"] for t in equipes if t.get("triCode") == code), None)


@mcp.tool
async def jalons(equipe: str | None = None, joueur: str | None = None,
                 gardiens: bool = False, limite: int = 15) -> list[dict]:
    """Jalons à venir (milestones) : joueurs qui approchent d'un chiffre rond en carrière
    (ex. 1000e match, 500e but, 1000e point, 300e victoire d'un gardien).
    equipe : abréviation ou nom (MTL, Canadiens, CH...). joueur : nom (filtre).
    gardiens=True pour les jalons des gardiens."""
    url = f"{STATS}/milestones/{'goalies' if gardiens else 'skaters'}"
    rows = (await _get(url)).get("data", [])
    if equipe:
        code = _equipe(equipe)
        tid = await _id_equipe(code)
        rows = [r for r in rows if code in [str(v) for v in r.values()]
                or tid in (r.get("currentTeamId"), r.get("teamId"))]
    if joueur:
        nom = _norm(joueur)
        rows = [r for r in rows if any(nom in _norm(str(v)) for v in r.values() if isinstance(v, str))]
    return [_alleger(r) for r in rows[:limite]]


# ---------- NHL EDGE (suivi des joueurs et de la rondelle) ----------

MOTS_EDGE = ("shot", "speed", "skat", "distance", "zone", "burst", "percentile", "rank", "leag")


def _aplatir(x, prefixe: str = "", out: dict | None = None) -> dict:
    """{"topShotSpeed": {"metric": 157.9}} -> {"topShotSpeed.metric": 157.9}.
    Ne garde que les nombres (la prose et les listes de détails font exploser le contexte
    des petits modèles : 8k tokens pour l'assistant HA)."""
    out = {} if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            if k.lower() in ("imperial", "mph") or "url" in k.lower():
                continue  # on garde le métrique seulement
            _aplatir(v, f"{prefixe}.{k}" if prefixe else k, out)
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        out[prefixe] = round(x, 3) if isinstance(x, float) else x
    return out


def _edge_compact(d: dict, max_champs: int = 30) -> dict:
    plat = _aplatir(d)
    utiles = {k: v for k, v in plat.items()
              if any(m in k.lower() for m in MOTS_EDGE) and not k.lower().endswith(("id", "season", "gametype"))}
    return dict(list(utiles.items())[:max_champs])


@mcp.tool
async def edge_joueur(nom: str) -> dict:
    """Stats NHL EDGE d'un patineur cette saison : tir le plus puissant (km/h et mi/h),
    vitesse de patin maximale, pointes de vitesse, distance parcourue, temps par zone,
    avec rang/percentile LNH. Accepte un nom ou un ID."""
    j = await _trouver_joueur(nom)
    d = await _get(f"{API}/edge/skater-detail/{j['id']}/now")
    return {"joueur": j.get("nom"), "equipe": j.get("equipe"), "source": "NHL EDGE",
            "unites": "vitesses en km/h, distances en km", "stats": _edge_compact(d)}


@mcp.tool
async def edge_meneurs() -> dict:
    """Meneurs NHL EDGE de la ligue cette saison : tir le plus puissant, patineur le plus
    rapide, plus longue distance parcourue, etc."""
    d = await _get(f"{API}/edge/skater-landing/now")
    meneurs = {}
    for cat, v in (d.get("leaders") or d).items():
        if not isinstance(v, dict):
            continue
        p = v.get("player") or {}
        nom = f'{_txt(p.get("firstName"))} {_txt(p.get("lastName"))}'.strip()
        meneurs[cat] = {"joueur": nom or None, "equipe": _txt(p.get("team", {}).get("abbrev")) if isinstance(p.get("team"), dict) else p.get("teamAbbrev"),
                        **_edge_compact({k: x for k, x in v.items() if k != "player"}, 4)}
    return {"source": "NHL EDGE", "unites": "vitesses en km/h, distances en km", "meneurs": meneurs}


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


@mcp.tool(output_schema=None)
@_outil_texte
async def xg_joueur(nom: str, saison: int | None = None) -> dict:
    """Expected goals d'un joueur (MoneyPuck), toutes situations confondues : buts, xG, buts moins xG,
    xG/60, xG% sur glace, plus le détail 5 contre 5 et avantage numérique. saison : année de début (2026 = 2026-27)."""
    j = await _trouver_joueur(nom)
    lignes = {r.get("situation"): r for r in await _mp("skaters", saison) if str(r.get("playerId")) == str(j["id"])}
    if "all" not in lignes:
        return {"reponse": f"Pas de données MoneyPuck pour {j.get('nom', nom)}."}
    t = _ligne_xg(lignes["all"])
    detail = {}
    for sit, nom_sit in (("5on5", "5c5"), ("5on4", "avantage_numerique")):
        if sit in lignes:
            l = _ligne_xg(lignes[sit])
            detail[nom_sit] = {"buts": l["buts"], "xg": l["xg"]}
    reponse = (f'{t["joueur"]} ({t["equipe"]}), toutes situations : {t["buts"]} buts pour {t["xg"]} buts attendus '
               f'({t["buts_moins_xg"]:+}) en {t["pj"]} matchs.')
    return {"reponse": reponse, **t, "detail": detail, "source": "MoneyPuck.com"}


@mcp.tool(output_schema=None)
@_outil_texte
async def meneurs_xg(tri: str = "xg", equipe: str | None = None,
                     min_pj: int = 5, limite: int = 8) -> dict:
    """Classement des patineurs selon MoneyPuck, toutes situations confondues. tri : xg, xg_par_60,
    buts_moins_xg, xg_pct_sur_glace, game_score. Filtre optionnel par équipe (MTL, Canadiens, Leafs...)."""
    situation = "all"
    equipe = _equipe(equipe) if equipe else None
    lignes = [
        _ligne_xg(r) for r in await _mp("skaters")
        if r.get("situation") == situation
        and _f(r, "games_played") >= min_pj
        and (not equipe or _code_mp(r.get("team")) == equipe)
    ]
    lignes.sort(key=lambda x: x.get(tri) or 0, reverse=True)
    garder = ("joueur", "equipe", "pj", "buts", "xg", "buts_moins_xg", "xg_par_60", "xg_pct_sur_glace", "game_score")
    lignes = [{k: l[k] for k in garder} for l in lignes[:limite]]  # compact : contexte 8k de l'assistant HA
    reponse = ", ".join(f'{i}. {l["joueur"]} ({l["equipe"]}) {l.get(tri)}' for i, l in enumerate(lignes[:5], 1))
    return {"tri": tri, "reponse": f"Meneurs ({tri}) : {reponse}." if lignes else "Aucun joueur trouvé.",
            "joueurs": lignes}


@mcp.tool(output_schema=None)
@_outil_texte
async def xg_equipes(equipe: str | None = None, cinq_contre_cinq: bool = False, limite: int = 10) -> dict:
    """Expected goals par ÉQUIPE (MoneyPuck), triés par xG%, toutes situations : xGF, xGA, buts vs attendus.
    « Quelle équipe a le meilleur xG% ? » -> cet outil. equipe : filtre optionnel (MTL, CH...).
    cinq_contre_cinq : true seulement si l'utilisateur dit « 5 contre 5 »."""
    situation = "5on5" if cinq_contre_cinq else "all"
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
    for i, r in enumerate(out, 1):
        r["rang"] = i
    if equipe:
        code = _equipe(equipe)
        out = [r for r in out if _code_mp(r["equipe"]) == code]
        reponse = (f'{code} : xG% de {out[0]["xg_pct"]} % ({situation}), {out[0]["rang"]}e dans la LNH.'
                   if out else f"Pas de données pour {code}.")
    else:
        reponse = "Meilleurs xG% (" + situation + ") : " + ", ".join(
            f'{r["rang"]}. {r["equipe"]} {r["xg_pct"]} %' for r in out[:5]) + "."
    return {"situation": situation, "reponse": reponse, "equipes": out[:limite]}


@mcp.tool(output_schema=None)
@_outil_texte
async def gsax_gardiens(min_pj: int = 3, limite: int = 10) -> dict:
    """Gardiens classés par buts sauvés au-dessus des attentes (GSAx, MoneyPuck).
    Toutes situations. GSAx positif = le gardien arrête plus que la qualité des tirs le prédit.
    « Meilleur gardien », « qui a le meilleur GSAx » -> cet outil."""
    situation = "all"
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
    out = out[:limite]
    reponse = ("Meilleurs GSAx : " + ", ".join(
        f'{i}. {g["gardien"]} ({g["equipe"]}) {g["gsax"]:+}' for i, g in enumerate(out[:5], 1)) + ".") if out else "Aucun gardien."
    return {"reponse": reponse, "gardiens": out}


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


# ---------- Carrière d'un joueur (âge, bio, saison par saison) ----------

def _saison_txt(s) -> str:
    s = str(s)
    return f"{s[:4]}-{s[6:8]}" if len(s) == 8 else s


def _saison_voulue(saison: str) -> str | None:
    """« 2018 », « 2018-19 », « 2018-2019 », « 20182019 » -> « 20182019 » (2018 = saison 2018-19)."""
    brut = str(saison or "").strip()
    if re.fullmatch(r"\d{8}", brut):
        return brut
    m = re.search(r"\d{4}", brut)
    if not m:
        return None
    a = int(m.group())
    return f"{a}{a + 1}"


HOF_URL = "https://records.nhl.com/site/api/hof/players"
HOF_TTL = 86400
_hof_diag = {"etat": "jamais lu"}


async def _temple() -> dict:
    """Temple de la renommée du hockey (records.nhl.com) : {"ids": {playerId: date}, "noms": [(nom normalisé, date)]}.
    Beaucoup de lignes n'ont pas de playerId : on garde aussi le nom (miscFullName) pour les retrouver."""
    hit = _cache.get(HOF_URL)
    if hit and time.time() - hit[0] < HOF_TTL:
        return hit[1]
    try:
        r = await _client.get(HOF_URL, follow_redirects=True)
        r.raise_for_status()
        corps = r.json()
        lignes = corps.get("data", []) if isinstance(corps, dict) else corps
    except (httpx.HTTPError, ValueError) as e:
        _hof_diag.update(etat=f"erreur : {type(e).__name__} {str(e)[:150]}")
        return {"ids": {}, "noms": []}
    _hof_diag.update(etat=f"{len(lignes)} lignes")
    ids, noms = {}, []
    for l in lignes:
        date = str(l.get("dateInducted") or "")
        if l.get("playerId"):
            ids[int(l["playerId"])] = date
        nom = _norm(re.sub(r'"[^"]*"', " ", str(l.get("miscFullName") or "")))
        if nom:
            noms.append((nom, date))
    hof = {"ids": ids, "noms": noms}
    _cache[HOF_URL] = (time.time(), hof)
    return hof


def _date_temple(hof: dict, pid: int, prenom: str, nom: str) -> str | None:
    if pid in hof["ids"]:
        return hof["ids"][pid]
    p, n = _norm(prenom), _norm(nom)
    for complet, date in hof["noms"]:
        mots = complet.split()
        if n and n in mots and p and (p in mots or any(m.startswith(p[:3]) for m in mots)):
            return date
    return None


@mcp.tool(output_schema=None)
@_outil_texte
async def carriere_joueur(nom: str, saison: str = "") -> dict:
    """Âge, date et lieu de naissance, taille, poids, repêchage et stats de CARRIÈRE d'un joueur,
    avec ses stats saison par saison dans la LNH (toutes les années) et ses TROPHÉES (Hart, Art Ross,
    Vézina, Calder, Conn Smythe...) de toutes les saisons, et s'il est au TEMPLE DE LA RENOMMÉE (HOF) avec
    l'année d'intronisation. « Quel âge a X ? », « quels trophées a gagné X ? », « X est-il au Temple ? »,
    « combien de buts X a-t-il en carrière ? », « ses stats en 2018-19 ? » -> cet outil.
    saison : optionnelle (« 2018-19 », « 2018 ») pour une seule saison passée."""
    j = await _trouver_joueur(nom)
    d = await _get(f"{API}/player/{j['id']}/landing")
    qui = f'{_txt(d.get("firstName"))} {_txt(d.get("lastName"))}'
    gardien = d.get("position") == "G"

    age = None
    if d.get("birthDate"):
        n = datetime.strptime(d["birthDate"], "%Y-%m-%d").date()
        auj = datetime.now(TZ).date()
        age = auj.year - n.year - ((auj.month, auj.day) < (n.month, n.day))
    lieu = ", ".join(x for x in (_txt(d.get("birthCity")), _txt(d.get("birthStateProvince")),
                                  d.get("birthCountry")) if x)
    dr = d.get("draftDetails") or {}
    repechage = (f'{dr.get("year")}, {dr.get("teamAbbrev")}, {dr.get("overallPick")}e au total'
                 if dr.get("year") else "non repêché")

    def ligne(s):
        if gardien:
            return (f'{_saison_txt(s.get("season"))} {_txt(s.get("teamName"))}: '
                    f'{s.get("gamesPlayed", 0)} PJ, {s.get("wins", 0)} V, '
                    f'{(s.get("savePctg") or 0):.3f} %arr, {(s.get("goalsAgainstAvg") or 0):.2f} MBA')
        return (f'{_saison_txt(s.get("season"))} {_txt(s.get("teamName"))}: {s.get("gamesPlayed", 0)} PJ, '
                f'{s.get("goals", 0)} B, {s.get("assists", 0)} A, {s.get("points", 0)} PTS')

    lnh = [s for s in d.get("seasonTotals", []) if s.get("leagueAbbrev") == "NHL" and s.get("gameTypeId") == 2]

    # Trophées : [{"trophy": {"default": "Art Ross Trophy", "fr": "Trophée Art Ross"}, "seasons": [{"seasonId": 20182019}]}]
    trophees = []  # (saison, nom)
    for a in d.get("awards", []) or []:
        nom_t = _txt(a.get("trophy"))
        for se in a.get("seasons", []) or []:
            trophees.append((str(se.get("seasonId")), nom_t))
    trophees.sort()

    def txt_trophees(liste):
        par_nom = {}
        for sa, nt in liste:
            par_nom.setdefault(nt, []).append(_saison_txt(sa))
        return " ; ".join(f"{nt} ({', '.join(sa)})" for nt, sa in par_nom.items())
    carriere = ((d.get("careerTotals") or {}).get("regularSeason") or {})

    date_hof = _date_temple(await _temple(), int(j["id"]), _txt(d.get("firstName")), _txt(d.get("lastName")))
    if date_hof:
        temple = f"Intronisé au Temple de la renommée du hockey en {date_hof[:4]}."
    elif d.get("inHHOF"):
        temple = "Membre du Temple de la renommée du hockey."
    else:
        temple = "Pas (encore) au Temple de la renommée du hockey."

    voulue = _saison_voulue(saison) if saison else None
    if voulue:
        lignes = [s for s in lnh if str(s.get("season")) == voulue]
        if lignes:
            reponse = f"{qui}, " + " ; ".join(ligne(s) for s in lignes) + "."
            cette = [t for t in trophees if t[0] == voulue]
            if cette:
                reponse += " Trophées cette saison-là : " + txt_trophees(cette) + "."
        else:
            reponse = f"{qui} n'a pas joué dans la LNH en {_saison_txt(voulue)}."
        return {"reponse": reponse}

    if gardien:
        tot = (f'{carriere.get("gamesPlayed", 0)} matchs, {carriere.get("wins", 0)} victoires, '
               f'{carriere.get("shutouts", 0)} blanchissages')
    else:
        tot = (f'{carriere.get("goals", 0)} buts, {carriere.get("assists", 0)} passes, '
               f'{carriere.get("points", 0)} points en {carriere.get("gamesPlayed", 0)} matchs')
    reponse = (f"{qui}" + (f" a {age} ans (né le {d['birthDate']}" + (f" à {lieu}" if lieu else "") + ")" if age is not None else "")
               + f". En carrière dans la LNH : {tot}."
               + (f" Trophées : {txt_trophees(trophees)}." if trophees else "")
               + f" {temple}")
    return {
        "reponse": reponse,
        "temple_renommee": temple,
        "equipe": d.get("currentTeamAbbrev"),
        "position": d.get("position"),
        "taille_cm": d.get("heightInCentimeters"),
        "poids_kg": d.get("weightInKilograms"),
        "repechage": repechage,
        "trophees": [f"{_saison_txt(sa)} {nt}" for sa, nt in trophees],
        "saisons_lnh": [ligne(s) for s in lnh],
    }


# ---------- Recherche inversée : joueurs nés un jour donné ----------

MOIS = {"janvier": 1, "jan": 1, "january": 1, "fevrier": 2, "fev": 2, "february": 2, "feb": 2, "mars": 3,
        "march": 3, "mar": 3, "avril": 4, "april": 4, "avr": 4, "apr": 4, "mai": 5, "may": 5, "juin": 6,
        "june": 6, "jun": 6, "juillet": 7, "july": 7, "juil": 7, "jul": 7, "aout": 8, "august": 8,
        "aug": 8, "septembre": 9, "september": 9, "sept": 9, "sep": 9, "octobre": 10, "october": 10,
        "oct": 10, "novembre": 11, "november": 11, "nov": 11, "decembre": 12, "december": 12, "dec": 12}
BIOS_TTL = 86400  # les bios changent rarement : cache d'une journée
_tache_bios = None


def _jour_mois(date: str) -> tuple[int, int, int | None]:
    """« 12 juin », « 12 juin 1993 », « 12/06 », « 1993-06-12 », « June 12 », « aujourd'hui » -> (jour, mois, année|None)."""
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(date or ""))
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    n = _norm(str(date or ""))
    if n in DATES_RELATIVES:
        d = datetime.now(TZ).date() + timedelta(days=DATES_RELATIVES[n])
        return d.day, d.month, None
    m = None
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    annee = re.search(r"\b(1[89]\d\d|20\d\d)\b", n)
    annee = int(annee.group()) if annee else None
    sans_annee = re.sub(r"\b(1[89]\d\d|20\d\d)\b", " ", n)
    for mot, mois in sorted(MOIS.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{mot}\b", sans_annee):
            j = re.search(r"\b(\d{1,2})(?:er|e)?\b", sans_annee)
            if j:
                return int(j.group(1)), mois, annee
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})\b", sans_annee)  # 12/06 = 12 juin (format québécois)
    if m:
        return int(m.group(1)), int(m.group(2)), annee
    raise ValueError(f"Date « {date} » non comprise. Exemple : « 12 juin » ou « 1993-06-12 ».")


async def _bios(saisons: str) -> list[dict]:
    """Bios LNH (date et lieu de naissance) des patineurs et gardiens. saisons = 'actifs' ou 'toutes'."""
    cle = f"bios:{saisons}"
    hit = _cache.get(cle)
    if hit and time.time() - hit[0] < BIOS_TTL:
        return hit[1]
    if saisons == "toutes":
        expr = f"seasonId>=19171918 and seasonId<={_saison_lnh()} and gameTypeId=2"
        agg = "true"
    else:
        expr = f"seasonId={_saison_lnh()} and gameTypeId=2"
        agg = "false"
    rows = []
    for genre, champ_nom in (("skater", "skaterFullName"), ("goalie", "goalieFullName")):
        r = await _client.get(f"{STATS}/{genre}/bios", follow_redirects=True, timeout=300,
                              params={"isAggregate": agg, "isGame": "false", "limit": -1, "cayenneExp": expr})
        r.raise_for_status()
        for x in r.json().get("data", []):
            rows.append({
                "nom": x.get(champ_nom) or x.get("fullName"),
                "naissance": (x.get("birthDate") or "")[:10],
                "lieu": ", ".join(v for v in (x.get("birthCity"), x.get("birthStateProvinceCode"),
                                              x.get("birthCountryCode")) if v),
                "equipe": x.get("currentTeamAbbrev") or x.get("teamAbbrevs") or "",
                "pos": x.get("positionCode") or ("G" if genre == "goalie" else ""),
                "pj": x.get("gamesPlayed"),
            })
    _cache[cle] = (time.time(), rows)
    return rows


@mcp.tool(output_schema=None)
@_outil_texte
async def nes_le(date: str, toutes_les_epoques: str = "non") -> dict:
    """RECHERCHE INVERSÉE : quels joueurs de la LNH sont nés un jour donné (anniversaires).
    « Quel joueur est né le 12 juin ? », « qui fête son anniversaire aujourd'hui ? », « joueurs nés le
    12 juin 1993 ». date : « 12 juin », « aujourd'hui », « 1993-06-12 »... Par défaut : joueurs de la
    saison en cours. toutes_les_epoques : « oui » pour chercher aussi parmi les anciens joueurs."""
    jour, mois, annee = _jour_mois(date)
    epoque = "toutes" if _norm(str(toutes_les_epoques)) in ("oui", "yes", "true", "1", "toutes") else "actifs"
    if epoque == "toutes" and not (_cache.get("bios:toutes") and time.time() - _cache["bios:toutes"][0] < BIOS_TTL):
        # Toute l'histoire = des milliers de joueurs, lent la première fois : on prépare la liste en
        # arrière-plan et on répond tout de suite si elle n'est pas prête (le client HA coupe vite).
        import asyncio
        global _tache_bios
        if _tache_bios is None or _tache_bios.done():
            _tache_bios = asyncio.create_task(_bios("toutes"))
        try:
            rows = await asyncio.wait_for(asyncio.shield(_tache_bios), 8)
        except asyncio.TimeoutError:
            return {"reponse": "Je prépare la liste de tous les joueurs de l'histoire de la LNH, ça prend "
                               "une minute la première fois. Réessaie dans un instant."}
    else:
        rows = await _bios(epoque)
    mmjj = f"-{mois:02d}-{jour:02d}"
    trouves = [r for r in rows if r["naissance"].endswith(mmjj) and (not annee or r["naissance"].startswith(str(annee)))]
    # Plus de matchs d'abord : les noms connus sortent en premier
    trouves.sort(key=lambda r: -(r.get("pj") or 0))
    noms_mois = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                 "septembre", "octobre", "novembre", "décembre"]
    quand = f"{jour} {noms_mois[mois]}" + (f" {annee}" if annee else "")
    portee = "dans l'histoire de la LNH" if epoque == "toutes" else "parmi les joueurs de cette saison"
    if not trouves:
        reponse = f"Aucun joueur né le {quand} {portee}."
    else:
        tete = ", ".join(f'{r["nom"]} ({r["equipe"] or "retraité"}, {r["naissance"][:4]})' for r in trouves[:3])
        reste = len(trouves) - 3
        reponse = (f"{len(trouves)} joueur(s) né(s) le {quand} {portee} : {tete}"
                   + (f", et {reste} autre(s)" if reste > 0 else "") + ".")
    # Détails courts : avec une longue liste, le modèle vocal récitait 15 noms en puces.
    return {"reponse": reponse,
            "autres": [f'{r["nom"]} ({r["naissance"][:4]})' for r in trouves[3:8]]}


# ---------- Tendances des joueurs d'une équipe (tirs, buts, passes, points) ----------
# Répond à « qui a le plus de chances de faire plus de 3 lancés ce soir ? » peu importe la
# formulation : le modèle peut passer la stat et le seuil en mots, en chiffres ou en jargon de paris.

STATS_TENDANCE = {
    "shots": ["tirs", "tir", "lances", "lance", "lancers", "lancer", "lances au but", "tirs au but",
              "sog", "shots", "shot", "shots on goal", "tir au but"],
    "goals": ["buts", "but", "goals", "goal", "marquer", "compter", "marqueur", "buteur", "score"],
    "assists": ["passes", "passe", "aides", "aide", "assists", "assist", "mentions d aide"],
    "points": ["points", "point", "pts", "pt"],
}
NOMS_STAT = {"shots": "tirs", "goals": "buts", "assists": "passes", "points": "points"}
SEUIL_DEFAUT = {"shots": 3, "goals": 1, "assists": 1, "points": 1}
CHIFFRES = {"zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
            "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "one": 1, "two": 2, "three": 3, "four": 4,
            "five": 5}


def _stat_tendance(stat: str | None) -> str:
    n = _norm(stat or "tirs")
    for cle, alias in STATS_TENDANCE.items():
        if n == cle or n in alias:
            return cle
    for cle, alias in STATS_TENDANCE.items():  # « plus de 3 lancés au but » passé comme stat
        if any(f" {a} " in f" {n} " for a in alias if len(a) > 2):
            return cle
    raise ValueError(f"Stat inconnue « {stat} ». Choix : tirs (lancés), buts, passes, points.")


def _seuil(seuil, stat: str) -> int:
    """Seuil en nombre minimal : « plus de 3 » / « over 2.5 » / 3.5 -> 4 ; « 3+ », « au moins 3 », 3 -> 3."""
    if seuil in (None, ""):
        return SEUIL_DEFAUT[stat]
    if isinstance(seuil, (int, float)):
        v = float(seuil)
        return int(v) + 1 if v != int(v) else int(v)
    brut = str(seuil).replace(",", ".")
    dec = re.search(r"\d+\.\d+", brut)
    if dec:                               # 2.5 -> 3 (« over 2.5 »)
        return int(float(dec.group())) + 1
    n = _norm(brut)
    for mot, val in CHIFFRES.items():
        n = re.sub(rf"\b{mot.strip()}\b", str(val), n)
    m = re.search(r"\d+", n)
    if not m:
        return SEUIL_DEFAUT[stat]
    v = int(m.group())
    strict = any(k in f" {n} " for k in (" plus de ", " over ", " more than ", ">", " superieur ", " au dessus "))
    return int(v) + 1 if strict and "=" not in n else int(v)


def _mmss(sec) -> str | None:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return None
    return f"{sec // 60}:{sec % 60:02d}"


GAMELOG_TTL = 900  # 15 min : une vingtaine d'appels par question, inutile de les refaire à chaque fois


async def _game_log(pid: int) -> list[dict]:
    url = f"{API}/player/{pid}/game-log/now"
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < GAMELOG_TTL:
        return hit[1]
    try:
        r = await _client.get(url, follow_redirects=True)
        r.raise_for_status()
        log = r.json().get("gameLog", [])
    except (httpx.HTTPError, ValueError):
        return []
    _cache[url] = (time.time(), log)
    return log


LIGUE = {"lnh", "nhl", "ligue", "toute la ligue", "tous", "toutes", "ce soir", "partout", "league", "all"}


def _val_stat(g: dict, cle: str) -> int:
    if cle == "points":
        return (g.get("points") if g.get("points") is not None else (g.get("goals") or 0) + (g.get("assists") or 0)) or 0
    return g.get(cle) or 0


async def _lignes_equipe(eq: str, cle: str, mini: int, n: int = 10, preselection: int | None = None) -> tuple[list, list]:
    """Taux de réussite récent de chaque patineur d'une équipe pour « cle >= mini ».
    preselection : ne garder que les N meilleurs à la moyenne de la saison (mode ligue, moins d'appels)."""
    import asyncio
    club = await _get(f"{API}/club-stats/{eq}/now")
    patineurs = [p for p in club.get("skaters", []) if (p.get("gamesPlayed") or 0) > 0]
    if preselection:
        champ = {"shots": "shots", "goals": "goals", "assists": "assists", "points": "points"}.get(cle, cle)
        patineurs.sort(key=lambda p: (p.get(champ) or 0) / max(p.get("gamesPlayed") or 1, 1), reverse=True)
        patineurs = patineurs[:preselection]
    logs = await asyncio.gather(*[_game_log(p["playerId"]) for p in patineurs])

    cal = await _get(f"{API}/club-schedule-season/{eq}/now")
    finis = [g for g in cal.get("games", []) if g.get("gameType") in (2, 3) and g.get("gameState") in ("OFF", "FINAL")]
    dernier_id = finis[-1]["id"] if finis else None
    try:
        blesses_eq = {_norm(r["joueur"] or "") for r in (await _blesses_mp())[0] if r["equipe"] == eq}
    except httpx.HTTPError:
        blesses_eq = set()

    lignes, absents = [], []
    for p, log in zip(patineurs, logs):
        nom = f'{_txt(p.get("firstName"))} {_txt(p.get("lastName"))}'.strip()
        recents = log[:n]  # l'API renvoie le plus récent d'abord
        if not recents:
            continue
        reussis = sum(1 for g in recents if _val_stat(g, cle) >= mini)
        saison_reussis = sum(1 for g in log if _val_stat(g, cle) >= mini)
        ligne = {
            "joueur": nom,
            "equipe": eq,
            "pos": p.get("positionCode"),
            "reussi_recent": f"{reussis}/{len(recents)}",
            "pct_recent": round(100 * reussis / len(recents)),
            "pct_saison": round(100 * saison_reussis / len(log)),
            "moyenne_recente": round(sum(_val_stat(g, cle) for g in recents) / len(recents), 2),
        }
        absent = _norm(nom) in blesses_eq or (dernier_id and recents[0].get("gameId") != dernier_id)
        (absents if absent else lignes).append(ligne)
    return lignes, absents


def _tri(l: dict):
    return (l["pct_recent"], l["moyenne_recente"], l["pct_saison"])


@mcp.tool(output_schema=None)
@_outil_texte
async def tendances_joueurs(equipe: str = "MTL", stat: str = "tirs", seuil: str = "") -> dict:
    """QUI a le plus de chances d'atteindre X tirs (lancés), buts, passes ou points ce soir :
    « qui va lancer le plus ? », « plus de 3 lancés ? », « over 2.5 tirs », « qui va marquer ? ».
    equipe : une équipe (MTL, Canadiens, CH...) ou « LNH » pour TOUTE LA LIGUE (tous les matchs de ce
    soir). Si l'utilisateur ne nomme pas d'équipe, ou dit « dans la ligue », « ce soir », mettre « LNH ».
    stat : tirs, buts, passes ou points. seuil : tel que dit (« plus de 3 », « 3+ », « over 2.5 »)."""
    import asyncio
    cle = _stat_tendance(stat)
    mini = _seuil(seuil if seuil not in (None, "") else (stat if re.search(r"\d|\b(un|deux|trois|quatre|cinq|six)\b", _norm(stat or "")) else None), cle)
    nom_stat = NOMS_STAT[cle]
    seuil_txt = f"{mini} {nom_stat if mini > 1 else nom_stat.rstrip('s')} ou plus"

    # ----- Toute la ligue : seulement les équipes qui jouent ce soir -----
    if not equipe or _norm(equipe) in LIGUE:
        data = await _get(f"{API}/score/{_date(None)}")
        matchs = [g for g in data.get("games", []) if g.get("gameState") in ("FUT", "PRE", "LIVE", "CRIT")]
        if not matchs:
            return {"reponse": "Aucun match de la LNH ce soir (ou ils sont tous terminés).", "seuil": seuil_txt}
        adv = {}
        for g in matchs:
            h, a = g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]
            adv[h], adv[a] = a, h
        sem = asyncio.Semaphore(6)

        async def une(eq):
            async with sem:
                try:
                    return await _lignes_equipe(eq, cle, mini, preselection=6)
                except httpx.HTTPError:
                    return [], []
        res = await asyncio.gather(*[une(eq) for eq in adv])
        lignes = sorted((l for r in res for l in r[0]), key=_tri, reverse=True)
        if not lignes:
            return {"reponse": "Pas assez de données pour les matchs de ce soir.", "seuil": seuil_txt}
        top = lignes[:3]
        morceaux = [f'{l["joueur"]} ({NOMS.get(l["equipe"], l["equipe"])} contre {adv[l["equipe"]]}, '
                    f'{l["reussi_recent"]} derniers matchs, moyenne {l["moyenne_recente"]:g})' for l in top]
        reponse = (f"Dans toute la LNH ce soir ({len(matchs)} matchs), {seuil_txt} : le plus probable est "
                   + morceaux[0] + (", puis " + " et ".join(morceaux[1:]) if len(morceaux) > 1 else "") + ".")
        return {
            "reponse": reponse,
            "seuil": seuil_txt,
            "top5": [f'{l["joueur"]} ({l["equipe"]}) {l["reussi_recent"]} (moy. {l["moyenne_recente"]:g})' for l in lignes[:5]],
            "fenetre": "10 derniers matchs, 6 meilleurs de chaque équipe qui joue ce soir",
        }

    # ----- Une équipe -----
    eq = _equipe(equipe)
    lignes, absents = await _lignes_equipe(eq, cle, mini)
    lignes.sort(key=_tri, reverse=True)

    g = await _match_equipe(eq, None)
    if g:
        adv_eq = g["homeTeam"]["abbrev"] if g["awayTeam"]["abbrev"] == eq else g["awayTeam"]["abbrev"]
        contexte = f"ce soir contre {adv_eq}"
    else:
        contexte = "ne joue pas ce soir (tendance pour son prochain match)"

    if lignes:
        top = lignes[:3]
        morceaux = [f'{l["joueur"]} ({l["reussi_recent"]} derniers matchs, moyenne {l["moyenne_recente"]:g})'
                    for l in top]
        reponse = (f"{NOMS.get(eq, eq)} {contexte}, {seuil_txt} : le plus probable est " + morceaux[0]
                   + (", puis " + " et ".join(morceaux[1:]) if len(morceaux) > 1 else "") + ".")
    else:
        reponse = f"Aucune donnée de match pour {eq} cette saison."
    if absents:
        reponse += " Absents du dernier match ou blessés, exclus : " + ", ".join(a["joueur"] for a in absents[:4]) + "."

    # Détails volontairement courts : un long JSON faisait échouer le modèle local (contexte 8k)
    return {
        "reponse": reponse,
        "seuil": seuil_txt,
        "top5": [f'{l["joueur"]} {l["reussi_recent"]} (moy. {l["moyenne_recente"]:g})' for l in lignes[:5]],
        "fenetre": "10 derniers matchs",
    }


# ---------- Santé (Uptime Kuma, Homepage) ----------

_DEMARRAGE = time.time()


@mcp.custom_route("/health", methods=["GET"])
async def sante(request):
    """Hors du chemin secret : dit seulement si le serveur tourne, sans rien exposer d'autre."""
    from starlette.responses import JSONResponse
    try:
        outils = await mcp.list_tools()
    except AttributeError:  # FastMCP 2.x
        outils = await mcp.get_tools()
    return JSONResponse({"statut": "ok", "outils": len(outils),
                         "en_ligne_depuis_h": round((time.time() - _DEMARRAGE) / 3600, 1)})


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
