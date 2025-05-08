import os
import re
import html
import json
import shutil
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil, log10
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image, ImageTk
from curl_cffi import requests
from curl_cffi.requests.exceptions import Timeout
from zipfile import ZipFile
import time
import datetime
import urllib.parse


def fetch_with_flaresolverr(url, flaresolverr_url):
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
        html_result = solution.get("solution", {}).get("response", "")
        if not html_result.strip():
            print("[FlareSolverr] ⚠️ Réponse vide après rendu.")
        else:
            print("[FlareSolverr] ✅ Contournement réussi, contenu récupéré.")
        return html_result

    except Exception as e:
        print(f"[FlareSolverr] ❌ Erreur : {e}")
        return ""



REGEX_URL = r"^https://sushiscan\.(fr|net)/catalogue/[a-z0-9-]+/$"
ROOT_FOLDER = "DL SushiScan"
THREADS = 10
COOKIE_CACHE_PATH = "cookie_cache.json"


# --- Helpers ---
def sanitize_folder_name(name):
    return re.sub(r'[<>:"/\\|?*\n\r]', "_", name).strip()


def make_request(url, cookie, ua):
    headers = {
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Cookie": f"cf_clearance={cookie}",
        "User-Agent": ua,
    }
    #print(f"[INFO] 📡 Requête en cours : {url}")
    return requests.get(url, headers=headers, impersonate="chrome", timeout=10)


def parse_lr(text, left, right, recursive, unescape=True):
    pattern = re.escape(left) + "(.*?)" + re.escape(right)
    matches = re.findall(pattern, text)
    if unescape:
        matches = [html.unescape(match) for match in matches]
    return matches if recursive else matches[0] if matches else None


def convert_webp_to_jpg(image_path):
    if image_path.lower().endswith(".webp"):
        im = Image.open(image_path).convert("RGB")
        new_path = image_path[:-5] + ".jpg"
        im.save(new_path, "JPEG")
        os.remove(image_path)
        return new_path
    return image_path

def test_cookie_validity(domain, cookie, ua):
    """Teste si le cookie cf_clearance est encore valide pour .fr ou .net"""
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
    clean_title = sanitize_folder_name(title)
    clean_volume = sanitize_folder_name(volume)
    parent_dir = os.path.dirname(folder_path)
    cbz_name = os.path.join(parent_dir, f"{clean_title} - {clean_volume}.cbz")
    with ZipFile(cbz_name, "w") as cbz:
        for root, _, files in os.walk(folder_path):
            for file in sorted(files):
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, folder_path)
                cbz.write(full_path, arcname)
    try:
        with ZipFile(cbz_name, "r") as test_zip:
            test_zip.testzip()
    except Exception:
        return False
    if os.path.exists(cbz_name) and os.path.getsize(cbz_name) > 10000:
        shutil.rmtree(folder_path)
        return True
    return False


def download_image(
    url, folder, cookie, ua, i, number_len, cancel_event, progress_callback=None
):
    if cancel_event.is_set():
        return

    headers = {
        "Accept": "image/webp,image/jpeg,image/png,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Cookie": f"cf_clearance={cookie}",
        "User-Agent": ua,
    }

    for attempt in range(3):
        if cancel_event.is_set():
            return

        r = requests.get(url, headers=headers, impersonate="chrome")

        if r.status_code == 200:
            content_type = r.headers.get("Content-Type", "")

            if "image" not in content_type:
                print(
                    f"[Ignoré] Contenu non-image détecté (Content-Type: {content_type}) pour l'URL: {url}"
                )
                return

            # Détermination de l'extension basée sur Content-Type
            if "webp" in content_type:
                ext = "webp"
            elif "jpeg" in content_type or "jpg" in content_type:
                ext = "jpg"
            elif "png" in content_type:
                ext = "png"
            else:
                print(
                    f"[Ignoré] Format d'image inconnu (Content-Type: {content_type}) pour l'URL: {url}"
                )
                return

            filename = os.path.join(folder, str(i).zfill(number_len) + "." + ext)
            with open(filename, "wb") as f:
                f.write(r.content)

            final_path = convert_webp_to_jpg(filename)
            print(f"Image {i} téléchargée avec succès : {final_path}")
            if progress_callback:
                progress_callback()
            return  # ✅ quitte immédiatement après succès

        else:
            print(
                f"[Erreur] Échec du téléchargement (statut: {r.status_code}), tentative {attempt+1}/3 pour l'URL: {url}"
            )

    print(
        f"[Erreur définitive] Impossible de télécharger l'image après 3 essais : {url}"
    )



