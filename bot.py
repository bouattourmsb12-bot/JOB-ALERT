"""
Bot d'alertes emploi -> Telegram (Youssef)
Sources : APEC (sans inscription) + France Travail (optionnel, API officielle).
Envoie chaque NOUVELLE offre CDI junior finance/banque/data sur Telegram.
"""
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- RÉGLAGES
KEYWORDS = [
    "analyste crédit", "risque de crédit", "credit analyst", "analyste risques",
    "risk analyst", "analyste financier", "financial analyst", "FP&A",
    "contrôleur de gestion", "business controller", "finance business partner",
    "auditeur interne", "auditeur junior", "contrôle interne",
    "business analyst finance", "data analyst finance", "analyste reporting",
    "transformation finance", "chargé d'affaires entreprises",
    "analyste engagements", "analyste ALM", "analyste trésorerie",
    "analyste financement", "investment analyst",
]

# Mots qui excluent une offre (dans l'intitulé)
EXCLUDE = re.compile(
    r"senior|sénior|manager|lead\b|expert|confirmé|expérimenté|directeur|"
    r"responsable|head of|stage|stagiaire|intern|alternan|apprenti|cdd|v\.?i\.?e\b|freelance",
    re.IGNORECASE,
)

MAX_AGE_HOURS = 48          # n'envoie que les offres publiées il y a moins de 48 h
SEEN_FILE = Path(__file__).with_name("seen.json")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FT_CLIENT_ID = os.environ.get("FT_CLIENT_ID")          # optionnel
FT_CLIENT_SECRET = os.environ.get("FT_CLIENT_SECRET")  # optionnel

HEADERS = {"User-Agent": "Mozilla/5.0 (job-alert-bot perso)"}


# ---------------------------------------------------------------- OUTILS
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    # on garde les 5000 derniers identifiants pour limiter la taille du fichier
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-5000:], indent=0))


def is_recent(date_str: str) -> bool:
    if not date_str:
        return True
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - d < timedelta(hours=MAX_AGE_HOURS)
    except ValueError:
        return True


def clean(txt: str, n: int) -> str:
    """Enlève le HTML et coupe proprement à n caractères."""
    txt = re.sub(r"<[^>]+>", " ", txt or "")
    txt = re.sub(r"\s+", " ", html.unescape(txt)).strip()
    return txt if len(txt) <= n else txt[:n].rsplit(" ", 1)[0] + "…"


def enrich_apec(job: dict) -> None:
    """Va chercher la fiche complète APEC : salaire, résumé, profil, lien de candidature."""
    num = job["id"].replace("apec-", "")
    try:
        d = requests.get(
            "https://www.apec.fr/cms/webservices/offre/public",
            params={"numeroOffre": num}, headers=HEADERS, timeout=30,
        ).json()
        job["salaire"] = d.get("salaireTexte") or ""
        job["resume"] = clean(d.get("texteHtml"), 450) or job.get("resume", "")
        job["profil"] = clean(d.get("texteHtmlProfil"), 300)
        if d.get("adresseUrlCandidature"):
            job["postuler"] = d["adresseUrlCandidature"]
    except Exception as ex:
        print(f"[APEC détail] {num}: {ex}")
    time.sleep(1)


def send_telegram(job: dict) -> None:
    e = html.escape
    lines = [
        f"🆕 <b>{e(job['titre'])}</b>",
        f"🏢 {e(job['entreprise'])}",
        f"📍 {e(job['ville'])}",
        f"🎓 Expérience : {e(job['experience'])}",
    ]
    if job.get("salaire"):
        lines.append(f"💰 {e(job['salaire'])}")
    lines.append(f"🕒 {e(job['date'][:16].replace('T', ' '))}  ·  {e(job['source'])}")
    if job.get("resume"):
        lines.append(f"\n📝 <b>Le poste :</b> {e(job['resume'])}")
    if job.get("profil"):
        lines.append(f"\n👤 <b>Profil :</b> {e(job['profil'])}")
    lines.append(f"\n👉 <b><a href=\"{e(job.get('postuler') or job['lien'])}\">POSTULER</a></b>")
    if job.get("postuler") and job["postuler"] != job["lien"]:
        lines.append(f"📄 <a href=\"{e(job['lien'])}\">Voir l'offre complète</a>")

    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines)[:4000],
              "parse_mode": "HTML", "disable_web_page_preview": "true"},
        timeout=20,
    )
    if r.status_code == 429:  # trop de messages d'un coup : on patiente
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
        return send_telegram(job)
    if not r.ok:
        print(f"[Telegram] erreur {r.status_code}: {r.text[:200]}")
    time.sleep(1)


# ---------------------------------------------------------------- APEC
APEC_EXP = {  # codes internes APEC -> années d'expérience minimum
    "597150": "aucune", "597151": "1 an", "597152": "2 ans", "597153": "3 ans",
    "200269": "non précisée",
}


