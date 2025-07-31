"""
SushiDL - Application de téléchargement de mangas depuis SushiScan.fr/net
Fonctionnalités principales :
- Contournement de la protection Cloudflare via les cookies cf_clearance
- Utilisation de FlareSolverr pour les protections anti-bot avancées
- Téléchargement multi-thread des images
- Conversion automatique WebP vers JPG
- Archivage CBZ des chapitres
- Interface graphique intuitive avec suivi de progression
"""

import os
import re
import html
import json
import shutil
import threading
import time
import datetime

import tkinter as tk
from tkinter import messagebox, ttk

from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil, log10
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image, ImageTk
from curl_cffi import requests
from zipfile import ZipFile


def robust_download_image(img_url, headers, max_try=4, delay=2):
    """
    Télécharge une image de manière robuste avec plusieurs tentatives.
    Contourne les protections Cloudflare et vérifie l'intégrité des images.
    
    Args:
        img_url (str): URL de l'image à télécharger
        headers (dict): En-têtes HTTP à utiliser
        max_try (int): Nombre maximum de tentatives
        delay (int): Délai initial entre les tentatives (augmente exponentiellement)
    
    Returns:
        bytes: Contenu brut de l'image
    
    Raises:
        Exception: Après échec de toutes les tentatives
    """
    last_exc = None
    for attempt in range(1, max_try + 1):
        try:
            # Essaye d'abord avec curl_cffi.requests si dispo (bypass cloudflare)
            try:
                import curl_cffi.requests as cffi_requests
                r = cffi_requests.get(
                    img_url,
                    headers=headers,
                    impersonate="chrome",
                    timeout=20
                )
                r.raise_for_status()
                raw = r.content
            except ImportError:
                import requests as cffi_requests
                r = cffi_requests.get(
                    img_url,
                    headers=headers,
                    timeout=20
                )
                r.raise_for_status()
                raw = r.content

            # Détection HTML (Cloudflare/captcha au lieu d'une image)
            if raw[:6] == b'<html>' or b'<html' in raw[:1024].lower():
                raise Exception("Réponse HTML (protection serveur ou Cloudflare)")

            # Vérifie si c'est bien une image (fail si corrompue/invalide)
            try:
                Image.open(BytesIO(raw))
            except Exception as test_e:
                print(f"[WARNING] Tentative {attempt}: reçu n'est pas une image reconnue: {test_e}")
                last_exc = test_e
                time.sleep(delay * attempt)
                continue

            # Succès - retourne les données brutes de l'image
            return raw

        except Exception as e:
            print(f"[WARNING] Tentative {attempt} échouée pour {img_url} : {e}")
            last_exc = e
            # Pause exponentielle si code 403/429 (trop de requêtes)
            if hasattr(e, 'response') and getattr(e.response, 'status_code', 200) in (403, 429):
                time.sleep(delay * attempt * 2)
            else:
                time.sleep(delay * attempt)
    raise Exception(f"Impossible de télécharger l'image {img_url} après {max_try} tentatives: {last_exc}")


def fetch_with_flaresolverr(url, flaresolverr_url):
    """
    Contourne les protections anti-bot via FlareSolverr.
    Exécute un navigateur headless pour récupérer le contenu après rendu JavaScript.
    
    Args:
        url (str): URL à récupérer
        flaresolverr_url (str): URL du service FlareSolverr
    
    Returns:
        str: Contenu HTML après exécution du JavaScript
    """
    print(f"[INFO] ⚡ Appel FlareSolverr à : {flaresolverr_url}")
    if hasattr(MangaApp, 'current_instance'):
        MangaApp.current_instance.log(f"⚡ Passage via FlareSolverr pour : {url}", level="info")
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
        "render": True  # 🔥 Force le rendu JS pour obtenir le DOM final avec <img>
    }
    try:
        response = requests.post(
            flaresolverr_url.rstrip("/") + "/v1",
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        solution = response.json()

        # Vérification basique du résultat
        html_result = solution.get("solution", {}).get("response")
        if not html_result or not html_result.strip():
            print("[FlareSolverr] ⚠️ Réponse vide après rendu.")
        else:
            print("[FlareSolverr] ✅ Contournement réussi, contenu récupéré.")
        return html_result

    except Exception as e:
        print(f"[FlareSolverr] ❌ Erreur : {e}")
        return ""


# Expressions régulières et constantes globales
REGEX_URL = r"^https://sushiscan\.(fr|net)/catalogue/[a-z0-9-]+/$"  # Format des URLs valides
ROOT_FOLDER = "DL SushiScan"  # Dossier racine pour les téléchargements
THREADS = 3  # Nombre de threads pour le téléchargement parallèle
COOKIE_CACHE_PATH = "cookie_cache.json"  # Fichier de cache pour les cookies


# --- Fonctions utilitaires ---
def sanitize_folder_name(name):
    """Nettoie les noms de dossier en supprimant les caractères invalids"""
    return re.sub(r'[<>:"/\\|?*\n\r]', "_", name).strip()


def make_request(url, cookie, ua):
    """Effectue une requête HTTP avec les cookies et l'user-agent appropriés"""
    headers = {
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Cookie": f"cf_clearance={cookie}",
        "User-Agent": ua,
        "Connection": "close",  # Important pour éviter les fuites de mémoire
    }
    return requests.get(url, headers=headers, impersonate="chrome", timeout=10)


def parse_lr(text, left, right, recursive, unescape=True):
    """
    Parse le texte entre deux délimiteurs (left et right)
    
    Args:
        text (str): Texte à parser
        left (str): Délimiteur gauche
        right (str): Délimiteur droit
        recursive (bool): Récupère toutes les occurrences si True
        unescape (bool): Décode les entités HTML si True
    
    Returns:
        str/list: Résultat du parsing selon le mode
    """
    pattern = re.escape(left) + "(.*?)" + re.escape(right)
    matches = re.findall(pattern, text)
    if unescape:
        matches = [html.unescape(match) for match in matches]
    return matches if recursive else matches[0] if matches else None


def test_cookie_validity(domain, cookie, ua):
    """
    Vérifie si un cookie cf_clearance est encore valide
    
    Args:
        domain (str): Domaine à tester (.fr ou .net)
        cookie (str): Valeur du cookie cf_clearance
        ua (str): User-Agent à utiliser
    
    Returns:
        bool: True si le cookie est valide, False sinon
    """
    test_url = f"https://sushiscan.{domain}/catalogue/one-piece/"  # Manga populaire garanti
    try:
        r = make_request(test_url, cookie, ua)
        if r.status_code == 200 and "entry-title" in r.text:
            return True
        else:
            return False
    except Exception as e:
        return False


def interpret_curl_error(message):
    """Traduit les erreurs cURL en messages compréhensibles"""
    if "curl: (6)" in message:
        return "Nom d'hôte introuvable (DNS)."
    elif "curl: (7)" in message:
        return "Connexion refusée ou impossible (serveur hors ligne ?)."
    elif "curl: (28)" in message:
        return "⏱️ Délai d'attente dépassé (timeout réseau)."
    elif "curl: (35)" in message:
        return "Erreur SSL/TLS lors de la connexion sécurisée."
    elif "curl: (56)" in message:
        return "Connexion interrompue (réponse incomplète ou terminée prématurément)."
    else:
        return None


def archive_cbz(folder_path, title, volume):
    """
    Crée une archive CBZ à partir d'un dossier d'images
    
    Args:
        folder_path (str): Chemin du dossier contenant les images
        title (str): Titre du manga
        volume (str): Numéro du volume/chapitre
    
    Returns:
        bool: True si l'archivage a réussi, False sinon
    """
    clean_title = sanitize_folder_name(title)
    clean_volume = sanitize_folder_name(volume)
    parent_dir = os.path.dirname(folder_path)
    cbz_name = os.path.join(parent_dir, f"{clean_title} - {clean_volume}.cbz")
    
    # Création de l'archive ZIP
    with ZipFile(cbz_name, "w") as cbz:
        for root, _, files in os.walk(folder_path):
            for file in sorted(files):  # Tri alphabétique pour l'ordre des pages
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, folder_path)
                cbz.write(full_path, arcname)
    
    # Vérification de l'intégrité de l'archive
    try:
        with ZipFile(cbz_name, "r") as test_zip:
            test_zip.testzip()
    except Exception:
        return False
    
    # Suppression du dossier original si l'archive est valide
    if os.path.exists(cbz_name) and os.path.getsize(cbz_name) > 10000:
        shutil.rmtree(folder_path)
        return True
    return False


