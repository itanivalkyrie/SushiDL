# -*- coding: utf-8 -*-
"""
SushiDL - Application de téléchargement de mangas depuis SushiScan.fr/net
Fonctionnalités principales :
- Contournement de la protection Cloudflare via les cookies cf_clearance
- Authentification manuelle via cookies par domaine et User-Agent
- Téléchargement multi-thread des images
- Conversion automatique WebP vers JPG
- Archivage CBZ des chapitres
- Interface graphique intuitive avec suivi de progression
"""

import os
import re
import html
import json
import csv
import base64
import shutil
import threading
import time
import datetime
import queue
import sys
import unicodedata
import webbrowser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image, ImageOps, ImageSequence, ImageTk
from curl_cffi import requests
from zipfile import ZipFile


def configure_console_io():
    """Configure la sortie console pour limiter les problèmes d'encodage."""
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


configure_console_io()


def repair_mojibake_text(text):
    """
    Tente de reparer un texte mojibake courant (UTF-8 lu en latin-1/cp1252).
    Applique plusieurs passes pour couvrir les doubles/triples decodages.
    """
    value = str(text or "")
    if not value:
        return value

    suspicious_markers = (
        "\u00C3",
        "\u00C2",
        "\u00E2\u20AC",
        "\u00F0\u0178",
        "\u00EF\u00BB\u00BF",
    )
    current = value
    for _ in range(4):
        if not any(marker in current for marker in suspicious_markers):
            break
        fixed = None
        for codec in ("latin-1", "cp1252"):
            try:
                candidate = current.encode(codec, errors="strict").decode("utf-8", errors="strict")
            except Exception:
                continue
            if candidate and candidate != current:
                fixed = candidate
                break
        if not fixed:
            break
        current = fixed
    return current


class DownloadCancelled(Exception):
    """Erreur levée lorsqu'une annulation utilisateur est demandée."""