def fetch_apec() -> list:
    jobs = []
    for kw in KEYWORDS:
        try:
            r = requests.post(
                "https://www.apec.fr/cms/webservices/rechercheOffre",
                json={
                    "motsCles": kw,
                    "typesContrat": ["101888"],  # CDI
                    "sorts": [{"type": "DATE", "direction": "DESCENDING"}],
                    "pagination": {"range": 30, "startIndex": 0},
                    "activeFiltre": True,
                    "pointGeolocDeReference": {"distance": 0},
                },
                headers=HEADERS, timeout=30,
            )
            r.raise_for_status()
            for o in r.json().get("resultats", []):
                exp = str(o.get("niveauExperience"))
                if exp not in APEC_EXP:      # 4 ans et plus -> on ignore
                    continue
                jobs.append({
                    "id": "apec-" + o["numeroOffre"],
                    "titre": o.get("intitule", ""),
                    "entreprise": o.get("nomCommercial") or "Confidentiel",
                    "ville": o.get("lieuTexte", ""),
                    "experience": APEC_EXP[exp],
                    "date": o.get("datePublication", ""),
                    "resume": clean(o.get("texteOffre"), 450),
                    "source": "APEC",
                    "lien": "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/"
                            + o["numeroOffre"],
                })
        except Exception as ex:  # une source qui plante ne bloque pas le reste
            print(f"[APEC] {kw}: {ex}")
        time.sleep(2)
    return jobs


# ---------------------------------------------------------------- FRANCE TRAVAIL
def fetch_france_travail() -> list:
    if not (FT_CLIENT_ID and FT_CLIENT_SECRET):
        print("[France Travail] identifiants absents -> source ignorée")
        return []
    try:
        tok = requests.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token",
            params={"realm": "/partenaire"},
            data={"grant_type": "client_credentials", "client_id": FT_CLIENT_ID,
                  "client_secret": FT_CLIENT_SECRET, "scope": "api_offresdemploiv2 o2dsoffre"},
            timeout=30,
        ).json()["access_token"]
    except Exception as ex:
        print(f"[France Travail] token: {ex}")
        return []

    jobs = []
    for kw in KEYWORDS:
        try:
            r = requests.get(
                "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
                params={"motsCles": kw, "typeContrat": "CDI", "publieeDepuis": 1, "range": "0-49"},
                headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                timeout=30,
            )
            if r.status_code == 204:  # aucun résultat
                continue
            r.raise_for_status()
            for o in r.json().get("resultats", []):
                exp = o.get("experienceLibelle", "")
                m = re.search(r"(\d+)\s*An", exp)
                if m and int(m.group(1)) > 3:
                    continue
                jobs.append({
                    "id": "ft-" + o["id"],
                    "titre": o.get("intitule", ""),
                    "entreprise": (o.get("entreprise") or {}).get("nom", "Non communiqué"),
                    "ville": (o.get("lieuTravail") or {}).get("libelle", ""),
                    "experience": exp or "non précisée",
                    "date": o.get("dateCreation", ""),
                    "salaire": (o.get("salaire") or {}).get("libelle", ""),
                    "resume": clean(o.get("description"), 450),
                    "source": "France Travail",
                    "lien": (o.get("origineOffre") or {}).get("urlOrigine")
                            or f"https://candidat.francetravail.fr/offres/recherche/detail/{o['id']}",
                })
        except Exception as ex:
            print(f"[France Travail] {kw}: {ex}")
        time.sleep(0.5)
    return jobs


# ---------------------------------------------------------------- MAIN
def main() -> None:
    seen = load_seen()
    first_run = not seen
    jobs = fetch_apec() + fetch_france_travail()

    # dédoublonnage : même id, ou même (titre + entreprise) sur 2 sources
    new, keys = [], set()
    for j in jobs:
        key = (j["titre"].lower().strip(), j["entreprise"].lower().strip())
        if j["id"] in seen or key in keys:
            continue
        keys.add(key)
        seen.add(j["id"])
        if EXCLUDE.search(j["titre"]) or not is_recent(j["date"]):
            continue
        new.append(j)

    print(f"{len(jobs)} offres récupérées, {len(new)} nouvelles")
    if first_run:
        # 1er lancement : on n'inonde pas Telegram, on envoie juste un résumé
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID,
                  "text": f"✅ Bot activé. {len(new)} offres récentes mémorisées. "
                          f"Tu recevras désormais chaque nouvelle offre."},
            timeout=20,
        )
    else:
        for j in sorted(new, key=lambda x: x["date"]):
            if j["source"] == "APEC":
                enrich_apec(j)
            send_telegram(j)
    save_seen(seen)


if __name__ == "__main__":
    main()