def download_image(
    url, folder, cookie, ua, i, number_len, cancel_event, failed_downloads,
    progress_callback=None, referer_url=None, webp2jpg_enabled=False
):
    """
    Télécharge une image unique avec gestion d'erreurs et conversion optionnelle
    
    Args:
        url (str): URL de l'image
        folder (str): Dossier de destination
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
        i (int): Index de l'image (pour le nom de fichier)
        number_len (int): Longueur du padding numérique (ex: 003.jpg)
        cancel_event (threading.Event): Événement d'annulation
        failed_downloads (list): Liste des échecs à remplir
        progress_callback (func): Callback de progression
        referer_url (str): URL Referer à utiliser
        webp2jpg_enabled (bool): Activer la conversion WebP->JPG
    """
    import os
    if cancel_event.is_set():
        return

    # Configuration des en-têtes HTTP
    referer = referer_url or ("https://sushiscan.net/" if "sushiscan.net" in url else "https://sushiscan.fr/")
    headers = {
        "Accept": "image/webp,image/jpeg,image/png,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Cookie": f"cf_clearance={cookie}" if cookie else "",
        "User-Agent": ua,
        "Referer": referer,
    }
    if not cookie:
        headers.pop("Cookie")

    # Détermination de l'extension et du nom de fichier
    ext = url.split(".")[-1].split("?")[0]
    filename = os.path.join(folder, f"{str(i+1).zfill(number_len)}.{ext}")

    # Téléchargement robuste avec plusieurs tentatives
    try:
        raw = robust_download_image(url, headers)
        with open(filename, "wb") as f:
            f.write(raw)
        
        # Conversion WebP vers JPG si activée
        if webp2jpg_enabled and filename.lower().endswith('.webp'):
            try:
                img = Image.open(filename).convert('RGB')
                new_path = filename[:-5] + '.jpg'
                img.save(new_path, 'JPEG')
                os.remove(filename)
                filename = new_path
            except Exception as conv_e:
                print(f"[Erreur] Conversion WebP -> JPG : {conv_e}")
        
        # Mise à jour de la progression
        if progress_callback:
            progress_callback(i+1)
        if hasattr(MangaApp, "current_instance") and hasattr(MangaApp.current_instance, "log"):
            MangaApp.current_instance.log(f"🖼️ Image {i+1} téléchargée : {os.path.basename(filename)}", level="info")
        return
    
    except Exception as e:
        print(f"[Exception] Direct download failed after retries: {e}. Fallback FlareSolverr.")

    # Fallback via FlareSolverr en cas d'échec
    try:
        print("[INFO] Fallback FlareSolverr image : " + url)
        import requests
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": 90000,
            "render": True
        }
        resp = requests.post(MangaApp.flaresolverr_url_static.rstrip("/") + "/v1", json=payload, timeout=90)
        resp.raise_for_status()
        sol = resp.json()
        img_data = sol.get("solution", {}).get("response", None)
        
        if img_data:
            import base64
            try:
                # Tentative de décodage base64
                img_bytes = base64.b64decode(img_data)
                with open(filename, "wb") as f:
                    f.write(img_bytes)
                
                # Conversion si nécessaire
                if webp2jpg_enabled and filename.lower().endswith('.webp'):
                    try:
                        img = Image.open(filename).convert('RGB')
                        new_path = filename[:-5] + '.jpg'
                        img.save(new_path, 'JPEG')
                        os.remove(filename)
                        filename = new_path
                    except Exception as conv_e:
                        print(f"[Erreur] Conversion WebP -> JPG : {conv_e}")
                
                if progress_callback:
                    progress_callback(i+1)
                print("[INFO] Image téléchargée via FlareSolverr : " + url)
                return
            except Exception:
                # Fallback si pas en base64
                with open(filename, "wb") as f:
                    f.write(img_data.encode() if isinstance(img_data, str) else img_data)
                
                if webp2jpg_enabled and filename.lower().endswith('.webp'):
                    try:
                        img = Image.open(filename).convert('RGB')
                        new_path = filename[:-5] + '.jpg'
                        img.save(new_path, 'JPEG')
                        os.remove(filename)
                        filename = new_path
                    except Exception as conv_e:
                        print(f"[Erreur] Conversion WebP -> JPG : {conv_e}")
                
                if progress_callback:
                    progress_callback(i+1)
                print("[INFO] Image téléchargée via FlareSolverr (non base64): " + url)
                return
    except Exception as e:
        print(f"[FlareSolverr] Échec fallback pour {url} : {e}")

    # Échec définitif - ajout à la liste des échecs
    failed_downloads.append(url)
    print(f"[Erreur définitive] Impossible de télécharger l'image après fallback : {url}")


def fetch_manga_data(url, cookie, ua):
    """
    Récupère les données d'un manga : titre et liste des volumes/chapitres
    
    Args:
        url (str): URL de la page catalogue du manga
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
    
    Returns:
        tuple: (titre, liste de tuples (label, url))
    """
    r = make_request(url, cookie, ua)
    if r.status_code != 200:
        raise Exception("Accès refusé ou URL invalide")
    html_content = r.text

    # Extraction du titre
    title = html.unescape(
        parse_lr(
            html_content, '<h1 class="entry-title" itemprop="name">', "</h1>", False
        )
    )

    # Recherche des liens vers les volumes/chapitres
    matches = re.findall(
        r'<a href="(https://sushiscan\.(fr|net)/[^"]+)">\s*<span class="chapternum">(.*?)</span>',
        html_content,
    )
    print(f"[INFO] {len(matches)} volumes/chapitres détectés")

    # Élimination des doublons
    seen = set()
    pairs = []
    for url, _, label in matches:
        if url in seen:
            continue
        seen.add(url)
        pairs.append((label.strip(), url))

    pairs.reverse()  # Pour afficher dans l'ordre croissant
    return title, pairs