def fetch_manga_data(url, cookie, ua):
    r = make_request(url, cookie, ua)
    if r.status_code != 200:
        raise Exception("Accès refusé ou URL invalide")
    html_content = r.text

    title = html.unescape(
        parse_lr(
            html_content, '<h1 class="entry-title" itemprop="name">', "</h1>", False
        )
    )

    # Recherche souple des liens vers les volumes ou chapitres
    matches = re.findall(
        r'<a href="(https://sushiscan\.(fr|net)/[^"]+)">\s*<span class="chapternum">(.*?)</span>',
        html_content,
    )
    print(f"[INFO] {len(matches)} volumes/chapitres détectés")

    seen = set()
    pairs = []
    for url, _, label in matches:
        if url in seen:
            continue
        seen.add(url)
        pairs.append((label.strip(), url))

    pairs.reverse()  # Pour afficher dans l'ordre croissant
    return title, pairs


def get_images(link, cookie, ua, retries=2, delay=5):
    """
    Tente d'extraire les images via JSON ou balises <img>.
    Si aucun résultat, passe automatiquement par FlareSolverr.
    """
    def extract_images(r_text):
        # Étape 1 — JSON via ts_reader.run(...)
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
                    return images
            except Exception as e:
                print(f"[Erreur] Parsing JSON : {e}")

        # Étape 2 — Fallback : images dans #readerarea (HTML statique)
        soup = BeautifulSoup(r_text, "html.parser")
        reader = soup.find("div", id="readerarea")
        if reader:
            images = [
                img["src"]
                for img in reader.find_all("img", src=True)
                if not img["src"].startswith("data:") and img["src"].endswith((".jpg", ".jpeg", ".png", ".webp"))
            ]
            if images:
                print(f"[INFO] ✅ {len(images)} images détectées dans #readerarea.")
                return images

        # Étape 3 — Fallback ultime via regex
        img_urls = re.findall(
            r'<img[^>]+(?:src|data-src)=["\'](https://[^"\'>]+\.(?:webp|jpg|jpeg|jpe|png))["\']',
            r_text,
            re.IGNORECASE,
        )
        img_urls = list(dict.fromkeys(img_urls))
        if img_urls:
            print(f"[INFO] ✅ {len(img_urls)} images détectées via <img> (regex fallback).")
        return img_urls


    # --- Phase 1 : tentative directe sans FlareSolverr
    try:
        r = make_request(link, cookie, ua)
        print(f"[INFO] 📡 Requête HTTP directe reçue (len={len(r.text)})")
        images = extract_images(r.text)
        if images:
            return images
        else:
            print("[INFO] ⚠️ Aucune image trouvée en direct, tentative via FlareSolverr...")
    except Exception as e:
        message = str(e)
        interpretation = interpret_curl_error(message)
        if interpretation:
            print(f"[WARNING] ❌ {interpretation} Passage via FlareSolverr...")
            if hasattr(MangaApp, 'current_instance'):
                MangaApp.current_instance.log(interpretation, level="warning")
        else:
            print(f"[WARNING] ❌ Erreur directe : {message} → tentative via FlareSolverr...")
            if hasattr(MangaApp, 'current_instance'):
                MangaApp.current_instance.log(f"Erreur directe : {message}", level="warning")


    # --- Phase 2 : Fallback via FlareSolverr
    try:
        r_text = fetch_with_flaresolverr(link, MangaApp.flaresolverr_url_static)
        images = extract_images(r_text)
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
):
    if cancel_event.is_set():
        return
    clean_title = sanitize_folder_name(title)
    clean_volume = sanitize_folder_name(volume)
    folder = os.path.join(ROOT_FOLDER, clean_title, clean_volume)

    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        logger(f"Erreur création dossier: {str(e)}", level="error")
        return

    number_len = ceil(log10(len(images))) if len(images) > 0 else 1
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = []
        progress_counter = {"done": 0}
        lock = threading.Lock()
        for i, url in enumerate(images):
            if cancel_event.is_set():
                break

            def progress_callback():
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
                    progress_callback=progress_callback,
                )
            )

            if update_progress:
                update_progress(i + 1, len(images))
        for future in as_completed(futures):
            if cancel_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break

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
                logger(f"❌ Échec de création CBZ pour {clean_volume}", level="warning")
        else:
            logger(f"CBZ non créé pour {clean_volume} (option décochée)", level="info")


