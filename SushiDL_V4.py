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
from zipfile import ZipFile
import time
import datetime

REGEX_URL = '^https://sushiscan.net/catalogue/[a-z0-9-]+/$'
ROOT_FOLDER = "DL SushiScan"
THREADS = 10
COOKIE_CACHE_PATH = "cookie_cache.json"

# --- Helpers ---
def sanitize_folder_name(name):
    return re.sub(r'[<>:"/\\|?*\n\r]', '_', name).strip()

def make_request(url, cookie, ua):
    headers = {
        'Accept': '*/*',
        'Accept-Language': 'fr-FR,fr;q=0.9',
        'Cookie': f'cf_clearance={cookie}',
        'User-Agent': ua
    }
    return requests.get(url, headers=headers, impersonate="chrome")

def parse_lr(text, left, right, recursive, unescape=True):
    pattern = re.escape(left) + '(.*?)' + re.escape(right)
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
    with ZipFile(cbz_name, 'w') as cbz:
        for root, _, files in os.walk(folder_path):
            for file in sorted(files):
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, folder_path)
                cbz.write(full_path, arcname)
    try:
        with ZipFile(cbz_name, 'r') as test_zip:
            test_zip.testzip()
    except Exception:
        return False
    if os.path.exists(cbz_name) and os.path.getsize(cbz_name) > 10000:
        shutil.rmtree(folder_path)
        return True
    return False

def download_image(url, folder, cookie, ua, i, number_len, cancel_event):
    if cancel_event.is_set():
        return
    headers = {
        'Accept': '*/*',
        'Accept-Language': 'fr-FR,fr;q=0.9',
        'Cookie': f'cf_clearance={cookie}',
        'User-Agent': ua
    }
    for attempt in range(3):
        if cancel_event.is_set():
            return
        r = requests.get(url, headers=headers, impersonate="chrome")
        if r.status_code == 200:
            ext = url.split('.')[-1].split('?')[0]
            filename = os.path.join(folder, str(i).zfill(number_len) + '.' + ext)
            with open(filename, 'wb') as f:
                f.write(r.content)
            final_path = convert_webp_to_jpg(filename)
            print(f"Image {i} ok : {final_path}")
            return
    print(f"[Erreur] Image non téléchargée : {url}")

def fetch_manga_data(url, cookie, ua):
    r = make_request(url, cookie, ua)
    if r.status_code != 200:
        raise Exception("Accès refusé ou URL invalide")
    html_content = r.text
    title = html.unescape(parse_lr(html_content, '<h1 class="entry-title" itemprop="name">', '</h1>', False))
    volumes = parse_lr(html_content, '<span class="chapternum"> ', '</span>', True)
    links = parse_lr(html_content, '<a href="', '">\n<span class="chapternum">', True)

    volumes.reverse()
    links.reverse()
    return title, list(zip(volumes, links))

def get_images(link, cookie, ua):
    r = make_request(link, cookie, ua)
    if r.status_code == 200:
        json_str = parse_lr(r.text, 'ts_reader.run(', ');</script>', False)
        try:
            data = json.loads(json_str)
            return [img.replace("http://", "https://") for img in data['sources'][0]['images']]
        except Exception as e:
            print(f"[Erreur] Parsing JSON : {e}")
            return []

def download_volume(volume, images, title, cookie, ua, logger, cancel_event, cbz_enabled=True):
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
        for i, url in enumerate(images):
            if cancel_event.is_set():
                break
            futures.append(executor.submit(download_image, url, folder, cookie, ua, i, number_len, cancel_event))
        
        for future in as_completed(futures):
            if cancel_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
    
    if not cancel_event.is_set() and os.path.exists(folder):
        if cbz_enabled:
            if archive_cbz(folder, title, volume):
                logger(f"CBZ créé : {clean_title} - {clean_volume}.cbz", level="cbz")
            else:
                logger(f"Échec de création CBZ pour {clean_volume}", level="warning")
        else:
            logger(f"CBZ non créé pour {clean_volume} (option décochée)", level="info")