def get_images(link, cookie, ua, retries=2, delay=5, debug_mode=True):
    """
    Récupère la liste des URLs d'images pour un volume/chapitre
    
    Args:
        link (str): URL de la page du volume
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
        retries (int): Tentatives de récupération
        delay (int): Délai entre les tentatives
        debug_mode (bool): Activer le mode debug
    
    Returns:
        list: Liste des URLs d'images
    """
    def clean_parasites(images, domain):
        """Filtre les images parasites (logos, pubs) pour sushiscan.fr"""
        if domain != "fr":
            return images

        PARASITE_KEYWORDS = ["ads", "sponsor", "banner", "footer", "cover", "logo", "pub"]
        filtered = []
        for img in images:
            if any(keyword in img.lower() for keyword in PARASITE_KEYWORDS):
                continue
            if "sushiscan.fr/wp-content/uploads/" in img:
                continue
            filtered.append(img)

        removed = len(images) - len(filtered)
        if removed > 0:
            print(f"[INFO] 🔪 {removed} image(s) parasite(s) supprimée(s) dynamiquement (filtrage avancé)")       
        return filtered

    def extract_images(r_text, domain):
        """Extrait les URLs d'images depuis le contenu HTML"""
        # Étape 1 — Extraction depuis le JSON ts_reader.run
        json_str = parse_lr(r_text, "ts_reader.run(", ");</script>", False)
        if json_str:
            try:
                data = json.loads(json_str)
                images = [
                    img.replace("http://", "https://")
                    for img in data["sources"][0]["images"]
                ]
                if images:
                    print(f"[INFO] ✅ {len(images)} images détectées via ts_reader.run.")
                    images = clean_parasites(images, domain)
                    print(f"[INFO] ✅ {len(images)} images finales après filtrage.")
                    return images
            except Exception as e:
                print(f"[Erreur] Parsing JSON : {e}")

        # Étape 2 — Fallback : balises img dans #readerarea
        soup = BeautifulSoup(r_text, "html.parser")

        # Supprimer les divs inutiles pour .fr
        if domain == "fr":
            for div in soup.find_all("div", class_="bixbox"):
                div.decompose()

        reader = soup.find("div", id="readerarea")
        if reader:
            images = []
            for img in reader.find_all("img"):
                src = img.get("data-src") or img.get("src")
                if not src:
                    continue
                if src.startswith("data:"):
                    continue
                if src.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
                    images.append(src)
            if images:
                images = clean_parasites(images, domain)
                print(f"[INFO] ✅ {len(images)} images finales après filtrage.")
                return images

        # Étape 3 — Fallback regex brut
        img_urls = re.findall(
            r'<img[^>]+(?:src|data-src)=["\'](https://[^"\'>]+\.(?:webp|jpg|jpeg|jpe|png|avif))["\']',
            r_text,
            re.IGNORECASE,
        )
        img_urls = [url for url in img_urls if not url.startswith("data:")]
        img_urls = list(dict.fromkeys(img_urls))  # Supprime les doublons
        if img_urls:
            img_urls = clean_parasites(img_urls, domain)
            print(f"[INFO] ✅ {len(img_urls)} images finales après filtrage.")
        return img_urls

    # --- Phase 1 : tentative directe sans FlareSolverr ---
    try:
        time.sleep(1)
        r = make_request(link, cookie, ua)
        print(f"[INFO] 📡 Requête HTTP directe reçue (len={len(r.text)})")
        domain = "fr" if "sushiscan.fr" in link else "net"

        # Sauvegarde debug si activé
        if debug_mode:
            debug_file = f"debug_sushiscan_{domain}.log"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(r.text)
            print(f"[DEBUG] 📁 Fichier {debug_file} généré.")

        images = extract_images(r.text, domain)
        if images:
            return images
        else:
            print("[INFO] ⚠️ Aucune image trouvée en direct, tentative via FlareSolverr...")
    except Exception as e:
        message = str(e)
        interpretation = interpret_curl_error(message)
        if interpretation:
            print(f"[WARNING] {interpretation} Passage via FlareSolverr...")
            if hasattr(MangaApp, 'current_instance'):
                MangaApp.current_instance.log(interpretation, level="warning")
        else:
            print(f"[WARNING] Erreur directe : {message} → tentative via FlareSolverr...")
            if hasattr(MangaApp, 'current_instance'):
                MangaApp.current_instance.log(f"Erreur directe : {message}", level="warning")

    # --- Phase 2 : Fallback via FlareSolverr ---
    try:
        r_text = fetch_with_flaresolverr(link, MangaApp.flaresolverr_url_static)
        domain = "fr" if "sushiscan.fr" in link else "net"

        # Sauvegarde debug si activé
        if debug_mode:
            debug_file = f"debug_sushiscan_{domain}_flaresolverr.log"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(r_text)
            print(f"[DEBUG] 📁 Fichier {debug_file} généré.")

        # Récupération de l'image de couverture
        get_cover_image(r_text)

        images = extract_images(r_text, domain)
        if images:
            return images
        else:
            print("[WARNING] Aucun JSON ni image HTML même après FlareSolverr.")
    except Exception as e:
        print(f"[FlareSolverr] ❌ Erreur lors du fallback : {e}")

    print(f"[Échec définitif] ❌ Impossible d’extraire des images depuis : {link}")
    return []