def save_cookie_cache(cookies_dict, ua, cbz, flaresolverr_url):
    data = {
        "cookies": cookies_dict,
        "ua": ua,
        "cbz_enabled": cbz,
        "flaresolverr_url": flaresolverr_url,
        "last_url": MangaApp.last_url_used,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(COOKIE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_cookie_cache():
    default_cbz = True
    default_fs = "http://localhost:8191"
    if not os.path.exists(COOKIE_CACHE_PATH):
        return {}, None, default_cbz, default_fs, ""
    try:
        with open(COOKIE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = datetime.datetime.fromisoformat(data.get("timestamp"))
        age = (datetime.datetime.now(datetime.timezone.utc) - timestamp).total_seconds()
        if age <= 86400:
            return (
                data.get("cookies", {}),
                data.get("ua"),
                data.get("cbz_enabled", default_cbz),
                data.get("flaresolverr_url", default_fs),
                data.get("last_url", "")
            )
    except Exception as e:
        print(f"[Warning] Erreur lecture cache cookie : {e}")
    return {}, None, default_cbz, default_fs, ""


class MangaApp:
    flaresolverr_url_static = "http://localhost:8191"

    def fetch_manga_image(self, url):
        """Récupère et affiche l'image du manga depuis l'URL (sushiscan.fr via FlareSolverr ou direct)"""

        # 1. Supprimer l'image précédente si présente
        if hasattr(self, "manga_image_label"):
            self.manga_image_label.destroy()
        # Supprimer le précédent message si encore là
        if hasattr(self, "cover_loading_label"):
            self.cover_loading_label.destroy()

        # Créer dynamiquement le message "Chargement de la couverture..."
        self.cover_loading_label = tk.Label(self.status_label.master, text="🕒 Chargement de la couverture...", font=("Segoe UI Emoji", 10))
        self.cover_loading_label.pack()
        # 2. Afficher un message de chargement visuel temporaire

        try:
            cookie = self.get_cookie(url)
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            headers = {
                'User-Agent': user_agent,
                'Cookie': f'cf_clearance={cookie}',
                'Referer': url,
            }

            img_tag = None

            if "sushiscan.fr" in url:
                self.log("⚡ sushiscan.fr détecté → passage par FlareSolverr", level="warning")
                self.status_label.config(text="🛡️ FlareSolverr en cours, merci de patienter...")

                html = fetch_with_flaresolverr(url, MangaApp.flaresolverr_url_static)
                self.status_label.config(text="")  # Retirer l’indication FlareSolverr

                if not html:
                    self.cover_loading_label.destroy()
                    self.log("❌ FlareSolverr a échoué à récupérer la page", level="error")
                    return

                soup = BeautifulSoup(html, "html.parser")

                # Essai avec le conteneur thumb-container
                thumb = soup.find("div", class_="thumb-container")
                if thumb:
                    img = thumb.find("img", src=True)
                    if img and img["src"].endswith((".jpg", ".jpeg", ".png", ".webp")):
                        img_tag = img

                # Sinon, boucle sur les balises <img>
                if not img_tag:
                    for img in soup.find_all("img", src=True):
                        src = img["src"]
                        if not src.startswith("data:") and src.endswith((".jpg", ".jpeg", ".png", ".webp")):
                            img_tag = img
                            break

                if not img_tag:
                    self.cover_loading_label.destroy()
                    self.log("❌ Aucune image valide trouvée via FlareSolverr", level="error")
                    return

            else:
                self.log("🌐 Accès direct à sushiscan.net", level="info")
                r = requests.get(url, headers=headers)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                img_tag = soup.find("img", class_="attachment- size- wp-post-image")
                if not img_tag or not img_tag.get("src"):
                    self.cover_loading_label.destroy()
                    self.log("❌ Image non trouvée sur sushiscan.net", level="error")
                    return

            # --- Affichage image ---
            img_url = urllib.parse.urljoin(url, img_tag["src"].split("?")[0])
            if img_url.startswith("data:image") or img_url.lower().endswith(".svg"):
                self.cover_loading_label.destroy()
                self.log("❌ Image encodée ou SVG non supporté", level="error")
                return

            self.log(f"📥 Téléchargement de l'image : {img_url}", level="info")
            r = requests.get(img_url, headers=headers)
            r.raise_for_status()

            img_data = BytesIO(r.content)
            img = Image.open(img_data)
            img = img.resize((130, int(130 * img.height / img.width)))
            img_tk = ImageTk.PhotoImage(img)

            # Supprimer le label "Chargement..."
            self.cover_loading_label.destroy()

            self.manga_image_label = tk.Label(self.root, image=img_tk)
            self.manga_image_label.image = img_tk
            self.manga_image_label.place(x=10, y=10)

            current_width = self.root.winfo_width()
            current_height = self.root.winfo_height()
            new_width = current_width + img.width + 130
            self.root.geometry(f"{new_width}x{current_height}")

            self.log("✅ Image de couverture affichée avec succès", level="success")

        except requests.exceptions.RequestException as e:
            if hasattr(self, "cover_loading_label"):
                self.cover_loading_label.destroy()
            self.status_label.config(text="")
            self.log(f"❌ Erreur réseau lors de l'image : {e}", level="error")

        except Exception as e:
            if hasattr(self, "cover_loading_label"):
                self.cover_loading_label.destroy()
            self.status_label.config(text="")
            self.log(f"❌ Erreur inattendue dans fetch_manga_image : {e}", level="error")


    def update_cookie_status(self):
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
        MangaApp.current_instance = self
        self.total_chapters_to_process = 0
        self.chapters_done = 0
        self.root = tk.Tk()
        self.root.title("SushiDL 🍣")
        self.cbz_enabled = tk.BooleanVar(value=True)
        self.url = tk.StringVar()
        self.ua = tk.StringVar()
        self.flaresolverr_url = tk.StringVar()

        # Variables pour les cookies
        self.cookie_fr = tk.StringVar()
        self.cookie_net = tk.StringVar()

        # Chargement du cache
        cookies, ua, cbz, fs_url, last_url = load_cookie_cache()
        self.cookie_fr.set(cookies.get("fr", ""))
        self.cookie_net.set(cookies.get("net", ""))
        self.flaresolverr_url.set(fs_url)
        MangaApp.flaresolverr_url_static = fs_url.strip()
        print(f"[INFO] FlareSolverr URL chargé : {MangaApp.flaresolverr_url_static}")
        self.ua.set(ua)
        self.cbz_enabled.set(cbz)
        self.url.set(last_url)  
        MangaApp.last_url_used = last_url
        self.check_vars = []
        self.check_items = []
        self.image_progress_index = None  # pour mise à jour horizontale du log d'image
        self.pairs = []
        self.title = ""
        self.cancel_event = threading.Event()

        self.setup_ui()
        self.update_cookie_status()
        self.check_cookie_age_periodically()

        self.log("Application démarrée.", level="info")
        self.root.mainloop()

    def log(self, message, level="info"):
        if not message.strip():
            return
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
        self.progress = tk.DoubleVar(value=0)
        # === Bloc supérieur : cookies + config ===
        frame = tk.Frame(self.root)
        frame.pack(padx=20, pady=10)

        font_label = ("Segoe UI Emoji", 10)
        font_entry = ("Segoe UI Emoji", 10)
        row = 0

        tk.Label(frame, text="Cookie cf_clearance (.fr):", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.cookie_fr, width=50, font=font_entry).grid(row=row, column=1, pady=2, sticky="w")

        self.cookie_fr_status = tk.Label(frame, text="", font=("Segoe UI", 10), fg="green")
        self.cookie_fr_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        tk.Label(frame, text="Cookie cf_clearance (.net):", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.cookie_net, width=50, font=font_entry).grid(row=row, column=1, pady=2, sticky="w")

        self.cookie_net_status = tk.Label(frame, text="", font=("Segoe UI", 10), fg="green")
        self.cookie_net_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1


        tk.Label(frame, text="User-Agent :", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.ua, width=60, font=font_entry).grid(row=row, column=1, columnspan=2, pady=2)
        row += 1

        tk.Label(frame, text="FlareSolverr URL :", font=font_label).grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.flaresolverr_url, width=60, font=font_entry).grid(row=row, column=1, columnspan=2, pady=2)
        row += 1

        # === Ligne CBZ + Sauvegarder
        cbz_frame = tk.Frame(self.root)
        cbz_frame.pack(pady=(0, 15))

        self.cbz_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(
            cbz_frame,
            text=".CBZ",
            variable=self.cbz_enabled,
            bg="SystemButtonFace"
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            cbz_frame,
            text="Sauvegarder Paramètres",
            command=self.save_current_cookie,
            width=30
        ).pack(side="left")

        # === Champ URL du manga + bouton Analyser + label de statut
        url_frame = tk.Frame(self.root)
        url_frame.pack(pady=(0, 10))

        tk.Label(url_frame, text="URL du manga :", font=font_label).pack(anchor="w")
        tk.Entry(url_frame, textvariable=self.url, width=70, font=font_entry).pack()

        analyze_frame = tk.Frame(self.root)
        analyze_frame.pack()  # Ce frame contient le bouton + label juste en dessous

        tk.Button(analyze_frame, text="Analyser", command=self.load_volumes, width=30).pack(pady=(5, 2))

        self.status_label = tk.Label(analyze_frame, text="", font=("Segoe UI Emoji", 10))
        self.status_label.pack()

        # === En-tête Volume(s) + Filtre ===
        vol_header = tk.Frame(self.root)
        vol_header.pack(fill="x", padx=20, pady=(10, 0))

        tk.Label(
            vol_header,
            text="Volume(s) | Chapitre(s)",
            font=("Segoe UI Emoji", 10, "bold"),
            fg="#444"
        ).pack(side="left")

        filter_group = tk.Frame(vol_header)
        filter_group.pack(side="right")
        tk.Label(filter_group, text="🔍", font=("Segoe UI Emoji", 9)).pack(side="left")
        self.filter_text = tk.StringVar()
        self.filter_entry = tk.Entry(filter_group, textvariable=self.filter_text, width=25)
        self.filter_entry.pack(side="left", padx=5)
        self.filter_entry.bind("<KeyRelease>", lambda e: self.apply_filter())

        self.clear_filter_button = tk.Button(filter_group, text="❌", command=self.clear_filter)
        self.clear_filter_button.pack(side="left")

        # 🔒 Désactive le filtre au départ
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

        def center_volumes(event):
            canvas_width = event.width
            self.canvas.itemconfig(self.canvas_window, width=canvas_width)

        self.canvas.bind("<Configure>", center_volumes)
        self.vol_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        vol_frame_container.pack(fill="both", expand=True, padx=15, pady=(5, 15))
        # === Zone d'action regroupée : sélection + téléchargement
        action_frame = tk.Frame(self.root)
        action_frame.pack(pady=(10, 10))

        self.master_var = tk.BooleanVar(value=True)
        master_chk = tk.Checkbutton(
            action_frame,
            text="Tous/Aucun",
            variable=self.master_var,
            command=lambda: self.toggle_all_volumes(self.master_var.get()),
            bg="SystemButtonFace"
        )
        master_chk.pack(side="left", padx=10)

        invert_btn = tk.Button(
            action_frame,
            text="Inverser",
            command=self.invert_selection
        )
        invert_btn.pack(side="left", padx=10)

        self.dl_button = tk.Button(
            action_frame,
            text="Télécharger",
            command=self.download_selected,
            width=20,
            state="disabled"
        )
        self.dl_button.pack(side="left", padx=10)

        self.cancel_button = tk.Button(
            action_frame,
            text="Annuler",
            command=self.cancel_download,
            width=15,
            state="disabled"
        )
        self.cancel_button.pack(side="left", padx=10)

        # === Barre de progression ===
        progress_frame = tk.Frame(self.root)
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
        log_frame = tk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word", bg="white", font=("Segoe UI", 9))

        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
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
        self.update_cookie_status()
        url = self.url.get().strip()
        cookie = self.get_cookie(url)
        self.filter_text.set("")  # 🔄 vide le filtre au début de l'analyse
        try:
            self.title, self.pairs = fetch_manga_data(
                url, cookie, self.ua.get().strip()
            )
            threading.Thread(target=self.fetch_manga_image, args=(url,), daemon=True).start()
            MangaApp.last_url_used = url
            save_cookie_cache(
                {
                    "fr": self.cookie_fr.get().strip(),
                    "net": self.cookie_net.get().strip(),
                },
                self.ua.get().strip(),
                self.cbz_enabled.get(),
                self.flaresolverr_url.get().strip()
            )
        except Exception as e:
            self.log(f"Erreur : {str(e)}", level="error")
            self.toast("❌ Impossible de charger la liste")
            return

        for widget in self.vol_frame.winfo_children():
            widget.destroy()

        self.check_vars = []
        self.check_items = []
        # Centrage de la grille
        columns = 4
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

            self.check_items.append((chk, vol))  # ✅ bien ici, pas en dehors de la boucle

        self.dl_button.config(state="normal")
        self.canvas.yview_moveto(0)
        self.log("Liste chargée avec succès.", level="success")
        self.filter_entry.config(state="normal")
        self.clear_filter_button.config(state="normal")




    def toggle_all_volumes(self, state):
        for var in self.check_vars:
            var.set(state)
    
    def invert_selection(self):
        for var in self.check_vars:
            var.set(not var.get())

    def apply_filter(self):
        raw = self.filter_text.get().strip()

        # Si l'utilisateur tape "7*", on cherche 70 à 79, donc \b7\d\b ou \b7\d+
        if raw.endswith("*") and raw[:-1].isdigit():
            prefix = raw[:-1]
            pattern = rf"\b{prefix}\d\b|\b{prefix}\d\d*\b"
        else:
            # Fallback au filtre standard (match texte simple sans wildcard)
            pattern = re.escape(raw.lower())

        try:
            regex = re.compile(pattern)
        except re.error:
            self.log(f"❌ Filtre invalide : {raw}", level="error")
            return

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
        self.filter_text.set("")       # vide le champ de texte
        self.apply_filter()            # relance le filtre (qui affichera tout)

    def download_selected(self):
        self.cancel_event.clear()
        self.filter_entry.config(state="normal")
        self.clear_filter_button.config(state="normal")
        selected = []
        for (chk, label), (vol, link), var in zip(self.check_items, self.pairs, self.check_vars):
            if var.get() and chk.winfo_ismapped():  # => visible + sélectionné
                selected.append((vol, link))
        if not selected:
            self.log("Aucun volume sélectionné.", level="info")
            return

        self.dl_button.config(text="Téléchargement...", state="disabled")
        self.cancel_button.config(state="normal")
        self.filter_entry.config(state="disabled")
        self.clear_filter_button.config(state="disabled")
        self.progress.set(0)
        self.root.update_idletasks()

        def task():
            total = len(selected)
            failed = []

            self.image_progress_index = None  # reset pour le volume en cours

            def per_image_progress(done, total_images):
                nonlocal last_progress_update, last_progress_value
                now = time.time()
                last_progress_value = [done, total_images]

                # Rafraîchissement limité à une fois toutes les 0.5 secondes
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
                # 🔁 Mise à jour de la barre de progression visuelle
                percent = (done / total_images) * 100 if total_images else 0
                self.progress.set(percent)
                self.progress_label.config(text=f"{int(percent)}%")
                self.root.update_idletasks()
            last_progress_update = 0
            last_progress_value = [0, 0]  # [done, total]
            for idx, (vol, link) in enumerate(selected):
                start_time = time.time()
                if self.cancel_event.is_set():
                    break

                cookie = self.get_cookie(link)
                self.root.title(f"SushiDL - {vol}")
                self.log(f"📄 📥 Volume : {vol} | Lien : {link}", level="info")
                # Vérifie si le fichier CBZ existe déjà
                clean_title = sanitize_folder_name(self.title)
                clean_volume = sanitize_folder_name(vol)
                cbz_path = os.path.join(ROOT_FOLDER, clean_title, f"{clean_title} - {clean_volume}.cbz")

                if os.path.exists(cbz_path) and os.path.getsize(cbz_path) > 10_000:
                    self.log(f"⏩ CBZ déjà existant, saut de : {vol}", level="info")
                    continue  # passe au volume suivant


                # ✅ Réinitialisation progression
                self.image_progress_index = None
                self.progress.set(0)
                self.progress_label.config(text="0%")
                self.root.update_idletasks()
                last_progress_value = [0, 1]  # évite division par 0 par défaut

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
                    )

                    # ✅ Mise à jour finale propre de la progression
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
                        )
                    else:
                        self.log(f"❌ Retry échoué : {vol}", level="error")
                        retry_failed.append(vol)

                if retry_failed:
                    self.log(
                        f"⛔ Volumes définitivement échoués : {', '.join(retry_failed)}",
                        level="error",
                    )

            self.dl_button.config(text="Télécharger la sélection", state="normal")
            self.cancel_button.config(state="disabled")
            if self.cancel_event.is_set():
                self.log("Téléchargement annulé !", level="warning")
                self.progress.set(0)
            else:
                self.log("Tous les volumes ont été traités.", level="success")
            self.cancel_event.clear()

        threading.Thread(target=task).start()

    def cancel_download(self):
        self.cancel_event.set()
        self.log("Annulation demandée...", level="warning")
        self.cancel_button.config(state="disabled")

    def check_cookie_age_periodically(self):
        try:
            if os.path.exists(COOKIE_CACHE_PATH):
                with open(COOKIE_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                timestamp = datetime.datetime.fromisoformat(data.get("timestamp"))
                age = (
                    datetime.datetime.now(datetime.timezone.utc) - timestamp
                ).total_seconds()
                if age > 3600:
                    self.log("Cookie expiré depuis plus d'1h", level="warning")
        except Exception as e:
            self.log(f"Erreur vérification cookie: {e}", level="error")
        self.root.after(3600000, self.check_cookie_age_periodically)

    def update_flaresolverr_url(self):
        # Mettre à jour l'URL de FlareSolverr avec la nouvelle valeur entrée par l'utilisateur
        new_url = self.flaresolverr_url.get().strip()
        if new_url:
            MangaApp.flaresolverr_url_static = new_url
            self.log(f"FlareSolverr URL mis à jour : {new_url}", level="success")
        else:
            self.log("L'URL de FlareSolverr est vide.", level="error")

    def save_current_cookie(self):
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
            )
            self.log("Cookies, UA et FlareSolverr URL sauvegardés !", level="success")
            self.update_cookie_status()
        except Exception as e:
            self.log(f"Erreur sauvegarde: {e}", level="error")


if __name__ == "__main__":
    MangaApp()

    def update_global_progress(self, count_done):
        if self.total_chapters_to_process == 0:
            self.progress_var.set(0)
        else:
            pct = int((count_done / self.total_chapters_to_process) * 100)
            self.progress_var.set(pct)
