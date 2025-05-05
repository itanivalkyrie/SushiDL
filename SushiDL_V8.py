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
from PIL import Image
from curl_cffi import requests
from curl_cffi.requests.exceptions import Timeout
from zipfile import ZipFile
import time
import datetime


def fetch_with_flaresolverr(url, flaresolverr_url):
    print(f"[DEBUG] Appel FlareSolverr à : {flaresolverr_url}")
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    try:
        r = requests.post(
            flaresolverr_url.rstrip("/") + "/v1", json=payload, timeout=60
        )
        r.raise_for_status()
        solution = r.json()
        print("[FlareSolverr] ✅ Contournement réussi.")
        return solution.get("solution", {}).get("response", "")
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
    print(f"[DEBUG] 📡 Requête en cours : {url}")
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
            break
            return

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
    print(f"[DEBUG] {len(matches)} volumes/chapitres détectés")

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
    Version hybride : JSON + fallback HTML <img src>
    Bypass Cloudflare si nécessaire via FlareSolverr
    Compatible sushiscan.fr et .net, chapitres et volumes
    """
    attempt = 0
    while attempt <= retries:
        try:
            try:
                r = make_request(link, cookie, ua)
                if r.status_code != 200 or not r.text.strip():
                    raise Exception("Réponse vide ou erreur HTTP")

                r_text = r.text

                # Détection Cloudflare silencieux
                cloudflare_markers = [
                    "cf-challenge",
                    "cf-browser-verification",
                    "Just a moment",
                    "DDoS protection by Cloudflare",
                ]
                if any(marker in r_text for marker in cloudflare_markers):
                    raise Exception("Protection Cloudflare détectée")

            except Exception as e:
                print(f"[WARNING] Passage via FlareSolverr ({type(e).__name__})")
                r_text = fetch_with_flaresolverr(link, MangaApp.flaresolverr_url_static)

            # 🔍 Tentative JSON ts_reader.run(...)
            json_str = parse_lr(r_text, "ts_reader.run(", ");</script>", False)
            if json_str:
                try:
                    data = json.loads(json_str)
                    images = [
                        img.replace("http://", "https://")
                        for img in data["sources"][0]["images"]
                    ]
                    print(f"[DEBUG] ✅ {len(images)} images détectées via JSON.")
                    return images
                except Exception as e:
                    print(f"[Erreur] Parsing JSON : {e}")

            # 🔁 Fallback HTML brut
            img_urls = re.findall(
                r'<img[^>]+(?:src|data-src)=["\'](https://[^"\'>]+\.(?:webp|jpg|jpeg|png))["\']',
                r_text,
                re.IGNORECASE,
            )
            img_urls = list(dict.fromkeys(img_urls))  # supprime doublons

            if img_urls:
                print(
                    f"[DEBUG] ✅ {len(img_urls)} images détectées via balises <img> (fallback)."
                )
                return img_urls

            print("[WARNING] Aucun JSON ni image HTML trouvé.")
            return []

        except Exception as e:
            print(f"[Erreur] {type(e).__name__} : {e} (tentative {attempt+1})")

        attempt += 1
        if attempt <= retries:
            time.sleep(delay)

    print(
        f"[Échec définitif] ❌ Impossible d’accéder après {retries+1} tentatives : {link}"
    )
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
                logger(f"📦 CBZ créé : {cbz_path} ({size_mb} MB)", level="cbz")
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
        return {}, None, default_cbz, default_fs
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
    return {}, None, default_cbz, default_fs


class MangaApp:
    flaresolverr_url_static = "http://localhost:8191"

    def show_cookie_status(self):
        try:
            status = []
            for domain in ["fr", "net"]:
                cookie = getattr(self, f"cookie_{domain}").get().strip()
                status.append(f"Cookie .{domain}: {'✅' if cookie else '❌'}")
            self.cookie_status_label.config(text=" | ".join(status), fg="dark green")
        except Exception as e:
            self.log(f"Erreur statut cookies: {e}", level="error")

    def __init__(self):
        self.total_chapters_to_process = 0
        self.chapters_done = 0
        self.root = tk.Tk()
        self.root.title("SushiScan Manga Downloader")
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
        print(f"[DEBUG] FlareSolverr URL chargé : {MangaApp.flaresolverr_url_static}")
        self.ua.set(ua)
        self.cbz_enabled.set(cbz)
        self.url.set(last_url)  
        MangaApp.last_url_used = last_url
        self.check_vars = []
        self.image_progress_index = None  # pour mise à jour horizontale du log d'image
        self.pairs = []
        self.title = ""
        self.cancel_event = threading.Event()

        self.setup_ui()
        self.show_cookie_status()
        self.check_cookie_age_periodically()

        self.log("Application démarrée.", level="info")
        self.root.mainloop()

    def log(self, message, level="info"):
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
        # Frame principal
        frame = tk.Frame(self.root)
        frame.pack(padx=20, pady=10)

        font_label = ("Segoe UI Emoji", 10)
        font_entry = ("Segoe UI Emoji", 10)

        # Champs de saisie
        tk.Label(frame, text="URL du manga :", font=font_label).grid(
            row=0, column=0, sticky="w", pady=2
        )
        tk.Entry(frame, textvariable=self.url, width=50, font=font_entry).grid(
            row=0, column=1, pady=2
        )

        tk.Label(frame, text="Cookie cf_clearance (.fr):", font=font_label).grid(
            row=1, column=0, sticky="w", pady=2
        )
        tk.Entry(frame, textvariable=self.cookie_fr, width=50, font=font_entry).grid(
            row=1, column=1, pady=2
        )

        tk.Label(frame, text="Cookie cf_clearance (.net):", font=font_label).grid(
            row=2, column=0, sticky="w", pady=2
        )
        tk.Entry(frame, textvariable=self.cookie_net, width=50, font=font_entry).grid(
            row=2, column=1, pady=2
        )

        tk.Label(frame, text="User-Agent :", font=font_label).grid(
            row=3, column=0, sticky="w", pady=2
        )
        tk.Entry(frame, textvariable=self.ua, width=50, font=font_entry).grid(
            row=3, column=1, pady=2
        )

        tk.Label(frame, text="FlareSolverr URL :", font=font_label).grid(
            row=4, column=0, sticky="w", pady=2
        )
        tk.Entry(
            frame, textvariable=self.flaresolverr_url, width=50, font=font_entry
        ).grid(row=4, column=1, pady=2)

        # Statut cookies
        self.cookie_status_label = tk.Label(
            self.root, text="", font=("Segoe UI", 9), fg="green"
        )
        self.cookie_status_label.pack(pady=(0, 5))

        # Boutons
        tk.Button(
            self.root,
            text="Sauvegarder Paramètres",
            command=self.save_current_cookie,
            width=30,
        ).pack(pady=(0, 10))

        tk.Button(
            self.root, text="Analyser les volumes", command=self.load_volumes, width=30
        ).pack(pady=(0, 15))

        # Barre de filtre
        filter_frame = tk.Frame(self.root)
        filter_frame.pack(padx=20, pady=(0, 5), anchor="w")

        tk.Label(filter_frame, text="🔍 Filtrer :", font=("Segoe UI Emoji", 9)).pack(side="left")
        self.filter_text = tk.StringVar()
        entry = tk.Entry(filter_frame, textvariable=self.filter_text, width=30)
        entry.pack(side="left", padx=5)
        entry.bind("<KeyRelease>", lambda e: self.apply_filter())  # 🔄 appel automatique à chaque frappe
        tk.Button(filter_frame, text="❌ Effacer", command=self.clear_filter).pack(side="left", padx=(5, 0))

        tk.Label(self.root, text="Volume(s) | Chapitre(s)", font=("Segoe UI Emoji", 10, "bold"), fg="#444").pack(anchor="w", padx=20, pady=(10, 0))

        vol_frame_outer = tk.Frame(self.root, bg="white", bd=1, relief="ridge", highlightbackground="#ccc", highlightthickness=1)
        vol_frame_outer.pack(fill="both", expand=True, padx=15, pady=(5, 15))

        vol_frame_container = tk.Frame(vol_frame_outer, bg="white")
        vol_frame_container.pack(fill="both", expand=True, padx=5, pady=5)

        self.canvas = tk.Canvas(vol_frame_container, height=300, bg="white", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(vol_frame_container, orient="vertical", command=self.canvas.yview)


        self.vol_frame = tk.Frame(self.canvas, bg="white")
        self.vol_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.canvas.create_window((0, 0), window=self.vol_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Gestion du redimensionnement pour maintenir le centrage
        def resize_canvas(event):
            self.canvas.itemconfig("all", width=event.width)
            self.canvas.coords("all", event.width // 2, 0)

        self.canvas.bind("<Configure>", resize_canvas)

        # Contrôles
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=4)
        self.dl_button = tk.Button(
            btn_frame,
            text="Télécharger la sélection",
            command=self.download_selected,
            state="disabled",
            width=20,
        )
        self.dl_button.pack(side=tk.LEFT, padx=5)
        self.cancel_button = tk.Button(
            btn_frame,
            text="Annuler",
            command=self.cancel_download,
            state="disabled",
            width=20,
        )
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        tk.Checkbutton(
            self.root,
            text="Créer un fichier .cbz après téléchargement",
            variable=self.cbz_enabled,
        ).pack(pady=(0, 10))

        self.progress_bar = ttk.Progressbar(self.root, variable=self.progress, maximum=100)
        self.progress_bar.pack(fill="x", padx=20, pady=(0, 10))

        # Label intégré, fond transparent
        self.progress_label = ttk.Label(
            self.progress_bar,
            text="0%",
            font=("Segoe UI", 9, "bold"),
            anchor="center",
            background=""  # <- important !
        )
        self.progress_label.place(relx=0.5, rely=0.5, anchor="center")

        # Journal
        tk.Label(
            self.root, text="Journal", font=("Segoe UI Emoji", 10, "bold"), fg="#444"
        ).pack(anchor="w", padx=20, pady=(10, 0))
        log_frame = tk.Frame(
            self.root,
            bg="white",
            bd=1,
            relief="ridge",
            highlightbackground="#ccc",
            highlightthickness=1,
        )
        log_frame.pack(fill="both", expand=True, padx=15, pady=(5, 15))
        self.log_text = tk.Text(
            log_frame,
            height=12,
            bg="white",
            fg="black",
            font=("Segoe UI Emoji", 10),
            bd=0,
            spacing1=3,
        )
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_text.tag_config("info", foreground="deep sky blue")
        self.log_text.tag_config("success", foreground="spring green")
        self.log_text.tag_config("error", foreground="orange red")
        self.log_text.tag_config("warning", foreground="dark orange")
        self.log_text.tag_config("cbz", foreground="goldenrod")

    def get_cookie(self, url):
        """Sélectionne automatiquement le cookie selon le domaine"""
        if "sushiscan.fr" in url:
            return self.cookie_fr.get().strip()
        elif "sushiscan.net" in url:
            return self.cookie_net.get().strip()
        return ""

    def load_volumes(self):
        url = self.url.get().strip()
        cookie = self.get_cookie(url)
        try:
            self.title, self.pairs = fetch_manga_data(
                url, cookie, self.ua.get().strip()
            )
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

        self.check_vars.clear()

        # Configuration de la grille : 4 colonnes parfaitement centrées
        for i in range(4):
            self.vol_frame.columnconfigure(i, weight=1, uniform="col")

        # Checkbox "Tout sélectionner/déselectionner" centrée sur toute la largeur
        self.master_var = tk.BooleanVar(value=True)
        master_chk = tk.Checkbutton(
            self.vol_frame,
            text="Tout sélectionner/déselectionner",
            variable=self.master_var,
            command=lambda: self.toggle_all_volumes(self.master_var.get()),
        )
        master_chk.grid(row=0, column=0, columnspan=4, pady=5, sticky="ew")
        # Bouton "Inverser la sélection"
        invert_btn = tk.Button(
            self.vol_frame,
            text="Inverser la sélection",
            command=self.invert_selection
        )
        invert_btn.grid(row=1, column=0, columnspan=4, pady=(0, 10))
        # Ajout des volumes centrés dans leurs colonnes
        for i, (vol, link) in enumerate(self.pairs):
            var = tk.BooleanVar(value=True)
            self.check_vars.append(var)

            frame = tk.Frame(self.vol_frame)
            frame.grid(row=(i // 4) + 2, column=i % 4, padx=5, pady=5, sticky="ew")

            frame.columnconfigure(0, weight=1)  # Configuration pour centrage horizontal
            chk = tk.Checkbutton(frame, text=vol, variable=var, bg="white")
            chk.grid(sticky="ew")  # Remplissage horizontal pour centrer correctement

        self.dl_button.config(state="normal")
        self.canvas.yview_moveto(0)  # Repositionne l'ascenseur tout en haut
        self.toast("Liste chargée avec succès.")  # Affiche une notification

    def toggle_all_volumes(self, state):
        for var in self.check_vars:
            var.set(state)
    
    def invert_selection(self):
        for var in self.check_vars:
            var.set(not var.get())

    def apply_filter(self):
        keyword = self.filter_text.get().lower()
        widgets = self.vol_frame.winfo_children()[2:]  # ignore master_chk + bouton Inverser

        # Réinitialisation : tout réafficher si filtre vide
        if not keyword:
            for i, widget in enumerate(widgets):
                row = i // 4 + 2
                col = i % 4
                widget.grid(row=row, column=col, padx=5, pady=5, sticky="ew")
            return

        # Appliquer le filtre avec réorganisation
        row = 0
        col = 0
        for widget, (label, _) in zip(widgets, self.pairs):
            if keyword in label.lower():
                widget.grid(row=row + 2, column=col, padx=5, pady=5, sticky="ew")
                col += 1
                if col == 4:
                    col = 0
                    row += 1
            else:
                widget.grid_remove()
    def clear_filter(self):
        self.filter_text.set("")       # vide le champ de texte
        self.apply_filter()            # relance le filtre (qui affichera tout)

    def download_selected(self):
        self.cancel_event.clear()
        selected = [
            (vol, link)
            for (vol, link), var in zip(self.pairs, self.check_vars)
            if var.get()
        ]
        if not selected:
            self.log("Aucun volume sélectionné.", level="info")
            return

        self.dl_button.config(text="Téléchargement...", state="disabled")
        self.cancel_button.config(state="normal")
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

    def save_current_cookie(self):
        try:
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
            self.show_cookie_status()
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