def download_volume(
    volume,
    images,
    title,
    cookie,
    ua,
    logger,
    cancel_event,
    cbz_enabled=True,
    update_progress=None,
    webp2jpg_enabled=True
):
    """
    Télécharge un volume complet avec gestion de progression et archivage
    
    Args:
        volume (str): Nom du volume/chapitre
        images (list): URLs des images
        title (str): Titre du manga
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
        logger (func): Fonction de log
        cancel_event (threading.Event): Événement d'annulation
        cbz_enabled (bool): Activer la création CBZ
        update_progress (func): Callback de progression
        webp2jpg_enabled (bool): Activer la conversion WebP->JPG
    """
    if cancel_event.is_set():
        return
    
    # Préparation des chemins
    clean_title = sanitize_folder_name(title)
    clean_volume = sanitize_folder_name(volume)
    folder = os.path.join(ROOT_FOLDER, clean_title, clean_volume)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        logger(f"Erreur création dossier: {str(e)}", level="error")
        return

    # Calcul du padding pour les noms de fichiers
    number_len = ceil(log10(len(images))) if len(images) > 0 else 1
    failed_downloads = []
    
    # Téléchargement parallèle avec ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        progress_counter = {"done": 0}
        lock = threading.Lock()
        
        for i, url in enumerate(images):
            time.sleep(0.5)  # Délai entre les lancements de threads
            if cancel_event.is_set():
                break

            def progress_callback(i):
                """Callback de progression thread-safe"""
                with lock:
                    progress_counter["done"] += 1
                    if update_progress:
                        update_progress(progress_counter["done"], len(images))

            futures.append(
                executor.submit(
                    download_image,
                    url,
                    folder,
                    cookie,
                    ua,
                    i,
                    number_len,
                    cancel_event,
                    failed_downloads,
                    progress_callback=progress_callback,
                    webp2jpg_enabled=webp2jpg_enabled
                )
            )

            if update_progress:
                update_progress(i + 1, len(images))
                
        # Attente de la complétion des threads
        for future in as_completed(futures):
            if cancel_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
    
    # Gestion des échecs de téléchargement
    if failed_downloads:
        logger(f"⚠️ {len(failed_downloads)} image(s) n'ont pas pu être téléchargées.", level="warning")
        try:
            import tkinter.simpledialog as simpledialog
            res = messagebox.askyesno("Erreur de téléchargement", "Des images ont échoué. Voulez-vous modifier le cookie et relancer le téléchargement complet de ce volume ?")
            if res:
                new_cookie = simpledialog.askstring("Nouveau cookie", "Entrez le nouveau cookie cf_clearance :", parent=MangaApp.current_instance.root)
                if new_cookie:
                    shutil.rmtree(folder)
                    logger("📦 Ancien dossier supprimé. Relancement du téléchargement avec le nouveau cookie...", level="info")
                    download_volume(volume, images, title, new_cookie, ua, logger, cancel_event, cbz_enabled, update_progress, webp2jpg_enabled)
                    return
                else:
                    logger("❌ Aucun cookie saisi. Le volume ne sera pas complété.", level="error")
                    return
        except Exception as e:
            logger(f"❌ Erreur durant la relance : {e}", level="error")
        return

    # Archivage CBZ si réussi
    if not cancel_event.is_set() and os.path.exists(folder):
        if cbz_enabled:
            if archive_cbz(folder, title, volume):
                cbz_path = os.path.join(
                    ROOT_FOLDER, clean_title, f"{clean_title} - {clean_volume}.cbz"
                )
                size_mb = round(os.path.getsize(cbz_path) / (1024 * 1024), 2)
                logger("", level="info")  # ligne vide
                logger(f"CBZ créé : {cbz_path} ({size_mb} MB)", level="cbz")
            else:
                logger(f"Échec de création CBZ pour {clean_volume}", level="warning")
        else:
            logger(f"CBZ non créé pour {clean_volume} (option décochée)", level="info")