def save_cookie_cache(cf_clearance, ua, cbz):
    data = {
        "cf_clearance": cf_clearance,
        "ua": ua,
        "cbz_enabled": cbz,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    with open(COOKIE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_cookie_cache():
    default_cbz = True
    if not os.path.exists(COOKIE_CACHE_PATH):
        return None, None, default_cbz
    try:
        with open(COOKIE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = datetime.datetime.fromisoformat(data.get("timestamp"))
        age = (datetime.datetime.now(datetime.timezone.utc) - timestamp).total_seconds()
        if age <= 86400:
            return data.get("cf_clearance"), data.get("ua"), data.get("cbz_enabled", default_cbz)
    except Exception as e:
        print(f"[Warning] Erreur lecture cache cookie : {e}")
    return None, None, default_cbz

class MangaApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SushiScan Manga Downloader")
        self.cbz_enabled = tk.BooleanVar(value=True)
        self.url = tk.StringVar()
        self.cookie = tk.StringVar()
        self.ua = tk.StringVar()
        cf, ua, cbz = load_cookie_cache()
        if cf and ua:
            self.cookie.set(cf)
            self.ua.set(ua)
        if cbz is not None:
            self.cbz_enabled.set(cbz)
        cf, ua, cbz = load_cookie_cache()
        if cf and ua:
            self.cookie.set(cf)
            self.ua.set(ua)
        self.check_vars = []
        self.pairs = []
        self.title = ""
        self.cancel_event = threading.Event()

        self.setup_ui()
        self.check_cookie_age_periodically()

        self.log("Application démarrée.", level="info")
        self.root.mainloop()

    def log(self, message, level="info"):
        emoji = {
            "info": "💬",
            "success": "✅", 
            "error": "🔴",
            "warning": "⚠️",
            "cbz": "📦"
        }.get(level, "")
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {emoji} {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", formatted, level)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def setup_ui(self):
        self.progress = tk.DoubleVar(value=0)

        # Frame principal pour les champs de saisie
        frame = tk.Frame(self.root)
        frame.pack(padx=20, pady=10)

        font_label = ("Segoe UI Emoji", 10)
        font_entry = ("Segoe UI Emoji", 10)

        # Configuration des champs de saisie
        tk.Label(frame, text="URL du manga :", font=font_label).grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(frame, textvariable=self.url, width=50, font=font_entry).grid(row=0, column=1, pady=2)

        tk.Label(frame, text="Cookie cf_clearance :", font=font_label).grid(row=1, column=0, sticky='w', pady=2)
        tk.Entry(frame, textvariable=self.cookie, width=50, font=font_entry).grid(row=1, column=1, pady=2)

        tk.Label(frame, text="User-Agent :", font=font_label).grid(row=2, column=0, sticky='w', pady=2)
        tk.Entry(frame, textvariable=self.ua, width=50, font=font_entry).grid(row=2, column=1, pady=2)

        # Statut du cookie et boutons de sauvegarde
        self.cookie_status = tk.Label(self.root, text="", font=("Segoe UI", 9), fg="green")
        self.cookie_status.pack(pady=(0, 5))
        
        tk.Button(self.root, 
                text="Sauver Cookie & User-Agent", 
                command=self.save_current_cookie, 
                width=30).pack(pady=(0, 10))

        # Bouton Analyser les volumes déplacé ici
        tk.Button(self.root, 
                text="Analyser les volumes", 
                command=self.load_volumes, 
                width=30).pack(pady=(0, 15))

        # Frame pour la sélection des volumes
        self.vol_frame = tk.Frame(self.root)
        self.vol_frame.pack(padx=10, pady=5)

        # Boutons de contrôle
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=4)
        self.dl_button = tk.Button(btn_frame, 
                                text="Télécharger la sélection", 
                                command=self.download_selected, 
                                state="disabled", 
                                width=20)
        self.dl_button.pack(side=tk.LEFT, padx=5)
        self.cancel_button = tk.Button(btn_frame, 
                                    text="Annuler", 
                                    command=self.cancel_download, 
                                    state="disabled", 
                                    width=20)
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        tk.Checkbutton(self.root, text="Créer un fichier .cbz après téléchargement", variable=self.cbz_enabled).pack(pady=(0, 10))

        # Barre de progression
        self.progress_bar = ttk.Progressbar(self.root, 
                                        variable=self.progress, 
                                        maximum=100)
        self.progress_bar.pack(fill='x', padx=20, pady=(0, 10))

        # Section du journal
        tk.Label(self.root, text="Journal", font=("Segoe UI Emoji", 10, "bold"), fg="#444").pack(anchor="w", padx=20, pady=(10, 0))
        log_frame = tk.Frame(self.root, bg="white", bd=1, relief="ridge", highlightbackground="#ccc", highlightthickness=1)
        log_frame.pack(fill="both", expand=True, padx=15, pady=(5, 15))
        self.log_text = tk.Text(log_frame, height=12, bg="white", fg="black", font=("Segoe UI Emoji", 10), bd=0, spacing1=3)
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.cbz_enabled.trace_add("write", lambda *_: self.auto_save_cbz())
        self.log_text.tag_config("info", foreground="deep sky blue")
        self.log_text.tag_config("success", foreground="spring green")
        self.log_text.tag_config("error", foreground="orange red")
        self.log_text.tag_config("warning", foreground="dark orange")
        self.log_text.tag_config("cbz", foreground="goldenrod")

    def load_volumes(self):
        try:
            self.title, self.pairs = fetch_manga_data(self.url.get().strip(), self.cookie.get().strip(), self.ua.get().strip())
        except Exception as e:
            self.log(f"Erreur : {str(e)}", level="error")
            return

        for widget in self.vol_frame.winfo_children():
            widget.destroy()

        self.check_vars.clear()
        
        self.master_var = tk.BooleanVar(value=True)
        master_chk = tk.Checkbutton(
            self.vol_frame,
            text="Tout sélectionner/déselectionner",
            variable=self.master_var,
            command=lambda: self.toggle_all_volumes(self.master_var.get())
        )
        master_chk.grid(row=0, column=0, columnspan=4, sticky='w', pady=5)

        for i, (vol, _) in enumerate(self.pairs):
            var = tk.BooleanVar(value=True)
            self.check_vars.append(var)
            tk.Checkbutton(self.vol_frame, text=vol, variable=var).grid(
                row=(i // 4) + 1,
                column=i % 4,
                sticky='w',
                padx=2
            )

        self.dl_button.config(state="normal")

    def toggle_all_volumes(self, state):
        for var in self.check_vars:
            var.set(state)

    def download_selected(self):
        self.cancel_event.clear()
        selected = [(vol, link) for (vol, link), var in zip(self.pairs, self.check_vars) if var.get()]
        if not selected:
            self.log("Aucun volume sélectionné.", level="info")
            return

        self.dl_button.config(text="Téléchargement...", state="disabled")
        self.cancel_button.config(state="normal")
        self.progress.set(0)
        self.root.update_idletasks()

        def task():
            total = len(selected)
            for idx, (vol, link) in enumerate(selected):
                if self.cancel_event.is_set():
                    break
                images = get_images(link, self.cookie.get().strip(), self.ua.get().strip())
                if images:
                    self.log(f"Début du volume: {vol}", level="info")
                    download_volume(vol, images, self.title, self.cookie.get().strip(), self.ua.get().strip(), self.log, self.cancel_event, self.cbz_enabled.get())
                else:
                    self.log(f"Aucune image trouvée pour {vol}", level="warning")
                self.progress.set(((idx + 1) / total) * 100)
                self.root.update_idletasks()

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
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
                age = (datetime.datetime.now(datetime.timezone.utc) - timestamp).total_seconds()
                if age > 3600:
                    self.cookie_status.config(text="⚠️ Cookie expiré depuis plus d'1h", fg="red")
                    self.log("Le cookie enregistré a plus de 1h. Pense à le renouveler.", level="warning")
                else:
                    expire = timestamp + datetime.timedelta(seconds=3600)
                    local_expire = expire.astimezone()
                    self.cookie_status.config(text=f"🕒 Cookie valide jusqu’à {local_expire.strftime('%H:%M:%S')}", fg="green")
        except Exception as e:
            self.log(f"Erreur vérification cookie périodique : {str(e)}", level="error")
        self.root.after(3600000, self.check_cookie_age_periodically)

    def save_current_cookie(self):
        save_cookie_cache(self.cookie.get().strip(), self.ua.get().strip(), self.cbz_enabled.get())
        self.log("Cookie & User-Agent sauvegardés pour réutilisation future.", level="success")

    def save_current_cookie(self):
        current_cbz = self.cbz_enabled.get()
        print(f"[Debug] Sauvegarde cbz_enabled = {current_cbz}")
        save_cookie_cache(self.cookie.get().strip(), self.ua.get().strip(), current_cbz)
        self.log("Cookie, User-Agent et option CBZ sauvegardés.", level="success")

    def auto_save_cbz(self):
        cbz = self.cbz_enabled.get()
        save_cookie_cache(self.cookie.get().strip(), self.ua.get().strip(), cbz)
        status = "activée ✅" if cbz else "désactivée ❌"
        self.log(f"Préférence CBZ sauvegardée automatiquement : {status}", level="info")
if __name__ == "__main__":
    MangaApp()