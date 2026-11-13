"""Bot de surveillance de stock PS5 -> notification Discord.

Aucune dependance a installer : uniquement la bibliotheque standard Python.
Lancer :  python bot.py          (tourne en boucle)
          python bot.py --test   (envoie un message test sur Discord et fait un seul tour)
"""
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
CONFIG = DOSSIER / "config.json"

NAVIGATEUR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

EN_STOCK, RUPTURE, INCONNU = "EN STOCK", "rupture", "inconnu"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def telecharger(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": NAVIGATEUR,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return r.status, data.decode("utf-8", errors="replace")


# ---------- Sony (playstation.com) ----------
# On lit le bouton d'achat tel qu'il est affiche sur la page (le meme que tu vois) :
#   <span data-qa="mfeCtaMain#offer0#finalPrice">Non disponible à l'achat</span>  -> rupture
#   <span data-qa="mfeCtaMain#offer0#finalPrice">519,99 €</span>                  -> en stock
# Attention : le champ "availability" des donnees produit dit parfois InStock alors
# que le bouton est indisponible, donc on ne s'y fie PAS.
SONY_BOUTON = re.compile(r'data-qa="mfeCtaMain#offer0#finalPrice"[^>]*>([^<]*)<')
SONY_CODE = re.compile(r'data-flip-key="cta-descriptor-0-([A-Z_]+):')
SONY_PRIX = re.compile(r'"price":"([\d.]+)","priceCurrency":"EUR"')


def verifier_sony(produit):
    _, html = telecharger(produit["url"])
    m = SONY_BOUTON.search(html)
    if not m:
        return INCONNU, None, "bouton d'achat introuvable sur la page"
    texte = m.group(1).replace("&#x27;", "'").strip()
    code = SONY_CODE.search(html)
    code = code.group(1) if code else ""
    p = SONY_PRIX.search(html)
    prix = float(p.group(1)) if p else None
    if "disponible" in texte.lower() or code == "UNAVAILABLE":
        return RUPTURE, prix, texte
    if "€" in texte or re.search(r"\d", texte):
        return EN_STOCK, prix, f"bouton : {texte} ({code or 'achat possible'})"
    return INCONNU, prix, f"bouton inattendu : {texte!r} ({code})"


# ---------- Amazon ----------
def verifier_amazon(produit):
    status, html = telecharger(produit["url"])
    bas = html.lower()
    if status != 200 or "captcha" in bas or 'id="producttitle"' not in bas:
        return INCONNU, None, f"Amazon a bloque/refuse la page (HTTP {status})"
    prix = None
    m = re.search(r'class="a-price-whole">\s*([\d\s. ]+)', html)
    if m:
        try:
            prix = float(re.sub(r"[^\d]", "", m.group(1)))
        except ValueError:
            pass
    dispo_txt = ""
    m = re.search(r'<div id="availability"[^>]*>(.*?)</div>', html, re.S)
    if m:
        dispo_txt = re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()
    if "indisponible" in dispo_txt.lower() or 'id="add-to-cart-button"' not in html:
        return RUPTURE, prix, dispo_txt or "pas de bouton panier"
    prix_max = produit.get("prix_max")
    if prix_max and prix and prix > prix_max:
        return RUPTURE, prix, f"dispo mais trop cher ({prix:.0f} EUR > {prix_max} EUR, sans doute un revendeur)"
    return EN_STOCK, prix, dispo_txt or "bouton panier present"


VERIFS = {"sony": verifier_sony, "amazon": verifier_amazon}


# ---------- Discord ----------
def discord(webhook, contenu, embed=None):
    if not webhook.startswith("https://"):
        log("!! Webhook Discord non configure dans config.json")
        return
    payload = {"content": contenu}
    if embed:
        payload["embeds"] = [embed]
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "botps5 (1.0)"},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        log(f"!! Erreur Discord HTTP {e.code} : verifie l'URL du webhook")
    except Exception as e:
        log(f"!! Erreur Discord : {e}")


def alerte_stock(webhook, produit, prix, detail):
    prix_txt = f"{prix:.2f} EUR" if prix else "prix inconnu"
    discord(webhook, "@everyone 🚨 **PS5 EN STOCK !** 🚨", {
        "title": produit["nom"],
        "url": produit["url"],
        "description": f"**{prix_txt}** - {detail}\n\n👉 [Clique ici pour acheter]({produit['url']})",
        "color": 0x00C853,
    })


# ---------- Boucle ----------
def un_tour(cfg, etat):
    webhook = cfg["discord_webhook"]
    rappel = cfg.get("rappel_minutes_si_toujours_en_stock", 30) * 60
    for p in cfg["produits"]:
        cle = p["url"]
        try:
            statut, prix, detail = VERIFS[p["site"]](p)
        except Exception as e:
            statut, prix, detail = INCONNU, None, f"erreur : {e}"
        prix_txt = f"{prix:.2f} EUR" if prix else "-"
        log(f"{statut:9} | {p['nom']} | {prix_txt} | {detail}")

        avant = etat.get(cle, {})
        if statut == EN_STOCK:
            deja = avant.get("statut") == EN_STOCK
            if not deja or time.time() - avant.get("alerte", 0) > rappel:
                alerte_stock(webhook, p, prix, detail)
                etat[cle] = {"statut": EN_STOCK, "alerte": time.time()}
        elif statut == RUPTURE:
            if avant.get("statut") == EN_STOCK:
                discord(webhook, f"❌ Plus de stock : {p['nom']}")
            etat[cle] = {"statut": RUPTURE}
        else:
            # Inconnu : on prévient une seule fois que la surveillance de ce produit ne marche pas
            if not avant.get("prevenu_inconnu"):
                discord(webhook, f"⚠️ Impossible de verifier **{p['nom']}** : {detail}")
            etat.setdefault(cle, {})["prevenu_inconnu"] = True
        time.sleep(random.uniform(2, 5))  # petite pause entre deux sites


def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    # Sur GitHub, le webhook est stocke dans un "secret" (variable d'environnement)
    if os.environ.get("DISCORD_WEBHOOK"):
        cfg["discord_webhook"] = os.environ["DISCORD_WEBHOOK"]
    etat = {}
    if "--une-fois" in sys.argv:
        # Mode GitHub Actions : un seul tour, l'etat est garde dans etat.json entre deux lancements
        fichier_etat = DOSSIER / "etat.json"
        if fichier_etat.exists():
            etat = json.loads(fichier_etat.read_text(encoding="utf-8"))
        un_tour(cfg, etat)
        fichier_etat.write_text(json.dumps(etat), encoding="utf-8")
        return
    if "--test" in sys.argv:
        discord(cfg["discord_webhook"], "✅ Test du bot PS5 : les notifications Discord fonctionnent !")
        un_tour(cfg, etat)
        return
    discord(cfg["discord_webhook"], "🤖 Bot PS5 demarre, je surveille le stock.")
    intervalle = cfg.get("intervalle_minutes", 3) * 60
    while True:
        un_tour(cfg, etat)
        attente = intervalle + random.uniform(-20, 20)
        log(f"Prochaine verification dans {attente / 60:.1f} min\n")
        time.sleep(max(60, attente))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Bot arrete.")