def save_cookie_cache(cookies_dict, ua, cbz, flaresolverr_url, webp2jpg_enabled):
    """
    Sauvegarde les paramètres dans un fichier JSON
    
    Args:
        cookies_dict (dict): Cookies par domaine
        ua (str): User-Agent
        cbz (bool): Préférence CBZ
        flaresolverr_url (str): URL FlareSolverr
        webp2jpg_enabled (bool): Préférence conversion
    """
    data = {
        "cookies": cookies_dict,
        "ua": ua,
        "cbz_enabled": cbz,
        "flaresolverr_url": flaresolverr_url,
        "last_url": MangaApp.last_url_used,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "webp2jpg_enabled": webp2jpg_enabled,
    }
    with open(COOKIE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_cookie_cache():
    """Charge les paramètres depuis le fichier cache"""
    default_cbz = True
    default_fs = "http://localhost:8191"
    default_webp2jpg = True
    
    if not os.path.exists(COOKIE_CACHE_PATH):
        return {}, None, default_cbz, default_fs, ""
    
    try:
        with open(COOKIE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = datetime.datetime.fromisoformat(data.get("timestamp"))
        age = (datetime.datetime.now(datetime.timezone.utc) - timestamp).total_seconds()
        
        # Cache valide pendant 24h
        if age <= 86400:
            return (
                data.get("cookies", {}),
                data.get("ua"),
                data.get("cbz_enabled", default_cbz),
                data.get("flaresolverr_url", default_fs),
                data.get("last_url", ""),
                data.get("webp2jpg_enabled", default_webp2jpg)
            )
    except Exception as e:
        print(f"[Warning] Erreur lecture cache cookie : {e}")
    
    return {}, None, default_cbz, default_fs, "", default_webp2jpg


def get_cover_image(r_text):
    """
    Récupère et affiche l'image de couverture d'un manga
    
    Args:
        r_text (str): Contenu HTML de la page
    """
    print("[DEBUG] >>> get_cover_image appelée")
    soup = BeautifulSoup(r_text, "html.parser")
    img = soup.select_one("div.thumb img[src], div.thumb-container img[src]")
    img_url = None

    # Recherche de l'URL de l'image
    if img and img.get("src", "").startswith("http"):
        img_url = img["src"]
    else:
        # Fallback aux balises meta
        meta_tags = soup.find_all("meta", attrs={"property": True})
        for tag in meta_tags:
            if tag["property"] in ["og:image", "og:image:secure_url"]:
                candidate = tag.get("content")
                if candidate and candidate.startswith("http"):
                    img_url = candidate
                    break

    # Téléchargement et affichage de l'image
    if img_url:
        if hasattr(MangaApp, 'current_instance'):
            MangaApp.current_instance.cover_url = img_url
            try:
                headers = {
                    "User-Agent": MangaApp.current_instance.ua.get().strip(),
                }
                # Configuration du Referer selon le domaine
                if "sushiscan.fr" in img_url:
                    headers["Referer"] = "https://sushiscan.fr/"
                elif "sushiscan.net" in img_url:
                    headers["Referer"] = "https://sushiscan.net/"
                
                cookie = MangaApp.current_instance.get_cookie(img_url)
                headers["Cookie"] = f"cf_clearance={cookie}"

                # Téléchargement avec curl-cffi ou requests
                try:
                    import curl_cffi.requests as cffi_requests
                    r = cffi_requests.get(
                        img_url,
                        headers=headers,
                        impersonate="chrome",
                        timeout=10
                    )
                    r.raise_for_status()
                    raw = r.content
                    print("[DEBUG] (curl_cffi) Download OK")
                except ImportError:
                    import requests as cffi_requests
                    r = cffi_requests.get(
                        img_url,
                        headers=headers,
                        timeout=10
                    )
                    r.raise_for_status()
                    raw = r.content
                    print("[DEBUG] (requests) Download OK")

                # Vérification que c'est bien une image
                preview = raw[:120]
                if raw[:6] == b'<html>' or b'<html' in raw[:1024].lower():
                    with open("debug_cover.html", "wb") as f:
                        f.write(raw)
                    if hasattr(MangaApp, "current_instance") and hasattr(MangaApp.current_instance, "log"):
                        MangaApp.current_instance.log("⚠️ Réception HTML au lieu d'une image. Voir debug_cover.html", level="error")
                        MangaApp.current_instance.log(f"[DEBUG] Réponse serveur (cover): {preview!r}", level="error")
                    raise Exception("La couverture n’est PAS une image mais du HTML. Voir debug_cover.html")
                
                # Création de la prévisualisation
                image = Image.open(BytesIO(raw))
                if image.format == "WEBP":
                    image = image.convert("RGB")
                image.thumbnail((160, 240))
                MangaApp.current_instance.cover_preview = ImageTk.PhotoImage(image)
                MangaApp.current_instance.cover_label.configure(image=MangaApp.current_instance.cover_preview)
                MangaApp.current_instance.cover_label.image = MangaApp.current_instance.cover_preview
            except Exception as err:
                print(f"[ERREUR] Affichage couverture : {err}")
                if hasattr(MangaApp, "current_instance") and hasattr(MangaApp.current_instance, "log"):
                    MangaApp.current_instance.log(f"[ERREUR] Affichage couverture : {err}", level="error")
        return img_url

    return None


class MangaApp:
    """
    Classe principale de l'application - Interface graphique Tkinter
    Gère l'ensemble de l'UI et la logique de téléchargement
    """
    flaresolverr_url_static = "http://localhost:8191"  # URL par défaut

    def fetch_manga_image(self, url):
        """Charge et affiche l'image de couverture d'un manga"""
        # Détruire les anciens widgets s'ils existent
        if hasattr(self, "manga_image_label"):
            self.manga_image_label.destroy()
        if hasattr(self, "cover_loading_label"):
            self.cover_loading_label.destroy()

        # Label temporaire pendant le chargement
        self.cover_loading_label = tk.Label(self.right_frame, text="🕒 Chargement...", font=("Segoe UI Emoji", 9))
        self.cover_loading_label.pack(pady=5)
        self.root.update()

        try:
            cookie = self.get_cookie(url)
            user_agent = self.ua.get().strip()
            headers = {
                'User-Agent': user_agent,
                'Cookie': f'cf_clearance={cookie}',
                'Referer': url,
            }
            
            # Téléchargement de la page
            r = requests.get(url, headers=headers)
            r.raise_for_status()
            
            # Parsing HTML pour trouver l'image
            soup = BeautifulSoup(r.text, "html.parser")
            
            # Sélecteurs différents selon le domaine
            if "sushiscan.fr" in url:
                img_tag = soup.select_one("div.thumb-container img")
            else:
                img_tag = soup.select_one("div.thumb img")
                
            if img_tag and 'src' in img_tag.attrs:
                img_url = img_tag['src']
                self.log(f"Image trouvée: {img_url}", level="info")
            else:
                # Fallback aux balises meta
                meta = soup.find("meta", property="og:image")
                if meta and 'content' in meta.attrs:
                    img_url = meta['content']
                    self.log(f"Image trouvée via meta: {img_url}", level="info")
                else:
                    raise Exception("Aucune image trouvée dans la page")

            # Téléchargement et affichage
            r_img = requests.get(img_url, headers={'User-Agent': user_agent})
            img_data = BytesIO(r_img.content)
            img = Image.open(img_data)
            img.thumbnail((160, 240))
            img_tk = ImageTk.PhotoImage(img)

            # Mise à jour UI
            self.cover_loading_label.destroy()
            self.manga_image_label = tk.Label(
                self.right_frame,
                image=img_tk,
                borderwidth=2,
                relief="ridge",
                bg="#202020"
            )
            self.manga_image_label.image = img_tk
            self.manga_image_label.pack(pady=10)
            
            self.log("Couverture affichée avec succès", level="success")

        except Exception as e:
            self.cover_loading_label.destroy()
            error_label = tk.Label(
                self.right_frame,
                text="❌ Échec chargement",
                fg="red",
                font=("Segoe UI", 9)
            )
            error_label.pack(pady=10)
            self.log(f"Erreur couverture: {str(e)}", level="error")

    def update_cookie_status(self):
        """Met à jour les indicateurs de validité des cookies"""
        try:
            ua = self.ua.get().strip()
            for domain in ["fr", "net"]:
                cookie = getattr(self, f"cookie_{domain}").get().strip()
                label = getattr(self, f"cookie_{domain}_status")
                if cookie and test_cookie_validity(domain, cookie, ua):
                    label.config(text="✅", fg="green")
                else:
                    label.config(text="❌", fg="red")
        except Exception as e:
            self.log(f"Erreur statut cookies: {e}", level="error")

    def __init__(self):
        """Initialise l'interface graphique et charge les paramètres"""
        MangaApp.current_instance = self
        self.total_chapters_to_process = 0
        self.chapters_done = 0
        self.root = tk.Tk()
        self.root.title("SushiDL 🍣")
        
        # Variables Tkinter
        self.cbz_enabled = tk.BooleanVar(value=True)
        self.webp2jpg_enabled = tk.BooleanVar(value=True)
        self.url = tk.StringVar()
        self.ua = tk.StringVar()
        self.flaresolverr_url = tk.StringVar()
        self.cookie_fr = tk.StringVar()
        self.cookie_net = tk.StringVar()

        # Chargement du cache
        cookies, ua, cbz, fs_url, last_url, webp2jpg_enabled = load_cookie_cache()
        self.cookie_fr.set(cookies.get("fr", ""))
        self.cookie_net.set(cookies.get("net", ""))
        self.flaresolverr_url.set(fs_url)
        MangaApp.flaresolverr_url_static = fs_url.strip()
        print(f"[INFO] FlareSolverr URL chargé : {MangaApp.flaresolverr_url_static}")
        self.ua.set(ua)
        self.cbz_enabled.set(str(cbz).lower() in ("1", "true", "yes"))
        self.webp2jpg_enabled.set(str(webp2jpg_enabled).lower() in ("1", "true", "yes"))
        self.url.set(last_url)  
        MangaApp.last_url_used = last_url
        
        # Initialisation des composants UI
        self.check_vars = []
        self.check_items = []
        self.image_progress_index = None
        self.pairs = []
        self.title = ""
        self.cancel_event = threading.Event()

        # === Création du layout principal ===
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Création des colonnes gauche/droite
        self.columns_frame = tk.Frame(main_frame)
        self.columns_frame.pack(fill="both", expand=True)
        self.left_frame = tk.Frame(self.columns_frame)
        self.left_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        self.right_frame = tk.Frame(self.columns_frame, width=220)
        self.right_frame.pack(side="right", fill="y")

        # Construction de l'interface
        self.setup_ui()
        self.update_cookie_status()
        self.check_cookie_age_periodically()

        self.log("Application démarrée.", level="info")
        self.root.mainloop()

    def log(self, message, level="info"):
        """Ajoute une entrée dans le journal avec formatage"""
        if not message.strip():
            return
        # Emojis selon le niveau de log
        emoji = {
            "info": "💬",
            "success": "✅",
            "error": "🔴",
            "warning": "⚠️",
            "cbz": "📦",
        }.get(level, "")
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {emoji} {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", formatted, level)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def toast(self, message):
        """Affiche une notification temporaire"""
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.configure(bg="#333")
        x = self.root.winfo_x() + self.root.winfo_width() - 260
        y = self.root.winfo_y() + 40
        toast.geometry(f"250x30+{x}+{y}")
        tk.Label(toast, text=message, bg="#333", fg="white", font=("Segoe UI", 9)).pack(
            fill="both", expand=True
        )
        toast.after(2000, toast.destroy)

    def setup_ui(self):
        """Configure tous les éléments de l'interface graphique"""
        self.progress = tk.DoubleVar(value=0)
        
        # === Bloc supérieur : cookies + config ===
        frame = tk.Frame(self.left_frame)
        frame.pack(padx=20, pady=10)

        font_label = ("Segoe UI Emoji", 10)
        font_entry = ("Segoe UI Emoji", 10)
        row = 0

        # Champ cookie .fr
        tk.Label(frame, text="Cookie cf_clearance (.fr):", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.cookie_fr, width=50, font=font_entry).grid(row=row, column=1, pady=2, sticky="w")
        self.cookie_fr_status = tk.Label(frame, text="", font=("Segoe UI", 10), fg="green")
        self.cookie_fr_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        # Champ cookie .net
        tk.Label(frame, text="Cookie cf_clearance (.net):", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.cookie_net, width=50, font=font_entry).grid(row=row, column=1, pady=2, sticky="w")
        self.cookie_net_status = tk.Label(frame, text="", font=("Segoe UI", 10), fg="green")
        self.cookie_net_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        # Champ User-Agent
        tk.Label(frame, text="User-Agent :", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.ua, width=60, font=font_entry).grid(row=row, column=1, columnspan=2, pady=2)
        row += 1

        # Champ FlareSolverr
        tk.Label(frame, text="FlareSolverr URL :", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.flaresolverr_url, width=60, font=font_entry).grid(row=row, column=1, columnspan=2, pady=2)
        row += 1

        # === Ligne CBZ + Sauvegarder ===
        cbz_frame = tk.Frame(self.left_frame)
        cbz_frame.pack(pady=(0, 15))
        tk.Checkbutton(
            cbz_frame,
            text=".CBZ",
            variable=self.cbz_enabled,
            bg="SystemButtonFace"
        ).pack(side="left", padx=(0, 10))
        tk.Checkbutton(
            cbz_frame,
            text="WEBP en JPG",
            variable=self.webp2jpg_enabled,
            bg="SystemButtonFace"
        ).pack(side="left", padx=(0, 10))
        tk.Button(
            cbz_frame,
            text="Sauvegarder Paramètres",
            command=self.save_current_cookie,
            width=30
        ).pack(side="left")
        
        # === Zone couverture (droite) ===
        self.cover_frame = tk.Frame(self.right_frame, pady=10)
        self.cover_frame.pack(fill="both", expand=True)
        self.cover_label = tk.Label(self.cover_frame)
        self.cover_label.pack()
        
        # === Champ URL du manga ===
        url_frame = tk.Frame(self.left_frame)
        url_frame.pack(pady=(0, 10))
        tk.Label(url_frame, text="URL du manga :", font=font_label).pack(anchor="w")
        tk.Entry(url_frame, textvariable=self.url, width=70, font=font_entry).pack()

        # Bouton Analyser + label statut
        analyze_frame = tk.Frame(self.left_frame)
        analyze_frame.pack()
        tk.Button(analyze_frame, text="Analyser", command=self.load_volumes, width=30).pack(pady=(5, 2))
        self.status_label = tk.Label(analyze_frame, text="", font=("Segoe UI Emoji", 10))
        self.status_label.pack()

        # === En-tête Volume(s) + Filtre ===
        vol_header = tk.Frame(self.left_frame)
        vol_header.pack(fill="x", padx=20, pady=(10, 0))
        tk.Label(
            vol_header,
            text="Volume(s) | Chapitre(s)",
            font=("Segoe UI Emoji", 10, "bold"),
            fg="#444"
        ).pack(side="left")

        # Groupe filtre
        filter_group = tk.Frame(vol_header)
        filter_group.pack(side="right")
        tk.Label(filter_group, text="🔍", font=("Segoe UI Emoji", 9)).pack(side="left")
        self.filter_text = tk.StringVar()
        self.filter_entry = tk.Entry(filter_group, textvariable=self.filter_text, width=25)
        self.filter_entry.pack(side="left", padx=5)
        self.filter_entry.bind("<KeyRelease>", lambda e: self.apply_filter())
        self.clear_filter_button = tk.Button(filter_group, text="❌", command=self.clear_filter)
        self.clear_filter_button.pack(side="left")

        # Désactivation initiale du filtre
        self.filter_entry.config(state="disabled")
        self.clear_filter_button.config(state="disabled")
        
        # === Zone scrollable des volumes ===
        vol_frame_container = tk.Frame(self.root, bd=1, relief="sunken", bg="SystemButtonFace")
        canvas_frame = tk.Frame(vol_frame_container)
        canvas_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, height=300, bg="#f0f0f0", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.vol_frame = tk.Frame(self.canvas, bg="#f0f0f0")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.vol_frame, anchor="n")

        # Callback de redimensionnement
        def center_volumes(event):
            canvas_width = event.width
            self.canvas.itemconfig(self.canvas_window, width=canvas_width)

        self.canvas.bind("<Configure>", center_volumes)
        self.vol_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        vol_frame_container.pack(fill="both", expand=True, padx=15, pady=(5, 15))
        
        # === Zone d'actions ===
        action_frame = tk.Frame(self.left_frame)
        action_frame.pack(pady=(10, 10))

        # Checkbox "Tous/Aucun"
        self.master_var = tk.BooleanVar(value=True)
        master_chk = tk.Checkbutton(
            action_frame,
            text="Tous/Aucun",
            variable=self.master_var,
            command=lambda: self.toggle_all_volumes(self.master_var.get()),
            bg="SystemButtonFace"
        )
        master_chk.pack(side="left", padx=10)

        # Bouton Inverser
        invert_btn = tk.Button(
            action_frame,
            text="Inverser",
            command=self.invert_selection
        )
        invert_btn.pack(side="left", padx=10)

        # Bouton Télécharger
        self.dl_button = tk.Button(
            action_frame,
            text="Télécharger",
            command=self.download_selected,
            width=20,
            state="disabled"
        )
        self.dl_button.pack(side="left", padx=10)

        # Bouton Annuler
        self.cancel_button = tk.Button(
            action_frame,
            text="Annuler",
            command=self.cancel_download,
            width=15,
            state="disabled"
        )
        self.cancel_button.pack(side="left", padx=10)

        # === Barre de progression ===
        progress_frame = tk.Frame(self.left_frame)
        progress_frame.pack(fill="x", padx=20, pady=(0, 10))
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress, maximum=100)
        self.progress_bar.pack(fill="x")
        self.progress_label = tk.Label(
            progress_frame,
            text="0%",
            font=("Segoe UI", 8),
            anchor="center",
            bg=self.root["bg"]
        )
        self.progress_label.place(relx=0.5, rely=0.5, anchor="center")

        # === Journal (log) ===
        log_frame = tk.Frame(self.left_frame)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word", bg="white", font=("Segoe UI", 9))
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        
        # Configuration des couleurs pour les différents niveaux de log
        self.log_text.tag_config("success", foreground="green")
        self.log_text.tag_config("info", foreground="#007acc")
        self.log_text.tag_config("error", foreground="red")
        self.log_text.tag_config("warning", foreground="#e67e22")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def get_cookie(self, url):
        """Sélectionne automatiquement le cookie selon le domaine"""
        if "sushiscan.fr" in url:
            return self.cookie_fr.get().strip()
        elif "sushiscan.net" in url:
            return self.cookie_net.get().strip()
        return ""

    def load_volumes(self):
        """Charge la liste des volumes/chapitres pour l'URL donnée"""
        self.update_cookie_status()
        url = self.url.get().strip()
        cookie = self.get_cookie(url)
        self.filter_text.set("")  # Réinitialise le filtre
        
        try:
            # Récupération des données du manga
            self.title, self.pairs = fetch_manga_data(
                url, cookie, self.ua.get().strip()
            )
            
            # Tentative de récupération de la couverture
            try:
                r = make_request(url, self.get_cookie(url), self.ua.get().strip())
                get_cover_image(r.text)
            except Exception as e:
                self.log(f"Erreur chargement couverture: {str(e)}", level="error")
            
            # Sauvegarde dans le cache
            MangaApp.last_url_used = url
            save_cookie_cache(
                {
                    "fr": self.cookie_fr.get().strip(),
                    "net": self.cookie_net.get().strip(),
                },
                self.ua.get().strip(),
                self.cbz_enabled.get(),
                self.flaresolverr_url.get().strip(),
                self.webp2jpg_enabled.get()
            )
        except Exception as e:
            self.log(f"Erreur : {str(e)}", level="error")
            self.toast("❌ Impossible de charger la liste")
            return

        # Nettoyage de la zone d'affichage
        for widget in self.vol_frame.winfo_children():
            widget.destroy()

        # Création des checkboxes pour chaque volume
        self.check_vars = []
        self.check_items = []
        columns = 4  # Nombre de colonnes pour la grille
        
        for col in range(columns):
            self.vol_frame.grid_columnconfigure(col, weight=1)

        for i, (vol, link) in enumerate(self.pairs):
            var = tk.BooleanVar(value=True)
            self.check_vars.append(var)

            chk = tk.Checkbutton(
                self.vol_frame,
                text=vol,
                variable=var,
                bg="SystemButtonFace",
                activebackground="SystemButtonFace",
                highlightthickness=0,
                bd=0,
                anchor="center",
                justify="center"
            )
            chk.grid(row=(i // columns) + 2, column=i % columns, padx=15, pady=5, sticky="n")
            self.check_items.append((chk, vol))

        # Activation des contrôles
        self.dl_button.config(state="normal")
        self.canvas.yview_moveto(0)
        self.log("Liste chargée avec succès.", level="success")
        self.filter_entry.config(state="normal")
        self.clear_filter_button.config(state="normal")

    def toggle_all_volumes(self, state):
        """Coche/décoche toutes les cases à cocher"""
        for var in self.check_vars:
            var.set(state)
    
    def invert_selection(self):
        """Inverse la sélection actuelle"""
        for var in self.check_vars:
            var.set(not var.get())

    def apply_filter(self):
        """Filtre la liste des volumes selon le texte saisi"""
        raw = self.filter_text.get().strip()

        # Support des wildcards (ex: "7*" pour volumes 70-79)
        if raw.endswith("*") and raw[:-1].isdigit():
            prefix = raw[:-1]
            pattern = rf"\b{prefix}\d\b|\b{prefix}\d\d*\b"
        else:
            pattern = re.escape(raw.lower())

        try:
            regex = re.compile(pattern)
        except re.error:
            self.log(f"❌ Filtre invalide : {raw}", level="error")
            return

        # Application du filtre
        row = 0
        col = 0
        for chk, label in self.check_items:
            if regex.search(label.lower()):
                chk.grid(row=row + 2, column=col, padx=15, pady=5, sticky="n")
                col += 1
                if col == 4:
                    col = 0
                    row += 1
            else:
                chk.grid_remove()

    def clear_filter(self):
        """Réinitialise le filtre et affiche tous les volumes"""
        self.filter_text.set("")       
        self.apply_filter()            

    def download_selected(self):
        """Lance le téléchargement des volumes sélectionnés"""
        self.cancel_event.clear()
        selected = []
        for (chk, label), (vol, link), var in zip(self.check_items, self.pairs, self.check_vars):
            if var.get() and chk.winfo_ismapped():  # Visible + sélectionné
                selected.append((vol, link))
                
        if not selected:
            self.log("Aucun volume sélectionné.", level="info")
            return

        # Configuration UI pour le téléchargement
        self.dl_button.config(text="Téléchargement...", state="disabled")
        self.cancel_button.config(state="normal")
        self.filter_entry.config(state="disabled")
        self.clear_filter_button.config(state="disabled")
        self.progress.set(0)
        self.root.update_idletasks()

        def task():
            """Fonction exécutée dans un thread séparé pour le téléchargement"""
            total = len(selected)
            failed = []
            self.image_progress_index = None  # reset pour le volume en cours

            def per_image_progress(done, total_images):
                """Callback de progression pour les images individuelles"""
                nonlocal last_progress_update, last_progress_value
                now = time.time()
                last_progress_value = [done, total_images]

                # Limite la fréquence de rafraîchissement
                if now - last_progress_update < 0.5:
                    return
                last_progress_update = now

                percent = round((done / total_images) * 100, 1) if total_images else 0
                timestamp = time.strftime("%H:%M:%S")
                line = f"[{timestamp}] 🖼️ Progression image : {done}/{total_images} ({percent}%)"

                self.log_text.configure(state="normal")
                if self.image_progress_index is None:
                    self.image_progress_index = self.log_text.index("end-1c")
                    self.log_text.insert("end", line + "\n", "info")
                else:
                    if self.image_progress_index is not None:
                        try:
                            index_float = float(self.image_progress_index)
                            self.log_text.delete(self.image_progress_index, str(index_float + 1))
                            self.log_text.insert(self.image_progress_index, line + "\n", "info")
                        except (TypeError, ValueError):
                            self.log_text.insert("end", line + "\n", "info")
                            self.image_progress_index = self.log_text.index("end-1c")
                    else:
                        self.log_text.insert("end", line + "\n", "info")
                        self.image_progress_index = self.log_text.index("end-1c")
                self.log_text.configure(state="disabled")
                self.log_text.see("end")
                
                # Mise à jour de la barre de progression
                percent = (done / total_images) * 100 if total_images else 0
                self.progress.set(percent)
                self.progress_label.config(text=f"{int(percent)}%")
                self.root.update_idletasks()
                
            last_progress_update = 0
            last_progress_value = [0, 0]  # [fait, total]
            
            # Traitement de chaque volume sélectionné
            for idx, (vol, link) in enumerate(selected):
                time.sleep(2)  # Pause de 2 secondes entre les volumes
                start_time = time.time()
                if self.cancel_event.is_set():
                    break

                cookie = self.get_cookie(link)
                self.root.title(f"SushiDL - {vol}")
                self.log(f"📄 📥 Volume : {vol} | Lien : {link}", level="info")
                
                # Vérification de l'existence du CBZ
                clean_title = sanitize_folder_name(self.title)
                clean_volume = sanitize_folder_name(vol)
                cbz_path = os.path.join(ROOT_FOLDER, clean_title, f"{clean_title} - {clean_volume}.cbz")

                if os.path.exists(cbz_path) and os.path.getsize(cbz_path) > 10_000:
                    self.log(f"⏩ CBZ déjà existant, saut de : {vol}", level="info")
                    continue  # passe au volume suivant

                # Réinitialisation de la progression
                self.image_progress_index = None
                self.progress.set(0)
                self.progress_label.config(text="0%")
                self.root.update_idletasks()
                last_progress_value = [0, 1]  # évite division par 0

                # Récupération des images
                images = get_images(link, cookie, self.ua.get().strip())
                last_progress_value = [0, max(1, len(images))]  # mise à jour avec le vrai total
                self.log(f"🔍 {len(images)} image(s) trouvée(s)", level="info")

                if images:
                    self.log(f"🚀 Début du téléchargement pour : {vol}", level="success")
                    download_volume(
                        vol,
                        images,
                        self.title,
                        cookie,
                        self.ua.get().strip(),
                        self.log,
                        self.cancel_event,
                        self.cbz_enabled.get(),
                        update_progress=per_image_progress,
                        webp2jpg_enabled=self.webp2jpg_enabled.get()
                    )

                    # Mise à jour finale de la progression
                    done, total = last_progress_value
                    if done and total:
                        percent = round((done / total) * 100, 1)
                        timestamp = time.strftime("%H:%M:%S")
                        line = f"[{timestamp}] 🖼️ Progression image : {done}/{total} ({percent}%)"
                        self.log_text.configure(state="normal")
                        if self.image_progress_index is not None:
                            try:
                                index_float = float(self.image_progress_index)
                                self.log_text.delete(self.image_progress_index, str(index_float + 1))
                                self.log_text.insert(self.image_progress_index, line + "\n", "info")
                            except (TypeError, ValueError):
                                self.log_text.insert("end", line + "\n", "info")
                                self.image_progress_index = self.log_text.index("end-1c")
                        else:
                            self.log_text.insert("end", line + "\n", "info")
                            self.image_progress_index = self.log_text.index("end-1c")
                        self.log_text.configure(state="disabled")
                        self.log_text.see("end")

                        self.progress.set(percent)
                        self.progress_label.config(text=f"{int(percent)}%")
                        self.root.update_idletasks()

                    elapsed = round(time.time() - start_time, 2)
                    self.log(f"⏱️ Temps écoulé : {elapsed} secondes", level="info")
                else:
                    self.log(f"⚠️ Échec récupération images pour {vol}", level="warning")
                    failed.append((vol, link))

            # Tentative de récupération des échecs
            if not self.cancel_event.is_set() and failed:
                self.log(
                    f"🔁 Retry des volumes échoués ({len(failed)} restants)",
                    level="warning",
                )
                retry_failed = []

                for vol, link in failed:
                    if self.cancel_event.is_set():
                        break
                    cookie = self.get_cookie(link)
                    images = get_images(link, cookie, self.ua.get().strip())
                    if images:
                        self.log(f"✅ Retry réussi : {vol}", level="info")
                        download_volume(
                            vol,
                            images,
                            self.title,
                            cookie,
                            self.ua.get().strip(),
                            self.log,
                            self.cancel_event,
                            self.cbz_enabled.get(),
                            update_progress=per_image_progress,
                            webp2jpg_enabled=self.webp2jpg_enabled.get()
                        )
                    else:
                        self.log(f"❌ Retry échoué : {vol}", level="error")
                        retry_failed.append(vol)

                if retry_failed:
                    self.log(
                        f"⛔ Volumes définitivement échoués : {', '.join(retry_failed)}",
                        level="error",
                    )

            # Finalisation
            self.dl_button.config(text="Télécharger la sélection", state="normal")
            self.cancel_button.config(state="disabled")
            if self.cancel_event.is_set():
                self.log("Téléchargement annulé !", level="warning")
                self.progress.set(0)
            else:
                self.log("Tous les volumes ont été traités.", level="success")
            self.cancel_event.clear()

        # Lancement dans un thread séparé
        threading.Thread(target=task).start()

    def cancel_download(self):
        """Annule le téléchargement en cours"""
        self.cancel_event.set()
        self.log("Annulation demandée...", level="warning")
        self.cancel_button.config(state="disabled")

    def check_cookie_age_periodically(self):
        """Vérifie périodiquement l'âge des cookies"""
        try:
            if os.path.exists(COOKIE_CACHE_PATH):
                with open(COOKIE_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                timestamp = datetime.datetime.fromisoformat(data.get("timestamp"))
                age = (
                    datetime.datetime.now(datetime.timezone.utc) - timestamp
                ).total_seconds()
                if age > 3600:  # 1 heure
                    self.log("Cookie expiré depuis plus d'1h", level="warning")
        except Exception as e:
            self.log(f"Erreur vérification cookie: {e}", level="error")
        self.root.after(3600000, self.check_cookie_age_periodically)  # Re-programme après 1h

    def update_flaresolverr_url(self):
        """Met à jour l'URL de FlareSolverr avec la valeur saisie"""
        new_url = self.flaresolverr_url.get().strip()
        if new_url:
            MangaApp.flaresolverr_url_static = new_url
            self.log(f"FlareSolverr URL mis à jour : {new_url}", level="success")
        else:
            self.log("L'URL de FlareSolverr est vide.", level="error")

    def save_current_cookie(self):
        """Sauvegarde les paramètres actuels dans le cache"""
        try:
            self.update_flaresolverr_url()
            cookies = {
                "fr": self.cookie_fr.get().strip(),
                "net": self.cookie_net.get().strip(),
            }
            save_cookie_cache(
                cookies,
                self.ua.get().strip(),
                self.cbz_enabled.get(),
                self.flaresolverr_url.get().strip(),
                self.webp2jpg_enabled.get()
            )
            self.log("Cookies, UA, CBZ, WEBP en JPG et FlareSolverr URL sauvegardés !", level="success")
            self.update_cookie_status()
        except Exception as e:
            self.log(f"Erreur sauvegarde: {e}", level="error")


# Point d'entrée de l'application
if __name__ == "__main__":
    MangaApp()