class ImageDownloadError(Exception):
    """Erreur de téléchargement enrichie avec type et code HTTP."""

    def __init__(self, message, status_code=None, kind="retryable", phase="direct"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.phase = phase


def get_status_code_from_exception(exc):
    """Extrait un code HTTP depuis une exception réseau si disponible."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    return status_code


def classify_download_failure(status_code=None, message=""):
    """Classe les échecs de téléchargement pour piloter la stratégie de retry."""
    if status_code in (404, 410):
        return "missing"
    if status_code in (401, 403, 429, 500, 502, 503, 504):
        return "blocked_or_retryable"

    lower = (message or "").lower()
    if any(marker in lower for marker in ("cloudflare", "just a moment", "attention required", "captcha")):
        return "blocked_or_retryable"
    return "retryable"


def format_duration_short(seconds):
    """Formate une duree lisible courte (mm:ss ou hh:mm:ss)."""
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def recommend_action_for_failure(status_code=None, reason=""):
    """Suggère une action utilisateur adaptée à une cause d'échec."""
    lower = (reason or "").lower()
    normalized = "".join(
        ch for ch in unicodedata.normalize("NFKD", lower) if unicodedata.category(ch) != "Mn"
    )
    if "echec de creation cbz" in normalized or "archive_cbz" in normalized or "zip" in normalized:
        return "Vérifier l'espace disque, les droits d'écriture et le nom du fichier, puis relancer."
    if "dossier de tome introuvable" in normalized or "erreur creation dossier" in normalized:
        return "Vérifier le dossier de destination (chemin/droits), puis relancer."
    if "aucune image telechargee" in normalized:
        return "Vérifier l'URL, le cookie cf_clearance et le User-Agent, puis relancer l'analyse."
    if "recuperation images impossible" in normalized:
        return "Vérifier l'accès au chapitre (URL/cookie/User-Agent), puis relancer."
    if "tome non finalise" in normalized or "retry non finalise" in normalized:
        return "Corriger d'abord la cause technique indiquée, puis relancer."
    if status_code in (401, 403) or "cloudflare" in normalized or "forbidden" in normalized:
        return "Vérifier ou mettre à jour le cookie cf_clearance et le User-Agent."
    if status_code == 429:
        return "Limiter la cadence; attendre avant de relancer."
    if status_code in (500, 502, 503, 504):
        return "Erreur serveur temporaire; relancer plus tard."
    if status_code in (404, 410) or "not found" in normalized:
        return "Page absente côté serveur; ignorer cette page."
    if "timeout" in normalized or "dns" in normalized or "connexion" in normalized or "connection" in normalized:
        return "Vérifier la connexion réseau et relancer."
    return "Relancer le tome; si l'échec persiste, vérifier cookie et User-Agent."


def interruptible_sleep(cancel_event, duration):
    """Attend `duration` secondes, interrompu si annulation demandée."""
    if duration <= 0:
        return False
    if cancel_event is None:
        time.sleep(duration)
        return False
    return cancel_event.wait(duration)


def normalize_tome_label(label):
    """Normalise l'affichage des labels en remplaçant 'Volume' par 'Tome'."""
    cleaned = (label or "").strip()
    if not cleaned:
        return ""
    return re.sub(r"(?i)\bvolume\b", "Tome", cleaned)


def normalize_image_url(url):
    """Normalise les URLs d'images (https forcé, schéma manquant géré)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw.replace("http://", "https://")


def normalize_hostname(host):
    """Normalise un hostname en minuscule sans préfixe www."""
    value = (host or "").strip().lower()
    if value.startswith("www."):
        value = value[4:]
    return value


def get_supported_site_from_host(host):
    """Retourne le site supporté correspondant au host (ou chaîne vide)."""
    value = normalize_hostname(host)
    if not value:
        return ""
    if value == "sushiscan.fr" or value.endswith(".sushiscan.fr"):
        return "sushiscan.fr"
    if value == "sushiscan.net" or value.endswith(".sushiscan.net"):
        return "sushiscan.net"
    if value == "mangas-origines.fr" or value.endswith(".mangas-origines.fr"):
        return "mangas-origines.fr"
    if value == "hentai-origines.fr" or value.endswith(".hentai-origines.fr"):
        return "hentai-origines.fr"
    return ""


def get_supported_site_from_url(url):
    """Retourne le site supporté correspondant à une URL (ou chaîne vide)."""
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        host = ""
    return get_supported_site_from_host(host)


def get_site_root_url(url):
    """Retourne l'URL racine d'un site à partir d'une URL complète."""
    try:
        parsed = urlparse(url)
        host = normalize_hostname(parsed.hostname)
    except Exception:
        return ""
    if not host:
        return ""
    scheme = (parsed.scheme or "https").lower()
    return f"{scheme}://{host}/"


def get_sushiscan_domain_from_host(host):
    """Retourne 'fr' ou 'net' pour un host SushiScan (racine ou sous-domaine)."""
    site = get_supported_site_from_host(host)
    if site == "sushiscan.fr":
        return "fr"
    if site == "sushiscan.net":
        return "net"
    return ""


def get_sushiscan_domain_from_url(url):
    """Retourne 'fr' ou 'net' depuis une URL SushiScan (racine ou sous-domaine)."""
    return get_sushiscan_domain_from_host((urlparse(url).hostname or "").strip().lower() if url else "")


def get_cookie_domain_from_host(host):
    """Retourne le domaine cookie interne: fr/net/origines (ou chaîne vide)."""
    site = get_supported_site_from_host(host)
    mapping = {
        "sushiscan.fr": "fr",
        "sushiscan.net": "net",
        "mangas-origines.fr": "origines",
        "hentai-origines.fr": "hentai",
    }
    return mapping.get(site, "")


def get_cookie_domain_from_url(url):
    """Retourne le domaine cookie interne depuis une URL supportée."""
    return get_cookie_domain_from_host((urlparse(url).hostname or "").strip().lower() if url else "")


def is_valid_catalogue_url(url):
    """Valide une URL d'œuvre supportée avec slash final optionnel."""
    value = (url or "").strip()
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme.lower() != "https":
        return False

    site = get_supported_site_from_host(parsed.hostname)
    if not site:
        return False

    path = (parsed.path or "").strip()
    if site in ("sushiscan.fr", "sushiscan.net"):
        match = re.match(r"^/catalogue/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    elif site == "mangas-origines.fr":
        match = re.match(r"^/oeuvre/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    elif site == "hentai-origines.fr":
        match = re.match(r"^/manga/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    else:
        return False

    if not match:
        return False
    slug = match.group(1).strip()
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug, flags=re.IGNORECASE))


_HTTP_THREAD_LOCAL = threading.local()


def _get_http_session():
    """Retourne une session HTTP par thread pour reutiliser les connexions."""
    session = getattr(_HTTP_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _HTTP_THREAD_LOCAL.session = session
    return session


def _http_get(url, headers=None, timeout=10):
    """Requete GET avec session keep-alive et fallback direct."""
    session = _get_http_session()
    try:
        return session.get(url, headers=headers, impersonate="chrome", timeout=timeout)
    except Exception:
        return requests.get(url, headers=headers, impersonate="chrome", timeout=timeout)


def robust_download_image(img_url, headers, max_try=4, delay=2, cancel_event=None):
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
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        try:
            r = _http_get(img_url, headers=headers, timeout=20)
            status_code = getattr(r, "status_code", None)
            if status_code and status_code >= 400:
                kind = classify_download_failure(status_code, f"HTTP Error {status_code}")
                raise ImageDownloadError(
                    f"HTTP Error {status_code}",
                    status_code=status_code,
                    kind=kind,
                    phase="direct",
                )
            r.raise_for_status()
            raw = r.content

            # Détection HTML (Cloudflare/captcha au lieu d'une image)
            if raw[:6] == b'<html>' or b'<html' in raw[:1024].lower():
                raise ImageDownloadError(
                    "Réponse HTML (protection serveur ou Cloudflare)",
                    kind="blocked_or_retryable",
                    phase="direct",
                )

            # Vérifie si c'est bien une image (fail si corrompue/invalide)
            try:
                Image.open(BytesIO(raw))
            except Exception as test_e:
                runtime_log(
                    f"Tentative {attempt}: contenu non reconnu comme image: {test_e}",
                    level="warning",
                    context={"action": "image_integrity"},
                )
                last_exc = ImageDownloadError(
                    f"Contenu non image: {test_e}",
                    kind="retryable",
                    phase="direct",
                )
                if interruptible_sleep(cancel_event, delay * attempt):
                    raise DownloadCancelled("Téléchargement annulé.")
                continue

            # Succès - retourne les données brutes de l'image
            return raw

        except DownloadCancelled:
            raise
        except ImageDownloadError as e:
            runtime_log(
                f"Tentative {attempt} échouée pour {img_url}: {e}",
                level="warning",
                context={"action": "image_retry"},
            )
            last_exc = e
            if e.kind == "missing":
                raise e
            sleep_time = min(delay * (2 ** attempt), 60) if e.status_code in (403, 429) else (delay * attempt)
            if interruptible_sleep(cancel_event, sleep_time):
                raise DownloadCancelled("Téléchargement annulé.")
        except Exception as e:
            runtime_log(
                f"Tentative {attempt} échouée pour {img_url}: {e}",
                level="warning",
                context={"action": "image_retry"},
            )
            status_code = get_status_code_from_exception(e)
            kind = classify_download_failure(status_code, str(e))
            wrapped = ImageDownloadError(
                str(e),
                status_code=status_code,
                kind=kind,
                phase="direct",
            )
            last_exc = wrapped
            if kind == "missing":
                raise wrapped

            # Backoff exponentiel pour les erreurs 403/429
            if status_code in (403, 429):
                sleep_time = min(delay * (2 ** attempt), 60)  # Max 60 secondes
                if interruptible_sleep(cancel_event, sleep_time):
                    raise DownloadCancelled("Téléchargement annulé.")
            else:
                if interruptible_sleep(cancel_event, delay * attempt):
                    raise DownloadCancelled("Téléchargement annulé.")
    if isinstance(last_exc, Exception):
        raise last_exc
    raise ImageDownloadError(
        f"Impossible de télécharger l'image {img_url} après {max_try} tentatives.",
        kind="retryable",
        phase="direct",
    )


# Expressions régulières et constantes globales
APP_NAME = "SushiDL"
APP_VERSION = "11.2.7"
REGEX_URL = r"^https://(?:sushiscan\.(?:fr|net)/catalogue|mangas-origines\.fr/oeuvre|hentai-origines\.fr/manga)/[a-z0-9_-]+/?$"  # Formats d'URL valides
ROOT_FOLDER = "DL SushiScan"  # Dossier racine pour les téléchargements
THREADS = 3  # Nombre de threads pour le téléchargement parallèle
COOKIE_DOMAINS = ("fr", "net", "origines", "hentai")
COVER_RATIO_WIDTH = 2
COVER_RATIO_HEIGHT = 3
COVER_TARGET_HEIGHT = 150
BASE_DIR = Path(__file__).resolve().parent
COOKIE_CACHE_PATH = BASE_DIR / "cookie_cache.json"  # Fichier de cache pour les cookies
CONFIG_PATH = BASE_DIR / "config.json"  # Configuration globale de l'application
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
DIRECT_USER_AGENT_DEFAULT = DEFAULT_USER_AGENT
DEFAULT_APP_CONFIG = {
    "auth_mode": "manual",
    "manual_links": {
        "cookie_fr": "https://sushiscan.fr",
        "cookie_net": "https://sushiscan.net",
        "cookie_origines": "https://mangas-origines.fr",
        "cookie_hentai": "https://hentai-origines.fr",
        "user_agent": "https://httpbin.org/user-agent",
        "cookie_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
        "cloudflare_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
    },
}
CF_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "__cf_chl",
    "challenge-platform",
    "attention required",
)
LOG_LEVELS = ("debug", "info", "success", "warning", "error", "cbz")
LOG_EMOJIS = {
    "debug": "ðŸ”Ž",
    "info": "ðŸ’¬",
    "success": "âœ…",
    "warning": "âš ï¸",
    "error": "ðŸ”´",
    "cbz": "ðŸ“¦",
}
LOG_ANSI_COLORS = {
    "debug": "\033[90m",
    "info": "\033[36m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "cbz": "\033[35m",
}
ANSI_RESET = "\033[0m"
CONSOLE_USE_EMOJI = False
GUI_USE_EMOJI = False


def _merge_config(default_cfg, user_cfg):
    """Fusionne user_cfg dans default_cfg sans perdre les clés par défaut."""
    if not isinstance(default_cfg, dict):
        return user_cfg
    merged = {}
    safe_user = user_cfg if isinstance(user_cfg, dict) else {}
    for key, value in default_cfg.items():
        if isinstance(value, dict):
            merged[key] = _merge_config(value, safe_user.get(key, {}))
        elif isinstance(value, list):
            user_value = safe_user.get(key, value)
            merged[key] = user_value if isinstance(user_value, list) else list(value)
        else:
            merged[key] = safe_user.get(key, value)
    for key, value in safe_user.items():
        if key not in merged:
            merged[key] = value
    return merged


def _write_json_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_app_config():
    """Charge config.json et applique les valeurs par défaut manquantes."""
    if not CONFIG_PATH.exists():
        cfg = dict(DEFAULT_APP_CONFIG)
        _write_json_file(CONFIG_PATH, cfg)
        return cfg
    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        merged = _merge_config(DEFAULT_APP_CONFIG, raw)
        if merged != raw:
            _write_json_file(CONFIG_PATH, merged)
        return merged
    except Exception as exc:
        try:
            print(f"[WARN] Erreur lecture config.json ({exc}), valeurs par défaut utilisées.")
        except Exception:
            pass
        return dict(DEFAULT_APP_CONFIG)


APP_CONFIG = load_app_config()


def get_manual_link(config_key, default_value):
    """Retourne un lien manuel depuis config.json (ou valeur par defaut)."""
    links = APP_CONFIG.get("manual_links", {}) if isinstance(APP_CONFIG, dict) else {}
    if not isinstance(links, dict):
        return default_value
    raw = (links.get(config_key) or "").strip()
    return raw or default_value


def strip_console_unsafe_chars(text):
    """Retire certains symboles non ASCII (notamment emojis) en console Windows."""
    value = repair_mojibake_text(text or "")
    if os.name != "nt":
        return value

    # Supprime les emojis pour éviter les glyphes non supportés.
    value = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\ufe0f]", "", value)

    # Si la console est bien en UTF, on conserve les accents.
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in encoding:
        return value

    # Fallback consoles legacy: translittération ASCII.
    value = unicodedata.normalize("NFKD", value)
    return value.encode("ascii", errors="ignore").decode("ascii", errors="ignore")


def normalize_log_level(level):
    """Normalise un niveau de log supporté."""
    candidate = (level or "info").strip().lower()
    return candidate if candidate in LOG_LEVELS else "info"


def format_log_context(context):
    """Formate un contexte de log lisible et stable."""
    if not context:
        return ""
    if isinstance(context, str):
        value = context.strip()
        return f" [{value}]" if value else ""
    if isinstance(context, dict):
        ordered_keys = ("domain", "tome", "action")
        parts = []
        for key in ordered_keys:
            value = str(context.get(key, "")).strip()
            if value:
                parts.append(f"{key}={value}")
        for key, value in context.items():
            if key in ordered_keys:
                continue
            value_txt = str(value).strip()
            if value_txt:
                parts.append(f"{key}={value_txt}")
        if parts:
            return " [" + " | ".join(parts) + "]"
    return ""


def console_supports_color():
    """Retourne True si la console semble supporter ANSI."""
    if os.getenv("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def format_console_line(message, level="info", context=None, timestamp=None, with_emoji=True):
    """Construit une ligne de log homogène pour la console."""
    lvl = normalize_log_level(level)
    ts = timestamp or time.strftime("%H:%M:%S")
    emoji = (LOG_EMOJIS.get(lvl, "") + " ") if with_emoji else ""
    safe_message = strip_console_unsafe_chars(repair_mojibake_text(message))
    ctx = format_log_context(context)
    return f"[{ts}] {emoji}{safe_message}{ctx}"


def emit_console_log(message, level="info", context=None, timestamp=None, with_emoji=None):
    """Écrit un log homogène en console, avec couleur si possible."""
    if with_emoji is None:
        with_emoji = CONSOLE_USE_EMOJI
    line = format_console_line(
        message=message,
        level=level,
        context=context,
        timestamp=timestamp,
        with_emoji=with_emoji,
    )
    lvl = normalize_log_level(level)
    if console_supports_color():
        color = LOG_ANSI_COLORS.get(lvl, "")
        if color:
            print(f"{color}{line}{ANSI_RESET}")
            return
    print(line)


def runtime_log(message, level="info", context=None):
    """
    Route un message vers le logger GUI quand disponible,
    sinon vers la console uniquement.
    """
    text = repair_mojibake_text(str(message or "").strip())
    if not text:
        return

    app_cls = globals().get("MangaApp")
    app = getattr(app_cls, "current_instance", None) if app_cls is not None else None
    if app is not None and hasattr(app, "log"):
        app.log(text, level=level, context=context)
        return
    emit_console_log(text, level=level, context=context)


def is_cloudflare_challenge_page(content):
    """Détecte une page de challenge Cloudflare."""
    text = (content or "").lower()
    if not text:
        return True
    return any(marker in text for marker in CF_CHALLENGE_MARKERS)


def strip_html_tags(text):
    """Supprime les balises HTML d'une chaîne."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


# --- Fonctions utilitaires ---
def sanitize_folder_name(name):
    """Nettoie les noms de dossier en supprimant les caractères invalids"""
    return re.sub(r'[<>:"/\\|?*\n\r]', "_", name).strip()


def make_request(url, cookie, ua):
    """Effectue une requête HTTP avec les cookies et l'user-agent appropriés"""
    headers = {
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": ua or DEFAULT_USER_AGENT,
        "Accept-Encoding": "gzip, deflate, br",
    }
    referer_root = get_site_root_url(url)
    if referer_root:
        headers["Referer"] = referer_root

    cookie_header = ""
    app = getattr(MangaApp, "current_instance", None)
    if app and hasattr(app, "get_cookie_header_for_url"):
        try:
            cookie_header = app.get_cookie_header_for_url(url, fallback_cookie=cookie)
        except Exception as exc:
            runtime_log(
                f"Impossible de calculer l'en-tete Cookie: {exc}",
                level="debug",
                context={"action": "make_request"},
            )
            cookie_header = ""

    if not cookie_header and cookie:
        cookie_header = f"cf_clearance={cookie}"
    if cookie_header:
        headers["Cookie"] = cookie_header
    return _http_get(url, headers=headers, timeout=10)


def detect_local_user_agent():
    """
    Tente de générer un User-Agent local cohérent avec le navigateur principal.
    Retourne (ua, source).
    """
    # Base stable pour Chrome/Edge sur Windows.
    if os.name == "nt":
        browser_keys = [
            ("chrome", r"SOFTWARE\Google\Chrome\BLBeacon"),
            ("edge", r"SOFTWARE\Microsoft\Edge\BLBeacon"),
        ]
        version = ""
        source = "fallback"
        try:
            import winreg

            for browser_name, reg_path in browser_keys:
                for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(root, reg_path) as key:
                            raw_version, _ = winreg.QueryValueEx(key, "version")
                            clean = str(raw_version or "").strip()
                            if clean:
                                version = clean
                                source = f"registre:{browser_name}"
                                break
                    except Exception:
                        continue
                if version:
                    break
        except Exception:
            pass

        if not version:
            version = "127.0.0.0"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )
        return ua, source

    # Fallback non Windows
    return DEFAULT_USER_AGENT, "fallback"


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


def test_cookie_validity(domain, cookie, ua, probe_url=None):
    """
    Vérifie si un cookie cf_clearance est encore valide
    
    Args:
        domain (str): Domaine à tester (.fr ou .net)
        cookie (str): Valeur du cookie cf_clearance
        ua (str): User-Agent à utiliser
    
    Returns:
        bool: True si le cookie est valide, False sinon
    """
    if not cookie:
        return False
    status = evaluate_cookie_and_challenge(domain, cookie, ua, probe_url=probe_url)
    return bool(status.get("cookie_valid", False))


def evaluate_cookie_and_challenge(domain, cookie, ua, probe_url=None):
    """
    Evalue l'etat cookie + challenge Cloudflare.
    Retourne:
      - cookie_valid: bool
      - challenge_state: "present" | "absent" | "unknown"
      - http_status: int|None
    """
    result = {"cookie_valid": False, "challenge_state": "unknown", "http_status": None}
    if domain not in ("fr", "net"):
        return result

    test_url = probe_url or f"https://sushiscan.{domain}/"
    expected_host = f"sushiscan.{domain}"
    if expected_host not in test_url:
        test_url = f"https://{expected_host}/"

    try:
        r = make_request(test_url, cookie or "", ua)
        status_code = int(getattr(r, "status_code", 0) or 0)
        result["http_status"] = status_code or None
        text = (getattr(r, "text", "") or "").lower()

        has_content_markers = any(
            marker in text
            for marker in (
                "sushiscan",
                "entry-title",
                "wp-manga",
                "chapternum",
                "readerarea",
                "ts_reader.run",
            )
        )
        challenge_blocking = is_cloudflare_challenge_page(text) and not has_content_markers

        if status_code == 200 and not challenge_blocking:
            result["challenge_state"] = "absent"
            result["cookie_valid"] = bool((cookie or "").strip())
            return result

        if challenge_blocking or status_code in (401, 403, 429, 503):
            result["challenge_state"] = "present"
        else:
            result["challenge_state"] = "unknown"
        result["cookie_valid"] = False
        return result
    except Exception as exc:
        runtime_log(
            f"Test cookie/challenge non concluant: {exc}",
            level="debug",
            context={"action": "cookie_probe", "domain": domain},
        )
        return result


def interpret_curl_error(message):
    """Traduit les erreurs cURL en messages compréhensibles"""
    if "curl: (6)" in message:
        return "Nom d'hôte introuvable (DNS)."
    elif "curl: (7)" in message:
        return "Connexion refusée ou impossible (serveur hors ligne ?)."
    elif "curl: (28)" in message:
        return "Délai d'attente dépassé (timeout réseau)."
    elif "curl: (35)" in message:
        return "Erreur SSL/TLS lors de la connexion sécurisée."
    elif "curl: (56)" in message:
        return "Connexion interrompue (réponse incomplète ou terminée prématurément)."
    else:
        return None


def archive_cbz(folder_path, title, volume, remove_source=True):
    """
    Crée une archive CBZ à partir d'un dossier d'images
    
    Args:
        folder_path (str): Chemin du dossier contenant les images
        title (str): Titre du manga
        volume (str): Libellé tome/chapitre
    
    Returns:
        bool: True si l'archivage a réussi, False sinon
    """
    clean_title = sanitize_folder_name(title)
    clean_volume = sanitize_folder_name(normalize_tome_label(volume))
    parent_dir = os.path.dirname(folder_path)
    cbz_name = os.path.join(parent_dir, f"{clean_title} - {clean_volume}.cbz")
    
    try:
        # Création de l'archive ZIP
        with ZipFile(cbz_name, "w") as cbz:
            for root, _, files in os.walk(folder_path):
                for file in sorted(files):  # Tri alphabétique pour l'ordre des pages
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, folder_path)
                    cbz.write(full_path, arcname)
    except Exception:
        return False
    
    # Vérification de l'intégrité de l'archive
    try:
        with ZipFile(cbz_name, "r") as test_zip:
            corrupt_member = test_zip.testzip()
            if corrupt_member:
                return False
            image_members = [
                name
                for name in test_zip.namelist()
                if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif"))
            ]
            if not image_members:
                return False
    except Exception:
        return False
    
    # Suppression du dossier original si l'archive est valide
    try:
        if os.path.exists(cbz_name) and os.path.getsize(cbz_name) > 10000:
            if remove_source:
                shutil.rmtree(folder_path)
            return True
    except Exception:
        return False
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

    normalized_url = normalize_image_url(url)

    def register_failure(kind, reason, status_code=None):
        failed_downloads.append(
            {
                "url": normalized_url,
                "kind": kind,
                "status_code": status_code,
                "reason": str(reason),
            }
        )

    if cancel_event.is_set():
        register_failure("cancelled", "Annulation demandée avant téléchargement.")
        return

    # Configuration des en-têtes HTTP
    referer = referer_url or get_site_root_url(normalized_url) or "https://sushiscan.net/"
    app = getattr(MangaApp, "current_instance", None)
    cookie_header = ""
    if app and hasattr(app, "get_cookie_header_for_url"):
        try:
            cookie_header = app.get_cookie_header_for_url(normalized_url, fallback_cookie=cookie)
        except Exception as exc:
            runtime_log(
                f"Impossible de calculer le cookie d'image: {exc}",
                level="debug",
                context={"action": "download_image"},
            )
            cookie_header = ""
    if not cookie_header and cookie:
        cookie_header = f"cf_clearance={cookie}"

    headers = {
        "Accept": "image/webp,image/jpeg,image/png,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": ua,
        "Referer": referer,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    # Détermination de l'extension et du nom de fichier
    parsed_path = (urlparse(normalized_url).path or "").lower()
    ext = parsed_path.rsplit(".", 1)[-1] if "." in parsed_path else "jpg"
    if ext not in {"jpg", "jpeg", "png", "webp", "avif"}:
        ext = "jpg"
    filename = os.path.join(folder, f"{str(i + 1).zfill(number_len)}.{ext}")

    # Téléchargement direct prioritaire
    try:
        raw = robust_download_image(normalized_url, headers, cancel_event=cancel_event)
        with open(filename, "wb") as f:
            f.write(raw)

        # Conversion WebP vers JPG si activée
        if webp2jpg_enabled and filename.lower().endswith(".webp"):
            try:
                img = Image.open(filename).convert("RGB")
                new_path = filename[:-5] + ".jpg"
                img.save(new_path, "JPEG", quality=90)
                os.remove(filename)
                filename = new_path
            except Exception as conv_e:
                runtime_log(f"Erreur conversion WebP->JPG: {conv_e}", level="warning", context={"action": "webp2jpg"})

        # Mise à jour de la progression
        if progress_callback:
            progress_callback(i + 1)
        if hasattr(MangaApp, "current_instance") and hasattr(MangaApp.current_instance, "log"):
            MangaApp.current_instance.log(
                f"Image {i + 1} téléchargée : {os.path.basename(filename)}",
                level="info",
            )
        return

    except DownloadCancelled:
        register_failure("cancelled", "Annulation demandée pendant téléchargement direct.")
        return
    except ImageDownloadError as e:
        if e.kind == "missing":
            register_failure("missing", str(e), status_code=e.status_code)
            runtime_log(
                f"Image absente côté serveur (HTTP {e.status_code}): {normalized_url}",
                level="info",
                context={"action": "download", "url": normalized_url},
            )
            return
        register_failure(e.kind, str(e), status_code=e.status_code)
        runtime_log(
            f"Échec direct après retries: {e}",
            level="warning",
            context={"action": "download", "url": normalized_url},
        )
        return
    except Exception as e:
        status_code = get_status_code_from_exception(e)
        kind = classify_download_failure(status_code, str(e))
        register_failure(kind, str(e), status_code=status_code)
        runtime_log(
            f"Échec direct après retries: {e}",
            level="warning",
            context={"action": "download", "url": normalized_url},
        )
        return


def extract_manga_title_from_html(url, html_content):
    """Extrait un titre de manga/œuvre depuis le HTML source."""
    html_content = html_content or ""
    soup = BeautifulSoup(html_content, "html.parser")
    title_tag = soup.select_one("h1.entry-title, .post-title h1, .summary__content h1, h1")
    if title_tag:
        title = title_tag.get_text(" ", strip=True)
        if title:
            return title
    parsed_title = parse_lr(
        html_content, '<h1 class="entry-title" itemprop="name">', "</h1>", False
    )
    title = html.unescape(parsed_title) if parsed_title else ""
    if title:
        return title

    path = (urlparse(url).path or "").strip("/")
    if path:
        return path.split("/")[-1].replace("-", " ").strip() or "Sans titre"
    return "Sans titre"


def parse_manga_data_from_html(url, html_content, emit_logs=True):
    """
    Parse le HTML du catalogue et retourne (title, pairs).
    """
    html_content = html_content or ""

    soup = BeautifulSoup(html_content, "html.parser")

    # Extraction du titre (multi-sites)
    title = extract_manga_title_from_html(url, html_content)

    source_site = get_supported_site_from_url(url)
    source_host = normalize_hostname(urlparse(url).hostname)
    path_value = (urlparse(url).path or "").strip()
    source_slug = ""
    if source_site in ("sushiscan.fr", "sushiscan.net"):
        match_slug = re.match(r"^/catalogue/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    elif source_site == "mangas-origines.fr":
        match_slug = re.match(r"^/oeuvre/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    elif source_site == "hentai-origines.fr":
        match_slug = re.match(r"^/manga/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    pairs = []

    def is_same_site(full_link):
        link_site = get_supported_site_from_url(full_link)
        if source_site and link_site:
            return source_site == link_site
        link_host = normalize_hostname(urlparse(full_link).hostname)
        return bool(source_host and link_host and source_host == link_host)

    # 1) Structure classique avec span.chapternum
    matches = re.findall(
        r'<a href="([^"]+)">\s*<span class="chapternum">(.*?)</span>',
        html_content,
        re.IGNORECASE | re.DOTALL,
    )
    for href, label in matches:
        full_link = urljoin(url, href.strip())
        if not is_same_site(full_link):
            continue
        clean_label = normalize_tome_label(strip_html_tags(html.unescape(label)))
        if clean_label:
            pairs.append((clean_label, full_link))

    # 2) Fallback sur liste de chapitres
    if not pairs:
        for a in soup.select("li.wp-manga-chapter a[href], .listing-chapters_wrap a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            full_link = urljoin(url, href)
            if not is_same_site(full_link):
                continue
            label = normalize_tome_label(a.get_text(" ", strip=True))
            if label:
                pairs.append((label, full_link))

    # 3) Fallback regex ciblé pour les pages où la liste est injectée côté script.
    if not pairs and source_site in ("mangas-origines.fr", "hentai-origines.fr") and source_slug:
        if source_site == "mangas-origines.fr":
            site_host = "mangas-origines.fr"
            path_prefix = "oeuvre"
        else:
            site_host = "hentai-origines.fr"
            path_prefix = "manga"
        pattern_abs = re.compile(
            rf"https?://(?:www\.)?{re.escape(site_host)}/{path_prefix}/{re.escape(source_slug)}/(chapitre-[^\"'<>/\s]+(?:/[^\"'<>/\s]+)*)/?",
            flags=re.IGNORECASE,
        )
        pattern_rel = re.compile(
            rf"/{path_prefix}/{re.escape(source_slug)}/(chapitre-[^\"'<>/\s]+(?:/[^\"'<>/\s]+)*)/?",
            flags=re.IGNORECASE,
        )
        chapter_links = []

        for match in pattern_abs.finditer(html_content):
            chapter_slug = (match.group(1) or "").strip("/")
            link = f"https://{site_host}/{path_prefix}/{source_slug}/{chapter_slug}/"
            chapter_links.append(link)

        for match in pattern_rel.finditer(html_content):
            chapter_slug = (match.group(1) or "").strip("/")
            link = f"https://{site_host}/{path_prefix}/{source_slug}/{chapter_slug}/"
            chapter_links.append(link)

        seen_links = set()
        for link in chapter_links:
            if link in seen_links:
                continue
            seen_links.add(link)
            slug = (urlparse(link).path or "").strip("/").split("/")[-1]
            label = normalize_tome_label(slug.replace("-", " ").strip().title())
            if label:
                pairs.append((label, link))

    # Élimination des doublons
    seen = set()
    unique_pairs = []
    for label, link in pairs:
        if link in seen:
            continue
        seen.add(link)
        unique_pairs.append((label, link))

    if not unique_pairs:
        raise Exception("Aucun tome/chapitre détecté (page protégée ou structure modifiée).")

    unique_pairs.reverse()  # Pour afficher dans l'ordre croissant
    if emit_logs:
        runtime_log(
            f"{len(unique_pairs)} tomes/chapitres détectés",
            level="info",
            context={"action": "parse_catalogue"},
        )
    return title, unique_pairs


def fetch_mangas_origines_chapters_via_ajax(url, cookie, ua, emit_logs=True):
    """Récupère les chapitres via l'endpoint AJAX Madara (mangas/hentai-origines)."""
    site = get_supported_site_from_url(url)
    if site not in ("mangas-origines.fr", "hentai-origines.fr"):
        return []

    parsed = urlparse(url)
    base_url = (url if url.endswith("/") else f"{url}/").strip()
    if site == "mangas-origines.fr":
        path_prefix = "oeuvre"
    else:
        path_prefix = "manga"

    match_slug = re.match(rf"^/{path_prefix}/([^/?#]+)/?$", parsed.path or "", flags=re.IGNORECASE)
    if not match_slug:
        return []
    source_slug = (match_slug.group(1) or "").strip().lower()
    if not source_slug:
        return []

    app = getattr(MangaApp, "current_instance", None)
    cookie_header = ""
    if app and hasattr(app, "get_cookie_header_for_url"):
        try:
            cookie_header = app.get_cookie_header_for_url(url, fallback_cookie=cookie)
        except Exception:
            cookie_header = ""
    if not cookie_header and cookie:
        cookie_header = f"cf_clearance={cookie}"

    headers = {
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": ua or DEFAULT_USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base_url,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    pairs = []
    max_page = 1
    page = 1
    while page <= max_page:
        endpoint = urljoin(base_url, f"ajax/chapters/?t={page}")
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                data={},
                impersonate="chrome",
                timeout=15,
            )
        except Exception:
            break
        if int(getattr(response, "status_code", 0) or 0) != 200:
            break

        html_part = response.text or ""
        soup = BeautifulSoup(html_part, "html.parser")

        # Détecte les pages disponibles (si pagination côté site).
        page_ids = []
        for a in soup.select(".listing-chapters_wrap .pagination .page a[data-page]"):
            raw_page = (a.get("data-page") or "").strip()
            if raw_page.isdigit():
                page_ids.append(int(raw_page))
        if page_ids:
            max_page = max(max_page, max(page_ids))

        for a in soup.select("li.wp-manga-chapter a[href], .listing-chapters_wrap a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            full_link = urljoin(base_url, href)
            if get_supported_site_from_url(full_link) != site:
                continue
            link_path = (urlparse(full_link).path or "").lower()
            if f"/{path_prefix}/{source_slug}/" not in link_path:
                continue
            label = normalize_tome_label(a.get_text(" ", strip=True))
            if not label:
                chapter_slug = link_path.strip("/").split("/")[-1]
                label = normalize_tome_label(chapter_slug.replace("-", " ").strip().title())
            if label:
                pairs.append((label, full_link))

        page += 1

    seen = set()
    unique_pairs = []
    for label, link in pairs:
        if link in seen:
            continue
        seen.add(link)
        unique_pairs.append((label, link))

    if unique_pairs:
        unique_pairs.reverse()
        if emit_logs:
            log_domain = "origines" if site == "mangas-origines.fr" else "hentai"
            runtime_log(
                f"{len(unique_pairs)} tomes/chapitres détectés via AJAX.",
                level="info",
                context={"action": "parse_catalogue_ajax", "domain": log_domain},
            )
    return unique_pairs


def fetch_manga_data(url, cookie, ua, return_html=False, progress_callback=None, emit_logs=True):
    """
    Récupère les données d'un manga : titre et liste des tomes/chapitres
    
    Args:
        url (str): URL de la page catalogue du manga
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
    
    Returns:
        tuple: (titre, liste de tuples (label, url))
    """
    if callable(progress_callback):
        progress_callback("fetch")
    r = make_request(url, cookie, ua)
    if r.status_code != 200:
        final_url = getattr(r, "url", "") or ""
        detail = f"HTTP {r.status_code}"
        if final_url and final_url != url:
            detail += f" -> {final_url}"
        if int(getattr(r, "status_code", 0) or 0) == 403:
            if get_cookie_domain_from_url(url) in COOKIE_DOMAINS:
                detail += " | Vérifie le cookie cf_clearance du domaine"
            else:
                detail += " | Vérifie l'accès au site (protection ou blocage)"
        raise Exception(f"Accès refusé ou URL invalide ({detail})")

    html_content = r.text or ""
    if callable(progress_callback):
        progress_callback("parse")
    site = get_supported_site_from_url(url)
    if site in ("mangas-origines.fr", "hentai-origines.fr"):
        parse_error = None
        try:
            title, pairs = parse_manga_data_from_html(
                url,
                html_content,
                emit_logs=False,
            )
        except Exception as exc:
            parse_error = exc
            title = extract_manga_title_from_html(url, html_content)
            pairs = []

        ajax_pairs = fetch_mangas_origines_chapters_via_ajax(url, cookie, ua, emit_logs=emit_logs)
        if ajax_pairs:
            pairs = ajax_pairs
            if not title:
                title = extract_manga_title_from_html(url, html_content)
        elif not pairs:
            if parse_error is not None:
                raise parse_error
            raise Exception("Aucun tome/chapitre détecté sur ce site.")
    else:
        title, pairs = parse_manga_data_from_html(
            url,
            html_content,
            emit_logs=emit_logs,
        )
    if return_html:
        return title, pairs, html_content
    return title, pairs


def build_mangas_origines_list_url(url):
    """Construit l'URL chapitre en mode liste (?style=list) pour les sites Origines."""
    site = get_supported_site_from_url(url)
    if site not in ("mangas-origines.fr", "hentai-origines.fr"):
        return (url or "").strip()

    parsed = urlparse((url or "").strip())
    host = normalize_hostname(parsed.hostname)
    path = (parsed.path or "").strip()
    if not path:
        return (url or "").strip()

    # Si l'URL est en mode paginé (.../p/2/), revenir sur l'URL chapitre canonique.
    path = re.sub(r"/p/\d+/?$", "/", path, flags=re.IGNORECASE)
    if not path.endswith("/"):
        path = f"{path}/"

    query_pairs = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True) if (k or "").lower() != "style"]
    query_pairs.insert(0, ("style", "list"))
    query = urlencode(query_pairs, doseq=True)
    scheme = (parsed.scheme or "https").lower()
    return urlunparse((scheme, host, path, "", query, ""))


def get_images(link, cookie, ua, retries=3, delay=2, debug_mode=False):
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
            runtime_log(
                f"{removed} image(s) parasite(s) supprimée(s) dynamiquement.",
                level="debug",
                context={"action": "image_filter", "domain": domain},
            )
        return filtered

    def extract_images(r_text, domain):
        """Extrait les URLs d'images depuis le contenu HTML"""
        def dedupe_images(items):
            seen = set()
            unique = []
            for item in items or []:
                if item in seen:
                    continue
                seen.add(item)
                unique.append(item)
            return unique

        def dedupe_entries(entries):
            seen = set()
            unique = []
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                url_value = (entry.get("url") or "").strip()
                if not url_value or url_value in seen:
                    continue
                seen.add(url_value)
                unique.append(entry)
            return unique

        def parse_int(raw_value):
            value = (str(raw_value or "").strip() or "0")
            if not value.isdigit():
                return 0
            return int(value)

        def median_value(values):
            filtered_values = sorted(v for v in values if v > 0)
            if not filtered_values:
                return 0
            mid = len(filtered_values) // 2
            if len(filtered_values) % 2:
                return filtered_values[mid]
            return (filtered_values[mid - 1] + filtered_values[mid]) / 2

        def looks_like_image_url(value):
            candidate = normalize_image_url((value or "").strip())
            if not candidate or candidate.startswith("data:"):
                return ""
            parsed_path = (urlparse(candidate).path or "").lower()
            if parsed_path.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")):
                return candidate
            return ""

        def collect_images_from_container(container):
            if container is None:
                return []
            collected = []
            for img in container.find_all("img"):
                src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or img.get("data-cfsrc")
                normalized_src = looks_like_image_url(src)
                if normalized_src:
                    collected.append(normalized_src)
            return collected

        def collect_madara_page_entries(soup_obj):
            collected = []
            if soup_obj is None:
                return collected
            for img in soup_obj.select("div.reading-content div.page-break img, div.reading-content div.page-break.no-gaps img"):
                src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or img.get("data-cfsrc")
                normalized_src = looks_like_image_url(src)
                if not normalized_src:
                    continue
                collected.append(
                    {
                        "url": normalized_src,
                        "width": parse_int(img.get("width")),
                        "height": parse_int(img.get("height")),
                    }
                )
            return collected

        def trim_edge_ads_by_resolution(entries):
            """Retire les pubs probables début/fin si leur résolution est atypiquement basse."""
            if domain not in ("origines", "hentai") or len(entries or []) < 3:
                return entries
            safe_entries = list(entries)
            middle_entries = [entry for entry in safe_entries[1:-1] if entry.get("width") and entry.get("height")]
            if not middle_entries:
                middle_entries = [entry for entry in safe_entries if entry.get("width") and entry.get("height")]
            if not middle_entries:
                return safe_entries

            ref_w = float(median_value([entry.get("width", 0) for entry in middle_entries]) or 0)
            ref_h = float(median_value([entry.get("height", 0) for entry in middle_entries]) or 0)
            if ref_w <= 0 or ref_h <= 0:
                return safe_entries
            ref_area = ref_w * ref_h

            def is_edge_ad(entry):
                w = float(entry.get("width") or 0)
                h = float(entry.get("height") or 0)
                if w <= 0 or h <= 0:
                    return False
                area = w * h
                return ((w < ref_w * 0.78) and (h < ref_h * 0.78)) or (area < ref_area * 0.58)

            removed = 0
            if safe_entries and is_edge_ad(safe_entries[0]):
                safe_entries = safe_entries[1:]
                removed += 1
            if safe_entries and is_edge_ad(safe_entries[-1]):
                safe_entries = safe_entries[:-1]
                removed += 1

            if removed:
                runtime_log(
                    f"{removed} image(s) pub supprimée(s) (début/fin, résolution atypique).",
                    level="debug",
                    context={"action": "image_filter", "domain": domain},
                )
            return safe_entries

        # Étape 0 — Priorité à la structure Madara (mangas-origines)
        if domain == "origines":
            soup = BeautifulSoup(r_text, "html.parser")
            entries = collect_madara_page_entries(soup)
            entries = dedupe_entries(entries)
            entries = trim_edge_ads_by_resolution(entries)
            images = [entry["url"] for entry in entries]
            if images:
                images = clean_parasites(images, domain)
                runtime_log(
                    f"{len(images)} images finales après filtrage.",
                    level="info",
                    context={"action": "extract_images", "domain": domain},
                )
                return images

        # Étape 1 — Extraction depuis le JSON ts_reader.run
        json_str = parse_lr(r_text, "ts_reader.run(", ");</script>", False)
        if json_str:
            try:
                data = json.loads(json_str)
                images = [
                    normalize_image_url(img)
                    for img in data["sources"][0]["images"]
                ]
                if images:
                    images = dedupe_images(images)
                    runtime_log(
                        f"{len(images)} images détectées via ts_reader.run.",
                        level="info",
                        context={"action": "extract_images", "domain": domain},
                    )
                    images = clean_parasites(images, domain)
                    runtime_log(
                        f"{len(images)} images finales après filtrage.",
                        level="info",
                        context={"action": "extract_images", "domain": domain},
                    )
                    return images
            except Exception as e:
                runtime_log(f"Erreur parsing JSON images: {e}", level="warning", context={"action": "extract_images"})

        # Étape 2 — Fallback : balises img dans #readerarea
        soup = BeautifulSoup(r_text, "html.parser")

        # Supprimer les divs inutiles pour .fr
        if domain == "fr":
            for div in soup.find_all("div", class_="bixbox"):
                div.decompose()

        reader = soup.find("div", id="readerarea")
        if reader:
            images = collect_images_from_container(reader)
            images = dedupe_images(images)
            if images:
                images = clean_parasites(images, domain)
                runtime_log(
                    f"{len(images)} images finales après filtrage.",
                    level="info",
                    context={"action": "extract_images", "domain": domain},
                )
                return images

        # Étape 2b — Fallback : structure Madara (mangas-origines)
        entries = collect_madara_page_entries(soup)
        entries = dedupe_entries(entries)
        entries = trim_edge_ads_by_resolution(entries)
        images = [entry["url"] for entry in entries]
        if images:
            images = clean_parasites(images, domain)
            runtime_log(
                f"{len(images)} images finales après filtrage.",
                level="info",
                context={"action": "extract_images", "domain": domain},
            )
            return images

        reading_container = soup.select_one("div.reading-content, div.entry-content_wrap")
        if reading_container:
            images = collect_images_from_container(reading_container)
            images = dedupe_images(images)
            if images:
                images = clean_parasites(images, domain)
                runtime_log(
                    f"{len(images)} images finales après filtrage.",
                    level="info",
                    context={"action": "extract_images", "domain": domain},
                )
                return images

        # Étape 3 — Fallback regex brut
        img_urls = re.findall(
            r'<img[^>]+(?:src|data-src)=["\']\s*(https://[^"\'>]+\.(?:webp|jpg|jpeg|jpe|png|avif))["\']',
            r_text,
            re.IGNORECASE,
        )
        img_urls = [normalize_image_url(url) for url in img_urls if not url.startswith("data:")]
        img_urls = list(dict.fromkeys(img_urls))  # Supprime les doublons
        if img_urls:
            img_urls = clean_parasites(img_urls, domain)
            runtime_log(
                f"{len(img_urls)} images finales après filtrage.",
                level="info",
                context={"action": "extract_images", "domain": domain},
            )
        return img_urls

    attempt_count = max(1, int(retries or 1))
    site = get_supported_site_from_url(link)
    if site == "sushiscan.fr":
        domain = "fr"
    elif site == "sushiscan.net":
        domain = "net"
    elif site == "mangas-origines.fr":
        domain = "origines"
    elif site == "hentai-origines.fr":
        domain = "hentai"
    else:
        domain = normalize_hostname(urlparse(link).hostname) or "-"

    candidate_links = [(link or "").strip()]
    if domain in ("origines", "hentai"):
        list_url = build_mangas_origines_list_url(link)
        candidate_links = [list_url] if list_url else []
        if link and link.strip() and link.strip() not in candidate_links:
            candidate_links.append(link.strip())
        if candidate_links:
            runtime_log(
                "Mode lecture privilégié: style=list.",
                level="debug",
                context={"action": "get_images", "domain": domain},
            )

    for link_idx, candidate_link in enumerate(candidate_links, start=1):
        for attempt in range(1, attempt_count + 1):
            try:
                r = make_request(candidate_link, cookie, ua)
                body = r.text or ""
                runtime_log(
                    f"Requête HTTP directe reçue (len={len(body)}) [tentative {attempt}/{attempt_count}]",
                    level="debug",
                    context={"action": "get_images", "url_idx": f"{link_idx}/{len(candidate_links)}"},
                )

                if debug_mode:
                    suffix = f"_url{link_idx}_attempt{attempt}" if (attempt_count > 1 or len(candidate_links) > 1) else ""
                    debug_file = f"debug_sushiscan_{domain}{suffix}.log"
                    with open(debug_file, "w", encoding="utf-8") as f:
                        f.write(body)
                    runtime_log(
                        f"Fichier debug généré: {debug_file}",
                        level="debug",
                        context={"action": "debug_dump"},
                    )

                images = extract_images(body, domain)
                if images:
                    return images

                runtime_log(
                    f"Aucune image trouvée en accès direct (tentative {attempt}/{attempt_count}).",
                    level="warning",
                    context={"action": "get_images"},
                )
            except Exception as e:
                message = str(e)
                interpretation = interpret_curl_error(message)
                if interpretation:
                    runtime_log(
                        f"{interpretation} (tentative {attempt}/{attempt_count})",
                        level="warning",
                        context={"action": "get_images"},
                    )
                else:
                    runtime_log(
                        f"Erreur directe (tentative {attempt}/{attempt_count}): {message}.",
                        level="warning",
                        context={"action": "get_images"},
                    )

            if attempt < attempt_count:
                sleep_time = max(0, delay * (2 ** (attempt - 1)))
                runtime_log(
                    f"Nouvelle tentative extraction images dans {sleep_time}s.",
                    level="debug",
                    context={"action": "get_images"},
                )
                time.sleep(sleep_time)

    runtime_log(
        f"Impossible d'extraire des images depuis: {link}",
        level="error",
        context={"action": "get_images"},
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
    webp2jpg_enabled=True,
    referer_url=None,
    smart_resume_enabled=True,
    error_callback=None,
    output_root=ROOT_FOLDER,
):
    """Télécharge un volume complet avec gestion de progression et archivage."""
    if cancel_event.is_set():
        return None

    tome_label = normalize_tome_label(volume)
    clean_title = sanitize_folder_name(title)
    clean_tome = sanitize_folder_name(tome_label)
    base_output_dir = os.fspath(output_root) if output_root else ROOT_FOLDER
    base_output_dir = str(base_output_dir).strip() or ROOT_FOLDER
    base_output_dir = os.path.abspath(base_output_dir)
    folder = os.path.join(base_output_dir, clean_title, clean_tome)
    target_domain = get_cookie_domain_from_url(referer_url or "")
    app = getattr(MangaApp, "current_instance", None)

    def report_error(stage, reason, status_code=None):
        if callable(error_callback):
            try:
                error_callback(
                    {
                        "tome": tome_label,
                        "stage": stage,
                        "reason": str(reason),
                        "status_code": status_code,
                        "action": recommend_action_for_failure(status_code, str(reason)),
                    }
                )
            except Exception as cb_exc:
                logger(f"Erreur callback erreurs tome: {cb_exc}", level="debug")

    def infer_ext(page_url):
        parsed_path = (urlparse(normalize_image_url(page_url)).path or "").lower()
        ext = parsed_path.rsplit(".", 1)[-1] if "." in parsed_path else "jpg"
        if ext not in {"jpg", "jpeg", "png", "webp", "avif"}:
            ext = "jpg"
        return ext

    active_cookie = (cookie or "").strip()
    can_prompt_cookie_retry = True

    while True:
        if cancel_event.is_set():
            return None

        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            logger(f"Erreur création dossier: {e}", level="error")
            report_error("prepare", f"Erreur création dossier: {e}")
            return False

        number_len = max(1, len(str(len(images))))
        failed_downloads = []

        existing_indexes = set()
        if smart_resume_enabled:
            for i, page_url in enumerate(images):
                page_no = str(i + 1).zfill(number_len)
                expected_ext = infer_ext(page_url)
                candidates = {expected_ext, "jpg", "jpeg", "png", "webp", "avif"}
                if expected_ext == "webp" and webp2jpg_enabled:
                    candidates.add("jpg")
                for ext in candidates:
                    candidate_path = os.path.join(folder, f"{page_no}.{ext}")
                    if os.path.exists(candidate_path):
                        try:
                            if os.path.getsize(candidate_path) > 128:
                                existing_indexes.add(i)
                                break
                        except OSError:
                            continue

        existing_count = len(existing_indexes)
        if smart_resume_enabled and existing_count:
            logger(
                f"Reprise intelligente: {existing_count}/{len(images)} page(s) déjà présentes pour {tome_label}.",
                level="info",
            )

        with ThreadPoolExecutor(max_workers=THREADS) as executor:
            futures = []
            progress_counter = {"done": existing_count}
            lock = threading.Lock()

            if update_progress and existing_count:
                update_progress(existing_count, len(images))

            for i, url in enumerate(images):
                if i in existing_indexes:
                    continue
                if cancel_event.wait(0.1):
                    break

                def progress_callback(_idx):
                    with lock:
                        progress_counter["done"] += 1
                        if update_progress:
                            update_progress(progress_counter["done"], len(images))

                futures.append(
                    executor.submit(
                        download_image,
                        url,
                        folder,
                        active_cookie,
                        ua,
                        i,
                        number_len,
                        cancel_event,
                        failed_downloads,
                        progress_callback=progress_callback,
                        referer_url=referer_url,
                        webp2jpg_enabled=webp2jpg_enabled,
                    )
                )

            for future in as_completed(futures):
                if cancel_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    future.result()
                except Exception as thread_e:
                    failed_downloads.append(
                        {
                            "url": "",
                            "kind": "retryable",
                            "status_code": None,
                            "reason": f"Exception thread: {thread_e}",
                        }
                    )

        if cancel_event.is_set():
            logger(f"Téléchargement annulé pour {tome_label}.", level="warning")
            return None

        normalized_failures = []
        for fail in failed_downloads:
            if isinstance(fail, dict):
                normalized_failures.append(
                    {
                        "url": (fail.get("url") or "").strip(),
                        "kind": (fail.get("kind") or "retryable").strip(),
                        "status_code": fail.get("status_code"),
                        "reason": (fail.get("reason") or "Échec inconnu").strip(),
                    }
                )
            else:
                normalized_failures.append(
                    {
                        "url": str(fail).strip(),
                        "kind": "retryable",
                        "status_code": None,
                        "reason": "Échec non typé",
                    }
                )

        missing_failures = [f for f in normalized_failures if f["kind"] == "missing"]
        hard_failures = [f for f in normalized_failures if f["kind"] not in ("missing", "cancelled")]

        if missing_failures:
            sample_missing = missing_failures[0].get("url") or "URL inconnue"
            logger(
                f"{len(missing_failures)} page(s) absente(s) (404/410) sur {tome_label}. Exemple: {sample_missing}",
                level="warning",
            )
            logger("CBZ maintenu: les pages manquantes sont ignorées.", level="info")

        if hard_failures:
            sample_hard = hard_failures[0]
            sample_reason = sample_hard.get("reason") or "cause inconnue"
            sample_status = sample_hard.get("status_code")
            logger(
                f"{len(hard_failures)} image(s) bloquée(s)/non téléchargeable(s) sur {tome_label}. Exemple: {sample_reason}",
                level="warning",
            )
            report_error("download", sample_reason, sample_status)
            if cancel_event.is_set():
                return None

            if not can_prompt_cookie_retry:
                logger("Relance cookie déjà tentée une fois; abandon du tome.", level="warning")
                return False

            can_prompt_cookie_retry = False
            try:
                if app and hasattr(app, "ask_yes_no"):
                    res = app.ask_yes_no(
                        "Erreur de téléchargement",
                        "Des images ont échoué. Voulez-vous modifier le cookie et relancer le téléchargement complet de ce tome ?",
                    )
                else:
                    res = messagebox.askyesno(
                        "Erreur de téléchargement",
                        "Des images ont échoué. Voulez-vous modifier le cookie et relancer le téléchargement complet de ce tome ?",
                    )

                if cancel_event.is_set():
                    return None

                if res:
                    if app and hasattr(app, "ask_string"):
                        new_cookie = app.ask_string(
                            "Nouveau cookie",
                            "Entrez le nouveau cookie cf_clearance :",
                        )
                    else:
                        import tkinter.simpledialog as simpledialog
                        new_cookie = simpledialog.askstring(
                            "Nouveau cookie",
                            "Entrez le nouveau cookie cf_clearance :",
                        )

                    if cancel_event.is_set():
                        return None

                    new_cookie = (new_cookie or "").strip()
                    if new_cookie:
                        active_cookie = new_cookie
                        if app and target_domain in COOKIE_DOMAINS:
                            try:
                                cookie_var = app._get_cookie_var_for_domain(target_domain)
                                if cookie_var is None:
                                    raise RuntimeError("Domaine cookie non supporté pour synchronisation UI.")
                                app.run_on_ui(cookie_var.set, active_cookie)
                                app.sync_cookie_source_for_domain(target_domain)
                                app.persist_settings()
                            except Exception as sync_err:
                                logger(f"Impossible de synchroniser le nouveau cookie: {sync_err}", level="warning")

                        shutil.rmtree(folder, ignore_errors=True)
                        logger(
                            "Ancien dossier supprimé. Relancement du téléchargement avec le nouveau cookie...",
                            level="info",
                        )
                        continue

                logger("Aucun cookie saisi. Le tome ne sera pas complété.", level="error")
            except Exception as e:
                logger(f"Erreur durant la relance : {e}", level="error")
            return False

        if cancel_event.is_set():
            return None
        if not os.path.exists(folder):
            report_error("prepare", "Dossier de tome introuvable après téléchargement.")
            return False

        file_count = sum(len(files) for _, _, files in os.walk(folder))
        if file_count == 0:
            logger(f"Aucune image téléchargée pour {tome_label}.", level="error")
            report_error("download", "Aucune image téléchargée pour ce tome.")
            return False

        if not cbz_enabled:
            logger(f"CBZ desactive pour {clean_tome}: images conservees.", level="info")
            return True

        if archive_cbz(folder, title, tome_label, remove_source=True):
            cbz_path = os.path.join(
                base_output_dir, clean_title, f"{clean_title} - {clean_tome}.cbz"
            )
            try:
                size_mb = round(os.path.getsize(cbz_path) / (1024 * 1024), 2)
            except OSError:
                size_mb = 0
            logger("", level="info")
            logger(f"CBZ créé : {cbz_path} ({size_mb} MB)", level="cbz")
            return True
        logger(f"Échec de création CBZ pour {clean_tome}", level="warning")
        report_error("archive_cbz", f"Échec de création CBZ pour {clean_tome}")
        return False

SECRET_DPAPI_PREFIX = "dpapi:"


def _dpapi_protect_bytes(raw_bytes):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt_protect_data = crypt32.CryptProtectData
    crypt_protect_data.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt_protect_data.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(raw_bytes)
    in_blob = DataBlob(len(raw_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DataBlob()
    if not crypt_protect_data(ctypes.byref(in_blob), "SushiDL", None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def _dpapi_unprotect_bytes(protected_bytes):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt_unprotect_data = crypt32.CryptUnprotectData
    crypt_unprotect_data.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt_unprotect_data.restype = wintypes.BOOL

    in_buf = ctypes.create_string_buffer(protected_bytes)
    in_blob = DataBlob(len(protected_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DataBlob()
    description = wintypes.LPWSTR()
    if not crypt_unprotect_data(
        ctypes.byref(in_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def protect_secret_value(value):
    """Chiffre localement une valeur sensible (DPAPI sous Windows)."""
    plain = (value or "").strip()
    if not plain:
        return ""
    if os.name != "nt":
        return plain
    try:
        protected = _dpapi_protect_bytes(plain.encode("utf-8"))
        return SECRET_DPAPI_PREFIX + base64.b64encode(protected).decode("ascii")
    except Exception as exc:
        runtime_log(f"DPAPI indisponible, valeur stockée en clair: {exc}", level="warning")
        return plain


def unprotect_secret_value(value):
    """Déchiffre une valeur sensible stockée via protect_secret_value."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(SECRET_DPAPI_PREFIX):
        return raw
    encoded = raw[len(SECRET_DPAPI_PREFIX):]
    try:
        protected = base64.b64decode(encoded.encode("ascii"))
        clear_bytes = _dpapi_unprotect_bytes(protected)
        return clear_bytes.decode("utf-8", errors="ignore").strip()
    except Exception as exc:
        runtime_log(f"Impossible de déchiffrer une valeur sensible: {exc}", level="warning")
        return ""

def save_cookie_cache(
    cookies_dict,
    ua,
    cbz,
    webp2jpg_enabled,
    smart_resume_enabled=True,
    verbose_logs=True,
    cookie_sources=None,
    cookie_user_agents=None,
    cookie_headers=None,
):
    """
    Sauvegarde les paramètres dans un fichier JSON
    
    Args:
        cookies_dict (dict): Cookies par domaine
        ua (str): User-Agent
        cbz (bool): Préférence CBZ
        webp2jpg_enabled (bool): Préférence conversion
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    normalized_cookies = {domain: (cookies_dict.get(domain) or "").strip() for domain in COOKIE_DOMAINS}
    normalized_sources = {domain: (cookie_sources or {}).get(domain, "") for domain in COOKIE_DOMAINS}
    normalized_cookie_uas = {domain: (cookie_user_agents or {}).get(domain, "") for domain in COOKIE_DOMAINS}
    existing_cookies = {domain: "" for domain in COOKIE_DOMAINS}
    existing_updated_at = {domain: "" for domain in COOKIE_DOMAINS}
    if COOKIE_CACHE_PATH.exists():
        try:
            with COOKIE_CACHE_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            raw_existing_cookies = existing.get("cookies", {}) if isinstance(existing, dict) else {}
            raw_existing_cookies_encrypted = (
                existing.get("cookies_encrypted", {}) if isinstance(existing, dict) else {}
            )
            raw_existing_updated = existing.get("cookie_updated_at", {}) if isinstance(existing, dict) else {}
            if isinstance(raw_existing_cookies_encrypted, dict):
                existing_cookies = {
                    domain: unprotect_secret_value(raw_existing_cookies_encrypted.get(domain, ""))
                    for domain in COOKIE_DOMAINS
                }
            if isinstance(raw_existing_cookies, dict):
                existing_cookies = {
                    domain: existing_cookies.get(domain) or (raw_existing_cookies.get(domain) or "").strip()
                    for domain in COOKIE_DOMAINS
                }
            if isinstance(raw_existing_updated, dict):
                existing_updated_at = {
                    domain: (raw_existing_updated.get(domain) or "").strip()
                    for domain in COOKIE_DOMAINS
                }
        except Exception as exc:
            runtime_log(f"Lecture cache existant impossible: {exc}", level="warning")

    cookie_updated_at = {domain: "" for domain in COOKIE_DOMAINS}
    for domain in COOKIE_DOMAINS:
        current_cookie = normalized_cookies[domain]
        previous_cookie = existing_cookies.get(domain, "")
        previous_ts = existing_updated_at.get(domain, "")
        if not current_cookie:
            cookie_updated_at[domain] = ""
        elif current_cookie == previous_cookie and previous_ts:
            cookie_updated_at[domain] = previous_ts
        else:
            cookie_updated_at[domain] = now_iso

    encrypted_cookies = {
        domain: protect_secret_value(normalized_cookies[domain])
        for domain in COOKIE_DOMAINS
    }
    plain_cookies = {domain: "" for domain in COOKIE_DOMAINS} if os.name == "nt" else normalized_cookies

    data = {
        "cookies": plain_cookies,
        "cookies_encrypted": encrypted_cookies,
        "ua": (ua or DEFAULT_USER_AGENT).strip(),
        "cbz_enabled": bool(cbz),
        "last_url": MangaApp.last_url_used,
        "timestamp": now_iso,
        "cookie_updated_at": cookie_updated_at,
        "cookie_sources": normalized_sources,
        "cookie_user_agents": normalized_cookie_uas,
        "cookie_headers": {domain: "" for domain in COOKIE_DOMAINS},
        "webp2jpg_enabled": bool(webp2jpg_enabled),
        "smart_resume_enabled": bool(smart_resume_enabled),
        "verbose_logs": bool(verbose_logs),
    }
    COOKIE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = COOKIE_CACHE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, COOKIE_CACHE_PATH)
    return cookie_updated_at


def load_cookie_cache():
    """Charge les paramètres depuis le fichier cache"""
    default_cbz = True
    default_webp2jpg = True
    default_smart_resume = True
    default_verbose_logs = True
    
    if not COOKIE_CACHE_PATH.exists():
        return (
            {domain: "" for domain in COOKIE_DOMAINS},
            DEFAULT_USER_AGENT,
            default_cbz,
            "",
            default_webp2jpg,
            default_smart_resume,
            default_verbose_logs,
            {domain: "" for domain in COOKIE_DOMAINS},
            {domain: "" for domain in COOKIE_DOMAINS},
            {domain: "" for domain in COOKIE_DOMAINS},
            {domain: "" for domain in COOKIE_DOMAINS},
        )
    
    try:
        with COOKIE_CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)

        cookies = data.get("cookies", {})
        if not isinstance(cookies, dict):
            cookies = {}
        encrypted_cookies = data.get("cookies_encrypted", {})
        if isinstance(encrypted_cookies, dict):
            cookies = {
                domain: unprotect_secret_value(encrypted_cookies.get(domain)) or (cookies.get(domain) or "").strip()
                for domain in COOKIE_DOMAINS
            }
        else:
            cookies = {domain: (cookies.get(domain) or "").strip() for domain in COOKIE_DOMAINS}

        # Les préférences (UA, FlareSolverr, etc.) ne dépendent pas
        # de la validité temporelle du cookie Cloudflare.
        cookie_sources = data.get("cookie_sources", {})
        if not isinstance(cookie_sources, dict):
            cookie_sources = {}

        cookie_user_agents = data.get("cookie_user_agents", {})
        if not isinstance(cookie_user_agents, dict):
            cookie_user_agents = {}
        cookie_headers = data.get("cookie_headers", {})
        if not isinstance(cookie_headers, dict):
            cookie_headers = {}
        rebuilt_cookie_headers = {
            domain: (
                f"cf_clearance={cookies.get(domain, '').strip()}"
                if (cookies.get(domain) or "").strip()
                else ""
            )
            for domain in COOKIE_DOMAINS
        }
        for domain in COOKIE_DOMAINS:
            if not rebuilt_cookie_headers[domain]:
                rebuilt_cookie_headers[domain] = (cookie_headers.get(domain) or "").strip()
        cookie_updated_at = data.get("cookie_updated_at", {})
        if not isinstance(cookie_updated_at, dict):
            cookie_updated_at = {}

        return (
            {domain: (cookies.get(domain) or "").strip() for domain in COOKIE_DOMAINS},
            (data.get("ua") or DEFAULT_USER_AGENT).strip(),
            data.get("cbz_enabled", default_cbz),
            (data.get("last_url") or "").strip(),
            data.get("webp2jpg_enabled", default_webp2jpg),
            bool(data.get("smart_resume_enabled", default_smart_resume)),
            bool(data.get("verbose_logs", default_verbose_logs)),
            {domain: (cookie_sources.get(domain) or "").strip() for domain in COOKIE_DOMAINS},
            {domain: (cookie_user_agents.get(domain) or "").strip() for domain in COOKIE_DOMAINS},
            {domain: rebuilt_cookie_headers.get(domain, "") for domain in COOKIE_DOMAINS},
            {domain: (cookie_updated_at.get(domain) or "").strip() for domain in COOKIE_DOMAINS},
        )
    except Exception as e:
        runtime_log(f"Erreur lecture cache cookie : {e}", level="warning")
    
    return (
        {domain: "" for domain in COOKIE_DOMAINS},
        DEFAULT_USER_AGENT,
        default_cbz,
        "",
        default_webp2jpg,
        default_smart_resume,
        default_verbose_logs,
        {domain: "" for domain in COOKIE_DOMAINS},
        {domain: "" for domain in COOKIE_DOMAINS},
        {domain: "" for domain in COOKIE_DOMAINS},
        {domain: "" for domain in COOKIE_DOMAINS},
    )


def get_cover_image(r_text):
    """Récupère et affiche l'image de couverture d'un manga."""
    runtime_log("Analyse de la couverture en cours.", level="debug", context={"action": "cover"})
    soup = BeautifulSoup(r_text, "html.parser")
    app = getattr(MangaApp, "current_instance", None)
    page_url = ""
    if app is not None:
        page_url = app.run_on_ui(app.url.get, wait=True, default="").strip()
    if not page_url:
        og_url = soup.find("meta", attrs={"property": "og:url"})
        page_url = (og_url.get("content") or "").strip() if og_url else ""

    def resolve_cover_candidate(raw_candidate):
        candidate = normalize_image_url((raw_candidate or "").strip().strip("\"'"))
        if not candidate or candidate.startswith("data:"):
            return ""
        if candidate.startswith("http"):
            return candidate
        if page_url:
            return normalize_image_url(urljoin(page_url, candidate))
        site_root = get_site_root_url(page_url)
        if site_root:
            return normalize_image_url(urljoin(site_root, candidate))
        return ""

    def extract_src_from_srcset(raw_srcset):
        srcset = (raw_srcset or "").strip()
        if not srcset:
            return ""
        entries = [part.strip() for part in srcset.split(",") if part.strip()]
        if not entries:
            return ""
        # Prend la dernière entrée (souvent la meilleure résolution).
        last_entry = entries[-1]
        return (last_entry.split()[0] if last_entry.split() else "").strip()

    def extract_cover_from_img(tag):
        if tag is None:
            return ""
        for attr_name in ("data-src", "data-lazy-src", "src", "data-cfsrc"):
            candidate = resolve_cover_candidate(tag.get(attr_name))
            if candidate:
                return candidate
        for attr_name in ("data-srcset", "srcset"):
            candidate = resolve_cover_candidate(extract_src_from_srcset(tag.get(attr_name)))
            if candidate:
                return candidate
        return ""

    cover_selectors = (
        "div.thumb img",
        "div.thumb-container img",
        ".summary_image img",
        ".summary_image a img",
        ".post-thumb img",
        "img.wp-post-image",
        ".manga-info-pic img",
        ".profile-manga img",
        ".post-content_item .summary-content img",
    )
    img_url = None

    for selector in cover_selectors:
        cover_img = soup.select_one(selector)
        img_url = extract_cover_from_img(cover_img)
        if img_url:
            break

    if not img_url:
        for node in soup.select(".summary_image, .thumb, .thumb-container, .post-thumb, .profile-manga"):
            style_value = (node.get("style") or "").strip()
            if not style_value:
                continue
            match = re.search(r"background-image\s*:\s*url\(([^)]+)\)", style_value, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = resolve_cover_candidate(match.group(1))
            if candidate:
                img_url = candidate
                break

    if not img_url:
        for tag in soup.find_all("meta", attrs={"property": True}):
            if tag["property"] in ["og:image", "og:image:secure_url"]:
                candidate = resolve_cover_candidate((tag.get("content") or "").strip())
                if candidate:
                    img_url = candidate
                    break
    if not img_url:
        for tag in soup.find_all("meta", attrs={"name": True}):
            if (tag.get("name") or "").strip().lower() in ("twitter:image", "twitter:image:src"):
                candidate = resolve_cover_candidate((tag.get("content") or "").strip())
                if candidate:
                    img_url = candidate
                    break

    if not img_url:
        return None

    if app is None:
        return img_url

    app.cover_url = img_url
    try:
        referer_url = page_url
        if not referer_url:
            referer_url = get_site_root_url(img_url) or "https://sushiscan.fr/"

        cookie = app.get_cookie(img_url)
        cookie_header = app.get_cookie_header_for_url(img_url, fallback_cookie=cookie)
        headers = {
            "User-Agent": app.get_request_user_agent_for_url(img_url),
            "Referer": referer_url,
        }
        if cookie_header:
            headers["Cookie"] = cookie_header

        raw = robust_download_image(
            normalize_image_url(img_url),
            headers,
            max_try=2,
            delay=1,
        )
        runtime_log(
            "Téléchargement couverture OK via accès direct.",
            level="debug",
            context={"action": "cover"},
        )

        target_w, target_h = app.run_on_ui(app.get_cover_target_size, wait=True, default=(100, 150))
        image = Image.open(BytesIO(raw))
        is_animated = bool(getattr(image, "is_animated", False))

        if is_animated:
            frames = []
            durations = []
            max_frames = 120
            for idx, frame in enumerate(ImageSequence.Iterator(image)):
                if idx >= max_frames:
                    break
                rgba_frame = frame.convert("RGBA")
                fitted_frame = ImageOps.fit(
                    rgba_frame,
                    (int(target_w), int(target_h)),
                    method=Image.LANCZOS,
                    centering=(0.5, 0.5),
                )
                frame_delay = int(frame.info.get("duration") or image.info.get("duration") or 100)
                durations.append(max(60, frame_delay))
                frames.append(fitted_frame)

            if len(frames) > 1:
                app.run_on_ui(lambda: app._apply_cover_animation(frames, durations))
                return img_url

            # GIF mono-frame: fallback statique.
            if frames:
                app.run_on_ui(lambda: app._apply_cover_static(frames[0]))
                return img_url

        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        elif image.mode == "RGBA":
            image = image.convert("RGB")
        fitted = ImageOps.fit(
            image,
            (int(target_w), int(target_h)),
            method=Image.LANCZOS,
            centering=(0.5, 0.5),
        )
        app.run_on_ui(lambda: app._apply_cover_static(fitted))
    except Exception as err:
        runtime_log(f"Erreur affichage couverture: {err}", level="error", context={"action": "cover"})
    return img_url

class MangaApp:
    """
    Classe principale de l'application - Interface graphique Tkinter
    Gère l'ensemble de l'UI et la logique de téléchargement
    """
    last_url_used = ""

    def run_on_ui(self, callback, *args, wait=False, default=None, **kwargs):
        """
        Exécute une fonction sur le thread UI.
        - wait=False : asynchrone
        - wait=True  : synchrone (bloque le thread appelant jusqu'au résultat)
        """
        if threading.current_thread() is threading.main_thread():
            return callback(*args, **kwargs)

        if wait:
            done = threading.Event()
            holder = {"result": default, "error": None}

            def wrapped():
                try:
                    holder["result"] = callback(*args, **kwargs)
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    done.set()

            self.ui_queue.put(wrapped)
            done.wait()
            if holder["error"] is not None:
                raise holder["error"]
            return holder["result"]

        self.ui_queue.put(lambda: callback(*args, **kwargs))
        return default

    def process_ui_queue(self):
        """Traite les actions UI planifiées depuis les threads de fond."""
        try:
            for _ in range(200):
                action = self.ui_queue.get_nowait()
                try:
                    action()
                except Exception as exc:
                    emit_console_log(f"Erreur action UI planifiée: {exc}", level="error", context={"action": "ui_queue"})
        except queue.Empty:
            pass
        finally:
            self.root.after(30, self.process_ui_queue)

    def _set_progress_ui(self, percent):
        self.progress.set(percent)
        self.progress_label.config(text=f"{int(percent)}%")

    def _set_current_volume_ui(self, volume_label=None):
        if not hasattr(self, "current_volume_status_label"):
            return
        label = (volume_label or "").strip()
        if not label:
            text = "Tome/Chapitre en cours: --"
        elif label.lower().startswith(("tome", "chapitre", "volume")):
            text = f"{label} en cours"
        else:
            text = f"Tome/Chapitre {label} en cours"
        self.current_volume_status_label.config(text=text)

    def _set_eta_ui(self, tome_eta=None, global_eta=None):
        if not hasattr(self, "eta_label"):
            return
        tome_text = format_duration_short(tome_eta)
        global_text = format_duration_short(global_eta)
        self.eta_label.config(text=f"ETA Tome: {tome_text} | ETA Global: {global_text}")

    def _set_download_controls(self, is_running):
        self.download_in_progress = bool(is_running)
        if is_running:
            self.dl_button.config(text="Téléchargement...", state="disabled")
            self.cancel_button.config(state="normal")
            self.filter_entry.config(state="disabled")
            self.clear_filter_button.config(state="disabled")
            if hasattr(self, "url_entry"):
                self.url_entry.config(state="disabled")
            if hasattr(self, "analyze_button"):
                self.analyze_button.config(state="disabled")
            if hasattr(self, "invert_button"):
                self.invert_button.config(state="disabled")
            if hasattr(self, "master_toggle_button"):
                self.master_toggle_button.config(state="disabled")
            self._set_workflow_step("download", "Téléchargement en cours...")
        else:
            self.dl_button.config(text="Télécharger")
            self.cancel_button.config(state="disabled")
            self.filter_entry.config(state="normal")
            self.clear_filter_button.config(state="normal")
            if hasattr(self, "url_entry"):
                self.url_entry.config(state="normal")
            if hasattr(self, "analyze_button"):
                self.analyze_button.config(
                    state="disabled" if getattr(self, "analysis_in_progress", False) else "normal"
                )
            if hasattr(self, "invert_button"):
                self.invert_button.config(state="normal")
            if hasattr(self, "master_toggle_button"):
                self.master_toggle_button.config(state="normal")
            if hasattr(self, "set_filter_placeholder") and not self.filter_text.get().strip():
                self.set_filter_placeholder()
            self._set_current_volume_ui(None)
            self._set_eta_ui()
            self._set_progress_detail_ui(None, None)
            if getattr(self, "current_workflow_step", "") != "logs":
                if getattr(self, "check_items", None):
                    self._set_workflow_step("select", "Sélectionne les tomes à télécharger.")
                else:
                    self._set_workflow_step("source", "Renseigne une URL puis lance l'analyse.")
        self.update_master_toggle_button()

    def _set_progress_detail_ui(self, done=None, total=None):
        if not hasattr(self, "progress_detail_label"):
            return
        if done is None or total is None:
            self.progress_detail_label.config(text="Images: --/--")
            return
        self.progress_detail_label.config(text=f"Images: {int(done)}/{int(total)}")

    def _set_workflow_step(self, step, hint_text=None):
        order = ("auth", "source", "select", "download", "logs")
        if step not in order:
            step = "auth"
        self.current_workflow_step = step
        if not hasattr(self, "workflow_labels"):
            return

        active_index = order.index(step)
        for idx, key in enumerate(order):
            label = self.workflow_labels.get(key)
            if label is None:
                continue
            if idx < active_index:
                label.config(bg="#d8ebf8", fg=self.palette["text"])
            elif idx == active_index:
                label.config(bg=self.palette["accent"], fg="#ffffff")
            else:
                label.config(bg=self.palette["card_alt"], fg=self.palette["muted"])

        if hint_text and hasattr(self, "workflow_hint_label"):
            self.workflow_hint_label.config(text=hint_text)

    def _show_volume_empty_state(self, text, tone="muted"):
        if not hasattr(self, "vol_empty_label"):
            return
        fg_map = {
            "muted": self.palette["muted"],
            "info": self.palette["accent_hover"],
            "warning": "#a16207",
            "error": self.palette["danger"],
        }
        self.vol_empty_label.config(
            text=repair_mojibake_text(text or ""),
            fg=fg_map.get(tone, self.palette["muted"]),
            bg=self.palette["canvas_bg"],
        )
        if hasattr(self, "canvas"):
            self.vol_empty_label.place(in_=self.canvas, relx=0.5, rely=0.5, anchor="center")
            self.vol_empty_label.lift()
        else:
            self.vol_empty_label.place(relx=0.5, rely=0.5, anchor="center")
        if hasattr(self, "canvas"):
            self.canvas.after_idle(lambda: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def _hide_volume_empty_state(self):
        if hasattr(self, "vol_empty_label"):
            self.vol_empty_label.place_forget()
        if hasattr(self, "canvas"):
            self.canvas.after_idle(lambda: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def _is_volume_visible(self, chk):
        try:
            return str(chk.winfo_manager()) == "grid"
        except Exception:
            return False

    def _refresh_volume_empty_state(self):
        if not hasattr(self, "check_items"):
            return
        total = len(self.check_items)
        if total == 0:
            self._show_volume_empty_state("Aucun Tome/Chapitre chargé.", tone="muted")
            return
        visible = 0
        for chk, _label in self.check_items:
            if self._is_volume_visible(chk):
                visible += 1
        if visible == 0:
            self._show_volume_empty_state("Aucun résultat avec ce filtre.", tone="warning")
        else:
            self._hide_volume_empty_state()

    def _update_error_tab_title(self, focus_errors=False):
        if not hasattr(self, "error_tab"):
            return
        count = len(getattr(self, "volume_error_entries", []) or [])
        self.error_tab_title = f"Erreurs ({count})"
        self._refresh_selection_tab_buttons()
        if focus_errors and count > 0:
            self._select_selection_tab("error")
            self._set_workflow_step("logs", "Des erreurs sont disponibles dans l'onglet Erreurs.")

    def _on_selection_tab_changed(self, _event=None):
        selected = getattr(self, "active_selection_tab", "selection")
        if selected == "error":
            self._set_workflow_step("logs", "Consulte les erreurs par tome.")
        else:
            if getattr(self, "download_in_progress", False):
                self._set_workflow_step("download", "Téléchargement en cours...")
            else:
                self._set_workflow_step("select", "Sélectionne les tomes à télécharger.")

    def _layout_tab_row(self, header, buttons, order):
        """Place les onglets en chevauchement 1px pour eliminer les doubles separations."""
        if header is None or not buttons or not order:
            return
        try:
            self.root.update_idletasks()
        except Exception:
            return
        x = 0
        max_h = 0
        for index, key in enumerate(order):
            btn = buttons.get(key)
            if btn is None:
                continue
            try:
                w = max(1, btn.winfo_reqwidth())
                h = max(1, btn.winfo_reqheight())
            except Exception:
                continue
            if index > 0:
                x -= 1
            btn.place(x=x, y=0, width=w, height=h)
            x += w
            if h > max_h:
                max_h = h
        if max_h > 0:
            header.configure(height=max_h)

    def _refresh_selection_tab_buttons(self):
        """Rafraichit les onglets visuels Tomes / Chapitres et Erreurs."""
        if not hasattr(self, "selection_tab_buttons"):
            return
        palette = getattr(self, "palette", {}) or {}
        active_key = getattr(self, "active_selection_tab", "selection")
        default_bg = palette.get("card_alt", "#f3f6fa")
        default_fg = palette.get("muted", "#5f6b7a")
        selected_bg = "#ffffff"
        selected_fg = palette.get("text", "#1f2937")
        border_color = palette.get("border", "#b8c0cb")
        titles = {
            "selection": "Tomes / Chapitres",
            "error": getattr(self, "error_tab_title", "Erreurs (0)"),
        }
        selected_widget = None
        for key, button in self.selection_tab_buttons.items():
            is_selected = key == active_key
            bg, fg = (selected_bg, selected_fg) if is_selected else (default_bg, default_fg)
            button.configure(
                text=titles.get(key, ""),
                bg=bg,
                fg=fg,
                activebackground=bg,
                activeforeground=fg,
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=border_color,
                highlightcolor=border_color,
            )
            if is_selected:
                selected_widget = button
        self._layout_tab_row(
            getattr(self, "selection_tabs_header", None),
            self.selection_tab_buttons,
            getattr(self, "selection_tab_order", ("selection", "error")),
        )
        if selected_widget is not None:
            try:
                selected_widget.lift()
            except Exception:
                pass
        self._update_selection_top_border_mask(selected_widget, selected_bg)

    def _update_selection_top_border_mask(self, selected_widget, mask_bg):
        """Masque la ligne haute de l'encart sous l'onglet actif (style attache)."""
        if not hasattr(self, "selection_content_border") or not hasattr(self, "selection_tabs_header"):
            return
        if not hasattr(self, "selection_top_border_mask"):
            self.selection_top_border_mask = tk.Frame(
                self.selection_content_border,
                bg=self.palette.get("card_bg", "#f7f9fc"),
                bd=0,
                highlightthickness=0,
            )
        if not hasattr(self, "selection_tab_bottom_mask"):
            self.selection_tab_bottom_mask = tk.Frame(
                self.selection_tabs_header,
                bg=self.palette.get("card_bg", "#f7f9fc"),
                bd=0,
                highlightthickness=0,
            )
        if selected_widget is None:
            self.selection_top_border_mask.place_forget()
            self.selection_tab_bottom_mask.place_forget()
            return
        try:
            self.root.update_idletasks()
            hx = selected_widget.winfo_x()
            hy = selected_widget.winfo_y()
            hh = selected_widget.winfo_height()
            w = selected_widget.winfo_width()
            header_x = self.selection_tabs_header.winfo_x()
            border_x = self.selection_content_border.winfo_x()
            x = header_x + hx - border_x
            if w <= 1:
                self.selection_top_border_mask.place_forget()
                self.selection_tab_bottom_mask.place_forget()
                return
            inner_x = max(1, x + 1)
            inner_w = max(1, w - 2)
            self.selection_top_border_mask.configure(bg=mask_bg)
            self.selection_top_border_mask.place(
                x=inner_x,
                y=0,
                width=inner_w,
                height=1,
            )
            self.selection_top_border_mask.lift()
            self.selection_tab_bottom_mask.configure(bg=mask_bg)
            self.selection_tab_bottom_mask.place(
                x=max(1, hx + 1),
                y=max(0, hy + hh - 1),
                width=inner_w,
                height=1,
            )
            self.selection_tab_bottom_mask.lift()
        except Exception:
            self.selection_top_border_mask.place_forget()
            self.selection_tab_bottom_mask.place_forget()

    def _select_selection_tab(self, tab_key):
        """Affiche l'onglet selection (tomes) ou erreurs dans l'encart du bas."""
        if not hasattr(self, "selection_tab_pages"):
            return
        page = self.selection_tab_pages.get(tab_key)
        if page is None:
            return
        self.active_selection_tab = tab_key
        for tab_page in self.selection_tab_pages.values():
            tab_page.pack_forget()
        page.pack(fill="both", expand=True)
        self._refresh_selection_tab_buttons()
        self._on_selection_tab_changed()

    def _shortcut_analyze(self, _event=None):
        if getattr(self, "analysis_in_progress", False):
            return "break"
        if getattr(self, "download_in_progress", False):
            return "break"
        if hasattr(self, "analyze_button") and str(self.analyze_button.cget("state")) == "disabled":
            return "break"
        self.load_volumes()
        return "break"

    def _shortcut_download(self, _event=None):
        if hasattr(self, "dl_button") and str(self.dl_button.cget("state")) != "disabled":
            self.download_selected()
        return "break"

    def _shortcut_focus_filter(self, _event=None):
        if not hasattr(self, "filter_entry"):
            return "break"
        if str(self.filter_entry.cget("state")) == "disabled":
            return "break"
        self.filter_entry.focus_set()
        self.filter_entry.icursor("end")
        if getattr(self, "filter_placeholder_active", False):
            self.clear_filter_placeholder()
        return "break"

    def _shortcut_focus_logs(self, _event=None):
        if hasattr(self, "log_text"):
            self.log_text.focus_set()
        self._set_workflow_step("logs", "Consulte le journal et les erreurs.")
        return "break"

    def ask_yes_no(self, title, prompt):
        return self.run_on_ui(
            lambda: messagebox.askyesno(title, prompt, parent=self.root),
            wait=True,
            default=False,
        )

    def ask_string(self, title, prompt):
        import tkinter.simpledialog as simpledialog
        return self.run_on_ui(
            simpledialog.askstring,
            title,
            prompt,
            wait=True,
            default=None,
            parent=self.root,
        )

    def _reset_analysis_auth_state(self, reset_domains=COOKIE_DOMAINS, reset_ua=True, clear_label=True):
        """Réinitialise l'état d'auth d'analyse (par domaine et/ou UA)."""
        if not hasattr(self, "analysis_auth_state") or not isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state = {**{domain: None for domain in COOKIE_DOMAINS}, "ua": None}
        domains = tuple(reset_domains or ())
        for domain in domains:
            if domain in COOKIE_DOMAINS:
                self.analysis_auth_state[domain] = None
        if reset_ua:
            self.analysis_auth_state["ua"] = None
        self.analysis_auth_last_domain = None
        self.analysis_auth_last_message = ""
        if clear_label and hasattr(self, "status_label"):
            self.run_on_ui(lambda: self.status_label.config(text="", foreground="#5f6f88"))

    def _schedule_auth_status_update(self, *_args):
        """Rafraîchit les badges auth sans invalider l'état d'analyse en mémoire."""
        if not hasattr(self, "cookie_sources"):
            return
        # Toute modification UA remet le statut UA en attente (ou invalide si vide).
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["ua"] = None if self.get_direct_user_agent().strip() else False
        # Le test listing depend aussi du couple cookie+UA: on invalide puis on reprobe.
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            for domain in COOKIE_DOMAINS:
                self.cookie_probe_state[domain] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=COOKIE_DOMAINS, delay_ms=1200)

    def _schedule_auth_status_update_cookie_fr(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .fr sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["fr"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["fr"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("fr",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_net(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .net sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["net"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["net"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("net",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_origines(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .origines sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["origines"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["origines"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("origines",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_hentai(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .hentai-origines sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["hentai"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["hentai"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("hentai",), delay_ms=1200)

    def _schedule_auth_status_update_url(self, *_args):
        """Rafraîchit les badges auth au changement d'URL sans effacer l'historique d'analyse."""
        if not hasattr(self, "cookie_sources"):
            return
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)

    def _source_to_display(self, source):
        _ = source
        return ""

    def _set_auth_badge(self, widget, state):
        """Applique un badge visuel pour statut auth: pending / valid / invalid."""
        pending_bg = "#FFC067"
        pending_fg = "#6a4b00"
        valid_bg = "#c6e8d2"
        valid_fg = "#1f2937"
        invalid_bg = "#efc2c7"
        invalid_fg = "#7a1f28"
        if isinstance(state, bool):
            normalized = "valid" if state else "invalid"
        else:
            normalized = str(state or "").strip().lower()
        if normalized in ("pending", "en_attente", "waiting"):
            widget.config(text="Validation en cours", bg=pending_bg, fg=pending_fg)
        elif normalized in ("valid", "ok", "true", "1"):
            widget.config(text="Validé", bg=valid_bg, fg=valid_fg)
        else:
            widget.config(text="A vérifier", bg=invalid_bg, fg=invalid_fg)

    def _apply_auth_tab_state_style(self, state):
        """Memorise l'etat visuel de l'onglet Authentification et rafraichit son rendu."""
        normalized = str(state or "pending").strip().lower()
        if normalized not in {"valid", "pending", "invalid"}:
            normalized = "pending"
        self.auth_tab_visual_state = normalized
        self._refresh_config_tab_buttons()

    def _refresh_config_tab_buttons(self):
        """Rafraichit les onglets visuels Journal / Authentification / Options."""
        if not hasattr(self, "config_tab_buttons"):
            return
        palette = getattr(self, "palette", {}) or {}
        active_key = getattr(self, "active_config_tab", "journal")
        auth_state = getattr(self, "auth_tab_visual_state", "pending")
        auth_colors = {
            "valid": ("#c6e8d2", "#1f2937"),
            "pending": ("#FFC067", "#6a4b00"),
            "invalid": ("#efc2c7", "#7a1f28"),
        }
        default_bg = palette.get("card_alt", "#f3f6fa")
        default_fg = palette.get("muted", "#5f6b7a")
        selected_bg = "#ffffff"
        selected_fg = palette.get("text", "#1f2937")
        border_color = palette.get("border", "#b8c0cb")
        selected_border_color = palette.get("border", "#b8c0cb")

        titles = {
            "journal": "Journal",
            "auth": getattr(self, "auth_tab_title", "Authentification (0/5)"),
            "options": "Options",
        }

        selected_widget = None
        selected_bg_for_mask = selected_bg
        for key, button in self.config_tab_buttons.items():
            is_selected = key == active_key
            if key == "auth":
                bg, fg = auth_colors.get(auth_state, auth_colors["pending"])
            else:
                bg, fg = (selected_bg, selected_fg) if is_selected else (default_bg, default_fg)

            tab_border = selected_border_color if is_selected else border_color

            button.configure(
                text=titles.get(key, ""),
                bg=bg,
                fg=fg,
                activebackground=bg,
                activeforeground=fg,
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=tab_border,
                highlightcolor=tab_border,
            )
            if is_selected:
                selected_widget = button
                selected_bg_for_mask = bg

        self._layout_tab_row(
            getattr(self, "config_tabs_header", None),
            self.config_tab_buttons,
            getattr(self, "config_tab_order", ("journal", "auth", "options")),
        )
        if selected_widget is not None:
            try:
                selected_widget.lift()
            except Exception:
                pass
        self._update_config_top_border_mask(selected_widget, selected_bg_for_mask)

    def _update_config_top_border_mask(self, selected_widget, mask_bg):
        """Masque la ligne haute de l'encart + le bas de l'onglet actif (effet attache)."""
        if not hasattr(self, "config_content_border"):
            return
        if not hasattr(self, "config_top_border_mask"):
            self.config_top_border_mask = tk.Frame(
                self.config_content_border,
                bg=self.palette.get("card_bg", "#f7f9fc"),
                bd=0,
                highlightthickness=0,
            )
        if not hasattr(self, "config_tab_bottom_mask"):
            self.config_tab_bottom_mask = tk.Frame(
                self.config_tabs_header,
                bg=self.palette.get("card_bg", "#f7f9fc"),
                bd=0,
                highlightthickness=0,
            )
        if selected_widget is None:
            self.config_top_border_mask.place_forget()
            self.config_tab_bottom_mask.place_forget()
            return
        try:
            self.root.update_idletasks()
            hx = selected_widget.winfo_x()
            hy = selected_widget.winfo_y()
            hh = selected_widget.winfo_height()
            w = selected_widget.winfo_width()
            header_x = self.config_tabs_header.winfo_x()
            border_x = self.config_content_border.winfo_x()
            x = header_x + hx - border_x
            if w <= 1:
                self.config_top_border_mask.place_forget()
                self.config_tab_bottom_mask.place_forget()
                return
            inner_x = max(1, x + 1)
            inner_w = max(1, w - 2)
            self.config_top_border_mask.configure(bg=mask_bg)
            self.config_top_border_mask.place(
                x=inner_x,
                y=0,
                width=inner_w,
                height=1,
            )
            self.config_top_border_mask.lift()
            self.config_tab_bottom_mask.configure(bg=mask_bg)
            self.config_tab_bottom_mask.place(
                x=max(0, hx),
                y=max(0, hy + hh - 1),
                width=max(1, w),
                height=1,
            )
            self.config_tab_bottom_mask.lift()
        except Exception:
            self.config_top_border_mask.place_forget()
            self.config_tab_bottom_mask.place_forget()

    def _select_config_tab(self, tab_key):
        """Affiche la page de configuration choisie."""
        if not hasattr(self, "config_tab_pages"):
            return
        page = self.config_tab_pages.get(tab_key)
        if page is None:
            return
        self.active_config_tab = tab_key
        for tab_page in self.config_tab_pages.values():
            tab_page.pack_forget()
        page.pack(fill="both", expand=True)
        self._refresh_config_tab_buttons()

    def _refresh_auth_tab_badge(self):
        """Met a jour le titre et la couleur de l'onglet d'authentification."""
        if not hasattr(self, "config_auth_page"):
            return
        states = getattr(self, "auth_badge_states", {}) or {}
        keys = list(COOKIE_DOMAINS) + ["ua"]
        normalized = [str(states.get(key, "pending")).strip().lower() for key in keys]
        valid_count = sum(1 for state in normalized if state == "valid")
        total = len(keys)

        if total > 0 and all(state == "valid" for state in normalized):
            auth_state = "valid"
        elif any(state == "invalid" for state in normalized):
            auth_state = "invalid"
        else:
            auth_state = "pending"
        self.auth_tab_title = f"Authentification ({valid_count}/{total})"
        self._apply_auth_tab_state_style(auth_state)
        self._refresh_config_tab_buttons()

    def _on_config_tab_changed(self, _event=None):
        """Synchronise les onglets visuels avec l'onglet de configuration actif."""
        self._refresh_config_tab_buttons()

    def _finalize_config_panel_layout(self):
        """Stabilise la hauteur du bloc haut pour eviter les sauts entre onglets."""
        if not hasattr(self, "config_content_panel"):
            return
        try:
            self.root.update_idletasks()
        except Exception:
            return
        option_h = getattr(self, "config_options_page", None)
        option_h = option_h.winfo_reqheight() if option_h is not None else 0
        auth_h = getattr(self, "config_auth_page", None)
        auth_h = auth_h.winfo_reqheight() if auth_h is not None else 0
        journal_h = getattr(self, "config_journal_page", None)
        journal_h = journal_h.winfo_reqheight() if journal_h is not None else 0

        target_h = max(option_h, auth_h, journal_h, 220)
        self.config_panel_min_height = target_h
        self.config_content_panel.pack_propagate(False)
        self.config_content_panel.configure(height=target_h)
        self._refresh_config_tab_buttons()

    def run_auth_diagnostics(self):
        """Lance un test complet cookies + User-Agent sur tous les domaines."""
        if not hasattr(self, "analysis_auth_state") or not isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state = {**{domain: None for domain in COOKIE_DOMAINS}, "ua": None}
        if not hasattr(self, "cookie_probe_state") or not isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state = {domain: None for domain in COOKIE_DOMAINS}

        for domain in COOKIE_DOMAINS:
            self.analysis_auth_state[domain] = None
            self.cookie_probe_state[domain] = None
        self.analysis_auth_state["ua"] = None if self.get_direct_user_agent().strip() else False

        self.update_cookie_status(validate=False)
        self.update_runtime_status()
        self.log("Tests Auth lancés (cookies + User-Agent).", level="info")
        self._schedule_startup_ua_probe()
        self._schedule_cookie_listing_probe(domains=COOKIE_DOMAINS, delay_ms=0)

    def _set_analysis_status_label(self, text, success=None):
        """Affiche un retour court sur le resultat d'analyse auth."""
        if not hasattr(self, "status_label"):
            return
        if success is True:
            color = "#0f9d58"
            self._set_workflow_step("select", "Analyse terminée. Sélectionne les tomes à télécharger.")
        elif success is False:
            color = "#d93025"
            self._set_workflow_step("source", "Analyse en échec. Vérifie URL/cookies puis relance.")
        else:
            color = "#2f73d9"
            self._set_workflow_step("source", "Analyse en cours...")
        self.status_label.config(text=repair_mojibake_text(text or ""), foreground=color)

    def _mark_analysis_auth_state(self, domain, success, message=""):
        """Mémorise un résultat auth basé sur une analyse réelle."""
        if domain not in COOKIE_DOMAINS:
            return
        normalized_success = bool(success)
        if not hasattr(self, "analysis_auth_state") or not isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state = {**{domain_key: None for domain_key in COOKIE_DOMAINS}, "ua": None}
        self.analysis_auth_state[domain] = normalized_success
        # Ne pas invalider le User-Agent sur un échec domaine: 403 est souvent cookie-only.
        if normalized_success:
            self.analysis_auth_state["ua"] = True
        elif not self.get_direct_user_agent().strip():
            self.analysis_auth_state["ua"] = False
        self.analysis_auth_last_domain = domain
        self.analysis_auth_last_message = (message or "").strip()
        if normalized_success:
            cookie_value = getattr(self, f"cookie_{domain}").get().strip()
            self._mark_cookie_updated(domain, cookie_value)

        label_text = (
            f"Auth .{domain} validée (liste chargée)"
            if normalized_success
            else (
                f"Auth .{domain} non validée (vérifier cookie .{domain})"
                if self.get_direct_user_agent().strip()
                else f"Auth .{domain} non validée (vérifier cookie .{domain} + User-Agent)"
            )
        )
        if self.analysis_auth_last_message:
            label_text = f"{label_text} - {self.analysis_auth_last_message}"

        self.run_on_ui(lambda: self._set_analysis_status_label(label_text, success=normalized_success))
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)

    def _mark_cookie_updated(self, domain, cookie_value):
        """Met à jour le timestamp local de changement cookie pour le domaine."""
        if domain not in COOKIE_DOMAINS:
            return
        if not hasattr(self, "cookie_updated_at") or not isinstance(self.cookie_updated_at, dict):
            self.cookie_updated_at = {domain_key: "" for domain_key in COOKIE_DOMAINS}
        value = (cookie_value or "").strip()
        if value:
            self.cookie_updated_at[domain] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        else:
            self.cookie_updated_at[domain] = ""

    def _refresh_auth_labels(self, active_domain=None):
        """Met à jour les intitulés auth en mode manuel."""
        _ = active_domain
        self.cookie_fr_label_var.set("Cookie (.fr) :")
        self.cookie_net_label_var.set("Cookie (.net) :")
        self.cookie_origines_label_var.set("Cookie (.origines) :")
        self.cookie_hentai_label_var.set("Cookie (.hentai-origines 🔞) :")
        self.ua_label_var.set("User-Agent :")

    def update_cookie_status(self, validate=True):
        """Met à jour badges et libellés de source pour cookies/UA."""
        _ = validate
        try:
            if not hasattr(self, "cookie_sources"):
                return
            current_url = self.url.get().strip()
            active_domain = self.get_domain_from_url(current_url)
            self._refresh_auth_labels(active_domain=active_domain)
            if not all(
                hasattr(self, name)
                for name in ("cookie_fr_status", "cookie_net_status", "cookie_origines_status", "cookie_hentai_status", "ua_status")
            ):
                return

            badge_states = {}
            for domain in COOKIE_DOMAINS:
                analysis_domain_state = (getattr(self, "analysis_auth_state", {}) or {}).get(domain)
                probe_domain_state = (getattr(self, "cookie_probe_state", {}) or {}).get(domain)
                cookie_var = self._get_cookie_var_for_domain(domain)
                cookie_value = cookie_var.get().strip() if cookie_var is not None else ""

                badge = self._get_cookie_badge_for_domain(domain)
                if badge is None:
                    continue
                if analysis_domain_state is True:
                    badge_state = "valid"
                    valid = True
                elif analysis_domain_state is False:
                    badge_state = "invalid"
                    valid = False
                elif probe_domain_state is True:
                    badge_state = "valid"
                    valid = True
                elif probe_domain_state is False:
                    badge_state = "invalid"
                    valid = False
                else:
                    # Avant analyse (ou reset explicite), on attend le verdict.
                    badge_state = "pending"
                    valid = False
                self.auth_validity[domain] = valid
                badge_states[domain] = badge_state
                self._set_auth_badge(badge, badge_state)

            analysis_ua_state = (getattr(self, "analysis_auth_state", {}) or {}).get("ua")
            ua_present = bool(self.get_direct_user_agent().strip())
            if analysis_ua_state is True:
                ua_badge_state = "valid"
                ua_valid = True
            elif not ua_present:
                ua_badge_state = "invalid"
                ua_valid = False
            else:
                ua_badge_state = "pending"
                ua_valid = False
            self.auth_validity["ua"] = ua_valid
            badge_states["ua"] = ua_badge_state
            self._set_auth_badge(self.ua_status, ua_badge_state)
            self.auth_badge_states = badge_states
            self._refresh_auth_tab_badge()
        except Exception as e:
            self.log(f"Erreur statut cookies: {e}", level="error")

    def _schedule_runtime_status_update(self, *_args):
        """Planifie la mise à jour de la barre d'état."""
        self.run_on_ui(self.update_runtime_status)

    def update_runtime_status(self):
        """Met a jour la barre d'etat de l'application."""
        try:
            current_url = self.url.get().strip()
            domain = self.get_domain_from_url(current_url) or "-"
            active_cookie = ""
            source = "-"
            cookie_sources = getattr(self, "cookie_sources", {}) or {}
            cookie_var = self._get_cookie_var_for_domain(domain)
            if cookie_var is not None:
                active_cookie = cookie_var.get().strip()
                source = (cookie_sources.get(domain) or ("manual" if active_cookie else "none")).strip()

            cookie_state = "présent" if active_cookie else "absent"
            source_display_map = {"manual": "manuel", "none": "aucun"}
            source_display = source_display_map.get(source.lower(), source or "aucun")

            analysis_state = None
            analysis_ua_state = None
            probe_state = None
            if domain in COOKIE_DOMAINS:
                analysis_state = (getattr(self, "analysis_auth_state", {}) or {}).get(domain)
                analysis_ua_state = (getattr(self, "analysis_auth_state", {}) or {}).get("ua")
                probe_state = (getattr(self, "cookie_probe_state", {}) or {}).get(domain)
            ua_present = bool(self.get_direct_user_agent().strip())

            if analysis_state is True and (analysis_ua_state is True or ua_present):
                auth_state = "validée par analyse"
            elif analysis_state is False:
                auth_state = (
                    f"échec: vérifier cookie .{domain}"
                    if ua_present
                    else f"échec: vérifier cookie .{domain} + User-Agent"
                )
            elif probe_state is True:
                auth_state = "cookie teste OK (listing)"
            elif probe_state is False:
                auth_state = f"cookie à vérifier .{domain} (listing KO)"
            else:
                auth_state = "en attente d'analyse"

            status_text = (
                f"Domaine actif: {domain} | Cookie: {cookie_state} ({source_display}) | Auth: {auth_state}"
            )
            self.runtime_status.set(repair_mojibake_text(status_text))
        except Exception as exc:
            self.runtime_status.set(repair_mojibake_text(f"Statut indisponible: {exc}"))

    def _schedule_startup_ua_probe(self):
        """Lance un micro-test User-Agent en fond sur le domaine actif."""
        if not self.get_direct_user_agent().strip():
            return
        threading.Thread(target=self._run_startup_ua_probe, daemon=True).start()

    def _run_startup_ua_probe(self):
        """Micro-test léger: une requête racine sur le domaine actif."""
        try:
            ua_value = self.get_direct_user_agent().strip()
            if not ua_value:
                return

            current_url = self.run_on_ui(self.url.get, wait=True, default="").strip()
            domain = self.get_domain_from_url(current_url)
            if domain not in COOKIE_DOMAINS:
                self.log("Micro-test User-Agent ignoré: aucun domaine actif supporté.", level="debug")
                return

            ua_probe_urls = {
                "fr": "https://sushiscan.fr/",
                "net": "https://sushiscan.net/",
                "origines": "https://mangas-origines.fr/",
                "hentai": "https://hentai-origines.fr/",
            }
            probe_url = ua_probe_urls.get(domain, "")
            if not probe_url:
                return
            response = make_request(probe_url, "", ua_value)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code <= 0:
                return

            if not hasattr(self, "analysis_auth_state") or not isinstance(self.analysis_auth_state, dict):
                self.analysis_auth_state = {**{domain_key: None for domain_key in COOKIE_DOMAINS}, "ua": None}
            self.analysis_auth_state["ua"] = True
            self.run_on_ui(lambda: self.update_cookie_status(validate=False))
            self.run_on_ui(self.update_runtime_status)
            self.log(
                f"Micro-test User-Agent: HTTP {status_code} sur .{domain}, User-Agent validé.",
                level="info",
            )
        except Exception as exc:
            self.log(f"Micro-test User-Agent non concluant: {exc}", level="debug")

    def _schedule_cookie_listing_probe(self, domains=COOKIE_DOMAINS, delay_ms=1200):
        valid_domains = tuple(d for d in (domains or ()) if d in COOKIE_DOMAINS)
        if not valid_domains:
            return
        if not hasattr(self, "cookie_probe_after_ids") or not isinstance(self.cookie_probe_after_ids, dict):
            self.cookie_probe_after_ids = {domain: None for domain in COOKIE_DOMAINS}

        for domain in valid_domains:
            pending_id = self.cookie_probe_after_ids.get(domain)
            if pending_id:
                try:
                    self.root.after_cancel(pending_id)
                except Exception:
                    pass
                self.cookie_probe_after_ids[domain] = None

            def launch_probe(d=domain):
                self.cookie_probe_after_ids[d] = None
                threading.Thread(
                    target=self._run_cookie_listing_probe,
                    args=((d,),),
                    daemon=True,
                ).start()

            if delay_ms and delay_ms > 0:
                self.cookie_probe_after_ids[domain] = self.root.after(delay_ms, launch_probe)
            else:
                launch_probe()

    def _run_cookie_listing_probe(self, domains=COOKIE_DOMAINS):
        try:
            valid_domains = tuple(d for d in (domains or ()) if d in COOKIE_DOMAINS)
            if not valid_domains:
                return
            if not hasattr(self, "cookie_probe_state") or not isinstance(self.cookie_probe_state, dict):
                self.cookie_probe_state = {domain: None for domain in COOKIE_DOMAINS}

            ua_value = self.get_direct_user_agent().strip()
            if not ua_value:
                return

            probe_urls = getattr(self, "cookie_listing_probe_urls", {}) or {}
            for domain in valid_domains:
                cookie_var = self._get_cookie_var_for_domain(domain)
                if cookie_var is None:
                    continue
                cookie_value = self.run_on_ui(cookie_var.get, wait=True, default="").strip()
                previous_state = self.cookie_probe_state.get(domain)

                if not cookie_value:
                    if previous_state is not False:
                        self.cookie_probe_state[domain] = False
                        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
                        self.run_on_ui(self.update_runtime_status)
                    continue

                fallback_probe_urls = {
                    "fr": "https://sushiscan.fr/catalogue/one-piece/",
                    "net": "https://sushiscan.net/catalogue/one-piece/",
                    "origines": "https://mangas-origines.fr/oeuvre/826-solo-leveling/",
                    "hentai": "https://hentai-origines.fr/manga/stop-smoking/",
                }
                probe_url = (probe_urls.get(domain) or "").strip() or fallback_probe_urls.get(domain, "")
                if not probe_url:
                    continue
                probe_ok = False
                failure_reason = ""
                try:
                    _title, pairs = fetch_manga_data(
                        probe_url,
                        cookie_value,
                        ua_value,
                        return_html=False,
                        progress_callback=None,
                        emit_logs=False,
                    )
                    probe_ok = bool(pairs)
                    if not probe_ok:
                        failure_reason = "listing vide"
                except Exception as probe_exc:
                    probe_ok = False
                    failure_reason = str(probe_exc)

                self.cookie_probe_state[domain] = probe_ok
                self.run_on_ui(lambda: self.update_cookie_status(validate=False))
                self.run_on_ui(self.update_runtime_status)

                if probe_ok:
                    if previous_state is not True:
                        self.log(
                            f"Test cookie .{domain} : Réussite.",
                            level="info",
                        )
                else:
                    if previous_state is not False:
                        self.log(
                            f"Test cookie .{domain} : Échec.",
                            level="warning",
                        )
        except Exception as exc:
            self.log(f"Probe cookie non concluant: {exc}", level="debug")

    def __init__(self):
        """Initialise l'interface graphique et charge les paramètres"""
        MangaApp.current_instance = self
        self.total_chapters_to_process = 0
        self.chapters_done = 0
        self.ui_queue = queue.Queue()
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")

        # Fenêtre modernisée: redimensionnable avec taille minimale confortable.
        self.root.geometry("1140x1040")
        self.root.minsize(940, 1040)
        self.root.maxsize(self.root.winfo_screenwidth(), 1070)
        self.root.resizable(True, True)
        self.log_entries = []
        self.log_lock = threading.Lock()
        self.max_log_entries = 5000
        self.log_ready = False
        self.configure_styles()
        
        # Variables Tkinter
        self.cbz_enabled = tk.BooleanVar(value=True)
        self.webp2jpg_enabled = tk.BooleanVar(value=True)
        self.smart_resume_enabled = tk.BooleanVar(value=True)
        self.verbose_logs = tk.BooleanVar(value=True)
        self.url = tk.StringVar()
        self.ua = tk.StringVar()
        self.cookie_fr = tk.StringVar()
        self.cookie_net = tk.StringVar()
        self.cookie_origines = tk.StringVar()
        self.cookie_hentai = tk.StringVar()
        self.cookie_fr_label_var = tk.StringVar(value="Cookie (.fr) :")
        self.cookie_net_label_var = tk.StringVar(value="Cookie (.net) :")
        self.cookie_origines_label_var = tk.StringVar(value="Cookie (.origines) :")
        self.cookie_hentai_label_var = tk.StringVar(value="Cookie (.hentai-origines 🔞) :")
        self.ua_label_var = tk.StringVar(value="User-Agent :")
        self.runtime_status = tk.StringVar(value="Prêt.")
        self.log_filter_level = tk.StringVar(value="all")
        self.log_autoscroll = tk.BooleanVar(value=True)
        self.console_logs_enabled = tk.BooleanVar(value=True)
        self.show_cookies = tk.BooleanVar(value=False)
        self.filter_placeholder_text = "Filtre"
        self.filter_placeholder_active = False
        self.cover_target_height = COVER_TARGET_HEIGHT
        self.auth_validity = {**{domain: False for domain in COOKIE_DOMAINS}, "ua": False}
        self.local_ua_source = "manual"
        self.ua_runtime_validity = None
        self.analysis_auth_state = {**{domain: None for domain in COOKIE_DOMAINS}, "ua": None}
        self.cookie_probe_state = {domain: None for domain in COOKIE_DOMAINS}
        self.cookie_probe_after_ids = {domain: None for domain in COOKIE_DOMAINS}
        self.cookie_listing_probe_urls = {
            "fr": "https://sushiscan.fr/catalogue/one-piece/",
            "net": "https://sushiscan.net/catalogue/one-piece/",
            "origines": "https://mangas-origines.fr/oeuvre/826-solo-leveling/",
            "hentai": "https://hentai-origines.fr/manga/stop-smoking/",
        }
        self.analysis_auth_last_domain = None
        self.analysis_auth_last_message = ""
        self.analysis_in_progress = False
        self.url.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_fr.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_net.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_origines.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_hentai.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_fr.trace_add("write", self._schedule_auth_status_update_cookie_fr)
        self.cookie_net.trace_add("write", self._schedule_auth_status_update_cookie_net)
        self.cookie_origines.trace_add("write", self._schedule_auth_status_update_cookie_origines)
        self.cookie_hentai.trace_add("write", self._schedule_auth_status_update_cookie_hentai)
        self.ua.trace_add("write", self._schedule_auth_status_update)
        self.url.trace_add("write", self._schedule_auth_status_update_url)

        # Chargement du cache
        (
            cookies,
            ua,
            cbz,
            last_url,
            webp2jpg_enabled,
            smart_resume_enabled,
            verbose_logs_enabled,
            cookie_sources,
            cookie_user_agents,
            cookie_headers,
            cookie_updated_at,
        ) = load_cookie_cache()
        self.cookie_fr.set(cookies.get("fr", ""))
        self.cookie_net.set(cookies.get("net", ""))
        self.cookie_origines.set(cookies.get("origines", ""))
        self.cookie_hentai.set(cookies.get("hentai", ""))
        runtime_log(f"{APP_NAME} v{APP_VERSION}", level="info")
        runtime_log(f"Cache cookie : {COOKIE_CACHE_PATH}", level="info")
        runtime_log(f"Config : {CONFIG_PATH}", level="info")
        runtime_log("Mode authentification: manuel.", level="info")
        detected_ua, ua_source = detect_local_user_agent()
        self.local_ua_source = ua_source
        self.ua.set((ua or detected_ua or DEFAULT_USER_AGENT).strip())
        self.cookie_sources = {domain: (cookie_sources.get(domain) or "").strip() for domain in COOKIE_DOMAINS}
        self.cookie_user_agents = {domain: (cookie_user_agents.get(domain) or "").strip() for domain in COOKIE_DOMAINS}
        self.cookie_headers = {domain: (cookie_headers.get(domain) or "").strip() for domain in COOKIE_DOMAINS}
        self.cookie_updated_at = {domain: (cookie_updated_at.get(domain) or "").strip() for domain in COOKIE_DOMAINS}

        direct_ua = (self.ua.get() or DIRECT_USER_AGENT_DEFAULT).strip()
        for domain in COOKIE_DOMAINS:
            cookie_value = (cookies.get(domain) or "").strip()
            source = (self.cookie_sources.get(domain) or "").strip().lower()
            if source != "manual":
                source = "manual" if cookie_value else ""
            self.cookie_sources[domain] = source

            if cookie_value:
                self.cookie_user_agents[domain] = direct_ua
            else:
                self.cookie_user_agents[domain] = ""

            if not self.cookie_headers.get(domain):
                self.cookie_headers[domain] = f"cf_clearance={cookie_value}" if cookie_value else ""

        self.last_known_cookies = {domain: (cookies.get(domain) or "").strip() for domain in COOKIE_DOMAINS}
        self.cbz_enabled.set(str(cbz).lower() in ("1", "true", "yes"))
        self.webp2jpg_enabled.set(str(webp2jpg_enabled).lower() in ("1", "true", "yes"))
        self.smart_resume_enabled.set(str(smart_resume_enabled).lower() in ("1", "true", "yes"))
        self.verbose_logs.set(str(verbose_logs_enabled).lower() in ("1", "true", "yes"))
        self.url.set(last_url)  
        MangaApp.last_url_used = last_url
        
        # Initialisation des composants UI
        self.check_vars = []
        self.check_items = []
        self.image_progress_index = None
        self.download_in_progress = False
        self.pairs = []
        self.title = ""
        self.cancel_event = threading.Event()
        self.cover_preview = None
        self.cover_animation_frames = []
        self.cover_animation_durations = []
        self.cover_animation_index = 0
        self.cover_animation_after_id = None
        self.volume_error_entries = []
        self.download_output_root = os.path.abspath(ROOT_FOLDER)

        # Configuration de l'interface
        self.setup_ui()
        self._toggle_cookie_visibility()
        self.normalize_display_texts()
        self.log_ready = True
        self.refresh_log_view()
        self.root.bind("<Return>", lambda _e: self.load_volumes())
        self.root.bind("<Control-Return>", self._shortcut_analyze)
        self.root.bind("<Control-KP_Enter>", self._shortcut_analyze)
        self.root.bind("<Control-d>", self._shortcut_download)
        self.root.bind("<Control-D>", self._shortcut_download)
        self.root.bind("<Control-f>", self._shortcut_focus_filter)
        self.root.bind("<Control-F>", self._shortcut_focus_filter)
        self.root.bind("<Control-l>", self._shortcut_focus_logs)
        self.root.bind("<Control-L>", self._shortcut_focus_logs)
        self.root.bind("<Control-s>", lambda _e: self.save_current_cookie())
        self.root.bind("<Escape>", lambda _e: self.cancel_download())
        self.root.after(30, self.process_ui_queue)
        self.update_cookie_status(validate=False)
        self.update_runtime_status()
        self.root.after(600, self._schedule_startup_ua_probe)
        self.root.after(900, lambda: self._schedule_cookie_listing_probe(domains=COOKIE_DOMAINS, delay_ms=0))

        self.log(f"Application démarrée - {APP_NAME} v{APP_VERSION}.", level="info")
        self.root.mainloop()

    def log(self, message, level="info", context=None):
        """Ajoute une entrée de log unifiée (GUI + terminal)."""
        text = repair_mojibake_text(str(message or "").strip())
        if not text:
            return

        normalized_level = normalize_log_level(level)
        timestamp = time.strftime("%H:%M:%S")
        context_suffix = format_log_context(context)
        full_message = f"{text}{context_suffix}"
        entry = {
            "timestamp": timestamp,
            "level": normalized_level,
            "message": full_message,
        }
        with self.log_lock:
            self.log_entries.append(entry)
            if len(self.log_entries) > self.max_log_entries:
                self.log_entries = self.log_entries[-self.max_log_entries:]

        if getattr(self, "log_ready", False) and hasattr(self, "log_text"):
            self.run_on_ui(self._append_log_entry, entry)

        verbose_enabled = self.run_on_ui(
            self.verbose_logs.get,
            wait=True,
            default=True,
        )
        if normalized_level == "debug" and not verbose_enabled:
            return

        console_enabled = self.run_on_ui(
            self.console_logs_enabled.get,
            wait=True,
            default=True,
        )
        if console_enabled:
            emit_console_log(
                message=text,
                level=normalized_level,
                context=context,
                timestamp=timestamp,
                with_emoji=CONSOLE_USE_EMOJI,
            )

    def _should_display_log_entry(self, entry):
        """Filtre d'affichage du journal GUI."""
        level = normalize_log_level(entry.get("level", "info"))
        selected = (self.log_filter_level.get() or "all").strip().lower()
        verbose_enabled = bool(self.verbose_logs.get())
        if not verbose_enabled and level == "debug":
            return False
        if selected == "all":
            return True
        return level == selected

    def _format_log_entry(self, entry):
        """Formate une entrée pour affichage dans le widget log."""
        level = normalize_log_level(entry.get("level", "info"))
        timestamp = entry.get("timestamp") or time.strftime("%H:%M:%S")
        message = repair_mojibake_text(entry.get("message", ""))
        emoji = LOG_EMOJIS.get(level, "") if GUI_USE_EMOJI else ""
        if emoji:
            return repair_mojibake_text(f"[{timestamp}] {emoji} {message}")
        return repair_mojibake_text(f"[{timestamp}] {message}")

    def _insert_log_line(self, text, level):
        """Insere une ligne dans le journal sans laisser de ligne vide finale."""
        if self.log_text.compare("end-1c", ">", "1.0"):
            self.log_text.insert("end-1c", "\n")
        self.log_text.insert("end-1c", text, level)

    def _scroll_log_to_bottom(self):
        """Place la vue sur la derniere ligne utile du journal."""
        try:
            self.log_text.see("end-2c")
        except Exception:
            self.log_text.see("end-1c")

    def _append_log_entry(self, entry):
        """Ajoute une entrée dans la vue GUI si elle passe les filtres."""
        if not self._should_display_log_entry(entry):
            return
        entry["message"] = repair_mojibake_text(entry.get("message", ""))
        level = normalize_log_level(entry.get("level", "info"))
        formatted = self._format_log_entry(entry)
        self.log_text.configure(state="normal")
        self._insert_log_line(formatted, level)
        self.log_text.configure(state="disabled")
        if self.log_autoscroll.get():
            self._scroll_log_to_bottom()

    def refresh_log_view(self, *_args):
        """Rafraîchit le journal GUI selon les filtres actifs."""
        with self.log_lock:
            entries_snapshot = list(self.log_entries)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for entry in entries_snapshot:
            entry["message"] = repair_mojibake_text(entry.get("message", ""))
            if not self._should_display_log_entry(entry):
                continue
            level = normalize_log_level(entry.get("level", "info"))
            self._insert_log_line(self._format_log_entry(entry), level)
        self.log_text.configure(state="disabled")
        if self.log_autoscroll.get():
            self._scroll_log_to_bottom()

    def clear_log_entries(self):
        """Efface le journal en mémoire et dans l'UI."""
        with self.log_lock:
            self.log_entries.clear()
        self.refresh_log_view()

    def copy_visible_logs(self):
        """Copie le contenu visible du journal dans le presse-papiers."""
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            self.log("Le journal est vide.", level="warning")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.log("Journal copié dans le presse-papiers.", level="success")

    def export_visible_logs(self):
        """Exporte le journal visible dans un fichier texte."""
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            self.log("Le journal est vide.", level="warning")
            return
        default_name = f"sushidl_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        out_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Exporter le journal",
            defaultextension=".log",
            initialfile=default_name,
            filetypes=[("Fichier log", "*.log"), ("Texte", "*.txt"), ("Tous les fichiers", "*.*")],
        )
        if not out_path:
            return
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            self.log(f"Journal exporté: {out_path}", level="success")
        except Exception as exc:
            self.log(f"Erreur export journal: {exc}", level="error")

    def _append_volume_error_row(self, entry):
        if not hasattr(self, "error_tree"):
            return
        status_code = entry.get("status_code")
        status_text = "" if status_code in (None, "") else str(status_code)
        values = (
            entry.get("time", ""),
            entry.get("tome", ""),
            entry.get("stage", ""),
            status_text,
            entry.get("reason", ""),
            entry.get("action", ""),
        )
        self.error_tree.insert("", "end", values=values)
        children = self.error_tree.get_children()
        if len(children) > 500:
            for item_id in children[:-500]:
                self.error_tree.delete(item_id)

    def add_volume_error(self, tome, stage, reason, status_code=None, action=None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "tome": repair_mojibake_text((tome or "").strip() or "?"),
            "stage": repair_mojibake_text((stage or "").strip() or "download"),
            "status_code": status_code if status_code not in ("", None) else "",
            "reason": repair_mojibake_text((reason or "").strip() or "Erreur inconnue"),
            "action": repair_mojibake_text(
                (action or "").strip() or recommend_action_for_failure(status_code, reason)
            ),
        }
        self.volume_error_entries.append(entry)
        if len(self.volume_error_entries) > 2000:
            self.volume_error_entries = self.volume_error_entries[-2000:]
        self.run_on_ui(self._append_volume_error_row, entry)
        self.run_on_ui(self._update_error_tab_title, True)

    def clear_volume_errors(self):
        self.volume_error_entries = []
        if hasattr(self, "error_tree"):
            for item_id in self.error_tree.get_children():
                self.error_tree.delete(item_id)
        self.run_on_ui(self._update_error_tab_title, False)

    def export_volume_errors(self):
        if not self.volume_error_entries:
            self.log("Aucune erreur tome à exporter.", level="info")
            return
        default_name = f"sushidl_erreurs_tomes_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        out_path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Exporter les erreurs par tome",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV", "*.csv"), ("Tous les fichiers", "*.*")],
        )
        if not out_path:
            return
        try:
            with open(out_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["time", "tome", "stage", "status_code", "reason", "action"]
                )
                writer.writeheader()
                for entry in self.volume_error_entries:
                    writer.writerow(entry)
            self.log(f"Erreurs par tome exportées: {out_path}", level="success")
        except Exception as exc:
            self.log(f"Erreur export erreurs par tome: {exc}", level="error")

    def copy_volume_errors(self):
        if not self.volume_error_entries:
            self.log("Aucune erreur tome à copier.", level="info")
            return
        try:
            rows = ["Heure\tTome\tÉtape\tHTTP\tCause\tAction recommandée"]
            for entry in self.volume_error_entries:
                status_code = entry.get("status_code")
                status_text = "" if status_code in (None, "") else str(status_code)
                fields = [
                    str(entry.get("time", "")),
                    str(entry.get("tome", "")),
                    str(entry.get("stage", "")),
                    status_text,
                    str(entry.get("reason", "")),
                    str(entry.get("action", "")),
                ]
                cleaned = [field.replace("\t", " ").replace("\r", " ").replace("\n", " ") for field in fields]
                rows.append("\t".join(cleaned))
            content = "\n".join(rows)
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update_idletasks()
            self.log("Erreurs par tome copiées dans le presse-papiers.", level="success")
        except Exception as exc:
            self.log(f"Erreur copie erreurs par tome: {exc}", level="error")

    def toast(self, message):
        """Affiche une notification temporaire"""
        def _show():
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

        self.run_on_ui(_show)

    def configure_styles(self):
        """Configure un style moderne inspire de Breeze (clair + accent bleu)."""
        style = ttk.Style(self.root)
        available = set(style.theme_names())
        preferred = "clam" if "clam" in available else style.theme_use()
        try:
            style.theme_use(preferred)
        except Exception:
            pass

        self.palette = {
            "app_bg": "#f5f7fa",
            "card_bg": "#f7f9fc",
            "card_alt": "#f3f6fa",
            "text": "#1f2937",
            "muted": "#5f6b7a",
            "accent": "#1f7ae0",
            "accent_hover": "#1866bf",
            "danger": "#d14343",
            "border": "#dce3ec",
            "canvas_bg": "#f3f6fa",
            "log_bg": "#f7f9fc",
            "progress_trough": "#dfe6ee",
        }

        self.root.configure(bg=self.palette["app_bg"])

        style.configure(
            ".",
            font=("Segoe UI", 10),
            background=self.palette["app_bg"],
            foreground=self.palette["text"],
            troughcolor=self.palette["app_bg"],
            selectbackground=self.palette["accent"],
            selectforeground="#ffffff",
        )
        style.map(".", foreground=[("disabled", "#9aa8b8")])
        style.configure("App.TFrame", background=self.palette["app_bg"])
        style.configure("Card.TFrame", background=self.palette["card_bg"])
        style.configure(
            "Card.TLabelframe",
            background=self.palette["card_bg"],
            borderwidth=1,
            relief="solid",
            padding=(12, 10, 12, 12),
        )
        style.configure(
            "Card.TLabelframe.Label",
            background="#ffffff",
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 9),
            padding=(10, 2),
            borderwidth=1,
            relief="solid",
        )
        style.configure("App.TLabel", background=self.palette["app_bg"], foreground=self.palette["text"])
        style.configure("Card.TLabel", background=self.palette["card_bg"], foreground=self.palette["text"])
        style.configure("CardMuted.TLabel", background=self.palette["card_bg"], foreground=self.palette["muted"])
        style.configure("Muted.TLabel", background=self.palette["app_bg"], foreground=self.palette["muted"])
        style.configure("Title.TLabel", background=self.palette["app_bg"], foreground=self.palette["text"], font=("Segoe UI Semibold", 16))
        style.configure("Subtitle.TLabel", background=self.palette["app_bg"], foreground=self.palette["muted"], font=("Segoe UI", 9))

        style.configure("Card.TCheckbutton", background=self.palette["card_bg"], foreground=self.palette["text"], padding=(2, 1))
        style.map("Card.TCheckbutton", background=[("active", self.palette["card_bg"])])
        style.configure("Tome.TCheckbutton", background=self.palette["canvas_bg"], foreground=self.palette["text"], padding=(2, 1))
        style.map("Tome.TCheckbutton", background=[("active", self.palette["canvas_bg"])])

        style.configure(
            "Card.TEntry",
            fieldbackground=self.palette["card_alt"],
            foreground=self.palette["text"],
            background=self.palette["card_alt"],
            padding=4,
        )
        style.map("Card.TEntry", fieldbackground=[("disabled", "#eceff1")])
        style.configure(
            "Card.TCombobox",
            fieldbackground=self.palette["card_alt"],
            foreground=self.palette["text"],
            background=self.palette["card_alt"],
            padding=3,
        )
        style.map(
            "Card.TCombobox",
            fieldbackground=[("readonly", self.palette["card_alt"])],
            background=[("readonly", self.palette["card_alt"])],
            foreground=[("readonly", self.palette["text"])],
        )

        style.configure(
            "Primary.TButton",
            foreground="#ffffff",
            background=self.palette["accent"],
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
            borderwidth=1,
            relief="raised",
        )
        style.map(
            "Primary.TButton",
            background=[("active", self.palette["accent_hover"]), ("disabled", "#94a3b8")],
            foreground=[("disabled", "#f8fafc")],
            relief=[("pressed", "sunken"), ("active", "raised"), ("!disabled", "raised"), ("disabled", "raised")],
        )
        style.configure(
            "Download.TButton",
            foreground="#1f2937",
            background="#bfe8c7",
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
            borderwidth=1,
        )
        style.map(
            "Download.TButton",
            background=[("active", "#a9dbb4"), ("disabled", "#deefe2")],
            foreground=[("disabled", "#63736a")],
        )
        style.configure(
            "Cancel.TButton",
            foreground="#ffffff",
            background="#d45757",
            padding=(10, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Cancel.TButton",
            background=[("active", "#bf4949"), ("disabled", "#efc4c4")],
            foreground=[("disabled", "#fff4f8")],
        )
        style.configure(
            "Secondary.TButton",
            foreground=self.palette["text"],
            background="#ffffff",
            padding=(10, 6),
            font=("Segoe UI", 9),
            borderwidth=1,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#f4f7fb"), ("disabled", "#eff3f8")],
            foreground=[("disabled", "#9aa1a9")],
        )

        style.configure("TNotebook", background=self.palette["app_bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background=self.palette["card_alt"],
            foreground=self.palette["muted"],
            padding=(11, 5),
            font=("Segoe UI Semibold", 9),
            borderwidth=1,
            relief="flat",
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff"), ("active", "#ebf1f9"), ("!selected", self.palette["card_alt"])],
            foreground=[("selected", self.palette["text"]), ("active", self.palette["text"])],
            padding=[("selected", (11, 5)), ("!selected", (11, 5))],
            expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
            relief=[("selected", "flat"), ("!selected", "flat")],
        )
        style.configure(
            "ConfigContent.TNotebook",
            background=self.palette["card_bg"],
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.layout("ConfigContent.TNotebook.Tab", [])
        style.configure(
            "Treeview",
            background=self.palette["card_bg"],
            fieldbackground=self.palette["card_bg"],
            foreground=self.palette["text"],
            rowheight=22,
            bordercolor=self.palette["border"],
            lightcolor=self.palette["border"],
            darkcolor=self.palette["border"],
        )
        style.configure(
            "Treeview.Heading",
            background=self.palette["card_alt"],
            foreground=self.palette["text"],
            borderwidth=1,
            relief="solid",
            padding=(6, 4),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Treeview.Heading", background=[("active", "#e9f0f9")])

        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=self.palette["progress_trough"],
            background=self.palette["accent"],
            thickness=14,
        )

    def setup_ui(self):
        """Configure tous les elements de l'interface graphique."""
        self.progress = tk.DoubleVar(value=0)

        def create_titled_section(parent, title, bottom_margin):
            section_wrap = ttk.Frame(parent, style="App.TFrame")
            section_wrap.pack(fill="x", expand=False, pady=(0, bottom_margin))
            section_tabs_header = tk.Frame(
                section_wrap,
                bg=self.palette["app_bg"],
                bd=0,
                highlightthickness=0,
            )
            section_tabs_header.pack(fill="x", expand=False, pady=(0, 0))
            section_tab_label = tk.Label(
                section_tabs_header,
                text=title,
                font=("Segoe UI Semibold", 9),
                padx=11,
                pady=4,
                bd=0,
                relief="flat",
                bg="#ffffff",
                fg=self.palette["text"],
                highlightthickness=1,
                highlightbackground=self.palette["border"],
                highlightcolor=self.palette["border"],
            )
            section_tab_label.pack(side="left", padx=(0, 0), pady=(0, 0))

            section_border = tk.Frame(
                section_wrap,
                bg=self.palette["card_bg"],
                highlightbackground=self.palette["border"],
                highlightcolor=self.palette["border"],
                highlightthickness=1,
                bd=0,
            )
            section_border.pack(fill="x", expand=False)

            section_top_mask = tk.Frame(
                section_border,
                bg="#ffffff",
                bd=0,
                highlightthickness=0,
            )
            section_top_mask.place(x=1, y=0, width=2, height=1)

            section_content_panel = tk.Frame(
                section_border,
                bg=self.palette["card_bg"],
                bd=0,
                highlightthickness=0,
            )
            section_content_panel.pack(fill="both", expand=True, padx=0, pady=0)

            section_content = ttk.Frame(section_content_panel, style="Card.TFrame")
            section_content.pack(fill="both", expand=True, padx=12, pady=9)

            def update_section_mask(_event=None):
                try:
                    section_wrap.update_idletasks()
                    x = section_tabs_header.winfo_x() + section_tab_label.winfo_x() - section_border.winfo_x() + 1
                    w = max(1, section_tab_label.winfo_width() - 2)
                    section_top_mask.place_configure(x=x, width=w)
                except Exception:
                    pass

            section_tab_label.bind("<Configure>", update_section_mask)
            section_tabs_header.bind("<Configure>", update_section_mask)
            self.root.after_idle(update_section_mask)
            return section_content, section_wrap

        main_frame = ttk.Frame(self.root, style="App.TFrame", padding=(18, 8))
        main_frame.pack(fill="both", expand=True)

        top_tabs_wrap = ttk.Frame(main_frame, style="App.TFrame")
        top_tabs_wrap.pack(fill="x", expand=False, pady=(0, 6))

        self.config_tabs_header = tk.Frame(
            top_tabs_wrap,
            bg=self.palette["app_bg"],
            bd=0,
            highlightthickness=0,
        )
        self.config_tabs_header.pack(fill="x", expand=False, pady=(0, 0))

        self.config_content_border = tk.Frame(
            top_tabs_wrap,
            bg=self.palette["card_bg"],
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["border"],
            highlightthickness=1,
            bd=0,
        )
        self.config_content_border.pack(fill="x", expand=False, pady=(0, 0))
        self.config_content_panel = tk.Frame(
            self.config_content_border,
            bg=self.palette["card_bg"],
            bd=0,
            highlightthickness=0,
        )
        self.config_content_panel.pack(fill="both", expand=True, padx=0, pady=0)

        self.config_journal_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_auth_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_options_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")

        self.config_tab_pages = {
            "journal": self.config_journal_page,
            "auth": self.config_auth_page,
            "options": self.config_options_page,
        }
        self.config_tab_buttons = {}
        self.config_tab_order = ("journal", "auth", "options")
        self.active_config_tab = "journal"
        self.auth_tab_title = "Authentification (0/5)"
        self.auth_tab_visual_state = "pending"

        for idx, (key, title) in enumerate((("journal", "Journal"), ("auth", self.auth_tab_title), ("options", "Options"))):
            button = tk.Label(
                self.config_tabs_header,
                text=title,
                font=("Segoe UI Semibold", 9),
                cursor="hand2",
                padx=11,
                pady=4,
                bd=0,
                relief="flat",
                highlightthickness=1,
            )
            button.bind("<Button-1>", lambda _event, tab_key=key: self._select_config_tab(tab_key))
            self.config_tab_buttons[key] = button
        self.config_tabs_header.bind(
            "<Configure>",
            lambda _e: self._layout_tab_row(self.config_tabs_header, self.config_tab_buttons, self.config_tab_order),
        )

        def create_config_tab_content(parent):
            content = ttk.Frame(parent, style="Card.TFrame", padding=(12, 12, 12, 12))
            content.pack(fill="both", expand=True)
            return content

        self.config_journal_tab = create_config_tab_content(self.config_journal_page)
        self.config_auth_tab = create_config_tab_content(self.config_auth_page)
        self.config_options_tab = create_config_tab_content(self.config_options_page)
        self._refresh_config_tab_buttons()
        self._select_config_tab("journal")
        self.config_auth_tab.grid_columnconfigure(1, weight=1)

        log_frame = ttk.Frame(self.config_journal_tab, style="Card.TFrame")
        log_frame.pack(fill="both", expand=True)
        log_toolbar = ttk.Frame(log_frame, style="Card.TFrame")
        log_toolbar.pack(fill="x", pady=(0, 4))
        ttk.Label(log_toolbar, text="Niveau:", style="Card.TLabel", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.log_filter_combo = ttk.Combobox(
            log_toolbar,
            width=9,
            state="readonly",
            values=["all", "info", "success", "warning", "error", "debug", "cbz"],
            textvariable=self.log_filter_level,
            style="Card.TCombobox",
        )
        self.log_filter_combo.pack(side="left")
        self.log_filter_combo.bind("<<ComboboxSelected>>", self.refresh_log_view)
        self.log_filter_combo.set("all")
        ttk.Checkbutton(
            log_toolbar,
            text="Auto-scroll",
            variable=self.log_autoscroll,
            style="Card.TCheckbutton",
        ).pack(side="left", padx=(10, 0))
        ttk.Button(log_toolbar, text="Effacer", command=self.clear_log_entries, style="Secondary.TButton").pack(side="right", padx=(4, 0))
        ttk.Button(log_toolbar, text="Copier", command=self.copy_visible_logs, style="Secondary.TButton").pack(side="right", padx=(4, 0))
        ttk.Button(log_toolbar, text="Exporter", command=self.export_visible_logs, style="Secondary.TButton").pack(side="right")

        log_text_container = ttk.Frame(log_frame, style="Card.TFrame")
        log_text_container.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_text_container,
            height=6,
            state="disabled",
            wrap="word",
            bg=self.palette["log_bg"],
            fg=self.palette["text"],
            font=("Consolas", 9),
            relief="flat",
            bd=0,
            padx=8,
            pady=4,
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_text_container, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.tag_config("debug", foreground="#64748b")
        self.log_text.tag_config("success", foreground="#27ae60")
        self.log_text.tag_config("info", foreground="#3daee9")
        self.log_text.tag_config("error", foreground="#da4453")
        self.log_text.tag_config("warning", foreground="#f67400")
        self.log_text.tag_config("cbz", foreground="#7c3aed")

        font_label = ("Segoe UI", 10)
        font_entry = ("Segoe UI", 10)
        row = 0

        ttk.Label(
            self.config_auth_tab,
            textvariable=self.cookie_fr_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.cookie_fr_entry = ttk.Entry(
            self.config_auth_tab, textvariable=self.cookie_fr, width=64, font=font_entry, style="Card.TEntry", show="*"
        )
        self.cookie_fr_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.cookie_fr_status = tk.Label(
            self.config_auth_tab,
            text="Validation en cours",
            font=("Segoe UI Semibold", 9),
            fg="#1f2937",
            bg="#FFC067",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.cookie_fr_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        ttk.Label(
            self.config_auth_tab,
            textvariable=self.cookie_net_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.cookie_net_entry = ttk.Entry(
            self.config_auth_tab, textvariable=self.cookie_net, width=64, font=font_entry, style="Card.TEntry", show="*"
        )
        self.cookie_net_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.cookie_net_status = tk.Label(
            self.config_auth_tab,
            text="Validation en cours",
            font=("Segoe UI Semibold", 9),
            fg="#1f2937",
            bg="#FFC067",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.cookie_net_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        ttk.Label(
            self.config_auth_tab,
            textvariable=self.cookie_origines_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.cookie_origines_entry = ttk.Entry(
            self.config_auth_tab, textvariable=self.cookie_origines, width=64, font=font_entry, style="Card.TEntry", show="*"
        )
        self.cookie_origines_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.cookie_origines_status = tk.Label(
            self.config_auth_tab,
            text="Validation en cours",
            font=("Segoe UI Semibold", 9),
            fg="#1f2937",
            bg="#FFC067",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.cookie_origines_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        ttk.Label(
            self.config_auth_tab,
            textvariable=self.cookie_hentai_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.cookie_hentai_entry = ttk.Entry(
            self.config_auth_tab, textvariable=self.cookie_hentai, width=64, font=font_entry, style="Card.TEntry", show="*"
        )
        self.cookie_hentai_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.cookie_hentai_status = tk.Label(
            self.config_auth_tab,
            text="Validation en cours",
            font=("Segoe UI Semibold", 9),
            fg="#1f2937",
            bg="#FFC067",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.cookie_hentai_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        ttk.Label(
            self.config_auth_tab,
            textvariable=self.ua_label_var,
            style="Card.TLabel",
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=4, padx=(4, 8)
        )
        self.ua_entry = ttk.Entry(self.config_auth_tab, textvariable=self.ua, font=font_entry, style="Card.TEntry")
        self.ua_entry.grid(row=row, column=1, pady=4, sticky="ew")
        self.ua_status = tk.Label(
            self.config_auth_tab,
            text="Validation en cours",
            font=("Segoe UI Semibold", 9),
            fg="#1f2937",
            bg="#FFC067",
            padx=10,
            pady=3,
            relief="solid",
            borderwidth=1,
            width=16,
        )
        self.ua_status.grid(row=row, column=2, sticky="w", padx=10)
        row += 1

        auth_actions_row = ttk.Frame(self.config_auth_tab, style="Card.TFrame")
        auth_actions_row.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 2), padx=(4, 4))
        auth_actions_row.columnconfigure(0, weight=1)
        ttk.Button(
            auth_actions_row,
            text="Tester tout",
            command=self.run_auth_diagnostics,
            style="Secondary.TButton",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            auth_actions_row,
            text="Aide Cookie",
            command=lambda: self._open_external_link(
                get_manual_link(
                    "cookie_help",
                    get_manual_link(
                        "cloudflare_help",
                        "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
                    ),
                )
            ),
            style="Secondary.TButton",
        ).grid(row=0, column=1, sticky="e")
        row += 1

        options_intro = ttk.Label(
            self.config_options_tab,
            text="Ajuste le format de sortie et le comportement des journaux.",
            style="CardMuted.TLabel",
            font=("Segoe UI", 9),
        )
        options_intro.pack(anchor="w", padx=(6, 6), pady=(2, 10))

        options_groups = ttk.Frame(self.config_options_tab, style="Card.TFrame")
        options_groups.pack(fill="x", padx=(4, 4), pady=(0, 2))
        options_groups.grid_columnconfigure(0, weight=1, uniform="options_col")
        options_groups.grid_columnconfigure(1, weight=1, uniform="options_col")
        options_groups.grid_rowconfigure(0, weight=1)

        left_stack = ttk.Frame(options_groups, style="Card.TFrame")
        left_stack.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        left_stack.grid_columnconfigure(0, weight=1)
        left_stack.grid_rowconfigure(0, weight=0)
        left_stack.grid_rowconfigure(1, weight=1)

        output_box = tk.Frame(
            left_stack,
            bg=self.palette["card_bg"],
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["border"],
            highlightthickness=1,
            bd=0,
            takefocus=0,
        )
        output_box.grid(row=0, column=0, sticky="nsew")
        output_inner = ttk.Frame(output_box, style="Card.TFrame")
        output_inner.pack(fill="both", expand=True, padx=10, pady=(8, 8))

        save_row = ttk.Frame(left_stack, style="Card.TFrame")
        save_row.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        logs_box = tk.Frame(
            options_groups,
            bg=self.palette["card_bg"],
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["border"],
            highlightthickness=1,
            bd=0,
            takefocus=0,
        )
        logs_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        logs_inner = ttk.Frame(logs_box, style="Card.TFrame")
        logs_inner.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(
            output_inner,
            text="Sortie",
            style="Card.TLabel",
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            logs_inner,
            text="Journal et affichage",
            style="Card.TLabel",
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Button(
            save_row,
            text="Sauvegarder paramètres",
            command=self.save_current_cookie,
            style="Primary.TButton",
        ).pack(expand=True)

        def add_option_line(parent, text, variable, description, command=None, bottom=8):
            line = ttk.Frame(parent, style="Card.TFrame")
            line.pack(fill="x", anchor="w", pady=(0, bottom))
            check = ttk.Checkbutton(
                line,
                text=text,
                variable=variable,
                style="Card.TCheckbutton",
                command=command,
            )
            check.pack(anchor="w")
            ttk.Label(
                line,
                text=description,
                style="CardMuted.TLabel",
                font=("Segoe UI", 8),
            ).pack(anchor="w", padx=(24, 0), pady=(0, 1))

        add_option_line(
            output_inner,
            ".CBZ",
            self.cbz_enabled,
            "Crée une archive CBZ par tome/chapitre téléchargé.",
            bottom=6,
        )
        add_option_line(
            output_inner,
            "WEBP en JPG",
            self.webp2jpg_enabled,
            "Convertit les images WEBP en JPG pour une compatibilité maximale.",
            bottom=0,
        )
        add_option_line(
            logs_inner,
            "Reprise intelligente",
            self.smart_resume_enabled,
            "Reprend uniquement les pages manquantes après interruption.",
        )
        add_option_line(
            logs_inner,
            "Logs détaillés",
            self.verbose_logs,
            "Affiche les étapes techniques complètes dans le journal.",
            command=self.refresh_log_view,
        )
        add_option_line(
            logs_inner,
            "Logs terminal",
            self.console_logs_enabled,
            "Duplique les logs dans la console pour le diagnostic.",
        )
        add_option_line(
            logs_inner,
            "Afficher cookies",
            self.show_cookies,
            "Affiche les valeurs réelles des cookies dans les champs.",
            command=self._toggle_cookie_visibility,
            bottom=0,
        )

        self._setup_auth_link_placeholders()

        source_card, self.source_section_tabs = create_titled_section(main_frame, "Sources", 6)

        url_cover_frame = ttk.Frame(source_card, style="Card.TFrame")
        url_cover_frame.pack(fill="x")
        cover_w, cover_h = self.get_cover_target_size()

        self.cover_frame = tk.Frame(
            url_cover_frame,
            width=cover_w,
            height=cover_h,
            bg=self.palette["card_alt"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=2,
            relief="sunken",
        )
        self.cover_frame.pack_propagate(False)
        self.cover_frame.pack(side="left", padx=(4, 14), pady=2)
        self.cover_label = tk.Label(
            self.cover_frame,
            bg="#ffffff",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            text="",
            fg=self.palette["muted"],
            font=("Segoe UI", 9),
        )
        self.cover_label.pack(fill="both", expand=True)
        self._show_default_cover_placeholder()

        url_frame = ttk.Frame(url_cover_frame, style="Card.TFrame")
        url_frame.pack(side="left", fill="x", expand=True)

        ttk.Label(url_frame, text="URL du Manga/Manhwa/BD :", style="Card.TLabel", font=font_label).pack(anchor="w")
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url, font=font_entry, style="Card.TEntry")
        self.url_entry.pack(fill="x", pady=(2, 0))
        self._attach_link_placeholder(
            self.url_entry,
            self.url,
            "https://sushiscan.fr/catalogue/slug/ ou https://mangas-origines.fr/oeuvre/slug/ ou https://hentai-origines.fr/manga/slug/ (🔞)",
            None,
        )

        analyze_frame = ttk.Frame(url_frame, style="Card.TFrame")
        analyze_frame.pack(pady=(6, 0), anchor="w")
        self.analyze_button = ttk.Button(
            analyze_frame,
            text="Analyser le lien",
            command=self.load_volumes,
            style="Primary.TButton",
        )
        self.analyze_button.pack(side="left")
        self.status_label = ttk.Label(analyze_frame, text="", style="Card.TLabel", font=("Segoe UI", 9))
        self.status_label.pack(side="left", padx=(12, 0))

        selection_wrap = ttk.Frame(main_frame, style="App.TFrame")
        selection_wrap.pack(fill="both", expand=True, pady=(0, 4))
        self.selection_tabs_header = tk.Frame(
            selection_wrap,
            bg=self.palette["app_bg"],
            bd=0,
            highlightthickness=0,
        )
        self.selection_tabs_header.pack(fill="x", expand=False, pady=(0, 0))
        self.selection_content_border = tk.Frame(
            selection_wrap,
            bg=self.palette["card_bg"],
            highlightbackground=self.palette["border"],
            highlightcolor=self.palette["border"],
            highlightthickness=1,
            bd=0,
        )
        self.selection_content_border.pack(fill="both", expand=True, pady=(0, 0))
        self.selection_content_panel = tk.Frame(
            self.selection_content_border,
            bg=self.palette["card_bg"],
            bd=0,
            highlightthickness=0,
        )
        self.selection_content_panel.pack(fill="both", expand=True, padx=0, pady=0)

        selection_tab = ttk.Frame(self.selection_content_panel, style="Card.TFrame")
        self.error_tab = ttk.Frame(self.selection_content_panel, style="Card.TFrame")
        self.selection_tab_pages = {
            "selection": selection_tab,
            "error": self.error_tab,
        }
        self.selection_tab_buttons = {}
        self.selection_tab_order = ("selection", "error")
        self.active_selection_tab = "selection"
        self.error_tab_title = "Erreurs (0)"

        for idx, (key, title) in enumerate((("selection", "Tomes / Chapitres"), ("error", self.error_tab_title))):
            button = tk.Label(
                self.selection_tabs_header,
                text=title,
                font=("Segoe UI Semibold", 9),
                cursor="hand2",
                padx=11,
                pady=4,
                bd=0,
                relief="flat",
                highlightthickness=1,
            )
            button.bind("<Button-1>", lambda _event, tab_key=key: self._select_selection_tab(tab_key))
            self.selection_tab_buttons[key] = button
        self.selection_tabs_header.bind(
            "<Configure>",
            lambda _e: self._layout_tab_row(
                self.selection_tabs_header, self.selection_tab_buttons, self.selection_tab_order
            ),
        )
        self._refresh_selection_tab_buttons()
        self._select_selection_tab("selection")

        selection_panel = tk.Frame(
            selection_tab,
            bg=self.palette["card_bg"],
            highlightbackground=self.palette["border"],
            highlightthickness=0,
            bd=0,
        )
        selection_panel.pack(fill="both", expand=True)
        center_card = ttk.Frame(selection_panel, style="Card.TFrame")
        center_card.pack(fill="both", expand=True, padx=12, pady=9)

        vol_header = ttk.Frame(center_card, style="Card.TFrame")
        vol_header.pack(fill="x", pady=(0, 4))

        left_group = ttk.Frame(vol_header, style="Card.TFrame")
        left_group.pack(side="left")

        filter_group = ttk.Frame(left_group, style="Card.TFrame")
        filter_group.pack(side="left")
        self.filter_text = tk.StringVar()

        filter_box = tk.Frame(
            filter_group,
            bg=self.palette["card_alt"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
        )
        filter_box.pack(side="left", padx=(0, 10))

        self.filter_entry = tk.Entry(
            filter_box,
            textvariable=self.filter_text,
            width=28,
            relief="flat",
            bd=0,
            bg=self.palette["card_alt"],
            fg=self.palette["muted"],
            disabledbackground=self.palette["card_alt"],
            disabledforeground="#8b95a5",
            insertbackground=self.palette["text"],
            font=font_entry,
        )
        self.filter_entry.pack(side="left", padx=(8, 0), pady=4)
        self.filter_entry.bind("<FocusIn>", self.on_filter_focus_in)
        self.filter_entry.bind("<FocusOut>", self.on_filter_focus_out)
        self.filter_entry.bind("<KeyRelease>", lambda e: self.apply_filter())
        self.clear_filter_button = tk.Button(
            filter_box,
            text="×",
            command=self.clear_filter,
            relief="solid",
            bd=1,
            width=2,
            padx=0,
            pady=0,
            bg=self.palette["card_bg"],
            fg=self.palette["muted"],
            activebackground="#fbe4ea",
            activeforeground="#7f1d1d",
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
        )
        self.clear_filter_button.pack(side="left", padx=(4, 6), pady=2)
        self.clear_filter_button.bind("<Enter>", self.on_clear_filter_enter)
        self.clear_filter_button.bind("<Leave>", self.on_clear_filter_leave)

        self.master_toggle_button = ttk.Button(
            left_group,
            text="Tout cocher",
            command=self.toggle_all_button_action,
            style="Secondary.TButton",
            state="disabled",
        )
        self.master_toggle_button.pack(side="left", padx=(0, 8))

        self.invert_button = ttk.Button(
            left_group,
            text="Inverser",
            command=self.invert_selection,
            style="Secondary.TButton",
            state="disabled",
        )
        self.invert_button.pack(side="left")
        self.selection_status_label = ttk.Label(
            left_group,
            text="Sélection: 0/0",
            style="Muted.TLabel",
            font=("Segoe UI", 9),
        )
        self.selection_status_label.pack(side="left", padx=(12, 0))

        download_group = ttk.Frame(vol_header, style="Card.TFrame")
        download_group.pack(side="right")

        self.dl_button = ttk.Button(
            download_group,
            text="Télécharger la sélection",
            command=self.download_selected,
            style="Download.TButton",
            state="disabled",
        )
        self.dl_button.pack(side="left", padx=(0, 8))

        self.cancel_button = ttk.Button(
            download_group,
            text="Annuler",
            command=self.cancel_download,
            style="Cancel.TButton",
            state="disabled",
        )
        self.cancel_button.pack(side="left")
        self.download_hint_label = ttk.Label(
            download_group,
            text="Sélectionne au moins 1 tome.",
            style="Muted.TLabel",
            font=("Segoe UI", 8),
        )
        self.download_hint_label.pack(side="left", padx=(10, 0))

        self.set_filter_placeholder()
        self.filter_entry.config(state="disabled")
        self.clear_filter_button.config(state="disabled")

        vol_frame_container = tk.Frame(
            center_card,
            bg=self.palette["canvas_bg"],
            highlightbackground=self.palette["border"],
            highlightthickness=1,
            bd=0,
        )
        vol_frame_container.pack(fill="both", expand=True)

        canvas_frame = ttk.Frame(vol_frame_container, style="Card.TFrame")
        canvas_frame.pack(fill="both", expand=True, padx=1, pady=1)

        self.canvas = tk.Canvas(
            canvas_frame,
            bg=self.palette["canvas_bg"],
            highlightthickness=0,
            height=220,  # Zone volontairement plus haute pour afficher davantage de tomes.
        )
        self.scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.vol_frame = tk.Frame(self.canvas, bg=self.palette["canvas_bg"])
        self.canvas_window = self.canvas.create_window((0, 0), window=self.vol_frame, anchor="nw")
        self.vol_empty_label = tk.Label(
            self.canvas,
            text="Aucun Tome/Chapitre chargé.",
            bg=self.palette["canvas_bg"],
            fg=self.palette["muted"],
            font=("Segoe UI", 10),
            bd=0,
            highlightthickness=0,
        )
        self.vol_empty_label.place(in_=self.canvas, relx=0.5, rely=0.5, anchor="center")

        def center_volumes(event):
            canvas_width = event.width
            self.canvas.coords(self.canvas_window, 0, 0)
            self.canvas.itemconfig(self.canvas_window, width=canvas_width)

        self.canvas.bind("<Configure>", center_volumes)
        self.vol_frame.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        progress_frame = ttk.Frame(center_card, style="Card.TFrame")
        progress_frame.pack(fill="x", pady=(4, 0))
        self.current_volume_status_label = ttk.Label(
            progress_frame,
            text="Tome/Chapitre en cours: --",
            style="Muted.TLabel",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.current_volume_status_label.pack(side="left")
        self.progress_detail_label = ttk.Label(
            progress_frame,
            text="Images: --/--",
            style="Muted.TLabel",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.progress_detail_label.pack(side="left", padx=(12, 0))
        self.eta_label = ttk.Label(
            progress_frame,
            text="ETA Tome: --:-- | ETA Global: --:--",
            style="Muted.TLabel",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.eta_label.pack(side="left", padx=(12, 0))
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress,
            maximum=100,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.progress_label = ttk.Label(
            progress_frame,
            text="0%",
            style="Muted.TLabel",
            font=("Segoe UI Semibold", 9),
            width=5,
            anchor="e",
        )
        self.progress_label.pack(side="left", padx=(8, 0))

        error_panel = tk.Frame(
            self.error_tab,
            bg=self.palette["card_bg"],
            highlightbackground=self.palette["border"],
            highlightthickness=0,
            bd=0,
        )
        error_panel.pack(fill="both", expand=True)
        error_frame = ttk.Frame(error_panel, style="Card.TFrame")
        error_frame.pack(fill="both", expand=True, padx=12, pady=9)
        error_toolbar = ttk.Frame(error_frame, style="Card.TFrame")
        error_toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(
            error_toolbar,
            text="Effacer",
            command=self.clear_volume_errors,
            style="Secondary.TButton",
        ).pack(side="right", padx=(4, 0))
        ttk.Button(
            error_toolbar,
            text="Copier",
            command=self.copy_volume_errors,
            style="Secondary.TButton",
        ).pack(side="right", padx=(4, 0))
        ttk.Button(
            error_toolbar,
            text="Exporter",
            command=self.export_volume_errors,
            style="Secondary.TButton",
        ).pack(side="right")

        error_tree_container = ttk.Frame(error_frame, style="Card.TFrame")
        error_tree_container.pack(fill="both", expand=True)
        self.error_tree = ttk.Treeview(
            error_tree_container,
            columns=("time", "tome", "stage", "http", "reason", "action"),
            show="headings",
            height=8,
        )
        self.error_tree.heading("time", text="Heure")
        self.error_tree.heading("tome", text="Tome")
        self.error_tree.heading("stage", text="Étape")
        self.error_tree.heading("http", text="HTTP")
        self.error_tree.heading("reason", text="Cause")
        self.error_tree.heading("action", text="Action recommandée")
        self.error_tree.column("time", width=64, anchor="center", stretch=False)
        self.error_tree.column("tome", width=160, anchor="w", stretch=False)
        self.error_tree.column("stage", width=90, anchor="center", stretch=False)
        self.error_tree.column("http", width=60, anchor="center", stretch=False)
        self.error_tree.column("reason", width=290, anchor="w", stretch=True)
        self.error_tree.column("action", width=360, anchor="w", stretch=True)
        self.error_tree.pack(side="left", fill="both", expand=True)
        error_scroll = ttk.Scrollbar(error_tree_container, orient="vertical", command=self.error_tree.yview)
        error_scroll.pack(side="right", fill="y")
        self.error_tree.configure(yscrollcommand=error_scroll.set)

        status_frame = ttk.Frame(main_frame, style="Card.TFrame")
        status_frame.pack(fill="x")
        status_box = tk.Label(
            status_frame,
            textvariable=self.runtime_status,
            anchor="w",
            fg=self.palette["muted"],
            bg=self.palette["card_alt"],
            font=("Segoe UI", 8),
            padx=10,
            pady=6,
            relief="solid",
            borderwidth=1,
        )
        status_box.pack(fill="x")

        self._update_error_tab_title(focus_errors=False)
        self._set_workflow_step("auth", "Renseigne cookies et User-Agent manuels.")

    def normalize_display_texts(self):
        """Normalise les textes affiches (accents/casse) sur l'UI."""

        def normalize_ui_text(value):
            fixed = repair_mojibake_text(value or "")
            replacements = {
                "Validee": "Validé",
                "Validée": "Validé",
                "A verifier": "A vérifier",
                "Telecharger": "Télécharger",
                "selection": "sélection",
                "Selection": "Sélection",
                "Entree": "Entrée",
                "Etape": "Étape",
                "recommandee": "recommandée",
                "detailles": "détaillés",
                "parametres": "paramètres",
                "decocher": "décocher",
                "resultat": "résultat",
            }
            for source, target in replacements.items():
                fixed = fixed.replace(source, target)
            return fixed

        def normalize_widget_text(widget):
            try:
                raw = widget.cget("text")
            except Exception:
                raw = None
            if isinstance(raw, str) and raw:
                fixed = normalize_ui_text(raw)
                if fixed != raw:
                    try:
                        widget.configure(text=fixed)
                    except Exception:
                        pass

            if isinstance(widget, ttk.Treeview):
                for col in widget["columns"]:
                    try:
                        heading_text = widget.heading(col, "text")
                    except Exception:
                        continue
                    fixed = normalize_ui_text(heading_text or "")
                    if fixed and fixed != heading_text:
                        widget.heading(col, text=fixed)

            for child in widget.winfo_children():
                normalize_widget_text(child)

        for var_name in (
            "runtime_status",
            "cookie_fr_label_var",
            "cookie_net_label_var",
            "cookie_origines_label_var",
            "cookie_hentai_label_var",
            "ua_label_var",
        ):
            var = getattr(self, var_name, None)
            if isinstance(var, tk.StringVar):
                try:
                    var.set(normalize_ui_text(var.get()))
                except Exception:
                    pass

        try:
            normalize_widget_text(self.root)
        except Exception:
            pass

    def get_cover_target_size(self):
        """Retourne la taille de rendu des couvertures avec ratio fixe 2:3."""
        target_h = max(1, int(getattr(self, "cover_target_height", COVER_TARGET_HEIGHT) or COVER_TARGET_HEIGHT))
        ratio = COVER_RATIO_WIDTH / COVER_RATIO_HEIGHT
        target_w = max(1, int(round(target_h * ratio)))
        return target_w, target_h

    def _stop_cover_animation(self):
        """Arrête l'animation de couverture en cours (si active)."""
        after_id = getattr(self, "cover_animation_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.cover_animation_after_id = None
        self.cover_animation_index = 0
        self.cover_animation_frames = []
        self.cover_animation_durations = []

    def _apply_cover_static(self, fitted_image):
        """Affiche une couverture statique."""
        self._stop_cover_animation()
        self.cover_preview = ImageTk.PhotoImage(fitted_image)
        self.cover_label.configure(image=self.cover_preview, text="")
        self.cover_label.image = self.cover_preview

    def _apply_cover_animation(self, frames, durations):
        """Affiche une couverture animée (GIF) avec boucle Tkinter."""
        self._stop_cover_animation()
        if not frames:
            return

        self.cover_animation_frames = [ImageTk.PhotoImage(frame) for frame in frames]
        self.cover_animation_durations = [max(60, int(delay or 100)) for delay in durations] or [100] * len(
            self.cover_animation_frames
        )
        if len(self.cover_animation_durations) < len(self.cover_animation_frames):
            self.cover_animation_durations.extend(
                [100] * (len(self.cover_animation_frames) - len(self.cover_animation_durations))
            )
        self.cover_animation_index = 0
        self.cover_preview = self.cover_animation_frames[0]
        self.cover_label.configure(image=self.cover_preview, text="")
        self.cover_label.image = self.cover_preview

        if len(self.cover_animation_frames) <= 1:
            return

        def tick():
            if not self.cover_animation_frames:
                self.cover_animation_after_id = None
                return
            self.cover_animation_index = (self.cover_animation_index + 1) % len(self.cover_animation_frames)
            frame = self.cover_animation_frames[self.cover_animation_index]
            self.cover_preview = frame
            self.cover_label.configure(image=frame, text="")
            self.cover_label.image = frame
            delay = self.cover_animation_durations[self.cover_animation_index]
            self.cover_animation_after_id = self.root.after(delay, tick)

        first_delay = self.cover_animation_durations[0]
        self.cover_animation_after_id = self.root.after(first_delay, tick)

    def _show_default_cover_placeholder(self):
        """Affiche le visuel par défaut de couverture avant la première analyse."""
        if not hasattr(self, "cover_label"):
            return

        placeholder_path = BASE_DIR / "assets" / "sushidl.png"
        if not placeholder_path.exists():
            self._stop_cover_animation()
            self.cover_preview = None
            self.cover_label.configure(image="", text="Couverture")
            self.cover_label.image = None
            return

        try:
            target_w, target_h = self.get_cover_target_size()

            with Image.open(placeholder_path) as src:
                img = src.convert("RGB")
                fitted = ImageOps.fit(
                    img,
                    (int(target_w), int(target_h)),
                    method=Image.LANCZOS,
                    centering=(0.5, 0.5),
                )
            self._apply_cover_static(fitted)
        except Exception as exc:
            self._stop_cover_animation()
            self.cover_preview = None
            self.cover_label.configure(image="", text="Couverture")
            self.cover_label.image = None
            self.log(f"Placeholder couverture indisponible: {exc}", level="debug")

    def _open_external_link(self, url):
        """Ouvre un lien externe dans le navigateur par défaut."""
        target = (url or "").strip()
        if not target:
            return
        try:
            webbrowser.open(target, new=2)
            self.log(f"Ouverture lien: {target}", level="info")
        except Exception as exc:
            self.log(f"Impossible d'ouvrir le lien {target}: {exc}", level="error")

    def _paste_into_entry(self, entry_widget):
        """Colle le contenu du presse-papiers dans un Entry cible."""
        if entry_widget is None:
            return
        try:
            if str(entry_widget.cget("state")) == "disabled":
                return
        except Exception:
            pass
        try:
            clip_text = self.root.clipboard_get()
        except Exception:
            return
        if clip_text is None:
            return
        clip_text = str(clip_text)
        try:
            entry_widget.focus_set()
        except Exception:
            pass
        try:
            if bool(entry_widget.selection_present()):
                entry_widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        try:
            entry_widget.insert("insert", clip_text)
            entry_widget.icursor("end")
        except Exception:
            pass

    def _show_entry_context_menu(self, event, entry_widget):
        """Affiche un menu contextuel minimal avec action Coller."""
        if entry_widget is None:
            return "break"
        self._context_menu_entry = entry_widget
        if not hasattr(self, "entry_paste_menu"):
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="Coller", command=lambda: self._paste_into_entry(self._context_menu_entry))
            self.entry_paste_menu = menu
        can_paste = True
        try:
            if str(entry_widget.cget("state")) == "disabled":
                can_paste = False
        except Exception:
            pass
        if can_paste:
            try:
                _ = self.root.clipboard_get()
            except Exception:
                can_paste = False
        self.entry_paste_menu.entryconfigure(0, state=("normal" if can_paste else "disabled"))
        try:
            self.entry_paste_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.entry_paste_menu.grab_release()
            except Exception:
                pass
        return "break"

    def _bind_entry_paste_menu(self, entry_widget, placeholder_widget=None):
        """Active le clic droit Coller sur un Entry (et son placeholder si présent)."""
        if entry_widget is None:
            return
        entry_widget.bind(
            "<Button-3>",
            lambda event, target=entry_widget: self._show_entry_context_menu(event, target),
            add="+",
        )
        # Compatibilité Linux/macOS selon backend Tk.
        entry_widget.bind(
            "<Button-2>",
            lambda event, target=entry_widget: self._show_entry_context_menu(event, target),
            add="+",
        )
        if placeholder_widget is not None:
            placeholder_widget.bind(
                "<Button-3>",
                lambda event, target=entry_widget: self._show_entry_context_menu(event, target),
            )
            placeholder_widget.bind(
                "<Button-2>",
                lambda event, target=entry_widget: self._show_entry_context_menu(event, target),
            )

    def _attach_link_placeholder(self, entry_widget, text_variable, placeholder_text, link_url):
        """
        Place un placeholder cliquable par-dessus un Entry sans modifier la valeur réelle.
        Le champ reste vide en interne tant que l'utilisateur n'a rien saisi.
        """
        if entry_widget is None:
            return
        parent = entry_widget.master
        if parent is None:
            return
        bg_color = "#ffffff"
        if hasattr(self, "palette"):
            bg_color = self.palette.get("input_bg", "#ffffff")

        placeholder = tk.Label(
            parent,
            text=placeholder_text,
            fg=self.palette.get("muted", "#7f8c8d"),
            bg=bg_color,
            font=("Segoe UI", 9),
            cursor="hand2" if link_url else "xterm",
            padx=2,
            pady=0,
        )

        state = {"visible": False}

        def show_placeholder():
            has_value = bool((text_variable.get() or "").strip())
            if has_value:
                if state["visible"]:
                    placeholder.place_forget()
                    state["visible"] = False
                return
            if not state["visible"]:
                placeholder.place(in_=entry_widget, x=6, y=4)
                state["visible"] = True

        def hide_placeholder():
            if state["visible"]:
                placeholder.place_forget()
                state["visible"] = False

        def on_focus_in(_event=None):
            hide_placeholder()

        def on_focus_out(_event=None):
            show_placeholder()

        def on_click(_event=None):
            if link_url:
                self._open_external_link(link_url)
            try:
                entry_widget.focus_set()
            except Exception:
                pass

        if link_url:
            placeholder.bind("<Button-1>", on_click)
        else:
            placeholder.bind("<Button-1>", lambda _e: entry_widget.focus_set())
        entry_widget.bind("<FocusIn>", on_focus_in, add="+")
        entry_widget.bind("<FocusOut>", on_focus_out, add="+")
        self._bind_entry_paste_menu(entry_widget, placeholder_widget=placeholder)
        text_variable.trace_add("write", lambda *_args: show_placeholder())
        show_placeholder()

    def _setup_auth_link_placeholders(self):
        """Initialise les placeholders cliquables pour cookies et User-Agent."""
        ua_link = get_manual_link("user_agent", "https://httpbin.org/user-agent")
        cookie_fr_link = get_manual_link("cookie_fr", "https://sushiscan.fr")
        cookie_net_link = get_manual_link("cookie_net", "https://sushiscan.net")
        cookie_origines_link = get_manual_link("cookie_origines", "https://mangas-origines.fr")
        cookie_hentai_link = get_manual_link("cookie_hentai", "https://hentai-origines.fr")
        self._attach_link_placeholder(
            self.cookie_fr_entry,
            self.cookie_fr,
            'Cookie cf_clearance sushiscan.fr (cliquer pour ouvrir le site si besoin).',
            cookie_fr_link,
        )
        self._attach_link_placeholder(
            self.cookie_net_entry,
            self.cookie_net,
            'Cookie cf_clearance sushiscan.net (cliquer pour ouvrir le site si besoin).',
            cookie_net_link,
        )
        self._attach_link_placeholder(
            self.cookie_origines_entry,
            self.cookie_origines,
            'Cookie cf_clearance mangas-origines.fr (cliquer pour ouvrir le site si besoin).',
            cookie_origines_link,
        )
        self._attach_link_placeholder(
            self.cookie_hentai_entry,
            self.cookie_hentai,
            'Cookie cf_clearance hentai-origines.fr (🔞, cliquer pour ouvrir le site si besoin).',
            cookie_hentai_link,
        )
        self._attach_link_placeholder(
            self.ua_entry,
            self.ua,
            'Cliquer ici pour accéder à : Votre User-Agent (copier/coller seulement la partie à droite entre les "" )',
            ua_link,
        )
        self.root.after_idle(self._finalize_config_panel_layout)

    def _toggle_cookie_visibility(self):
        show_char = "" if bool(self.show_cookies.get()) else "*"
        if hasattr(self, "cookie_fr_entry"):
            self.cookie_fr_entry.config(show=show_char)
        if hasattr(self, "cookie_net_entry"):
            self.cookie_net_entry.config(show=show_char)
        if hasattr(self, "cookie_origines_entry"):
            self.cookie_origines_entry.config(show=show_char)
        if hasattr(self, "cookie_hentai_entry"):
            self.cookie_hentai_entry.config(show=show_char)


    def get_domain_from_url(self, url):
        """Retourne le domaine cookie interne: fr/net/origines."""
        return get_cookie_domain_from_url(url)

    def _get_cookie_var_for_domain(self, domain):
        """Retourne la StringVar cookie liée au domaine."""
        mapping = {
            "fr": getattr(self, "cookie_fr", None),
            "net": getattr(self, "cookie_net", None),
            "origines": getattr(self, "cookie_origines", None),
            "hentai": getattr(self, "cookie_hentai", None),
        }
        return mapping.get(domain)

    def _get_cookie_entry_for_domain(self, domain):
        """Retourne le widget Entry cookie lié au domaine."""
        mapping = {
            "fr": getattr(self, "cookie_fr_entry", None),
            "net": getattr(self, "cookie_net_entry", None),
            "origines": getattr(self, "cookie_origines_entry", None),
            "hentai": getattr(self, "cookie_hentai_entry", None),
        }
        return mapping.get(domain)

    def _get_cookie_badge_for_domain(self, domain):
        """Retourne le badge visuel cookie lié au domaine."""
        mapping = {
            "fr": getattr(self, "cookie_fr_status", None),
            "net": getattr(self, "cookie_net_status", None),
            "origines": getattr(self, "cookie_origines_status", None),
            "hentai": getattr(self, "cookie_hentai_status", None),
        }
        return mapping.get(domain)

    def get_cookie(self, url):
        """Sélectionne automatiquement le cookie selon le domaine"""
        domain = self.get_domain_from_url(url)
        cookie_var = self._get_cookie_var_for_domain(domain)
        if cookie_var is not None:
            return self.run_on_ui(cookie_var.get, wait=True, default="").strip()
        return ""

    def get_direct_user_agent(self):
        """UA direct (champ UI), utilisé avec cookies manuels."""
        return self.run_on_ui(self.ua.get, wait=True, default="").strip() or DIRECT_USER_AGENT_DEFAULT

    def sync_cookie_source_for_domain(self, domain):
        """Synchronise l'origine du cookie si l'utilisateur a saisi une nouvelle valeur."""
        if domain not in COOKIE_DOMAINS:
            return
        cookie_var = self._get_cookie_var_for_domain(domain)
        if cookie_var is None:
            return
        current_cookie = self.run_on_ui(cookie_var.get, wait=True, default="").strip()
        previous_cookie = (self.last_known_cookies.get(domain) or "").strip()

        if current_cookie and current_cookie != previous_cookie:
            self.cookie_sources[domain] = "manual"
            self.cookie_user_agents[domain] = self.get_direct_user_agent()
            self.cookie_headers[domain] = f"cf_clearance={current_cookie}"
            self.last_known_cookies[domain] = current_cookie
            self._mark_cookie_updated(domain, current_cookie)
        elif not current_cookie:
            self.cookie_sources[domain] = ""
            self.cookie_user_agents[domain] = ""
            self.cookie_headers[domain] = ""
            self.last_known_cookies[domain] = ""
            self._mark_cookie_updated(domain, "")

    def get_request_user_agent_for_domain(self, domain):
        """UA effectif pour un domaine selon l'origine du cookie."""
        self.sync_cookie_source_for_domain(domain)
        return self.get_direct_user_agent()

    def get_request_user_agent_for_url(self, url):
        domain = self.get_domain_from_url(url)
        return self.get_request_user_agent_for_domain(domain)

    def get_cookie_header_for_domain(self, domain, fallback_cookie=None):
        """Retourne l'en-tête Cookie effectif (complet si disponible)."""
        if domain not in COOKIE_DOMAINS:
            return ""
        header = (self.cookie_headers.get(domain) or "").strip()
        if header:
            return header
        cookie_var = self._get_cookie_var_for_domain(domain)
        if cookie_var is None:
            return ""
        cookie_value = (fallback_cookie or self.run_on_ui(cookie_var.get, wait=True, default="")).strip()
        if cookie_value:
            return f"cf_clearance={cookie_value}"
        return ""

    def get_cookie_header_for_url(self, url, fallback_cookie=None):
        domain = self.get_domain_from_url(url)
        return self.get_cookie_header_for_domain(domain, fallback_cookie=fallback_cookie)

    def persist_settings(self):
        """Sauvegarde silencieuse des paramètres courants."""
        direct_ua = self.get_direct_user_agent()
        cookies = {}
        for domain in COOKIE_DOMAINS:
            cookie_var = self._get_cookie_var_for_domain(domain)
            cookies[domain] = self.run_on_ui(cookie_var.get, wait=True, default="").strip() if cookie_var else ""

        # Si l'utilisateur a modifié manuellement un cookie, on repasse en mode UA direct.
        for domain in COOKIE_DOMAINS:
            current_cookie = (cookies.get(domain) or "").strip()
            previous_cookie = (self.last_known_cookies.get(domain) or "").strip()
            if current_cookie and current_cookie != previous_cookie:
                self.cookie_sources[domain] = "manual"
                self.cookie_user_agents[domain] = direct_ua
                self.cookie_headers[domain] = f"cf_clearance={current_cookie}"
                self.last_known_cookies[domain] = current_cookie
                self._mark_cookie_updated(domain, current_cookie)
            elif current_cookie:
                self.cookie_sources[domain] = "manual"
                self.cookie_user_agents[domain] = direct_ua
                self.cookie_headers[domain] = f"cf_clearance={current_cookie}"
            elif not current_cookie:
                self.cookie_sources[domain] = ""
                self.cookie_user_agents[domain] = ""
                self.cookie_headers[domain] = ""
                self.last_known_cookies[domain] = ""
                self._mark_cookie_updated(domain, "")

        cbz_enabled = bool(self.run_on_ui(self.cbz_enabled.get, wait=True, default=True))
        webp2jpg_enabled = bool(self.run_on_ui(self.webp2jpg_enabled.get, wait=True, default=True))
        smart_resume_enabled = bool(self.run_on_ui(self.smart_resume_enabled.get, wait=True, default=True))
        verbose_logs_enabled = bool(self.run_on_ui(self.verbose_logs.get, wait=True, default=True))
        updated_at = save_cookie_cache(
            cookies,
            direct_ua,
            cbz_enabled,
            webp2jpg_enabled,
            smart_resume_enabled,
            verbose_logs_enabled,
            cookie_sources=self.cookie_sources,
            cookie_user_agents=self.cookie_user_agents,
            cookie_headers=self.cookie_headers,
        )
        if isinstance(updated_at, dict):
            self.cookie_updated_at = {domain: (updated_at.get(domain) or "").strip() for domain in COOKIE_DOMAINS}

    def ensure_cookie_for_domain(self, domain, force_refresh=False, probe_url=None):
        """
        Retourne le cookie manuel du domaine.
        Aucun rafraîchissement automatique n'est effectué.
        """
        _ = probe_url
        if domain not in COOKIE_DOMAINS:
            return ""

        cookie_var = self._get_cookie_var_for_domain(domain)
        if cookie_var is None:
            return ""
        cookie = self.run_on_ui(cookie_var.get, wait=True, default="").strip()
        direct_ua = self.get_direct_user_agent()

        if cookie:
            self.cookie_sources[domain] = "manual"
            self.cookie_user_agents[domain] = direct_ua
            self.cookie_headers[domain] = f"cf_clearance={cookie}"
            self.last_known_cookies[domain] = cookie
            self._mark_cookie_updated(domain, cookie)
            return cookie

        if force_refresh:
            self.log(
                f"Cookie .{domain} vide: renseigne cf_clearance manuellement pour ce domaine.",
                level="warning",
            )
        return ""

    def ensure_cookie_for_url(self, url, force_refresh=False):
        """Rafraîchit le cookie du domaine de l'URL si nécessaire."""
        domain = self.get_domain_from_url(url)
        if not domain:
            return self.get_cookie(url)
        return self.ensure_cookie_for_domain(domain, force_refresh=force_refresh, probe_url=url)

    def load_volumes(self):
        """Charge la liste des tomes/chapitres pour l'URL donnée."""
        if getattr(self, "analysis_in_progress", False):
            self.log("Analyse déjà en cours, patiente quelques secondes.", level="warning")
            return

        self._set_workflow_step("source", "Validation URL et analyse catalogue...")
        self._set_analysis_status_label("Analyse en cours: validation URL...", success=None)
        url = self.url.get().strip()
        if not is_valid_catalogue_url(url):
            self.log(
                "URL invalide. Formats attendus: https://sushiscan.fr|net/catalogue/slug/ ou https://mangas-origines.fr/oeuvre/slug/ ou https://hentai-origines.fr/manga/slug/ (🔞).",
                level="error",
            )
            self._set_analysis_status_label("URL invalide", success=False)
            self.toast("URL invalide")
            return

        def set_analysis_step(step):
            labels = {
                "validate": "Analyse en cours: validation URL...",
                "fetch": "Analyse en cours: récupération du catalogue...",
                "parse": "Analyse en cours: parsing des tomes/chapitres...",
                "cover": "Analyse en cours: récupération de la couverture...",
            }
            text = labels.get(step)
            if text:
                self.run_on_ui(lambda: self._set_analysis_status_label(text, success=None))

        cookie = self.get_cookie(url)
        ua_for_url = self.get_request_user_agent_for_url(url)
        domain = self.get_domain_from_url(url)
        if domain in COOKIE_DOMAINS:
            self._reset_analysis_auth_state(reset_domains=(domain,), reset_ua=False, clear_label=False)
            self.update_cookie_status(validate=False)
            self.update_runtime_status()
        if not cookie and domain in COOKIE_DOMAINS:
            self.log(
                f"Cookie .{domain} vide: si Cloudflare demande un challenge, renseigne cf_clearance manuellement.",
                level="warning",
            )
        self.filter_text.set("")

        self.analysis_in_progress = True
        if hasattr(self, "analyze_button"):
            self.analyze_button.config(state="disabled")
        self.update_master_toggle_button()
        self._hide_volume_empty_state()

        def finish_analysis():
            self.analysis_in_progress = False
            if hasattr(self, "analyze_button"):
                self.analyze_button.config(state="normal")
            self.update_master_toggle_button()

        def handle_error(error_text):
            error_text = repair_mojibake_text(error_text)
            self.log(f"Erreur: {error_text}", level="error")
            lowered = error_text.lower()
            auth_related = any(
                marker in lowered
                for marker in (
                    "http 403",
                    "accès refusé",
                    "acces refuse",
                    "forbidden",
                    "cloudflare",
                    "challenge",
                    "cookie",
                )
            )
            if domain in COOKIE_DOMAINS:
                fail_reason = "échec d'authentification" if auth_related else "analyse échouée"
                self._mark_analysis_auth_state(domain, False, fail_reason)
                self.log(
                    (
                        f"Auth .{domain} non validée: vérifie le cookie cf_clearance .{domain}."
                        if ua_for_url.strip()
                        else f"Auth .{domain} non validée: vérifie le cookie cf_clearance .{domain} et le User-Agent."
                    ),
                    level="warning",
                )
            else:
                self._set_analysis_status_label("Analyse échouée (auth non concluante)", success=False)

            if "http 403" in lowered or "accès refusé" in lowered or "acces refuse" in lowered:
                self.log(
                    (
                        f"HTTP 403 détecté: vérifie le cookie cf_clearance .{domain}."
                        if ua_for_url.strip() and domain in COOKIE_DOMAINS
                        else "HTTP 403 détecté: vérifie le cookie cf_clearance du domaine et le User-Agent."
                    ),
                    level="warning",
                )
            self.update_cookie_status(validate=True)
            self._set_analysis_status_label("Analyse échouée", success=False)
            self.toast("Impossible de charger la liste")
            finish_analysis()

        def apply_pairs_ui():
            for widget in self.vol_frame.winfo_children():
                if widget is getattr(self, "vol_empty_label", None):
                    widget.place_forget()
                    continue
                widget.destroy()

            self.check_vars = []
            self.check_items = []
            columns = 4
            for col in range(columns):
                self.vol_frame.grid_columnconfigure(col, weight=1)

            for i, (vol, _link) in enumerate(self.pairs):
                var = tk.BooleanVar(value=True)
                self.check_vars.append(var)
                chk = ttk.Checkbutton(
                    self.vol_frame,
                    text=vol,
                    variable=var,
                    style="Tome.TCheckbutton",
                    takefocus=False,
                    command=self.update_master_toggle_button,
                )
                chk.grid(row=(i // columns) + 2, column=i % columns, padx=15, pady=5, sticky="n")
                self.check_items.append((chk, vol))

            if not self.pairs:
                self._show_volume_empty_state("Aucun tome detecte pour cette URL.", tone="warning")
                self.log("Aucun tome detecte.", level="warning")
            else:
                self._hide_volume_empty_state()
            self.canvas.yview_moveto(0)
            self.log("Liste chargée avec succès.", level="success")
            has_pairs = bool(self.pairs)
            self.filter_entry.config(state="normal" if has_pairs else "disabled")
            self.clear_filter_button.config(state="normal" if has_pairs else "disabled")
            if has_pairs and not self.filter_text.get().strip():
                self.set_filter_placeholder()
            self.master_toggle_button.config(state="normal" if has_pairs else "disabled")
            self.invert_button.config(state="normal" if has_pairs else "disabled")
            self.update_master_toggle_button()
            self._refresh_volume_empty_state()
            if has_pairs:
                self._set_analysis_status_label("Analyse terminée.", success=True)
            else:
                self._set_analysis_status_label("Analyse terminée: liste vide.", success=False)
            finish_analysis()

        def fetch_progress_callback(step):
            set_analysis_step(step)

        def worker():
            try:
                set_analysis_step("fetch")
                title, pairs, html_content = fetch_manga_data(
                    url,
                    cookie,
                    ua_for_url,
                    return_html=True,
                    progress_callback=fetch_progress_callback,
                )
                self.title = title
                self.pairs = pairs
                self.ua_runtime_validity = bool((ua_for_url or "").strip())

                if domain in COOKIE_DOMAINS:
                    if self.pairs:
                        self._mark_analysis_auth_state(
                            domain,
                            True,
                            f"{len(self.pairs)} tome(s)/chapitre(s) détecté(s)",
                        )
                        self.log(
                            f"Auth .{domain} validée par analyse: cookie + User-Agent OK.",
                            level="success",
                        )
                    else:
                        self._mark_analysis_auth_state(domain, False, "liste vide")
                        self.log(
                            (
                                f"Auth .{domain} non validée: vérifie le cookie cf_clearance .{domain}."
                                if ua_for_url.strip()
                                else f"Auth .{domain} non validée: vérifie le cookie cf_clearance .{domain} et le User-Agent."
                            ),
                            level="warning",
                        )
                self.run_on_ui(lambda: self.update_cookie_status(validate=True))
            except Exception as exc:
                self.run_on_ui(lambda err=exc: handle_error(str(err)))
                return

            try:
                set_analysis_step("cover")
                get_cover_image(html_content)
            except Exception as cover_exc:
                self.log(f"Erreur chargement couverture: {cover_exc}", level="error")

            try:
                MangaApp.last_url_used = url
                self.persist_settings()
            except Exception as save_exc:
                self.log(f"Erreur sauvegarde paramètres: {save_exc}", level="error")

            try:
                self.run_on_ui(apply_pairs_ui, wait=True)
            except Exception as ui_exc:
                self.log(f"Erreur rendu liste: {ui_exc}", level="error")
                self.run_on_ui(finish_analysis)

        threading.Thread(target=worker, daemon=True).start()

    def are_all_volumes_selected(self):
        """Retourne True si toutes les cases sont cochées."""
        return bool(self.check_vars) and all(var.get() for var in self.check_vars)

    def update_master_toggle_button(self):
        """Met à jour le libellé du bouton global de sélection."""
        if not hasattr(self, "master_toggle_button"):
            return
        text = "Tout décocher" if self.are_all_volumes_selected() else "Tout cocher"
        self.master_toggle_button.config(text=text)
        self._update_selection_status()

    def _update_selection_status(self):
        total = len(getattr(self, "check_items", []) or [])
        selected_total = 0
        visible_total = 0
        selected_visible = 0

        for (chk, _label), var in zip(self.check_items, self.check_vars):
            is_selected = bool(var.get())
            if is_selected:
                selected_total += 1
            if self._is_volume_visible(chk):
                visible_total += 1
                if is_selected:
                    selected_visible += 1

        if hasattr(self, "selection_status_label"):
            label_text = f"Sélection: {selected_total}/{total}"
            if visible_total and visible_total != total:
                label_text = f"{label_text} (affichés: {selected_visible}/{visible_total})"
            self.selection_status_label.config(text=label_text)
        if hasattr(self, "download_hint_label"):
            if total == 0:
                hint = "Charge d'abord une source."
            elif selected_visible <= 0:
                hint = "Sélectionne au moins 1 tome visible."
            elif visible_total != total:
                hint = f"{selected_visible} tome(s) visible(s) prêt(s)."
            else:
                hint = f"{selected_visible} tome(s) prêt(s) au téléchargement."
            self.download_hint_label.config(text=hint)

        can_download = (
            total > 0
            and selected_visible > 0
            and not getattr(self, "download_in_progress", False)
            and not getattr(self, "analysis_in_progress", False)
        )
        if hasattr(self, "dl_button"):
            self.dl_button.config(state="normal" if can_download else "disabled")

    def toggle_all_button_action(self):
        """Bascule globalement entre tout cocher et tout décocher."""
        target_state = not self.are_all_volumes_selected()
        self.toggle_all_volumes(target_state)

    def toggle_all_volumes(self, state):
        """Coche/décoche toutes les cases à cocher."""
        for var in self.check_vars:
            var.set(state)
        self.update_master_toggle_button()
    
    def invert_selection(self):
        """Inverse la sélection actuelle."""
        for var in self.check_vars:
            var.set(not var.get())
        self.update_master_toggle_button()

    def apply_filter(self):
        """Filtre la liste des tomes selon le texte saisi"""
        raw = ""
        if not self.filter_placeholder_active:
            raw = self.filter_text.get().strip().lower()
        
        row = 0
        col = 0
        for chk, label in self.check_items:
            label_lower = label.lower()
            
            # Filtre optimisé avec recherche de sous-chaîne
            if not raw or raw in label_lower or \
            (raw.endswith('*') and raw[:-1].isdigit() and label_lower.startswith(raw[:-1])):
                chk.grid(row=row, column=col, padx=15, pady=5, sticky="n")
                col += 1
                if col == 4:
                    col = 0
                    row += 1
            else:
                chk.grid_remove()
        self._update_selection_status()
        self._refresh_volume_empty_state()

    def clear_filter(self):
        """Réinitialise le filtre et affiche tous les tomes"""
        self.filter_text.set("")
        self.filter_placeholder_active = False
        self.apply_filter()
        self.set_filter_placeholder()

    def set_filter_placeholder(self):
        """Affiche le placeholder du champ filtre si vide."""
        if self.filter_text.get().strip():
            return
        self.filter_placeholder_active = True
        self.filter_text.set(self.filter_placeholder_text)
        self.filter_entry.config(fg=self.palette["muted"])

    def clear_filter_placeholder(self):
        """Retire le placeholder du champ filtre."""
        if not self.filter_placeholder_active:
            return
        self.filter_placeholder_active = False
        self.filter_text.set("")
        self.filter_entry.config(fg=self.palette["text"])

    def on_filter_focus_in(self, _event=None):
        """Nettoie le placeholder quand le champ prend le focus."""
        if self.filter_placeholder_active:
            self.clear_filter_placeholder()

    def on_filter_focus_out(self, _event=None):
        """Restaure le placeholder si le champ est vide."""
        if not self.filter_text.get().strip():
            self.set_filter_placeholder()

    def on_clear_filter_enter(self, _event=None):
        """Survol du bouton de remise à zéro du filtre."""
        if str(self.clear_filter_button.cget("state")) == "disabled":
            return
        self.clear_filter_button.config(bg="#fbe4ea", fg="#7f1d1d")

    def on_clear_filter_leave(self, _event=None):
        """Fin de survol du bouton de remise à zéro du filtre."""
        if str(self.clear_filter_button.cget("state")) == "disabled":
            return
        self.clear_filter_button.config(bg=self.palette["card_bg"], fg=self.palette["muted"])

    def download_selected(self):
        """Lance le téléchargement des tomes sélectionnés."""
        self.cancel_event.clear()
        selected = []
        for (chk, _label), (vol, link), var in zip(self.check_items, self.pairs, self.check_vars):
            if var.get() and self._is_volume_visible(chk):
                selected.append((vol, link))

        if not selected:
            self.log("Aucun tome sélectionné.", level="info")
            return

        initial_output_root = (getattr(self, "download_output_root", "") or os.path.abspath(ROOT_FOLDER)).strip()
        if not initial_output_root:
            initial_output_root = os.path.abspath(ROOT_FOLDER)
        try:
            os.makedirs(initial_output_root, exist_ok=True)
        except OSError:
            pass

        output_root = filedialog.askdirectory(
            parent=self.root,
            title="Choisir le dossier de destination",
            initialdir=initial_output_root,
            mustexist=False,
        )
        if not output_root:
            self.log("Téléchargement annulé: aucun dossier de destination sélectionné.", level="info")
            return
        output_root = os.path.abspath(output_root)
        self.download_output_root = output_root
        self.log(f"Dossier de destination: {output_root}", level="info")

        self._set_workflow_step("download", "Préparation du téléchargement...")
        self._set_download_controls(True)
        self._set_progress_ui(0)
        self._set_current_volume_ui(None)
        self._set_eta_ui()
        self._set_progress_detail_ui(None, None)

        cbz_enabled = self.cbz_enabled.get()
        webp2jpg_enabled = self.webp2jpg_enabled.get()
        smart_resume_enabled = self.smart_resume_enabled.get()

        def task():
            failed = []
            total_volumes = len(selected)
            completed_volumes = 0
            completed_volume_durations = []
            global_start = time.time()

            def push_idle_global_eta():
                if self.cancel_event.is_set():
                    return
                remaining = max(0, total_volumes - completed_volumes)
                if remaining == 0:
                    self.run_on_ui(self._set_eta_ui, None, 0)
                    return
                if completed_volume_durations:
                    avg_duration = sum(completed_volume_durations) / len(completed_volume_durations)
                elif completed_volumes > 0:
                    avg_duration = (time.time() - global_start) / completed_volumes
                else:
                    avg_duration = None
                global_eta = (avg_duration * remaining) if avg_duration is not None else None
                self.run_on_ui(self._set_eta_ui, None, global_eta)

            for vol, link in selected:
                if self.cancel_event.wait(0.2):
                    break

                volume_start = time.time()
                domain = self.get_domain_from_url(link)
                cookie = self.get_cookie(link)
                self.run_on_ui(self.root.title, f"SushiDL - {vol}")
                self.run_on_ui(self._set_current_volume_ui, vol)

                if not cookie and domain in COOKIE_DOMAINS:
                    self.log(
                        f"Cookie .{domain} vide pour {vol}: téléchargement possible seulement si le site ne demande pas de challenge.",
                        level="warning",
                    )

                self.log(
                    f"Téléchargement du tome: {vol}",
                    level="info",
                    context={"domain": domain, "tome": vol, "action": "download_start"},
                )

                clean_title = sanitize_folder_name(self.title)
                clean_tome = sanitize_folder_name(normalize_tome_label(vol))
                if cbz_enabled:
                    cbz_path = os.path.join(output_root, clean_title, f"{clean_title} - {clean_tome}.cbz")
                    if os.path.exists(cbz_path) and os.path.getsize(cbz_path) > 10_000:
                        self.log(
                            f"CBZ déjà existant, saut du tome: {vol}",
                            level="info",
                            context={"domain": domain, "tome": vol, "action": "skip_existing"},
                        )
                        self.run_on_ui(self._set_progress_detail_ui, None, None)
                        completed_volumes += 1
                        push_idle_global_eta()
                        continue

                self.run_on_ui(self._set_progress_ui, 0)

                ua = self.get_request_user_agent_for_url(link)
                images = get_images(link, cookie, ua)
                self.log(
                    f"{len(images)} image(s) trouvée(s)",
                    level="info",
                    context={"domain": domain, "tome": vol, "action": "images_count"},
                )

                if images:
                    self.run_on_ui(self._set_progress_detail_ui, 0, len(images))
                    progress_state = {"last_done": 0, "last_ts": 0.0}
                    volume_error_state = {"reported": False, "stage": "", "reason": ""}

                    def volume_error_callback(payload):
                        if not isinstance(payload, dict):
                            return
                        stage = payload.get("stage") or "download"
                        reason = payload.get("reason") or "Erreur inconnue"
                        volume_error_state["reported"] = True
                        volume_error_state["stage"] = stage
                        volume_error_state["reason"] = reason
                        self.add_volume_error(
                            payload.get("tome") or vol,
                            stage,
                            reason,
                            payload.get("status_code"),
                            payload.get("action"),
                        )

                    def per_image_progress(done, total_images):
                        percent = round((done / total_images) * 100, 1) if total_images else 0
                        self.run_on_ui(self._set_progress_ui, percent)
                        self.run_on_ui(self._set_progress_detail_ui, done, total_images)

                        now = time.time()
                        if (
                            done == total_images
                            or now - progress_state["last_ts"] >= 1.5
                            or done - progress_state["last_done"] >= 15
                        ):
                            progress_state["last_done"] = done
                            progress_state["last_ts"] = now
                            self.log(
                                f"Progression image: {done}/{total_images} ({int(percent)}%)",
                                level="info",
                            )

                        tome_eta = None
                        projected_current_total = None
                        elapsed = max(0.001, now - volume_start)
                        if total_images and done > 0:
                            projected_current_total = elapsed * (total_images / done)
                            if done < total_images:
                                tome_eta = elapsed * ((total_images - done) / done)
                            else:
                                tome_eta = 0

                        if completed_volume_durations:
                            avg_volume = sum(completed_volume_durations) / len(completed_volume_durations)
                        else:
                            avg_volume = projected_current_total

                        remaining_after_current = max(0, total_volumes - completed_volumes - 1)
                        if avg_volume is not None:
                            base_current = tome_eta
                            if base_current is None:
                                base_current = projected_current_total
                            if base_current is None:
                                base_current = avg_volume
                            global_eta = max(0, base_current) + (remaining_after_current * avg_volume)
                        else:
                            global_eta = None
                        self.run_on_ui(self._set_eta_ui, tome_eta, global_eta)

                    self.log(
                        "Début du téléchargement.",
                        level="success",
                        context={"domain": domain, "tome": vol, "action": "download_begin"},
                    )
                    dl_result = download_volume(
                        vol,
                        images,
                        self.title,
                        cookie,
                        ua,
                        self.log,
                        self.cancel_event,
                        cbz_enabled,
                        update_progress=per_image_progress,
                        webp2jpg_enabled=webp2jpg_enabled,
                        referer_url=link,
                        smart_resume_enabled=smart_resume_enabled,
                        error_callback=volume_error_callback,
                        output_root=output_root,
                    )
                    if dl_result is None and self.cancel_event.is_set():
                        break

                    if dl_result is False:
                        self.log(
                            "Tome non finalisé.",
                            level="warning",
                            context={"domain": domain, "tome": vol, "action": "download_incomplete"},
                        )
                        if not volume_error_state["reported"]:
                            self.add_volume_error(
                                vol,
                                "download",
                                "Tome non finalisé.",
                                None,
                                recommend_action_for_failure(None, "Tome non finalisé."),
                            )
                        stage = (volume_error_state.get("stage") or "").strip().lower()
                        should_retry = stage not in {"archive_cbz", "archive_cleanup", "prepare"}
                        if should_retry:
                            failed.append((vol, link))
                    else:
                        self.run_on_ui(self._set_progress_ui, 100)
                        elapsed = max(0.0, time.time() - volume_start)
                        completed_volume_durations.append(elapsed)
                        self.log(
                            f"Temps écoulé: {round(elapsed, 2)} secondes",
                            level="info",
                            context={"domain": domain, "tome": vol, "action": "download_done"},
                        )
                else:
                    self.run_on_ui(self._set_progress_detail_ui, None, None)
                    reason = "Échec récupération images."
                    self.log(
                        reason,
                        level="warning",
                        context={"domain": domain, "tome": vol, "action": "images_fetch_failed"},
                    )
                    self.add_volume_error(
                        vol,
                        "images",
                        reason,
                        None,
                        recommend_action_for_failure(None, reason),
                    )
                    failed.append((vol, link))

                completed_volumes += 1
                push_idle_global_eta()

            if not self.cancel_event.is_set() and failed:
                self.run_on_ui(self._set_eta_ui, None, None)
                self.log(
                    f"Retry des tomes échoués ({len(failed)} restants)",
                    level="warning",
                )
                retry_failed = []

                for vol, link in failed:
                    if self.cancel_event.is_set():
                        break
                    self.run_on_ui(self._set_current_volume_ui, vol)
                    cookie = self.get_cookie(link)
                    ua = self.get_request_user_agent_for_url(link)
                    images = get_images(link, cookie, ua)
                    if images:
                        self.log(f"Retry réussi: {vol}", level="info")
                        retry_error_state = {"reported": False}

                        def retry_error_callback(payload):
                            if not isinstance(payload, dict):
                                return
                            retry_error_state["reported"] = True
                            self.add_volume_error(
                                payload.get("tome") or vol,
                                payload.get("stage") or "download",
                                payload.get("reason") or "Erreur inconnue",
                                payload.get("status_code"),
                                payload.get("action"),
                            )

                        retry_result = download_volume(
                            vol,
                            images,
                            self.title,
                            cookie,
                            ua,
                            self.log,
                            self.cancel_event,
                            cbz_enabled,
                            update_progress=None,
                            webp2jpg_enabled=webp2jpg_enabled,
                            referer_url=link,
                            smart_resume_enabled=smart_resume_enabled,
                            error_callback=retry_error_callback,
                            output_root=output_root,
                        )
                        if retry_result is False:
                            if not retry_error_state["reported"]:
                                self.add_volume_error(
                                    vol,
                                    "retry",
                                    "Retry non finalisé.",
                                    None,
                                    recommend_action_for_failure(None, "Retry non finalisé."),
                                )
                            retry_failed.append(vol)
                        if retry_result is None and self.cancel_event.is_set():
                            break
                    else:
                        reason = f"Retry échoué: récupération images impossible ({vol})."
                        self.log(reason, level="error")
                        self.add_volume_error(
                            vol,
                            "retry",
                            reason,
                            None,
                            recommend_action_for_failure(None, reason),
                        )
                        retry_failed.append(vol)

                if retry_failed:
                    self.log(
                        f"Tomes définitivement échoués: {', '.join(retry_failed)}",
                        level="error",
                    )

            if self.cancel_event.is_set():
                self.log("Téléchargement annulé !", level="warning")
                self.run_on_ui(self._set_progress_ui, 0)
                self.run_on_ui(self._set_workflow_step, "logs", "Téléchargement annulé. Consulte le journal.")
            else:
                self.log("Tous les tomes ont été traités.", level="success")
                self.run_on_ui(self._set_workflow_step, "logs", "Traitement terminé. Vérifie le journal final.")

            self.cancel_event.clear()
            self.run_on_ui(self._set_download_controls, False)
            self.run_on_ui(self._set_eta_ui, None, None)
            self.run_on_ui(self.root.title, f"{APP_NAME} v{APP_VERSION}")

        threading.Thread(target=task, daemon=True).start()

    def cancel_download(self):
        """Annule le téléchargement en cours"""
        self.cancel_event.set()
        self.log("Annulation demandée...", level="warning")
        self.cancel_button.config(state="disabled")
        self._set_workflow_step("logs", "Annulation demandée. Attente de fin des threads...")

    def save_current_cookie(self):
        """Sauvegarde les paramètres actuels dans le cache"""
        try:
            self.persist_settings()
            self.log("Cookies, UA, CBZ, WEBP->JPG et préférences logs sauvegardées !", level="success")
            self.update_cookie_status()
            self.update_runtime_status()
        except Exception as e:
            self.log(f"Erreur sauvegarde: {e}", level="error")


# Point d'entrée de l'application
if __name__ == "__main__":
    runtime_log(f"Lancement de {APP_NAME} v{APP_VERSION}", level="info")
    MangaApp()
