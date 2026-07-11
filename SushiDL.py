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
import argparse
import html
import json
import csv
import math
import base64
import zlib
import hashlib
import shutil
import threading
import time
import datetime
import queue
import sys
import unicodedata
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence, ImageTk
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


class SushiDLError(Exception):
    """Erreur de base contrôlée par SushiDL."""


class AuthError(SushiDLError):
    """Erreur liée à l'authentification ou à une protection serveur."""


class ParseError(SushiDLError):
    """Erreur liée au parsing d'une page source."""


class ArchiveError(SushiDLError):
    """Erreur liée à la création ou validation d'une archive."""


class ImageDownloadError(Exception):
    """Erreur de téléchargement enrichie avec type et code HTTP."""

    def __init__(self, message, status_code=None, kind="retryable", phase="direct"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind
        self.phase = phase


@dataclass(frozen=True)
class MangaAnalysis:
    """Résultat structuré d'une analyse catalogue."""

    title: str = ""
    pairs: list[tuple[str, str]] = field(default_factory=list)
    volume_metadata: dict = field(default_factory=dict)
    series_metadata: dict = field(default_factory=dict)
    html_content: str = ""


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


def should_offer_cookie_refresh(status_code=None, reason=""):
    """Indique si l'échec justifie de demander un renouvellement de cookie."""
    lower = (reason or "").lower()
    normalized = "".join(
        ch for ch in unicodedata.normalize("NFKD", lower) if unicodedata.category(ch) != "Mn"
    )
    if status_code in (401, 403):
        return True
    markers = (
        "http error 401",
        "http error 403",
        "forbidden",
        "acces refuse",
        "cloudflare",
        "just a moment",
        "challenge",
        "cookie",
        "unauthorized",
    )
    return any(marker in normalized for marker in markers)


def interruptible_sleep(cancel_event, duration):
    """Attend `duration` secondes, interrompu si annulation demandée."""
    if duration <= 0:
        return False
    if cancel_event is None:
        time.sleep(duration)
        return False
    return cancel_event.wait(duration)


def normalize_chapter_label_preserve_title(label):
    """Normalise un label chapitre en conservant son titre après deux-points."""
    cleaned = repair_mojibake_text((label or "").strip())
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|:")
    match = re.match(
        r"(?i)^(?:ep|episode|chapitre|chapter|chap)\s*[-._:# ]*(extra(?:\s*[-._ ]*\s*\d+)?|[0-9]+(?:[-.,][0-9]+)*(?:\s*[A-Za-z])?)\b\s*(?::\s*(.+))?$",
        cleaned,
    )
    if not match:
        return ""
    raw_number = re.sub(r"\s+", " ", match.group(1).strip())
    if raw_number.lower().startswith("extra"):
        number = re.sub(r"(?i)^extra\s*[-._ ]*\s*(\d+)$", r"Extra \1", raw_number).strip()
    else:
        number = raw_number.replace(",", ".").strip()
    suffix = (match.group(2) or "").strip()
    chapter_label = f"Chap {number}".strip()
    return f"{chapter_label} : {suffix}" if suffix else chapter_label


def normalize_tome_label(label):
    """Normalise les labels en standardisant épisode/chapitre/tome et en retirant les titres redondants."""
    cleaned = repair_mojibake_text((label or "").strip())
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|:")

    number_pattern = r"([0-9]+(?:[.,][0-9]+)?(?:\s*[A-Za-z])?)"

    tome_composite_match = re.match(
        rf"(?i)^(?:tome|volume)\s*[-._:# ]*{number_pattern}(?:\s*\([^)]*\))?\s*[-–—:]\s*(.+)$",
        cleaned,
    )
    if tome_composite_match:
        tome_number = tome_composite_match.group(1).replace(",", ".").strip()
        child_raw = tome_composite_match.group(2).strip()
        child_label = normalize_chapter_label_preserve_title(child_raw) or normalize_tome_label(child_raw)
        return f"Tome {tome_number} - {child_label or tome_composite_match.group(2).strip()}".strip()

    chapter_match = re.match(
        rf"(?i)^(?:ep|episode|chapitre|chapter)\s*[-._:# ]*\s*{number_pattern}\b",
        cleaned,
    )
    if chapter_match:
        return f"Chapitre {chapter_match.group(1).replace(',', '.')}".strip()

    tome_match = re.match(
        rf"(?i)^(?:tome|volume)\s*[-._:# ]*\s*{number_pattern}\b",
        cleaned,
    )
    if tome_match:
        return f"Tome {tome_match.group(1).replace(',', '.')}".strip()

    return re.sub(r"(?i)\bvolume\b", "Tome", cleaned)


def normalize_image_url(url):
    """Normalise les URLs d'images (https forcé, schéma manquant géré)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    try:
        parsed = urlparse(raw)
        if parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost"}:
            return raw
    except Exception:
        pass
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
    if value == "toonfr.com" or value.endswith(".toonfr.com"):
        return "toonfr.com"
    if value == "ortegascans.fr" or value.endswith(".ortegascans.fr"):
        return "ortegascans.fr"
    if value == "hentaizone.xyz" or value.endswith(".hentaizone.xyz"):
        return "hentaizone.xyz"
    if value == "scan-manga.com" or value.endswith(".scan-manga.com"):
        return "scan-manga.com"
    if value == "crunchyscan.fr" or value.endswith(".crunchyscan.fr"):
        return "crunchyscan.fr"
    if value == "scan-hentai.net" or value.endswith(".scan-hentai.net"):
        return "scan-hentai.net"
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
        "toonfr.com": "toonfr",
        "ortegascans.fr": "ortega",
        "hentaizone.xyz": "hentaizone",
        "scan-manga.com": "scanmanga",
        "crunchyscan.fr": "crunchyscan",
        "scan-hentai.net": "scanhentai",
    }
    return mapping.get(site, "")


def get_cookie_domain_from_url(url):
    """Retourne le domaine cookie interne depuis une URL supportée."""
    return get_cookie_domain_from_host((urlparse(url).hostname or "").strip().lower() if url else "")


def get_site_domain_key(url):
    """Retourne la clé de domaine interne utilisée par les parseurs et téléchargements."""
    site = get_supported_site_from_url(url)
    mapping = {
        "sushiscan.fr": "fr",
        "sushiscan.net": "net",
        "mangas-origines.fr": "origines",
        "hentai-origines.fr": "hentai",
        "toonfr.com": "toonfr",
        "ortegascans.fr": "ortega",
        "hentaizone.xyz": "hentaizone",
        "scan-manga.com": "scanmanga",
        "crunchyscan.fr": "crunchyscan",
        "scan-hentai.net": "scanhentai",
    }
    return mapping.get(site) or normalize_hostname(urlparse(url).hostname) or "-"


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
    elif site == "toonfr.com":
        match = re.match(r"^/webtoon/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    elif site == "ortegascans.fr":
        match = re.match(r"^/serie/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    elif site == "hentaizone.xyz":
        match = re.match(r"^/manga/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    elif site == "scan-manga.com":
        match = re.match(r"^/\d+(?:-\d+)?/([^/?#]+)\.html$", path, flags=re.IGNORECASE)
    elif site in ("crunchyscan.fr", "scan-hentai.net"):
        match = re.match(r"^/lecture-en-ligne/([^/?#]+)/?$", path, flags=re.IGNORECASE)
    else:
        return False

    if not match:
        return False
    return is_valid_catalogue_slug(match.group(1))


def is_valid_catalogue_slug(slug):
    """Valide un segment de slug, y compris les caractères percent-encodés."""
    value = (slug or "").strip()
    if not value:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in value):
        return False
    if any(ch in value for ch in "/\\?#"):
        return False
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        return False

    decoded = unquote(value)
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in decoded):
        return False
    return not any(ch in decoded for ch in "/\\?#")


def extract_supported_catalogue_url(text):
    """Extrait la première URL catalogue supportée depuis un collage bruité."""
    value = repair_mojibake_text(text or "")
    if not value:
        return ""

    candidates = []
    if len(value) <= 2048 and is_valid_catalogue_url(value.strip()):
        candidates.append(value.strip())
    candidates.extend(re.findall(r"https?://[^\s<>'\"\\)\]]+", value, flags=re.IGNORECASE))

    for candidate in candidates:
        cleaned = repair_mojibake_text(candidate).strip()
        cleaned = cleaned.rstrip(".,;:)]}\"'")
        if is_valid_catalogue_url(cleaned):
            return cleaned
    return ""


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
                raise ImageDownloadError(
                    f"Contenu non image: {test_e}",
                    kind="invalid_image",
                    phase="direct",
                )

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
            if e.kind in ("missing", "invalid_image"):
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


def clamp_download_threads(value):
    """Normalise le nombre de telechargements paralleles autorises."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = DEFAULT_DOWNLOAD_THREADS
    return max(MIN_DOWNLOAD_THREADS, min(MAX_DOWNLOAD_THREADS, count))


def _is_html_payload_start(payload):
    snippet = bytes(payload or b"")[:1024].lower()
    return snippet.startswith(b"<html") or b"<html" in snippet or b"<!doctype html" in snippet


def validate_image_file(path):
    """Verifie qu'un fichier disque est bien lisible comme image."""
    with Image.open(path) as image:
        image.verify()


def is_text_page_url(url):
    return str(url or "").startswith(TEXT_PAGE_URL_PREFIX)


def _text_block_text(block):
    if isinstance(block, dict):
        return normalize_novel_text(block.get("text", ""))
    return normalize_novel_text(block)


def _text_block_align(block):
    if isinstance(block, dict):
        align = str(block.get("align") or "left").strip().lower()
        return align if align in {"left", "center", "right"} else "left"
    return "left"


def _text_block_kind(block):
    if isinstance(block, dict):
        kind = str(block.get("kind") or "text").strip().lower()
        return kind if kind in {"text", "image", "spacer"} else "text"
    return "text"


def _text_page_cache_key(source_url, title, paragraphs):
    block_payload = []
    for block in paragraphs or []:
        if _text_block_kind(block) == "image" and isinstance(block, dict):
            block_payload.append(f"image:{_text_block_align(block)}:{block.get('src', '')}")
        elif _text_block_kind(block) == "spacer" and isinstance(block, dict):
            block_payload.append(f"spacer:{block.get('height', '')}")
        else:
            block_payload.append(f"text:{_text_block_align(block)}:{_text_block_text(block)}")
    payload = "\n".join([(source_url or "").strip(), (title or "").strip(), "\n".join(block_payload)])
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:20]


def store_text_page_bytes(key, page_bytes):
    safe_key = (key or "").strip()
    if not safe_key:
        return
    with TEXT_PAGE_CACHE_LOCK:
        TEXT_PAGE_CACHE[safe_key] = list(page_bytes or [])
        if safe_key in TEXT_PAGE_CACHE_ORDER:
            TEXT_PAGE_CACHE_ORDER.remove(safe_key)
        TEXT_PAGE_CACHE_ORDER.append(safe_key)
        while len(TEXT_PAGE_CACHE_ORDER) > TEXT_PAGE_CACHE_MAX_ITEMS:
            old_key = TEXT_PAGE_CACHE_ORDER.pop(0)
            TEXT_PAGE_CACHE.pop(old_key, None)


def get_text_page_bytes(url):
    if not is_text_page_url(url):
        return None
    parsed = urlparse(url)
    key = (parsed.netloc or "").strip()
    page_name = (parsed.path or "").strip("/").rsplit("/", 1)[-1]
    page_number_text = page_name.rsplit(".", 1)[0]
    try:
        page_index = int(page_number_text) - 1
    except (TypeError, ValueError):
        return None
    with TEXT_PAGE_CACHE_LOCK:
        pages = TEXT_PAGE_CACHE.get(key)
        if pages is None:
            return None
        if key in TEXT_PAGE_CACHE_ORDER:
            TEXT_PAGE_CACHE_ORDER.remove(key)
        TEXT_PAGE_CACHE_ORDER.append(key)
        if 0 <= page_index < len(pages):
            return pages[page_index]
    return None


def _image_url_cache_key(link, max_images=None):
    safe_link = (link or "").strip()
    try:
        limit = int(max_images or 0)
    except (TypeError, ValueError):
        limit = 0
    return (safe_link, max(0, limit))


def get_cached_image_urls(link, max_images=None):
    key = _image_url_cache_key(link, max_images)
    with IMAGE_URL_CACHE_LOCK:
        cached = IMAGE_URL_CACHE.get(key)
        if cached is not None:
            if key in IMAGE_URL_CACHE_ORDER:
                IMAGE_URL_CACHE_ORDER.remove(key)
            IMAGE_URL_CACHE_ORDER.append(key)
            return list(cached)
        if key[1]:
            full_key = _image_url_cache_key(link, None)
            full_cached = IMAGE_URL_CACHE.get(full_key)
            if full_cached is not None:
                if full_key in IMAGE_URL_CACHE_ORDER:
                    IMAGE_URL_CACHE_ORDER.remove(full_key)
                IMAGE_URL_CACHE_ORDER.append(full_key)
                return list(full_cached[:key[1]])
    return None


def store_cached_image_urls(link, images, max_images=None):
    clean_images = [item for item in (images or []) if item]
    if not link or not clean_images:
        return
    key = _image_url_cache_key(link, max_images)
    with IMAGE_URL_CACHE_LOCK:
        IMAGE_URL_CACHE[key] = list(clean_images)
        if key in IMAGE_URL_CACHE_ORDER:
            IMAGE_URL_CACHE_ORDER.remove(key)
        IMAGE_URL_CACHE_ORDER.append(key)
        while len(IMAGE_URL_CACHE_ORDER) > IMAGE_URL_CACHE_MAX_ITEMS:
            old_key = IMAGE_URL_CACHE_ORDER.pop(0)
            IMAGE_URL_CACHE.pop(old_key, None)


def log_perf(logger, label, started_at, **context):
    """Journalise un temps d'etape si le logger est disponible."""
    if not callable(logger):
        return
    elapsed = max(0.0, time.perf_counter() - float(started_at or time.perf_counter()))
    if elapsed < PERF_LOG_MIN_SECONDS:
        return
    details = " | ".join(f"{key}={value}" for key, value in context.items() if value not in (None, ""))
    suffix = f" ({details})" if details else ""
    try:
        logger(f"[perf] {label}: {elapsed:.2f}s{suffix}", level="debug")
    except TypeError:
        logger(f"[perf] {label}: {elapsed:.2f}s{suffix}")


def should_reduce_threads_for_failures(failures):
    """Detecte les erreurs qui meritent de ralentir plutot que demander un cookie."""
    for failure in failures or []:
        status_code = failure.get("status_code") if isinstance(failure, dict) else None
        if status_code in ADAPTIVE_THREAD_FAILURE_CODES:
            return True
        reason = (failure.get("reason") if isinstance(failure, dict) else str(failure or "")).lower()
        if "429" in reason or "too many" in reason or "rate limit" in reason or "timeout" in reason:
            return True
    return False


def download_image_to_file(img_url, filename, headers, max_try=4, delay=2, cancel_event=None):
    """
    Telecharge une image vers un fichier .part puis renomme atomiquement.
    Evite de conserver les images completes en memoire pendant les gros lots.
    """
    normalized_url = normalize_image_url(img_url)
    tmp_filename = f"{filename}.part-{threading.get_ident()}"
    last_exc = None

    for attempt in range(1, max_try + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            session = _get_http_session()
            try:
                response = session.get(
                    normalized_url,
                    headers=headers,
                    impersonate="chrome",
                    timeout=20,
                    stream=True,
                )
            except TypeError:
                response = session.get(
                    normalized_url,
                    headers=headers,
                    impersonate="chrome",
                    timeout=20,
                )
            except Exception:
                try:
                    response = requests.get(
                        normalized_url,
                        headers=headers,
                        impersonate="chrome",
                        timeout=20,
                        stream=True,
                    )
                except TypeError:
                    response = requests.get(
                        normalized_url,
                        headers=headers,
                        impersonate="chrome",
                        timeout=20,
                    )

            status_code = getattr(response, "status_code", None)
            if status_code and status_code >= 400:
                kind = classify_download_failure(status_code, f"HTTP Error {status_code}")
                raise ImageDownloadError(
                    f"HTTP Error {status_code}",
                    status_code=status_code,
                    kind=kind,
                    phase="direct",
                )
            response.raise_for_status()

            first_bytes = bytearray()
            bytes_written = 0
            iter_content = getattr(response, "iter_content", None)
            with open(tmp_filename, "wb") as out:
                if callable(iter_content):
                    for chunk in iter_content(chunk_size=256 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled("Téléchargement annulé.")
                        if not chunk:
                            continue
                        if len(first_bytes) < 1024:
                            missing = 1024 - len(first_bytes)
                            first_bytes.extend(chunk[:missing])
                        out.write(chunk)
                        bytes_written += len(chunk)
                else:
                    raw = response.content
                    first_bytes.extend(raw[:1024])
                    out.write(raw)
                    bytes_written = len(raw)

            if bytes_written <= 0:
                raise ImageDownloadError(
                    "Réponse vide",
                    kind="retryable",
                    phase="direct",
                )
            if _is_html_payload_start(first_bytes):
                raise ImageDownloadError(
                    "Réponse HTML (protection serveur ou Cloudflare)",
                    kind="blocked_or_retryable",
                    phase="direct",
                )

            try:
                validate_image_file(tmp_filename)
            except Exception as test_e:
                runtime_log(
                    f"Tentative {attempt}: contenu non reconnu comme image: {test_e}",
                    level="warning",
                    context={"action": "image_integrity"},
                )
                raise ImageDownloadError(
                    f"Contenu non image: {test_e}",
                    kind="invalid_image",
                    phase="direct",
                )

            os.replace(tmp_filename, filename)
            return filename

        except DownloadCancelled:
            try:
                os.remove(tmp_filename)
            except OSError:
                pass
            raise
        except ImageDownloadError as exc:
            try:
                os.remove(tmp_filename)
            except OSError:
                pass
            runtime_log(
                f"Tentative {attempt} échouée pour {normalized_url}: {exc}",
                level="warning",
                context={"action": "image_retry"},
            )
            last_exc = exc
            if exc.kind in ("missing", "invalid_image"):
                raise exc
            sleep_time = min(delay * (2 ** attempt), 60) if exc.status_code in (403, 429) else (delay * attempt)
            if interruptible_sleep(cancel_event, sleep_time):
                raise DownloadCancelled("Téléchargement annulé.")
        except Exception as exc:
            try:
                os.remove(tmp_filename)
            except OSError:
                pass
            runtime_log(
                f"Tentative {attempt} échouée pour {normalized_url}: {exc}",
                level="warning",
                context={"action": "image_retry"},
            )
            status_code = get_status_code_from_exception(exc)
            kind = classify_download_failure(status_code, str(exc))
            wrapped = ImageDownloadError(
                str(exc),
                status_code=status_code,
                kind=kind,
                phase="direct",
            )
            last_exc = wrapped
            if kind == "missing":
                raise wrapped
            sleep_time = min(delay * (2 ** attempt), 60) if status_code in (403, 429) else (delay * attempt)
            if interruptible_sleep(cancel_event, sleep_time):
                raise DownloadCancelled("Téléchargement annulé.")

    if isinstance(last_exc, Exception):
        raise last_exc
    raise ImageDownloadError(
        f"Impossible de télécharger l'image {normalized_url} après {max_try} tentatives.",
        kind="retryable",
        phase="direct",
    )


# Expressions régulières et constantes globales
APP_NAME = "SushiDL"
APP_VERSION = "11.16.7"
REGEX_URL = r"^https://(?:sushiscan\.(?:fr|net)/catalogue|mangas-origines\.fr/oeuvre|hentai-origines\.fr/manga|toonfr\.com/webtoon|ortegascans\.fr/serie|hentaizone\.xyz/manga|crunchyscan\.fr/lecture-en-ligne|scan-hentai\.net/lecture-en-ligne)/[^/?#\s]+/?$|^https://www\.scan-manga\.com/\d+(?:-\d+)?/[^/?#\s]+\.html$"  # Formats d'URL valides
ROOT_FOLDER = "DL SushiScan"  # Dossier racine pour les téléchargements
DEFAULT_DOWNLOAD_THREADS = 3
MIN_DOWNLOAD_THREADS = 1
MAX_DOWNLOAD_THREADS = 8
THREADS = DEFAULT_DOWNLOAD_THREADS  # Compatibilite interne historique.
UI_CALL_TIMEOUT_SECONDS = 15  # Timeout max pour un appel synchrone vers le thread UI
UI_QUEUE_BATCH_LIMIT = 90
UI_QUEUE_TIME_BUDGET_SECONDS = 0.009
VOLUME_RENDER_BATCH_SIZE = 36  # Rendu progressif des gros listings pour eviter les timeouts UI
GUI_LOG_FLUSH_INTERVAL_MS = 70
GUI_LOG_FLUSH_MAX_BATCH = 120
VOLUME_COMPACT_MODE_THRESHOLD = 180  # Au-dela, bascule vers un rendu compact plus rapide
VOLUME_FAST_WIDGET_THRESHOLD = 2400  # Fallback ultime pour des catalogues exceptionnellement grands
VOLUME_GROUP_HEADER_MAX_ITEMS = 220  # Au-dela, garde une grille canvas simple sans en-tetes de groupe.
VOLUME_VIRTUALIZATION_BUFFER_ROWS = 4
VOLUME_VIRTUAL_HEADER_ROW_HEIGHT = 42
VOLUME_VIRTUAL_REFRESH_THROTTLE_MS = 12
VOLUME_VIRTUAL_ROW_HEIGHT_COMPACT = 36
VOLUME_VIRTUAL_ROW_HEIGHT_CARD = 62
VOLUME_MAX_GRID_COLUMNS = 4
VOLUME_CARD_COLUMN_WIDTH = 300
VOLUME_COMPACT_COLUMN_WIDTH = 236
VOLUME_FAST_COLUMN_WIDTH = 236
PREVIEW_PAGE_LIMIT = 5
PREVIEW_CACHE_MAX_ITEMS = 3
PREVIEW_SCANMANGA_PAGE_LIMIT = 1
PREVIEW_MAX_IMAGE_DIMENSION = 1600
COVER_ANIMATION_MAX_FRAMES = 24
IMAGE_URL_CACHE_MAX_ITEMS = 512
PROGRESS_UI_MIN_INTERVAL = 0.18
PROGRESS_UI_MIN_DELTA = 3
ADAPTIVE_THREAD_FAILURE_CODES = {429, 500, 502, 503, 504}
PERF_LOG_MIN_SECONDS = 0.05
SPINNER_FRAMES = ("|", "/", "-", "\\")
MAX_VISIBLE_ERROR_ROWS = 500
COOKIE_DOMAINS = (
    "fr",
    "net",
    "origines",
    "hentai",
    "toonfr",
    "ortega",
    "hentaizone",
    "scanmanga",
    "crunchyscan",
    "scanhentai",
)
COVER_RATIO_WIDTH = 2
COVER_RATIO_HEIGHT = 3
COVER_TARGET_HEIGHT = 150
BASE_DIR = Path(__file__).resolve().parent
APP_ICON_PATH = BASE_DIR / "assets" / "sushidl.ico"
COOKIE_CACHE_PATH = BASE_DIR / "cookie_cache.json"  # Fichier de cache pour les cookies
CONFIG_PATH = BASE_DIR / "config.json"  # Configuration globale de l'application
ANALYSIS_CACHE_PATH = BASE_DIR / "analysis_cache.json"
CATALOG_STATE_PATH = BASE_DIR / "catalog_state.json"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
ANALYSIS_CACHE_LOCK = threading.Lock()
ANALYSIS_CACHE_MEMORY = None
ANALYSIS_CACHE_SCHEMA_VERSION = 6
CATALOG_STATE_LOCK = threading.Lock()
CATALOG_STATE_MEMORY = None
CATALOG_STATE_SCHEMA_VERSION = 1
WATCHLIST_LOCK = threading.Lock()
WATCHLIST_MEMORY = None
WATCHLIST_SCHEMA_VERSION = 1
IMAGE_URL_CACHE = {}
IMAGE_URL_CACHE_ORDER = []
IMAGE_URL_CACHE_LOCK = threading.Lock()
TEXT_PAGE_URL_PREFIX = "sushidl-textpage://"
TEXT_PAGE_CACHE = {}
TEXT_PAGE_CACHE_LOCK = threading.Lock()
TEXT_PAGE_CACHE_MAX_ITEMS = 128
TEXT_PAGE_CACHE_ORDER = []
SCANMANGA_BROWSER_LOCK = threading.Lock()
SCANMANGA_BROWSER_THREAD = None
SCANMANGA_BROWSER_TASKS = None
CRUNCHY_BROWSER_LOCK = threading.Lock()
CRUNCHY_BROWSER_THREAD = None
CRUNCHY_BROWSER_TASKS = None
SCANMANGA_IMAGE_HOSTS = {
    "cdn.scan-manga.com",
    "data.scan-manga.com",
    "data2.scan-manga.com",
    "data3.scan-manga.com",
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
DIRECT_USER_AGENT_DEFAULT = DEFAULT_USER_AGENT
DEFAULT_APP_CONFIG = {
    "auth_mode": "manual",
    "analysis_cache_ttl_seconds": 21600,
    "fragile_sites": {
        "toonfr": {"enabled": True, "max_threads": 1, "delay_between_volumes": 0.4},
        "ortega": {"enabled": True, "max_threads": 1, "delay_between_volumes": 0.4},
        "hentaizone": {"enabled": True, "max_threads": 2, "delay_between_volumes": 0.25},
        "scanmanga": {"enabled": True, "max_threads": 1, "delay_between_volumes": 0.5},
        "crunchyscan": {"enabled": True, "max_threads": 1, "delay_between_volumes": 0.4},
        "scanhentai": {"enabled": True, "max_threads": 1, "delay_between_volumes": 0.4},
    },
    "manual_links": {
        "cookie_fr": "https://sushiscan.fr",
        "cookie_net": "https://sushiscan.net",
        "cookie_origines": "https://mangas-origines.fr",
        "cookie_hentai": "https://hentai-origines.fr",
        "cookie_toonfr": "https://toonfr.com",
        "cookie_ortega": "https://ortegascans.fr",
        "cookie_hentaizone": "https://hentaizone.xyz",
        "cookie_scanmanga": "https://www.scan-manga.com",
        "cookie_crunchyscan": "https://crunchyscan.fr",
        "cookie_scanhentai": "https://scan-hentai.net",
        "user_agent": "https://httpbin.org/user-agent",
        "cookie_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
        "cloudflare_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance",
    },
}
STARTUP_COOKIE_LISTING_PROBE_URLS = {
    "fr": "https://sushiscan.fr/catalogue/one-piece/",
    "net": "https://sushiscan.net/catalogue/one-piece/",
    "origines": "https://mangas-origines.fr/oeuvre/826-solo-leveling/",
    "hentai": "https://hentai-origines.fr/manga/stop-smoking/",
    "toonfr": "https://toonfr.com/webtoon/ma-brute/",
    "ortega": "https://ortegascans.fr/serie/moby-dick",
    "hentaizone": "https://hentaizone.xyz/manga/stepmothers-friends/",
    "scanmanga": "https://www.scan-manga.com/16363/Death-Penalty.html",
    "crunchyscan": "https://crunchyscan.fr/lecture-en-ligne/hajime-no-ippo",
    "scanhentai": "https://scan-hentai.net/lecture-en-ligne/even-a-hopeless-romantic-wants-to-be-loved",
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
    "debug": "🔎",
    "info": "💬",
    "success": "✅",
    "warning": "⚠️",
    "error": "🔴",
    "cbz": "📦",
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


def set_private_file_permissions(path):
    """Restreint les permissions fichier quand la plateforme le permet."""
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except Exception:
        pass


def redact_sensitive_text(text):
    """Masque les cookies et jetons probables avant journalisation."""
    value = str(text or "")
    if not value:
        return ""
    patterns = (
        r"(?i)(cf_clearance=)[^;\s]+",
        r"(?i)(Cookie:\s*)[^\r\n]+",
        r"(?i)(cookies?['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;}]+",
    )
    for pattern in patterns:
        value = re.sub(pattern, r"\1[REDACTED]", value)
    return value


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


def get_analysis_cache_ttl_seconds():
    try:
        return max(0, int((APP_CONFIG or {}).get("analysis_cache_ttl_seconds", 21600)))
    except (TypeError, ValueError):
        return 21600


def _read_analysis_cache():
    global ANALYSIS_CACHE_MEMORY
    with ANALYSIS_CACHE_LOCK:
        if ANALYSIS_CACHE_MEMORY is not None:
            return dict(ANALYSIS_CACHE_MEMORY)
        if not ANALYSIS_CACHE_PATH.exists():
            ANALYSIS_CACHE_MEMORY = {}
            return {}
        try:
            with ANALYSIS_CACHE_PATH.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            ANALYSIS_CACHE_MEMORY = data if isinstance(data, dict) else {}
            return dict(ANALYSIS_CACHE_MEMORY)
        except Exception as exc:
            runtime_log(f"Cache analyse illisible: {exc}", level="debug")
            ANALYSIS_CACHE_MEMORY = {}
            return {}


def _analysis_cache_key(url, ua):
    return hashlib.sha256(f"{(url or '').strip()}|{(ua or '').strip()}".encode("utf-8", errors="ignore")).hexdigest()


def get_cached_analysis(url, ua):
    ttl = get_analysis_cache_ttl_seconds()
    if ttl <= 0:
        return None
    cache = _read_analysis_cache()
    entry = cache.get(_analysis_cache_key(url, ua))
    if not isinstance(entry, dict):
        return None
    try:
        schema_version = int(entry.get("schema_version") or 0)
    except (TypeError, ValueError):
        return None
    if schema_version != ANALYSIS_CACHE_SCHEMA_VERSION:
        return None
    try:
        age = time.time() - float(entry.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ttl:
        return None
    pairs = entry.get("pairs")
    if not isinstance(pairs, list):
        return None
    return {
        "title": entry.get("title") or "",
        "pairs": [(str(item[0]), str(item[1])) for item in pairs if isinstance(item, list) and len(item) >= 2],
        "volume_metadata": entry.get("volume_metadata") if isinstance(entry.get("volume_metadata"), dict) else {},
        "series_metadata": entry.get("series_metadata") if isinstance(entry.get("series_metadata"), dict) else {},
        "html_content": entry.get("html_content") or "",
        "age_seconds": age,
    }


def store_cached_analysis(url, ua, title, pairs, volume_metadata=None, series_metadata=None, html_content=""):
    global ANALYSIS_CACHE_MEMORY
    cache = _read_analysis_cache()
    cache[_analysis_cache_key(url, ua)] = {
        "schema_version": ANALYSIS_CACHE_SCHEMA_VERSION,
        "url": (url or "").strip(),
        "timestamp": time.time(),
        "title": title or "",
        "pairs": [[a, b] for a, b in (pairs or [])],
        "volume_metadata": volume_metadata or {},
        "series_metadata": series_metadata or {},
        "html_content": html_content or "",
    }
    if len(cache) > 80:
        ordered = sorted(cache.items(), key=lambda item: float((item[1] or {}).get("timestamp") or 0), reverse=True)
        cache = dict(ordered[:80])
    with ANALYSIS_CACHE_LOCK:
        ANALYSIS_CACHE_MEMORY = dict(cache)
        _write_json_file(ANALYSIS_CACHE_PATH, cache)


def _catalog_state_key(url):
    safe_url = normalize_image_url((url or "").strip())
    return safe_url.rstrip("/")


def _read_catalog_state():
    global CATALOG_STATE_MEMORY
    with CATALOG_STATE_LOCK:
        if CATALOG_STATE_MEMORY is not None:
            return dict(CATALOG_STATE_MEMORY)
        if not CATALOG_STATE_PATH.exists():
            CATALOG_STATE_MEMORY = {"schema_version": CATALOG_STATE_SCHEMA_VERSION, "catalogues": {}}
            return dict(CATALOG_STATE_MEMORY)
        try:
            with CATALOG_STATE_PATH.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                data = {}
            if int(data.get("schema_version") or 0) != CATALOG_STATE_SCHEMA_VERSION:
                data = {"schema_version": CATALOG_STATE_SCHEMA_VERSION, "catalogues": {}}
            data.setdefault("catalogues", {})
            if not isinstance(data.get("catalogues"), dict):
                data["catalogues"] = {}
            CATALOG_STATE_MEMORY = data
            return dict(CATALOG_STATE_MEMORY)
        except Exception as exc:
            runtime_log(f"Etat catalogue illisible: {exc}", level="debug")
            CATALOG_STATE_MEMORY = {"schema_version": CATALOG_STATE_SCHEMA_VERSION, "catalogues": {}}
            return dict(CATALOG_STATE_MEMORY)


def _write_catalog_state(data):
    global CATALOG_STATE_MEMORY
    safe_data = data if isinstance(data, dict) else {}
    safe_data["schema_version"] = CATALOG_STATE_SCHEMA_VERSION
    safe_data.setdefault("catalogues", {})
    with CATALOG_STATE_LOCK:
        CATALOG_STATE_MEMORY = dict(safe_data)
        _write_json_file(CATALOG_STATE_PATH, safe_data)


def update_catalog_state(url, title, pairs, domain="", volume_metadata=None):
    """Memorise l'etat d'un catalogue et retourne le delta depuis la derniere analyse."""
    key = _catalog_state_key(url)
    if not key:
        return {}
    current_items = {}
    for label, link in pairs or []:
        safe_link = (link or "").strip()
        if not safe_link:
            continue
        current_items[safe_link] = normalize_tome_label(label) or safe_link
    state = _read_catalog_state()
    catalogues = state.setdefault("catalogues", {})
    previous = catalogues.get(key) if isinstance(catalogues.get(key), dict) else {}
    previous_items = previous.get("known_items") if isinstance(previous.get("known_items"), dict) else {}
    first_seen = not bool(previous_items)
    new_urls = [link for link in current_items if link not in previous_items]
    removed_urls = [link for link in previous_items if link not in current_items]
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_items = [{"label": current_items[link], "url": link} for link in new_urls]
    removed_items = [{"label": previous_items.get(link, link), "url": link} for link in removed_urls]
    catalogues[key] = {
        "schema_version": CATALOG_STATE_SCHEMA_VERSION,
        "url": key,
        "title": normalize_metadata_text(title) or previous.get("title") or "",
        "domain": (domain or get_cookie_domain_from_url(key) or "").strip(),
        "last_checked_at": now_iso,
        "last_count": len(current_items),
        "known_items": current_items,
        "last_new_items": new_items if not first_seen else [],
        "last_removed_items": removed_items if not first_seen else [],
        "volume_metadata": volume_metadata if isinstance(volume_metadata, dict) else {},
    }
    _write_catalog_state(state)
    return {
        "url": key,
        "title": catalogues[key]["title"],
        "first_seen": first_seen,
        "previous_count": len(previous_items) if previous_items else int(previous.get("last_count") or 0),
        "current_count": len(current_items),
        "new_count": 0 if first_seen else len(new_items),
        "removed_count": 0 if first_seen else len(removed_items),
        "new_items": [] if first_seen else new_items,
        "removed_items": [] if first_seen else removed_items,
        "last_checked_at": now_iso,
    }


def format_catalog_state_summary(summary):
    if not summary:
        return ""
    current_count = int(summary.get("current_count") or 0)
    if summary.get("first_seen"):
        return f"Etat catalogue mémorisé: {current_count} élément(s)."
    new_count = int(summary.get("new_count") or 0)
    removed_count = int(summary.get("removed_count") or 0)
    if new_count or removed_count:
        parts = []
        if new_count:
            parts.append(f"+{new_count} nouveau(x)")
        if removed_count:
            parts.append(f"-{removed_count} retiré(s)")
        return f"Evolution catalogue: {current_count} élément(s), {' / '.join(parts)}."
    return f"Aucune nouveauté depuis la dernière analyse ({current_count} élément(s))."


def _read_watchlist():
    global WATCHLIST_MEMORY
    with WATCHLIST_LOCK:
        if WATCHLIST_MEMORY is not None:
            return dict(WATCHLIST_MEMORY)
        if not WATCHLIST_PATH.exists():
            WATCHLIST_MEMORY = {
                "schema_version": WATCHLIST_SCHEMA_VERSION,
                "settings": {"interval_minutes": 360, "auto_download": False, "notify_only": True},
                "items": [],
            }
            return dict(WATCHLIST_MEMORY)
        try:
            with WATCHLIST_PATH.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or int(data.get("schema_version") or 0) != WATCHLIST_SCHEMA_VERSION:
                data = {
                    "schema_version": WATCHLIST_SCHEMA_VERSION,
                    "settings": {"interval_minutes": 360, "auto_download": False, "notify_only": True},
                    "items": [],
                }
            data.setdefault("settings", {"interval_minutes": 360, "auto_download": False, "notify_only": True})
            data.setdefault("items", [])
            WATCHLIST_MEMORY = data
            return dict(WATCHLIST_MEMORY)
        except Exception as exc:
            runtime_log(f"Watchlist illisible: {exc}", level="debug")
            WATCHLIST_MEMORY = {
                "schema_version": WATCHLIST_SCHEMA_VERSION,
                "settings": {"interval_minutes": 360, "auto_download": False, "notify_only": True},
                "items": [],
            }
            return dict(WATCHLIST_MEMORY)


def _write_watchlist(data):
    global WATCHLIST_MEMORY
    safe_data = data if isinstance(data, dict) else {}
    safe_data["schema_version"] = WATCHLIST_SCHEMA_VERSION
    safe_data.setdefault("settings", {"interval_minutes": 360, "auto_download": False, "notify_only": True})
    safe_data.setdefault("items", [])
    if not isinstance(safe_data.get("items"), list):
        safe_data["items"] = []
    with WATCHLIST_LOCK:
        WATCHLIST_MEMORY = dict(safe_data)
        _write_json_file(WATCHLIST_PATH, safe_data)


def add_or_update_watchlist_url(url, title="", enabled=True):
    """Ajoute une URL a suivre. L'interface planificateur utilisera ce socle."""
    safe_url = _catalog_state_key(url)
    if not safe_url:
        return False
    data = _read_watchlist()
    items = data.setdefault("items", [])
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for item in items:
        if isinstance(item, dict) and _catalog_state_key(item.get("url")) == safe_url:
            item["title"] = normalize_metadata_text(title) or item.get("title", "")
            item["enabled"] = bool(enabled)
            item["updated_at"] = now_iso
            break
    else:
        items.append(
            {
                "url": safe_url,
                "title": normalize_metadata_text(title),
                "enabled": bool(enabled),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
    _write_watchlist(data)
    return True


def remove_watchlist_url(url):
    safe_url = _catalog_state_key(url)
    if not safe_url:
        return False
    data = _read_watchlist()
    items = data.setdefault("items", [])
    kept_items = [
        item for item in items
        if not (isinstance(item, dict) and _catalog_state_key(item.get("url")) == safe_url)
    ]
    if len(kept_items) == len(items):
        return False
    data["items"] = kept_items
    _write_watchlist(data)
    return True


def get_watchlist_entries_with_state():
    data = _read_watchlist()
    state = _read_catalog_state()
    catalogues = state.get("catalogues") if isinstance(state.get("catalogues"), dict) else {}
    entries = []
    for item in data.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        safe_url = _catalog_state_key(item.get("url"))
        if not safe_url:
            continue
        known = catalogues.get(safe_url) if isinstance(catalogues.get(safe_url), dict) else {}
        new_items = known.get("last_new_items") if isinstance(known.get("last_new_items"), list) else []
        removed_items = known.get("last_removed_items") if isinstance(known.get("last_removed_items"), list) else []
        entries.append(
            {
                "url": safe_url,
                "title": normalize_metadata_text(item.get("title")) or normalize_metadata_text(known.get("title")) or safe_url,
                "enabled": bool(item.get("enabled", True)),
                "created_at": item.get("created_at") or "",
                "updated_at": item.get("updated_at") or "",
                "last_checked_at": known.get("last_checked_at") or "",
                "last_count": int(known.get("last_count") or 0),
                "last_new_count": len(new_items),
                "last_removed_count": len(removed_items),
                "last_new_items": new_items,
                "domain": (known.get("domain") or get_cookie_domain_from_url(safe_url) or "").strip(),
            }
        )
    entries.sort(key=lambda entry: str(entry.get("title") or entry.get("url") or "").lower())
    return entries


def get_fragile_site_settings(domain):
    fragile = (APP_CONFIG or {}).get("fragile_sites", {})
    if not isinstance(fragile, dict):
        return {}
    settings = fragile.get(domain) or {}
    return settings if isinstance(settings, dict) and settings.get("enabled", False) else {}


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
            value = redact_sensitive_text(str(context.get(key, "")).strip())
            if value:
                parts.append(f"{key}={value}")
        for key, value in context.items():
            if key in ordered_keys:
                continue
            value_txt = redact_sensitive_text(str(value).strip())
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
    safe_message = strip_console_unsafe_chars(redact_sensitive_text(repair_mojibake_text(message)))
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
    text = redact_sensitive_text(repair_mojibake_text(str(message or "").strip()))
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


def sanitize_cookie_value(value):
    """Nettoie une valeur de cookie pour éviter l'injection d'en-têtes HTTP."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    cleaned_chars = []
    for ch in raw:
        code = ord(ch)
        if ch in "\r\n;":
            continue
        if code < 32 or code == 127:
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip()
    if len(cleaned) > 4096:
        cleaned = cleaned[:4096]
    return cleaned


def sanitize_cookie_header(header_value):
    """Nettoie un header Cookie complet (name=value; name2=value2)."""
    raw = str(header_value or "").strip()
    if not raw:
        return ""
    raw = raw.replace("\r", "").replace("\n", "")
    safe_parts = []
    for part in raw.split(";"):
        token = part.strip()
        if not token or "=" not in token:
            continue
        name, value = token.split("=", 1)
        safe_name = re.sub(r"[^A-Za-z0-9!#$%&'*+.^_`|~-]", "", (name or "").strip())
        if not safe_name:
            continue
        safe_value_chars = []
        for ch in (value or "").strip():
            code = ord(ch)
            if ch == ";":
                continue
            if code < 32 or code == 127:
                continue
            safe_value_chars.append(ch)
        safe_value = "".join(safe_value_chars).strip()
        safe_parts.append(f"{safe_name}={safe_value}")
    cleaned = "; ".join(safe_parts).strip()
    if len(cleaned) > 4096:
        cleaned = cleaned[:4096]
    return cleaned


def build_cf_clearance_cookie_header(cookie_value):
    """Construit un header Cookie a partir d'un cf_clearance ou d'un header complet."""
    raw = str(cookie_value or "").strip()
    if not raw:
        return ""

    # Accepte soit:
    # - la valeur brute de cf_clearance
    # - un cookie unique de type name=value
    # - un header complet de type name=value; name2=value2
    if ";" in raw or re.match(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+\s*=", raw):
        return sanitize_cookie_header(raw)

    safe_cookie = sanitize_cookie_value(raw)
    if not safe_cookie:
        return ""
    return f"cf_clearance={safe_cookie}"


def resolve_cookie_header_for_url(url, cookie="", use_app_provider=True):
    """Retourne l'en-tête Cookie effectif pour une URL."""
    cookie_header = ""
    if use_app_provider:
        app = getattr(MangaApp, "current_instance", None)
        if app and hasattr(app, "get_cookie_header_for_url"):
            try:
                cookie_header = app.get_cookie_header_for_url(url, fallback_cookie=cookie)
            except Exception as exc:
                runtime_log(
                    f"Impossible de calculer l'en-tete Cookie: {exc}",
                    level="debug",
                    context={"action": "cookie_header"},
                )
                cookie_header = ""

    cookie_header = sanitize_cookie_header(cookie_header)
    if not cookie_header and cookie:
        cookie_header = build_cf_clearance_cookie_header(cookie)
    return sanitize_cookie_header(cookie_header)


def build_request_headers(url, cookie="", ua="", accept="*/*", referer_url=None, use_app_provider=True):
    """Construit les en-têtes HTTP communs de SushiDL."""
    headers = {
        "Accept": accept or "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": ua or DEFAULT_USER_AGENT,
        "Accept-Encoding": "gzip, deflate, br",
    }
    referer_root = referer_url or get_site_root_url(url)
    if referer_root:
        headers["Referer"] = referer_root

    cookie_header = resolve_cookie_header_for_url(url, cookie, use_app_provider=use_app_provider)
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def build_scanmanga_client_hints(ua=""):
    """Construit les Client Hints Chrome suffisants pour Scan-Manga."""
    safe_ua = (ua or DEFAULT_USER_AGENT).strip()
    version_match = re.search(r"\bChrome/(\d+)", safe_ua)
    chrome_major = version_match.group(1) if version_match else "127"
    return {
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua": f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
    }


def build_scanmanga_navigation_headers(url, cookie="", ua=""):
    """Headers de navigation requis par Scan-Manga avant les appels API lecteur."""
    safe_ua = (ua or DEFAULT_USER_AGENT).strip()
    headers = build_request_headers(
        url,
        cookie,
        safe_ua,
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        referer_url=None,
    )
    headers.update(build_scanmanga_client_hints(safe_ua))
    headers.update({
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    })
    return headers


def build_scanmanga_api_headers(chapter_url, cookie="", ua=""):
    """Headers fetch utilisés par le lecteur Scan-Manga pour bqj.scan-manga.com."""
    safe_ua = (ua or DEFAULT_USER_AGENT).strip()
    headers = build_request_headers(
        "https://bqj.scan-manga.com/",
        cookie,
        safe_ua,
        accept="*/*",
        referer_url="https://www.scan-manga.com/",
        use_app_provider=False,
    )
    headers.update(build_scanmanga_client_hints(safe_ua))
    headers.update({
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://www.scan-manga.com",
        "source": (chapter_url or "").strip(),
        "token": "yf",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    return headers


def build_scanmanga_image_headers(image_url, chapter_url, cookie="", ua=""):
    """Headers proches de ceux du navigateur pour les images data.scan-manga.com."""
    safe_ua = (ua or DEFAULT_USER_AGENT).strip()
    headers = build_request_headers(
        image_url,
        cookie,
        safe_ua,
        accept="*/*",
        referer_url=(chapter_url or "https://www.scan-manga.com/"),
        use_app_provider=False,
    )
    headers.update(build_scanmanga_client_hints(safe_ua))
    return headers


def new_scanmanga_browser_state():
    return {"playwright": None, "browser": None, "context": None, "page": None, "ua": ""}


def dispose_scanmanga_browser_state(state):
    """Ferme un état Playwright depuis le thread qui le possède."""
    for key in ("page", "context", "browser"):
        item = state.get(key)
        if item is not None:
            try:
                item.close()
            except Exception:
                pass
            state[key] = None
    playwright_obj = state.get("playwright")
    if playwright_obj is not None:
        try:
            playwright_obj.stop()
        except Exception:
            pass
        state["playwright"] = None
    state["ua"] = ""


def close_scanmanga_browser_state():
    """Demande l'arrêt de la session navigateur Scan-Manga réutilisée."""
    global SCANMANGA_BROWSER_THREAD, SCANMANGA_BROWSER_TASKS
    with SCANMANGA_BROWSER_LOCK:
        tasks = SCANMANGA_BROWSER_TASKS
        thread = SCANMANGA_BROWSER_THREAD
        SCANMANGA_BROWSER_TASKS = None
        SCANMANGA_BROWSER_THREAD = None
    if tasks is not None:
        try:
            tasks.put({"action": "stop"})
        except Exception:
            pass
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        try:
            thread.join(timeout=5)
        except Exception:
            pass


def reset_scanmanga_browser_context(state):
    """Recycle le contexte Playwright Scan-Manga sans fermer le navigateur."""
    page = state.get("page")
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    context = state.get("context")
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    state["context"] = None
    state["page"] = None
    state["referer"] = ""


def get_scanmanga_browser_page(state, ua=""):
    """Retourne une page Chromium réutilisée pour le fallback image Scan-Manga."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ImageDownloadError(
            f"Fallback navigateur Scan-Manga indisponible: installe playwright ({exc}).",
            kind="blocked_or_retryable",
            phase="browser",
        )

    safe_ua = (ua or DEFAULT_USER_AGENT).strip()
    if state.get("playwright") is None:
        state["playwright"] = sync_playwright().start()
    if state.get("browser") is None:
        chromium = state["playwright"].chromium
        launch_options = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            state["browser"] = chromium.launch(channel="chrome", **launch_options)
        except Exception:
            state["browser"] = chromium.launch(**launch_options)
        runtime_log(
            "Playwright Scan-Manga: session navigateur initialisée.",
            level="info",
            context={"action": "playwright_session", "domain": "scanmanga"},
        )
    if state.get("context") is None or state.get("ua") != safe_ua:
        if state.get("context") is not None:
            try:
                state["context"].close()
            except Exception:
                pass
        state["context"] = state["browser"].new_context(
            user_agent=safe_ua,
            locale="fr-FR",
            viewport={"width": 1365, "height": 900},
        )
        state["context"].add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        state["page"] = None
        state["ua"] = safe_ua
    if state.get("page") is None or state["page"].is_closed():
        state["page"] = state["context"].new_page()
        state["reader_errors"] = []

        def remember_reader_error(error):
            message = normalize_metadata_text(str(error))
            if message:
                state.setdefault("reader_errors", []).append(message[:300])
                state["reader_errors"] = state["reader_errors"][-4:]

        state["page"].on("pageerror", remember_reader_error)
    return state["page"]


def fetch_scanmanga_image_with_context_request(state, img_url, referer_url):
    """Télécharge une image via l'API request Playwright du contexte navigateur."""
    context = state.get("context")
    if context is None:
        raise ImageDownloadError(
            "Contexte navigateur Scan-Manga indisponible.",
            kind="blocked_or_retryable",
            phase="browser",
        )
    response = context.request.get(
        normalize_image_url(img_url),
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": (referer_url or "https://www.scan-manga.com/").strip(),
        },
        timeout=30000,
    )
    raw = response.body()
    content_type = ""
    try:
        content_type = (response.headers or {}).get("content-type", "")
    except Exception:
        content_type = ""
    body = base64.b64encode(raw or b"").decode("ascii")
    return {
        "status": int(getattr(response, "status", 0) or 0),
        "contentType": content_type,
        "body": body,
    }


def accept_scanmanga_reader_warning(page):
    """Valide l'avertissement lecteur Scan-Manga quand il est présent."""
    try:
        return bool(
            page.evaluate(
                """
                () => {
                    let changed = false;
                    try {
                        const raw = localStorage.getItem("lelParametres") || "{}";
                        const params = JSON.parse(raw) || {};
                        if (typeof idm !== "undefined" && idm) {
                            const key = String(idm);
                            if (!Object.prototype.hasOwnProperty.call(params, key)) {
                                params[key] = "";
                                localStorage.setItem("lelParametres", JSON.stringify(params));
                                changed = true;
                            }
                        }
                    } catch (e) {}
                    const button = document.querySelector("button.oui-avertissement-btn");
                    if (button && button.offsetParent !== null) {
                        button.click();
                        changed = true;
                    }
                    return changed;
                }
                """
            )
        )
    except Exception:
        return False


def fetch_scanmanga_image_in_browser_state(state, img_url, referer_url, ua, cancel_event=None):
    normalized_url = normalize_image_url(img_url)
    safe_referer = (referer_url or "https://www.scan-manga.com/").strip()
    referer_key = safe_referer.split("#", 1)[0]
    if state.get("referer") and state.get("referer") != referer_key:
        reset_scanmanga_browser_context(state)
    last_error = None
    result = None
    for attempt in range(1, 4):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        page = get_scanmanga_browser_page(state, ua)
        current_url = (getattr(page, "url", "") or "").split("#", 1)[0]
        must_reload = attempt > 1 or current_url != referer_key
        if must_reload:
            runtime_log(
                "Playwright Scan-Manga: analyse du lecteur et préparation du contexte image.",
                level="info",
                context={"action": "playwright_reader", "domain": "scanmanga"},
            )
            page.goto(safe_referer, wait_until="domcontentloaded", timeout=30000)
            state["referer"] = referer_key
            warning_accepted = accept_scanmanga_reader_warning(page)
            if warning_accepted:
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    time.sleep(0.25)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            runtime_log(
                "Playwright Scan-Manga: récupération des images du chapitre en cours.",
                level="info",
                context={"action": "playwright_download", "domain": "scanmanga"},
            )
        try:
            result = page.evaluate(
                """
                async (url) => {
                    const response = await fetch(url, {
                        cache: "reload",
                        credentials: "omit",
                        referrerPolicy: "strict-origin-when-cross-origin"
                    });
                    const buffer = await response.arrayBuffer();
                    let binary = "";
                    const bytes = new Uint8Array(buffer);
                    const chunkSize = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                    }
                    return {
                        status: response.status,
                        contentType: response.headers.get("content-type") || "",
                        body: btoa(binary)
                    };
                }
                """,
                normalized_url,
            )
            if int((result or {}).get("status") or 0) == 200:
                return result
            last_error = f"HTTP {int((result or {}).get('status') or 0)}"
            try:
                request_result = fetch_scanmanga_image_with_context_request(state, normalized_url, safe_referer)
                if int((request_result or {}).get("status") or 0) == 200:
                    return request_result
                last_error = f"{last_error} | context request HTTP {int((request_result or {}).get('status') or 0)}"
                result = request_result
            except Exception as request_exc:
                last_error = f"{last_error} | context request: {request_exc}"
        except Exception as exc:
            result = None
            last_error = f"page fetch: {exc}"
            try:
                result = fetch_scanmanga_image_with_context_request(state, normalized_url, safe_referer)
                if int((result or {}).get("status") or 0) == 200:
                    return result
                last_error = f"{last_error} | context request HTTP {int((result or {}).get('status') or 0)}"
            except Exception as request_exc:
                result = None
                last_error = f"{last_error} | context request: {request_exc}"
        if attempt < 3:
            reset_scanmanga_browser_context(state)
            try:
                page.wait_for_timeout(350 * attempt)
            except Exception:
                time.sleep(0.35 * attempt)
    if result is not None:
        return result
    raise ImageDownloadError(
        f"Navigateur Scan-Manga échoué: {last_error or 'fetch impossible'}",
        kind="blocked_or_retryable",
        phase="browser",
    )


def scanmanga_browser_worker_loop(tasks):
    """Thread propriétaire de Playwright pour Scan-Manga."""
    state = new_scanmanga_browser_state()
    try:
        while True:
            task = tasks.get()
            if not isinstance(task, dict):
                continue
            if task.get("action") == "stop":
                break
            response = task.get("response")
            try:
                result = fetch_scanmanga_image_in_browser_state(
                    state,
                    task.get("url") or "",
                    task.get("referer") or "",
                    task.get("ua") or DEFAULT_USER_AGENT,
                )
                if response is not None:
                    response.put({"ok": True, "result": result})
            except Exception as exc:
                if response is not None:
                    response.put({"ok": False, "error": exc})
    finally:
        dispose_scanmanga_browser_state(state)


def get_scanmanga_browser_tasks():
    """Démarre au besoin l'unique thread navigateur Scan-Manga."""
    global SCANMANGA_BROWSER_THREAD, SCANMANGA_BROWSER_TASKS
    with SCANMANGA_BROWSER_LOCK:
        if SCANMANGA_BROWSER_THREAD is not None and SCANMANGA_BROWSER_THREAD.is_alive() and SCANMANGA_BROWSER_TASKS is not None:
            return SCANMANGA_BROWSER_TASKS
        SCANMANGA_BROWSER_TASKS = queue.Queue()
        SCANMANGA_BROWSER_THREAD = threading.Thread(
            target=scanmanga_browser_worker_loop,
            args=(SCANMANGA_BROWSER_TASKS,),
            daemon=True,
            name="scanmanga-browser",
        )
        SCANMANGA_BROWSER_THREAD.start()
        return SCANMANGA_BROWSER_TASKS


def fetch_scanmanga_image_with_browser(img_url, referer_url, ua, cancel_event=None):
    """Récupère une image via l'unique session navigateur Scan-Manga."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")
    response = queue.Queue(maxsize=1)
    get_scanmanga_browser_tasks().put(
        {
            "action": "fetch",
            "url": normalize_image_url(img_url),
            "referer": referer_url,
            "ua": ua,
            "response": response,
        }
    )
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        try:
            payload = response.get(timeout=0.2)
            break
        except queue.Empty:
            continue
    if payload.get("ok"):
        return payload.get("result") or {}
    error = payload.get("error")
    if isinstance(error, Exception):
        raise error
    raise ImageDownloadError(
        f"Navigateur Scan-Manga échoué: {error or 'fetch impossible'}",
        kind="blocked_or_retryable",
        phase="browser",
    )


def download_scanmanga_image_with_browser(img_url, filename, referer_url, ua, cancel_event=None):
    """Télécharge une image Scan-Manga depuis le contexte réel du lecteur."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")

    normalized_url = normalize_image_url(img_url)
    tmp_filename = f"{filename}.part-browser-{threading.get_ident()}"
    result = fetch_scanmanga_image_with_browser(normalized_url, referer_url, ua, cancel_event=cancel_event)

    status_code = int((result or {}).get("status") or 0)
    content_type = ((result or {}).get("contentType") or "").lower()
    if status_code != 200:
        raise ImageDownloadError(
            f"Fallback navigateur Scan-Manga HTTP {status_code}",
            status_code=status_code,
            kind=classify_download_failure(status_code, f"HTTP {status_code}"),
            phase="browser",
        )
    if content_type and "image" not in content_type:
        raise ImageDownloadError(
            f"Fallback navigateur Scan-Manga: contenu non image ({content_type or 'inconnu'})",
            kind="invalid_image",
            phase="browser",
        )

    raw = base64.b64decode((result or {}).get("body") or "")
    if _is_html_payload_start(raw):
        raise ImageDownloadError(
            "Fallback navigateur Scan-Manga: réponse HTML",
            kind="blocked_or_retryable",
            phase="browser",
        )
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    try:
        with open(tmp_filename, "wb") as handle:
            handle.write(raw)
        validate_image_file(tmp_filename)
        os.replace(tmp_filename, filename)
    except Exception:
        try:
            if os.path.exists(tmp_filename):
                os.remove(tmp_filename)
        except OSError:
            pass
        raise


def download_preview_image_with_browser(img_url, referer_url, ua, cancel_event=None):
    """Retourne une image PIL depuis le fallback navigateur Scan-Manga."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")

    normalized_url = normalize_image_url(img_url)
    result = fetch_scanmanga_image_with_browser(normalized_url, referer_url, ua, cancel_event=cancel_event)

    if result is None or int((result or {}).get("status") or 0) != 200:
        raise ImageDownloadError(
            "Preview Scan-Manga via navigateur impossible.",
            status_code=int((result or {}).get("status") or 0) if result else None,
            kind="blocked_or_retryable",
            phase="browser",
        )
    raw = base64.b64decode((result or {}).get("body") or "")
    if _is_html_payload_start(raw):
        raise ImageDownloadError("Preview Scan-Manga: réponse HTML", kind="blocked_or_retryable", phase="browser")
    with Image.open(BytesIO(raw)) as image:
        return image.convert("RGB")


def new_crunchy_browser_state():
    return {"playwright": None, "browser": None, "context": None, "page": None, "ua": "", "site": ""}


def dispose_crunchy_browser_state(state):
    for key in ("page", "context", "browser"):
        item = state.get(key)
        if item is not None:
            try:
                item.close()
            except Exception:
                pass
            state[key] = None
    playwright_obj = state.get("playwright")
    if playwright_obj is not None:
        try:
            playwright_obj.stop()
        except Exception:
            pass
        state["playwright"] = None
    state["ua"] = ""
    state["site"] = ""


def reset_crunchy_browser_context(state):
    page = state.get("page")
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    context = state.get("context")
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    state["context"] = None
    state["page"] = None
    state["site"] = ""


def get_crunchy_browser_page(state, url, ua=""):
    """Retourne une page Chromium réutilisée pour CrunchyScan et Scan-Hentai."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise AuthError(f"Lecteur navigateur indisponible: installe playwright ({exc}).")

    safe_ua = (ua or DEFAULT_USER_AGENT).strip()
    site = get_site_domain_key(url)
    if state.get("playwright") is None:
        state["playwright"] = sync_playwright().start()
    if state.get("browser") is None:
        chromium = state["playwright"].chromium
        launch_options = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            state["browser"] = chromium.launch(channel="chrome", **launch_options)
        except Exception:
            state["browser"] = chromium.launch(**launch_options)
        runtime_log(
            f"Playwright {site}: session navigateur initialisée.",
            level="info",
            context={"action": "playwright_session", "domain": site},
        )
    if state.get("context") is None or state.get("ua") != safe_ua or state.get("site") != site:
        if state.get("context") is not None:
            try:
                state["context"].close()
            except Exception:
                pass
        state["context"] = state["browser"].new_context(
            user_agent=safe_ua,
            locale="fr-FR",
            viewport={"width": 1280, "height": 1600},
        )
        state["context"].add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        state["page"] = None
        state["ua"] = safe_ua
        state["site"] = site
    if state.get("page") is None or state["page"].is_closed():
        state["page"] = state["context"].new_page()
        state["reader_errors"] = []

        def remember_reader_error(error):
            message = normalize_metadata_text(str(error))
            if message:
                state.setdefault("reader_errors", []).append(message[:300])
                state["reader_errors"] = state["reader_errors"][-4:]

        state["page"].on("pageerror", remember_reader_error)
    return state["page"]


def parse_cookie_header_for_playwright(cookie, host):
    safe_host = normalize_hostname(host)
    raw_cookie = re.sub(r"^\s*cookie\s*:\s*", "", cookie or "", flags=re.IGNORECASE)
    if raw_cookie and "=" not in raw_cookie:
        raw_cookie = f"cf_clearance={raw_cookie}"
    cookies = []
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": f".{safe_host}" if safe_host else "",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return [item for item in cookies if item.get("domain")]


def _fetch_crunchy_reader_blobs_once(state, link, cookie, ua, max_images=None, cancel_event=None):
    """Récupère les images blob du lecteur CrunchyScan/Scan-Hentai depuis une session navigateur unique."""
    chapter_url = normalize_image_url(link)
    page = get_crunchy_browser_page(state, chapter_url, ua)
    context = state.get("context")
    if context is not None and cookie:
        try:
            host = normalize_hostname(urlparse(chapter_url).hostname)
            context.add_cookies(parse_cookie_header_for_playwright(cookie, host))
        except Exception:
            pass
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")
    site = get_site_domain_key(chapter_url)
    state["reader_errors"] = []
    runtime_log(
        f"Playwright {site}: analyse du lecteur en cours.",
        level="info",
        context={"action": "playwright_reader", "domain": site},
    )
    response = page.goto(chapter_url, wait_until="domcontentloaded", timeout=25000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    try:
        for selector in (
            "button.oui-avertissement-btn",
            "button:has-text('OK')",
            "button:has-text('Accepter')",
            "button:has-text('Continuer')",
        ):
            try:
                page.locator(selector).first.click(timeout=900)
                break
            except Exception:
                continue
    except Exception:
        pass
    try:
        page.wait_for_selector("img.imageView, img[data-img]", timeout=45000)
    except Exception:
        pass

    challenge_state = page.evaluate(
        """
        () => {
            const text = (document.body && document.body.innerText || '').toLowerCase();
            const title = (document.title || '').toLowerCase();
            return {
                url: location.href,
                title,
                challenge: title.includes('just a moment') || title.includes('un instant') ||
                    text.includes('vérification de sécurité') || text.includes('verification de securite') ||
                    text.includes('cloudflare') && text.includes('ray id'),
                forbidden: text.includes('error 403') || text.includes('http 403')
            };
        }
        """
    )
    if isinstance(challenge_state, dict) and challenge_state.get("challenge"):
        raise AuthError(
            "Lecteur CrunchyScan/Scan-Hentai bloqué par Cloudflare. "
            "Renouvelle le cookie depuis une page chapitre /read/... avec le même User-Agent."
        )

    total = int(page.evaluate("() => document.querySelectorAll('img.imageView, img[data-img]').length") or 0)
    if total <= 0:
        diagnostic = page.evaluate(
            """
            () => ({
                title: document.title || '',
                url: location.href,
                text: (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 220),
                images: document.querySelectorAll('img').length,
                imageView: document.querySelectorAll('img.imageView').length,
                dataImg: document.querySelectorAll('img[data-img]').length,
                dataMeta: document.querySelectorAll('[data-meta]').length,
                buttons: Array.from(document.querySelectorAll('button')).slice(0, 5).map(button => (button.innerText || button.textContent || '').trim()).filter(Boolean)
            })
            """
        )
        status_code = ""
        try:
            status_code = str(response.status) if response is not None else ""
        except Exception:
            status_code = ""
        if isinstance(diagnostic, dict):
            details = (
                f"HTTP {status_code or '?'} | titre={diagnostic.get('title') or '?'} | "
                f"images={diagnostic.get('images', 0)} | data-meta={diagnostic.get('dataMeta', 0)} | "
                f"url={diagnostic.get('url') or chapter_url}"
            )
            excerpt = normalize_metadata_text(diagnostic.get("text") or "")
            if excerpt:
                details = f"{details} | extrait={excerpt}"
            raise ParseError(f"Aucune image lecteur CrunchyScan/Scan-Hentai détectée ({details}).")
        raise ParseError("Aucune image lecteur CrunchyScan/Scan-Hentai détectée.")
    try:
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('img.imageView, img[data-img]')).some(image => (image.currentSrc || image.src || '').startsWith('blob:'))",
            timeout=18000,
        )
    except Exception:
        ready_count = int(
            page.evaluate(
                "() => Array.from(document.querySelectorAll('img.imageView, img[data-img]')).filter(image => (image.currentSrc || image.src || '').startsWith('blob:')).length"
            ) or 0
        )
        errors = "; ".join(state.get("reader_errors") or [])
        suffix = f" | erreur lecteur={errors}" if errors else ""
        raise ParseError(
            f"Lecteur CrunchyScan/Scan-Hentai non initialisé: aucun blob créé ({ready_count}/{total} prêt).{suffix}"
        )
    limit = min(total, int(max_images or total))
    runtime_log(
        f"Playwright {site}: {total} image(s) détectée(s), récupération de {limit} page(s).",
        level="info",
        context={"action": "playwright_images", "domain": site},
    )
    blobs = []
    for index in range(limit):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        payload = page.evaluate(
            """
            async ({index}) => {
                const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
                const bytesToBase64 = (bytes) => {
                    let binary = '';
                    const chunkSize = 0x8000;
                    for (let j = 0; j < bytes.length; j += chunkSize) {
                        binary += String.fromCharCode(...bytes.subarray(j, j + chunkSize));
                    }
                    return btoa(binary);
                };
                const looksLikeImage = (bytes, contentType) => {
                    if ((contentType || '').toLowerCase().includes('image/')) return true;
                    return (bytes[0] === 0xff && bytes[1] === 0xd8) ||
                        (bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) ||
                        (bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46) ||
                        (bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46);
                };
                const imageToJpegBase64 = async (img) => {
                    for (let i = 0; i < 120; i++) {
                        if (img.complete && img.naturalWidth && img.naturalHeight) break;
                        await sleep(125);
                    }
                    const width = img.naturalWidth;
                    const height = img.naturalHeight;
                    if (!width || !height) return {ok: false, error: 'image non chargée'};
                    const canvas = document.createElement('canvas');
                    canvas.width = width;
                    canvas.height = height;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, width, height);
                    const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
                    const commaIndex = dataUrl.indexOf(',');
                    if (commaIndex < 0) return {ok: false, error: 'canvas invalide'};
                    return {ok: true, body: dataUrl.slice(commaIndex + 1), contentType: 'image/jpeg'};
                };
                const images = Array.from(document.querySelectorAll('img.imageView, img[data-img]'));
                const img = images[index];
                if (!img) return {ok: false, error: 'image introuvable'};

                // Le lecteur ne déchiffre les images lazy qu'une fois visibles. Un scroll
                // réel suivi de plusieurs tours de boucle est plus fiable qu'un simple wait.
                for (let round = 0; round < 3; round++) {
                    img.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'});
                    window.dispatchEvent(new Event('scroll'));
                    for (let i = 0; i < 40; i++) {
                        const src = img.currentSrc || img.getAttribute('src') || '';
                        if (src.startsWith('blob:')) {
                            try {
                                const response = await fetch(src);
                                const buffer = await response.arrayBuffer();
                                const bytes = new Uint8Array(buffer);
                                const contentType = response.headers.get('content-type') || '';
                                if (response.ok && bytes.byteLength > 128 && looksLikeImage(bytes, contentType)) {
                                    return {ok: true, body: bytesToBase64(bytes), contentType};
                                }
                            } catch (error) {
                                // Le canvas ci-dessous peut lire un blob affiché même si fetch(blob:)
                                // est refusé par le contexte de la page.
                            }
                            const canvasPayload = await imageToJpegBase64(img);
                            if (canvasPayload.ok) return canvasPayload;
                        }
                        await sleep(125);
                    }
                    await sleep(250);
                }
                const finalSrc = img.currentSrc || img.getAttribute('src') || '';
                if (finalSrc.startsWith('blob:')) {
                    const src = img.getAttribute('src') || '';
                    try {
                        const response = await fetch(src);
                        const bytes = new Uint8Array(await response.arrayBuffer());
                        const contentType = response.headers.get('content-type') || '';
                        if (response.ok && bytes.byteLength > 128 && looksLikeImage(bytes, contentType)) {
                            return {ok: true, body: bytesToBase64(bytes), contentType};
                        }
                    } catch (error) {
                        // Tentative canvas finale ci-dessous.
                    }
                    return await imageToJpegBase64(img);
                }
                return {ok: false, error: 'blob non chargé'};
            }
            """,
            {"index": index},
        )
        if not payload or not payload.get("ok"):
            raise ImageDownloadError(
                f"Lecteur navigateur: {payload.get('error') if isinstance(payload, dict) else 'blob indisponible'}",
                kind="blocked_or_retryable",
                phase="browser",
            )
        raw = base64.b64decode(payload.get("body") or "")
        if not raw or _is_html_payload_start(raw):
            raise ImageDownloadError("Lecteur navigateur: payload invalide", kind="invalid_image", phase="browser")
        blobs.append(raw)
    runtime_log(
        f"Playwright {site}: récupération terminée ({len(blobs)} image(s)).",
        level="success",
        context={"action": "playwright_images_done", "domain": site},
    )
    return blobs


def fetch_crunchy_reader_blobs_in_state(state, link, cookie, ua, max_images=None, cancel_event=None):
    """Récupère un chapitre, puis renouvelle une fois le contexte en cas d'état lecteur instable."""
    last_error = None
    for attempt in range(2):
        try:
            return _fetch_crunchy_reader_blobs_once(
                state,
                link,
                cookie,
                ua,
                max_images=max_images,
                cancel_event=cancel_event,
            )
        except (DownloadCancelled, AuthError):
            raise
        except Exception as exc:
            last_error = exc
            if attempt:
                break
            runtime_log(
                f"Playwright {get_site_domain_key(link)}: contexte lecteur réinitialisé après échec transitoire.",
                level="warning",
                context={"action": "playwright_retry", "domain": get_site_domain_key(link)},
            )
            reset_crunchy_browser_context(state)
            if interruptible_sleep(cancel_event, 0.35):
                raise DownloadCancelled("Téléchargement annulé.")
    raise last_error or ParseError("Lecteur CrunchyScan/Scan-Hentai indisponible.")


def crunchy_browser_worker_loop(tasks):
    state = new_crunchy_browser_state()
    try:
        while True:
            task = tasks.get()
            if not isinstance(task, dict):
                continue
            if task.get("action") == "stop":
                break
            response = task.get("response")
            try:
                result = fetch_crunchy_reader_blobs_in_state(
                    state,
                    task.get("link") or "",
                    task.get("cookie") or "",
                    task.get("ua") or DEFAULT_USER_AGENT,
                    max_images=task.get("max_images"),
                )
                if response is not None:
                    response.put({"ok": True, "result": result})
            except Exception as exc:
                if response is not None:
                    response.put({"ok": False, "error": exc})
    finally:
        dispose_crunchy_browser_state(state)


def get_crunchy_browser_tasks():
    global CRUNCHY_BROWSER_THREAD, CRUNCHY_BROWSER_TASKS
    with CRUNCHY_BROWSER_LOCK:
        if CRUNCHY_BROWSER_THREAD is not None and CRUNCHY_BROWSER_THREAD.is_alive() and CRUNCHY_BROWSER_TASKS is not None:
            return CRUNCHY_BROWSER_TASKS
        CRUNCHY_BROWSER_TASKS = queue.Queue()
        CRUNCHY_BROWSER_THREAD = threading.Thread(
            target=crunchy_browser_worker_loop,
            args=(CRUNCHY_BROWSER_TASKS,),
            daemon=True,
            name="crunchy-reader-browser",
        )
        CRUNCHY_BROWSER_THREAD.start()
        return CRUNCHY_BROWSER_TASKS


def fetch_crunchy_reader_images(link, cookie, ua, max_images=None, emit_logs=True, cancel_event=None):
    """Expose les blobs lecteur comme URLs temporaires pour le pipeline existant."""
    response = queue.Queue(maxsize=1)
    get_crunchy_browser_tasks().put(
        {
            "action": "fetch_reader",
            "link": link,
            "cookie": cookie,
            "ua": ua,
            "max_images": max_images,
            "response": response,
        }
    )
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Téléchargement annulé.")
        try:
            payload = response.get(timeout=0.2)
            break
        except queue.Empty:
            continue
    if not payload.get("ok"):
        error = payload.get("error")
        if isinstance(error, Exception):
            raise error
        raise ParseError(str(error or "Lecteur navigateur indisponible."))
    page_bytes = payload.get("result") or []
    key_payload = f"{link}|{len(page_bytes)}|{hashlib.sha1(b''.join(page_bytes[:1]) if page_bytes else b'').hexdigest()}"
    key = hashlib.sha1(key_payload.encode("utf-8", errors="replace")).hexdigest()[:20]
    store_text_page_bytes(key, page_bytes)
    urls = [f"{TEXT_PAGE_URL_PREFIX}{key}/{idx + 1}.jpg" for idx in range(len(page_bytes))]
    if emit_logs:
        runtime_log(
            f"{len(urls)} image(s) récupérée(s) via lecteur navigateur.",
            level="info",
            context={"action": "extract_images", "domain": get_site_domain_key(link)},
        )
    return urls


def make_request(url, cookie, ua):
    """Effectue une requête HTTP avec les cookies et l'user-agent appropriés."""
    if get_supported_site_from_url(url) == "scan-manga.com":
        return _http_get(url, headers=build_scanmanga_navigation_headers(url, cookie, ua), timeout=20)
    headers = build_request_headers(url, cookie, ua)
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
    probe_urls = {
        "fr": "https://sushiscan.fr/catalogue/one-piece/",
        "net": "https://sushiscan.net/catalogue/one-piece/",
        "origines": "https://mangas-origines.fr/oeuvre/826-solo-leveling/",
        "hentai": "https://hentai-origines.fr/manga/stop-smoking/",
        "toonfr": "https://toonfr.com/webtoon/ma-brute/",
        "ortega": "https://ortegascans.fr/serie/moby-dick",
        "hentaizone": "https://hentaizone.xyz/manga/stepmothers-friends/",
        "scanmanga": "https://www.scan-manga.com/16363/Death-Penalty.html",
        "crunchyscan": "https://crunchyscan.fr/lecture-en-ligne/hajime-no-ippo",
        "scanhentai": "https://scan-hentai.net/lecture-en-ligne/even-a-hopeless-romantic-wants-to-be-loved",
    }
    if domain not in probe_urls:
        return result

    test_url = (probe_url or probe_urls.get(domain) or "").strip()
    if not test_url:
        return result

    expected_site = get_supported_site_from_host(urlparse(test_url).hostname or "")
    if not expected_site:
        expected_site = get_supported_site_from_url(probe_urls.get(domain, ""))
    if expected_site:
        current_site = get_supported_site_from_url(test_url)
        if current_site != expected_site:
            test_url = probe_urls.get(domain, test_url)

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
                "reading-content",
                "listing-chapters_wrap",
                "wp-manga-chapter",
                "toonfr",
                "hentaizone",
                "scan-manga",
                "crunchyscan",
                "scanhentai",
                "chapter-link",
                "manga_cover",
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


def remove_tree_safely(folder_path, expected_parent=None):
    """Supprime un dossier uniquement s'il correspond au dossier attendu."""
    target = Path(folder_path).resolve(strict=False)
    parent = Path(expected_parent).resolve(strict=False) if expected_parent else target.parent
    if not str(target) or target == parent or target.parent != parent:
        raise ValueError(f"Refus suppression dossier inattendu: {target}")
    if target.anchor and target == Path(target.anchor):
        raise ValueError(f"Refus suppression racine: {target}")
    if target.exists():
        shutil.rmtree(target)


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
    tmp_cbz_name = f"{cbz_name}.tmp"
    
    try:
        if os.path.exists(tmp_cbz_name):
            os.remove(tmp_cbz_name)
        # Création de l'archive ZIP
        with ZipFile(tmp_cbz_name, "w") as cbz:
            for root, _, files in os.walk(folder_path):
                for file in sorted(files):  # Tri alphabétique pour l'ordre des pages
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, folder_path)
                    cbz.write(full_path, arcname)
    except Exception as exc:
        runtime_log(f"Création CBZ impossible: {exc}", level="warning", context={"action": "archive_cbz"})
        try:
            if os.path.exists(tmp_cbz_name):
                os.remove(tmp_cbz_name)
        except OSError:
            pass
        return False
    
    # Vérification de l'intégrité de l'archive
    try:
        with ZipFile(tmp_cbz_name, "r") as test_zip:
            corrupt_member = test_zip.testzip()
            if corrupt_member:
                try:
                    os.remove(tmp_cbz_name)
                except OSError:
                    pass
                return False
            image_members = [
                name
                for name in test_zip.namelist()
                if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif"))
            ]
            if not image_members:
                try:
                    os.remove(tmp_cbz_name)
                except OSError:
                    pass
                return False
    except Exception as exc:
        runtime_log(f"Validation CBZ impossible: {exc}", level="warning", context={"action": "archive_cbz"})
        try:
            if os.path.exists(tmp_cbz_name):
                os.remove(tmp_cbz_name)
        except OSError:
            pass
        return False
    
    # Suppression du dossier original si l'archive est valide
    try:
        if os.path.exists(tmp_cbz_name) and os.path.getsize(tmp_cbz_name) > 10000:
            os.replace(tmp_cbz_name, cbz_name)
            if remove_source:
                remove_tree_safely(folder_path, expected_parent=parent_dir)
            return True
        if os.path.exists(tmp_cbz_name):
            os.remove(tmp_cbz_name)
    except Exception as exc:
        runtime_log(f"Finalisation CBZ impossible: {exc}", level="warning", context={"action": "archive_cbz"})
        try:
            if os.path.exists(tmp_cbz_name):
                os.remove(tmp_cbz_name)
        except OSError:
            pass
        return False
    return False


COMICINFO_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif")


def extract_comic_number(label):
    """Extrait le premier numero exploitable depuis un libelle de tome/chapitre."""
    value = repair_mojibake_text(label or "")
    match = re.search(r"(\d+(?:[.,]\d+)?)", value)
    return match.group(1).replace(",", ".") if match else ""


def count_downloaded_images(folder_path):
    """Compte uniquement les fichiers images presents dans le dossier telecharge."""
    total = 0
    for _, _, files in os.walk(folder_path):
        total += sum(1 for name in files if name.lower().endswith(COMICINFO_IMAGE_EXTENSIONS))
    return total


def is_chapter_label(label):
    """Retourne True si le libelle normalise correspond a un chapitre."""
    return normalize_tome_label(label).lower().startswith("chapitre ")


def build_high_res_cover_candidates(cover_url):
    """Retourne des variantes possibles de meilleure qualite, sans reseau."""
    safe_url = normalize_image_url((cover_url or "").strip())
    if not safe_url:
        return []
    candidates = [safe_url]
    parsed = urlparse(safe_url)
    path = parsed.path or ""
    variants = []
    size_suffix_path = re.sub(r"-(?:\d{2,5})x(?:\d{2,5})(\.[A-Za-z0-9]{3,5})$", r"\1", path)
    if size_suffix_path != path:
        variants.append(urlunparse((parsed.scheme, parsed.netloc, size_suffix_path, parsed.params, parsed.query, parsed.fragment)))
    # Ne jamais retirer les suffixes numeriques Scan-Manga: ils font partie de la vraie couverture.
    if normalize_hostname(parsed.hostname) != "static.scan-manga.com":
        numeric_suffix_path = re.sub(r"_\d{3,6}(\.[A-Za-z0-9]{3,5})$", r"\1", path)
        if numeric_suffix_path != path:
            variants.append(urlunparse((parsed.scheme, parsed.netloc, numeric_suffix_path, parsed.params, parsed.query, parsed.fragment)))
    for variant in variants:
        normalized = normalize_image_url(variant)
        if normalized and normalized not in candidates:
            candidates.insert(0, normalized)
    return candidates


def robust_download_cover_best(cover_url, cookie, ua, referer_url=None, max_try=2, delay=1):
    """Telecharge la meilleure variante de couverture disponible, avec fallback."""
    last_exc = None
    base_referer = referer_url or get_site_root_url(cover_url) or cover_url
    best = None
    for candidate in build_high_res_cover_candidates(cover_url):
        headers = build_request_headers(
            candidate,
            cookie,
            ua or DEFAULT_USER_AGENT,
            accept="image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
            referer_url=base_referer,
        )
        try:
            raw = robust_download_image(candidate, headers, max_try=max_try, delay=delay)
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
            area = max(0, int(width or 0) * int(height or 0))
            if best is None or area > best[0]:
                best = (area, candidate, raw)
        except Exception as exc:
            last_exc = exc
            continue
    if best is not None:
        return best[1], best[2]
    if last_exc:
        raise last_exc
    raise ImageDownloadError("Couverture introuvable.", kind="missing", phase="cover")


def get_archive_label_for_link(label, link, metadata_by_url=None):
    """Retourne le libellé à utiliser pour le nom du CBZ."""
    metadata = {}
    if isinstance(metadata_by_url, dict):
        metadata = metadata_by_url.get((link or "").strip()) or {}
    archive_label = (metadata.get("archive_label") or "").strip() if isinstance(metadata, dict) else ""
    return archive_label or label


COMICINFO_SOURCE_LABELS = {
    "fr": "sushiscan.fr",
    "net": "sushiscan.net",
    "origines": "mangas-origines.fr",
    "hentai": "hentai-origines.fr",
    "toonfr": "toonfr.com",
    "ortega": "ortegascans.fr",
    "hentaizone": "hentaizone.xyz",
    "scanmanga": "scan-manga.com",
    "crunchyscan": "crunchyscan.fr",
    "scanhentai": "scan-hentai.net",
}


def comicinfo_source_label(value):
    """Retourne une source lisible pour ComicInfo, jamais un simple alias cookie."""
    raw = normalize_metadata_text(value)
    if not raw:
        return ""
    raw = raw.strip()
    lowered = raw.lower().lstrip(".")
    return COMICINFO_SOURCE_LABELS.get(lowered, lowered if "." in lowered else raw)


def comicinfo_source_label_from_url(url):
    """Deduit le domaine complet a stocker comme source ComicInfo."""
    safe_url = (url or "").strip()
    cookie_domain = get_cookie_domain_from_url(safe_url)
    if cookie_domain:
        return comicinfo_source_label(cookie_domain)
    return comicinfo_source_label(normalize_hostname(urlparse(safe_url).hostname))


def build_comicinfo_xml(
    series,
    volume_label,
    page_count=0,
    total_count=None,
    web_url="",
    source_domain="",
    notes="",
    series_metadata=None,
):
    """Construit un ComicInfo.xml compatible Komga/ComicRack avec les metadonnees disponibles."""
    series_metadata = dict(series_metadata or {})
    series_name = repair_mojibake_text(series or "").strip() or APP_NAME
    series_name = first_metadata_value(series_metadata.get("series")) or series_name
    chapter_title = repair_mojibake_text(normalize_tome_label(volume_label) or "").strip()
    source_domain = comicinfo_source_label(source_domain)
    publisher = first_metadata_value(series_metadata.get("publisher"))
    publisher = publisher if publisher and publisher not in COMICINFO_SOURCE_LABELS else comicinfo_source_label(publisher)
    publisher = publisher or source_domain
    tags = metadata_join(series_metadata.get("tags") or [source_domain, APP_NAME])
    if not tags:
        tags = ", ".join(filter(None, [source_domain, APP_NAME]))
    root = ET.Element("ComicInfo")

    fields = {
        "Series": series_name,
        "Title": chapter_title,
        "Number": extract_comic_number(chapter_title),
        "Count": str(total_count) if total_count else "",
        "PageCount": str(page_count) if page_count else "",
        "Summary": first_metadata_value(series_metadata.get("summary")),
        "Year": first_metadata_value(series_metadata.get("year")),
        "Month": first_metadata_value(series_metadata.get("month")),
        "Day": first_metadata_value(series_metadata.get("day")),
        "Writer": metadata_join(series_metadata.get("writer")),
        "Penciller": metadata_join(series_metadata.get("penciller")),
        "Inker": metadata_join(series_metadata.get("inker")),
        "Colorist": metadata_join(series_metadata.get("colorist")),
        "Letterer": metadata_join(series_metadata.get("letterer")),
        "CoverArtist": metadata_join(series_metadata.get("cover_artist")),
        "Editor": metadata_join(series_metadata.get("editor")),
        "Translator": metadata_join(series_metadata.get("translator")),
        "Genre": metadata_join(series_metadata.get("genre")),
        "LanguageISO": first_metadata_value(series_metadata.get("language_iso")) or "fr",
        "Manga": first_metadata_value(series_metadata.get("manga")) or "YesAndRightToLeft",
        "Web": (web_url or first_metadata_value(series_metadata.get("web"))).strip(),
        "Publisher": publisher,
        "Tags": tags,
        "ScanInformation": f"Generated by {APP_NAME} v{APP_VERSION}",
        "Notes": (notes or "").strip(),
    }
    for tag, value in fields.items():
        if value:
            ET.SubElement(root, tag).text = str(value)

    try:
        ET.indent(root, space="  ")
    except Exception:
        pass
    return ET.ElementTree(root)


def write_comicinfo_xml(
    folder_path,
    series,
    volume_label,
    page_count=0,
    total_count=None,
    web_url="",
    source_domain="",
    notes="",
    series_metadata=None,
):
    """Ecrit ComicInfo.xml dans le dossier qui sera archive en CBZ."""
    xml_path = os.path.join(folder_path, "ComicInfo.xml")
    tree = build_comicinfo_xml(
        series=series,
        volume_label=volume_label,
        page_count=page_count,
        total_count=total_count,
        web_url=web_url,
        source_domain=source_domain,
        notes=notes,
        series_metadata=series_metadata,
    )
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)
    return xml_path


def write_download_report(folder_path, tome_label, image_urls, failed_downloads):
    """Ecrit un rapport lisible dans le dossier archive quand des pages manquent."""
    failures = list(failed_downloads or [])
    if not failures:
        return None
    report_path = os.path.join(folder_path, "SushiDL_report.txt")
    lines = [
        f"Rapport SushiDL - {tome_label}",
        f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Images attendues: {len(image_urls or [])}",
        f"Images manquantes ou invalides: {len(failures)}",
        "",
        "Le CBZ a ete genere avec les pages disponibles.",
        "Les entrees ci-dessous indiquent les images que SushiDL n'a pas pu recuperer.",
        "",
    ]
    for idx, failure in enumerate(failures, start=1):
        if isinstance(failure, dict):
            url = failure.get("url") or failure.get("image_url") or ""
            reason = failure.get("reason") or failure.get("error") or "Erreur inconnue"
            status_code = failure.get("status_code")
        else:
            url = ""
            reason = str(failure or "Erreur inconnue")
            status_code = None
        http_part = f"HTTP {status_code} - " if status_code not in (None, "") else ""
        lines.append(f"{idx}. {http_part}{reason}")
        if url:
            lines.append(f"   URL: {url}")
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    return report_path


def write_chapter_cover_page(folder_path, cover_url, cookie, ua, referer_url=None, webp2jpg_enabled=True):
    """Ajoute la couverture en page 000_cover.jpg pour les CBZ de chapitres."""
    safe_cover_url = normalize_image_url(cover_url)
    if not safe_cover_url:
        return ""
    cover_path = os.path.join(folder_path, "000_cover.jpg")
    if os.path.exists(cover_path):
        try:
            if os.path.getsize(cover_path) > 1024:
                return cover_path
        except OSError:
            pass

    _used_cover_url, raw = robust_download_cover_best(
        safe_cover_url,
        cookie,
        (ua or DEFAULT_USER_AGENT).strip(),
        referer_url=referer_url or get_site_root_url(safe_cover_url) or safe_cover_url,
        max_try=2,
        delay=1,
    )
    image = Image.open(BytesIO(raw))
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        bg.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
        image = bg
    else:
        image = image.convert("RGB")
    image.save(cover_path, "JPEG", quality=95, optimize=True)
    return cover_path


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

    if is_text_page_url(normalized_url):
        filename = os.path.join(folder, f"{str(i + 1).zfill(number_len)}.jpg")
        tmp_filename = f"{filename}.part-text-{threading.get_ident()}"
        try:
            raw = get_text_page_bytes(normalized_url)
            if not raw:
                raise ImageDownloadError(
                    "Page texte générée introuvable en cache.",
                    kind="missing",
                    phase="text-page",
                )
            os.makedirs(folder, exist_ok=True)
            with open(tmp_filename, "wb") as out:
                out.write(raw)
            validate_image_file(tmp_filename)
            os.replace(tmp_filename, filename)
            if progress_callback:
                progress_callback(i + 1)
            return
        except ImageDownloadError as exc:
            register_failure(exc.kind, str(exc), status_code=exc.status_code)
            return
        except Exception as exc:
            register_failure("retryable", str(exc))
            runtime_log(
                f"Ecriture page texte Scan-Manga échouée: {exc}",
                level="warning",
                context={"action": "download_text_page", "url": normalized_url},
            )
            return
        finally:
            try:
                if os.path.exists(tmp_filename):
                    os.remove(tmp_filename)
            except OSError:
                pass

    # Configuration des en-têtes HTTP
    referer = referer_url or get_site_root_url(normalized_url) or "https://sushiscan.net/"
    if normalize_hostname(urlparse(normalized_url).hostname) in SCANMANGA_IMAGE_HOSTS:
        headers = build_scanmanga_image_headers(normalized_url, referer, "", ua)
    else:
        headers = build_request_headers(
            normalized_url,
            cookie,
            ua,
            accept="image/webp,image/jpeg,image/png,*/*;q=0.8",
            referer_url=referer,
        )

    # Détermination de l'extension et du nom de fichier
    parsed_path = (urlparse(normalized_url).path or "").lower()
    is_scanmanga_cdn_image = normalize_hostname(urlparse(normalized_url).hostname) in SCANMANGA_IMAGE_HOSTS
    ext = parsed_path.rsplit(".", 1)[-1] if "." in parsed_path else "jpg"
    if ext not in {"jpg", "jpeg", "png", "webp", "avif"}:
        ext = "jpg"
    filename = os.path.join(folder, f"{str(i + 1).zfill(number_len)}.{ext}")

    if is_scanmanga_cdn_image:
        try:
            download_scanmanga_image_with_browser(
                normalized_url,
                filename,
                referer,
                ua,
                cancel_event=cancel_event,
            )
            if webp2jpg_enabled and filename.lower().endswith(".webp"):
                try:
                    with Image.open(filename) as source_image:
                        img = source_image.convert("RGB")
                    new_path = filename[:-5] + ".jpg"
                    img.save(new_path, "JPEG", quality=90)
                    os.remove(filename)
                    filename = new_path
                except Exception as conv_e:
                    runtime_log(f"Erreur conversion WebP->JPG: {conv_e}", level="warning", context={"action": "webp2jpg"})
            if progress_callback:
                progress_callback(i + 1)
            return
        except DownloadCancelled:
            register_failure("cancelled", "Annulation demandée pendant téléchargement navigateur Scan-Manga.")
            return
        except ImageDownloadError as browser_exc:
            register_failure(browser_exc.kind, str(browser_exc), status_code=browser_exc.status_code)
            runtime_log(
                f"Téléchargement navigateur Scan-Manga échoué: {browser_exc}",
                level="warning",
                context={"action": "download", "url": normalized_url},
            )
            return
        except Exception as browser_exc:
            status_code = get_status_code_from_exception(browser_exc)
            kind = classify_download_failure(status_code, str(browser_exc))
            register_failure(kind, str(browser_exc), status_code=status_code)
            runtime_log(
                f"Téléchargement navigateur Scan-Manga échoué: {browser_exc}",
                level="warning",
                context={"action": "download", "url": normalized_url},
            )
            return

    # Téléchargement direct prioritaire, en flux disque pour limiter la memoire.
    try:
        download_image_to_file(
            normalized_url,
            filename,
            headers,
            cancel_event=cancel_event,
        )

        # Conversion WebP vers JPG si activée
        if webp2jpg_enabled and filename.lower().endswith(".webp"):
            try:
                with Image.open(filename) as source_image:
                    img = source_image.convert("RGB")
                new_path = filename[:-5] + ".jpg"
                img.save(new_path, "JPEG", quality=90)
                os.remove(filename)
                filename = new_path
            except Exception as conv_e:
                runtime_log(f"Erreur conversion WebP->JPG: {conv_e}", level="warning", context={"action": "webp2jpg"})

        # Mise à jour de la progression
        if progress_callback:
            progress_callback(i + 1)
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
        if e.kind == "invalid_image":
            register_failure("invalid_image", str(e), status_code=e.status_code)
            runtime_log(
                f"Image ignorée car invalide/non reconnue: {normalized_url}",
                level="warning",
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


def normalize_manga_title_case(title):
    """Force au minimum une majuscule initiale si le titre n'en contient aucune."""
    normalized_title = repair_mojibake_text(str(title or "").strip())
    if not normalized_title:
        return ""
    if any(char.isalpha() and char.isupper() for char in normalized_title):
        return normalized_title
    title_chars = list(normalized_title)
    for index, char in enumerate(title_chars):
        if char.isalpha():
            title_chars[index] = char.upper()
            break
    return "".join(title_chars)


def extract_manga_title_from_html(url, html_content):
    """Extrait un titre de manga/œuvre depuis le HTML source."""
    html_content = html_content or ""
    soup = BeautifulSoup(html_content, "html.parser")
    source_site = get_supported_site_from_url(url)
    if source_site == "scan-manga.com":
        title_tag = soup.find("title")
        if title_tag:
            title_text = normalize_metadata_text(title_tag.get_text(" ", strip=True))
            title_text = re.sub(r"\s*\|\s*Scan-Manga\s*$", "", title_text, flags=re.IGNORECASE).strip()
            title_text = re.sub(r"\s*\((?:Webtoon|Novel|Manga)\)\s*$", "", title_text, flags=re.IGNORECASE).strip()
            if " » " in title_text:
                title_text = title_text.split(" » ", 1)[0].strip()
            if title_text:
                return normalize_manga_title_case(title_text)
    if source_site in ("crunchyscan.fr", "scan-hentai.net"):
        for selector in ("h1", "meta[property='og:title']", "title"):
            node = soup.select_one(selector) if selector != "title" else soup.find("title")
            if not node:
                continue
            if node.name == "meta":
                title_text = normalize_metadata_text(node.get("content"))
            else:
                title_text = normalize_metadata_text(node.get_text(" ", strip=True))
            title_text = re.sub(r"^\s*Lire\s+", "", title_text, flags=re.IGNORECASE)
            title_text = re.sub(r"\s+en scan VF\s*/?\s*FR.*$", "", title_text, flags=re.IGNORECASE)
            title_text = re.sub(r"\s*\|\s*(?:Crunchyscan|ScanHentai)\s*$", "", title_text, flags=re.IGNORECASE)
            if " » " in title_text:
                title_text = title_text.split(" » ", 1)[0].strip()
            if title_text:
                return normalize_manga_title_case(title_text)

    title_tag = soup.select_one("h1.entry-title, .post-title h1, .summary__content h1, h1")
    if title_tag:
        title = title_tag.get_text(" ", strip=True)
        if title:
            return normalize_manga_title_case(title)
    parsed_title = parse_lr(
        html_content, '<h1 class="entry-title" itemprop="name">', "</h1>", False
    )
    title = html.unescape(parsed_title) if parsed_title else ""
    if title:
        return normalize_manga_title_case(title)

    path = (urlparse(url).path or "").strip("/")
    if path:
        return normalize_manga_title_case(path.split("/")[-1].replace("-", " ").strip() or "Sans titre")
    return "Sans titre"


def normalize_metadata_text(value):
    """Nettoie une valeur de metadata sans inventer de contenu."""
    text = repair_mojibake_text(strip_html_tags(html.unescape(str(value or ""))))
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n:-")
    return text


def normalize_novel_text(value):
    """Nettoie un texte Novel sans supprimer sa ponctuation source."""
    text = repair_mojibake_text(strip_html_tags(html.unescape(str(value or ""))))
    return re.sub(r"\s+", " ", text).strip()


def split_metadata_values(value):
    """Decoupe une valeur auteur/genre tout en gardant l'ordre source."""
    text = normalize_metadata_text(value)
    if not text:
        return []
    parts = re.split(r"\s*(?:,|;|\||/|\bet\b|\band\b)\s*", text, flags=re.IGNORECASE)
    seen = set()
    values = []
    for part in parts:
        cleaned = normalize_metadata_text(part)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(cleaned)
    return values


def metadata_join(values):
    """Formate une liste de metadata pour ComicInfo.xml."""
    if values is None:
        return ""
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    flattened = []
    for value in raw_values:
        if isinstance(value, dict):
            for key in ("name", "title", "label", "value"):
                if value.get(key):
                    flattened.append(value.get(key))
                    break
        else:
            flattened.append(value)
    return ", ".join(split_metadata_values(", ".join(str(value or "") for value in flattened)))


def first_metadata_value(values):
    if isinstance(values, (list, tuple, set)):
        for value in values:
            cleaned = normalize_metadata_text(value)
            if cleaned:
                return cleaned
        return ""
    return normalize_metadata_text(values)


def extract_year_month_day(value):
    text = normalize_metadata_text(value)
    if not text:
        return "", "", ""
    iso_match = re.search(r"\b(19\d{2}|20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b", text)
    if iso_match:
        return iso_match.group(1), str(int(iso_match.group(2))), str(int(iso_match.group(3)))
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return (year_match.group(1), "", "") if year_match else ("", "", "")


def extract_meta_content(soup, *names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag:
            content = normalize_metadata_text(tag.get("content"))
            if content:
                return content
    return ""


def collect_metadata_links(soup, selectors):
    values = []
    for selector in selectors:
        for node in soup.select(selector):
            text = normalize_metadata_text(node.get_text(" ", strip=True))
            if text:
                values.append(text)
    return split_metadata_values(", ".join(values))


def is_summary_candidate(text):
    """Filtre les blocs qui ressemblent a une description, pas a une table de details."""
    cleaned = normalize_metadata_text(text)
    if len(cleaned) < 80:
        return False
    normalized = unicodedata.normalize("NFKD", cleaned.lower()).encode("ascii", "ignore").decode("ascii")
    metadata_markers = (
        "statut",
        "type",
        "annee",
        "auteur",
        "dessinateur",
        "genre",
        "chapitre",
        "tome",
    )
    marker_count = sum(1 for marker in metadata_markers if marker in normalized)
    return marker_count < 3


def select_best_summary(soup):
    """Recupere le resume visible le plus complet sans prendre les tables de metadata."""
    candidates = []
    selectors = (
        ".description-summary .summary__content",
        ".description-summary",
        ".manga-excerpt",
        ".summary-content > p",
        "div[itemprop='description']",
        ".entry-content > p",
        ".post-content > p",
        ".content > p",
    )
    for selector in selectors:
        for node in soup.select(selector):
            text = normalize_metadata_text(node.get_text(" ", strip=True))
            if is_summary_candidate(text):
                candidates.append(text)

    # Certains templates placent le resume dans une div voisine sans classe explicite.
    for paragraph in soup.find_all("p"):
        text = normalize_metadata_text(paragraph.get_text(" ", strip=True))
        if is_summary_candidate(text):
            candidates.append(text)

    if not candidates:
        return ""
    return max(candidates, key=len)


def add_table_fields(fields, soup):
    """Extrait les paires label/valeur depuis les tables HTML classiques."""
    for row in soup.select("tr"):
        cells = [cell for cell in row.find_all(("th", "td"), recursive=False)]
        if len(cells) < 2:
            cells = row.find_all(("th", "td"))
        if len(cells) >= 2:
            label = cells[0].get_text(" ", strip=True)
            value = cells[1].get_text(" ", strip=True)
            clean_label = normalize_metadata_text(label).lower()
            clean_value = normalize_metadata_text(value)
            if clean_label and clean_value:
                fields.setdefault(clean_label, []).append(clean_value)


def extract_scanmanga_series_metadata(soup):
    """Extrait les champs de la fiche technique Scan-Manga."""
    result = {
        "writer": "",
        "penciller": "",
        "translator": "",
        "genre": "",
        "publisher": "",
        "year": "",
        "month": "",
        "day": "",
        "status": "",
        "summary": "",
    }
    fiche = soup.select_one(".contenu_texte_fiche_technique, .content_texte_fiche_technique")
    if fiche is None:
        return result

    authors = []
    genres = []

    def scanmanga_direct_text(node):
        values = []
        for child in getattr(node, "contents", []) or []:
            if isinstance(child, str):
                text = normalize_metadata_text(child)
                if text:
                    values.append(text)
        return " ".join(values).strip()

    for li in fiche.select("li"):
        itemprop = normalize_metadata_text(li.get("itemprop")).lower()
        li_text = normalize_metadata_text(li.get_text(" ", strip=True))
        link_values = [normalize_metadata_text(a.get_text(" ", strip=True)) for a in li.select("a")]
        link_values = [value for value in link_values if value]

        if itemprop == "author":
            authors.extend(link_values or split_metadata_values(li_text))
        elif itemprop == "genre":
            if li_text:
                genres.append(li_text.replace(" - ", ", "))
        elif itemprop == "publisher":
            result["publisher"] = result["publisher"] or first_metadata_value(link_values or li_text)
        elif itemprop in ("translator", "creator"):
            result["translator"] = result["translator"] or metadata_join(link_values or li_text)
        elif itemprop in ("datepublished", "datecreated"):
            year, month, day = extract_year_month_day(li_text)
            result["year"] = result["year"] or year
            result["month"] = result["month"] or month
            result["day"] = result["day"] or day

        if re.fullmatch(r"(en cours|termin[eé]|terminée|pause|abandonn[eé]|complet)", li_text, flags=re.IGNORECASE):
            result["status"] = result["status"] or li_text
        if not result["year"]:
            year, month, day = extract_year_month_day(li_text)
            if year:
                result["year"], result["month"], result["day"] = year, month, day

    info_genres = []
    for node in fiche.select("a.infoBulle"):
        text = scanmanga_direct_text(node) or normalize_metadata_text(node.get_text(" ", strip=True))
        if text:
            info_genres.append(text.replace(" - ", ", "))
    genres.extend(info_genres)

    result["writer"] = metadata_join(authors)
    result["penciller"] = metadata_join(authors)
    result["genre"] = metadata_join(genres)

    summary_candidates = []
    for selector in (
        ".contenu_texte_fiche_description",
        ".content_texte_fiche_description",
        ".description",
        "[itemprop='description']",
    ):
        node = soup.select_one(selector)
        if node:
            text = normalize_metadata_text(node.get_text(" ", strip=True))
            if text:
                summary_candidates.append(text)
    if summary_candidates:
        result["summary"] = max(summary_candidates, key=len).rstrip(" £")
    return result


def parse_crunchy_family_chapters_from_html(url, soup, html_content=""):
    """Parse les catalogues CrunchyScan/Scan-Hentai (même moteur lecteur)."""
    source_site = get_supported_site_from_url(url)
    domain = get_cookie_domain_from_url(url)
    source_path = urlparse(url).path or ""
    source_match = re.search(r"/lecture-en-ligne/([^/?#]+)", source_path, flags=re.IGNORECASE)
    source_slug = source_match.group(1).strip().lower() if source_match else ""
    pairs = []
    metadata = {}
    seen = set()
    for anchor in soup.select("a.chapter-link[href], a[href*='/read/chapitre-'][href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        full_link = urljoin(url, href)
        if get_supported_site_from_url(full_link) != source_site:
            continue
        link_path = urlparse(full_link).path or ""
        if "/read/" not in link_path:
            continue
        link_match = re.search(r"/lecture-en-ligne/([^/?#]+)/read/", link_path, flags=re.IGNORECASE)
        link_slug = link_match.group(1).strip().lower() if link_match else ""
        if source_slug and link_slug and link_slug != source_slug:
            continue
        if full_link in seen:
            continue
        seen.add(full_link)
        raw_label = (
            normalize_metadata_text(anchor.get("title"))
            or normalize_metadata_text(anchor.get_text(" ", strip=True))
            or normalize_metadata_text(urlparse(full_link).path.rsplit("/", 1)[-1].replace("-", " "))
        )
        raw_label = re.sub(r"^\s*Lire\s+", "", raw_label, flags=re.IGNORECASE).strip()
        if (
            not re.search(r"(?i)\b(chapitre|chapter|episode|ep|tome|volume)\b", raw_label)
            or raw_label.lower() in {"lire", "liste animes", "liste anime", "liste"}
        ):
            path_label = normalize_metadata_text(urlparse(full_link).path.rsplit("/", 1)[-1].replace("-", " "))
            if re.search(r"(?i)\b(chapitre|chapter|episode|ep|tome|volume)\b", path_label):
                raw_label = path_label
        label = normalize_tome_label(raw_label)
        if not label:
            continue
        pairs.append((label, full_link))
        metadata[full_link] = {
            "archive_label": label,
            "group": "",
            "group_type": "chapter",
            "domain": domain,
        }
    return pairs, metadata


def crunchy_family_chapter_sort_key(item):
    """Trie les chapitres CrunchyScan/Scan-Hentai par numéro plutôt que par position HTML."""
    index, pair = item
    label, link = pair
    text = f"{label or ''} {urlparse(link or '').path.rsplit('/', 1)[-1].replace('-', ' ')}"
    match = re.search(r"(?i)\b(?:chapitre|chapter|episode|ep)\s*([0-9]+(?:[.,][0-9]+)?)", text)
    if match:
        try:
            return (0, float(match.group(1).replace(",", ".")), index)
        except Exception:
            pass
    if re.search(r"(?i)\bprologue\b", text):
        return (0, 0.0, index)
    return (1, index, 0)


def extract_crunchy_family_series_metadata(soup):
    """Extrait les blocs metadata du moteur CrunchyScan/Scan-Hentai."""
    result = {
        "writer": "",
        "penciller": "",
        "genre": "",
        "year": "",
        "month": "",
        "day": "",
        "status": "",
    }
    collected_genres = []

    for section in soup.select("[aria-labelledby]"):
        heading = section.select_one("h3")
        if not heading:
            continue
        label = normalize_metadata_text(heading.get_text(" ", strip=True))
        label_norm = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
        if not label_norm:
            continue

        values = []
        for anchor in section.select("a"):
            text = normalize_metadata_text(anchor.get_text(" ", strip=True))
            if text:
                values.append(text)
        if not values:
            for child in section.find_all(["p", "span"], recursive=True):
                if heading in child.parents:
                    continue
                text = normalize_metadata_text(child.get_text(" ", strip=True))
                if text and text not in values:
                    values.append(text)
        values = split_metadata_values(metadata_join(values))
        if not values:
            continue

        joined_values = metadata_join(values)
        has_author = any(token in label_norm for token in ("auteur", "author", "ecrivain", "writer", "scenariste", "scenario"))
        has_artist = any(token in label_norm for token in ("artiste", "artist", "dessinateur", "illustrateur", "illustrator", "penciller"))
        if has_author:
            result["writer"] = result["writer"] or joined_values
        if has_artist:
            result["penciller"] = result["penciller"] or joined_values
        if has_author or has_artist:
            continue
        if "status" in label_norm or "statut" in label_norm:
            result["status"] = first_metadata_value(values)
        elif "sortie" in label_norm or "date" in label_norm or "release" in label_norm:
            year, month, day = extract_year_month_day(first_metadata_value(values))
            result["year"] = result["year"] or year
            result["month"] = result["month"] or month
            result["day"] = result["day"] or day
        elif "genre" in label_norm:
            collected_genres.extend(values)
        elif "type" in label_norm or "categorie" in label_norm or "category" in label_norm:
            collected_genres.extend(values)

    result["genre"] = metadata_join(collected_genres)
    return result


def extract_series_metadata_from_html(url, html_content, title=""):
    """Extrait les metadonnees serie disponibles depuis la fiche catalogue."""
    html_content = html_content or ""
    soup = BeautifulSoup(html_content, "html.parser")
    source_domain = comicinfo_source_label_from_url(url)
    metadata = {
        "series": normalize_metadata_text(title) or extract_manga_title_from_html(url, html_content),
        "web": (url or "").strip(),
        "publisher": source_domain,
        "language_iso": "fr",
        "manga": "YesAndRightToLeft",
        "writer": "",
        "penciller": "",
        "translator": "",
        "genre": "",
        "summary": "",
        "year": "",
        "month": "",
        "day": "",
        "status": "",
        "tags": [],
        "cover_url": "",
    }

    summary = extract_meta_content(soup, "og:description", "description", "twitter:description")
    visible_summary = select_best_summary(soup)
    if visible_summary and len(visible_summary) > len(summary):
        summary = visible_summary
    metadata["summary"] = summary

    fields = {}

    def add_field(label, value):
        clean_label = normalize_metadata_text(label).lower()
        clean_value = normalize_metadata_text(value)
        if clean_label and clean_value:
            fields.setdefault(clean_label, []).append(clean_value)

    for item in soup.select(".post-content_item"):
        heading = item.select_one(".summary-heading, h5, h4, strong, b")
        content = item.select_one(".summary-content")
        if heading and content:
            add_field(heading.get_text(" ", strip=True), content.get_text(" ", strip=True))

    for row in soup.select("dl"):
        dts = row.find_all("dt")
        dds = row.find_all("dd")
        for dt, dd in zip(dts, dds):
            add_field(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
    add_table_fields(fields, soup)

    for label, value in fields.items():
        label_norm = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
        if any(token in label_norm for token in ("author", "auteur", "ecrivain", "writer")):
            metadata["writer"] = metadata_join([metadata["writer"], *value])
        elif any(token in label_norm for token in ("artist", "artiste", "dessinateur", "illustrateur", "penciller")):
            metadata["penciller"] = metadata_join([metadata["penciller"], *value])
        elif "traduct" in label_norm or "translator" in label_norm:
            metadata["translator"] = metadata_join([metadata["translator"], *value])
        elif "genre" in label_norm or "categorie" in label_norm or "category" in label_norm:
            metadata["genre"] = metadata_join([metadata["genre"], *value])
        elif "status" in label_norm or "statut" in label_norm:
            metadata["status"] = first_metadata_value(value)
        elif any(token in label_norm for token in ("year", "annee", "release", "sortie", "date")):
            year, month, day = extract_year_month_day(first_metadata_value(value))
            metadata["year"] = metadata["year"] or year
            metadata["month"] = metadata["month"] or month
            metadata["day"] = metadata["day"] or day

    genres = collect_metadata_links(
        soup,
        (
            ".genres-content a",
            ".summary-content .genres-content a",
            ".post-content_item .genres-content a",
            ".post-content_item .genres a",
            ".post-content_item a[href*='/genre/']",
            ".post-content_item a[href*='/manga-genre/']",
            ".post-content_item a[href*='/manga-genres/']",
            ".post-content_item a[href*='/webtoon-genre/']",
            ".manga-genres a",
            ".series-genres a",
            ".genre-list a",
            ".genres-list a",
            ".seriestugenre a[rel='tag']",
            ".seriestugenre a[href*='/genres/']",
            "a[href*='?tags=']",
        ),
    )
    if genres:
        metadata["genre"] = metadata_join([metadata["genre"], *genres])

    if get_supported_site_from_url(url) == "scan-manga.com":
        scanmanga_metadata = extract_scanmanga_series_metadata(soup)
        for key in ("writer", "penciller", "translator", "genre", "publisher", "year", "month", "day", "status", "summary"):
            value = scanmanga_metadata.get(key)
            if value:
                metadata[key] = value

    if get_supported_site_from_url(url) in ("crunchyscan.fr", "scan-hentai.net"):
        crunchy_metadata = extract_crunchy_family_series_metadata(soup)
        for key in ("writer", "penciller", "genre", "year", "month", "day", "status"):
            value = crunchy_metadata.get(key)
            if value:
                metadata[key] = value

    if not metadata["year"]:
        for candidate in (
            extract_meta_content(soup, "article:published_time", "datePublished", "pubdate"),
            extract_meta_content(soup, "article:modified_time", "dateModified"),
        ):
            year, month, day = extract_year_month_day(candidate)
            if year:
                metadata["year"], metadata["month"], metadata["day"] = year, month, day
                break

    initial_data = parse_ortega_initial_data(html_content)
    manga_data = initial_data.get("manga") if isinstance(initial_data, dict) else {}
    if isinstance(manga_data, dict):
        metadata["summary"] = metadata["summary"] or normalize_metadata_text(
            manga_data.get("description") or manga_data.get("synopsis") or manga_data.get("summary")
        )
        metadata["writer"] = metadata["writer"] or metadata_join(
            [
                manga_data.get("author") or manga_data.get("authors") or "",
                manga_data.get("writer") or "",
            ]
        )
        metadata["penciller"] = metadata["penciller"] or metadata_join(
            [
                manga_data.get("artist") or manga_data.get("artists") or "",
                manga_data.get("illustrator") or "",
            ]
        )
        metadata["genre"] = metadata["genre"] or metadata_join(
            manga_data.get("genres") or manga_data.get("categories") or ""
        )
        metadata["status"] = metadata["status"] or normalize_metadata_text(manga_data.get("status"))
        if not metadata["year"]:
            year, month, day = extract_year_month_day(
                manga_data.get("releaseDate") or manga_data.get("createdAt") or manga_data.get("updatedAt")
            )
            metadata["year"], metadata["month"], metadata["day"] = year, month, day

    tags = [source_domain, APP_NAME]
    if metadata["status"]:
        tags.append(metadata["status"])
    if metadata["genre"]:
        tags.extend(split_metadata_values(metadata["genre"]))
    metadata["tags"] = split_metadata_values(", ".join(tags))
    metadata["cover_url"] = extract_cover_url_from_html(url, html_content)
    return metadata


def extract_json_object_after_marker(text, marker):
    """Extrait un objet JSON/JS équilibré juste après un marqueur donné."""
    raw_text = str(text or "")
    if not raw_text or not marker:
        return ""
    marker_index = raw_text.find(marker)
    if marker_index < 0:
        return ""
    start_index = raw_text.find("{", marker_index + len(marker))
    if start_index < 0:
        return ""

    depth = 0
    escaped = False
    for index in range(start_index, len(raw_text)):
        char = raw_text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw_text[start_index : index + 1]
    return ""


def parse_ortega_initial_data(html_content):
    """Décode initialData embarqué dans le flux Next.js OrtegaScans."""
    raw_text = str(html_content or "")
    if not raw_text:
        return {}

    candidates = (
        ('initialData\\":', True),
        ('"initialData":', False),
    )
    for marker, escaped in candidates:
        raw_object = extract_json_object_after_marker(raw_text, marker)
        if not raw_object:
            continue
        decoded_object = raw_object
        if escaped:
            decoded_object = decoded_object.replace('\\"', '"')
        decoded_object = decoded_object.replace("\\/", "/")
        try:
            parsed = json.loads(decoded_object)
        except Exception:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("manga"), dict):
            return parsed
    return {}


def is_ortega_premium_chapter_locked(chapter):
    """Retourne True si le chapitre Ortega est réellement verrouillé premium."""
    if not bool((chapter or {}).get("isPremium")):
        return False
    premium_until = str((chapter or {}).get("premiumUntil") or "").strip()
    if premium_until.startswith("$D"):
        premium_until = premium_until[2:]
    if not premium_until:
        return True
    try:
        expiry = datetime.datetime.fromisoformat(premium_until.replace("Z", "+00:00"))
    except Exception:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    return expiry > now_utc


def parse_ortega_chapters_from_html(url, soup, source_slug, html_content=""):
    """Extrait les chapitres OrtegaScans et leur statut premium."""
    pairs = []
    metadata = {}
    seen_links = set()

    initial_data = parse_ortega_initial_data(html_content or str(soup))
    manga_data = initial_data.get("manga") if isinstance(initial_data, dict) else {}
    chapters_data = manga_data.get("chapters") if isinstance(manga_data, dict) else []
    if isinstance(chapters_data, list) and chapters_data:
        for chapter in chapters_data:
            if not isinstance(chapter, dict):
                continue
            chapter_number = chapter.get("number")
            if chapter_number in (None, ""):
                continue
            chapter_number_text = str(chapter_number).strip()
            if not chapter_number_text:
                continue
            full_link = urljoin(url, f"/serie/{source_slug}/chapter/{chapter_number_text}")
            if full_link in seen_links:
                continue
            seen_links.add(full_link)
            label = normalize_tome_label(f"Chapitre {chapter_number_text}")
            if not label:
                continue
            pairs.append((label, full_link))
            metadata[full_link] = {"premium": is_ortega_premium_chapter_locked(chapter)}
        if pairs:
            return pairs, metadata

    selector = f'a[href*="/serie/{source_slug}/chapter/"]'
    for anchor in soup.select(selector):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        full_link = urljoin(url, href)
        parsed_path = (urlparse(full_link).path or "").strip("/")
        match = re.match(
            rf"^serie/{re.escape(source_slug)}/chapter/([^/?#]+)/?$",
            parsed_path,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        raw_tokens = [repair_mojibake_text(token) for token in anchor.stripped_strings if str(token or "").strip()]
        if raw_tokens and raw_tokens[0].lower().startswith("commencer la lecture"):
            continue

        chapter_ref = (match.group(1) or "").strip()
        premium = any("PREMIUM" in token.upper() for token in raw_tokens)
        label = ""

        for idx, token in enumerate(raw_tokens):
            token_lower = token.lower()
            if token_lower in ("chapitre", "chapter", "ep", "episode"):
                if idx + 1 < len(raw_tokens):
                    number_token = raw_tokens[idx + 1].strip()
                    if number_token:
                        label = f"Chapitre {number_token}"
                        break

        if not label:
            chapter_number = chapter_ref.replace("-", " ").strip()
            label = f"Chapitre {chapter_number}" if chapter_number else ""

        clean_label = normalize_tome_label(label)
        if not clean_label or full_link in seen_links:
            continue
        seen_links.add(full_link)
        pairs.append((clean_label, full_link))
        metadata[full_link] = {"premium": premium}
    return pairs, metadata


def parse_scanmanga_chapters_from_html(url, soup, html_content=""):
    """Parse la liste statique des chapitres Scan-Manga depuis une page serie."""
    pairs = []
    metadata = {}
    seen_links = set()
    source_root = "https://www.scan-manga.com"
    chapter_pattern = re.compile(
        r"^https://www\.scan-manga\.com/lecture-en-ligne/[^\"'<>]+_(\d+)\.html$",
        flags=re.IGNORECASE,
    )

    chapter_anchors = soup.select('.chapitre_nom a[href*="/lecture-en-ligne/"]')
    if not chapter_anchors:
        chapter_anchors = soup.select('li.chapitre a[href*="/lecture-en-ligne/"]')
    if not chapter_anchors:
        chapter_anchors = soup.select('a[href*="/lecture-en-ligne/"]')

    def scanmanga_volume_label(anchor):
        volume_node = anchor.find_parent(class_=lambda value: value and "volume_manga" in str(value).split())
        if volume_node is None:
            return ""
        title_node = volume_node.select_one(".titre_volume_manga h3")
        raw_title = title_node.get_text(" ", strip=True) if title_node else ""
        return normalize_tome_label(raw_title)

    def scanmanga_chapter_text(anchor, fallback_label):
        chapter_node = anchor.find_parent(class_=lambda value: value and "chapitre_nom" in str(value).split())
        raw_text = chapter_node.get_text(" ", strip=True) if chapter_node else anchor.get_text(" ", strip=True)
        raw_text = normalize_metadata_text(raw_text)
        full_label = normalize_chapter_label_preserve_title(raw_text)
        if full_label:
            return full_label
        fallback = normalize_tome_label(fallback_label or raw_text)
        fallback_lower = fallback.lower()
        if raw_text and (fallback_lower in {"chapitre", "chapitre extra"} or fallback_lower.startswith("chapitre extra.")):
            return normalize_tome_label(raw_text)
        return fallback

    def scanmanga_display_label(anchor, full_link):
        path_name = urlparse(full_link).path.rsplit("/", 1)[-1]
        match_label = re.search(r"-Chapitre-([^_]+?)-FR_", path_name, flags=re.IGNORECASE)
        if match_label:
            url_label = normalize_tome_label(f"Chapitre {match_label.group(1).replace('-', '.')}")
        else:
            url_label = ""
        full_chapter_label = scanmanga_chapter_text(anchor, url_label)
        if not full_chapter_label:
            full_chapter_label = "Chap"
        short_chapter_label = full_chapter_label.split(" : ", 1)[0].strip() or full_chapter_label
        volume_label = scanmanga_volume_label(anchor)
        if volume_label:
            return (
                f"{volume_label} - {short_chapter_label}",
                f"{volume_label} - {full_chapter_label}",
                volume_label,
                short_chapter_label,
                full_chapter_label,
            )
        return short_chapter_label, full_chapter_label, "", short_chapter_label, full_chapter_label

    for a in chapter_anchors:
        href = (a.get("href") or "").strip()
        full_link = urljoin(source_root, href)
        if not chapter_pattern.match(full_link):
            continue
        if full_link in seen_links:
            continue
        seen_links.add(full_link)

        label, archive_label, volume_label, chapter_label, full_chapter_label = scanmanga_display_label(a, full_link)

        pairs.append((label, full_link))
        match_id = chapter_pattern.match(full_link)
        metadata[full_link] = {
            "chapter_id": int(match_id.group(1)) if match_id else None,
            "volume_label": volume_label,
            "chapter_label": chapter_label,
            "full_chapter_label": full_chapter_label,
            "archive_label": archive_label,
        }

    if not pairs:
        raw_links = re.findall(
            r"https://www\.scan-manga\.com/lecture-en-ligne/[^\"'<>]+_\d+\.html",
            html_content or "",
            flags=re.IGNORECASE,
        )
        for full_link in raw_links:
            if full_link in seen_links:
                continue
            seen_links.add(full_link)
            path_name = urlparse(full_link).path.rsplit("/", 1)[-1]
            match_label = re.search(r"-Chapitre-([^_]+?)-FR_", path_name, flags=re.IGNORECASE)
            label = normalize_tome_label(
                f"Chapitre {match_label.group(1).replace('-', '.')}" if match_label else "Chapitre"
            )
            match_id = chapter_pattern.match(full_link)
            pairs.append((label, full_link))
            metadata[full_link] = {"chapter_id": int(match_id.group(1)) if match_id else None}

    return pairs, metadata


def _scanmanga_base_convert_to_int(value, base_chars):
    total = 0
    for idx, char in enumerate(reversed(value or "")):
        char_value = base_chars.find(char)
        if char_value < 0:
            continue
        total += char_value * (len(base_chars) ** idx)
    return total


def decode_scanmanga_eval_script(script):
    """Decode l'obfuscateur simple utilisé autour de sml/sme/DataAPI."""
    match = re.search(
        r'eval\(function\s*\([^)]*\)\s*\{.*?\}\("([^"]+)",\s*(\d+),\s*"([^"]+)",\s*(\d+),\s*(\d+),\s*(\d+)\)\s*\)',
        script or "",
        flags=re.DOTALL,
    )
    if not match:
        return ""
    payload, _unused_w, q, offset, base, _unused_f = match.groups()
    try:
        offset = int(offset)
        base = int(base)
    except ValueError:
        return ""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"
    base_chars = alphabet[:base]
    separator = q[base] if len(q) > base else ""
    if not separator:
        return ""

    decoded = []
    index = 0
    while index < len(payload):
        chunk = []
        while index < len(payload) and payload[index] != separator:
            chunk.append(payload[index])
            index += 1
        index += 1
        token = "".join(chunk)
        if not token:
            continue
        for replacement_index, replacement_char in enumerate(q):
            token = token.replace(replacement_char, str(replacement_index))
        decoded.append(chr(_scanmanga_base_convert_to_int(token, base_chars) - offset))
    return "".join(decoded)


def extract_scanmanga_reader_vars(html_content):
    """Extrait idc/idm/sml/sme depuis une page lecteur Scan-Manga."""
    html_content = html_content or ""
    idc_match = re.search(r"const\s+idc\s*=\s*(\d+)\s*;", html_content)
    idm_match = re.search(r"const\s+idm\s*=\s*(\d+)\s*;", html_content)
    sml_match = re.search(r"var\s+sml\s*=\s*['\"]([^'\"]+)['\"]\s*;", html_content)
    sme_match = re.search(r"var\s+sme\s*=\s*['\"]([^'\"]+)['\"]\s*;", html_content)

    if (not sml_match or not sme_match) and "eval(function" in html_content:
        for script in re.findall(r"<script[^>]*>(.*?)</script>", html_content, flags=re.DOTALL | re.IGNORECASE):
            if "eval(function" not in script:
                continue
            decoded_script = decode_scanmanga_eval_script(script)
            if not decoded_script:
                continue
            sml_match = sml_match or re.search(r"var\s+sml\s*=\s*['\"]([^'\"]+)['\"]\s*;", decoded_script)
            sme_match = sme_match or re.search(r"var\s+sme\s*=\s*['\"]([^'\"]+)['\"]\s*;", decoded_script)
            if sml_match and sme_match:
                break

    if not idc_match or not sml_match or not sme_match:
        raise ParseError("Variables lecteur Scan-Manga introuvables (idc/sml/sme).")
    return {
        "idc": int(idc_match.group(1)),
        "idm": int(idm_match.group(1)) if idm_match else 0,
        "sml": sml_match.group(1),
        "sme": sme_match.group(1),
    }


def decode_scanmanga_data_api(payload, chapter_id):
    """Equivalent Python de DataAPI(D, idc)."""
    raw_text = (payload or "").strip()
    if not raw_text:
        raise ParseError("Réponse Scan-Manga vide.")
    raw = base64.b64decode(raw_text + "=" * (-len(raw_text) % 4))
    inflated = zlib.decompress(raw).decode("utf-8")
    suffix = format(int(chapter_id), "x")
    if inflated.endswith(suffix):
        inflated = inflated[: -len(suffix)]
    reversed_payload = inflated[::-1]
    decoded = base64.b64decode(reversed_payload + "=" * (-len(reversed_payload) % 4)).decode("utf-8")
    return json.loads(decoded)


def build_scanmanga_image_urls(data):
    """Transforme le JSON lecteur Scan-Manga en URLs d'images réelles."""
    if not isinstance(data, dict):
        return []
    host = (data.get("dN") or "").strip()
    serie = (data.get("s") or "").strip("/")
    volume = str(data.get("v") or "").strip("/")
    chapter = str(data.get("c") or "").strip("/")
    pages = data.get("p") or {}
    if not host or not serie or not volume or not chapter or not isinstance(pages, dict):
        return []

    def page_sort_key(item):
        key, _value = item
        try:
            return int(key)
        except (TypeError, ValueError):
            return str(key)

    images = []
    for _page, page_data in sorted(pages.items(), key=page_sort_key):
        if not isinstance(page_data, dict):
            continue
        filename = (page_data.get("f") or "").strip()
        ext = (page_data.get("e") or "").strip().lstrip(".")
        if not filename or not ext:
            continue
        images.append(f"https://{host}/{serie}/{volume}/{chapter}/{filename}.{ext}")
    return images


def extract_scanmanga_novel_chapter(html_content):
    """Extrait un chapitre texte Scan-Manga (Novel) depuis le lecteur HTML."""
    soup = BeautifulSoup(html_content or "", "html.parser")
    article = soup.select_one("article.aLN")
    content_node = soup.select_one(".ln_c_content")
    if not article or not content_node:
        return None

    title_node = article.select_one(".ln_c_title")
    title = normalize_novel_text(title_node.get_text(" ", strip=True) if title_node else "")
    blocks = []
    seen = set()

    def node_align(node):
        if not node:
            return "left"
        if getattr(node, "name", "") == "center":
            return "center"
        style = str(node.get("style") or "").lower() if hasattr(node, "get") else ""
        if "text-align" in style:
            if "center" in style:
                return "center"
            if "right" in style:
                return "right"
        parent = getattr(node, "parent", None)
        if getattr(parent, "name", "") == "center":
            return "center"
        return "left"

    def add_block(text, align="left"):
        text = normalize_novel_text(text)
        if not text:
            return
        dedupe_key = text.lower()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        blocks.append({"kind": "text", "text": text, "align": align if align in {"left", "center", "right"} else "left"})

    def add_spacer(height=24):
        blocks.append({"kind": "spacer", "height": max(8, min(80, int(height or 24)))})

    def image_source(node):
        if not node or not hasattr(node, "get"):
            return ""
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            value = str(node.get(attr) or "").strip()
            if value:
                return value
        srcset = str(node.get("srcset") or node.get("data-srcset") or "").strip()
        if srcset:
            first_candidate = srcset.split(",", 1)[0].strip()
            return first_candidate.split(" ", 1)[0].strip()
        return ""

    def add_image(node, align="center"):
        src = image_source(node)
        if not src:
            return
        alt = normalize_novel_text(node.get("alt", "") if hasattr(node, "get") else "")
        final_align = align if align in {"center", "right"} else "center"
        dedupe_key = f"image:{src}".lower()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        blocks.append(
            {
                "kind": "image",
                "src": src,
                "alt": alt,
                "align": final_align,
            }
        )

    for node in content_node.find_all(["center", "p", "h1", "h2", "h3", "blockquote", "li", "img", "br"], recursive=True):
        node_name = getattr(node, "name", None)
        if node_name == "br":
            add_spacer(18)
            continue
        if node_name == "center":
            for img in node.find_all("img", recursive=True):
                add_image(img, "center")
            add_block(node.get_text(" ", strip=True), "center")
            continue
        if node_name == "img":
            add_image(node, node_align(node))
            continue
        for img in node.find_all("img", recursive=True):
            add_image(img, node_align(node))
        text = node.get_text(" ", strip=True)
        if text:
            add_block(text, node_align(node))
        elif not node.find_all("img", recursive=True):
            add_spacer(18)

    if not blocks:
        raw_text = normalize_novel_text(content_node.get_text("\n", strip=True))
        blocks = [{"text": line.strip(), "align": "left"} for line in raw_text.splitlines() if line.strip()]

    total_text_len = sum(len(_text_block_text(block)) for block in blocks if _text_block_kind(block) == "text")
    image_count = sum(1 for block in blocks if _text_block_kind(block) == "image")
    if total_text_len < 80 and not image_count:
        return None
    return title or "Chapitre texte", blocks


def _load_text_render_font(size, bold=False):
    font_candidates = [
        r"C:\Windows\Fonts\DejaVuSerif-Bold.ttf" if bold else r"C:\Windows\Fonts\DejaVuSerif.ttf",
        r"C:\Windows\Fonts\NotoSerif-Bold.ttf" if bold else r"C:\Windows\Fonts\NotoSerif-Regular.ttf",
        r"C:\Windows\Fonts\georgiab.ttf" if bold else r"C:\Windows\Fonts\georgia.ttf",
        r"C:\Windows\Fonts\cambria.ttc",
        r"C:\Windows\Fonts\timesbd.ttf" if bold else r"C:\Windows\Fonts\times.ttf",
        r"C:\Windows\Fonts\seguisym.ttf",
        r"C:\Windows\Fonts\ARIALUNI.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for font_path in font_candidates:
        try:
            if font_path and os.path.exists(font_path):
                return ImageFont.truetype(font_path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load_text_symbol_font(size):
    font_candidates = [
        r"C:\Windows\Fonts\seguisym.ttf",
        r"C:\Windows\Fonts\SegoeIcons.ttf",
        r"C:\Windows\Fonts\ARIALUNI.ttf",
        r"C:\Windows\Fonts\DejaVuSans.ttf",
    ]
    for font_path in font_candidates:
        try:
            if font_path and os.path.exists(font_path):
                return ImageFont.truetype(font_path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _font_size(font):
    return int(getattr(font, "size", 30) or 30)


def _is_symbol_char(char):
    if not char:
        return False
    if char in "☆★◇◆○●◎◯□■△▲▽▼①②③④⑤⑥⑦⑧⑨⑩":
        return True
    return unicodedata.category(char) in {"So", "Sm"}


def _font_for_char(char, font):
    if _is_symbol_char(char):
        return _load_text_symbol_font(_font_size(font))
    return font


def _split_font_runs(text, font):
    runs = []
    current_font = None
    current_text = []
    for char in str(text or ""):
        char_font = _font_for_char(char, font)
        if current_font is not None and getattr(char_font, "path", None) == getattr(current_font, "path", None):
            current_text.append(char)
            continue
        if current_text:
            runs.append(("".join(current_text), current_font))
        current_font = char_font
        current_text = [char]
    if current_text:
        runs.append(("".join(current_text), current_font))
    return runs


def _text_bbox(draw, text, font):
    try:
        return draw.textbbox((0, 0), text, font=font)
    except Exception:
        width, height = draw.textsize(text, font=font)
        return (0, 0, width, height)


def _text_width(draw, text, font):
    total = 0
    for run_text, run_font in _split_font_runs(text, font):
        bbox = _text_bbox(draw, run_text, run_font)
        total += max(0, bbox[2] - bbox[0])
    return total


def _draw_text_with_fallback(draw, xy, text, fill, font):
    x, y = xy
    for run_text, run_font in _split_font_runs(text, font):
        draw.text((x, y), run_text, fill=fill, font=run_font)
        x += _text_width(draw, run_text, run_font)


def _wrap_text_for_width(draw, text, font, max_width):
    words = str(text or "").split()
    if not words:
        return [""]
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        lines.append(current)
        if _text_width(draw, word, font) <= max_width:
            current = word
            continue
        chunk = ""
        for char in word:
            candidate_chunk = f"{chunk}{char}"
            if not chunk or _text_width(draw, candidate_chunk, font) <= max_width:
                chunk = candidate_chunk
            else:
                lines.append(chunk)
                chunk = char
        current = chunk
    if current:
        lines.append(current)
    return lines


def _resolve_scanmanga_novel_image_url(src, source_url=""):
    value = str(src or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("data:image/"):
        return value
    source_text = str(source_url or "").strip()
    is_windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", source_text))
    if source_text and (is_windows_path or os.path.exists(source_text) or not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", source_text)):
        try:
            base_path = Path(source_text)
            base_dir = base_path.parent if base_path.suffix else base_path
            return str((base_dir / value).resolve())
        except Exception:
            pass
    return urljoin(source_url or "https://www.scan-manga.com/", value)


def _load_scanmanga_novel_image(src, source_url="", cookie="", ua=""):
    image_url = _resolve_scanmanga_novel_image_url(src, source_url)
    if not image_url:
        return None
    if image_url.startswith("data:image/"):
        header, _, payload = image_url.partition(",")
        if not payload:
            return None
        if ";base64" in header.lower():
            raw = base64.b64decode(payload)
        else:
            raw = unquote(payload).encode("latin-1", errors="ignore")
    else:
        if os.path.exists(image_url):
            raw = Path(image_url).read_bytes()
        else:
            parsed_image = urlparse(image_url)
            if parsed_image.scheme == "file":
                file_path = parsed_image.path
                if os.name == "nt" and re.match(r"^/[A-Za-z]:/", file_path):
                    file_path = file_path[1:]
                raw = Path(unquote(file_path)).read_bytes()
            elif not parsed_image.scheme and os.path.exists(image_url):
                raw = Path(image_url).read_bytes()
            else:
                image_host = normalize_hostname(parsed_image.hostname)
                if image_host in SCANMANGA_IMAGE_HOSTS:
                    result = fetch_scanmanga_image_with_browser(image_url, source_url, ua or DEFAULT_USER_AGENT)
                    raw = base64.b64decode((result or {}).get("body") or "")
                else:
                    headers = build_request_headers(
                        image_url,
                        cookie,
                        ua or DEFAULT_USER_AGENT,
                        accept="image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
                        referer_url=source_url or get_site_root_url(image_url) or "https://www.scan-manga.com/",
                    )
                    raw = robust_download_image(image_url, headers, max_try=2, delay=1)
    image = Image.open(BytesIO(raw))
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        bg = Image.new("RGB", image.size, (253, 250, 244))
        bg.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
        return bg
    return image.convert("RGB")


def _fit_image_to_page(image, max_width, max_height):
    if image is None:
        return None
    width, height = image.size
    if width <= 0 or height <= 0:
        return None
    scale = min(max_width / width, max_height / height, 1.0)
    target_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    if target_size == image.size:
        return image
    return image.resize(target_size, Image.Resampling.LANCZOS)


def render_scanmanga_novel_pages(title, paragraphs, source_url="", cookie="", ua="", max_pages=None):
    """Rend un chapitre Novel Scan-Manga en pages JPG utilisables par le pipeline CBZ."""
    clean_title = normalize_metadata_text(title or "Chapitre texte")
    clean_blocks = []
    for block in paragraphs or []:
        if _text_block_kind(block) == "image" and isinstance(block, dict):
            src = _resolve_scanmanga_novel_image_url(block.get("src", ""), source_url)
            if src:
                clean_blocks.append(
                    {
                        "kind": "image",
                        "src": src,
                        "alt": normalize_novel_text(block.get("alt", "")),
                        "align": _text_block_align(block) or "center",
                    }
                )
            continue
        if _text_block_kind(block) == "spacer" and isinstance(block, dict):
            try:
                spacer_height = int(block.get("height") or 24)
            except (TypeError, ValueError):
                spacer_height = 24
            clean_blocks.append({"kind": "spacer", "height": max(8, min(80, spacer_height))})
            continue
        text = _text_block_text(block)
        if text:
            clean_blocks.append({"kind": "text", "text": text, "align": _text_block_align(block)})
    while clean_blocks and clean_blocks[0].get("kind") == "spacer":
        clean_blocks.pop(0)
    while clean_blocks and clean_blocks[-1].get("kind") == "spacer":
        clean_blocks.pop()
    if not clean_blocks:
        return []

    width, height = 1200, 1700
    margin_x, margin_y = 90, 100
    background = (253, 250, 244)
    title_color = (38, 49, 64)
    text_color = (32, 32, 32)
    rule_color = (202, 190, 171)
    title_font = _load_text_render_font(42, bold=True)
    body_font = _load_text_render_font(30, bold=False)
    footer_font = _load_text_render_font(20, bold=False)
    max_text_width = width - (margin_x * 2)
    line_height = 42
    paragraph_gap = 24
    centered_paragraph_gap = 34
    image_gap = 34
    page_bytes = []
    page = None
    draw = None
    y = 0
    page_number = 0
    page_has_body = False
    try:
        page_limit = max(0, int(max_pages or 0))
    except (TypeError, ValueError):
        page_limit = 0

    def page_limit_reached():
        return bool(page_limit and len(page_bytes) >= page_limit)

    def new_page():
        nonlocal page, draw, y, page_number, page_has_body
        page_number += 1
        page = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(page)
        y = margin_y
        page_has_body = False
        if page_number == 1:
            title_lines = _wrap_text_for_width(draw, clean_title, title_font, max_text_width)
            for line in title_lines[:4]:
                _draw_text_with_fallback(draw, (margin_x, y), line, fill=title_color, font=title_font)
                y += 56
            draw.line((margin_x, y + 8, width - margin_x, y + 8), fill=rule_color, width=2)
            y += 48

    def save_page():
        if page is None or page_limit_reached():
            return
        footer = f"{page_number}"
        footer_width = _text_width(draw, footer, footer_font)
        _draw_text_with_fallback(draw, ((width - footer_width) / 2, height - 62), footer, fill=(110, 110, 110), font=footer_font)
        buffer = BytesIO()
        page.save(buffer, "JPEG", quality=92, optimize=True)
        page_bytes.append(buffer.getvalue())

    new_page()
    usable_bottom = height - 110
    for block in clean_blocks:
        if page_limit_reached():
            break
        if block["kind"] == "spacer":
            spacer_height = block["height"]
            if y + spacer_height > usable_bottom and page_has_body:
                save_page()
                if page_limit_reached():
                    break
                new_page()
            y += spacer_height
            continue
        if block["kind"] == "image":
            try:
                source_image = _load_scanmanga_novel_image(block["src"], source_url, cookie, ua)
                fitted = _fit_image_to_page(source_image, max_text_width, usable_bottom - margin_y)
                if fitted is None:
                    continue
            except Exception as exc:
                runtime_log(
                    f"Image Novel Scan-Manga ignorée: {exc}",
                    level="warning",
                    context={"action": "novel_image", "url": block.get("src", "")},
                )
                continue
            image_width, image_height = fitted.size
            if y + image_height + image_gap > usable_bottom and page_has_body:
                save_page()
                if page_limit_reached():
                    break
                new_page()
            if image_height > usable_bottom - margin_y:
                fitted = _fit_image_to_page(fitted, max_text_width, usable_bottom - margin_y)
                image_width, image_height = fitted.size
            align = block["align"]
            if align == "left" and block.get("explicit_align"):
                x = margin_x
            elif align == "right" and block.get("explicit_align"):
                x = max(margin_x, width - margin_x - image_width)
            else:
                x = max(margin_x, int((width - image_width) / 2))
            page.paste(fitted, (int(x), int(y)))
            y += image_height + image_gap
            page_has_body = True
            continue
        paragraph = block["text"]
        align = block["align"]
        lines = _wrap_text_for_width(draw, paragraph, body_font, max_text_width)
        block_gap = centered_paragraph_gap if align == "center" else paragraph_gap
        needed = max(1, len(lines)) * line_height + block_gap
        if y + needed > usable_bottom and page_has_body:
            save_page()
            if page_limit_reached():
                break
            new_page()
        for line in lines:
            if y + line_height > usable_bottom and page_has_body:
                save_page()
                if page_limit_reached():
                    break
                new_page()
            line_width = _text_width(draw, line, body_font)
            if align == "center":
                x = max(margin_x, (width - line_width) / 2)
            elif align == "right":
                x = max(margin_x, width - margin_x - line_width)
            else:
                x = margin_x
            _draw_text_with_fallback(draw, (x, y), line, fill=text_color, font=body_font)
            y += line_height
            page_has_body = True
        if page_limit_reached():
            break
        y += block_gap
    if not page_limit_reached():
        save_page()

    key = _text_page_cache_key(source_url, clean_title, clean_blocks)
    store_text_page_bytes(key, page_bytes)
    return [f"{TEXT_PAGE_URL_PREFIX}{key}/{idx + 1}.jpg" for idx in range(len(page_bytes))]


def request_scanmanga_reader_api(api_url, chapter_url, reader_vars, api_body, ua):
    """Appelle l'API lecteur Scan-Manga depuis une session vierge.

    Certains lecteurs avec avertissement ajoutent un état de navigation qui fait
    échouer bqj.scan-manga.com si le POST réutilise la session du GET HTML.
    """
    api_session = requests.Session()
    response = api_session.post(
        api_url,
        headers=build_scanmanga_api_headers(chapter_url, "", ua),
        json={
            "a": reader_vars["sme"],
            "b": reader_vars["sml"],
            "c": api_body["c"],
        },
        impersonate="chrome",
        timeout=20,
    )
    return response, int(getattr(response, "status_code", 0) or 0)


def fetch_scanmanga_images(link, cookie, ua, max_images=None, emit_logs=True, cancel_event=None):
    """Analyse les images Scan-Manga via l'API lecteur; les fichiers passent ensuite par Playwright."""
    chapter_url = (link or "").strip()
    if not chapter_url:
        return []
    if emit_logs:
        runtime_log(
            "Analyse du lecteur Scan-Manga via API en cours; les images seront récupérées via Playwright.",
            level="info",
            context={"action": "reader_analysis", "domain": "scanmanga"},
        )
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")
    session = requests.Session()
    page_response = session.get(
        chapter_url,
        headers=build_scanmanga_navigation_headers(chapter_url, cookie, ua),
        impersonate="chrome",
        timeout=20,
    )
    status_code = int(getattr(page_response, "status_code", 0) or 0)
    if status_code != 200:
        raise AuthError(f"Scan-Manga: page lecteur inaccessible (HTTP {status_code}).")
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")

    page_html = page_response.text or ""
    novel_chapter = extract_scanmanga_novel_chapter(page_html)
    if novel_chapter:
        novel_title, paragraphs = novel_chapter
        images = render_scanmanga_novel_pages(
            novel_title,
            paragraphs,
            chapter_url,
            cookie,
            ua,
            max_pages=max_images,
        )
        if max_images:
            images = images[:max_images]
        if emit_logs:
            runtime_log(
                f"{len(images)} page(s) texte Scan-Manga générée(s) depuis le chapitre Novel.",
                level="info",
                context={"action": "extract_text_pages", "domain": "scanmanga"},
            )
        return images

    reader_vars = extract_scanmanga_reader_vars(page_html)
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")
    fingerprint = {"gpu": "IC", "connection": "IC"}
    api_body = {
        "a": reader_vars["sme"],
        "b": reader_vars["sml"],
        "c": base64.b64encode(json.dumps(fingerprint, separators=(",", ":")).encode("utf-8")).decode("ascii"),
    }
    api_url = f"https://bqj.scan-manga.com/lel/{reader_vars['idc']}.json"
    api_response, api_status = request_scanmanga_reader_api(api_url, chapter_url, reader_vars, api_body, ua)
    if api_status != 200 and cookie:
        # Un cookie valide pour www.scan-manga.com peut être refusé par bqj.scan-manga.com.
        retry_session = requests.Session()
        retry_page = retry_session.get(
            chapter_url,
            headers=build_scanmanga_navigation_headers(chapter_url, "", ua),
            impersonate="chrome",
            timeout=20,
        )
        if int(getattr(retry_page, "status_code", 0) or 0) == 200:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Téléchargement annulé.")
            retry_vars = extract_scanmanga_reader_vars(retry_page.text or "")
            retry_body = {
                "a": retry_vars["sme"],
                "b": retry_vars["sml"],
                "c": api_body["c"],
            }
            api_response, api_status = request_scanmanga_reader_api(
                f"https://bqj.scan-manga.com/lel/{retry_vars['idc']}.json",
                chapter_url,
                retry_vars,
                retry_body,
                ua,
            )
            reader_vars = retry_vars
    if api_status != 200:
        raise AuthError(f"Scan-Manga: API lecteur inaccessible (HTTP {api_status}).")
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Téléchargement annulé.")

    data = decode_scanmanga_data_api(api_response.text or "", reader_vars["idc"])
    images = build_scanmanga_image_urls(data)
    if max_images:
        images = images[:max_images]
    if emit_logs:
        runtime_log(
            f"{len(images)} image(s) Scan-Manga détectée(s) via API lecteur.",
            level="info",
            context={"action": "extract_images", "domain": "scanmanga"},
        )
    return images


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
    elif source_site == "toonfr.com":
        match_slug = re.match(r"^/webtoon/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    elif source_site == "ortegascans.fr":
        match_slug = re.match(r"^/serie/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    elif source_site == "hentaizone.xyz":
        match_slug = re.match(r"^/manga/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    elif source_site == "scan-manga.com":
        match_slug = re.match(r"^/\d+(?:-\d+)?/([^/?#]+)\.html$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    elif source_site in ("crunchyscan.fr", "scan-hentai.net"):
        match_slug = re.match(r"^/lecture-en-ligne/([^/?#]+)/?$", path_value, flags=re.IGNORECASE)
        if match_slug:
            source_slug = match_slug.group(1).strip()
    pairs = []
    volume_metadata = {}

    if source_site in ("crunchyscan.fr", "scan-hentai.net"):
        pairs, volume_metadata = parse_crunchy_family_chapters_from_html(url, soup, html_content)
        unique_pairs = [
            pair
            for _index, pair in sorted(enumerate(pairs), key=crunchy_family_chapter_sort_key)
        ]
        ordered_metadata = {
            link: volume_metadata.get(link, {})
            for _label, link in unique_pairs
            if link
        }
        if not unique_pairs:
            raise ParseError("Aucun chapitre CrunchyScan/Scan-Hentai détecté (page protégée ou structure modifiée).")
        if emit_logs:
            runtime_log(
                f"{len(unique_pairs)} chapitre(s) {comicinfo_source_label_from_url(url)} détecté(s)",
                level="info",
                context={"action": "parse_catalogue", "domain": get_cookie_domain_from_url(url)},
            )
        return title, unique_pairs, ordered_metadata

    if source_site == "scan-manga.com":
        pairs, volume_metadata = parse_scanmanga_chapters_from_html(url, soup, html_content)
        unique_pairs = list(reversed(pairs))
        ordered_metadata = {
            link: volume_metadata.get(link, {})
            for _label, link in unique_pairs
            if link
        }
        if not unique_pairs:
            raise ParseError("Aucun chapitre Scan-Manga détecté (page protégée ou structure modifiée).")
        if emit_logs:
            runtime_log(
                f"{len(unique_pairs)} chapitre(s) Scan-Manga détecté(s)",
                level="info",
                context={"action": "parse_catalogue", "domain": "scanmanga"},
            )
        return title, unique_pairs, ordered_metadata

    if source_site == "ortegascans.fr" and source_slug:
        pairs, volume_metadata = parse_ortega_chapters_from_html(url, soup, source_slug, html_content)
        unique_pairs = list(reversed(pairs))
        ordered_metadata = {
            link: volume_metadata.get(link, {})
            for _label, link in unique_pairs
            if link
        }
        if not unique_pairs:
            raise Exception("Aucun tome/chapitre détecté (page protégée ou structure modifiée).")
        if emit_logs:
            premium_count = sum(1 for meta in ordered_metadata.values() if bool(meta.get("premium")))
            suffix = f", dont {premium_count} premium" if premium_count else ""
            runtime_log(
                f"{len(unique_pairs)} tomes/chapitres détectés{suffix}",
                level="info",
                context={"action": "parse_catalogue"},
            )
        return title, unique_pairs, ordered_metadata

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
        raise ParseError("Aucun tome/chapitre détecté (page protégée ou structure modifiée).")

    unique_pairs.reverse()  # Pour afficher dans l'ordre croissant
    if emit_logs:
        runtime_log(
            f"{len(unique_pairs)} tomes/chapitres détectés",
            level="info",
            context={"action": "parse_catalogue"},
        )
    return title, unique_pairs, {}


def fetch_mangas_origines_chapters_via_ajax(url, cookie, ua, emit_logs=True):
    """Récupère les chapitres via l'endpoint AJAX Madara (origines, hentai, toonfr)."""
    site = get_supported_site_from_url(url)
    if site not in ("mangas-origines.fr", "hentai-origines.fr", "toonfr.com"):
        return [], {}

    parsed = urlparse(url)
    base_url = (url if url.endswith("/") else f"{url}/").strip()
    if site == "mangas-origines.fr":
        path_prefix = "oeuvre"
    elif site == "hentai-origines.fr":
        path_prefix = "manga"
    else:
        path_prefix = "webtoon"

    match_slug = re.match(rf"^/{path_prefix}/([^/?#]+)/?$", parsed.path or "", flags=re.IGNORECASE)
    if not match_slug:
        return [], {}
    source_slug = (match_slug.group(1) or "").strip().lower()
    if not source_slug:
        return [], {}

    headers = build_request_headers(
        url,
        cookie,
        ua or DEFAULT_USER_AGENT,
        referer_url=base_url,
    )
    headers.update({
        "X-Requested-With": "XMLHttpRequest",
    })

    pairs = []
    max_page = 1
    page = 1
    while page <= max_page:
        if site == "toonfr.com":
            endpoint = urljoin(base_url, "ajax/chapters/")
        else:
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
        if page_ids and site != "toonfr.com":
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
        if site == "toonfr.com":
            break

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
            log_domain = (
                "origines"
                if site == "mangas-origines.fr"
                else "hentai" if site == "hentai-origines.fr" else "toonfr"
            )
            runtime_log(
                f"{len(unique_pairs)} tomes/chapitres détectés via AJAX.",
                level="info",
                context={"action": "parse_catalogue_ajax", "domain": log_domain},
            )
    return unique_pairs, {}


def fetch_manga_analysis(url, cookie, ua, progress_callback=None, emit_logs=True):
    """
    Récupère les données d'un manga sous forme structurée.
    
    Args:
        url (str): URL de la page catalogue du manga
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
    
    Returns:
        MangaAnalysis: titre, chapitres, métadonnées et HTML brut
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
        if int(getattr(r, "status_code", 0) or 0) in (401, 403):
            raise AuthError(f"Accès refusé ou URL invalide ({detail})")
        raise SushiDLError(f"Accès refusé ou URL invalide ({detail})")

    html_content = r.text or ""
    if callable(progress_callback):
        progress_callback("parse")
    site = get_supported_site_from_url(url)
    volume_metadata = {}
    if site in ("mangas-origines.fr", "hentai-origines.fr", "toonfr.com"):
        parse_error = None
        try:
            title, pairs, volume_metadata = parse_manga_data_from_html(
                url,
                html_content,
                emit_logs=False,
            )
        except Exception as exc:
            parse_error = exc
            title = extract_manga_title_from_html(url, html_content)
            pairs = []
            volume_metadata = {}

        ajax_pairs, ajax_metadata = fetch_mangas_origines_chapters_via_ajax(url, cookie, ua, emit_logs=emit_logs)
        if ajax_pairs:
            pairs = ajax_pairs
            volume_metadata = ajax_metadata
            if not title:
                title = extract_manga_title_from_html(url, html_content)
        elif not pairs:
            if parse_error is not None:
                raise parse_error
            raise ParseError("Aucun tome/chapitre détecté sur ce site.")
    else:
        title, pairs, volume_metadata = parse_manga_data_from_html(
            url,
            html_content,
            emit_logs=emit_logs,
        )
    series_metadata = extract_series_metadata_from_html(url, html_content, title)
    return MangaAnalysis(
        title=title or "",
        pairs=[(str(label), str(link)) for label, link in (pairs or [])],
        volume_metadata=dict(volume_metadata or {}),
        series_metadata=dict(series_metadata or {}),
        html_content=html_content,
    )


def fetch_manga_data(url, cookie, ua, return_html=False, progress_callback=None, emit_logs=True):
    """
    Récupère les données d'un manga : titre et liste des tomes/chapitres.
    Garde l'ancienne API tuple tout en alimentant les métadonnées historiques.
    """
    analysis = fetch_manga_analysis(
        url,
        cookie,
        ua,
        progress_callback=progress_callback,
        emit_logs=emit_logs,
    )
    fetch_manga_data.last_volume_metadata = dict(analysis.volume_metadata or {})
    fetch_manga_data.last_series_metadata = dict(analysis.series_metadata or {})
    if return_html:
        return analysis.title, analysis.pairs, analysis.html_content
    return analysis.title, analysis.pairs


fetch_manga_data.last_volume_metadata = {}
fetch_manga_data.last_series_metadata = {}


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


def get_images(link, cookie, ua, retries=3, delay=2, debug_mode=False, cancel_event=None, max_images=None, emit_logs=True):
    """
    Récupère la liste des URLs d'images pour un volume/chapitre
    
    Args:
        link (str): URL de la page du volume
        cookie (str): Cookie cf_clearance
        ua (str): User-Agent
        retries (int): Tentatives de récupération
        delay (int): Délai entre les tentatives
        debug_mode (bool): Activer le mode debug
        cancel_event (threading.Event|None): Annulation du worker si demandée
    
    Returns:
        list: Liste des URLs d'images
    """
    max_images = max(0, int(max_images or 0)) or None
    cached_images = get_cached_image_urls(link, max_images=max_images)
    if cached_images is not None:
        if emit_logs:
            runtime_log(
                f"{len(cached_images)} image(s) récupérée(s) depuis le cache session.",
                level="debug",
                context={"action": "get_images_cache", "domain": get_cookie_domain_from_url(link)},
            )
        return cached_images

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
            if emit_logs:
                runtime_log(
                    f"{removed} image(s) parasite(s) supprimée(s) dynamiquement.",
                    level="debug",
                    context={"action": "image_filter", "domain": domain},
                )
        return filtered

    def extract_images(r_text, domain):
        """Extrait les URLs d'images depuis le contenu HTML"""
        def finalize_images(items, detected_label=None):
            images = list(items or [])
            total_after_filter = len(images)
            limited_images = images[:max_images] if max_images else images
            if emit_logs:
                if detected_label:
                    runtime_log(
                        f"{len(limited_images)} image(s) retenue(s){' sur ' + str(total_after_filter) if max_images and total_after_filter > len(limited_images) else ''} via {detected_label}.",
                        level="info",
                        context={"action": "extract_images", "domain": domain},
                    )
                else:
                    runtime_log(
                        f"{len(limited_images)} image(s) retenue(s){' sur ' + str(total_after_filter) if max_images and total_after_filter > len(limited_images) else ''} après filtrage.",
                        level="info",
                        context={"action": "extract_images", "domain": domain},
                    )
            store_cached_image_urls(link, limited_images, max_images=max_images)
            return limited_images

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
                if emit_logs:
                    runtime_log(
                        f"{removed} image(s) pub supprimée(s) (début/fin, résolution atypique).",
                        level="debug",
                        context={"action": "image_filter", "domain": domain},
                    )
            return safe_entries

        if domain == "ortega":
            ortega_text = (r_text or "").replace("\\/", "/")
            api_matches = re.findall(
                r'(?:\\?["\'])url(?:\\?["\'])\s*:\s*(?:\\?["\'])(/api/chapters/[^"\'\\]+)',
                ortega_text,
                re.IGNORECASE,
            )
            images = [
                normalize_image_url(urljoin(link, match.strip().rstrip("\\")))
                for match in api_matches
                if match.strip().rstrip("\\")
            ]
            images = dedupe_images(images)
            if images:
                return finalize_images(images, detected_label="ortega.initialData")

        # Étape 0 — Priorité à la structure Madara (mangas-origines)
        if domain == "origines":
            soup = BeautifulSoup(r_text, "html.parser")
            entries = collect_madara_page_entries(soup)
            entries = dedupe_entries(entries)
            entries = trim_edge_ads_by_resolution(entries)
            images = [entry["url"] for entry in entries]
            if images:
                images = clean_parasites(images, domain)
                return finalize_images(images)

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
                    images = clean_parasites(images, domain)
                    return finalize_images(images, detected_label="ts_reader.run")
            except Exception as e:
                if emit_logs:
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
                return finalize_images(images)

        # Étape 2b — Fallback : structure Madara (mangas-origines)
        entries = collect_madara_page_entries(soup)
        entries = dedupe_entries(entries)
        entries = trim_edge_ads_by_resolution(entries)
        images = [entry["url"] for entry in entries]
        if images:
            images = clean_parasites(images, domain)
            return finalize_images(images)

        reading_container = soup.select_one("div.reading-content, div.entry-content_wrap")
        if reading_container:
            images = collect_images_from_container(reading_container)
            images = dedupe_images(images)
            if images:
                images = clean_parasites(images, domain)
                return finalize_images(images)

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
            return finalize_images(img_urls)
        return img_urls[:max_images] if max_images else img_urls

    attempt_count = max(1, int(retries or 1))
    domain = get_site_domain_key(link)

    if domain == "scanmanga":
        try:
            images = fetch_scanmanga_images(
                link,
                cookie,
                ua,
                max_images=max_images,
                emit_logs=emit_logs,
                cancel_event=cancel_event,
            )
            if images:
                store_cached_image_urls(link, images, max_images=max_images)
                return images
        except Exception as exc:
            if emit_logs:
                runtime_log(
                    f"Extraction Scan-Manga impossible: {exc}",
                    level="error",
                    context={"action": "get_images", "domain": "scanmanga"},
                )
            return []

    if domain in ("crunchyscan", "scanhentai"):
        try:
            if emit_logs:
                runtime_log(
                    f"Playwright {domain}: récupération des images demandée.",
                    level="info",
                    context={"action": "playwright_download", "domain": domain},
                )
            images = fetch_crunchy_reader_images(
                link,
                cookie,
                ua,
                max_images=max_images,
                emit_logs=emit_logs,
                cancel_event=cancel_event,
            )
            if images:
                if emit_logs:
                    runtime_log(
                        f"Playwright {domain}: {len(images)} image(s) prêtes pour le téléchargement.",
                        level="success",
                        context={"action": "playwright_download_done", "domain": domain},
                    )
                store_cached_image_urls(link, images, max_images=max_images)
                return images
        except Exception as exc:
            if emit_logs:
                runtime_log(
                    f"Extraction lecteur navigateur impossible: {exc}",
                    level="error",
                    context={"action": "get_images", "domain": domain},
                )
            return []

    candidate_links = [(link or "").strip()]
    if domain in ("origines", "hentai"):
        list_url = build_mangas_origines_list_url(link)
        candidate_links = [list_url] if list_url else []
        if link and link.strip() and link.strip() not in candidate_links:
            candidate_links.append(link.strip())
        if candidate_links:
            if emit_logs:
                runtime_log(
                    "Mode lecture privilégié: style=list.",
                    level="debug",
                    context={"action": "get_images", "domain": domain},
                )

    for link_idx, candidate_link in enumerate(candidate_links, start=1):
        for attempt in range(1, attempt_count + 1):
            if cancel_event is not None and cancel_event.is_set():
                if emit_logs:
                    runtime_log(
                        "Extraction images annulée.",
                        level="warning",
                        context={"action": "get_images"},
                    )
                return []
            try:
                r = make_request(candidate_link, cookie, ua)
                body = r.text or ""
                if emit_logs:
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
                    if emit_logs:
                        runtime_log(
                            f"Fichier debug généré: {debug_file}",
                            level="debug",
                            context={"action": "debug_dump"},
                        )

                images = extract_images(body, domain)
                if images:
                    return images

                if emit_logs:
                    runtime_log(
                        f"Aucune image trouvée en accès direct (tentative {attempt}/{attempt_count}).",
                        level="warning",
                        context={"action": "get_images"},
                    )
            except Exception as e:
                message = str(e)
                interpretation = interpret_curl_error(message)
                if emit_logs:
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
                if emit_logs:
                    runtime_log(
                        f"Nouvelle tentative extraction images dans {sleep_time}s.",
                        level="debug",
                        context={"action": "get_images"},
                    )
                if interruptible_sleep(cancel_event, sleep_time):
                    if emit_logs:
                        runtime_log(
                            "Extraction images annulée pendant l'attente de retry.",
                            level="warning",
                            context={"action": "get_images"},
                        )
                    return []

    if emit_logs:
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
    comicinfo_enabled=True,
    chapter_cover_enabled=True,
    referer_url=None,
    smart_resume_enabled=True,
    error_callback=None,
    output_root=ROOT_FOLDER,
    prompt_cookie_retry=True,
    total_count=None,
    series_metadata=None,
    cover_url=None,
    download_threads=None,
    archive_label=None,
):
    """Télécharge un volume complet avec gestion de progression et archivage."""
    if cancel_event.is_set():
        return None

    tome_label = normalize_tome_label(volume)
    archive_tome_label = normalize_tome_label(archive_label or volume)
    clean_title = sanitize_folder_name(title)
    clean_tome = sanitize_folder_name(tome_label)
    clean_archive_tome = sanitize_folder_name(archive_tome_label)
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
    force_resume_after_cookie_refresh = False
    current_download_threads = clamp_download_threads(download_threads)
    volume_started_at = time.perf_counter()

    while True:
        if cancel_event.is_set():
            return None

        attempt_started_at = time.perf_counter()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            logger(f"Erreur création dossier: {e}", level="error")
            report_error("prepare", f"Erreur création dossier: {e}")
            return False

        number_len = max(1, len(str(len(images))))
        failed_downloads = []

        existing_indexes = set()
        if smart_resume_enabled or force_resume_after_cookie_refresh:
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
        if (smart_resume_enabled or force_resume_after_cookie_refresh) and existing_count:
            logger(
                f"Reprise intelligente: {existing_count}/{len(images)} page(s) déjà présentes pour {tome_label}.",
                level="info",
            )
        log_perf(logger, "scan reprise", attempt_started_at, tome=tome_label, existantes=existing_count)

        download_started_at = time.perf_counter()
        worker_count = clamp_download_threads(current_download_threads)
        if worker_count != clamp_download_threads(download_threads):
            logger(f"Mode adaptatif: {worker_count} téléchargement(s) parallèle(s) pour {tome_label}.", level="info")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = []
            progress_counter = {"done": existing_count}
            lock = threading.Lock()

            if update_progress and existing_count:
                update_progress(existing_count, len(images))

            for i, url in enumerate(images):
                if i in existing_indexes:
                    continue
                if cancel_event.is_set():
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
        log_perf(logger, "telechargement images", download_started_at, tome=tome_label, threads=worker_count, images=len(images))

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

        soft_failures = [f for f in normalized_failures if f["kind"] in ("missing", "invalid_image")]
        missing_failures = [f for f in soft_failures if f["kind"] == "missing"]
        invalid_image_failures = [f for f in soft_failures if f["kind"] == "invalid_image"]
        hard_failures = [f for f in normalized_failures if f["kind"] not in ("missing", "invalid_image", "cancelled")]

        if missing_failures:
            sample_missing = missing_failures[0].get("url") or "URL inconnue"
            logger(
                f"{len(missing_failures)} page(s) absente(s) (404/410) sur {tome_label}. Exemple: {sample_missing}",
                level="warning",
            )
        if invalid_image_failures:
            sample_invalid = invalid_image_failures[0].get("url") or "URL inconnue"
            logger(
                f"{len(invalid_image_failures)} page(s) invalide(s)/illisible(s) sur {tome_label}. Exemple: {sample_invalid}",
                level="warning",
            )
        if soft_failures:
            logger("CBZ maintenu: les pages manquantes ou invalides sont ignorées.", level="info")

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

            if should_reduce_threads_for_failures(hard_failures) and current_download_threads > 1:
                current_download_threads = max(1, current_download_threads // 2)
                force_resume_after_cookie_refresh = True
                logger(
                    f"Ralentissement automatique: relance de {tome_label} avec {current_download_threads} thread(s) après erreurs serveur/rate-limit.",
                    level="warning",
                )
                continue

            should_prompt_cookie = any(
                should_offer_cookie_refresh(f.get("status_code"), f.get("reason"))
                for f in hard_failures
            )

            if not can_prompt_cookie_retry:
                logger("Relance cookie déjà tentée une fois; abandon du tome.", level="warning")
                return False

            if not prompt_cookie_retry:
                logger("Relance cookie interactive désactivée pour ce mode d'exécution.", level="warning")
                return False

            if not should_prompt_cookie or target_domain not in COOKIE_DOMAINS:
                logger("Échec non lié au cookie: abandon du tome sans popup de renouvellement.", level="warning")
                return False

            can_prompt_cookie_retry = False
            try:
                if app and hasattr(app, "prompt_cookie_refresh"):
                    res = app.prompt_cookie_refresh(
                        target_domain,
                        tome_label,
                        sample_reason,
                        cancel_event=cancel_event,
                    )
                elif app and hasattr(app, "ask_yes_no"):
                    res = app.ask_yes_no(
                        "Erreur de téléchargement",
                        "Le cookie semble expiré. Mets-le à jour puis confirme la relance.",
                    )
                else:
                    res = messagebox.askyesno(
                        "Erreur de téléchargement",
                        "Le cookie semble expiré. Mets-le à jour puis confirme la relance.",
                    )

                if cancel_event.is_set():
                    return None

                if res:
                    refreshed_cookie = active_cookie
                    if app and target_domain in COOKIE_DOMAINS:
                        try:
                            app.sync_cookie_source_for_domain(target_domain)
                            app.persist_settings()
                            refreshed_cookie = app.get_cookie(referer_url or "")
                        except Exception as sync_err:
                            logger(f"Impossible de récupérer le nouveau cookie: {sync_err}", level="warning")

                    refreshed_cookie = (refreshed_cookie or "").strip()
                    if refreshed_cookie:
                        active_cookie = refreshed_cookie
                        current_download_threads = 1
                        force_resume_after_cookie_refresh = True
                        logger(
                            "Cookie mis à jour. Relance du tome avec reprise intelligente au point d'arrêt et 1 thread de sécurité...",
                            level="info",
                        )
                        continue

                logger("Relance abandonnée: cookie non mis à jour.", level="error")
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
            log_perf(logger, "volume termine", volume_started_at, tome=tome_label, cbz=False)
            return True

        archive_started_at = time.perf_counter()
        effective_cover_url = (cover_url or "").strip()
        if not effective_cover_url and isinstance(series_metadata, dict):
            effective_cover_url = (series_metadata.get("cover_url") or "").strip()
        if chapter_cover_enabled and is_chapter_label(tome_label) and effective_cover_url:
            try:
                cover_path = write_chapter_cover_page(
                    folder,
                    effective_cover_url,
                    active_cookie,
                    ua,
                    referer_url=referer_url,
                    webp2jpg_enabled=webp2jpg_enabled,
                )
                if cover_path:
                    logger("Couverture ajoutée en première page du chapitre.", level="info")
            except Exception as cover_exc:
                logger(
                    f"Couverture non ajoutée pour {tome_label}: {cover_exc}",
                    level="warning",
                )

        if failed_downloads:
            try:
                report_path = write_download_report(folder, tome_label, images, failed_downloads)
                if report_path:
                    logger(
                        f"Rapport pages manquantes ajoute au CBZ: {os.path.basename(report_path)}",
                        level="warning",
                    )
            except Exception as report_exc:
                logger(f"Rapport pages manquantes non genere: {report_exc}", level="warning")

        if comicinfo_enabled:
            try:
                page_count = count_downloaded_images(folder)
                source_domain = comicinfo_source_label_from_url(referer_url or "")
                notes = ""
                if soft_failures:
                    notes = (
                        f"Archive generee par {APP_NAME} avec "
                        f"{len(soft_failures)} page(s) manquante(s) ou invalide(s)."
                    )
                write_comicinfo_xml(
                    folder,
                    title,
                    tome_label,
                    page_count=page_count,
                    total_count=total_count,
                    web_url=referer_url,
                    source_domain=source_domain,
                    notes=notes,
                    series_metadata=series_metadata,
                )
            except Exception as comicinfo_exc:
                logger(
                    f"ComicInfo.xml non genere pour {tome_label}: {comicinfo_exc}",
                    level="warning",
                )

        if archive_cbz(folder, title, archive_tome_label, remove_source=True):
            cbz_path = os.path.join(
                base_output_dir, clean_title, f"{clean_title} - {clean_archive_tome}.cbz"
            )
            try:
                size_mb = round(os.path.getsize(cbz_path) / (1024 * 1024), 2)
            except OSError:
                size_mb = 0
            logger("", level="info")
            if soft_failures:
                logger(
                    f"CBZ créé malgré {len(soft_failures)} page(s) manquante(s)/invalide(s) pour {tome_label}.",
                    level="warning",
                )
            logger(f"CBZ créé : {cbz_path} ({size_mb} MB)", level="cbz")
            log_perf(logger, "archive cbz", archive_started_at, tome=tome_label, taille=f"{size_mb} MB")
            log_perf(logger, "volume termine", volume_started_at, tome=tome_label, cbz=True)
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
    comicinfo_enabled=True,
    chapter_cover_enabled=True,
    download_threads=DEFAULT_DOWNLOAD_THREADS,
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
        "comicinfo_enabled": bool(comicinfo_enabled),
        "chapter_cover_enabled": bool(chapter_cover_enabled),
        "last_url": MangaApp.last_url_used,
        "timestamp": now_iso,
        "cookie_updated_at": cookie_updated_at,
        "cookie_sources": normalized_sources,
        "cookie_user_agents": normalized_cookie_uas,
        "cookie_headers": {domain: "" for domain in COOKIE_DOMAINS},
        "webp2jpg_enabled": bool(webp2jpg_enabled),
        "smart_resume_enabled": bool(smart_resume_enabled),
        "verbose_logs": bool(verbose_logs),
        "download_threads": clamp_download_threads(download_threads),
    }
    COOKIE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = COOKIE_CACHE_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, COOKIE_CACHE_PATH)
    set_private_file_permissions(COOKIE_CACHE_PATH)
    return cookie_updated_at


def load_cookie_cache():
    """Charge les paramètres depuis le fichier cache"""
    default_cbz = True
    default_comicinfo = True
    default_chapter_cover = True
    default_webp2jpg = True
    default_smart_resume = True
    default_verbose_logs = True
    default_download_threads = DEFAULT_DOWNLOAD_THREADS
    
    if not COOKIE_CACHE_PATH.exists():
        return (
            {domain: "" for domain in COOKIE_DOMAINS},
            DEFAULT_USER_AGENT,
            default_cbz,
            default_comicinfo,
            default_chapter_cover,
            "",
            default_webp2jpg,
            default_smart_resume,
            default_verbose_logs,
            default_download_threads,
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
                build_cf_clearance_cookie_header(cookies.get(domain, ""))
                if (cookies.get(domain) or "").strip()
                else ""
            )
            for domain in COOKIE_DOMAINS
        }
        for domain in COOKIE_DOMAINS:
            if not rebuilt_cookie_headers[domain]:
                rebuilt_cookie_headers[domain] = sanitize_cookie_header(cookie_headers.get(domain))
        cookie_updated_at = data.get("cookie_updated_at", {})
        if not isinstance(cookie_updated_at, dict):
            cookie_updated_at = {}

        return (
            {domain: (cookies.get(domain) or "").strip() for domain in COOKIE_DOMAINS},
            (data.get("ua") or DEFAULT_USER_AGENT).strip(),
            data.get("cbz_enabled", default_cbz),
            data.get("comicinfo_enabled", default_comicinfo),
            data.get("chapter_cover_enabled", default_chapter_cover),
            (data.get("last_url") or "").strip(),
            data.get("webp2jpg_enabled", default_webp2jpg),
            bool(data.get("smart_resume_enabled", default_smart_resume)),
            bool(data.get("verbose_logs", default_verbose_logs)),
            clamp_download_threads(data.get("download_threads", default_download_threads)),
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
        default_comicinfo,
        default_chapter_cover,
        "",
        default_webp2jpg,
        default_smart_resume,
        default_verbose_logs,
        default_download_threads,
        {domain: "" for domain in COOKIE_DOMAINS},
        {domain: "" for domain in COOKIE_DOMAINS},
        {domain: "" for domain in COOKIE_DOMAINS},
        {domain: "" for domain in COOKIE_DOMAINS},
    )


def extract_cover_url_from_html(page_url, html_content):
    """Extrait l'URL de couverture sans effectuer de telechargement."""
    soup = BeautifulSoup(html_content or "", "html.parser")
    page_url = (page_url or "").strip()
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

    def extract_srcset_entries(raw_srcset):
        srcset = (raw_srcset or "").strip()
        if not srcset:
            return []
        entries = [part.strip() for part in srcset.split(",") if part.strip()]
        candidates = []
        for entry in entries:
            parts = entry.split()
            if not parts:
                continue
            raw_url = parts[0].strip()
            weight = 0
            if len(parts) > 1:
                descriptor = parts[1].strip().lower()
                if descriptor.endswith("w") and descriptor[:-1].isdigit():
                    weight = int(descriptor[:-1])
                elif descriptor.endswith("x"):
                    try:
                        weight = int(float(descriptor[:-1]) * 1000)
                    except ValueError:
                        weight = 0
            candidates.append((raw_url, weight))
        candidates.sort(key=lambda item: item[1])
        return [url for url, _weight in candidates if url]

    def cover_candidate_score(url, source=""):
        value = (url or "").lower()
        score = 0
        if "cover" in value or "couverture" in value or "manga" in value:
            score += 30
        if source in ("srcset", "meta"):
            score += 10
        match = re.search(r"-(\d{2,5})x(\d{2,5})(?:\.[a-z0-9]{3,5})(?:$|[?#])", value)
        if match:
            try:
                score += int(match.group(1)) * int(match.group(2)) / 10000
            except Exception:
                pass
        return score

    candidates = []

    def add_candidate(raw_candidate, source=""):
        candidate = resolve_cover_candidate(raw_candidate)
        if not candidate:
            return
        candidates.append((candidate, cover_candidate_score(candidate, source)))

    def extract_cover_from_img(tag):
        if tag is None:
            return ""
        for attr_name in ("data-srcset", "srcset"):
            for candidate in extract_srcset_entries(tag.get(attr_name)):
                add_candidate(candidate, "srcset")
        for attr_name in ("data-src", "data-lazy-src", "src", "data-cfsrc"):
            candidate = resolve_cover_candidate(tag.get(attr_name))
            if candidate:
                add_candidate(candidate, attr_name)
                return candidate
        if candidates:
            return candidates[-1][0]
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
        "img.manga_cover",
        "img[alt*='couverture']",
    )
    img_url = None

    for selector in cover_selectors:
        cover_img = soup.select_one(selector)
        img_url = extract_cover_from_img(cover_img)
        if img_url:
            break

    if not img_url:
        for node in soup.select(".summary_image, .thumb, .thumb-container, .post-thumb, .profile-manga, [style*='cover.jpg'], [style*='background-image']"):
            style_value = (node.get("style") or "").strip()
            if not style_value:
                continue
            match = re.search(r"background-image\s*:\s*url\(([^)]+)\)", style_value, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = resolve_cover_candidate(match.group(1))
            if candidate:
                add_candidate(candidate, "style")
                img_url = candidate
                break

    if not img_url:
        for tag in soup.find_all("meta", attrs={"property": True}):
            if tag["property"] in ["og:image", "og:image:secure_url"]:
                candidate = resolve_cover_candidate((tag.get("content") or "").strip())
                if candidate:
                    add_candidate(candidate, "meta")
                    img_url = candidate
                    break
    if not img_url:
        for tag in soup.find_all("meta", attrs={"name": True}):
            if (tag.get("name") or "").strip().lower() in ("twitter:image", "twitter:image:src"):
                candidate = resolve_cover_candidate((tag.get("content") or "").strip())
                if candidate:
                    add_candidate(candidate, "meta")
                    img_url = candidate
                    break

    if candidates:
        seen = {}
        for candidate, score in candidates:
            seen[candidate] = max(score, seen.get(candidate, 0))
        img_url = max(seen.items(), key=lambda item: item[1])[0]

    if not img_url:
        return ""
    return img_url


def get_cover_image(r_text):
    """Récupère et affiche l'image de couverture d'un manga."""
    runtime_log("Analyse de la couverture en cours.", level="debug", context={"action": "cover"})
    app = getattr(MangaApp, "current_instance", None)
    page_url = ""
    if app is not None:
        page_url = app.run_on_ui(app.url.get, wait=True, default="").strip()
    img_url = extract_cover_url_from_html(page_url, r_text)
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
        selected_img_url, raw = robust_download_cover_best(
            img_url,
            cookie,
            app.get_request_user_agent_for_url(img_url),
            referer_url=referer_url,
            max_try=2,
            delay=1,
        )
        if selected_img_url and selected_img_url != img_url:
            img_url = selected_img_url
            app.cover_url = selected_img_url
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
            max_frames = COVER_ANIMATION_MAX_FRAMES
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
            if not done.wait(UI_CALL_TIMEOUT_SECONDS):
                raise TimeoutError(
                    f"run_on_ui(wait=True) timeout après {UI_CALL_TIMEOUT_SECONDS}s."
                )
            if holder["error"] is not None:
                raise holder["error"]
            return holder["result"]

        self.ui_queue.put(lambda: callback(*args, **kwargs))
        return default

    def process_ui_queue(self):
        """Traite les actions UI planifiées depuis les threads de fond."""
        start = time.perf_counter()
        processed = 0
        try:
            for _ in range(UI_QUEUE_BATCH_LIMIT):
                action = self.ui_queue.get_nowait()
                try:
                    action()
                except Exception as exc:
                    emit_console_log(f"Erreur action UI planifiée: {exc}", level="error", context={"action": "ui_queue"})
                processed += 1
                if time.perf_counter() - start >= UI_QUEUE_TIME_BUDGET_SECONDS:
                    break
        except queue.Empty:
            pass
        finally:
            delay = 5 if processed and not self.ui_queue.empty() else 30
            self.root.after(delay, self.process_ui_queue)

    def _set_progress_ui(self, percent):
        try:
            safe_percent = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            safe_percent = 0.0
        label_text = f"{int(safe_percent)}%"
        if self.last_progress_percent_ui == round(safe_percent, 2) and self.last_progress_text_ui == label_text:
            return
        self.last_progress_percent_ui = round(safe_percent, 2)
        self.last_progress_text_ui = label_text
        self.progress.set(safe_percent)
        if hasattr(self, "progress_bar") and self._is_ctk_widget(self.progress_bar):
            self.progress_bar.set(safe_percent / 100.0)
        self.progress_label.configure(text=label_text)

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
        if text == self.last_current_volume_text_ui:
            return
        self.last_current_volume_text_ui = text
        self.current_volume_status_label.configure(text=text)

    def _set_eta_ui(self, tome_eta=None, global_eta=None):
        if not hasattr(self, "eta_label"):
            return
        tome_text = format_duration_short(tome_eta)
        global_text = format_duration_short(global_eta)
        text = f"ETA Tome: {tome_text} | ETA Global: {global_text}"
        if text == self.last_eta_text_ui:
            return
        self.last_eta_text_ui = text
        self.eta_label.configure(text=text)

    def _set_download_controls(self, is_running):
        self.download_in_progress = bool(is_running)
        if is_running:
            self.dl_button.configure(text="Téléchargement...", state="disabled")
            self.cancel_button.configure(state="normal")
            self.filter_entry.configure(state="disabled")
            self.clear_filter_button.configure(state="disabled")
            if hasattr(self, "url_entry"):
                self.url_entry.configure(state="disabled")
            if hasattr(self, "analyze_button"):
                self.analyze_button.configure(state="disabled")
            if hasattr(self, "queue_button"):
                self.queue_button.configure(state="disabled")
            if hasattr(self, "invert_button"):
                self.invert_button.configure(state="disabled")
            if hasattr(self, "master_toggle_button"):
                self.master_toggle_button.configure(state="disabled")
            self._style_clear_filter_button(disabled=True)
            self._set_workflow_step("download", "Téléchargement en cours...")
        else:
            self.dl_button.configure(text="Télécharger la sélection")
            self.cancel_button.configure(state="disabled")
            self.filter_entry.configure(state="normal")
            self.clear_filter_button.configure(state="normal")
            if hasattr(self, "url_entry"):
                self.url_entry.configure(state="normal")
            if hasattr(self, "analyze_button"):
                self.analyze_button.configure(
                    state="disabled" if getattr(self, "analysis_in_progress", False) else "normal"
                )
            if hasattr(self, "queue_button"):
                self.queue_button.configure(state="normal")
            if hasattr(self, "invert_button"):
                self.invert_button.configure(state="normal")
            if hasattr(self, "master_toggle_button"):
                self.master_toggle_button.configure(state="normal")
            self._style_clear_filter_button(disabled=False)
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
            text = "Images: --/--"
            if text != self.last_progress_detail_ui:
                self.last_progress_detail_ui = text
                self.progress_detail_label.configure(text=text)
            return
        text = f"Images: {int(done)}/{int(total)}"
        if text == self.last_progress_detail_ui:
            return
        self.last_progress_detail_ui = text
        self.progress_detail_label.configure(text=text)

    def _set_download_runtime_ui(self, percent=None, done=None, total=None, tome_eta=None, global_eta=None):
        if percent is not None:
            self._set_progress_ui(percent)
        if done is not None or total is not None:
            self._set_progress_detail_ui(done, total)
        if tome_eta is not None or global_eta is not None:
            self._set_eta_ui(tome_eta, global_eta)

    def _start_analysis_loading_indicators(self, overlay_text="Chargement de la liste..."):
        self.analysis_spinner_running = True
        self.analysis_spinner_index = 0
        self.analysis_loading_overlay_text = repair_mojibake_text(overlay_text or "Chargement de la liste...")
        if hasattr(self, "analysis_spinner_label"):
            self.analysis_spinner_label.configure(text=SPINNER_FRAMES[0])
        if hasattr(self, "vol_loading_label"):
            self.vol_loading_label.configure(text=f"{SPINNER_FRAMES[0]} {self.analysis_loading_overlay_text}")
            try:
                host = getattr(self, "volume_list_container", None) or getattr(self, "vol_empty_label", None)
                if host is not None:
                    self.vol_loading_label.place(in_=host, relx=0.5, rely=0.5, anchor="center")
                    self.vol_loading_label.lift()
            except Exception:
                pass
        self._tick_analysis_spinner()

    def _tick_analysis_spinner(self):
        after_id = getattr(self, "analysis_spinner_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
            self.analysis_spinner_after_id = None
        if not getattr(self, "analysis_spinner_running", False):
            return
        self.analysis_spinner_index = (int(getattr(self, "analysis_spinner_index", 0) or 0) + 1) % len(SPINNER_FRAMES)
        frame = SPINNER_FRAMES[self.analysis_spinner_index]
        if hasattr(self, "analysis_spinner_label"):
            self.analysis_spinner_label.configure(text=frame)
        if hasattr(self, "vol_loading_label"):
            base = getattr(self, "analysis_loading_overlay_text", "Chargement de la liste...")
            self.vol_loading_label.configure(text=f"{frame} {base}")
        self.analysis_spinner_after_id = self.root.after(120, self._tick_analysis_spinner)

    def _stop_analysis_loading_indicators(self):
        self.analysis_spinner_running = False
        after_id = getattr(self, "analysis_spinner_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.analysis_spinner_after_id = None
        if hasattr(self, "analysis_spinner_label"):
            self.analysis_spinner_label.configure(text="")
        if hasattr(self, "vol_loading_label"):
            try:
                self.vol_loading_label.place_forget()
            except Exception:
                pass

    def _start_preview_spinner(self, message="Chargement des pages..."):
        self.preview_spinner_running = True
        self.preview_spinner_index = 0
        self.preview_spinner_message = repair_mojibake_text(message or "Chargement des pages...")
        self._tick_preview_spinner()

    def _tick_preview_spinner(self):
        after_id = getattr(self, "preview_spinner_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
            self.preview_spinner_after_id = None
        if not getattr(self, "preview_spinner_running", False):
            return
        self.preview_spinner_index = (int(getattr(self, "preview_spinner_index", 0) or 0) + 1) % len(SPINNER_FRAMES)
        frame = SPINNER_FRAMES[self.preview_spinner_index]
        message = getattr(self, "preview_spinner_message", "Chargement des pages...")
        if getattr(self, "preview_status_label", None):
            self.preview_status_label.configure(text=f"{frame} {message}")
        if getattr(self, "preview_image_label", None):
            try:
                current_image = self.preview_image_label.cget("image")
            except Exception:
                current_image = ""
            if not current_image:
                self.preview_image_label.configure(text=f"{frame} Chargement...")
        self.preview_spinner_after_id = self.root.after(120, self._tick_preview_spinner)

    def _stop_preview_spinner(self):
        self.preview_spinner_running = False
        after_id = getattr(self, "preview_spinner_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.preview_spinner_after_id = None

    def _build_preview_image_headers(self, image_url, referer_url, cookie, ua):
        normalized_url = normalize_image_url(image_url)
        referer = referer_url or get_site_root_url(normalized_url) or "https://sushiscan.net/"
        return build_request_headers(
            normalized_url,
            cookie,
            ua or DEFAULT_USER_AGENT,
            accept="image/webp,image/jpeg,image/png,*/*;q=0.8",
            referer_url=referer,
        )

    def _download_preview_pil_image(self, image_url, referer_url, cookie, ua):
        normalized_url = normalize_image_url(image_url)
        if is_text_page_url(normalized_url):
            raw = get_text_page_bytes(normalized_url)
            if not raw:
                raise ValueError("Page texte de preview introuvable en cache.")
            with Image.open(BytesIO(raw)) as image:
                image.load()
                preview = image.convert("RGB").copy()
            if max(preview.size or (0, 0)) > PREVIEW_MAX_IMAGE_DIMENSION:
                preview.thumbnail(
                    (PREVIEW_MAX_IMAGE_DIMENSION, PREVIEW_MAX_IMAGE_DIMENSION),
                    Image.LANCZOS,
                )
            return preview
        if normalize_hostname(urlparse(normalized_url).hostname) in SCANMANGA_IMAGE_HOSTS:
            preview = download_preview_image_with_browser(normalized_url, referer_url, ua or DEFAULT_USER_AGENT)
            if max(preview.size or (0, 0)) > PREVIEW_MAX_IMAGE_DIMENSION:
                preview.thumbnail(
                    (PREVIEW_MAX_IMAGE_DIMENSION, PREVIEW_MAX_IMAGE_DIMENSION),
                    Image.LANCZOS,
                )
            return preview
        headers = self._build_preview_image_headers(image_url, referer_url, cookie, ua)
        raw = robust_download_image(normalized_url, headers, max_try=3, delay=1)
        with Image.open(BytesIO(raw)) as image:
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            preview = image.copy()
        if max(preview.size or (0, 0)) > PREVIEW_MAX_IMAGE_DIMENSION:
            preview.thumbnail(
                (PREVIEW_MAX_IMAGE_DIMENSION, PREVIEW_MAX_IMAGE_DIMENSION),
                Image.LANCZOS,
            )
        return preview

    def _touch_preview_cache_key(self, key):
        cache_key = str(key or "").strip()
        if not cache_key:
            return
        order = getattr(self, "preview_cache_order", None)
        if order is None:
            order = []
            self.preview_cache_order = order
        if cache_key in order:
            order.remove(cache_key)
        order.append(cache_key)

    def _get_preview_cache_entry(self, key):
        cache_key = str(key or "").strip()
        if not cache_key:
            return None
        entry = (getattr(self, "preview_cache", None) or {}).get(cache_key)
        if entry:
            self._touch_preview_cache_key(cache_key)
        return entry

    def _store_preview_cache_entry(self, key, entry):
        cache_key = str(key or "").strip()
        if not cache_key or not isinstance(entry, dict):
            return
        cache = getattr(self, "preview_cache", None)
        if cache is None:
            cache = {}
            self.preview_cache = cache
        cache[cache_key] = entry
        self._touch_preview_cache_key(cache_key)
        order = getattr(self, "preview_cache_order", None)
        if order is None:
            order = []
            self.preview_cache_order = order
        while len(order) > PREVIEW_CACHE_MAX_ITEMS:
            oldest_key = order.pop(0)
            if oldest_key != cache_key:
                cache.pop(oldest_key, None)

    def get_volume_meta(self, index=None, link=None):
        """Retourne le metadata d'un tome/chapitre courant."""
        safe_link = (link or "").strip()
        if not safe_link and index is not None:
            try:
                safe_link = ((self.pairs or [])[index][1] or "").strip()
            except Exception:
                safe_link = ""
        if not safe_link:
            return {}
        metadata = getattr(self, "volume_meta_by_url", None) or {}
        value = metadata.get(safe_link)
        return value if isinstance(value, dict) else {}

    def is_volume_premium(self, index=None, link=None):
        """Indique si le tome/chapitre est premium et doit être ignoré."""
        return bool(self.get_volume_meta(index=index, link=link).get("premium"))

    def _get_preview_item_payload(self, index):
        if index is None or index < 0 or index >= len(getattr(self, "pairs", []) or []):
            return None
        volume_label, link = self.pairs[index]
        link = (link or "").strip()
        if not link:
            return None
        return {
            "index": index,
            "key": link,
            "title": normalize_tome_label(volume_label) or f"Chapitre {index + 1}",
            "link": link,
            "cookie": self.get_cookie(link),
            "ua": self.get_request_user_agent_for_url(link),
            "premium": self.is_volume_premium(index=index, link=link),
        }

    def _close_preview_window(self):
        self._stop_preview_spinner()
        window = getattr(self, "preview_window", None)
        self.preview_window = None
        self.preview_header_label = None
        self.preview_count_label = None
        self.preview_image_frame = None
        self.preview_image_label = None
        self.preview_status_label = None
        self.preview_prev_button = None
        self.preview_next_button = None
        self.preview_close_button = None
        self.preview_ctk_image = None
        self.preview_resize_after_id = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def _ensure_preview_window(self):
        window = getattr(self, "preview_window", None)
        if window is not None:
            try:
                if window.winfo_exists():
                    return window
            except Exception:
                pass

        window = ctk.CTkToplevel(self.root)
        window.title("Prévisualisation")
        window.geometry("920x720")
        try:
            window.minsize(560, 420)
            window.transient(self.root)
        except Exception:
            pass
        window.protocol("WM_DELETE_WINDOW", self._close_preview_window)

        main = ctk.CTkFrame(window, fg_color=self.palette["panel_bg"], corner_radius=0)
        main.pack(fill="both", expand=True)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)
        self.preview_header_label = ctk.CTkLabel(
            header,
            text="Prévisualisation",
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 14),
            anchor="w",
        )
        self.preview_header_label.grid(row=0, column=0, sticky="w")
        self.preview_count_label = ctk.CTkLabel(
            header,
            text="0/0",
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            text_color=self.palette["muted_strong"],
            font=("Segoe UI Semibold", 10),
            width=84,
            height=28,
        )
        self.preview_count_label.grid(row=0, column=1, sticky="e")

        self.preview_image_frame = ctk.CTkFrame(
            main,
            fg_color=self.palette["canvas_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        self.preview_image_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self.preview_image_label = ctk.CTkLabel(
            self.preview_image_frame,
            text="Chargement...",
            fg_color="transparent",
            text_color=self.palette["muted_strong"],
            font=("Segoe UI", 12),
        )
        self.preview_image_label.place(relx=0.5, rely=0.5, anchor="center")

        footer = ctk.CTkFrame(main, fg_color="transparent")
        footer.pack(fill="x", padx=14, pady=(0, 14))
        footer.grid_columnconfigure(1, weight=1)
        self.preview_prev_button = ctk.CTkButton(
            footer,
            text="Précédent",
            width=100,
            height=30,
            corner_radius=6,
            command=self._goto_preview_prev,
        )
        self.preview_prev_button.grid(row=0, column=0, sticky="w")
        self.preview_status_label = ctk.CTkLabel(
            footer,
            text="Préparation...",
            fg_color="transparent",
            text_color=self.palette["muted_strong"],
            font=("Segoe UI", 11),
            anchor="w",
        )
        self.preview_status_label.grid(row=0, column=1, sticky="w", padx=12)
        action_group = ctk.CTkFrame(footer, fg_color="transparent")
        action_group.grid(row=0, column=2, sticky="e")
        self.preview_next_button = ctk.CTkButton(
            action_group,
            text="Suivant",
            width=100,
            height=30,
            corner_radius=6,
            command=self._goto_preview_next,
        )
        self.preview_next_button.pack(side="left", padx=(0, 8))
        self.preview_close_button = ctk.CTkButton(
            action_group,
            text="Fermer",
            width=92,
            height=30,
            corner_radius=6,
            fg_color=self.palette["card_bg"],
            hover_color=self.palette["card_alt"],
            text_color=self.palette["text"],
            border_width=1,
            border_color=self.palette["border"],
            command=self._close_preview_window,
        )
        self.preview_close_button.pack(side="left")

        window.bind("<Configure>", self._schedule_preview_image_refresh, add="+")
        self.preview_image_frame.bind("<Configure>", self._schedule_preview_image_refresh, add="+")
        self.preview_window = window
        self.preview_ctk_image = None
        return window

    def _show_preview_loading(self, title):
        window = self._ensure_preview_window()
        self.preview_header_label.configure(text=title)
        self.preview_count_label.configure(text="0/0")
        self.preview_status_label.configure(text="Chargement des pages...")
        self.preview_image_label.configure(image=None, text="Chargement...")
        self.preview_image_label.image = None
        self.preview_ctk_image = None
        self.preview_prev_button.configure(state="disabled")
        self.preview_next_button.configure(state="disabled")
        self._start_preview_spinner("Chargement des pages...")
        try:
            window.deiconify()
            window.lift()
            window.focus_force()
        except Exception:
            pass

    def _show_preview_error(self, title, message, request_id=None):
        if request_id is not None and request_id != getattr(self, "preview_request_id", 0):
            return
        self._stop_preview_spinner()
        self._ensure_preview_window()
        self.preview_header_label.configure(text=title)
        self.preview_count_label.configure(text="0/0")
        self.preview_status_label.configure(text="Prévisualisation indisponible.")
        self.preview_image_label.configure(image=None, text=repair_mojibake_text(str(message or "Impossible de charger la preview.")))
        self.preview_image_label.image = None
        self.preview_ctk_image = None
        self.preview_prev_button.configure(state="disabled")
        self.preview_next_button.configure(state="disabled")

    def _apply_preview_result(self, payload, pil_images, request_id):
        if request_id != getattr(self, "preview_request_id", 0):
            return
        self._stop_preview_spinner()
        if not pil_images:
            self._show_preview_error(payload.get("title"), "Aucune page exploitable trouvée.", request_id=request_id)
            return
        state = {
            "key": payload.get("key"),
            "title": payload.get("title"),
            "urls": list(payload.get("urls") or []),
            "images": list(pil_images),
            "index": 0,
        }
        self.preview_state = state
        self._store_preview_cache_entry(payload.get("key"), state)
        self._render_preview_page(0)

    def _schedule_preview_image_refresh(self, _event=None):
        if not getattr(self, "preview_window", None):
            return
        if not getattr(self, "preview_state", {}).get("images"):
            return
        after_id = getattr(self, "preview_resize_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.preview_resize_after_id = self.root.after(60, self._render_preview_page)

    def _render_preview_page(self, target_index=None):
        self.preview_resize_after_id = None
        state = getattr(self, "preview_state", None) or {}
        images = list(state.get("images") or [])
        if not images or not getattr(self, "preview_image_frame", None):
            return
        if target_index is not None:
            state["index"] = max(0, min(int(target_index), len(images) - 1))
        current_index = max(0, min(int(state.get("index", 0) or 0), len(images) - 1))
        state["index"] = current_index
        self.preview_header_label.configure(text=state.get("title") or "Prévisualisation")
        self.preview_count_label.configure(text=f"{current_index + 1}/{len(images)}")
        self.preview_status_label.configure(text=f"{len(images)} page(s) chargée(s)")
        self.preview_prev_button.configure(state=("normal" if current_index > 0 else "disabled"))
        self.preview_next_button.configure(state=("normal" if current_index < len(images) - 1 else "disabled"))
        image = images[current_index]
        try:
            target_w = max(220, int(self.preview_image_frame.winfo_width() or 0) - 24)
            target_h = max(220, int(self.preview_image_frame.winfo_height() or 0) - 24)
        except Exception:
            target_w, target_h = (860, 620)
        fitted = ImageOps.contain(image.copy(), (target_w, target_h), method=Image.LANCZOS)
        ctk_image = ctk.CTkImage(light_image=fitted, dark_image=fitted, size=fitted.size)
        self.preview_ctk_image = ctk_image
        self.preview_image_label.configure(image=ctk_image, text="")
        self.preview_image_label.image = ctk_image

    def _goto_preview_prev(self):
        state = getattr(self, "preview_state", None) or {}
        current_index = int(state.get("index", 0) or 0)
        if current_index > 0:
            self._render_preview_page(current_index - 1)

    def _goto_preview_next(self):
        state = getattr(self, "preview_state", None) or {}
        images = list(state.get("images") or [])
        current_index = int(state.get("index", 0) or 0)
        if current_index < len(images) - 1:
            self._render_preview_page(current_index + 1)

    def _load_preview_worker(self, payload, request_id):
        try:
            link = payload.get("link") or ""
            cookie = payload.get("cookie") or ""
            ua = payload.get("ua") or DEFAULT_USER_AGENT
            preview_page_limit = 1 if get_site_domain_key(link) == "scanmanga" else PREVIEW_PAGE_LIMIT
            image_urls = list(
                get_images(
                    link,
                    cookie,
                    ua,
                    retries=2,
                    delay=1,
                    cancel_event=None,
                    max_images=preview_page_limit,
                    emit_logs=False,
                )
                or []
            )
            if not image_urls:
                raise ValueError("Aucune page récupérable pour cette preview.")
            pil_images = []
            for image_url in image_urls:
                if request_id != getattr(self, "preview_request_id", 0):
                    return
                pil_images.append(self._download_preview_pil_image(image_url, link, cookie, ua))
            payload = dict(payload)
            payload["urls"] = image_urls
            self.run_on_ui(self._apply_preview_result, payload, pil_images, request_id)
        except Exception as exc:
            self.run_on_ui(
                self._show_preview_error,
                payload.get("title") or "Prévisualisation",
                f"Impossible de charger la preview: {exc}",
                request_id,
            )

    def open_volume_preview_by_index(self, index):
        payload = self._get_preview_item_payload(index)
        if not payload:
            self.log("Prévisualisation indisponible pour cet élément.", level="warning")
            return
        if payload.get("premium"):
            self.log(
                f"Prévisualisation ignorée: {payload.get('title') or 'élément'} est premium.",
                level="warning",
            )
            self.toast("Preview indisponible: premium")
            return
        self.preview_request_id = int(getattr(self, "preview_request_id", 0) or 0) + 1
        request_id = self.preview_request_id
        cached = self._get_preview_cache_entry(payload["key"])
        if cached and cached.get("images"):
            self._stop_preview_spinner()
            self.preview_state = {
                "key": payload["key"],
                "title": cached.get("title") or payload["title"],
                "urls": list(cached.get("urls") or []),
                "images": list(cached.get("images") or []),
                "index": 0,
            }
            self._ensure_preview_window()
            self._render_preview_page(0)
            try:
                self.preview_window.deiconify()
                self.preview_window.lift()
                self.preview_window.focus_force()
            except Exception:
                pass
            return
        self.preview_state = {"key": payload["key"], "title": payload["title"], "urls": [], "images": [], "index": 0}
        self._show_preview_loading(payload["title"])
        threading.Thread(
            target=self._load_preview_worker,
            args=(payload, request_id),
            daemon=True,
            name=f"preview-{index}",
        ).start()

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
                label.configure(bg=self.palette["accent_soft"], fg=self.palette["text"])
            elif idx == active_index:
                label.configure(bg=self.palette["accent"], fg="#ffffff")
            else:
                label.configure(bg=self.palette["card_alt"], fg=self.palette["muted"])

        if hint_text and hasattr(self, "workflow_hint_label"):
            self.workflow_hint_label.configure(text=hint_text)

    def _show_volume_empty_state(self, text, tone="muted"):
        if not hasattr(self, "vol_empty_label"):
            return
        fg_map = {
            "muted": self.palette["muted"],
            "info": self.palette["accent_hover"],
            "warning": "#a16207",
            "error": self.palette["danger"],
        }
        safe_text = repair_mojibake_text(text or "")
        tone_color = fg_map.get(tone, self.palette["muted"])
        if self._is_ctk_widget(self.vol_empty_label):
            self.vol_empty_label.configure(text=safe_text, text_color=tone_color)
        else:
            self.vol_empty_label.configure(text=safe_text, fg=tone_color, bg=self.palette["canvas_bg"])
        host = getattr(self, "volume_list_container", None)
        if host is not None:
            self.vol_empty_label.place(in_=host, relx=0.5, rely=0.5, anchor="center")
            self.vol_empty_label.lift()
        else:
            self.vol_empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_volume_empty_state(self):
        if hasattr(self, "vol_empty_label"):
            self.vol_empty_label.place_forget()

    def _is_volume_visible(self, chk):
        try:
            return str(chk.winfo_manager()) == "grid"
        except Exception:
            return False

    def _scroll_volumes_to_top(self):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        try:
            canvas.yview_moveto(0)
        except Exception:
            pass

    def _toggle_volume_card(self, var):
        var.set(not bool(var.get()))
        self.update_master_toggle_button()

    def _cancel_pending_volume_render(self):
        after_id = getattr(self, "volume_render_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.volume_render_after_id = None
        self._cancel_virtual_volume_refresh()
        self.volume_render_token = getattr(self, "volume_render_token", 0) + 1

    def _get_volume_viewport_width(self):
        candidates = [
            getattr(getattr(self, "vol_frame", None), "_parent_canvas", None),
            getattr(self, "volume_list_container", None),
            getattr(self, "vol_frame", None),
            getattr(self, "root", None),
        ]
        widths = []
        for widget in candidates:
            if widget is None:
                continue
            try:
                width = int(widget.winfo_width() or 0)
            except Exception:
                width = 0
            try:
                req_width = int(widget.winfo_reqwidth() or 0)
            except Exception:
                req_width = 0
            if width > 0:
                widths.append(width)
            if req_width > 0:
                widths.append(req_width)
        usable = [value for value in widths if value >= 320]
        if usable:
            return max(usable)
        return max(widths) if widths else 960

    def _should_use_fast_volume_widgets(self, total_items=None):
        if total_items is None:
            total_items = len(getattr(self, "pairs", []) or [])
        return total_items >= VOLUME_FAST_WIDGET_THRESHOLD

    def _get_volume_grid_columns(self, total_items):
        compact = self._should_use_compact_volume_mode(total_items)
        width = self._get_volume_viewport_width()
        layout_mode = (getattr(self, "volume_layout_mode", None).get() if hasattr(self, "volume_layout_mode") else "Auto") or "Auto"
        layout_mode = str(layout_mode).strip().lower()
        if compact:
            columns = max(1, int((width + 24) // 250))
            if layout_mode == "dense":
                columns = max(3, min(4, columns))
            else:
                columns = max(2, min(4, columns))
            return max(1, min(total_items or 1, columns))
        columns = max(1, int((width + 28) // 340))
        columns = max(2, min(4, columns))
        return max(1, min(total_items or 1, columns))

    def _get_volume_render_batch_size(self, total_items):
        if self._should_use_fast_volume_widgets(total_items):
            return max(1, total_items)
        if total_items <= 120:
            return max(1, total_items)
        if self._should_use_compact_volume_mode(total_items):
            if total_items >= 900:
                return 108
            if total_items >= 240:
                return 72
        return VOLUME_RENDER_BATCH_SIZE

    def _should_virtualize_volume_mode(self, total_items=None):
        if total_items is None:
            total_items = len(getattr(self, "pairs", []) or [])
        return int(total_items or 0) > 0

    def _get_volume_layout_mode_name(self):
        mode = (getattr(self, "volume_layout_mode", None).get() if hasattr(self, "volume_layout_mode") else "Dense") or "Dense"
        return str(mode).strip().title() or "Dense"

    def _get_volume_grid_column_width(self, columns, kind=None):
        kind = kind or self._get_virtual_volume_pool_kind()
        viewport_width = max(320, int(self._get_volume_viewport_width() or 960))
        if kind == "card":
            target = VOLUME_CARD_COLUMN_WIDTH
            minimum = 250
        elif kind == "compact":
            target = VOLUME_COMPACT_COLUMN_WIDTH
            minimum = 210
        else:
            target = VOLUME_FAST_COLUMN_WIDTH
            minimum = 210
        usable_width = max(minimum, viewport_width - 28)
        adaptive_width = max(minimum, int((usable_width / max(1, columns)) - 18))
        return min(target, adaptive_width)

    def _configure_volume_grid_columns(self, columns, kind=None):
        if not hasattr(self, "vol_frame"):
            return
        kind = kind or self._get_virtual_volume_pool_kind()
        column_width = self._get_volume_grid_column_width(columns, kind=kind)
        try:
            self.vol_frame.grid_anchor("n")
        except Exception:
            pass
        for col in range(VOLUME_MAX_GRID_COLUMNS):
            self.vol_frame.grid_columnconfigure(col, weight=0, minsize=0, uniform="")
        for col in range(columns):
            self.vol_frame.grid_columnconfigure(col, weight=0, minsize=column_width, uniform="volume_grid")

    def _get_centered_volume_grid_position(self, visible_index, total_visible, columns, row_offset=0, absolute_visible_index=None):
        if columns <= 1:
            return row_offset + visible_index, 0
        if absolute_visible_index is None:
            absolute_visible_index = visible_index
        row = visible_index // columns
        col = visible_index % columns
        remainder = total_visible % columns
        full_rows = total_visible // columns
        absolute_row = absolute_visible_index // columns
        if remainder and absolute_row == full_rows:
            start_col = max(0, (columns - remainder) // 2)
            col = start_col + (absolute_visible_index % columns)
        return row_offset + row, col

    def _volume_group_label_from_text(self, label):
        match = re.match(
            r"(?i)^\s*((?:Tome|Webtoon)\s+[0-9]+(?:[.,][0-9]+)?(?:\s*[A-Za-z])?)\s+-\s+",
            str(label or ""),
        )
        if not match:
            return ""
        return normalize_tome_label(match.group(1))

    def _compact_display_label(self, label):
        text = str(label or "").strip()
        match = re.match(
            r"(?i)^\s*(Tome|Webtoon)\s+([0-9]+(?:[.,][0-9]+)?(?:\s*[A-Za-z])?)\s+-\s+Chap\s+(.+?)\s*$",
            text,
        )
        if match:
            prefix = "W" if match.group(1).lower() == "webtoon" else "T"
            tome = match.group(2).replace(",", ".").replace(" ", "")
            chapter = match.group(3).strip()
            chapter = chapter.split(" : ", 1)[0].strip()
            chapter = re.sub(r"(?i)^Extra\b", "Ex", chapter).strip()
            chapter = re.sub(r"(?i)^Ex\s+", "Ex", chapter)
            return f"{prefix}{tome} C{chapter}"
        return text

    def _should_group_volume_display(self):
        total = len(getattr(self, "pairs", []) or [])
        if total <= 0 or total > VOLUME_GROUP_HEADER_MAX_ITEMS:
            return False
        current_url = ""
        if hasattr(self, "url"):
            try:
                current_url = self.url.get()
            except Exception:
                current_url = ""
        if self.get_domain_from_url(current_url) != "scanmanga":
            return False
        groups = [self._volume_group_label_from_text(label) for label, _link in getattr(self, "pairs", []) or []]
        return len({group for group in groups if group}) >= 2

    def _create_volume_group_header(self, parent, group_label):
        frame = ctk.CTkFrame(parent, fg_color=self.palette["card_alt"], corner_radius=4, border_width=1, border_color=self.palette["border"])
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=group_label,
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 11),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        return frame

    def _get_volume_group_header(self, group_label):
        headers = getattr(self, "volume_group_header_widgets", None)
        if headers is None:
            headers = {}
            self.volume_group_header_widgets = headers
        if group_label not in headers or not headers[group_label].winfo_exists():
            headers[group_label] = self._create_volume_group_header(self.vol_frame, group_label)
        return headers[group_label]

    def _grid_grouped_volume_widgets(self, visible_indices, columns, kind):
        for header in (getattr(self, "volume_group_header_widgets", {}) or {}).values():
            try:
                header.grid_remove()
            except Exception:
                pass
        for chk, _label in getattr(self, "check_items", []) or []:
            try:
                chk.grid_remove()
            except Exception:
                pass

        row = 0
        col = 0
        last_group = None
        for absolute_index in visible_indices:
            if absolute_index >= len(getattr(self, "check_items", []) or []):
                continue
            label = self.pairs[absolute_index][0] if absolute_index < len(getattr(self, "pairs", []) or []) else ""
            group = self._volume_group_label_from_text(label)
            if group and group != last_group:
                if col:
                    row += 1
                    col = 0
                header = self._get_volume_group_header(group)
                header.grid(row=row, column=0, columnspan=max(1, columns), sticky="ew", padx=10, pady=(10 if row else 4, 4))
                row += 1
                last_group = group
            chk, _label = self.check_items[absolute_index]
            self._grid_virtual_volume_pool_item(chk, kind, row, col)
            col += 1
            if col >= columns:
                col = 0
                row += 1

    def _cancel_virtual_volume_refresh(self):
        after_id = getattr(self, "volume_virtual_refresh_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.volume_virtual_refresh_after_id = None
        self.volume_last_canvas_yview = None

    def _on_volume_canvas_scroll_activity(self, _event=None):
        if not getattr(self, "volume_virtualized", False):
            return
        self._schedule_virtual_volume_refresh(delay_ms=1)

    def _on_volume_canvas_yview(self, first, last):
        callback = getattr(self, "volume_canvas_scrollbar_set", None)
        if callable(callback):
            try:
                callback(first, last)
            except Exception:
                pass
        if not getattr(self, "volume_virtualized", False):
            return
        try:
            current_yview = (round(float(first), 6), round(float(last), 6))
        except Exception:
            current_yview = None
        if current_yview != getattr(self, "volume_last_canvas_yview", None):
            self._schedule_virtual_volume_refresh(delay_ms=1)

    def _on_volume_scrollbar_command(self, *args):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        try:
            canvas.yview(*args)
        except Exception:
            return
        self._schedule_virtual_volume_refresh(delay_ms=1)

    def _on_volume_mousewheel(self, event):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        container = getattr(self, "volume_list_container", None)
        if canvas is None or container is None:
            return
        try:
            root_x = int(self.root.winfo_pointerx())
            root_y = int(self.root.winfo_pointery())
            left = int(container.winfo_rootx())
            top = int(container.winfo_rooty())
            right = left + int(container.winfo_width() or 0)
            bottom = top + int(container.winfo_height() or 0)
        except Exception:
            return
        if not (left <= root_x <= right and top <= root_y <= bottom):
            return
        delta = 0
        if hasattr(event, "delta") and int(event.delta or 0):
            delta = -1 * int(event.delta / 120) if int(event.delta) % 120 == 0 else (-1 if event.delta > 0 else 1)
        elif getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        if delta == 0:
            return "break"
        try:
            canvas.yview_scroll(delta, "units")
        except Exception:
            return "break"
        self._schedule_virtual_volume_refresh(delay_ms=1)
        return "break"

    def _on_volume_canvas_configure(self, _event=None):
        self._update_volume_canvas_window()
        if getattr(self, "volume_virtualized", False):
            self._schedule_virtual_volume_refresh(delay_ms=1)
        else:
            self._update_volume_canvas_scrollregion()

    def _on_volume_frame_configure(self, _event=None):
        if getattr(self, "volume_virtualized", False):
            return
        self._update_volume_canvas_scrollregion()

    def _update_volume_canvas_window(self, top_offset=None):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        window_id = getattr(self, "volume_canvas_window_id", None)
        if canvas is None or window_id is None:
            return
        try:
            width = max(1, int(canvas.winfo_width() or 1))
            canvas.itemconfigure(window_id, width=width)
            current_coords = canvas.coords(window_id) or [0, 0]
            y_value = float(current_coords[1]) if len(current_coords) > 1 else 0.0
            if top_offset is not None:
                y_value = max(0.0, float(top_offset))
            canvas.coords(window_id, 0.0, y_value)
        except Exception:
            pass

    def _update_volume_canvas_scrollregion(self, total_height=None):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        try:
            canvas_width = max(1, int(canvas.winfo_width() or 1))
            canvas_height = max(1, int(canvas.winfo_height() or 1))
            content_height = int(total_height or 0)
            if content_height <= 0 and hasattr(self, "vol_frame"):
                try:
                    self.root.update_idletasks()
                except Exception:
                    pass
                content_height = max(canvas_height, int(self.vol_frame.winfo_reqheight() or 0))
            content_height = max(canvas_height, content_height)
            canvas.configure(scrollregion=(0, 0, canvas_width, content_height))
        except Exception:
            pass

    def _use_canvas_volume_pool(self, kind=None):
        kind = kind or self._get_virtual_volume_pool_kind()
        return kind in {"fast", "compact", "card"}

    def _set_volume_canvas_render_mode(self, enabled):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        window_id = getattr(self, "volume_canvas_window_id", None)
        if canvas is not None and window_id is not None:
            try:
                canvas.itemconfigure(window_id, state=("hidden" if enabled else "normal"))
            except Exception:
                pass
        self.volume_canvas_render_active = bool(enabled)
        if not enabled:
            self._hide_canvas_volume_pool()
            self._hide_canvas_volume_headers()

    def _hide_canvas_volume_pool(self):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        for entry in getattr(self, "volume_canvas_item_pool", []) or []:
            entry["absolute_index"] = None
            entry["visible_position"] = None
            for item_id in entry.get("item_ids", ()):
                try:
                    canvas.itemconfigure(item_id, state="hidden")
                except Exception:
                    pass

    def _hide_canvas_volume_headers(self):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        for entry in getattr(self, "volume_canvas_header_pool", []) or []:
            entry["group_label"] = None
            for item_id in entry.get("item_ids", ()):
                try:
                    canvas.itemconfigure(item_id, state="hidden")
                except Exception:
                    pass

    def _create_canvas_volume_header_entry(self, slot_index):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return None
        tag = f"volume_canvas_header_{slot_index}"
        bg_id = canvas.create_rectangle(
            0,
            0,
            0,
            0,
            width=1,
            outline=self.palette["border"],
            fill=self.palette["card_alt"],
            state="hidden",
            tags=(tag, "volume_canvas_header"),
        )
        text_id = canvas.create_text(
            0,
            0,
            text="",
            anchor="w",
            fill=self.palette["text"],
            font=("Segoe UI Semibold", 11),
            state="hidden",
            tags=(tag, "volume_canvas_header"),
        )
        return {
            "slot_index": slot_index,
            "group_label": None,
            "item_ids": (bg_id, text_id),
            "bg_id": bg_id,
            "text_id": text_id,
        }

    def _ensure_canvas_volume_header_pool(self, capacity):
        pool = getattr(self, "volume_canvas_header_pool", None)
        if pool is None:
            pool = []
            self.volume_canvas_header_pool = pool
        while len(pool) < capacity:
            entry = self._create_canvas_volume_header_entry(len(pool))
            if entry is None:
                break
            pool.append(entry)
        return pool

    def _render_canvas_volume_header(self, entry, group_label, row_index, columns, metrics):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None or entry is None:
            return
        width = (columns * metrics["card_width"]) + max(0, columns - 1) * metrics["gap_x"]
        x1 = metrics["left_margin"]
        x2 = x1 + width
        y1 = (row_index * metrics["row_height"]) + 5
        y2 = y1 + 34
        try:
            canvas.coords(entry["bg_id"], x1, y1, x2, y2)
            canvas.itemconfigure(entry["bg_id"], state="normal")
            canvas.coords(entry["text_id"], x1 + 10, (y1 + y2) / 2.0)
            canvas.itemconfigure(entry["text_id"], text=group_label, state="normal")
            entry["group_label"] = group_label
        except Exception:
            pass

    def _create_canvas_volume_pool_entry(self, slot_index):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return None
        slot_tag = f"volume_canvas_slot_{slot_index}"
        bg_id = canvas.create_rectangle(0, 0, 0, 0, width=1, outline=self.palette["panel_shell"], fill=self.palette["card_bg"], state="hidden", tags=(slot_tag, "volume_canvas_card"))
        index_bg_id = canvas.create_rectangle(0, 0, 0, 0, width=0, outline="", fill=self.palette["card_alt"], state="hidden", tags=(slot_tag, "volume_canvas_index_bg"))
        index_text_id = canvas.create_text(0, 0, text="", anchor="center", fill=self.palette["muted"], font=("Segoe UI Semibold", 9), state="hidden", tags=(slot_tag, "volume_canvas_index_text"))
        title_id = canvas.create_text(0, 0, text="", anchor="w", fill=self.palette["text"], font=("Segoe UI", 10), state="hidden", tags=(slot_tag, "volume_canvas_title"))
        premium_bg_id = canvas.create_rectangle(0, 0, 0, 0, width=1, outline="#d6b44d", fill="#f4dd8c", state="hidden", tags=(slot_tag, "volume_canvas_premium_bg"))
        premium_text_id = canvas.create_text(0, 0, text="$", anchor="center", fill="#8f6500", font=("Segoe UI Semibold", 10), state="hidden", tags=(slot_tag, "volume_canvas_premium_text"))
        preview_tag = f"{slot_tag}_preview"
        preview_bg_id = canvas.create_rectangle(0, 0, 0, 0, width=1, outline="#a3cab1", fill="#dfeee5", state="hidden", tags=(preview_tag, "volume_canvas_preview"))
        preview_text_id = canvas.create_text(0, 0, text="⌕", anchor="center", fill="#2f6d4a", font=("Segoe UI Semibold", 10), state="hidden", tags=(preview_tag, "volume_canvas_preview"))
        checkbox_bg_id = canvas.create_rectangle(0, 0, 0, 0, width=1, outline=self.palette["border"], fill=self.palette["canvas_bg"], state="hidden", tags=(slot_tag, "volume_canvas_checkbox_bg"))
        checkbox_check_id = canvas.create_text(0, 0, text="✓", anchor="center", fill="#ffffff", font=("Segoe UI Semibold", 11), state="hidden", tags=(slot_tag, "volume_canvas_checkbox_check"))
        entry = {
            "slot_index": slot_index,
            "slot_tag": slot_tag,
            "preview_tag": preview_tag,
            "absolute_index": None,
            "item_ids": (bg_id, index_bg_id, index_text_id, title_id, premium_bg_id, premium_text_id, preview_bg_id, preview_text_id, checkbox_bg_id, checkbox_check_id),
            "bg_id": bg_id,
            "index_bg_id": index_bg_id,
            "index_text_id": index_text_id,
            "title_id": title_id,
            "premium_bg_id": premium_bg_id,
            "premium_text_id": premium_text_id,
            "preview_bg_id": preview_bg_id,
            "preview_text_id": preview_text_id,
            "checkbox_bg_id": checkbox_bg_id,
            "checkbox_check_id": checkbox_check_id,
        }
        canvas.tag_bind(slot_tag, "<Button-1>", lambda _event, slot=slot_index: self._on_canvas_volume_slot_click(slot))
        canvas.tag_bind(preview_tag, "<Button-1>", lambda _event, slot=slot_index: self._on_canvas_volume_preview_click(slot))
        return entry

    def _ensure_canvas_volume_pool(self, capacity):
        pool = getattr(self, "volume_canvas_item_pool", None)
        if pool is None:
            pool = []
            self.volume_canvas_item_pool = pool
        while len(pool) < capacity:
            entry = self._create_canvas_volume_pool_entry(len(pool))
            if entry is None:
                break
            pool.append(entry)
        return pool

    def _get_canvas_volume_item_metrics(self, columns, kind):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        try:
            canvas_width = int(canvas.winfo_width() or 0) if canvas is not None else 0
        except Exception:
            canvas_width = 0
        viewport_width = max(320, canvas_width or int(self._get_volume_viewport_width() or 960))
        gap_x = 18
        top_padding = 6
        if kind == "fast":
            row_height = 36
            card_height = 26
            usable_width = max(210, viewport_width - 32)
            card_width = min(250, max(210, int((usable_width - ((columns - 1) * gap_x)) / max(1, columns))))
        elif kind == "card":
            row_height = VOLUME_VIRTUAL_ROW_HEIGHT_CARD
            card_height = 46
            usable_width = max(260, viewport_width - 32)
            adaptive_width = max(260, int((usable_width - ((columns - 1) * gap_x)) / max(1, columns)))
            card_width = min(VOLUME_CARD_COLUMN_WIDTH, adaptive_width)
        else:
            row_height = VOLUME_VIRTUAL_ROW_HEIGHT_COMPACT
            card_height = 28
            usable_width = max(220, viewport_width - 32)
            adaptive_width = max(220, int((usable_width - ((columns - 1) * gap_x)) / max(1, columns)))
            card_width = min(VOLUME_COMPACT_COLUMN_WIDTH, adaptive_width)
        used_width = (columns * card_width) + max(0, columns - 1) * gap_x
        left_margin = max(12, int((viewport_width - used_width) / 2))
        return {
            "card_width": card_width,
            "card_height": card_height,
            "row_height": row_height,
            "gap_x": gap_x,
            "top_padding": top_padding,
            "left_margin": left_margin,
        }

    def _render_canvas_volume_entry(self, entry, absolute_index, visible_position, total_visible, columns, metrics, kind, grid_row=None, grid_col=None):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        vol, _link = self.pairs[absolute_index]
        var = self.check_vars[absolute_index]
        if grid_row is None or grid_col is None:
            row, col = self._get_centered_volume_grid_position(
                visible_position,
                total_visible,
                columns,
                absolute_visible_index=visible_position,
            )
        else:
            row, col = int(grid_row), int(grid_col)
        card_x = metrics["left_margin"] + (col * (metrics["card_width"] + metrics["gap_x"]))
        card_y = (row * metrics["row_height"]) + metrics["top_padding"]
        card_x2 = card_x + metrics["card_width"]
        card_y2 = card_y + metrics["card_height"]
        selected = bool(var.get())
        bg_fill = "#f8fbff" if selected else self.palette["card_bg"]
        bg_outline = "#a9bfd9" if selected else self.palette["panel_shell"]
        badge_fill = self.palette["accent_soft"] if selected else self.palette["card_alt"]
        badge_text = self.palette["accent_hover"] if selected else self.palette["muted"]
        checkbox_fill = self.palette["accent"] if selected else self.palette["canvas_bg"]
        checkbox_outline = self.palette["accent"] if selected else self.palette["border"]
        premium = self.is_volume_premium(index=absolute_index)
        display_vol = self._compact_display_label(vol)
        title_text = f"{absolute_index + 1}. {display_vol}" if kind == "fast" else str(display_vol)
        index_text = "" if kind == "fast" else str(absolute_index + 1)
        title_x = card_x + 14
        if kind != "fast":
            index_x1 = card_x + 10
            index_x2 = index_x1 + 26
            index_y1 = card_y + 3
            index_y2 = index_y1 + 20
            title_x = index_x2 + 10
            canvas.coords(entry["index_bg_id"], index_x1, index_y1, index_x2, index_y2)
            canvas.itemconfigure(entry["index_bg_id"], fill=badge_fill, state="normal")
            canvas.coords(entry["index_text_id"], (index_x1 + index_x2) / 2.0, (index_y1 + index_y2) / 2.0)
            canvas.itemconfigure(entry["index_text_id"], text=index_text, fill=badge_text, state="normal")
        else:
            canvas.itemconfigure(entry["index_bg_id"], state="hidden")
            canvas.itemconfigure(entry["index_text_id"], state="hidden")
        checkbox_size = 18 if kind == "fast" else 20
        checkbox_x2 = card_x2 - 12
        checkbox_x1 = checkbox_x2 - checkbox_size
        preview_size = 18 if kind == "fast" else 20
        preview_x2 = checkbox_x1 - 8
        preview_x1 = preview_x2 - preview_size
        premium_size = 18 if kind == "fast" else 20
        premium_x2 = preview_x1 - 8
        premium_x1 = premium_x2 - premium_size
        checkbox_y1 = card_y + int((metrics["card_height"] - checkbox_size) / 2)
        checkbox_y2 = checkbox_y1 + checkbox_size
        canvas.coords(entry["bg_id"], card_x, card_y, card_x2, card_y2)
        canvas.itemconfigure(entry["bg_id"], fill=bg_fill, outline=bg_outline, state="normal")
        canvas.coords(entry["title_id"], title_x, (card_y + card_y2) / 2.0)
        title_limit_x = premium_x1 if premium else preview_x1
        canvas.itemconfigure(entry["title_id"], text=title_text, fill=self.palette["text"], width=max(64, title_limit_x - title_x - 8), state="normal")
        if premium:
            canvas.coords(entry["premium_bg_id"], premium_x1, checkbox_y1, premium_x2, checkbox_y2)
            canvas.itemconfigure(entry["premium_bg_id"], state="normal")
            canvas.coords(entry["premium_text_id"], (premium_x1 + premium_x2) / 2.0, (checkbox_y1 + checkbox_y2) / 2.0)
            canvas.itemconfigure(entry["premium_text_id"], state="normal")
        else:
            canvas.itemconfigure(entry["premium_bg_id"], state="hidden")
            canvas.itemconfigure(entry["premium_text_id"], state="hidden")
        canvas.coords(entry["preview_bg_id"], preview_x1, checkbox_y1, preview_x2, checkbox_y2)
        canvas.itemconfigure(entry["preview_bg_id"], state="normal")
        canvas.coords(entry["preview_text_id"], (preview_x1 + preview_x2) / 2.0, (checkbox_y1 + checkbox_y2) / 2.0)
        canvas.itemconfigure(entry["preview_text_id"], state="normal")
        canvas.coords(entry["checkbox_bg_id"], checkbox_x1, checkbox_y1, checkbox_x2, checkbox_y2)
        canvas.itemconfigure(entry["checkbox_bg_id"], fill=checkbox_fill, outline=checkbox_outline, state="normal")
        canvas.coords(entry["checkbox_check_id"], (checkbox_x1 + checkbox_x2) / 2.0, (checkbox_y1 + checkbox_y2) / 2.0)
        canvas.itemconfigure(entry["checkbox_check_id"], state=("normal" if selected else "hidden"))
        entry["absolute_index"] = absolute_index
        entry["visible_position"] = visible_position

    def _refresh_canvas_volume_entry_style(self, entry):
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None or not entry:
            return
        absolute_index = entry.get("absolute_index")
        if absolute_index is None or absolute_index >= len(getattr(self, "check_vars", []) or []):
            return
        selected = bool(self.check_vars[absolute_index].get())
        bg_fill = "#f8fbff" if selected else self.palette["card_bg"]
        bg_outline = "#a9bfd9" if selected else self.palette["panel_shell"]
        badge_fill = self.palette["accent_soft"] if selected else self.palette["card_alt"]
        badge_text = self.palette["accent_hover"] if selected else self.palette["muted"]
        checkbox_fill = self.palette["accent"] if selected else self.palette["canvas_bg"]
        checkbox_outline = self.palette["accent"] if selected else self.palette["border"]
        try:
            canvas.itemconfigure(entry["bg_id"], fill=bg_fill, outline=bg_outline)
            canvas.itemconfigure(entry["checkbox_bg_id"], fill=checkbox_fill, outline=checkbox_outline)
            canvas.itemconfigure(entry["checkbox_check_id"], state=("normal" if selected else "hidden"))
            if canvas.itemcget(entry["index_bg_id"], "state") != "hidden":
                canvas.itemconfigure(entry["index_bg_id"], fill=badge_fill)
                canvas.itemconfigure(entry["index_text_id"], fill=badge_text)
        except Exception:
            pass

    def _refresh_canvas_volume_pool_entry(self, entry):
        if not entry:
            return
        absolute_index = entry.get("absolute_index")
        if absolute_index is None:
            return
        self._refresh_canvas_volume_entry_style(entry)

    def _on_canvas_volume_slot_click(self, slot_index):
        pool = getattr(self, "volume_canvas_item_pool", None) or []
        if slot_index >= len(pool):
            return
        entry = pool[slot_index]
        absolute_index = entry.get("absolute_index")
        if absolute_index is None or absolute_index >= len(getattr(self, "check_vars", []) or []):
            return
        var = self.check_vars[absolute_index]
        var.set(not bool(var.get()))
        self._refresh_canvas_volume_entry_style(entry)
        self.update_master_toggle_button()

    def _on_canvas_volume_preview_click(self, slot_index):
        pool = getattr(self, "volume_canvas_item_pool", None) or []
        if slot_index >= len(pool):
            return "break"
        entry = pool[slot_index]
        absolute_index = entry.get("absolute_index")
        if absolute_index is None:
            return "break"
        self.open_volume_preview_by_index(absolute_index)
        return "break"

    def _refresh_canvas_volume_card_styles(self):
        if not getattr(self, "volume_canvas_render_active", False):
            return
        for entry in getattr(self, "volume_canvas_item_pool", []) or []:
            if entry.get("absolute_index") is not None:
                self._refresh_canvas_volume_entry_style(entry)

    def _cancel_volume_pool_prewarm(self):
        after_id = getattr(self, "volume_pool_prewarm_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.volume_pool_prewarm_after_id = None
        self.volume_pool_prewarm_kind = None

    def _get_volume_pool_kind_columns(self, total_items, kind):
        width = self._get_volume_viewport_width()
        if kind == "card":
            columns = max(1, int((width + 28) // 340))
            columns = max(2, min(4, columns))
        else:
            columns = max(1, int((width + 24) // 250))
            columns = max(2, min(4, columns))
        return max(1, min(total_items or 1, columns))

    def _get_virtual_pool_target_size(self, kind, total_items=None):
        total_items = len(getattr(self, "pairs", []) or []) if total_items is None else int(total_items or 0)
        if total_items <= 0:
            return 0
        columns = self._get_volume_pool_kind_columns(total_items, kind)
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        try:
            canvas_height = max(1, int(canvas.winfo_height() or 1)) if canvas is not None else 720
        except Exception:
            canvas_height = 720
        if kind == "card":
            row_height = VOLUME_VIRTUAL_ROW_HEIGHT_CARD
            min_visible_rows = 6
        elif kind == "compact":
            row_height = VOLUME_VIRTUAL_ROW_HEIGHT_COMPACT
            min_visible_rows = 8
        else:
            row_height = 36
            min_visible_rows = 8
        visible_rows = max(min_visible_rows, int(math.ceil(canvas_height / float(max(1, row_height)))))
        target_rows = visible_rows + VOLUME_VIRTUALIZATION_BUFFER_ROWS
        return min(total_items, max(columns, columns * target_rows))

    def _get_volume_pool_kind_for_layout_mode(self, mode_name, total_items=None):
        total_items = len(getattr(self, "pairs", []) or []) if total_items is None else int(total_items or 0)
        if self._should_use_fast_volume_widgets(total_items):
            return "fast"
        mode_name = (mode_name or "Auto").strip().title()
        if mode_name == "Confort":
            return "card"
        if mode_name == "Dense":
            return "compact"
        return "compact" if total_items >= VOLUME_COMPACT_MODE_THRESHOLD else "card"

    def _is_volume_pool_ready_for_layout_mode(self, mode_name, total_items=None):
        kind = self._get_volume_pool_kind_for_layout_mode(mode_name, total_items=total_items)
        if self._use_canvas_volume_pool(kind):
            return True
        target_size = self._get_virtual_pool_target_size(kind, total_items=total_items)
        pools = getattr(self, "volume_virtual_widget_pools", None) or {}
        return len(pools.get(kind) or []) >= target_size

    def _schedule_volume_pool_prewarm(self, kind):
        self._cancel_volume_pool_prewarm()
        if getattr(self, "analysis_in_progress", False):
            return
        if not getattr(self, "volume_virtualized", False):
            return
        if self._use_canvas_volume_pool(kind):
            return
        total_items = len(getattr(self, "pairs", []) or [])
        if total_items <= 0:
            return
        target_size = self._get_virtual_pool_target_size(kind, total_items=total_items)
        pools = getattr(self, "volume_virtual_widget_pools", None)
        if pools is None:
            pools = {}
            self.volume_virtual_widget_pools = pools
        pool = pools.get(kind) or []
        pools[kind] = pool
        if len(pool) >= target_size:
            return
        self.volume_pool_prewarm_kind = kind

        def tick():
            if self.volume_pool_prewarm_kind != kind:
                return
            current_pools = getattr(self, "volume_virtual_widget_pools", None) or {}
            current_pool = current_pools.get(kind) or []
            batch_size = 6 if kind == "card" else 8
            remaining = max(0, target_size - len(current_pool))
            for _ in range(min(batch_size, remaining)):
                widget = self._create_virtual_volume_pool_item(kind)
                try:
                    widget.grid_remove()
                except Exception:
                    pass
                current_pool.append(widget)
            current_pools[kind] = current_pool
            self.volume_virtual_widget_pools = current_pools
            if len(current_pool) < target_size:
                self.volume_pool_prewarm_after_id = self.root.after(1, tick)
                return
            self.volume_pool_prewarm_after_id = None
            self.volume_pool_prewarm_kind = None
            pending_mode = getattr(self, "volume_layout_pending_mode", None)
            if pending_mode and self._get_volume_pool_kind_for_layout_mode(pending_mode, total_items=total_items) == kind:
                self.volume_layout_pending_mode = None
                self.root.after(1, lambda mode=pending_mode: self._on_volume_layout_mode_change(mode, allow_deferred=False))

        self.volume_pool_prewarm_after_id = self.root.after(1, tick)

    def _schedule_volume_layout_refresh(self, _event=None):
        if getattr(self, "analysis_in_progress", False):
            return
        if not getattr(self, "pairs", None):
            return
        after_id = getattr(self, "volume_layout_refresh_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.volume_layout_refresh_after_id = self.root.after(90, self._process_volume_layout_refresh)

    def _process_volume_layout_refresh(self):
        self.volume_layout_refresh_after_id = None
        if getattr(self, "analysis_in_progress", False):
            return
        total = len(getattr(self, "pairs", []) or [])
        if total <= 0:
            return
        new_columns = self._get_volume_grid_columns(total)
        if new_columns == int(getattr(self, "volume_grid_columns", 0) or 0):
            return
        self.volume_grid_columns = new_columns
        if getattr(self, "volume_virtualized", False):
            self.volume_virtual_window = None
            self.volume_grouped_virtual_rows_cache_key = None
            self.volume_grouped_virtual_rows_cache = []
            self._refresh_virtualized_volume_view(force=True, reset_scroll=False)
        else:
            self.apply_filter()

    def _get_active_volume_filter_text(self):
        if getattr(self, "filter_placeholder_active", False):
            return ""
        raw_value = getattr(self, "filter_text", None).get() if hasattr(self, "filter_text") else ""
        return str(raw_value or "").strip().lower()

    def _volume_matches_filter(self, label_lower, raw):
        return (
            not raw
            or raw in label_lower
            or (raw.endswith("*") and raw[:-1].isdigit() and label_lower.startswith(raw[:-1]))
        )

    def _compute_filtered_volume_indices(self, raw_filter=None):
        raw = self._get_active_volume_filter_text() if raw_filter is None else (raw_filter or "").strip().lower()
        labels = getattr(self, "volume_label_cache_lower", None) or [str(vol or "").lower() for vol, _link in getattr(self, "pairs", []) or []]
        return [
            index for index, label in enumerate(labels)
            if self._volume_matches_filter(label, raw)
        ]

    def _schedule_virtual_volume_refresh(self, delay_ms=60, force=False, reset_scroll=False):
        if not getattr(self, "volume_virtualized", False):
            return
        self._pending_virtual_volume_force = bool(force)
        self._pending_virtual_volume_reset_scroll = bool(reset_scroll)
        after_id = getattr(self, "volume_virtual_refresh_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        if delay_ms <= 1:
            now = time.perf_counter()
            last_refresh = float(getattr(self, "volume_virtual_last_refresh_at", 0.0) or 0.0)
            throttle_seconds = max(0.001, VOLUME_VIRTUAL_REFRESH_THROTTLE_MS / 1000.0)
            wait_ms = 0 if now - last_refresh >= throttle_seconds else max(1, int((throttle_seconds - (now - last_refresh)) * 1000))
            self.volume_virtual_refresh_after_id = self.root.after(wait_ms, self._process_virtual_volume_refresh)
        else:
            self.volume_virtual_refresh_after_id = self.root.after(delay_ms, self._process_virtual_volume_refresh)

    def _process_virtual_volume_refresh(self):
        self.volume_virtual_refresh_after_id = None
        if not getattr(self, "volume_virtualized", False):
            return
        if getattr(self, "volume_virtual_refresh_processing", False):
            self._schedule_virtual_volume_refresh(delay_ms=VOLUME_VIRTUAL_REFRESH_THROTTLE_MS)
            return
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        self.volume_virtual_refresh_processing = True
        try:
            force = bool(getattr(self, "_pending_virtual_volume_force", False))
            reset_scroll = bool(getattr(self, "_pending_virtual_volume_reset_scroll", False))
            self._pending_virtual_volume_force = False
            self._pending_virtual_volume_reset_scroll = False
            try:
                current_yview = tuple(round(float(value), 6) for value in canvas.yview())
            except Exception:
                current_yview = None
            last_yview = getattr(self, "volume_last_canvas_yview", None)
            if force or reset_scroll or current_yview != last_yview:
                self._refresh_virtualized_volume_view(force=force, reset_scroll=reset_scroll)
                self.volume_virtual_last_refresh_at = time.perf_counter()
                try:
                    self.volume_last_canvas_yview = tuple(round(float(value), 6) for value in canvas.yview())
                except Exception:
                    self.volume_last_canvas_yview = current_yview
        finally:
            self.volume_virtual_refresh_processing = False

    def _get_virtual_volume_row_window(self, total_rows, canvas_height, row_height, scroll_top, min_visible_rows=6):
        visible_rows = max(min_visible_rows, int(math.ceil(canvas_height / float(max(1, row_height)))))
        clamped_top = max(0.0, float(scroll_top or 0.0))
        first_visible_row = int(clamped_top // float(max(1, row_height)))
        first_row = max(0, first_visible_row - VOLUME_VIRTUALIZATION_BUFFER_ROWS)
        bottom_visible_row = int(math.ceil((clamped_top + canvas_height) / float(max(1, row_height))))
        last_row = min(
            total_rows,
            max(first_visible_row + visible_rows, bottom_visible_row) + VOLUME_VIRTUALIZATION_BUFFER_ROWS,
        )
        return first_row, last_row, visible_rows

    def _build_grouped_volume_virtual_rows(self, filtered_indices, columns):
        cache_key = (int(columns or 1), tuple(filtered_indices or ()))
        if cache_key == getattr(self, "volume_grouped_virtual_rows_cache_key", None):
            return list(getattr(self, "volume_grouped_virtual_rows_cache", []) or [])
        rows = []
        current_group = None
        current_indices = []

        def flush_items():
            nonlocal current_indices
            while current_indices:
                rows.append({"type": "items", "indices": current_indices[:columns]})
                current_indices = current_indices[columns:]

        for absolute_index in filtered_indices:
            label = self.pairs[absolute_index][0] if absolute_index < len(getattr(self, "pairs", []) or []) else ""
            group_label = self._volume_group_label_from_text(label) or "Sans tome"
            if group_label != current_group:
                flush_items()
                rows.append({"type": "header", "group": group_label})
                current_group = group_label
            current_indices.append(absolute_index)
            if len(current_indices) >= columns:
                flush_items()
        flush_items()
        self.volume_grouped_virtual_rows_cache_key = cache_key
        self.volume_grouped_virtual_rows_cache = list(rows)
        return rows

    def _refresh_grouped_canvas_volume_view(self, filtered_indices, total_visible, columns, canvas, force=False, reset_scroll=False):
        if reset_scroll:
            try:
                canvas.yview_moveto(0)
            except Exception:
                pass

        kind = self._get_virtual_volume_pool_kind()
        if not self._use_canvas_volume_pool(kind):
            return False

        self._set_volume_canvas_render_mode(True)
        rows = self._build_grouped_volume_virtual_rows(filtered_indices, columns)
        total_rows = len(rows)
        if total_visible <= 0 or total_rows <= 0:
            self._hide_canvas_volume_pool()
            self._hide_canvas_volume_headers()
            self.check_items = []
            self.volume_index_to_widget = {}
            self.volume_virtual_window = None
            self._update_volume_canvas_window(0)
            self._update_volume_canvas_scrollregion(0)
            self._update_selection_status()
            self._refresh_volume_empty_state()
            return True

        metrics = self._get_canvas_volume_item_metrics(columns, kind)
        metrics["row_height"] = max(int(metrics.get("row_height") or 0), VOLUME_VIRTUAL_HEADER_ROW_HEIGHT)
        row_height = metrics["row_height"]
        canvas_height = max(1, int(canvas.winfo_height() or 1))
        scroll_top = max(0.0, float(canvas.canvasy(0)))
        first_row, last_row, _visible_rows = self._get_virtual_volume_row_window(
            total_rows,
            canvas_height,
            row_height,
            scroll_top,
            min_visible_rows=8,
        )
        window = (first_row, last_row, total_visible, columns, kind, "grouped")
        if not force and window == getattr(self, "volume_virtual_window", None):
            return True
        self.volume_virtual_window = window

        visible_rows = rows[first_row:last_row]
        item_count = sum(len(row.get("indices", ())) for row in visible_rows if row.get("type") == "items")
        header_count = sum(1 for row in visible_rows if row.get("type") == "header")
        item_pool = self._ensure_canvas_volume_pool(item_count)
        header_pool = self._ensure_canvas_volume_header_pool(header_count)
        self._update_volume_canvas_window(0)
        self._update_volume_canvas_scrollregion(total_rows * row_height)
        self.check_items = []
        self.volume_index_to_widget = {}

        item_slot = 0
        header_slot = 0
        for offset, row_data in enumerate(visible_rows):
            row_index = first_row + offset
            if row_data.get("type") == "header":
                if header_slot < len(header_pool):
                    self._render_canvas_volume_header(header_pool[header_slot], row_data.get("group") or "", row_index, columns, metrics)
                header_slot += 1
                continue
            for col, absolute_index in enumerate(row_data.get("indices") or []):
                if item_slot >= len(item_pool):
                    continue
                entry = item_pool[item_slot]
                self._render_canvas_volume_entry(
                    entry,
                    absolute_index,
                    absolute_index,
                    total_visible,
                    columns,
                    metrics,
                    kind,
                    grid_row=row_index,
                    grid_col=col,
                )
                self.volume_index_to_widget[absolute_index] = entry
                item_slot += 1

        for extra_entry in item_pool[item_slot:]:
            extra_entry["absolute_index"] = None
            extra_entry["visible_position"] = None
            for item_id in extra_entry.get("item_ids", ()):
                try:
                    canvas.itemconfigure(item_id, state="hidden")
                except Exception:
                    pass
        for extra_header in header_pool[header_slot:]:
            extra_header["group_label"] = None
            for item_id in extra_header.get("item_ids", ()):
                try:
                    canvas.itemconfigure(item_id, state="hidden")
                except Exception:
                    pass

        self._update_selection_status()
        self._refresh_volume_empty_state()
        return True

    def _refresh_pooled_virtualized_volume_view(self, filtered_indices, total_visible, columns, canvas, force=False, reset_scroll=False):
        if reset_scroll:
            try:
                canvas.yview_moveto(0)
            except Exception:
                pass

        kind = self._get_virtual_volume_pool_kind()
        use_canvas_pool = self._use_canvas_volume_pool(kind)
        self._set_volume_canvas_render_mode(use_canvas_pool)
        if getattr(self, "volume_grouped", False) and use_canvas_pool:
            handled = self._refresh_grouped_canvas_volume_view(
                filtered_indices,
                total_visible,
                columns,
                canvas,
                force=force,
                reset_scroll=reset_scroll,
            )
            if handled:
                return

        if total_visible <= 0:
            self._hide_canvas_volume_pool()
            self._hide_canvas_volume_headers()
            for pool in (getattr(self, "volume_virtual_widget_pools", None) or {}).values():
                for widget in pool:
                    try:
                        widget.grid_remove()
                    except Exception:
                        pass
            self.check_items = []
            self.volume_index_to_widget = {}
            self.volume_virtual_window = None
            self._update_volume_canvas_window(0)
            self._update_volume_canvas_scrollregion(0)
            self._update_selection_status()
            self._refresh_volume_empty_state()
            return

        total_rows = max(1, (total_visible + columns - 1) // columns)
        if not use_canvas_pool:
            self._configure_volume_grid_columns(columns, kind=kind)
        if kind == "fast":
            row_height = 36
        elif kind == "compact":
            row_height = VOLUME_VIRTUAL_ROW_HEIGHT_COMPACT
        else:
            row_height = VOLUME_VIRTUAL_ROW_HEIGHT_CARD
        canvas_height = max(1, int(canvas.winfo_height() or 1))
        scroll_top = max(0.0, float(canvas.canvasy(0)))
        first_row, last_row, _visible_rows = self._get_virtual_volume_row_window(
            total_rows,
            canvas_height,
            row_height,
            scroll_top,
            min_visible_rows=8,
        )
        window = (first_row, last_row, total_visible, columns, kind)

        if not force and window == getattr(self, "volume_virtual_window", None):
            return
        self.volume_virtual_window = window

        start_index = first_row * columns
        end_index = min(total_visible, last_row * columns)
        visible_slice = filtered_indices[start_index:end_index]
        self.check_items = []
        self.volume_index_to_widget = {}
        self._update_volume_canvas_scrollregion(total_rows * row_height)

        if use_canvas_pool:
            metrics = self._get_canvas_volume_item_metrics(columns, kind)
            pool = self._ensure_canvas_volume_pool(max(0, len(visible_slice)))
            for local_pos, absolute_index in enumerate(visible_slice):
                entry = pool[local_pos]
                self._render_canvas_volume_entry(entry, absolute_index, start_index + local_pos, total_visible, columns, metrics, kind)
                self.volume_index_to_widget[absolute_index] = entry
            for extra_entry in pool[len(visible_slice):]:
                extra_entry["absolute_index"] = None
                extra_entry["visible_position"] = None
                for item_id in extra_entry.get("item_ids", ()):
                    try:
                        canvas.itemconfigure(item_id, state="hidden")
                    except Exception:
                        pass
            self._hide_canvas_volume_headers()
            self._update_selection_status()
            self._refresh_volume_empty_state()
            return

        pools = getattr(self, "volume_virtual_widget_pools", None)
        if pools is None:
            pools = {}
            self.volume_virtual_widget_pools = pools
        current_pool_mode = getattr(self, "volume_virtual_widget_pool_mode", None)
        if current_pool_mode != kind:
            for widget in (pools.get(current_pool_mode, []) or []):
                try:
                    widget.grid_remove()
                except Exception:
                    pass
            self.volume_virtual_widget_pool_mode = kind
        pool = pools.get(kind)
        if pool is None:
            pool = []
            pools[kind] = pool
        self.volume_virtual_widget_pool = pool

        visible_capacity = max(0, (last_row - first_row) * columns)
        while len(pool) < visible_capacity:
            pool.append(self._create_virtual_volume_pool_item(kind))

        self._update_volume_canvas_window(first_row * row_height)

        for local_pos, absolute_index in enumerate(visible_slice):
            vol, _link = self.pairs[absolute_index]
            var = self.check_vars[absolute_index]
            chk = pool[local_pos]
            self._assign_virtual_volume_pool_item(chk, kind, vol, var, absolute_index)
            row, col = self._get_centered_volume_grid_position(
                local_pos,
                total_visible,
                columns,
                absolute_visible_index=start_index + local_pos,
            )
            self._grid_virtual_volume_pool_item(chk, kind, row, col)
            self.check_items.append((chk, vol))
            self.volume_index_to_widget[absolute_index] = chk

        for extra_widget in pool[len(visible_slice):]:
            try:
                extra_widget.grid_remove()
            except Exception:
                pass

        self._refresh_volume_card_styles()
        self._update_selection_status()
        self._refresh_volume_empty_state()

    def _refresh_virtualized_volume_view(self, force=False, reset_scroll=False):
        if not getattr(self, "volume_virtualized", False):
            return
        canvas = getattr(getattr(self, "vol_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        if reset_scroll:
            try:
                canvas.yview_moveto(0)
            except Exception:
                pass
        filtered_indices = list(getattr(self, "filtered_volume_indices", []) or [])
        total_visible = len(filtered_indices)
        columns = max(1, int(getattr(self, "volume_grid_columns", 1) or 1))

        self._refresh_pooled_virtualized_volume_view(
            filtered_indices,
            total_visible,
            columns,
            canvas,
            force=force,
            reset_scroll=reset_scroll,
        )
        return

        for col in range(columns):
            self.vol_frame.grid_columnconfigure(col, weight=1)

        if total_visible <= 0:
            for widget in self.vol_frame.winfo_children():
                widget.destroy()
            self.check_items = []
            self.volume_index_to_widget = {}
            self.volume_virtual_window = None
            self._update_selection_status()
            self._refresh_volume_empty_state()
            return

        try:
            self.root.update_idletasks()
        except Exception:
            pass

        total_rows = max(1, (total_visible + columns - 1) // columns)
        row_height = (
            VOLUME_VIRTUAL_ROW_HEIGHT_COMPACT
            if bool(getattr(self, "use_compact_volume_mode", False))
            else VOLUME_VIRTUAL_ROW_HEIGHT_CARD
        )
        start_frac, end_frac = canvas.yview()
        canvas_height = max(1, int(canvas.winfo_height() or 1))
        first_row, last_row, _visible_rows = self._get_virtual_volume_row_window(
            total_rows,
            canvas_height,
            row_height,
            start_frac,
            end_frac,
            min_visible_rows=6,
        )
        window = (first_row, last_row, total_visible, columns)

        if not force and window == getattr(self, "volume_virtual_window", None):
            return
        self.volume_virtual_window = window

        for widget in self.vol_frame.winfo_children():
            widget.destroy()
        self.check_items = []
        self.volume_index_to_widget = {}

        top_height = first_row * row_height
        row_offset = 0
        if top_height > 0:
            top_spacer = ctk.CTkFrame(self.vol_frame, fg_color="transparent", corner_radius=0, height=top_height)
            top_spacer.grid(row=0, column=0, columnspan=columns, sticky="ew")
            top_spacer.grid_propagate(False)
            row_offset = 1

        start_index = first_row * columns
        end_index = min(total_visible, last_row * columns)
        visible_slice = filtered_indices[start_index:end_index]
        is_compact = bool(getattr(self, "use_compact_volume_mode", False))
        grid_padx = 12 if is_compact else 10
        grid_pady = 6 if is_compact else 8
        grid_sticky = "w" if is_compact else "ew"

        for local_pos, absolute_index in enumerate(visible_slice):
            vol, _link = self.pairs[absolute_index]
            var = self.check_vars[absolute_index]
            chk = self._create_volume_item(self.vol_frame, vol, var, absolute_index)
            row = row_offset + (local_pos // columns)
            col = local_pos % columns
            chk.grid(row=row, column=col, padx=grid_padx, pady=grid_pady, sticky=grid_sticky)
            self.check_items.append((chk, vol))
            self.volume_index_to_widget[absolute_index] = chk

        bottom_height = max(0, (total_rows - last_row) * row_height)
        if bottom_height > 0:
            bottom_row = row_offset + max(1, (len(visible_slice) + columns - 1) // columns)
            bottom_spacer = ctk.CTkFrame(self.vol_frame, fg_color="transparent", corner_radius=0, height=bottom_height)
            bottom_spacer.grid(row=bottom_row, column=0, columnspan=columns, sticky="ew")
            bottom_spacer.grid_propagate(False)

        self._refresh_volume_card_styles()
        self._update_selection_status()
        self._refresh_volume_empty_state()

    def _should_use_compact_volume_mode(self, total_items=None):
        _ = total_items
        return True

    def _capture_volume_selection_state(self):
        return [bool(var.get()) for var in getattr(self, "check_vars", []) or []]

    def _rerender_volume_layout(self):
        if getattr(self, "analysis_in_progress", False):
            return
        if not getattr(self, "pairs", None):
            self.use_compact_volume_mode = self._should_use_compact_volume_mode(0)
            self._update_volume_render_badges(0, 0)
            return
        total = len(getattr(self, "pairs", []) or [])
        next_compact = self._should_use_compact_volume_mode(total)
        next_fast = self._should_use_fast_volume_widgets(total)
        next_virtualized = self._should_virtualize_volume_mode(total)
        next_columns = self._get_volume_grid_columns(total)
        current_compact = bool(getattr(self, "use_compact_volume_mode", False))
        current_fast = bool(getattr(self, "use_fast_volume_widgets", False))
        current_virtualized = bool(getattr(self, "volume_virtualized", False))
        current_columns = int(getattr(self, "volume_grid_columns", 0) or 0)
        if (
            len(getattr(self, "check_vars", []) or []) == total
            and current_virtualized
            and next_virtualized
        ):
            self.use_compact_volume_mode = next_compact
            self.use_fast_volume_widgets = next_fast
            self.volume_virtualized = next_virtualized
            self.volume_grid_columns = next_columns
            self._refresh_volume_layout_mode_button()
            self.volume_virtual_window = None
            self._refresh_virtualized_volume_view(force=True, reset_scroll=False)
            self._update_volume_render_badges(total, total)
            return
        if (
            len(getattr(self, "check_vars", []) or []) == total
            and next_compact == current_compact
            and next_fast == current_fast
            and next_virtualized == current_virtualized
        ):
            self.use_compact_volume_mode = next_compact
            self.use_fast_volume_widgets = next_fast
            self.volume_virtualized = next_virtualized
            self.volume_grid_columns = next_columns
            self._refresh_volume_layout_mode_button()
            if current_virtualized:
                self.volume_virtual_window = None
                self._refresh_virtualized_volume_view(force=True, reset_scroll=False)
            elif next_columns != current_columns:
                self.apply_filter()
            else:
                self._refresh_volume_card_styles()
                self._update_selection_status()
            self._update_volume_render_badges(total if current_virtualized else len(self.check_items), total)
            return
        selection_state = self._capture_volume_selection_state()
        self._start_volume_render(selection_state=selection_state, feedback=False)

    def _on_volume_layout_mode_change(self, selected_value, allow_deferred=True):
        _ = selected_value
        _ = allow_deferred
        if hasattr(self, "volume_layout_mode"):
            self.volume_layout_mode.set("Dense")
        self.volume_layout_pending_mode = None
        self._refresh_volume_layout_mode_button()
        self._rerender_volume_layout()

    def _cycle_volume_layout_mode(self):
        return

    def _refresh_volume_layout_mode_button(self):
        if not hasattr(self, "volume_layout_button"):
            return
        pending_mode = getattr(self, "volume_layout_pending_mode", None)
        if pending_mode:
            self.volume_layout_button.configure(
                text=f"{pending_mode}...",
                fg_color=self.palette["warning_soft"],
                hover_color=self.palette["warning_soft"],
                text_color=self.palette["warning_text"],
            )
            return
        self.volume_layout_button.configure(
            text="",
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["panel_bg"],
            text_color=self.palette["text"],
        )

    def _update_volume_render_badges(self, built_count, total_count):
        if hasattr(self, "volume_count_badge"):
            if total_count <= 0:
                self.volume_count_badge.configure(text="0 élément")
            elif built_count < total_count:
                self.volume_count_badge.configure(text=f"{built_count}/{total_count} rendus")
            else:
                self.volume_count_badge.configure(text=f"{total_count} éléments")
        if hasattr(self, "selection_hint_label"):
            if total_count <= 0:
                hint = "Aucun tome ou chapitre détecté."
            elif built_count < total_count:
                hint = f"Rendu: {built_count}/{total_count}"
            elif getattr(self, "use_fast_volume_widgets", False):
                hint = "Grand catalogue : liste légère instantanée."
            elif getattr(self, "volume_virtualized", False):
                hint = "Grand catalogue : rendu virtualisé actif."
            else:
                hint = "Sélectionne les tomes ou chapitres à télécharger."
            self.selection_hint_label.configure(text=hint)

    def _sync_volume_card_style(self, card):
        if card is None or not self._is_ctk_widget(card):
            return
        selected = bool(getattr(card, "volume_var", None).get()) if hasattr(card, "volume_var") else False
        title = getattr(card, "volume_title_label", None)
        index = getattr(card, "volume_index_label", None)
        if selected:
            card.configure(fg_color="#f8fbff", border_color="#a9bfd9")
            if title is not None:
                title.configure(text_color=self.palette["text"])
            if index is not None:
                index.configure(fg_color=self.palette["accent_soft"], text_color=self.palette["accent_hover"])
        else:
            card.configure(fg_color=self.palette["card_bg"], border_color=self.palette["panel_shell"])
            if title is not None:
                title.configure(text_color=self.palette["text"])
            if index is not None:
                index.configure(fg_color=self.palette["card_alt"], text_color=self.palette["muted"])

    def _refresh_volume_card_styles(self):
        self._refresh_canvas_volume_card_styles()
        for card, _label in getattr(self, "check_items", []) or []:
            self._sync_volume_card_style(card)

    def _toggle_volume_widget(self, widget):
        var = getattr(widget, "volume_var", None)
        if var is None:
            return
        var.set(not bool(var.get()))
        self.update_master_toggle_button()

    def _assign_volume_card_content(self, card, volume_label, var, index):
        card.volume_var = var
        card.volume_index = index
        premium = self.is_volume_premium(index=index)
        display_label = self._compact_display_label(volume_label)
        if hasattr(card, "volume_title_label"):
            card.volume_title_label.configure(text=display_label)
        if hasattr(card, "volume_index_label"):
            card.volume_index_label.configure(text=str(index + 1))
        if hasattr(card, "volume_premium_badge"):
            card.volume_premium_badge.configure(text="$")
            card.volume_premium_badge.grid_remove() if not premium else card.volume_premium_badge.grid()
        if hasattr(card, "volume_checkbox"):
            card.volume_checkbox.configure(variable=var)
        if hasattr(card, "volume_preview_button"):
            card.volume_preview_button.configure(command=lambda idx=index: self.open_volume_preview_by_index(idx))
        self._sync_volume_card_style(card)

    def _create_volume_card(self, parent, volume_label, var, index):
        card = ctk.CTkFrame(
            parent,
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["panel_shell"],
            height=46,
        )
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_columnconfigure(2, weight=0)
        card.grid_columnconfigure(3, weight=0)
        card.grid_columnconfigure(4, weight=0)

        index_label = ctk.CTkLabel(
            card,
            text=str(index + 1),
            width=26,
            height=22,
            corner_radius=4,
            fg_color=self.palette["card_alt"],
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 9),
        )
        index_label.grid(row=0, column=0, padx=(10, 8), pady=8)

        title_label = ctk.CTkLabel(
            card,
            text=volume_label,
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI", 10),
            anchor="w",
        )
        title_label.grid(row=0, column=1, sticky="ew", pady=8)

        premium_badge = ctk.CTkLabel(
            card,
            text="$",
            width=24,
            height=22,
            corner_radius=5,
            fg_color="#f4dd8c",
            text_color="#8f6500",
            font=("Segoe UI Semibold", 11),
        )
        premium_badge.grid(row=0, column=2, padx=(8, 0), pady=8)

        preview_button = ctk.CTkButton(
            card,
            text="⌕",
            width=26,
            height=24,
            corner_radius=5,
            fg_color="#dfeee5",
            hover_color="#cfe6d8",
            border_width=1,
            border_color="#a3cab1",
            text_color="#2f6d4a",
            font=("Segoe UI Semibold", 11),
            command=lambda idx=index: self.open_volume_preview_by_index(idx),
        )
        preview_button.grid(row=0, column=3, padx=(8, 0), pady=8)

        checkbox = ctk.CTkCheckBox(
            card,
            text="",
            variable=var,
            width=24,
            height=24,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=4,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            border_color=self.palette["border"],
            checkmark_color="#ffffff",
            command=self.update_master_toggle_button,
        )
        checkbox.grid(row=0, column=4, padx=(8, 10), pady=8)

        toggle = lambda _event=None, _widget=card: self._toggle_volume_widget(_widget)
        card.bind("<Button-1>", toggle)
        index_label.bind("<Button-1>", toggle)
        title_label.bind("<Button-1>", toggle)

        card.volume_title_label = title_label
        card.volume_index_label = index_label
        card.volume_premium_badge = premium_badge
        card.volume_preview_button = preview_button
        card.volume_checkbox = checkbox
        self._assign_volume_card_content(card, volume_label, var, index)
        return card

    def _assign_compact_volume_item_content(self, item, volume_label, var, index):
        item.volume_var = var
        item.volume_index = index
        premium = self.is_volume_premium(index=index)
        display_label = self._compact_display_label(volume_label)
        if hasattr(item, "volume_title_label"):
            item.volume_title_label.configure(text=display_label)
        if hasattr(item, "volume_index_label"):
            item.volume_index_label.configure(text=str(index + 1))
        if hasattr(item, "volume_premium_badge"):
            item.volume_premium_badge.configure(text="$")
            item.volume_premium_badge.grid_remove() if not premium else item.volume_premium_badge.grid()
        if hasattr(item, "volume_checkbox"):
            item.volume_checkbox.configure(variable=var)
        if hasattr(item, "volume_preview_button"):
            item.volume_preview_button.configure(command=lambda idx=index: self.open_volume_preview_by_index(idx))
        self._sync_volume_card_style(item)

    def _create_compact_volume_item(self, parent, volume_label, var, index):
        outer_radius = 4
        item = ctk.CTkFrame(
            parent,
            fg_color=self.palette["card_bg"],
            corner_radius=outer_radius,
            border_width=1,
            border_color=self.palette["border"],
            height=40,
        )
        item.grid_propagate(False)
        item.grid_rowconfigure(0, weight=1)
        item.grid_columnconfigure(0, weight=1)

        content = ctk.CTkFrame(
            item,
            fg_color=self.palette["card_bg"],
            corner_radius=max(0, outer_radius - 1),
            border_width=0,
        )
        content.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        content.grid_rowconfigure(0, minsize=28)
        content.grid_columnconfigure(0, weight=0)
        content.grid_columnconfigure(1, weight=1)
        content.grid_columnconfigure(2, weight=0)
        content.grid_columnconfigure(3, weight=0)
        content.grid_columnconfigure(4, weight=0)

        index_label = ctk.CTkLabel(
            content,
            text=str(index + 1),
            width=22,
            height=18,
            corner_radius=4,
            fg_color=self.palette["card_alt"],
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 9),
        )
        index_label.grid(row=0, column=0, padx=(8, 6), pady=7)

        title_label = ctk.CTkLabel(
            content,
            text=volume_label,
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI", 10),
            anchor="w",
        )
        title_label.grid(row=0, column=1, sticky="ew", pady=6)

        premium_badge = ctk.CTkLabel(
            content,
            text="$",
            width=22,
            height=20,
            corner_radius=5,
            fg_color="#f4dd8c",
            text_color="#8f6500",
            font=("Segoe UI Semibold", 10),
        )
        premium_badge.grid(row=0, column=2, padx=(6, 0), pady=5)

        preview_button = ctk.CTkButton(
            content,
            text="⌕",
            width=24,
            height=22,
            corner_radius=5,
            fg_color="#dfeee5",
            hover_color="#cfe6d8",
            border_width=1,
            border_color="#a3cab1",
            text_color="#2f6d4a",
            font=("Segoe UI Semibold", 10),
            command=lambda idx=index: self.open_volume_preview_by_index(idx),
        )
        preview_button.grid(row=0, column=3, padx=(6, 0), pady=5)

        checkbox = ctk.CTkCheckBox(
            content,
            text="",
            variable=var,
            width=20,
            height=20,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            border_color=self.palette["border"],
            checkmark_color="#ffffff",
            command=self.update_master_toggle_button,
        )
        checkbox.grid(row=0, column=4, padx=(6, 8), pady=5)

        toggle = lambda _event=None, _widget=item: self._toggle_volume_widget(_widget)
        item.bind("<Button-1>", toggle)
        content.bind("<Button-1>", toggle)
        index_label.bind("<Button-1>", toggle)
        title_label.bind("<Button-1>", toggle)

        item.is_compact_volume_item = True
        item.volume_content_frame = content
        item.volume_title_label = title_label
        item.volume_index_label = index_label
        item.volume_premium_badge = premium_badge
        item.volume_preview_button = preview_button
        item.volume_checkbox = checkbox
        self._assign_compact_volume_item_content(item, volume_label, var, index)
        return item

    def _assign_fast_volume_item_content(self, item, volume_label, var, index):
        item.volume_var = var
        item.volume_index = index
        premium_prefix = "$ " if self.is_volume_premium(index=index) else ""
        item.configure(text=f"{index + 1}. {premium_prefix}{volume_label}", variable=var)

    def _create_fast_volume_item(self, parent, volume_label, var, index):
        item = ttk.Checkbutton(
            parent,
            text=f"{index + 1}. {volume_label}",
            variable=var,
            style="Tome.TCheckbutton",
            takefocus=False,
            command=self.update_master_toggle_button,
        )
        item.is_fast_volume_item = True
        self._assign_fast_volume_item_content(item, volume_label, var, index)
        return item

    def _get_virtual_volume_pool_kind(self):
        if getattr(self, "use_fast_volume_widgets", False):
            return "fast"
        if getattr(self, "use_compact_volume_mode", False):
            return "compact"
        return "card"

    def _create_virtual_volume_pool_item(self, kind):
        dummy_var = tk.BooleanVar(value=False)
        if kind == "fast":
            return self._create_fast_volume_item(self.vol_frame, "", dummy_var, 0)
        if kind == "compact":
            return self._create_compact_volume_item(self.vol_frame, "", dummy_var, 0)
        return self._create_volume_card(self.vol_frame, "", dummy_var, 0)

    def _assign_virtual_volume_pool_item(self, widget, kind, volume_label, var, index):
        if kind == "fast":
            self._assign_fast_volume_item_content(widget, volume_label, var, index)
            return
        if kind == "compact":
            self._assign_compact_volume_item_content(widget, volume_label, var, index)
            return
        self._assign_volume_card_content(widget, volume_label, var, index)

    def _grid_virtual_volume_pool_item(self, widget, kind, row, col):
        if kind == "fast":
            widget.grid(row=row, column=col, padx=8, pady=4, sticky="w")
        elif kind == "compact":
            widget.grid(row=row, column=col, padx=12, pady=6, sticky="w")
        else:
            widget.grid(row=row, column=col, padx=10, pady=8, sticky="ew")

    def _create_volume_item(self, parent, volume_label, var, index):
        if getattr(self, "use_fast_volume_widgets", False):
            return self._create_fast_volume_item(parent, volume_label, var, index)
        if getattr(self, "use_compact_volume_mode", False):
            return self._create_compact_volume_item(parent, volume_label, var, index)
        return self._create_volume_card(parent, volume_label, var, index)

    def _finalize_volume_render(self):
        self.volume_render_after_id = None
        self._stop_analysis_loading_indicators()
        self._scroll_volumes_to_top()
        feedback_enabled = bool(getattr(self, "volume_render_feedback_enabled", True))
        self.volume_render_feedback_enabled = True
        has_pairs = bool(self.pairs)
        self.filter_entry.configure(state="normal" if has_pairs else "disabled")
        self.clear_filter_button.configure(state="normal" if has_pairs else "disabled")
        self._style_clear_filter_button(disabled=not has_pairs)
        if has_pairs and not self.filter_text.get().strip():
            self.set_filter_placeholder()
        self.master_toggle_button.configure(state="normal" if has_pairs else "disabled")
        self.invert_button.configure(state="normal" if has_pairs else "disabled")
        self.update_master_toggle_button()
        if has_pairs and not self.filter_placeholder_active and self.filter_text.get().strip():
            self.apply_filter()
        self._refresh_volume_empty_state()
        built_count = len(self.pairs) if getattr(self, "volume_virtualized", False) else len(self.check_items)
        self._update_volume_render_badges(built_count, len(self.pairs))
        if has_pairs and feedback_enabled:
            self.log("Liste chargée avec succès.", level="success")
            summary = getattr(self, "catalog_state_summary", None) or {}
            if summary and not summary.get("first_seen") and int(summary.get("new_count") or 0) > 0:
                self._set_analysis_status_label(
                    f"Analyse terminée: {len(self.pairs)} éléments (+{int(summary.get('new_count') or 0)} nouveaux).",
                    success=True,
                )
            elif summary and summary.get("first_seen"):
                self._set_analysis_status_label(f"Analyse terminée: {len(self.pairs)} éléments mémorisés.", success=True)
            else:
                self._set_analysis_status_label(f"Analyse terminée: {len(self.pairs)} éléments.", success=True)
        elif not has_pairs and feedback_enabled:
            self._show_volume_empty_state("Aucun tome détecté pour cette URL.", tone="warning")
            self.log("Aucun tome détecté.", level="warning")
            self._set_analysis_status_label("Analyse terminée: liste vide.", success=False)
        if has_pairs and getattr(self, "volume_virtualized", False) and not getattr(self, "use_fast_volume_widgets", False):
            current_kind = self._get_virtual_volume_pool_kind()
            if current_kind == "compact":
                self._schedule_volume_pool_prewarm("card")
            elif current_kind == "card":
                self._schedule_volume_pool_prewarm("compact")
        callback = getattr(self, "volume_render_complete_callback", None)
        self.volume_render_complete_callback = None
        if callable(callback):
            callback()

    def _render_volume_cards_batch(self, token, start_index=0):
        if token != getattr(self, "volume_render_token", 0):
            return
        total = len(self.pairs)
        if total <= 0:
            self._finalize_volume_render()
            return
        columns = getattr(self, "volume_grid_columns", self._get_volume_grid_columns(total))
        if getattr(self, "volume_grouped", False):
            columns = min(columns, 3)
        kind = "compact" if getattr(self, "use_compact_volume_mode", False) else "card"
        self._configure_volume_grid_columns(columns, kind=kind)
        end_index = min(start_index + getattr(self, "volume_render_batch_size", VOLUME_RENDER_BATCH_SIZE), total)
        for i in range(start_index, end_index):
            vol, _link = self.pairs[i]
            selected_by_default = True
            if i < len(getattr(self, "volume_render_selection_state", []) or []):
                selected_by_default = bool(self.volume_render_selection_state[i])
            var = tk.BooleanVar(value=selected_by_default)
            self.check_vars.append(var)
            chk = self._create_volume_item(self.vol_frame, vol, var, i)
            if not getattr(self, "volume_grouped", False):
                row, col = self._get_centered_volume_grid_position(i, total, columns)
                self._grid_virtual_volume_pool_item(chk, kind, row, col)
            self.check_items.append((chk, vol))

        if getattr(self, "volume_grouped", False):
            self._grid_grouped_volume_widgets(list(range(end_index)), columns, kind)
        self._update_volume_render_badges(end_index, total)
        self._update_volume_canvas_window(0)
        self._update_volume_canvas_scrollregion()
        if end_index < total:
            if bool(getattr(self, "volume_render_feedback_enabled", True)):
                self._set_analysis_status_label(f"Analyse terminée: rendu {end_index}/{total}...", success=None)
            self.volume_render_after_id = self.root.after(
                1,
                lambda current=end_index, current_token=token: self._render_volume_cards_batch(current_token, current),
            )
            return
        self._finalize_volume_render()

    def _start_volume_render(self, on_complete=None, selection_state=None, feedback=True):
        self._cancel_pending_volume_render()
        self._cancel_volume_pool_prewarm()
        self.volume_render_complete_callback = on_complete
        self.volume_render_feedback_enabled = bool(feedback)
        for widget in self.vol_frame.winfo_children():
            widget.destroy()
        self._hide_canvas_volume_pool()
        self._hide_canvas_volume_headers()
        self._set_volume_canvas_render_mode(False)

        self.check_vars = []
        self.check_items = []
        self.filtered_volume_indices = []
        self.volume_index_to_widget = {}
        self.volume_virtual_window = None
        self.volume_virtual_widget_pool = []
        self.volume_virtual_widget_pools = {}
        self.volume_virtual_widget_pool_mode = None
        self.volume_group_header_widgets = {}
        self.volume_canvas_header_pool = []
        self.volume_canvas_render_active = False
        self.volume_virtual_top_spacer = None
        self.volume_virtual_bottom_spacer = None
        self.volume_last_canvas_yview = None
        self.volume_virtual_last_refresh_at = 0.0
        self.volume_virtual_refresh_processing = False
        self.volume_grouped_virtual_rows_cache_key = None
        self.volume_grouped_virtual_rows_cache = []
        self.last_applied_filter_raw = None
        total = len(self.pairs)
        self.volume_grouped = self._should_group_volume_display()
        self.use_fast_volume_widgets = self._should_use_fast_volume_widgets(total)
        self.use_compact_volume_mode = self._should_use_compact_volume_mode(total)
        self.volume_virtualized = self._should_virtualize_volume_mode(total)
        if getattr(self, "volume_grouped", False):
            self.use_fast_volume_widgets = False
            self.use_compact_volume_mode = True
            self.volume_virtualized = total > 0
        self._refresh_volume_layout_mode_button()
        self.volume_label_cache_lower = [str(vol or "").lower() for vol, _link in self.pairs]
        self.volume_render_selection_state = list(selection_state or [])
        self.volume_grid_columns = self._get_volume_grid_columns(total)
        self.volume_render_batch_size = self._get_volume_render_batch_size(total)
        self.filtered_volume_indices = list(range(total))
        self._configure_volume_grid_columns(
            self.volume_grid_columns,
            kind="fast" if self.use_fast_volume_widgets else ("compact" if self.use_compact_volume_mode else "card"),
        )
        if total <= 0:
            self._update_volume_canvas_window(0)
            self._update_volume_canvas_scrollregion(0)
            self._update_volume_render_badges(0, 0)
            self._finalize_volume_render()
            return
        self._hide_volume_empty_state()
        self.filter_entry.configure(state="disabled")
        self.clear_filter_button.configure(state="disabled")
        self.master_toggle_button.configure(state="disabled")
        self.invert_button.configure(state="disabled")
        self._style_clear_filter_button(disabled=True)
        if getattr(self, "volume_virtualized", False):
            self.check_vars = [
                tk.BooleanVar(
                    value=bool(self.volume_render_selection_state[index]) if index < len(self.volume_render_selection_state) else True
                )
                for index in range(total)
            ]
            self.filtered_volume_indices = list(range(total))
            self._update_volume_render_badges(total, total)
            self._refresh_virtualized_volume_view(force=True, reset_scroll=True)
            self._set_analysis_status_label(f"Analyse terminée: {total} éléments.", success=True)
            self._schedule_virtual_volume_refresh(delay_ms=90)
            self._finalize_volume_render()
            return
        token = getattr(self, "volume_render_token", 0)
        self._update_volume_render_badges(0, total)
        if self.volume_render_feedback_enabled:
            self._set_analysis_status_label(f"Analyse terminée: rendu 0/{total}...", success=None)
        self._render_volume_cards_batch(token, 0)

    def _refresh_volume_empty_state(self):
        if getattr(self, "volume_virtualized", False):
            total = len(getattr(self, "pairs", []) or [])
            visible = len(getattr(self, "filtered_volume_indices", []) or [])
            if total == 0:
                self._show_volume_empty_state("Aucun Tome/Chapitre chargé.", tone="muted")
            elif visible == 0:
                self._show_volume_empty_state("Aucun résultat avec ce filtre.", tone="warning")
            else:
                self._hide_volume_empty_state()
            return
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
        if hasattr(self, "error_count_badge"):
            if count > 0:
                self.error_count_badge.configure(
                    text=f"{count} erreur{'s' if count > 1 else ''}",
                    fg_color=self.palette["danger_soft"],
                    text_color=self.palette["danger_text"],
                )
            else:
                self.error_count_badge.configure(
                    text="Aucune erreur",
                    fg_color=self.palette["success_soft"],
                    text_color=self.palette["success_text"],
                )
        if hasattr(self, "error_summary_label"):
            if count > 0:
                if count == 1:
                    summary_text = "1 erreur récente capturée. Utilise Copier ou Exporter pour diagnostiquer."
                else:
                    summary_text = f"{count} erreurs récentes capturées. Utilise Copier ou Exporter pour diagnostiquer."
                self.error_summary_label.configure(
                    text=summary_text
                )
            else:
                self.error_summary_label.configure(
                    text="Aucune erreur capturée pour le moment. Les échecs de traitement apparaîtront ici."
                )
        if hasattr(self, "error_empty_label"):
            if count > 0:
                self.error_empty_label.place_forget()
            else:
                host = getattr(self, "error_tree_shell", None)
                if host is not None:
                    self.error_empty_label.place(in_=host, relx=0.5, rely=0.5, anchor="center")
        self._refresh_config_tab_buttons()
        if focus_errors and count > 0:
            self._select_config_tab("error")
            self._set_workflow_step("logs", "Des erreurs sont disponibles dans l'onglet Erreurs.")

    def _on_selection_tab_changed(self, _event=None):
        selected = getattr(self, "active_config_tab", "download")
        if selected == "error":
            self._set_workflow_step("logs", "Consulte les erreurs par tome.")
        elif selected == "download":
            if getattr(self, "download_in_progress", False):
                self._set_workflow_step("download", "Téléchargement en cours...")
            else:
                self._set_workflow_step("select", "Sélectionne les tomes à télécharger.")

    def _set_segmented_control_value(self, control, value):
        if control is None or value is None:
            return
        self._segmented_control_sync = True
        try:
            control.set(value)
        finally:
            self._segmented_control_sync = False

    def _style_tab_button(self, button, text, bg, fg, border, hover=None):
        if button is None:
            return
        hover = hover or bg
        if self._is_ctk_widget(button):
            button.configure(
                text=text,
                fg_color=bg,
                hover_color=hover,
                border_color=border,
                border_width=0,
                text_color=fg,
                corner_radius=4,
                font=self.ui_metrics["font_button"],
                height=self.ui_metrics["button_height_compact"],
            )
            return
        button.configure(
            text=text,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            highlightbackground=border,
            highlightcolor=border,
        )

    def _get_auth_tone(self, auth_state, selected=False):
        tones = {
            "valid": (
                self.palette["success_soft"],
                self.palette["success_text"],
                self.palette["success_border_strong" if selected else "success_border"],
                "#d6ebdd",
            ),
            "pending": (
                self.palette["warning_soft"],
                self.palette["warning_text"],
                self.palette["warning_border"],
                "#efe0bd",
            ),
            "invalid": (
                self.palette["danger_soft"],
                self.palette["danger_text"],
                self.palette["danger_border"],
                "#f0d6db",
            ),
        }
        return tones.get(auth_state, tones["pending"])

    def _on_selection_segment_change(self, selected_value):
        if getattr(self, "_segmented_control_sync", False):
            return
        key = (getattr(self, "selection_tab_value_to_key", {}) or {}).get(selected_value)
        if key and key != getattr(self, "active_selection_tab", None):
            self._select_selection_tab(key)

    def _on_config_segment_change(self, selected_value):
        if getattr(self, "_segmented_control_sync", False):
            return
        key = (getattr(self, "config_tab_value_to_key", {}) or {}).get(selected_value)
        if key and key != getattr(self, "active_config_tab", None):
            self._select_config_tab(key)

    def _layout_tab_row(self, header, buttons, order):
        """Place les onglets en chevauchement 1px pour eliminer les doubles separations."""
        if self._is_ctk_widget(header):
            return
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
        """Compatibilité: les onglets du bas sont fusionnés dans la barre d'onglets principale."""
        self._refresh_config_tab_buttons()

    def _update_selection_top_border_mask(self, selected_widget, mask_bg):
        """Masque la ligne haute de l'encart sous l'onglet actif."""
        if not hasattr(self, "selection_content_border") or not hasattr(self, "selection_tabs_header"):
            return
        if not hasattr(self, "selection_top_border_mask"):
            self.selection_top_border_mask = tk.Frame(
                self.selection_content_border,
                bg=self.palette.get("card_bg", "#ffffff"),
                bd=0,
                highlightthickness=0,
            )
        if selected_widget is None:
            self.selection_top_border_mask.place_forget()
            return
        try:
            self.root.update_idletasks()
            w = selected_widget.winfo_width()
            hx = selected_widget.winfo_x()
            header_x = self.selection_tabs_header.winfo_x()
            border_x = self.selection_content_border.winfo_x()
            x = header_x + hx - border_x
            if w <= 1:
                self.selection_top_border_mask.place_forget()
                return
            inner_x = max(1, x + 1)
            inner_w = max(1, w - 2)
            self.selection_top_border_mask.configure(bg=mask_bg)
            self.selection_top_border_mask.place(x=inner_x, y=0, width=inner_w, height=1)
            self.selection_top_border_mask.lift()
        except Exception:
            self.selection_top_border_mask.place_forget()

    def _select_selection_tab(self, tab_key):
        """Compatibilité: redirige vers l'onglet principal Téléchargement ou Erreurs."""
        target_key = "error" if tab_key == "error" else "download"
        self.active_selection_tab = "error" if tab_key == "error" else "selection"
        self._select_config_tab(target_key)

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

    def _focus_cookie_editor_for_domain(self, domain):
        """Bascule sur Authentification et focus le champ cookie du domaine."""
        try:
            self._select_config_tab("auth")
        except Exception:
            pass
        entry = self._get_cookie_entry_for_domain(domain)
        if entry is None:
            return
        try:
            entry.focus_set()
            if hasattr(entry, "icursor"):
                entry.icursor("end")
        except Exception:
            pass

    def _show_cookie_refresh_prompt(self, domain, volume_label, reason, done, holder):
        """Affiche une popup non modale pour demander la mise à jour du cookie."""
        existing = getattr(self, "cookie_refresh_prompt_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.destroy()
        except Exception:
            pass

        def apply_cookie_from_prompt(cookie_text, status_var):
            if domain not in COOKIE_DOMAINS:
                return True
            new_cookie = repair_mojibake_text(cookie_text or "").strip()
            if not new_cookie:
                status_var.set("Colle le nouveau cookie avant de relancer.")
                return False
            cookie_var = self._get_cookie_var_for_domain(domain)
            if cookie_var is None:
                status_var.set("Domaine cookie inconnu.")
                return False
            cookie_var.set(new_cookie)
            self.sync_cookie_source_for_domain(domain)
            self.persist_settings()
            self.update_cookie_status(validate=False)
            self.update_runtime_status()
            self._schedule_cookie_listing_probe(domains=(domain,), delay_ms=250)
            self.log(f"Cookie .{domain} mis à jour depuis la popup. Relance demandée.", level="success")
            return True

        def finalize(result):
            window = holder.get("window")
            try:
                if window is not None and window.winfo_exists():
                    window.destroy()
            except Exception:
                pass
            self.cookie_refresh_prompt_window = None
            holder["result"] = bool(result)
            done.set()

        domain_label = f".{domain}" if domain else "du domaine"
        title = "Cookie à renouveler"
        subtitle = f"{volume_label} a rencontré un accès refusé."
        detail = (
            f"Le cookie {domain_label} semble expiré ou refusé.\n"
            "Colle le nouveau cookie ci-dessous, puis clique sur OK et relancer.\n"
            "SushiDL mettra à jour le domaine et reprendra au même endroit."
        )
        if domain in COOKIE_DOMAINS:
            probe_url = STARTUP_COOKIE_LISTING_PROBE_URLS.get(domain) or "n/a"
            ua_used = self.get_request_user_agent_for_domain(domain)
            cookie_present = "oui" if self.get_cookie_header_for_domain(domain) else "non"
            detail += (
                "\n\nDiagnostic :"
                f"\n- Domaine : {domain_label}"
                f"\n- URL test : {probe_url}"
                f"\n- User-Agent : {ua_used[:120]}"
                f"\n- Cookie présent : {cookie_present}"
            )
        if reason:
            detail = f"{detail}\n\nCause détectée : {reason}"

        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.transient(self.root)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: finalize(False))

        outer = ctk.CTkFrame(
            win,
            fg_color=self.palette["card_bg"],
            corner_radius=10,
            border_width=1,
            border_color=self.palette["border"],
        )
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(
            outer,
            text=title,
            font=("Segoe UI Semibold", 15),
            text_color=self.palette["text"],
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 6))

        ctk.CTkLabel(
            outer,
            text=subtitle,
            font=("Segoe UI Semibold", 12),
            text_color=self.palette["warning_text"],
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=14)

        ctk.CTkLabel(
            outer,
            text=detail,
            font=("Segoe UI", 11),
            text_color=self.palette["muted"],
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=14, pady=(8, 12))

        cookie_box = ctk.CTkFrame(
            outer,
            fg_color=self.palette["panel_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        cookie_box.pack(fill="x", padx=14, pady=(0, 12))
        cookie_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            cookie_box,
            text=f"Nouveau cookie {domain_label}",
            font=("Segoe UI Semibold", 12),
            text_color=self.palette["text"],
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))

        cookie_input = ctk.CTkTextbox(
            cookie_box,
            height=78,
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            font=("Segoe UI", 11),
            wrap="none",
        )
        cookie_input.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))

        status_var = tk.StringVar(value="Accepte cf_clearance seul ou un header Cookie complet.")
        ctk.CTkLabel(
            cookie_box,
            textvariable=status_var,
            font=("Segoe UI", 10),
            text_color=self.palette["muted"],
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

        def paste_cookie():
            try:
                value = self.root.clipboard_get()
            except Exception:
                status_var.set("Presse-papiers sans texte exploitable.")
                return
            cookie_input.delete("1.0", "end")
            cookie_input.insert("1.0", repair_mojibake_text(value or "").strip())
            status_var.set("Cookie collé. Clique sur OK et relancer.")

        def confirm_and_retry():
            cookie_text = cookie_input.get("1.0", "end").strip()
            if apply_cookie_from_prompt(cookie_text, status_var):
                finalize(True)

        actions = ctk.CTkFrame(outer, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=(0, 14))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=0)
        actions.grid_columnconfigure(2, weight=0)

        ctk.CTkButton(
            actions,
            text="Coller",
            command=paste_cookie,
            height=32,
            width=90,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            text_color=self.palette["text"],
            border_width=1,
            border_color=self.palette["border"],
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            actions,
            text="Annuler",
            command=lambda: finalize(False),
            height=32,
            width=90,
            corner_radius=6,
            fg_color=self.palette["danger_soft"],
            hover_color="#edd0d7",
            text_color=self.palette["danger_text"],
            border_width=1,
            border_color=self.palette["danger_border"],
        ).grid(row=0, column=1, sticky="e", padx=(0, 8))

        ok_button = ctk.CTkButton(
            actions,
            text="OK et relancer",
            command=confirm_and_retry,
            height=32,
            width=120,
            corner_radius=6,
            fg_color=self.palette["success_soft"],
            hover_color="#d6ebdd",
            text_color=self.palette["success_text"],
            border_width=1,
            border_color=self.palette["success_border"],
        )
        ok_button.grid(row=0, column=2)

        holder["window"] = win
        self.cookie_refresh_prompt_window = win

        try:
            win.update_idletasks()
            width = max(620, win.winfo_reqwidth())
            height = max(360, win.winfo_reqheight())
            x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - width) // 2)
            y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - height) // 3)
            win.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

        try:
            cookie_input.focus_set()
        except Exception:
            pass

    def prompt_cookie_refresh(self, domain, volume_label, reason="", cancel_event=None):
        """Attend qu'un utilisateur mette à jour le cookie puis confirme la relance."""
        if threading.current_thread() is threading.main_thread():
            return False

        done = threading.Event()
        holder = {"result": False, "window": None}
        self.ui_queue.put(
            lambda: self._show_cookie_refresh_prompt(domain, volume_label, reason, done, holder)
        )

        while not done.wait(0.2):
            if cancel_event is not None and cancel_event.is_set():
                self.ui_queue.put(lambda: holder.get("window") and holder["window"].destroy())
                return False
        return bool(holder["result"])

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
            def clear_status():
                if self._is_ctk_widget(self.status_label):
                    self.status_label.configure(text="", fg_color="transparent", text_color=self.palette["muted"])
                else:
                    self.status_label.configure(text="", foreground=self.palette["muted"])

            self.run_on_ui(clear_status)

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

    def _schedule_auth_status_update_cookie_toonfr(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .toonfr sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["toonfr"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["toonfr"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("toonfr",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_ortega(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .ortegascans sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["ortega"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["ortega"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("ortega",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_hentaizone(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .hentaizone sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["hentaizone"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["hentaizone"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("hentaizone",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_scanmanga(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .scanmanga sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["scanmanga"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["scanmanga"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("scanmanga",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_crunchyscan(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .crunchyscan sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["crunchyscan"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["crunchyscan"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("crunchyscan",), delay_ms=1200)

    def _schedule_auth_status_update_cookie_scanhentai(self, *_args):
        """Rafraîchit les badges auth après modification du cookie .scan-hentai sans reset global."""
        if not hasattr(self, "cookie_sources"):
            return
        if hasattr(self, "analysis_auth_state") and isinstance(self.analysis_auth_state, dict):
            self.analysis_auth_state["scanhentai"] = None
        if hasattr(self, "cookie_probe_state") and isinstance(self.cookie_probe_state, dict):
            self.cookie_probe_state["scanhentai"] = None
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)
        self._schedule_cookie_listing_probe(domains=("scanhentai",), delay_ms=1200)

    def _schedule_auth_status_update_url(self, *_args):
        """Rafraîchit les badges auth au changement d'URL sans effacer l'historique d'analyse."""
        if not hasattr(self, "cookie_sources"):
            return
        self.run_on_ui(lambda: self.update_cookie_status(validate=False))
        self.run_on_ui(self.update_runtime_status)

    def _source_to_display(self, source):
        _ = source
        return ""

    def _is_ctk_widget(self, widget):
        """Retourne True si le widget provient de CustomTkinter."""
        if widget is None:
            return False
        return widget.__class__.__module__.startswith("customtkinter")

    def _set_auth_badge(self, widget, state, text_override=None):
        """Applique un badge visuel pour statut auth: pending / valid / invalid."""
        pending_bg = self.palette["warning_soft"]
        pending_fg = self.palette["warning_text"]
        valid_bg = self.palette["success_soft"]
        valid_fg = self.palette["success_text"]
        invalid_bg = self.palette["danger_soft"]
        invalid_fg = self.palette["danger_text"]
        if isinstance(state, bool):
            normalized = "valid" if state else "invalid"
        else:
            normalized = str(state or "").strip().lower()
        if normalized in ("pending", "en_attente", "waiting"):
            label_text = text_override or "Validation en cours"
            if self._is_ctk_widget(widget):
                widget.configure(text=label_text, fg_color=pending_bg, text_color=pending_fg)
            else:
                widget.configure(text=label_text, bg=pending_bg, fg=pending_fg)
        elif normalized in ("valid", "ok", "true", "1"):
            label_text = text_override or ("Valide" if self._is_ctk_widget(widget) else "Validé")
            if self._is_ctk_widget(widget):
                widget.configure(text=label_text, fg_color=valid_bg, text_color=valid_fg)
            else:
                widget.configure(text=label_text, bg=valid_bg, fg=valid_fg)
        else:
            label_text = text_override or ("A verifier" if self._is_ctk_widget(widget) else "A vérifier")
            if self._is_ctk_widget(widget):
                widget.configure(text=label_text, fg_color=invalid_bg, text_color=invalid_fg)
            else:
                widget.configure(text=label_text, bg=invalid_bg, fg=invalid_fg)

    def _style_clear_filter_button(self, hovered=False, disabled=None):
        if not hasattr(self, "clear_filter_button"):
            return
        if disabled is None:
            disabled = str(self.clear_filter_button.cget("state")) == "disabled"
        if self._is_ctk_widget(self.clear_filter_button):
            if disabled:
                self.clear_filter_button.configure(
                    fg_color=self.palette["panel_bg"],
                    hover_color=self.palette["panel_bg"],
                    text_color="#a3adba",
                )
            elif hovered:
                self.clear_filter_button.configure(
                    fg_color=self.palette["danger_soft"],
                    hover_color="#edd0d7",
                    text_color=self.palette["danger_text"],
                )
            else:
                self.clear_filter_button.configure(
                    fg_color=self.palette["panel_bg"],
                    hover_color=self.palette["danger_soft"],
                    text_color=self.palette["muted"],
                )
            return
        if disabled:
            self.clear_filter_button.configure(bg=self.palette["card_bg"], fg="#a3adba")
        elif hovered:
            self.clear_filter_button.configure(bg="#fbe4ea", fg="#7f1d1d")
        else:
            self.clear_filter_button.configure(bg=self.palette["card_bg"], fg=self.palette["muted"])

    def _apply_auth_tab_state_style(self, state):
        """Memorise l'etat visuel de l'onglet Authentification et rafraichit son rendu."""
        normalized = str(state or "pending").strip().lower()
        if normalized not in {"valid", "pending", "invalid"}:
            normalized = "pending"
        self.auth_tab_visual_state = normalized
        self._refresh_config_tab_buttons()

    def _refresh_config_tab_buttons(self):
        """Rafraichit les onglets visuels principaux."""
        if not hasattr(self, "config_tab_buttons"):
            return
        active_key = getattr(self, "active_config_tab", "download")
        auth_state = getattr(self, "auth_tab_visual_state", "pending")
        error_count = len(getattr(self, "volume_error_entries", []) or [])
        titles = {
            "download": "Téléchargement",
            "journal": "Journal",
            "watch": "Suivi",
            "error": getattr(self, "error_tab_title", "Erreurs (0)"),
            "auth": getattr(self, "auth_tab_title", "Authentification (0/5)"),
            "options": "Options",
        }

        selected_widget = None
        selected_bg_for_mask = self.palette["card_bg"]
        for key in getattr(self, "config_tab_order", ("download", "journal", "error", "auth", "options")):
            button = self.config_tab_buttons.get(key)
            if button is None:
                continue
            is_selected = key == active_key
            if key == "auth":
                bg, fg, border, hover = self._get_auth_tone(auth_state, selected=is_selected)
            elif key == "error" and error_count > 0:
                bg = self.palette["danger_soft"]
                fg = self.palette["danger_text"]
                border = self.palette["danger_border"]
                hover = "#f0d6db"
            else:
                bg = self.palette["card_bg"] if is_selected else self.palette["panel_bg"]
                fg = self.palette["text"] if is_selected else self.palette["muted"]
                border = self.palette["border"]
                hover = self.palette["card_bg"] if is_selected else self.palette["card_alt"]
            self._style_tab_button(button, titles.get(key, ""), bg, fg, border, hover)
            if is_selected:
                selected_widget = button
                selected_bg_for_mask = bg

        self._update_config_top_border_mask(selected_widget, selected_bg_for_mask)

    def _update_config_top_border_mask(self, selected_widget, mask_bg):
        """Masque la ligne haute de l'encart sous l'onglet actif."""
        if not hasattr(self, "config_content_border") or not hasattr(self, "config_tabs_header"):
            return
        if not hasattr(self, "config_top_border_mask"):
            self.config_top_border_mask = tk.Frame(
                self.config_content_border,
                bg=self.palette.get("card_bg", "#ffffff"),
                bd=0,
                highlightthickness=0,
            )
        if selected_widget is None:
            self.config_top_border_mask.place_forget()
            return
        try:
            self.root.update_idletasks()
            w = selected_widget.winfo_width()
            hx = selected_widget.winfo_x()
            header_x = self.config_tabs_header.winfo_x()
            border_x = self.config_content_border.winfo_x()
            x = header_x + hx - border_x
            if w <= 1:
                self.config_top_border_mask.place_forget()
                return
            inner_x = max(1, x + 1)
            inner_w = max(1, w - 2)
            self.config_top_border_mask.configure(bg=mask_bg)
            self.config_top_border_mask.place(x=inner_x, y=0, width=inner_w, height=1)
            self.config_top_border_mask.lift()
        except Exception:
            self.config_top_border_mask.place_forget()

    def _select_config_tab(self, tab_key):
        """Affiche l'onglet principal choisi."""
        if not hasattr(self, "config_tab_pages"):
            return
        page = self.config_tab_pages.get(tab_key)
        if page is None:
            return
        self.active_config_tab = tab_key
        if tab_key == "error":
            self.active_selection_tab = "error"
        elif tab_key == "download":
            self.active_selection_tab = "selection"
        for tab_page in self.config_tab_pages.values():
            tab_page.pack_forget()
        page.pack(fill="both", expand=True)
        self._refresh_config_tab_buttons()
        if tab_key in {"download", "error"}:
            self._on_selection_tab_changed()

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
        self.auth_tab_progress_text = f"{valid_count}/{total}"
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
            fg_color = self.palette["success_soft"]
            text_color = "#1c6b41"
            self._set_workflow_step("select", "Analyse terminée. Sélectionne les tomes à télécharger.")
        elif success is False:
            fg_color = self.palette["danger_soft"]
            text_color = "#8a2f3b"
            self._set_workflow_step("source", "Analyse en échec. Vérifie URL/cookies puis relance.")
        else:
            fg_color = self.palette["accent_soft"]
            text_color = self.palette["accent_hover"]
            self._set_workflow_step("source", "Analyse en cours...")
        safe_text = repair_mojibake_text(text or "")
        if self._is_ctk_widget(self.status_label):
            self.status_label.configure(text=safe_text, fg_color=fg_color, text_color=text_color)
        else:
            self.status_label.configure(text=safe_text, foreground=text_color)
        if success is not None and not getattr(self, "analysis_in_progress", False):
            self._stop_analysis_loading_indicators()

    def _refresh_source_title_label(self, title=None):
        """Met a jour le titre visible du bloc source."""
        if not hasattr(self, "source_title_var"):
            return
        raw_title = self.title if title is None else title
        safe_title = normalize_manga_title_case(raw_title)
        if not safe_title:
            safe_title = "Manga/Manhwa/Comics..."
        self.source_title_var.set(safe_title)

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
        self.cookie_hentai_label_var.set("Cookie (.hentai-origines) :")
        self.cookie_toonfr_label_var.set("Cookie (.toonfr) :")
        self.cookie_ortega_label_var.set("Cookie (.ortegascans) :")
        self.cookie_hentaizone_label_var.set("Cookie (.hentaizone) :")
        self.cookie_scanmanga_label_var.set("Cookie (.scanmanga) :")
        self.cookie_crunchyscan_label_var.set("Cookie (.crunchyscan) :")
        self.cookie_scanhentai_label_var.set("Cookie (.scan-hentai) :")
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
                for name in (
                    "cookie_fr_status",
                    "cookie_net_status",
                    "cookie_origines_status",
                    "cookie_hentai_status",
                    "cookie_toonfr_status",
                    "cookie_ortega_status",
                    "cookie_hentaizone_status",
                    "cookie_scanmanga_status",
                    "cookie_crunchyscan_status",
                    "cookie_scanhentai_status",
                    "ua_status",
                )
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
                "toonfr": "https://toonfr.com/",
                "ortega": "https://ortegascans.fr/",
                "hentaizone": "https://hentaizone.xyz/",
                "scanmanga": "https://www.scan-manga.com/",
                "crunchyscan": "https://crunchyscan.fr/",
                "scanhentai": "https://scan-hentai.net/",
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

            probe_urls = getattr(self, "startup_cookie_listing_probe_urls", {}) or {}
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

                probe_url = (probe_urls.get(domain) or "").strip() or STARTUP_COOKIE_LISTING_PROBE_URLS.get(domain, "")
                if not probe_url:
                    continue

                ua_value = self.get_request_user_agent_for_domain(domain)
                if not ua_value:
                    self.cookie_probe_state[domain] = False
                    self.run_on_ui(lambda: self.update_cookie_status(validate=False))
                    self.run_on_ui(self.update_runtime_status)
                    continue

                probe_ok = False
                failure_reason = ""
                try:
                    status = evaluate_cookie_and_challenge(
                        domain,
                        cookie_value,
                        ua_value,
                        probe_url=probe_url,
                    )
                    probe_ok = bool(status.get("cookie_valid", False))
                    if not probe_ok:
                        http_status = status.get("http_status")
                        challenge_state = status.get("challenge_state")
                        failure_reason = (
                            f"HTTP {http_status} | challenge={challenge_state}"
                            if http_status
                            else f"challenge={challenge_state}"
                        )
                except Exception as probe_exc:
                    probe_ok = False
                    failure_reason = str(probe_exc)

                self.cookie_probe_state[domain] = probe_ok
                self.run_on_ui(lambda: self.update_cookie_status(validate=False))
                self.run_on_ui(self.update_runtime_status)

                if probe_ok:
                    if previous_state is not True:
                        self.log(
                            f"Test cookie .{domain} : Reussite.",
                            level="info",
                        )
                else:
                    if previous_state is not False:
                        self.log(
                            f"Test cookie .{domain} : Echec.",
                            level="warning",
                        )
                        if failure_reason:
                            self.log(
                                f"Probe .{domain} non validee: {failure_reason}",
                                level="debug",
                            )
        except Exception as exc:
            self.log(f"Probe cookie non concluant: {exc}", level="debug")

    def _refresh_log_option_cache(self, *_args):
        """Met en cache les options de logs pour éviter les waits UI en workers."""
        try:
            self.verbose_logs_cached = bool(self.verbose_logs.get())
        except Exception:
            self.verbose_logs_cached = True
        try:
            self.console_logs_enabled_cached = bool(self.console_logs_enabled.get())
        except Exception:
            self.console_logs_enabled_cached = True

    def _apply_window_icon(self):
        """Applique l'icône de fenêtre native à partir du fichier .ico multi-tailles."""
        if not APP_ICON_PATH.exists():
            return

        if os.name == "nt":
            try:
                self.root.iconbitmap(str(APP_ICON_PATH))
            except Exception:
                pass

        try:
            icon_image = Image.open(APP_ICON_PATH).convert("RGBA")
            icon_size = 64 if os.name == "nt" else 32
            icon_image = icon_image.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            self._window_icon_image = ImageTk.PhotoImage(icon_image)
            self.root.iconphoto(True, self._window_icon_image)
        except Exception:
            self._window_icon_image = None

    def __init__(self):
        """Initialise l'interface graphique et charge les paramètres"""
        MangaApp.current_instance = self
        self.total_chapters_to_process = 0
        self.chapters_done = 0
        self.ui_queue = queue.Queue()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self._window_icon_image = None
        self._apply_window_icon()

        # Fenêtre modernisée: redimensionnable avec taille minimale confortable.
        self.root.geometry("1140x1040")
        self.root.minsize(940, 1040)
        self.root.maxsize(self.root.winfo_screenwidth(), 1070)
        self.root.resizable(True, True)
        self.log_entries = []
        self.log_lock = threading.Lock()
        self.max_log_entries = 5000
        self.log_ready = False
        self.gui_log_queue = queue.Queue()
        self.gui_log_flush_lock = threading.Lock()
        self.gui_log_flush_scheduled = False
        self.last_progress_percent_ui = None
        self.last_progress_text_ui = None
        self.last_progress_detail_ui = None
        self.last_eta_text_ui = None
        self.last_current_volume_text_ui = None
        self.filter_apply_after_id = None
        self.last_applied_filter_raw = None
        self.configure_styles()
        
        # Variables Tkinter
        self.cbz_enabled = tk.BooleanVar(value=True)
        self.comicinfo_enabled = tk.BooleanVar(value=True)
        self.chapter_cover_enabled = tk.BooleanVar(value=True)
        self.webp2jpg_enabled = tk.BooleanVar(value=True)
        self.smart_resume_enabled = tk.BooleanVar(value=True)
        self.verbose_logs = tk.BooleanVar(value=True)
        self.download_threads = tk.IntVar(value=DEFAULT_DOWNLOAD_THREADS)
        self.url = tk.StringVar()
        self.ua = tk.StringVar()
        self.cookie_fr = tk.StringVar()
        self.cookie_net = tk.StringVar()
        self.cookie_origines = tk.StringVar()
        self.cookie_hentai = tk.StringVar()
        self.cookie_toonfr = tk.StringVar()
        self.cookie_ortega = tk.StringVar()
        self.cookie_hentaizone = tk.StringVar()
        self.cookie_scanmanga = tk.StringVar()
        self.cookie_crunchyscan = tk.StringVar()
        self.cookie_scanhentai = tk.StringVar()
        self.cookie_fr_label_var = tk.StringVar(value="Cookie (.fr) :")
        self.cookie_net_label_var = tk.StringVar(value="Cookie (.net) :")
        self.cookie_origines_label_var = tk.StringVar(value="Cookie (.origines) :")
        self.cookie_hentai_label_var = tk.StringVar(value="Cookie (.hentai-origines) :")
        self.cookie_toonfr_label_var = tk.StringVar(value="Cookie (.toonfr) :")
        self.cookie_ortega_label_var = tk.StringVar(value="Cookie (.ortegascans) :")
        self.cookie_hentaizone_label_var = tk.StringVar(value="Cookie (.hentaizone) :")
        self.cookie_scanmanga_label_var = tk.StringVar(value="Cookie (.scanmanga) :")
        self.cookie_crunchyscan_label_var = tk.StringVar(value="Cookie (.crunchyscan) :")
        self.cookie_scanhentai_label_var = tk.StringVar(value="Cookie (.scan-hentai) :")
        self.ua_label_var = tk.StringVar(value="User-Agent :")
        self.runtime_status = tk.StringVar(value="Prêt.")
        self.log_filter_level = tk.StringVar(value="all")
        self.volume_layout_mode = tk.StringVar(value="Dense")
        self.log_autoscroll = tk.BooleanVar(value=True)
        self.watchlist_url = tk.StringVar()
        self.watchlist_status = tk.StringVar(value="Aucune vérification lancée.")
        self.console_logs_enabled = tk.BooleanVar(value=True)
        self.show_cookies = tk.BooleanVar(value=False)
        self.verbose_logs_cached = True
        self.console_logs_enabled_cached = True
        self.filter_placeholder_text = "Filtre"
        self.filter_placeholder_active = False
        self.cover_target_height = COVER_TARGET_HEIGHT
        self.auth_validity = {**{domain: False for domain in COOKIE_DOMAINS}, "ua": False}
        self.local_ua_source = "manual"
        self.ua_runtime_validity = None
        self.analysis_auth_state = {**{domain: None for domain in COOKIE_DOMAINS}, "ua": None}
        self.cookie_probe_state = {domain: None for domain in COOKIE_DOMAINS}
        self.cookie_probe_after_ids = {domain: None for domain in COOKIE_DOMAINS}
        self.startup_cookie_listing_probe_urls = dict(STARTUP_COOKIE_LISTING_PROBE_URLS)
        # Compatibilité: conservé pour l'existant, mais la sonde utilise la version startup figée.
        self.cookie_listing_probe_urls = dict(self.startup_cookie_listing_probe_urls)
        self.analysis_auth_last_domain = None
        self.analysis_auth_last_message = ""
        self.analysis_in_progress = False
        self.watchlist_check_in_progress = False
        self.watchlist_rows = {}
        self.url.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_fr.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_net.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_origines.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_hentai.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_toonfr.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_ortega.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_hentaizone.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_scanmanga.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_crunchyscan.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_scanhentai.trace_add("write", self._schedule_runtime_status_update)
        self.cookie_fr.trace_add("write", self._schedule_auth_status_update_cookie_fr)
        self.cookie_net.trace_add("write", self._schedule_auth_status_update_cookie_net)
        self.cookie_origines.trace_add("write", self._schedule_auth_status_update_cookie_origines)
        self.cookie_hentai.trace_add("write", self._schedule_auth_status_update_cookie_hentai)
        self.cookie_toonfr.trace_add("write", self._schedule_auth_status_update_cookie_toonfr)
        self.cookie_ortega.trace_add("write", self._schedule_auth_status_update_cookie_ortega)
        self.cookie_hentaizone.trace_add("write", self._schedule_auth_status_update_cookie_hentaizone)
        self.cookie_scanmanga.trace_add("write", self._schedule_auth_status_update_cookie_scanmanga)
        self.cookie_crunchyscan.trace_add("write", self._schedule_auth_status_update_cookie_crunchyscan)
        self.cookie_scanhentai.trace_add("write", self._schedule_auth_status_update_cookie_scanhentai)
        self.ua.trace_add("write", self._schedule_auth_status_update)
        self.url.trace_add("write", self._schedule_auth_status_update_url)
        self.verbose_logs.trace_add("write", self._refresh_log_option_cache)
        self.console_logs_enabled.trace_add("write", self._refresh_log_option_cache)
        self._refresh_log_option_cache()

        # Chargement du cache
        (
            cookies,
            ua,
            cbz,
            comicinfo_enabled,
            chapter_cover_enabled,
            last_url,
            webp2jpg_enabled,
            smart_resume_enabled,
            verbose_logs_enabled,
            download_threads,
            cookie_sources,
            cookie_user_agents,
            cookie_headers,
            cookie_updated_at,
        ) = load_cookie_cache()
        self.cookie_fr.set(cookies.get("fr", ""))
        self.cookie_net.set(cookies.get("net", ""))
        self.cookie_origines.set(cookies.get("origines", ""))
        self.cookie_hentai.set(cookies.get("hentai", ""))
        self.cookie_toonfr.set(cookies.get("toonfr", ""))
        self.cookie_ortega.set(cookies.get("ortega", ""))
        self.cookie_hentaizone.set(cookies.get("hentaizone", ""))
        self.cookie_scanmanga.set(cookies.get("scanmanga", ""))
        self.cookie_crunchyscan.set(cookies.get("crunchyscan", ""))
        self.cookie_scanhentai.set(cookies.get("scanhentai", ""))
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
                self.cookie_headers[domain] = build_cf_clearance_cookie_header(cookie_value)

        self.last_known_cookies = {domain: (cookies.get(domain) or "").strip() for domain in COOKIE_DOMAINS}
        self.cbz_enabled.set(str(cbz).lower() in ("1", "true", "yes"))
        self.comicinfo_enabled.set(str(comicinfo_enabled).lower() in ("1", "true", "yes"))
        self.chapter_cover_enabled.set(str(chapter_cover_enabled).lower() in ("1", "true", "yes"))
        self.webp2jpg_enabled.set(str(webp2jpg_enabled).lower() in ("1", "true", "yes"))
        self.smart_resume_enabled.set(str(smart_resume_enabled).lower() in ("1", "true", "yes"))
        self.verbose_logs.set(str(verbose_logs_enabled).lower() in ("1", "true", "yes"))
        self.download_threads.set(clamp_download_threads(download_threads))
        self.url.set(last_url)  
        MangaApp.last_url_used = last_url
        
        # Initialisation des composants UI
        self.check_vars = []
        self.check_items = []
        self.image_progress_index = None
        self.download_in_progress = False
        self.pairs = []
        self.title = ""
        self.source_title_var = tk.StringVar(value="Manga/Manhwa/Comics...")
        self.cancel_event = threading.Event()
        self.cover_preview = None
        self.cover_url = ""
        self.volume_meta_by_url = {}
        self.series_metadata = {}
        self.catalog_state_summary = {}
        self.preview_window = None
        self.cookie_refresh_prompt_window = None
        self.preview_header_label = None
        self.preview_count_label = None
        self.preview_image_frame = None
        self.preview_image_label = None
        self.preview_status_label = None
        self.preview_prev_button = None
        self.preview_next_button = None
        self.preview_close_button = None
        self.preview_ctk_image = None
        self.preview_state = {"key": None, "title": "", "urls": [], "images": [], "index": 0}
        self.preview_request_id = 0
        self.preview_resize_after_id = None
        self.preview_spinner_after_id = None
        self.preview_spinner_running = False
        self.preview_spinner_index = 0
        self.preview_spinner_message = ""
        self.preview_cache = {}
        self.preview_cache_order = []
        self.gui_log_compact_entry = None
        self.gui_log_compact_count = 0
        self.gui_log_compact_updated_at = 0.0
        self.perf_records = []
        self.analysis_spinner_after_id = None
        self.analysis_spinner_running = False
        self.analysis_spinner_index = 0
        self.analysis_loading_overlay_text = ""
        self.cover_animation_frames = []
        self.cover_animation_durations = []
        self.cover_animation_index = 0
        self.cover_animation_after_id = None
        self.volume_render_after_id = None
        self.volume_virtual_refresh_after_id = None
        self.volume_layout_refresh_after_id = None
        self.volume_pool_prewarm_after_id = None
        self.volume_pool_prewarm_kind = None
        self.volume_layout_pending_mode = None
        self.volume_render_token = 0
        self.volume_render_complete_callback = None
        self.volume_render_feedback_enabled = True
        self.use_compact_volume_mode = False
        self.use_fast_volume_widgets = False
        self.volume_render_selection_state = []
        self.volume_virtualized = False
        self.volume_virtual_window = None
        self.volume_virtual_widget_pool = []
        self.volume_virtual_widget_pools = {}
        self.volume_virtual_widget_pool_mode = None
        self.volume_canvas_item_pool = []
        self.volume_canvas_header_pool = []
        self.volume_canvas_render_active = False
        self.volume_virtual_top_spacer = None
        self.volume_virtual_bottom_spacer = None
        self.volume_virtual_last_refresh_at = 0.0
        self.volume_virtual_refresh_processing = False
        self.volume_grouped_virtual_rows_cache_key = None
        self.volume_grouped_virtual_rows_cache = []
        self.filtered_volume_indices = []
        self.volume_index_to_widget = {}
        self.volume_label_cache_lower = []
        self.volume_error_entries = []
        self.error_row_widgets = []
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
        self.root.bind("<Control-r>", lambda _e: self.load_volumes(use_analysis_cache=False))
        self.root.bind("<Control-R>", lambda _e: self.load_volumes(use_analysis_cache=False))
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
        if text.startswith("[perf]"):
            self._record_perf_log(text)

        normalized_level = normalize_log_level(level)
        verbose_enabled = bool(getattr(self, "verbose_logs_cached", True))
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

        should_emit_debug = not (normalized_level == "debug" and not verbose_enabled)
        if should_emit_debug and getattr(self, "log_ready", False) and hasattr(self, "log_text"):
            self._queue_gui_log_entry(entry)

        if normalized_level == "debug" and not verbose_enabled:
            return

        console_enabled = bool(getattr(self, "console_logs_enabled_cached", True))
        if console_enabled:
            emit_console_log(
                message=text,
                level=normalized_level,
                context=context,
                timestamp=timestamp,
                with_emoji=CONSOLE_USE_EMOJI,
            )

    def _record_perf_log(self, text):
        match = re.match(r"\[perf\]\s*(.+?):\s*([0-9]+(?:\.[0-9]+)?)s", text or "")
        if not match:
            return
        self.perf_records.append((match.group(1).strip(), float(match.group(2))))
        if len(self.perf_records) > 200:
            self.perf_records = self.perf_records[-200:]

    def summarize_perf_records(self):
        records = list(getattr(self, "perf_records", []) or [])
        if not records:
            return "Aucune mesure performance disponible."
        grouped = {}
        for label, value in records:
            grouped.setdefault(label, []).append(value)
        return " | ".join(
            f"{label}: {sum(values):.2f}s total / {sum(values)/len(values):.2f}s moy."
            for label, values in sorted(grouped.items())
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

    def _queue_gui_log_entry(self, entry):
        """Regroupe les logs GUI pour éviter une action Tk par message."""
        entry = dict(entry)
        entry = self._compact_repetitive_gui_log(entry)
        if entry is not None:
            try:
                self.gui_log_queue.put_nowait(entry)
            except Exception:
                return
        should_schedule = False
        with self.gui_log_flush_lock:
            if not self.gui_log_flush_scheduled:
                self.gui_log_flush_scheduled = True
                should_schedule = True
        if should_schedule:
            self.run_on_ui(self._flush_gui_log_entries)

    def _compact_repetitive_gui_log(self, entry):
        """Compacte les logs tres bavards sans masquer les erreurs uniques."""
        message = repair_mojibake_text(entry.get("message", ""))
        context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        action = str(context.get("action") or "").strip()
        compactable = (
            action in {"image_retry", "download", "get_images_cache"}
            or message.startswith("Progression image:")
            or "Tentative " in message and "échouée" in message
        )
        if not compactable:
            previous = getattr(self, "gui_log_compact_entry", None)
            count = int(getattr(self, "gui_log_compact_count", 0) or 0)
            if previous is not None and count > 1:
                previous = dict(previous)
                previous["message"] = f"{previous.get('message', '')} (x{count})"
                try:
                    self.gui_log_queue.put_nowait(previous)
                except Exception:
                    pass
            self.gui_log_compact_entry = None
            self.gui_log_compact_count = 0
            return entry

        key = (
            normalize_log_level(entry.get("level", "info")),
            action,
            re.sub(r"https?://\S+", "<url>", message),
        )
        previous = getattr(self, "gui_log_compact_entry", None)
        previous_key = previous.get("_compact_key") if isinstance(previous, dict) else None
        now = time.time()
        if previous is not None and previous_key == key and now - float(getattr(self, "gui_log_compact_updated_at", 0.0) or 0.0) < 2.0:
            self.gui_log_compact_count = int(getattr(self, "gui_log_compact_count", 1) or 1) + 1
            self.gui_log_compact_updated_at = now
            return None
        if previous is not None:
            count = int(getattr(self, "gui_log_compact_count", 0) or 0)
            if count > 1:
                previous = dict(previous)
                previous["message"] = f"{previous.get('message', '')} (x{count})"
                try:
                    self.gui_log_queue.put_nowait(previous)
                except Exception:
                    pass
            else:
                try:
                    self.gui_log_queue.put_nowait(previous)
                except Exception:
                    pass
        entry["_compact_key"] = key
        self.gui_log_compact_entry = entry
        self.gui_log_compact_count = 1
        self.gui_log_compact_updated_at = now
        return None

    def _flush_gui_log_entries(self):
        """Insère un lot de logs dans le widget journal en une seule mise à jour."""
        if not getattr(self, "log_ready", False) or not hasattr(self, "log_text"):
            with self.gui_log_flush_lock:
                self.gui_log_flush_scheduled = False
            return

        compact_entry = getattr(self, "gui_log_compact_entry", None)
        compact_count = int(getattr(self, "gui_log_compact_count", 0) or 0)
        if compact_entry is not None and time.time() - float(getattr(self, "gui_log_compact_updated_at", 0.0) or 0.0) >= 0.5:
            compact_entry = dict(compact_entry)
            if compact_count > 1:
                compact_entry["message"] = f"{compact_entry.get('message', '')} (x{compact_count})"
            try:
                self.gui_log_queue.put_nowait(compact_entry)
            except Exception:
                pass
            self.gui_log_compact_entry = None
            self.gui_log_compact_count = 0

        drained = []
        for _ in range(GUI_LOG_FLUSH_MAX_BATCH):
            try:
                drained.append(self.gui_log_queue.get_nowait())
            except queue.Empty:
                break

        if drained:
            self.log_text.configure(state="normal")
            for entry in drained:
                if not self._should_display_log_entry(entry):
                    continue
                entry["message"] = repair_mojibake_text(entry.get("message", ""))
                level = normalize_log_level(entry.get("level", "info"))
                self._insert_log_line(self._format_log_entry(entry), level)
            self.log_text.configure(state="disabled")
            if self.log_autoscroll.get():
                self._scroll_log_to_bottom()

        if not self.gui_log_queue.empty():
            self.root.after(GUI_LOG_FLUSH_INTERVAL_MS, self._flush_gui_log_entries)
            return

        with self.gui_log_flush_lock:
            self.gui_log_flush_scheduled = False
            has_new_entries = not self.gui_log_queue.empty()
            if has_new_entries:
                self.gui_log_flush_scheduled = True
        if has_new_entries:
            self.root.after(GUI_LOG_FLUSH_INTERVAL_MS, self._flush_gui_log_entries)

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
        try:
            while True:
                self.gui_log_queue.get_nowait()
        except queue.Empty:
            pass
        with self.gui_log_flush_lock:
            self.gui_log_flush_scheduled = False
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

    def _scroll_error_rows_to_end(self):
        canvas = getattr(getattr(self, "error_list_frame", None), "_parent_canvas", None)
        if canvas is None:
            return
        try:
            canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _create_error_row_widget(self, parent, entry, index):
        status_code = entry.get("status_code")
        status_text = "" if status_code in (None, "") else str(status_code)
        stage_value = repair_mojibake_text(entry.get("stage", "") or "download")
        reason_value = repair_mojibake_text(entry.get("reason", "") or "Erreur inconnue")
        action_value = repair_mojibake_text(entry.get("action", "") or "Aucune recommandation")
        tome_value = repair_mojibake_text(entry.get("tome", "") or "?")
        time_value = repair_mojibake_text(entry.get("time", "") or "--:--:--")

        row = ctk.CTkFrame(
            parent,
            fg_color="#ffffff" if index % 2 == 0 else self.palette["tree_alt"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        row.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(row, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        header.grid_columnconfigure(1, weight=1)

        badges = ctk.CTkFrame(header, fg_color="transparent")
        badges.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            badges,
            text=time_value,
            width=72,
            height=28,
            corner_radius=8,
            fg_color=self.palette["card_alt"],
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            badges,
            text=stage_value,
            width=88,
            height=28,
            corner_radius=8,
            fg_color=self.palette["accent_soft"],
            text_color=self.palette["accent_hover"],
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 8))
        if status_text:
            ctk.CTkLabel(
                badges,
                text=f"HTTP {status_text}",
                width=90,
                height=28,
                corner_radius=8,
                fg_color=self.palette["danger_soft"],
                text_color=self.palette["danger_text"],
                font=("Segoe UI Semibold", 10),
            ).pack(side="left")

        ctk.CTkLabel(
            header,
            text=tome_value,
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 11),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew")

        body = ctk.CTkFrame(row, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        body.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            body,
            text="Cause",
            width=60,
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).grid(row=0, column=0, sticky="nw", padx=(0, 8))
        ctk.CTkLabel(
            body,
            text=reason_value,
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(
            body,
            text="Action",
            width=60,
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=(6, 0))
        ctk.CTkLabel(
            body,
            text=action_value,
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        return row

    def _append_volume_error_row(self, entry):
        if not hasattr(self, "error_list_frame"):
            return
        row_index = len(self.error_row_widgets)
        row = self._create_error_row_widget(self.error_list_frame, entry, row_index)
        row.pack(fill="x", pady=(0, 10))
        self.error_row_widgets.append(row)
        if len(self.error_row_widgets) > MAX_VISIBLE_ERROR_ROWS:
            stale = self.error_row_widgets.pop(0)
            stale.destroy()
        self._scroll_error_rows_to_end()

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
        for widget in getattr(self, "error_row_widgets", []) or []:
            try:
                widget.destroy()
            except Exception:
                pass
        self.error_row_widgets = []
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
            "app_bg": "#f1f3f6",
            "card_bg": "#ffffff",
            "card_alt": "#f6f8fa",
            "panel_bg": "#f7f8fa",
            "panel_shell": "#e3e7ec",
            "text": "#16202a",
            "muted": "#667382",
            "muted_strong": "#566273",
            "accent": "#24579a",
            "accent_hover": "#184679",
            "accent_soft": "#edf3fa",
            "accent_soft_hover": "#dfe9f5",
            "danger": "#d34b4b",
            "danger_soft": "#f6e5e8",
            "danger_text": "#8e3b47",
            "danger_border": "#dfc0c6",
            "success_soft": "#e3efe7",
            "success_text": "#38654a",
            "success_border": "#b6cfbf",
            "success_border_strong": "#7ca48a",
            "warning_soft": "#f7ead4",
            "warning_text": "#7a5f2f",
            "warning_border": "#e1cfac",
            "border": "#d0d7df",
            "input_bg": "#ffffff",
            "canvas_bg": "#fbfcfd",
            "log_bg": "#fcfdfe",
            "progress_trough": "#dde3ea",
            "tree_alt": "#fafbfd",
        }
        self.ui_metrics = {
            "button_height": 36,
            "button_height_compact": 34,
            "button_width_sm": 108,
            "button_width_md": 118,
            "button_width_lg": 196,
            "font_body": ("Segoe UI", 11),
            "font_body_sm": ("Segoe UI", 10),
            "font_button": ("Segoe UI Semibold", 11),
            "font_button_lg": ("Segoe UI Semibold", 12),
        }

        try:
            self.root.configure(fg_color=self.palette["app_bg"])
        except Exception:
            self.root.configure(bg=self.palette["app_bg"])

        style.configure(
            ".",
            font=self.ui_metrics["font_body"],
            background=self.palette["app_bg"],
            foreground=self.palette["text"],
            troughcolor=self.palette["app_bg"],
            selectbackground=self.palette["accent"],
            selectforeground="#ffffff",
        )
        style.map(".", foreground=[("disabled", "#9aa8b8")])
        style.configure("App.TFrame", background=self.palette["app_bg"])
        style.configure("Card.TFrame", background=self.palette["card_bg"])
        style.configure("OptionLine.TFrame", background=self.palette["panel_bg"])
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
        style.configure("OptionLineMuted.TLabel", background=self.palette["panel_bg"], foreground=self.palette["muted_strong"])
        style.configure("Muted.TLabel", background=self.palette["app_bg"], foreground=self.palette["muted"])
        style.configure("Title.TLabel", background=self.palette["app_bg"], foreground=self.palette["text"], font=("Segoe UI Semibold", 16))
        style.configure("Subtitle.TLabel", background=self.palette["app_bg"], foreground=self.palette["muted"], font=("Segoe UI", 10))

        style.configure("Card.TCheckbutton", background=self.palette["card_bg"], foreground=self.palette["text"], padding=(2, 1))
        style.map("Card.TCheckbutton", background=[("active", self.palette["card_bg"])])
        style.configure("OptionLine.TCheckbutton", background=self.palette["panel_bg"], foreground=self.palette["text"], padding=(2, 1))
        style.map("OptionLine.TCheckbutton", background=[("active", self.palette["panel_bg"])])
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
            rowheight=24,
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
        style.map(
            "Treeview",
            background=[("selected", "#dcecff")],
            foreground=[("selected", self.palette["text"])],
        )
        style.configure(
            "Error.Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground=self.palette["text"],
            rowheight=28,
            bordercolor=self.palette["border"],
            lightcolor=self.palette["border"],
            darkcolor=self.palette["border"],
        )
        style.configure(
            "Error.Treeview.Heading",
            background=self.palette["card_alt"],
            foreground=self.palette["text"],
            borderwidth=0,
            relief="flat",
            padding=(8, 6),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Error.Treeview.Heading",
            background=[("active", "#e6effa")],
        )
        style.map(
            "Error.Treeview",
            background=[("selected", "#dcecff")],
            foreground=[("selected", self.palette["text"])],
        )

        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=self.palette["progress_trough"],
            background=self.palette["accent"],
            thickness=14,
        )

    def _format_watchlist_datetime(self, value):
        text = str(value or "").strip()
        if not text:
            return "-"
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone()
            return parsed.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return text[:16]

    def _set_watchlist_status(self, message, level="info"):
        if hasattr(self, "watchlist_status"):
            self.watchlist_status.set(message)
        if message:
            self.log(message, level=level)

    def _build_watchlist_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(
            parent,
            fg_color=self.palette["panel_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        header.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 8))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Surveille les catalogues et vérifie les nouveaux chapitres sans lancer de téléchargement.",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=self.ui_metrics["font_body"],
            anchor="w",
        ).grid(row=0, column=0, columnspan=5, sticky="ew", padx=14, pady=(12, 6))

        self.watchlist_url_entry = ctk.CTkEntry(
            header,
            textvariable=self.watchlist_url,
            height=36,
            corner_radius=7,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            font=("Segoe UI", 11),
        )
        self.watchlist_url_entry.grid(row=1, column=0, sticky="ew", padx=(14, 8), pady=(0, 12))
        self._bind_catalogue_url_paste(self.watchlist_url_entry, self.watchlist_url)
        self._attach_link_placeholder(self.watchlist_url_entry, self.watchlist_url, "URL catalogue à suivre", None)

        actions = (
            ("Ajouter", self.add_watchlist_url_from_entry, self.palette["accent"], self.palette["accent_hover"], "#ffffff"),
            ("URL courante", self.add_current_url_to_watchlist, self.palette["panel_bg"], self.palette["card_alt"], self.palette["text"]),
            ("Vérifier tout", self.check_all_watchlist_urls, self.palette["panel_bg"], self.palette["card_alt"], self.palette["text"]),
            ("Rafraîchir", self.refresh_watchlist_view, self.palette["panel_bg"], self.palette["card_alt"], self.palette["text"]),
        )
        for col, (label, command, fg, hover, text_color) in enumerate(actions, start=1):
            button = ctk.CTkButton(
                header,
                text=label,
                command=command,
                width=112,
                height=36,
                corner_radius=7,
                fg_color=fg,
                hover_color=hover,
                border_width=0 if fg == self.palette["accent"] else 1,
                border_color=self.palette["border"],
                text_color=text_color,
                font=self.ui_metrics["font_button"],
            )
            button.grid(row=1, column=col, sticky="e", padx=(0, 8 if col < len(actions) else 14), pady=(0, 12))
            if label == "Vérifier tout":
                self.watchlist_check_all_button = button

        self.watchlist_status_label = ctk.CTkLabel(
            parent,
            textvariable=self.watchlist_status,
            fg_color="transparent",
            text_color=self.palette["muted_strong"],
            font=("Segoe UI", 10),
            anchor="w",
        )
        self.watchlist_status_label.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))

        self.watchlist_rows_frame = ctk.CTkScrollableFrame(
            parent,
            fg_color=self.palette["canvas_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
            scrollbar_button_color="#c5d3e2",
            scrollbar_button_hover_color="#aebfd1",
        )
        self.watchlist_rows_frame.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self.watchlist_rows_frame.grid_columnconfigure(0, weight=1)
        self.refresh_watchlist_view()

    def refresh_watchlist_view(self):
        frame = getattr(self, "watchlist_rows_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        self.watchlist_rows = {}
        entries = get_watchlist_entries_with_state()
        if not entries:
            ctk.CTkLabel(
                frame,
                text="Aucun catalogue suivi pour le moment.",
                fg_color="transparent",
                text_color=self.palette["muted"],
                font=("Segoe UI", 12),
                anchor="center",
            ).grid(row=0, column=0, sticky="nsew", padx=16, pady=24)
            self.watchlist_status.set("Ajoute une URL catalogue pour commencer le suivi.")
            return

        for row_index, entry in enumerate(entries):
            self._add_watchlist_row(frame, row_index, entry)
        self.watchlist_status.set(f"{len(entries)} catalogue(s) suivi(s).")

    def _add_watchlist_row(self, parent, row_index, entry):
        url = entry.get("url") or ""
        new_count = int(entry.get("last_new_count") or 0)
        tone_bg = "#edf8ef" if new_count else self.palette["card_bg"]
        tone_border = "#98c9a7" if new_count else self.palette["panel_shell"]
        row = ctk.CTkFrame(parent, fg_color=tone_bg, corner_radius=7, border_width=1, border_color=tone_border)
        row.grid(row=row_index, column=0, sticky="ew", padx=10, pady=(10 if row_index == 0 else 0, 8))
        row.grid_columnconfigure(0, weight=1)

        title_line = ctk.CTkFrame(row, fg_color="transparent")
        title_line.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 2))
        title_line.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            title_line,
            text=entry.get("title") or url,
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 12),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            title_line,
            text=f"+{new_count}" if new_count else "à jour",
            width=72,
            height=24,
            corner_radius=5,
            fg_color=self.palette["success_soft"] if new_count else self.palette["card_alt"],
            text_color=self.palette["success_text"] if new_count else self.palette["muted_strong"],
            font=("Segoe UI Semibold", 10),
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        detail = (
            f"{entry.get('domain') or '-'} | {int(entry.get('last_count') or 0)} élément(s) connus"
            f" | Dernière vérif: {self._format_watchlist_datetime(entry.get('last_checked_at'))}"
        )
        ctk.CTkLabel(
            row,
            text=detail,
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI", 10),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 2))
        ctk.CTkLabel(
            row,
            text=url,
            fg_color="transparent",
            text_color="#2f67b1",
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        for label, command, danger in (
            ("Ouvrir", lambda safe_url=url: self.open_watchlist_url(safe_url), False),
            ("Vérifier", lambda safe_url=url: self.check_watchlist_url(safe_url), False),
            ("Supprimer", lambda safe_url=url: self.remove_watchlist_entry(safe_url), True),
        ):
            ctk.CTkButton(
                actions,
                text=label,
                command=command,
                width=96,
                height=30,
                corner_radius=6,
                fg_color=self.palette["danger_soft"] if danger else self.palette["panel_bg"],
                hover_color="#f0d6db" if danger else self.palette["card_alt"],
                border_width=1,
                border_color=self.palette["danger_border"] if danger else self.palette["border"],
                text_color=self.palette["danger_text"] if danger else self.palette["text"],
                font=("Segoe UI", 10),
            ).pack(side="right", padx=(8, 0))
        self.watchlist_rows[url] = row

    def _add_watchlist_url(self, url, title=""):
        safe_url = normalize_image_url((url or "").strip())
        if not is_valid_catalogue_url(safe_url):
            self.toast("URL catalogue invalide")
            self._set_watchlist_status("URL non ajoutée: format catalogue invalide.", level="warning")
            return False
        if add_or_update_watchlist_url(safe_url, title=title, enabled=True):
            self.refresh_watchlist_view()
            self.toast("Catalogue ajouté au suivi")
            self._set_watchlist_status("Catalogue ajouté au suivi.", level="success")
            return True
        return False

    def add_watchlist_url_from_entry(self):
        url = "" if getattr(self, "watchlist_placeholder_active", False) else self.watchlist_url.get().strip()
        if self._add_watchlist_url(url):
            self.watchlist_url.set("")

    def add_current_url_to_watchlist(self):
        title = ""
        try:
            title = self.source_title_var.get().strip()
        except Exception:
            title = ""
        self._add_watchlist_url(self.url.get().strip(), title=title)

    def remove_watchlist_entry(self, url):
        if remove_watchlist_url(url):
            self.refresh_watchlist_view()
            self.toast("Catalogue retiré du suivi")
            self._set_watchlist_status("Catalogue retiré du suivi.", level="info")

    def open_watchlist_url(self, url):
        if not url:
            return
        self.url.set(url)
        self._select_config_tab("download")
        self._set_analysis_status_label("URL de suivi chargée. Lance l'analyse si besoin.", success=None)

    def _collect_watchlist_jobs(self, urls=None):
        wanted = {_catalog_state_key(url) for url in (urls or []) if _catalog_state_key(url)}
        entries = get_watchlist_entries_with_state()
        if wanted:
            entries = [entry for entry in entries if _catalog_state_key(entry.get("url")) in wanted]
        jobs = []
        for entry in entries:
            url = entry.get("url") or ""
            domain = self.get_domain_from_url(url)
            if domain in COOKIE_DOMAINS:
                self.sync_cookie_source_for_domain(domain)
            jobs.append(
                {
                    "url": url,
                    "domain": domain,
                    "cookie": self.get_cookie(url),
                    "ua": self.get_request_user_agent_for_url(url),
                }
            )
        return jobs

    def check_all_watchlist_urls(self):
        self._start_watchlist_check(self._collect_watchlist_jobs())

    def check_watchlist_url(self, url):
        self._start_watchlist_check(self._collect_watchlist_jobs(urls=[url]))

    def _start_watchlist_check(self, jobs):
        if self.watchlist_check_in_progress:
            self.toast("Vérification déjà en cours")
            return
        if not jobs:
            self._set_watchlist_status("Aucun catalogue à vérifier.", level="warning")
            return
        self.watchlist_check_in_progress = True
        if hasattr(self, "watchlist_check_all_button"):
            self.watchlist_check_all_button.configure(state="disabled")
        self._set_watchlist_status(f"Vérification de {len(jobs)} catalogue(s)...", level="info")

        def worker():
            total = len(jobs)
            successes = 0
            failures = 0
            new_total = 0
            for index, job in enumerate(jobs, start=1):
                url = job["url"]
                try:
                    analysis = fetch_manga_analysis(url, job.get("cookie") or "", job.get("ua") or "", emit_logs=False)
                    store_cached_analysis(
                        url,
                        job.get("ua") or "",
                        analysis.title,
                        analysis.pairs,
                        analysis.volume_metadata,
                        analysis.series_metadata,
                        analysis.html_content,
                    )
                    summary = update_catalog_state(
                        url,
                        analysis.title,
                        analysis.pairs,
                        domain=job.get("domain") or "",
                        volume_metadata=analysis.volume_metadata,
                    )
                    successes += 1
                    new_count = int((summary or {}).get("new_count") or 0)
                    new_total += new_count
                    message = format_catalog_state_summary(summary) or f"{analysis.title}: vérifié."
                    self.run_on_ui(
                        self._set_watchlist_status,
                        f"[{index}/{total}] {message}",
                        "success" if new_count else "info",
                    )
                except Exception as exc:
                    failures += 1
                    self.run_on_ui(self._set_watchlist_status, f"[{index}/{total}] Echec suivi: {url} ({exc})", "warning")
            self.run_on_ui(self._finish_watchlist_check, successes, failures, new_total)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_watchlist_check(self, successes, failures, new_total):
        self.watchlist_check_in_progress = False
        if hasattr(self, "watchlist_check_all_button"):
            self.watchlist_check_all_button.configure(state="normal")
        self.refresh_watchlist_view()
        if failures:
            self._set_watchlist_status(
                f"Vérification terminée: {successes} OK, {failures} échec(s), {new_total} nouveauté(s).",
                level="warning",
            )
        else:
            self._set_watchlist_status(
                f"Vérification terminée: {successes} OK, {new_total} nouveauté(s).",
                level="success",
            )

    def setup_ui(self):
        """Configure tous les elements de l'interface graphique."""
        self.progress = tk.DoubleVar(value=0)
        button_h = self.ui_metrics["button_height"]
        compact_button_h = self.ui_metrics["button_height_compact"]
        button_w_sm = self.ui_metrics["button_width_sm"]
        button_w_md = self.ui_metrics["button_width_md"]
        button_w_lg = self.ui_metrics["button_width_lg"]
        body_font = self.ui_metrics["font_body"]
        body_font_sm = self.ui_metrics["font_body_sm"]
        button_font = self.ui_metrics["font_button"]
        button_font_lg = self.ui_metrics["font_button_lg"]

        def create_titled_section(parent, title, bottom_margin, expand=False):
            section_wrap = ctk.CTkFrame(parent, fg_color="transparent")
            section_wrap.pack(fill="both" if expand else "x", expand=expand, pady=(0, bottom_margin))
            if title:
                section_tabs_header = ctk.CTkFrame(
                    section_wrap,
                    fg_color="transparent",
                )
                section_tabs_header.pack(fill="x", expand=False, pady=(0, 0))
                section_tab_label = ctk.CTkLabel(
                    section_tabs_header,
                    text=title,
                    font=("Segoe UI Semibold", 11),
                    height=24,
                    corner_radius=0,
                    fg_color="transparent",
                    text_color=self.palette["text"],
                )
                section_tab_label.pack(side="left", padx=(2, 0), pady=(0, 4))

            section_border = ctk.CTkFrame(
                section_wrap,
                fg_color=self.palette["card_bg"],
                corner_radius=6,
                border_width=1,
                border_color=self.palette["border"],
            )
            section_border.pack(fill="both" if expand else "x", expand=expand)

            section_content_panel = ctk.CTkFrame(
                section_border,
                fg_color=self.palette["card_bg"],
                corner_radius=0,
            )
            section_content_panel.pack(fill="both", expand=True, padx=0, pady=0)

            section_content = ctk.CTkFrame(section_content_panel, fg_color="transparent")
            section_content.pack(fill="both", expand=True, padx=14, pady=12)
            return section_content, section_wrap

        main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=18, pady=8)

        top_tabs_wrap = ctk.CTkFrame(main_frame, fg_color="transparent")
        top_tabs_wrap.pack(fill="both", expand=True, pady=(0, 6))

        self.config_tabs_header = ctk.CTkFrame(top_tabs_wrap, fg_color="transparent")
        self.config_tabs_header.pack(fill="x", expand=False, pady=(0, 0))
        self.config_tabs_header.grid_columnconfigure(0, weight=1)

        self.config_content_border = ctk.CTkFrame(
            top_tabs_wrap,
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        self.config_content_border.pack(fill="both", expand=True, pady=(0, 0))
        self.config_content_panel = ctk.CTkFrame(
            self.config_content_border,
            fg_color=self.palette["card_bg"],
            corner_radius=0,
        )
        self.config_content_panel.pack(fill="both", expand=True, padx=0, pady=0)

        self.config_download_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_journal_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_watch_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_error_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_auth_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")
        self.config_options_page = ttk.Frame(self.config_content_panel, style="Card.TFrame")

        self.config_tab_pages = {
            "download": self.config_download_page,
            "journal": self.config_journal_page,
            "watch": self.config_watch_page,
            "error": self.config_error_page,
            "auth": self.config_auth_page,
            "options": self.config_options_page,
        }
        self.config_tab_buttons = {}
        self.config_tab_order = ("download", "journal", "watch", "error", "auth", "options")
        self.active_config_tab = "download"
        self.active_selection_tab = "selection"
        self.auth_tab_title = "Authentification (0/5)"
        self.auth_tab_visual_state = "pending"
        self.auth_tab_progress_text = "0/5"
        self.config_tab_bar = ctk.CTkFrame(self.config_tabs_header, fg_color="transparent")
        self.config_tab_bar.grid(row=0, column=0, sticky="w")
        for key in self.config_tab_order:
            button = ctk.CTkButton(
                self.config_tab_bar,
                text="",
                command=lambda current_key=key: self._select_config_tab(current_key),
                width=170 if key == "download" else (190 if key == "auth" else 120),
                height=compact_button_h,
                corner_radius=6,
                border_width=0,
                font=button_font,
            )
            button.pack(side="left", padx=(0, 8))
            self.config_tab_buttons[key] = button

        def create_config_tab_content(parent):
            content = ttk.Frame(parent, style="Card.TFrame", padding=(12, 12, 12, 12))
            content.pack(fill="both", expand=True)
            return content

        self.config_download_tab = create_config_tab_content(self.config_download_page)
        self.config_journal_tab = create_config_tab_content(self.config_journal_page)
        self.config_watch_tab = create_config_tab_content(self.config_watch_page)
        self.config_error_tab = create_config_tab_content(self.config_error_page)
        self.config_auth_tab = create_config_tab_content(self.config_auth_page)
        self.config_options_tab = create_config_tab_content(self.config_options_page)
        self._refresh_config_tab_buttons()
        self._select_config_tab("download")
        self.config_auth_tab.grid_columnconfigure(0, weight=1)
        self.config_auth_tab.grid_columnconfigure(1, weight=0)
        self.config_auth_tab.grid_columnconfigure(2, weight=0)
        self._build_watchlist_tab(self.config_watch_tab)

        log_frame = ctk.CTkFrame(
            self.config_journal_tab,
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        log_frame.pack(fill="both", expand=True, padx=(4, 4), pady=(4, 2))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        log_toolbar = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_toolbar.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        log_toolbar.grid_columnconfigure(1, weight=0)
        log_toolbar.grid_columnconfigure(2, weight=1)

        left_toolbar = ctk.CTkFrame(log_toolbar, fg_color="transparent")
        left_toolbar.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            left_toolbar,
            text="Niveau",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=body_font,
        ).pack(side="left", padx=(0, 8))
        self.log_filter_combo = ctk.CTkComboBox(
            left_toolbar,
            width=144,
            height=compact_button_h,
            corner_radius=6,
            state="readonly",
            values=["all", "info", "success", "warning", "error", "debug", "cbz"],
            variable=self.log_filter_level,
            command=self.refresh_log_view,
            fg_color=self.palette["input_bg"],
            border_color=self.palette["border"],
            button_color=self.palette["accent"],
            button_hover_color=self.palette["accent_hover"],
            dropdown_fg_color="#ffffff",
            dropdown_hover_color=self.palette["card_alt"],
            dropdown_text_color=self.palette["text"],
            text_color=self.palette["text"],
            font=body_font,
            dropdown_font=body_font,
        )
        self.log_filter_combo.pack(side="left")
        self.log_filter_combo.set("all")

        self.log_autoscroll_checkbox = ctk.CTkCheckBox(
            log_toolbar,
            text="Auto-scroll",
            variable=self.log_autoscroll,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            border_color=self.palette["border"],
            text_color=self.palette["text"],
            font=body_font,
        )
        self.log_autoscroll_checkbox.grid(row=0, column=1, sticky="w", padx=(14, 0))

        actions_toolbar = ctk.CTkFrame(log_toolbar, fg_color="transparent")
        actions_toolbar.grid(row=0, column=2, sticky="e")
        for label, command in (
            ("Exporter", self.export_visible_logs),
            ("Copier", self.copy_visible_logs),
            ("Effacer", self.clear_log_entries),
        ):
            ctk.CTkButton(
                actions_toolbar,
                text=label,
                command=command,
                width=button_w_md,
                height=compact_button_h,
                corner_radius=6,
                fg_color=self.palette["panel_bg"],
                hover_color=self.palette["card_alt"],
                border_width=1,
                border_color=self.palette["border"],
                text_color=self.palette["text"],
                font=button_font,
            ).pack(side="right", padx=(6, 0))

        self.log_text = ctk.CTkTextbox(
            log_frame,
            height=220,
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
            fg_color=self.palette["log_bg"],
            text_color=self.palette["text"],
            scrollbar_button_color="#c5d3e2",
            scrollbar_button_hover_color="#aebfd1",
            font=("Consolas", 11),
            wrap="word",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.log_text.configure(state="disabled")

        self.log_text.tag_config("debug", foreground="#6b7280")
        self.log_text.tag_config("success", foreground=self.palette["success_text"])
        self.log_text.tag_config("info", foreground="#2f67b1")
        self.log_text.tag_config("error", foreground=self.palette["danger_text"])
        self.log_text.tag_config("warning", foreground="#8a6a32")
        self.log_text.tag_config("cbz", foreground="#6d4ec7")

        font_label = ("Segoe UI", 12)
        font_entry = ("Segoe UI", 12)
        badge_font = button_font

        def create_ctk_badge(parent):
            return ctk.CTkLabel(
                parent,
                text="Validation en cours",
                width=132,
                height=30,
                corner_radius=6,
                fg_color=self.palette["warning_soft"],
                text_color=self.palette["warning_text"],
                font=button_font,
            )

        auth_surface = ctk.CTkFrame(
            self.config_auth_tab,
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        auth_surface.grid(row=0, column=0, columnspan=3, sticky="ew", padx=(4, 4), pady=(4, 2))
        auth_surface.grid_columnconfigure(0, weight=0)
        auth_surface.grid_columnconfigure(1, weight=1)
        auth_surface.grid_columnconfigure(2, weight=0)

        row = 0

        ctk.CTkLabel(
            auth_surface,
            text="Renseigne les cookies par domaine et le User-Agent. Les controles se font en arriere-plan.",
            text_color=self.palette["muted"],
            font=body_font,
            anchor="w",
        ).grid(row=row, column=0, columnspan=3, sticky="ew", padx=(14, 14), pady=(14, 4))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_fr_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_fr_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_fr,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_fr_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_fr_status = create_ctk_badge(auth_surface)
        self.cookie_fr_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_net_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_net_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_net,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_net_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_net_status = create_ctk_badge(auth_surface)
        self.cookie_net_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_origines_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_origines_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_origines,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_origines_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_origines_status = create_ctk_badge(auth_surface)
        self.cookie_origines_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_hentai_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_hentai_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_hentai,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_hentai_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_hentai_status = create_ctk_badge(auth_surface)
        self.cookie_hentai_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_toonfr_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_toonfr_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_toonfr,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_toonfr_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_toonfr_status = create_ctk_badge(auth_surface)
        self.cookie_toonfr_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_ortega_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_ortega_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_ortega,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_ortega_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_ortega_status = create_ctk_badge(auth_surface)
        self.cookie_ortega_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_hentaizone_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_hentaizone_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_hentaizone,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_hentaizone_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_hentaizone_status = create_ctk_badge(auth_surface)
        self.cookie_hentaizone_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_scanmanga_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_scanmanga_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_scanmanga,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_scanmanga_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_scanmanga_status = create_ctk_badge(auth_surface)
        self.cookie_scanmanga_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_crunchyscan_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_crunchyscan_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_crunchyscan,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_crunchyscan_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_crunchyscan_status = create_ctk_badge(auth_surface)
        self.cookie_crunchyscan_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.cookie_scanhentai_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.cookie_scanhentai_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.cookie_scanhentai,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            show="*",
        )
        self.cookie_scanhentai_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.cookie_scanhentai_status = create_ctk_badge(auth_surface)
        self.cookie_scanhentai_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        ctk.CTkLabel(
            auth_surface,
            textvariable=self.ua_label_var,
            text_color=self.palette["text"],
            font=font_label,
        ).grid(
            row=row, column=0, sticky="w", pady=6, padx=(14, 12)
        )
        self.ua_entry = ctk.CTkEntry(
            auth_surface,
            textvariable=self.ua,
            font=font_entry,
            height=36,
            corner_radius=6,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
        )
        self.ua_entry.grid(row=row, column=1, pady=6, sticky="ew")
        self.ua_status = create_ctk_badge(auth_surface)
        self.ua_status.grid(row=row, column=2, sticky="w", padx=(12, 14))
        row += 1

        auth_actions_row = ctk.CTkFrame(auth_surface, fg_color="transparent")
        auth_actions_row.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(12, 12), padx=(14, 14))
        auth_actions_row.columnconfigure(0, weight=1)
        ctk.CTkButton(
            auth_actions_row,
            text="Tester tout",
            command=self.run_auth_diagnostics,
            width=button_w_md,
            height=compact_button_h,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
            font=button_font,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
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
            width=button_w_md,
            height=compact_button_h,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
            font=button_font,
        ).grid(row=0, column=1, sticky="e")
        row += 1

        options_intro = ttk.Label(
            self.config_options_tab,
            text="Ajuste le format de sortie et le comportement des journaux.",
            style="CardMuted.TLabel",
            font=("Segoe UI", 9),
        )
        options_intro.pack(anchor="w", padx=(6, 6), pady=(2, 6))

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

        output_box = ctk.CTkFrame(
            left_stack,
            fg_color=self.palette["panel_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        output_box.grid(row=0, column=0, sticky="nsew")
        output_inner = ctk.CTkFrame(output_box, fg_color="transparent")
        output_inner.pack(fill="both", expand=True, padx=10, pady=(6, 6))

        save_row = ctk.CTkFrame(left_stack, fg_color="transparent")
        save_row.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

        logs_box = ctk.CTkFrame(
            options_groups,
            fg_color=self.palette["panel_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        logs_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        logs_inner = ctk.CTkFrame(logs_box, fg_color="transparent")
        logs_inner.pack(fill="both", expand=True, padx=10, pady=8)

        ctk.CTkLabel(
            output_inner,
            text="Sortie",
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            logs_inner,
            text="Journal et affichage",
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkButton(
            save_row,
            text="Sauvegarder paramètres",
            command=self.save_current_cookie,
            height=34,
            corner_radius=6,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            text_color="#ffffff",
            font=("Segoe UI Semibold", 10),
        ).pack(expand=True)

        def add_option_line(parent, text, variable, description, command=None, bottom=6):
            line = ctk.CTkFrame(
                parent,
                fg_color=self.palette["panel_bg"],
                corner_radius=6,
                border_width=1,
                border_color=self.palette["panel_shell"],
            )
            line.pack(fill="x", anchor="w", pady=(0, bottom))
            check = ctk.CTkCheckBox(
                line,
                text=text,
                variable=variable,
                fg_color=self.palette["accent"],
                hover_color=self.palette["accent_hover"],
                border_color=self.palette["border"],
                checkmark_color="#ffffff",
                text_color=self.palette["text"],
                font=("Segoe UI", 11),
                checkbox_width=20,
                checkbox_height=20,
                corner_radius=4,
                command=command,
            )
            check.pack(anchor="w", padx=12, pady=(8, 1))
            ctk.CTkLabel(
                line,
                text=description,
                fg_color="transparent",
                text_color=self.palette["muted_strong"],
                font=("Segoe UI", 9),
                justify="left",
                anchor="w",
                wraplength=440,
            ).pack(anchor="w", fill="x", padx=(42, 12), pady=(0, 8))

        add_option_line(
            output_inner,
            ".CBZ",
            self.cbz_enabled,
            "Crée une archive CBZ par tome/chapitre téléchargé.",
            bottom=6,
        )
        add_option_line(
            output_inner,
            "ComicInfo.xml",
            self.comicinfo_enabled,
            "Ajoute les métadonnées Komga dans chaque archive CBZ.",
            bottom=6,
        )
        add_option_line(
            output_inner,
            "Couverture chapitres",
            self.chapter_cover_enabled,
            "Ajoute la couverture en première page des CBZ de chapitres uniquement.",
            bottom=6,
        )
        add_option_line(
            output_inner,
            "WEBP en JPG",
            self.webp2jpg_enabled,
            "Convertit les images WEBP en JPG pour une compatibilité maximale.",
            bottom=6,
        )
        threads_line = ctk.CTkFrame(
            output_inner,
            fg_color=self.palette["card_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["panel_shell"],
        )
        threads_line.pack(fill="x", anchor="w", pady=(0, 0))
        threads_header = ctk.CTkFrame(threads_line, fg_color="transparent")
        threads_header.pack(fill="x", padx=12, pady=(8, 1))
        ctk.CTkLabel(
            threads_header,
            text="Téléchargements parallèles",
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self.download_threads_menu = ctk.CTkOptionMenu(
            threads_header,
            values=[str(value) for value in (1, 2, 3, 4, 6, 8)],
            command=lambda value: self.download_threads.set(clamp_download_threads(value)),
            width=78,
            height=28,
            fg_color=self.palette["panel_bg"],
            button_color=self.palette["accent"],
            button_hover_color=self.palette["accent_hover"],
            text_color=self.palette["text"],
            dropdown_fg_color=self.palette["panel_bg"],
            dropdown_hover_color=self.palette["card_alt"],
            dropdown_text_color=self.palette["text"],
            font=("Segoe UI", 10),
        )
        self.download_threads_menu.set(str(clamp_download_threads(self.download_threads.get())))
        self.download_threads_menu.pack(side="right")
        ctk.CTkLabel(
            threads_line,
            text="Ajuste la rapidité sans saturer les sites protégés. 3 reste le réglage conseillé.",
            fg_color="transparent",
            text_color=self.palette["muted_strong"],
            font=("Segoe UI", 9),
            justify="left",
            anchor="w",
            wraplength=440,
        ).pack(anchor="w", fill="x", padx=12, pady=(0, 8))
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

        source_card, self.source_section_tabs = create_titled_section(self.config_download_tab, "", 8)

        url_cover_frame = ctk.CTkFrame(
            source_card,
            fg_color=self.palette["panel_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["panel_shell"],
        )
        url_cover_frame.pack(fill="x")
        cover_w, cover_h = self.get_cover_target_size()

        self.cover_frame = ctk.CTkFrame(
            url_cover_frame,
            width=cover_w,
            height=cover_h,
            fg_color=self.palette["card_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        self.cover_frame.pack_propagate(False)
        self.cover_frame.pack(side="left", padx=(14, 16), pady=14)
        self.cover_label = tk.Label(
            self.cover_frame,
            bg=self.palette["canvas_bg"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            text="",
            fg=self.palette["muted"],
            font=("Segoe UI", 9),
        )
        self.cover_label.pack(fill="both", expand=True)
        self._show_default_cover_placeholder()

        url_frame = ctk.CTkFrame(url_cover_frame, fg_color="transparent")
        url_frame.pack(side="left", fill="x", expand=True, padx=(0, 14), pady=14)

        source_meta_row = ctk.CTkFrame(url_frame, fg_color="transparent")
        source_meta_row.pack(fill="x")

        ctk.CTkLabel(
            source_meta_row,
            textvariable=self.source_title_var,
            text_color=self.palette["text"],
            font=button_font_lg,
            anchor="w",
        ).pack(side="left", anchor="w", fill="x", expand=True)
        self.source_ready_badge = ctk.CTkLabel(
            source_meta_row,
            text="Source",
            width=84,
            height=28,
            corner_radius=6,
            fg_color=self.palette["card_bg"],
            text_color=self.palette["muted_strong"],
            font=button_font,
        )
        self.source_ready_badge.pack(side="right")
        self.metadata_button = ctk.CTkButton(
            source_meta_row,
            text="Métadonnées",
            command=self.open_metadata_editor,
            width=110,
            height=28,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["muted_strong"],
            font=button_font,
        )
        self.metadata_button.pack(side="right", padx=(0, 8))
        ctk.CTkLabel(
            url_frame,
            text="Analyse le lien, valide la source et prépare la sélection de tomes.",
            text_color=self.palette["muted"],
            font=body_font,
        ).pack(anchor="w", pady=(2, 0))
        self.url_entry = ctk.CTkEntry(
            url_frame,
            textvariable=self.url,
            font=font_entry,
            height=40,
            corner_radius=8,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
        )
        self.url_entry.pack(fill="x", pady=(8, 0))
        self._bind_catalogue_url_paste(self.url_entry, self.url)
        self._attach_link_placeholder(
            self.url_entry,
            self.url,
            "https://sushiscan.fr/catalogue/slug/ ou https://mangas-origines.fr/oeuvre/slug/ ou https://hentai-origines.fr/manga/slug/ ou https://toonfr.com/webtoon/slug/ ou https://ortegascans.fr/serie/slug/ ou https://hentaizone.xyz/manga/slug/ ou https://www.scan-manga.com/1234/slug.html",
            None,
        )

        analyze_frame = ctk.CTkFrame(url_frame, fg_color="transparent")
        analyze_frame.pack(fill="x", pady=(10, 0))
        self.analyze_button = ctk.CTkButton(
            analyze_frame,
            text="Analyser le lien",
            command=self.load_volumes,
            width=button_w_lg + 10,
            height=button_h,
            corner_radius=8,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            text_color="#ffffff",
            font=button_font_lg,
        )
        self.analyze_button.pack(side="left")
        self.queue_button = ctk.CTkButton(
            analyze_frame,
            text="File d'attente",
            command=self.open_download_queue_dialog,
            width=130,
            height=button_h,
            corner_radius=8,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
            font=button_font,
        )
        self.queue_button.pack(side="left", padx=(8, 0))
        self.status_container = ctk.CTkFrame(
            analyze_frame,
            width=320,
            height=36,
            fg_color="transparent",
        )
        self.status_container.pack(side="left", padx=(12, 0))
        self.status_container.pack_propagate(False)
        self.analysis_spinner_label = ctk.CTkLabel(
            self.status_container,
            text="",
            width=18,
            fg_color="transparent",
            text_color=self.palette["accent_hover"],
            font=("Segoe UI Semibold", 12),
            anchor="center",
        )
        self.analysis_spinner_label.pack(side="left", padx=(0, 6))
        self.status_label = ctk.CTkLabel(
            self.status_container,
            text="",
            fg_color="transparent",
            corner_radius=6,
            text_color=self.palette["muted"],
            font=button_font,
            anchor="w",
        )
        self.status_label.pack(fill="both", expand=True)

        center_card, self.selection_section_tabs = create_titled_section(
            self.config_download_tab,
            "",
            0,
            expand=True,
        )
        self.error_tab = self.config_error_tab
        self.error_tab_title = "Erreurs (0)"

        toolbar_shell = ctk.CTkFrame(
            center_card,
            fg_color=self.palette["panel_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        toolbar_shell.pack(fill="x", pady=(0, 8))
        vol_header = ctk.CTkFrame(toolbar_shell, fg_color="transparent")
        vol_header.pack(fill="x", padx=12, pady=(10, 6))
        vol_header.grid_columnconfigure(0, weight=1)
        vol_header.grid_columnconfigure(1, weight=0)

        left_group = ctk.CTkFrame(vol_header, fg_color="transparent")
        left_group.grid(row=0, column=0, sticky="w")

        filter_group = ctk.CTkFrame(left_group, fg_color="transparent")
        filter_group.pack(side="left", padx=(0, 10))
        self.filter_text = tk.StringVar()

        filter_box = ctk.CTkFrame(
            filter_group,
            fg_color=self.palette["input_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["panel_shell"],
        )
        filter_box.pack(side="left")

        self.filter_entry = ctk.CTkEntry(
            filter_box,
            textvariable=self.filter_text,
            width=184,
            font=font_entry,
            height=button_h,
            corner_radius=6,
            border_width=0,
            fg_color=self.palette["input_bg"],
            text_color=self.palette["muted"],
        )
        self.filter_entry.pack(side="left", padx=(8, 0), pady=4)
        self.filter_entry.bind("<FocusIn>", self.on_filter_focus_in)
        self.filter_entry.bind("<FocusOut>", self.on_filter_focus_out)
        self.filter_entry.bind("<KeyRelease>", lambda e: self.schedule_filter_apply())
        self.clear_filter_button = ctk.CTkButton(
            filter_box,
            text="×",
            command=self.clear_filter,
            width=26,
            height=28,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["danger_soft"],
            text_color=self.palette["muted"],
            font=button_font,
        )
        self.clear_filter_button.pack(side="left", padx=(4, 6), pady=2)
        self.clear_filter_button.bind("<Enter>", self.on_clear_filter_enter)
        self.clear_filter_button.bind("<Leave>", self.on_clear_filter_leave)

        action_group = ctk.CTkFrame(left_group, fg_color="transparent")
        action_group.pack(side="left", padx=(0, 10))

        self.master_toggle_button = ctk.CTkButton(
            action_group,
            text="Tout cocher",
            command=self.toggle_all_button_action,
            width=button_w_sm,
            height=button_h,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
            font=button_font,
            state="disabled",
        )
        self.master_toggle_button.pack(side="left", padx=(0, 6))

        self.invert_button = ctk.CTkButton(
            action_group,
            text="Inverser",
            command=self.invert_selection,
            width=button_w_sm,
            height=button_h,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
            font=button_font,
            state="disabled",
        )
        self.invert_button.pack(side="left")

        info_group = ctk.CTkFrame(left_group, fg_color="transparent")
        info_group.pack(side="left", padx=(0, 12))

        self.volume_count_badge = ctk.CTkLabel(
            info_group,
            text="0 élément",
            width=132,
            height=30,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            text_color=self.palette["text"],
            font=button_font,
        )
        self.volume_count_badge.pack(side="left")

        download_group = ctk.CTkFrame(vol_header, fg_color="transparent")
        download_group.grid(row=0, column=1, sticky="e", padx=(16, 0))
        download_buttons_row = ctk.CTkFrame(download_group, fg_color="transparent")
        download_buttons_row.pack(anchor="e")

        self.dl_button = ctk.CTkButton(
            download_buttons_row,
            text="Télécharger la sélection",
            command=self.download_selected,
            width=button_w_lg,
            height=button_h,
            corner_radius=6,
            fg_color=self.palette["success_soft"],
            hover_color="#d7e7dd",
            text_color=self.palette["success_text"],
            font=button_font_lg,
            border_width=1,
            border_color=self.palette["success_border"],
            state="disabled",
        )
        self.dl_button.pack(side="left", padx=(0, 6))

        self.cancel_button = ctk.CTkButton(
            download_buttons_row,
            text="Annuler",
            command=self.cancel_download,
            width=button_w_sm,
            height=button_h,
            corner_radius=6,
            fg_color=self.palette["danger_soft"],
            hover_color="#edd0d7",
            text_color=self.palette["danger_text"],
            font=button_font,
            border_width=1,
            border_color=self.palette["danger_border"],
            state="disabled",
        )
        self.cancel_button.pack(side="left")
        hint_row = ctk.CTkFrame(toolbar_shell, fg_color="transparent")
        hint_row.pack(fill="x", padx=12, pady=(0, 10))
        self.download_hint_label = ctk.CTkLabel(
            hint_row,
            text="",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=body_font_sm,
            anchor="e",
        )
        self.download_hint_label.pack(side="right", padx=(12, 0))
        self.selection_hint_label = ctk.CTkLabel(
            hint_row,
            text="La liste sera rendue progressivement sur les gros catalogues.",
            fg_color="transparent",
            text_color=self.palette["muted_strong"],
            font=body_font,
            anchor="w",
        )
        self.selection_hint_label.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.set_filter_placeholder()
        self.filter_entry.configure(state="disabled")
        self.clear_filter_button.configure(state="disabled")
        self._style_clear_filter_button(disabled=True)

        vol_frame_container = ctk.CTkFrame(
            center_card,
            fg_color=self.palette["canvas_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["panel_shell"],
        )
        vol_frame_container.pack(fill="both", expand=True)
        self.volume_list_container = vol_frame_container
        self.volume_list_container.bind("<Configure>", self._schedule_volume_layout_refresh, add="+")

        volume_canvas_shell = ctk.CTkFrame(vol_frame_container, fg_color="transparent", corner_radius=0)
        volume_canvas_shell.pack(fill="both", expand=True, padx=6, pady=6)

        self.volume_canvas = tk.Canvas(
            volume_canvas_shell,
            height=260,
            bg=self.palette["canvas_bg"],
            highlightthickness=0,
            bd=0,
            relief="flat",
            yscrollincrement=1,
        )
        self.volume_canvas.pack(side="left", fill="both", expand=True)

        self.volume_scrollbar = ctk.CTkScrollbar(
            volume_canvas_shell,
            orientation="vertical",
            command=self._on_volume_scrollbar_command,
            fg_color=self.palette["card_bg"],
            button_color="#c5d3e2",
            button_hover_color="#aebfd1",
            width=14,
        )
        self.volume_scrollbar.pack(side="right", fill="y", padx=(6, 0))
        self.volume_canvas_scrollbar_set = getattr(self.volume_scrollbar, "set", None)
        self.volume_canvas.configure(yscrollcommand=self._on_volume_canvas_yview)

        self.vol_frame = ctk.CTkFrame(
            self.volume_canvas,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
        )
        self.volume_canvas_window_id = self.volume_canvas.create_window((0, 0), window=self.vol_frame, anchor="nw")
        self.vol_frame._parent_canvas = self.volume_canvas
        self.volume_canvas.bind("<Configure>", self._on_volume_canvas_configure, add="+")
        self.volume_canvas.bind("<Configure>", self._schedule_volume_layout_refresh, add="+")
        self.vol_frame.bind("<Configure>", self._on_volume_frame_configure, add="+")
        self.root.bind_all("<MouseWheel>", self._on_volume_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_volume_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_volume_mousewheel, add="+")
        self._update_volume_canvas_window(0)
        self._update_volume_canvas_scrollregion(0)
        self.vol_empty_label = ctk.CTkLabel(
            vol_frame_container,
            text="Aucun Tome/Chapitre chargé.",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=body_font,
        )
        self.vol_empty_label.place(in_=vol_frame_container, relx=0.5, rely=0.5, anchor="center")
        self.vol_loading_label = ctk.CTkLabel(
            vol_frame_container,
            text="",
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            text_color=self.palette["muted_strong"],
            font=button_font,
            height=34,
        )

        progress_frame = ctk.CTkFrame(
            center_card,
            fg_color=self.palette["panel_bg"],
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
        )
        progress_frame.pack(fill="x", pady=(4, 0))
        progress_frame.grid_columnconfigure(0, weight=1)
        progress_frame.grid_rowconfigure(1, weight=0)
        progress_meta_row = ctk.CTkFrame(progress_frame, fg_color="transparent")
        progress_meta_row.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        progress_meta_row.grid_columnconfigure(0, weight=1)
        progress_meta_row.grid_columnconfigure(1, weight=0)

        progress_status_group = ctk.CTkFrame(progress_meta_row, fg_color="transparent")
        progress_status_group.grid(row=0, column=0, sticky="w")
        self.current_volume_status_label = ctk.CTkLabel(
            progress_status_group,
            text="Tome/Chapitre en cours: --",
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            text_color=self.palette["muted_strong"],
            font=button_font,
            anchor="w",
            height=28,
        )
        self.current_volume_status_label.pack(side="left", padx=(0, 8))
        self.progress_detail_label = ctk.CTkLabel(
            progress_status_group,
            text="Images: --/--",
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            text_color=self.palette["muted_strong"],
            font=button_font,
            anchor="w",
            width=108,
            height=28,
        )
        self.progress_detail_label.pack(side="left", padx=(0, 8))
        self.eta_label = ctk.CTkLabel(
            progress_status_group,
            text="ETA Tome: --:-- | ETA Global: --:--",
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            text_color=self.palette["muted_strong"],
            font=button_font,
            anchor="w",
            width=220,
            height=28,
        )
        self.eta_label.pack(side="left")

        progress_title = ctk.CTkLabel(
            progress_meta_row,
            text="Progression",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
            anchor="e",
        )
        progress_title.grid(row=0, column=1, sticky="e")

        progress_bar_row = ctk.CTkFrame(progress_frame, fg_color="transparent")
        progress_bar_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        progress_bar_row.grid_columnconfigure(0, weight=1)
        self.progress_bar = ctk.CTkProgressBar(
            progress_bar_row,
            height=14,
            corner_radius=7,
            fg_color=self.palette["progress_trough"],
            progress_color=self.palette["accent"],
        )
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_bar.set(0)
        self.progress_label = ctk.CTkLabel(
            progress_bar_row,
            text="0%",
            fg_color=self.palette["card_bg"],
            corner_radius=6,
            text_color=self.palette["text"],
            font=button_font,
            width=54,
            height=28,
            anchor="e",
        )
        self.progress_label.grid(row=0, column=1, sticky="e", padx=(10, 0))

        error_panel = ctk.CTkFrame(
            self.error_tab,
            fg_color=self.palette["card_bg"],
            corner_radius=0,
        )
        error_panel.pack(fill="both", expand=True)
        error_frame = ctk.CTkFrame(
            error_panel,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
        )
        error_frame.pack(fill="both", expand=True, padx=12, pady=10)
        error_toolbar = ctk.CTkFrame(error_frame, fg_color="transparent")
        error_toolbar.pack(fill="x", padx=14, pady=(14, 10))
        error_toolbar.grid_columnconfigure(0, weight=1)

        error_intro = ctk.CTkFrame(error_toolbar, fg_color="transparent")
        error_intro.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            error_intro,
            text="Historique des erreurs",
            fg_color="transparent",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w")
        self.error_summary_label = ctk.CTkLabel(
            error_intro,
            text="Aucune erreur capturée pour le moment. Les échecs de traitement apparaîtront ici.",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=body_font,
        )
        self.error_summary_label.pack(anchor="w", pady=(2, 0))

        error_actions = ctk.CTkFrame(error_toolbar, fg_color="transparent")
        error_actions.grid(row=0, column=1, sticky="e")
        self.error_count_badge = ctk.CTkLabel(
            error_actions,
            text="Aucune erreur",
            width=118,
            height=30,
            corner_radius=6,
            fg_color=self.palette["success_soft"],
            text_color=self.palette["success_text"],
            font=button_font,
        )
        self.error_count_badge.pack(side="left", padx=(0, 10))
        for label, command in (
            ("Exporter", self.export_volume_errors),
            ("Copier", self.copy_volume_errors),
            ("Effacer", self.clear_volume_errors),
        ):
            ctk.CTkButton(
                error_actions,
                text=label,
                command=command,
                width=button_w_md,
                height=compact_button_h,
                corner_radius=6,
                fg_color=self.palette["panel_bg"],
                hover_color=self.palette["card_alt"],
                border_width=1,
                border_color=self.palette["border"],
                text_color=self.palette["text"],
                font=button_font,
            ).pack(side="right", padx=(6, 0))

        self.error_tree_shell = ctk.CTkFrame(
            error_frame,
            fg_color="#ffffff",
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        self.error_tree_shell.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        error_tree_header = ctk.CTkFrame(
            self.error_tree_shell,
            fg_color=self.palette["card_alt"],
            corner_radius=6,
        )
        error_tree_header.pack(fill="x", padx=10, pady=(10, 6))
        ctk.CTkLabel(
            error_tree_header,
            text="Chronologie",
            width=120,
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(side="left", padx=(12, 8), pady=8)
        ctk.CTkLabel(
            error_tree_header,
            text="Tome / Chapitre",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(side="left", padx=(0, 8), pady=8)
        ctk.CTkLabel(
            error_tree_header,
            text="Cause et action recommandée",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(side="right", padx=(8, 12), pady=8)

        self.error_list_frame = ctk.CTkScrollableFrame(
            self.error_tree_shell,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
            scrollbar_fg_color=self.palette["card_bg"],
            scrollbar_button_color="#c5d3e2",
            scrollbar_button_hover_color="#aebfd1",
        )
        self.error_list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.error_empty_label = ctk.CTkLabel(
            self.error_tree_shell,
            text="Aucune erreur affichée pour le moment.",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI", 10),
        )

        status_frame = ctk.CTkFrame(
            main_frame,
            fg_color=self.palette["panel_bg"],
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
        )
        status_frame.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(
            status_frame,
            text="Runtime",
            width=84,
            height=28,
            corner_radius=6,
            fg_color=self.palette["card_bg"],
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(12, 10), pady=10)
        self.runtime_status_label = ctk.CTkLabel(
            status_frame,
            textvariable=self.runtime_status,
            anchor="w",
            fg_color="transparent",
            text_color=self.palette["muted"],
            font=("Segoe UI", 10),
        )
        self.runtime_status_label.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=10)

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
            "cookie_toonfr_label_var",
            "cookie_ortega_label_var",
            "cookie_hentaizone_label_var",
            "cookie_scanmanga_label_var",
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
        if hasattr(self, "url_entry") and entry_widget is self.url_entry:
            return self._paste_catalogue_url_from_clipboard()
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

    def _paste_catalogue_url_from_clipboard(self, _event=None, target_variable=None):
        """Colle uniquement une URL catalogue exploitable dans le champ Source."""
        entry_widget = getattr(self, "url_entry", None)
        if entry_widget is None:
            return "break"
        try:
            if str(entry_widget.cget("state")) == "disabled":
                return "break"
        except Exception:
            pass

        try:
            clip_text = self.root.clipboard_get()
        except Exception:
            self.toast("Presse-papiers sans texte exploitable.")
            self.log("Collage URL ignoré: le presse-papiers ne contient pas de texte.", level="warning")
            return "break"

        clip_text = repair_mojibake_text(str(clip_text or ""))
        extracted_url = extract_supported_catalogue_url(clip_text)
        if not extracted_url:
            compact = re.sub(r"\s+", "", clip_text.strip())
            if len(compact) <= 2048 and compact.startswith(("https://", "http://")):
                extracted_url = compact
            else:
                self.toast("Aucune URL catalogue valide détectée.")
                self.log("Collage URL ignoré: aucune URL catalogue supportée détectée.", level="warning")
                return "break"

        try:
            entry_widget.focus_set()
        except Exception:
            pass
        target = target_variable if target_variable is not None else self.url
        target.set(extracted_url)
        try:
            entry_widget.icursor("end")
        except Exception:
            pass
        if extracted_url != clip_text.strip():
            self.log(f"URL extraite du presse-papiers: {extracted_url}", level="info")
        return "break"

    def _bind_catalogue_url_paste(self, entry_widget, target_variable=None):
        """Intercepte les collages clavier pour éviter les contenus non URL."""
        if entry_widget is None:
            return
        for sequence in ("<Control-v>", "<Control-V>", "<<Paste>>"):
            entry_widget.bind(
                sequence,
                lambda event, variable=target_variable: self._paste_catalogue_url_from_clipboard(event, variable),
            )

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
        cookie_toonfr_link = get_manual_link("cookie_toonfr", "https://toonfr.com")
        cookie_hentaizone_link = get_manual_link("cookie_hentaizone", "https://hentaizone.xyz")
        cookie_scanmanga_link = get_manual_link("cookie_scanmanga", "https://www.scan-manga.com")
        cookie_crunchyscan_link = get_manual_link("cookie_crunchyscan", "https://crunchyscan.fr")
        cookie_scanhentai_link = get_manual_link("cookie_scanhentai", "https://scan-hentai.net")
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
            'Cookie cf_clearance hentai-origines.fr (cliquer pour ouvrir le site si besoin).',
            cookie_hentai_link,
        )
        self._attach_link_placeholder(
            self.cookie_toonfr_entry,
            self.cookie_toonfr,
            'Cookie toonfr.com: cf_clearance seul ou header Cookie complet.',
            cookie_toonfr_link,
        )
        cookie_ortega_link = get_manual_link("cookie_ortega", "https://ortegascans.fr")
        self._attach_link_placeholder(
            self.cookie_ortega_entry,
            self.cookie_ortega,
            'Cookie ortegascans.fr: cf_clearance seul ou header Cookie complet.',
            cookie_ortega_link,
        )
        self._attach_link_placeholder(
            self.cookie_hentaizone_entry,
            self.cookie_hentaizone,
            'Cookie hentaizone.xyz: cf_clearance seul ou header Cookie complet.',
            cookie_hentaizone_link,
        )
        self._attach_link_placeholder(
            self.cookie_scanmanga_entry,
            self.cookie_scanmanga,
            'Cookie scan-manga.com: cf_clearance seul ou header Cookie complet.',
            cookie_scanmanga_link,
        )
        self._attach_link_placeholder(
            self.cookie_crunchyscan_entry,
            self.cookie_crunchyscan,
            'Cookie crunchyscan.fr: cf_clearance seul ou header Cookie complet.',
            cookie_crunchyscan_link,
        )
        self._attach_link_placeholder(
            self.cookie_scanhentai_entry,
            self.cookie_scanhentai,
            'Cookie scan-hentai.net: cf_clearance seul ou header Cookie complet.',
            cookie_scanhentai_link,
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
            self.cookie_fr_entry.configure(show=show_char)
        if hasattr(self, "cookie_net_entry"):
            self.cookie_net_entry.configure(show=show_char)
        if hasattr(self, "cookie_origines_entry"):
            self.cookie_origines_entry.configure(show=show_char)
        if hasattr(self, "cookie_hentai_entry"):
            self.cookie_hentai_entry.configure(show=show_char)
        if hasattr(self, "cookie_toonfr_entry"):
            self.cookie_toonfr_entry.configure(show=show_char)
        if hasattr(self, "cookie_ortega_entry"):
            self.cookie_ortega_entry.configure(show=show_char)
        if hasattr(self, "cookie_hentaizone_entry"):
            self.cookie_hentaizone_entry.configure(show=show_char)
        if hasattr(self, "cookie_scanmanga_entry"):
            self.cookie_scanmanga_entry.configure(show=show_char)
        if hasattr(self, "cookie_crunchyscan_entry"):
            self.cookie_crunchyscan_entry.configure(show=show_char)
        if hasattr(self, "cookie_scanhentai_entry"):
            self.cookie_scanhentai_entry.configure(show=show_char)


    def get_domain_from_url(self, url):
        """Retourne le domaine cookie interne: fr/net/origines/hentai/toonfr/ortega/hentaizone."""
        return get_cookie_domain_from_url(url)

    def _get_cookie_var_for_domain(self, domain):
        """Retourne la StringVar cookie liée au domaine."""
        mapping = {
            "fr": getattr(self, "cookie_fr", None),
            "net": getattr(self, "cookie_net", None),
            "origines": getattr(self, "cookie_origines", None),
            "hentai": getattr(self, "cookie_hentai", None),
            "toonfr": getattr(self, "cookie_toonfr", None),
            "ortega": getattr(self, "cookie_ortega", None),
            "hentaizone": getattr(self, "cookie_hentaizone", None),
            "scanmanga": getattr(self, "cookie_scanmanga", None),
            "crunchyscan": getattr(self, "cookie_crunchyscan", None),
            "scanhentai": getattr(self, "cookie_scanhentai", None),
        }
        return mapping.get(domain)

    def _get_cookie_entry_for_domain(self, domain):
        """Retourne le widget Entry cookie lié au domaine."""
        mapping = {
            "fr": getattr(self, "cookie_fr_entry", None),
            "net": getattr(self, "cookie_net_entry", None),
            "origines": getattr(self, "cookie_origines_entry", None),
            "hentai": getattr(self, "cookie_hentai_entry", None),
            "toonfr": getattr(self, "cookie_toonfr_entry", None),
            "ortega": getattr(self, "cookie_ortega_entry", None),
            "hentaizone": getattr(self, "cookie_hentaizone_entry", None),
            "scanmanga": getattr(self, "cookie_scanmanga_entry", None),
            "crunchyscan": getattr(self, "cookie_crunchyscan_entry", None),
            "scanhentai": getattr(self, "cookie_scanhentai_entry", None),
        }
        return mapping.get(domain)

    def _get_cookie_badge_for_domain(self, domain):
        """Retourne le badge visuel cookie lié au domaine."""
        mapping = {
            "fr": getattr(self, "cookie_fr_status", None),
            "net": getattr(self, "cookie_net_status", None),
            "origines": getattr(self, "cookie_origines_status", None),
            "hentai": getattr(self, "cookie_hentai_status", None),
            "toonfr": getattr(self, "cookie_toonfr_status", None),
            "ortega": getattr(self, "cookie_ortega_status", None),
            "hentaizone": getattr(self, "cookie_hentaizone_status", None),
            "scanmanga": getattr(self, "cookie_scanmanga_status", None),
            "crunchyscan": getattr(self, "cookie_crunchyscan_status", None),
            "scanhentai": getattr(self, "cookie_scanhentai_status", None),
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
            self.cookie_headers[domain] = build_cf_clearance_cookie_header(current_cookie)
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
        direct_ua = self.run_on_ui(self.ua.get, wait=True, default="").strip()
        if direct_ua:
            return direct_ua
        stored_ua = (self.cookie_user_agents.get(domain) or "").strip()
        if stored_ua:
            return stored_ua
        return DIRECT_USER_AGENT_DEFAULT

    def get_request_user_agent_for_url(self, url):
        domain = self.get_domain_from_url(url)
        return self.get_request_user_agent_for_domain(domain)

    def get_images_with_cookie_recovery(self, link, volume_label=None, cancel_event=None):
        """Extrait les images en proposant un renouvellement de cookie si l'accès est refusé."""
        safe_link = (link or "").strip()
        safe_volume = normalize_tome_label(volume_label or "") or "Chapitre"
        domain = self.get_domain_from_url(safe_link)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Extraction annulée.")

            cookie = self.get_cookie(safe_link)
            ua = self.get_request_user_agent_for_url(safe_link)
            try:
                extraction_started_at = time.perf_counter()
                images = get_images(
                    safe_link,
                    cookie,
                    ua,
                    cancel_event=cancel_event,
                )
                log_perf(self.log, "extraction images", extraction_started_at, tome=safe_volume, images=len(images))
                return cookie, ua, images
            except DownloadCancelled:
                raise
            except Exception as exc:
                reason = str(exc)
                if domain in COOKIE_DOMAINS and should_offer_cookie_refresh(None, reason):
                    self.log(
                        f"Extraction images bloquée pour {safe_volume}: {reason}",
                        level="warning",
                    )
                    confirmed = self.prompt_cookie_refresh(
                        domain,
                        safe_volume,
                        reason,
                        cancel_event=cancel_event,
                    )
                    if confirmed:
                        self.sync_cookie_source_for_domain(domain)
                        self.persist_settings()
                        continue
                raise

    def open_metadata_editor(self, wait=False, prompt_before_download=False):
        """Permet de corriger les métadonnées ComicInfo avant téléchargement."""
        metadata = dict(getattr(self, "series_metadata", {}) or {})
        if not metadata and not getattr(self, "title", ""):
            self.toast("Analyse d'abord une source")
            self.log("Aucune métadonnée à éditer: lance une analyse avant.", level="info")
            return False if wait else None

        window = ctk.CTkToplevel(self.root)
        window.title("Vérifier ComicInfo.xml" if prompt_before_download else "Métadonnées ComicInfo")
        window.geometry("760x620")
        window.transient(self.root)
        window.grab_set()
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        result = {"saved": False}

        header = ctk.CTkLabel(
            window,
            text=(
                "Vérifie les métadonnées avant création des CBZ"
                if prompt_before_download
                else "Métadonnées utilisées dans ComicInfo.xml"
            ),
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 15),
        )
        header.grid(row=0, column=0, sticky="w", padx=18, pady=(16, 8))

        body = ctk.CTkScrollableFrame(window, fg_color=self.palette["panel_bg"], corner_radius=8)
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 12))
        body.grid_columnconfigure(1, weight=1)

        fields = [
            ("series", "Série"),
            ("year", "Année"),
            ("writer", "Auteur"),
            ("penciller", "Dessinateur"),
            ("genre", "Genres"),
            ("status", "Statut"),
            ("publisher", "Source / éditeur"),
            ("web", "URL source"),
            ("cover_url", "URL couverture"),
        ]
        entries = {}
        for row, (key, label) in enumerate(fields):
            ctk.CTkLabel(
                body,
                text=label,
                text_color=self.palette["muted_strong"],
                font=("Segoe UI Semibold", 10),
                anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=(12, 10), pady=6)
            value = metadata.get(key)
            if isinstance(value, (list, tuple, set)):
                value = metadata_join(value)
            entry = ctk.CTkEntry(
                body,
                height=34,
                corner_radius=6,
                border_color=self.palette["border"],
                fg_color=self.palette["input_bg"],
                text_color=self.palette["text"],
                font=("Segoe UI", 10),
            )
            entry.insert(0, str(value or ""))
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=6)
            entries[key] = entry

        summary_row = len(fields)
        ctk.CTkLabel(
            body,
            text="Résumé",
            text_color=self.palette["muted_strong"],
            font=("Segoe UI Semibold", 10),
            anchor="nw",
        ).grid(row=summary_row, column=0, sticky="nw", padx=(12, 10), pady=6)
        summary_box = ctk.CTkTextbox(
            body,
            height=150,
            corner_radius=6,
            border_width=1,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            font=("Segoe UI", 10),
        )
        summary_box.insert("1.0", str(metadata.get("summary") or ""))
        summary_box.grid(row=summary_row, column=1, sticky="ew", padx=(0, 12), pady=6)

        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 16))
        actions.grid_columnconfigure(0, weight=1)

        def save_metadata():
            updated = dict(getattr(self, "series_metadata", {}) or {})
            for key, entry in entries.items():
                value = normalize_metadata_text(entry.get())
                if value:
                    updated[key] = value
                else:
                    updated.pop(key, None)
            summary = normalize_metadata_text(summary_box.get("1.0", "end-1c"))
            if summary:
                updated["summary"] = summary
            else:
                updated.pop("summary", None)
            if not updated.get("series"):
                updated["series"] = self.title or self.source_title_var.get()
            if updated.get("genre"):
                updated["tags"] = split_metadata_values(", ".join(filter(None, [updated.get("status", ""), updated.get("genre", "")])))
            self.series_metadata = updated
            self.log("Métadonnées ComicInfo mises à jour pour les prochains téléchargements.", level="success")
            if not prompt_before_download:
                self.toast("Métadonnées sauvegardées")
            result["saved"] = True
            window.destroy()

        def cancel_metadata():
            result["saved"] = False
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", cancel_metadata)

        ctk.CTkButton(
            actions,
            text="Annuler",
            command=cancel_metadata,
            width=120,
            height=34,
            corner_radius=6,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
        ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Continuer le téléchargement" if prompt_before_download else "Sauvegarder",
            command=save_metadata,
            width=210 if prompt_before_download else 140,
            height=34,
            corner_radius=6,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            text_color="#ffffff",
        ).grid(row=0, column=2)
        if wait:
            window.wait_window()
            return bool(result["saved"])
        return None

    def open_download_queue_dialog(self):
        """Ouvre une file d'attente simple: une URL catalogue par ligne."""
        window = ctk.CTkToplevel(self.root)
        window.title("File d'attente")
        window.geometry("760x520")
        window.transient(self.root)
        window.grab_set()
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)

        header_frame = ctk.CTkFrame(window, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        header_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header_frame,
            text="File d'attente de téléchargements",
            text_color=self.palette["text"],
            font=("Segoe UI Semibold", 15),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            header_frame,
            text="Une URL catalogue par ligne. SushiDL analysera chaque source et téléchargera tous les éléments non premium.",
            text_color=self.palette["muted"],
            font=("Segoe UI", 10),
            anchor="w",
            wraplength=700,
        ).grid(row=1, column=0, sticky="ew", pady=(6, 0))

        urls_box = ctk.CTkTextbox(
            window,
            corner_radius=8,
            border_width=1,
            border_color=self.palette["border"],
            fg_color=self.palette["input_bg"],
            text_color=self.palette["text"],
            font=("Consolas", 11),
        )
        urls_box.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 10))
        current_url = self.url.get().strip()
        if current_url:
            urls_box.insert("1.0", current_url + "\n")

        output_var = tk.StringVar(value=getattr(self, "download_output_root", "") or os.path.abspath(ROOT_FOLDER))
        output_row = ctk.CTkFrame(window, fg_color="transparent")
        output_row.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 10))
        output_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(output_row, text="Destination", text_color=self.palette["muted_strong"]).grid(row=0, column=0, padx=(0, 8))
        output_entry = ctk.CTkEntry(output_row, textvariable=output_var, height=34)
        output_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        def choose_output():
            selected = filedialog.askdirectory(
                parent=window,
                title="Choisir le dossier de destination",
                initialdir=output_var.get() or os.path.abspath(ROOT_FOLDER),
                mustexist=False,
            )
            if selected:
                output_var.set(os.path.abspath(selected))

        ctk.CTkButton(output_row, text="Parcourir", command=choose_output, width=100, height=34).grid(row=0, column=2)

        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 16))
        actions.grid_columnconfigure(0, weight=1)

        def launch_queue():
            raw_urls = [line.strip() for line in urls_box.get("1.0", "end-1c").splitlines()]
            urls = [line for line in raw_urls if line and not line.startswith("#")]
            if not urls:
                self.toast("Aucune URL")
                return
            output_root = os.path.abspath(output_var.get().strip() or ROOT_FOLDER)
            self.download_output_root = output_root
            window.destroy()
            self.start_download_queue(urls, output_root)

        ctk.CTkButton(
            actions,
            text="Annuler",
            command=window.destroy,
            width=120,
            height=34,
            fg_color=self.palette["panel_bg"],
            hover_color=self.palette["card_alt"],
            border_width=1,
            border_color=self.palette["border"],
            text_color=self.palette["text"],
        ).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Lancer la file",
            command=launch_queue,
            width=150,
            height=34,
            fg_color=self.palette["accent"],
            hover_color=self.palette["accent_hover"],
            text_color="#ffffff",
        ).grid(row=0, column=2)

    def start_download_queue(self, urls, output_root):
        """Télécharge plusieurs catalogues à la suite en sélection totale."""
        clean_urls = [url.strip() for url in urls if is_valid_catalogue_url(url.strip())]
        if not clean_urls:
            self.log("File d'attente vide ou URL non supportées.", level="warning")
            return
        if getattr(self, "download_in_progress", False):
            self.log("Téléchargement déjà en cours: impossible de lancer une file maintenant.", level="warning")
            return
        self.cancel_event.clear()
        self.download_in_progress = True
        self._set_download_controls(True)
        self._set_progress_ui(0)
        self._set_current_volume_ui(None)
        os.makedirs(output_root, exist_ok=True)

        cbz_enabled = self.cbz_enabled.get()
        comicinfo_enabled = self.comicinfo_enabled.get()
        chapter_cover_enabled = self.chapter_cover_enabled.get()
        webp2jpg_enabled = self.webp2jpg_enabled.get()
        smart_resume_enabled = self.smart_resume_enabled.get()
        download_threads = clamp_download_threads(self.download_threads.get())

        def task():
            try:
                queue_total = len(clean_urls)
                for queue_index, source_url in enumerate(clean_urls, start=1):
                    if self.cancel_event.is_set():
                        break
                    domain = self.get_domain_from_url(source_url)
                    cookie = self.get_cookie(source_url)
                    ua_for_url = self.get_request_user_agent_for_url(source_url)
                    self.run_on_ui(self.url.set, source_url)
                    self.log(f"File {queue_index}/{queue_total}: analyse {source_url}", level="info")
                    try:
                        analysis = fetch_manga_analysis(source_url, cookie, ua_for_url, emit_logs=False)
                        title = analysis.title
                        pairs = analysis.pairs
                        metadata_by_url = dict(analysis.volume_metadata or {})
                        series_metadata = dict(analysis.series_metadata or {})
                    except Exception as exc:
                        self.log(f"File: analyse échouée pour {source_url}: {exc}", level="error")
                        continue
                    self.run_on_ui(lambda resolved_title=title: self._refresh_source_title_label(resolved_title))
                    self.log(f"File: {len(pairs)} élément(s) détecté(s) pour {title}", level="success")
                    selected_pairs = [
                        (vol, link)
                        for vol, link in pairs
                        if not bool((metadata_by_url.get((link or "").strip()) or {}).get("premium"))
                    ]
                    for item_index, (vol, link) in enumerate(selected_pairs, start=1):
                        if self.cancel_event.is_set():
                            break
                        self.run_on_ui(self._set_current_volume_ui, vol)
                        self.log(f"File {queue_index}/{queue_total} - {item_index}/{len(selected_pairs)}: {vol}", level="info")
                        try:
                            cookie_item = self.get_cookie(link)
                            ua_item = self.get_request_user_agent_for_url(link)
                            images = get_images(link, cookie_item, ua_item, cancel_event=self.cancel_event, emit_logs=False)
                        except Exception as exc:
                            self.log(f"File: échec images {vol}: {exc}", level="error")
                            self.add_volume_error(vol, "images", str(exc), None, recommend_action_for_failure(None, str(exc)))
                            continue

                        progress_state = {"last_done": -1, "last_ts": 0.0}

                        def progress(done, total_images, base=item_index - 1, count=max(1, len(selected_pairs))):
                            now = time.time()
                            should_update = (
                                done in (0, total_images)
                                or now - progress_state["last_ts"] >= PROGRESS_UI_MIN_INTERVAL
                                or abs(int(done or 0) - int(progress_state["last_done"] or 0)) >= PROGRESS_UI_MIN_DELTA
                            )
                            if not should_update:
                                return
                            progress_state["last_done"] = int(done or 0)
                            progress_state["last_ts"] = now
                            item_percent = (float(done or 0) / float(total_images or 1)) if total_images else 0.0
                            self.run_on_ui(
                                self._set_download_runtime_ui,
                                ((base + item_percent) / count) * 100.0,
                                done,
                                total_images,
                            )

                        def error_callback(payload, fallback_vol=vol):
                            if not isinstance(payload, dict):
                                return
                            self.add_volume_error(
                                payload.get("tome") or fallback_vol,
                                payload.get("stage") or "download",
                                payload.get("reason") or "Erreur inconnue",
                                payload.get("status_code"),
                                payload.get("action"),
                            )

                        queue_threads = download_threads
                        fragile = get_fragile_site_settings(self.get_domain_from_url(link))
                        if fragile:
                            queue_threads = min(queue_threads, clamp_download_threads(fragile.get("max_threads", queue_threads)))
                        result = download_volume(
                            vol,
                            images,
                            title,
                            self.get_cookie(link),
                            self.get_request_user_agent_for_url(link),
                            self.log,
                            self.cancel_event,
                            cbz_enabled=cbz_enabled,
                            update_progress=progress,
                            webp2jpg_enabled=webp2jpg_enabled,
                            comicinfo_enabled=comicinfo_enabled,
                            chapter_cover_enabled=chapter_cover_enabled,
                            referer_url=link,
                            smart_resume_enabled=smart_resume_enabled,
                            error_callback=error_callback,
                            output_root=output_root,
                            prompt_cookie_retry=False,
                            total_count=len(pairs),
                            series_metadata=series_metadata,
                            cover_url=(series_metadata or {}).get("cover_url", ""),
                            download_threads=queue_threads,
                            archive_label=get_archive_label_for_link(vol, link, metadata_by_url),
                        )
                        if result is False:
                            self.log(f"File: élément non finalisé: {vol}", level="warning")
                if self.cancel_event.is_set():
                    self.log("File d'attente annulée.", level="warning")
                else:
                    self.log("File d'attente terminée.", level="success")
            finally:
                self.download_in_progress = False
                self.cancel_event.clear()
                self.run_on_ui(self._set_download_controls, False)
                self.run_on_ui(self._set_progress_detail_ui, None, None)
                self.run_on_ui(self.root.title, f"{APP_NAME} v{APP_VERSION}")

        threading.Thread(target=task, daemon=True, name="download-queue").start()

    def get_cookie_header_for_domain(self, domain, fallback_cookie=None):
        """Retourne l'en-tête Cookie effectif (complet si disponible)."""
        if domain not in COOKIE_DOMAINS:
            return ""
        header = sanitize_cookie_header(self.cookie_headers.get(domain))
        if header:
            return header
        cookie_var = self._get_cookie_var_for_domain(domain)
        if cookie_var is None:
            return ""
        cookie_value = (fallback_cookie or self.run_on_ui(cookie_var.get, wait=True, default="")).strip()
        return build_cf_clearance_cookie_header(cookie_value)

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
                self.cookie_headers[domain] = build_cf_clearance_cookie_header(current_cookie)
                self.last_known_cookies[domain] = current_cookie
                self._mark_cookie_updated(domain, current_cookie)
            elif current_cookie:
                self.cookie_sources[domain] = "manual"
                self.cookie_user_agents[domain] = direct_ua
                self.cookie_headers[domain] = build_cf_clearance_cookie_header(current_cookie)
            elif not current_cookie:
                self.cookie_sources[domain] = ""
                self.cookie_user_agents[domain] = ""
                self.cookie_headers[domain] = ""
                self.last_known_cookies[domain] = ""
                self._mark_cookie_updated(domain, "")

        cbz_enabled = bool(self.run_on_ui(self.cbz_enabled.get, wait=True, default=True))
        comicinfo_enabled = bool(self.run_on_ui(self.comicinfo_enabled.get, wait=True, default=True))
        chapter_cover_enabled = bool(self.run_on_ui(self.chapter_cover_enabled.get, wait=True, default=True))
        webp2jpg_enabled = bool(self.run_on_ui(self.webp2jpg_enabled.get, wait=True, default=True))
        smart_resume_enabled = bool(self.run_on_ui(self.smart_resume_enabled.get, wait=True, default=True))
        verbose_logs_enabled = bool(self.run_on_ui(self.verbose_logs.get, wait=True, default=True))
        download_threads = clamp_download_threads(self.run_on_ui(self.download_threads.get, wait=True, default=DEFAULT_DOWNLOAD_THREADS))
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
            comicinfo_enabled=comicinfo_enabled,
            chapter_cover_enabled=chapter_cover_enabled,
            download_threads=download_threads,
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
            self.cookie_headers[domain] = build_cf_clearance_cookie_header(cookie)
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

    def load_volumes(self, allow_cookie_retry=True, use_analysis_cache=True):
        """Charge la liste des tomes/chapitres pour l'URL donnée."""
        if getattr(self, "analysis_in_progress", False):
            self.log("Analyse déjà en cours, patiente quelques secondes.", level="warning")
            return

        self._set_workflow_step("source", "Validation URL et analyse catalogue...")
        self._set_analysis_status_label("Analyse en cours: validation URL...", success=None)
        self.title = ""
        self._refresh_source_title_label("")
        self.volume_meta_by_url = {}
        self.series_metadata = {}
        self.catalog_state_summary = {}
        self.cover_url = ""
        url = self.url.get().strip()
        if not is_valid_catalogue_url(url):
            self.log(
                "URL invalide. Formats attendus: https://sushiscan.fr|net/catalogue/slug/ ou https://mangas-origines.fr/oeuvre/slug/ ou https://hentai-origines.fr/manga/slug/ ou https://toonfr.com/webtoon/slug/ ou https://ortegascans.fr/serie/slug/ ou https://hentaizone.xyz/manga/slug/ ou https://www.scan-manga.com/1234/slug.html.",
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
            overlay_labels = {
                "validate": "Validation de l'URL...",
                "fetch": "Récupération du catalogue...",
                "parse": "Parsing des tomes/chapitres...",
                "cover": "Récupération de la couverture...",
            }
            text = labels.get(step)
            if text:
                self.run_on_ui(lambda: self._set_analysis_status_label(text, success=None))
            overlay_text = overlay_labels.get(step)
            if overlay_text:
                self.analysis_loading_overlay_text = overlay_text

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
        self._start_analysis_loading_indicators("Chargement de la liste...")
        self._cancel_pending_volume_render()
        if hasattr(self, "analyze_button"):
            self.analyze_button.configure(state="disabled")
        self.update_master_toggle_button()
        self._hide_volume_empty_state()

        def finish_analysis():
            self.analysis_in_progress = False
            self._stop_analysis_loading_indicators()
            if hasattr(self, "analyze_button"):
                self.analyze_button.configure(state="normal")
            self.update_master_toggle_button()

        def retry_analysis_after_cookie_update(reason):
            if not allow_cookie_retry or domain not in COOKIE_DOMAINS:
                return False
            if not should_offer_cookie_refresh(None, reason):
                return False
            self.log(
                f"Analyse bloquée pour .{domain}: proposition de renouvellement du cookie.",
                level="warning",
            )
            confirmed = self.prompt_cookie_refresh(
                domain,
                f"Analyse .{domain}",
                reason,
                cancel_event=None,
            )
            if not confirmed:
                self.log("Relance analyse abandonnée: cookie non mis à jour.", level="warning")
                return False
            try:
                self.sync_cookie_source_for_domain(domain)
                self.persist_settings()
            except Exception as sync_exc:
                self.log(f"Impossible de sauvegarder le cookie avant relance analyse: {sync_exc}", level="warning")
            self.log("Cookie mis à jour. Relance automatique de l'analyse...", level="info")
            self.run_on_ui(lambda: self.load_volumes(allow_cookie_retry=False))
            return True

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
            self._refresh_source_title_label("")
            self.volume_meta_by_url = {}
            self.series_metadata = {}
            self.catalog_state_summary = {}
            self.toast("Impossible de charger la liste")
            finish_analysis()

        def apply_pairs_ui():
            self._start_volume_render(on_complete=finish_analysis)

        def fetch_progress_callback(step):
            set_analysis_step(step)

        def worker():
            analysis_started_at = time.perf_counter()
            try:
                set_analysis_step("fetch")
                cached_analysis = get_cached_analysis(url, ua_for_url) if use_analysis_cache else None
                if cached_analysis:
                    analysis = MangaAnalysis(
                        title=cached_analysis["title"],
                        pairs=cached_analysis["pairs"],
                        volume_metadata=dict(cached_analysis.get("volume_metadata") or {}),
                        series_metadata=dict(cached_analysis.get("series_metadata") or {}),
                        html_content=cached_analysis.get("html_content", ""),
                    )
                    self.log(
                        f"Analyse chargée depuis le cache disque ({int(cached_analysis.get('age_seconds') or 0)}s).",
                        level="info",
                    )
                else:
                    analysis = fetch_manga_analysis(
                        url,
                        cookie,
                        ua_for_url,
                        progress_callback=fetch_progress_callback,
                    )
                    store_cached_analysis(
                        url,
                        ua_for_url,
                        analysis.title,
                        analysis.pairs,
                        analysis.volume_metadata,
                        analysis.series_metadata,
                        analysis.html_content,
                    )
                title = analysis.title
                pairs = analysis.pairs
                html_content = analysis.html_content
                self.title = title
                self.pairs = pairs
                self.volume_meta_by_url = dict(analysis.volume_metadata or {})
                self.series_metadata = dict(analysis.series_metadata or {})
                catalog_summary = update_catalog_state(
                    url,
                    title,
                    pairs,
                    domain=domain,
                    volume_metadata=analysis.volume_metadata,
                )
                self.catalog_state_summary = dict(catalog_summary or {})
                catalog_message = format_catalog_state_summary(catalog_summary)
                if catalog_message:
                    log_level = "success" if int((catalog_summary or {}).get("new_count") or 0) else "info"
                    self.log(catalog_message, level=log_level)
                    new_items = list((catalog_summary or {}).get("new_items") or [])[:5]
                    for item in new_items:
                        self.log(f"Nouveau: {item.get('label')}", level="success")
                    remaining_new = int((catalog_summary or {}).get("new_count") or 0) - len(new_items)
                    if remaining_new > 0:
                        self.log(f"... {remaining_new} autre(s) nouveauté(s).", level="success")
                log_perf(self.log, "analyse catalogue", analysis_started_at, domaine=domain, elements=len(pairs))
                self.ua_runtime_validity = bool((ua_for_url or "").strip())
                self.run_on_ui(lambda resolved_title=title: self._refresh_source_title_label(resolved_title))

                if domain in COOKIE_DOMAINS:
                    if self.pairs:
                        self._mark_analysis_auth_state(
                            domain,
                            True,
                            f"{len(self.pairs)} tome(s)/chapitre(s) détecté(s)",
                        )
                        if domain in ("crunchyscan", "scanhentai"):
                            self.log(
                                f"Auth .{domain} validée pour le catalogue. Le lecteur /read/... peut exiger un cookie Cloudflare renouvelé depuis une page chapitre.",
                                level="success",
                            )
                        else:
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
                error_text = str(exc)
                self.run_on_ui(lambda err=error_text: handle_error(err), wait=True, default=None)
                retry_analysis_after_cookie_update(error_text)
                return

            try:
                cover_started_at = time.perf_counter()
                set_analysis_step("cover")
                get_cover_image(html_content)
                log_perf(self.log, "couverture", cover_started_at, domaine=domain)
            except Exception as cover_exc:
                self.log(f"Erreur chargement couverture: {cover_exc}", level="error")

            try:
                MangaApp.last_url_used = url
                self.persist_settings()
            except Exception as save_exc:
                self.log(f"Erreur sauvegarde paramètres: {save_exc}", level="error")

            try:
                self.run_on_ui(apply_pairs_ui)
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
        self.master_toggle_button.configure(text=text)
        self._refresh_volume_card_styles()
        self._update_selection_status()

    def _update_selection_status(self):
        if getattr(self, "volume_virtualized", False):
            total = len(getattr(self, "check_vars", []) or [])
            filtered_indices = list(getattr(self, "filtered_volume_indices", []) or [])
            selected_total = sum(1 for var in self.check_vars if bool(var.get()))
            visible_total = len(filtered_indices) if total > 0 else 0
            selected_visible = sum(1 for index in filtered_indices if bool(self.check_vars[index].get()))
        else:
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

        if hasattr(self, "volume_count_badge"):
            if total <= 0:
                badge_text = "0 élément"
            elif selected_total > 0:
                badge_text = f"{selected_total}/{total} éléments"
            else:
                badge_text = f"{total} éléments"
            self.volume_count_badge.configure(text=badge_text)
        if hasattr(self, "volume_layout_chip"):
            layout_name = self._get_volume_layout_mode_name()
            effective_mode = "Dense" if getattr(self, "use_compact_volume_mode", False) else "Confort"
            chip_text = effective_mode if layout_name == "Auto" else layout_name
            chip_color = self.palette["accent_soft"] if effective_mode == "Confort" else self.palette["accent"]
            chip_text_color = self.palette["accent_hover"] if effective_mode == "Confort" else "#ffffff"
            self.volume_layout_chip.configure(text=f"Vue {chip_text}", fg_color=chip_color, text_color=chip_text_color)
        if hasattr(self, "selection_hint_label") and not getattr(self, "analysis_in_progress", False):
            if total == 0:
                hint_text = "Charge une source pour construire la grille."
            elif visible_total != total and visible_total > 0:
                hint_text = f"Filtre actif : {visible_total} élément(s) visibles."
            elif getattr(self, "volume_virtualized", False):
                hint_text = "Rendu canvas virtualisé actif."
            else:
                hint_text = "Sélectionne les tomes ou chapitres à télécharger."
            self.selection_hint_label.configure(text=hint_text)

        if hasattr(self, "download_hint_label"):
            if total == 0:
                hint = ""
            elif selected_visible <= 0:
                hint = "Sélectionne au moins 1 tome."
            elif visible_total != total:
                hint = f"{selected_visible} tome(s) visible(s) prêt(s)."
            else:
                hint = f"{selected_visible} tome(s) prêt(s) au téléchargement."
            self.download_hint_label.configure(text=hint)

        can_download = (
            total > 0
            and selected_visible > 0
            and not getattr(self, "download_in_progress", False)
            and not getattr(self, "analysis_in_progress", False)
        )
        if hasattr(self, "dl_button"):
            self.dl_button.configure(state="normal" if can_download else "disabled")

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

    def schedule_filter_apply(self, delay_ms=120):
        """Débounce le filtre pour garder la saisie fluide sur gros catalogues."""
        after_id = getattr(self, "filter_apply_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self.filter_apply_after_id = self.root.after(max(1, int(delay_ms)), self.apply_filter)

    def apply_filter(self):
        """Filtre la liste des tomes selon le texte saisi"""
        self.filter_apply_after_id = None
        raw = ""
        if not self.filter_placeholder_active:
            raw = self.filter_text.get().strip().lower()
        if raw == getattr(self, "last_applied_filter_raw", None):
            return
        self.last_applied_filter_raw = raw

        if getattr(self, "volume_virtualized", False):
            self.filtered_volume_indices = self._compute_filtered_volume_indices(raw)
            self.volume_virtual_window = None
            self.volume_grouped_virtual_rows_cache_key = None
            self.volume_grouped_virtual_rows_cache = []
            self._refresh_virtualized_volume_view(force=True, reset_scroll=True)
            self._update_selection_status()
            self._refresh_volume_empty_state()
            return

        columns = max(1, int(getattr(self, "volume_grid_columns", self._get_volume_grid_columns(len(self.check_items) or 0)) or 1))
        if getattr(self, "volume_grouped", False):
            columns = min(columns, 3)
        is_compact = bool(getattr(self, "use_compact_volume_mode", False))
        kind = "compact" if is_compact else "card"
        self._configure_volume_grid_columns(columns, kind=kind)
        visible_indices = []
        labels_lower = getattr(self, "volume_label_cache_lower", None) or [str(vol or "").lower() for vol, _link in getattr(self, "pairs", []) or []]
        for index, (chk, label) in enumerate(self.check_items):
            label_lower = labels_lower[index] if index < len(labels_lower) else str(label or "").lower()

            # Filtre optimisé avec recherche de sous-chaîne
            if not raw or raw in label_lower or \
            (raw.endswith('*') and raw[:-1].isdigit() and label_lower.startswith(raw[:-1])):
                visible_indices.append(index)
            else:
                chk.grid_remove()
        if getattr(self, "volume_grouped", False):
            self._grid_grouped_volume_widgets(visible_indices, columns, kind)
        else:
            for visible_pos, index in enumerate(visible_indices):
                chk, _label = self.check_items[index]
                row, col = self._get_centered_volume_grid_position(visible_pos, len(visible_indices), columns)
                self._grid_virtual_volume_pool_item(chk, kind, row, col)
        self.filtered_volume_indices = visible_indices
        self._update_volume_canvas_window(0)
        self._update_volume_canvas_scrollregion()
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
        if self._is_ctk_widget(self.filter_entry):
            self.filter_entry.configure(text_color=self.palette["muted"])
        else:
            self.filter_entry.configure(fg=self.palette["muted"])

    def clear_filter_placeholder(self):
        """Retire le placeholder du champ filtre."""
        if not self.filter_placeholder_active:
            return
        self.filter_placeholder_active = False
        self.filter_text.set("")
        if self._is_ctk_widget(self.filter_entry):
            self.filter_entry.configure(text_color=self.palette["text"])
        else:
            self.filter_entry.configure(fg=self.palette["text"])

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
        self._style_clear_filter_button(hovered=True)

    def on_clear_filter_leave(self, _event=None):
        """Fin de survol du bouton de remise à zéro du filtre."""
        if str(self.clear_filter_button.cget("state")) == "disabled":
            return
        self._style_clear_filter_button(hovered=False)

    def _build_download_plan_text(self, selected, premium_skipped, output_root):
        domain_counts = {}
        for _vol, link in selected:
            domain_counts[self.get_domain_from_url(link) or "inconnu"] = domain_counts.get(self.get_domain_from_url(link) or "inconnu", 0) + 1
        domain = next(iter(domain_counts), self.get_domain_from_url(self.url.get()))
        fragile = get_fragile_site_settings(domain)
        requested_threads = clamp_download_threads(self.download_threads.get())
        effective_threads = requested_threads
        if fragile:
            effective_threads = min(effective_threads, clamp_download_threads(fragile.get("max_threads", effective_threads)))
        free_gb = "?"
        try:
            free_gb = f"{shutil.disk_usage(output_root).free / (1024 ** 3):.1f} Go"
        except Exception:
            pass
        existing_cbz = 0
        clean_title = sanitize_folder_name(self.title)
        for vol, _link in selected:
            clean_tome = sanitize_folder_name(normalize_tome_label(get_archive_label_for_link(vol, _link, getattr(self, "volume_meta_by_url", {}) or {})))
            cbz_path = os.path.join(output_root, clean_title, f"{clean_title} - {clean_tome}.cbz")
            if os.path.exists(cbz_path) and os.path.getsize(cbz_path) > 10_000:
                existing_cbz += 1
        return (
            "Plan de téléchargement\n\n"
            f"- Titre : {self.title or '--'}\n"
            f"- Sélection : {len(selected)} élément(s)\n"
            f"- Déjà présents : {existing_cbz} CBZ\n"
            f"- Premium ignorés : {len(premium_skipped)}\n"
            f"- Dossier : {output_root}\n"
            f"- Espace libre : {free_gb}\n"
            f"- Sortie : CBZ={'oui' if self.cbz_enabled.get() else 'non'}, ComicInfo={'oui' if self.comicinfo_enabled.get() else 'non'}, Couverture chapitres={'oui' if self.chapter_cover_enabled.get() else 'non'}\n"
            f"- Threads : {effective_threads} ({'site fragile' if fragile else 'standard'})\n\n"
            "Lancer maintenant ?"
        )

    def _confirm_download_plan(self, selected, premium_skipped, output_root):
        text = self._build_download_plan_text(selected, premium_skipped, output_root)
        return messagebox.askyesno("Préflight téléchargement", text, parent=self.root)

    def download_selected(self):
        """Lance le téléchargement des tomes sélectionnés."""
        self.cancel_event.clear()
        selected = []
        premium_skipped = []
        if getattr(self, "volume_virtualized", False):
            visible_indices = set(getattr(self, "filtered_volume_indices", []) or [])
            for index, ((vol, link), var) in enumerate(zip(self.pairs, self.check_vars)):
                if index in visible_indices and var.get():
                    if self.is_volume_premium(index=index, link=link):
                        premium_skipped.append(vol)
                    else:
                        selected.append((vol, link))
        else:
            for index, ((chk, _label), (vol, link), var) in enumerate(zip(self.check_items, self.pairs, self.check_vars)):
                if var.get() and self._is_volume_visible(chk):
                    if self.is_volume_premium(index=index, link=link):
                        premium_skipped.append(vol)
                    else:
                        selected.append((vol, link))

        if premium_skipped:
            skipped_count = len(premium_skipped)
            self.log(
                f"{skipped_count} élément(s) premium ignoré(s): téléchargement réservé aux comptes premium.",
                level="warning",
            )
            self.toast(f"{skipped_count} premium ignoré(s)")

        if not selected:
            if premium_skipped:
                self.log("Aucun élément téléchargeable sélectionné.", level="info")
            else:
                self.log("Aucun tome sélectionné.", level="info")
            return

        if self.comicinfo_enabled.get():
            confirmed_metadata = self.open_metadata_editor(wait=True, prompt_before_download=True)
            if not confirmed_metadata:
                self.log("Téléchargement annulé: validation ComicInfo.xml interrompue.", level="info")
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
        if not self._confirm_download_plan(selected, premium_skipped, output_root):
            self.log("Téléchargement annulé au préflight.", level="info")
            return

        self._set_workflow_step("download", "Préparation du téléchargement...")
        self._set_download_controls(True)
        self._set_progress_ui(0)
        self._set_current_volume_ui(None)
        self._set_eta_ui()
        self._set_progress_detail_ui(None, None)

        cbz_enabled = self.cbz_enabled.get()
        comicinfo_enabled = self.comicinfo_enabled.get()
        chapter_cover_enabled = self.chapter_cover_enabled.get()
        webp2jpg_enabled = self.webp2jpg_enabled.get()
        smart_resume_enabled = self.smart_resume_enabled.get()
        download_threads = clamp_download_threads(self.download_threads.get())
        active_domain = self.get_domain_from_url(selected[0][1]) if selected else self.get_domain_from_url(self.url.get())
        fragile_settings = get_fragile_site_settings(active_domain)
        delay_between_volumes = 0.0
        if fragile_settings:
            download_threads = min(download_threads, clamp_download_threads(fragile_settings.get("max_threads", download_threads)))
            try:
                delay_between_volumes = max(0.0, float(fragile_settings.get("delay_between_volumes", 0.0)))
            except (TypeError, ValueError):
                delay_between_volumes = 0.0
            self.log(f"Profil site fragile actif pour .{active_domain}: threads={download_threads}, délai={delay_between_volumes}s.", level="info")

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
                if self.cancel_event.is_set():
                    break
                if delay_between_volumes and completed_volumes > 0:
                    interruptible_sleep(self.cancel_event, delay_between_volumes)

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

                try:
                    cookie, ua, images = self.get_images_with_cookie_recovery(
                        link,
                        volume_label=vol,
                        cancel_event=self.cancel_event,
                    )
                except DownloadCancelled:
                    break
                except Exception as exc:
                    reason = str(exc)
                    self.run_on_ui(self._set_progress_detail_ui, None, None)
                    self.log(
                        f"Échec récupération images: {reason}",
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
                    continue
                self.log(
                    f"{len(images)} image(s) trouvée(s)",
                    level="info",
                    context={"domain": domain, "tome": vol, "action": "images_count"},
                )

                if images:
                    self.run_on_ui(self._set_progress_detail_ui, 0, len(images))
                    progress_state = {"last_done": 0, "last_ts": 0.0, "last_ui_done": -1, "last_ui_ts": 0.0}
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
                        now = time.time()
                        should_update_ui = (
                            done in (0, total_images)
                            or now - progress_state["last_ui_ts"] >= PROGRESS_UI_MIN_INTERVAL
                            or done - progress_state["last_ui_done"] >= PROGRESS_UI_MIN_DELTA
                        )
                        if should_update_ui:
                            progress_state["last_ui_done"] = done
                            progress_state["last_ui_ts"] = now
                            self.run_on_ui(self._set_download_runtime_ui, percent, done, total_images)

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
                        self.run_on_ui(self._set_download_runtime_ui, None, None, None, tome_eta, global_eta)

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
                        comicinfo_enabled=comicinfo_enabled,
                        chapter_cover_enabled=chapter_cover_enabled,
                        referer_url=link,
                        smart_resume_enabled=smart_resume_enabled,
                        error_callback=volume_error_callback,
                        output_root=output_root,
                        total_count=len(self.pairs),
                        series_metadata=getattr(self, "series_metadata", {}),
                        cover_url=getattr(self, "cover_url", ""),
                        download_threads=download_threads,
                        archive_label=get_archive_label_for_link(vol, link, getattr(self, "volume_meta_by_url", {}) or {}),
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
                    try:
                        cookie, ua, images = self.get_images_with_cookie_recovery(
                            link,
                            volume_label=vol,
                            cancel_event=self.cancel_event,
                        )
                    except DownloadCancelled:
                        break
                    except Exception as exc:
                        images = []
                        reason = str(exc)
                        self.log(
                            f"Retry échoué: récupération images impossible ({vol}) - {reason}",
                            level="error",
                        )
                        self.add_volume_error(
                            vol,
                            "retry",
                            reason,
                            None,
                            recommend_action_for_failure(None, reason),
                        )
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
                            comicinfo_enabled=comicinfo_enabled,
                            chapter_cover_enabled=chapter_cover_enabled,
                            referer_url=link,
                            smart_resume_enabled=smart_resume_enabled,
                            error_callback=retry_error_callback,
                            output_root=output_root,
                            total_count=len(self.pairs),
                            series_metadata=getattr(self, "series_metadata", {}),
                            cover_url=getattr(self, "cover_url", ""),
                            download_threads=download_threads,
                            archive_label=get_archive_label_for_link(vol, link, getattr(self, "volume_meta_by_url", {}) or {}),
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
                self.log(f"Résumé performance: {self.summarize_perf_records()}", level="info")
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
        self.cancel_button.configure(state="disabled")
        self._set_workflow_step("logs", "Annulation demandée. Attente de fin des threads...")

    def save_current_cookie(self):
        """Sauvegarde les paramètres actuels dans le cache"""
        try:
            self.persist_settings()
            self.log("Cookies, UA, sorties, threads et préférences logs sauvegardés !", level="success")
            self.update_cookie_status()
            self.update_runtime_status()
        except Exception as e:
            self.log(f"Erreur sauvegarde: {e}", level="error")


class SushiCliBackend:
    """Pont minimal entre le backend SushiDL et la future interface terminale."""

    def load_settings(self):
        from cli.state import CliState

        (
            cookies,
            ua,
            cbz_enabled,
            comicinfo_enabled,
            chapter_cover_enabled,
            last_url,
            webp2jpg_enabled,
            smart_resume_enabled,
            verbose_logs,
            download_threads,
            _cookie_sources,
            _cookie_user_agents,
            _cookie_headers,
            _cookie_updated_at,
        ) = load_cookie_cache()

        cookie_status = {
            domain: ("PRESENT" if (cookies.get(domain) or "").strip() else "VIDE")
            for domain in COOKIE_DOMAINS
        }
        return CliState(
            cookies=dict(cookies),
            user_agent=(ua or DEFAULT_USER_AGENT).strip(),
            cbz_enabled=bool(cbz_enabled),
            comicinfo_enabled=bool(comicinfo_enabled),
            chapter_cover_enabled=bool(chapter_cover_enabled),
            webp2jpg_enabled=bool(webp2jpg_enabled),
            smart_resume_enabled=bool(smart_resume_enabled),
            verbose_logs=bool(verbose_logs),
            download_threads=clamp_download_threads(download_threads),
            current_url=(last_url or "").strip(),
            cookie_status=cookie_status,
        )

    def save_settings(self, state):
        MangaApp.last_url_used = (state.current_url or "").strip()
        save_cookie_cache(
            cookies_dict=dict(state.cookies),
            ua=(state.user_agent or DEFAULT_USER_AGENT).strip(),
            cbz=bool(state.cbz_enabled),
            webp2jpg_enabled=bool(state.webp2jpg_enabled),
            smart_resume_enabled=bool(state.smart_resume_enabled),
            verbose_logs=bool(state.verbose_logs),
            comicinfo_enabled=bool(state.comicinfo_enabled),
            chapter_cover_enabled=bool(state.chapter_cover_enabled),
            download_threads=clamp_download_threads(getattr(state, "download_threads", DEFAULT_DOWNLOAD_THREADS)),
        )

    def test_cookie(self, domain, cookie, ua):
        safe_domain = (domain or "").strip().lower()
        safe_cookie = (cookie or "").strip()
        safe_ua = (ua or DEFAULT_USER_AGENT).strip()
        if not safe_cookie:
            return False

        if safe_domain in ("fr", "net", "toonfr", "ortega", "hentaizone", "scanmanga", "crunchyscan", "scanhentai"):
            return test_cookie_validity(
                safe_domain,
                safe_cookie,
                safe_ua,
                probe_url=STARTUP_COOKIE_LISTING_PROBE_URLS.get(safe_domain),
            )

        if safe_domain in ("origines", "hentai"):
            try:
                probe_url = STARTUP_COOKIE_LISTING_PROBE_URLS.get(safe_domain)
                response = make_request(probe_url, safe_cookie, safe_ua)
                return int(getattr(response, "status_code", 0) or 0) == 200
            except Exception:
                return False
        return None

    def analyze_url(self, url, cookies, ua):
        safe_url = (url or "").strip()
        domain = get_cookie_domain_from_url(safe_url)
        if domain not in COOKIE_DOMAINS:
            raise ValueError("URL non supportee.")
        cookie = (cookies.get(domain) or "").strip()
        safe_ua = (ua or DEFAULT_USER_AGENT).strip()
        analysis = fetch_manga_analysis(
            safe_url,
            cookie,
            safe_ua,
            emit_logs=False,
        )
        update_catalog_state(
            safe_url,
            analysis.title,
            analysis.pairs,
            domain=domain,
            volume_metadata=analysis.volume_metadata,
        )
        return (
            analysis.title,
            domain,
            analysis.pairs,
            dict(analysis.volume_metadata or {}),
            dict(analysis.series_metadata or {}),
        )

    def resolve_domain(self, url):
        return get_cookie_domain_from_url((url or "").strip())

    def get_images_for_download(self, url, cookie, ua, cancel_event=None):
        return get_images(
            (url or "").strip(),
            (cookie or "").strip(),
            (ua or DEFAULT_USER_AGENT).strip(),
            cancel_event=cancel_event,
            emit_logs=False,
        )

    def download_selected_volume(
        self,
        item,
        image_urls,
        title,
        cookie,
        ua,
        output_dir,
        logger,
        update_progress,
        error_callback,
        cancel_event,
        cbz_enabled,
        comicinfo_enabled,
        chapter_cover_enabled,
        webp2jpg_enabled,
        smart_resume_enabled,
        download_threads=None,
        total_count=None,
        series_metadata=None,
        volume_metadata=None,
    ):
        return download_volume(
            item.label,
            list(image_urls or []),
            title,
            (cookie or "").strip(),
            (ua or DEFAULT_USER_AGENT).strip(),
            logger,
            cancel_event,
            cbz_enabled=bool(cbz_enabled),
            update_progress=update_progress,
            webp2jpg_enabled=bool(webp2jpg_enabled),
            comicinfo_enabled=bool(comicinfo_enabled),
            chapter_cover_enabled=bool(chapter_cover_enabled),
            referer_url=(item.url or "").strip(),
            smart_resume_enabled=bool(smart_resume_enabled),
            error_callback=error_callback,
            output_root=output_dir,
            prompt_cookie_retry=False,
            total_count=total_count,
            series_metadata=series_metadata,
            cover_url=(series_metadata or {}).get("cover_url", "") if isinstance(series_metadata, dict) else "",
            download_threads=download_threads,
            archive_label=get_archive_label_for_link(item.label, item.url, volume_metadata or {}),
        )


def run_self_test():
    """Exécute des tests rapides sans réseau pour valider les fonctions critiques."""
    import tempfile

    checks = []

    def check(name, condition):
        checks.append((name, bool(condition), "" if condition else "condition fausse"))

    def check_raises(name, exc_type, func):
        try:
            func()
        except exc_type:
            checks.append((name, True, ""))
        except Exception as exc:
            checks.append((name, False, f"exception inattendue: {type(exc).__name__}: {exc}"))
        else:
            checks.append((name, False, "aucune exception"))

    check(
        "url emoji percent-encoded",
        is_valid_catalogue_url("https://hentai-origines.fr/manga/hunter-to-nakama-no-wild-na-seikatsu-%e2%99%a5/"),
    )
    check("url slug classique", is_valid_catalogue_url("https://sushiscan.net/catalogue/one-piece/"))
    check("url scan-manga serie", is_valid_catalogue_url("https://www.scan-manga.com/16363/Death-Penalty.html"))
    check("domain scan-manga", get_cookie_domain_from_url("https://www.scan-manga.com/16363/Death-Penalty.html") == "scanmanga")
    check("url crunchyscan serie", is_valid_catalogue_url("https://crunchyscan.fr/lecture-en-ligne/hajime-no-ippo"))
    check("domain crunchyscan", get_cookie_domain_from_url("https://crunchyscan.fr/lecture-en-ligne/hajime-no-ippo") == "crunchyscan")
    check("url scan-hentai serie", is_valid_catalogue_url("https://scan-hentai.net/lecture-en-ligne/even-a-hopeless-romantic-wants-to-be-loved"))
    check("domain scan-hentai", get_cookie_domain_from_url("https://scan-hentai.net/lecture-en-ligne/even-a-hopeless-romantic-wants-to-be-loved") == "scanhentai")
    check("url scan-manga chapitre refusee", not is_valid_catalogue_url("https://www.scan-manga.com/lecture-en-ligne/Death-Penalty-Chapitre-56-FR_545712.html"))
    check("domain key", get_site_domain_key("https://ortegascans.fr/serie/moby-dick") == "ortega")
    check("url percent invalide refuse", not is_valid_catalogue_url("https://hentai-origines.fr/manga/bad%zz/"))
    check("url slash encode refuse", not is_valid_catalogue_url("https://hentai-origines.fr/manga/bad%2fslug/"))
    check(
        "extraction collage url",
        extract_supported_catalogue_url("x https://hentai-origines.fr/manga/test-%e2%99%a5/ y")
        == "https://hentai-origines.fr/manga/test-%e2%99%a5/",
    )
    check(
        "cookie brut",
        build_cf_clearance_cookie_header("abc123") == "cf_clearance=abc123",
    )
    check(
        "cookie header nettoye",
        build_cf_clearance_cookie_header("cf_clearance=abc\r\nInjected: nope; a=b")
        == "cf_clearance=abcInjected: nope; a=b",
    )
    check(
        "redaction log cookie",
        redact_sensitive_text("Cookie: cf_clearance=abc123; a=b") == "Cookie: [REDACTED]",
    )
    playwright_cookies = parse_cookie_header_for_playwright("Cookie: cf_clearance=abc; session_id=def", "crunchyscan.fr")
    check(
        "cookie playwright header",
        bool(playwright_cookies and playwright_cookies[0].get("name") == "cf_clearance" and playwright_cookies[0].get("domain") == ".crunchyscan.fr"),
    )
    raw_playwright_cookies = parse_cookie_header_for_playwright("raw-clearance-value", "crunchyscan.fr")
    check(
        "cookie playwright brut",
        bool(raw_playwright_cookies and raw_playwright_cookies[0].get("name") == "cf_clearance"),
    )
    headers = build_request_headers(
        "https://sushiscan.net/catalogue/one-piece/",
        "abc123",
        "UA-test",
    )
    check("headers user-agent", headers.get("User-Agent") == "UA-test")
    check("headers cookie", headers.get("Cookie") == "cf_clearance=abc123")
    check("headers referer", headers.get("Referer") == "https://sushiscan.net/")
    check(
        "cover haute resolution candidate",
        build_high_res_cover_candidates("https://example.test/covers/title-300x450.jpg")[0]
        == "https://example.test/covers/title.jpg",
    )
    check(
        "cover scan-manga suffix preserve",
        build_high_res_cover_candidates("https://static.scan-manga.com/img/manga/Infinite_Evolution_Starting_from_Zero_1_7111.jpg")[0]
        == "https://static.scan-manga.com/img/manga/Infinite_Evolution_Starting_from_Zero_1_7111.jpg",
    )
    check_raises("suppression racine refusee", ValueError, lambda: remove_tree_safely(".", expected_parent="."))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        folder = tmp_root / "Title" / "Chapitre 1"
        folder.mkdir(parents=True)
        for idx in range(1, 4):
            Image.effect_noise((900, 1200), 80).convert("RGB").save(folder / f"{idx:03d}.jpg", quality=90)
        archive_ok = archive_cbz(str(folder), "Title", "Chapitre 1", remove_source=True)
        check("archive cbz atomique", archive_ok)
        check("archive source supprimee", not folder.exists())
        check("archive finale presente", (tmp_root / "Title" / "Title - Chapitre 1.cbz").exists())
        check("archive tmp absente", not (tmp_root / "Title" / "Title - Chapitre 1.cbz.tmp").exists())

    global ANALYSIS_CACHE_PATH, ANALYSIS_CACHE_MEMORY, CATALOG_STATE_PATH, CATALOG_STATE_MEMORY, WATCHLIST_PATH, WATCHLIST_MEMORY
    old_cache_path = ANALYSIS_CACHE_PATH
    old_cache_memory = ANALYSIS_CACHE_MEMORY
    old_catalog_state_path = CATALOG_STATE_PATH
    old_catalog_state_memory = CATALOG_STATE_MEMORY
    old_watchlist_path = WATCHLIST_PATH
    old_watchlist_memory = WATCHLIST_MEMORY
    with tempfile.TemporaryDirectory() as tmp:
        ANALYSIS_CACHE_PATH = Path(tmp) / "analysis_cache.json"
        CATALOG_STATE_PATH = Path(tmp) / "catalog_state.json"
        WATCHLIST_PATH = Path(tmp) / "watchlist.json"
        ANALYSIS_CACHE_MEMORY = None
        CATALOG_STATE_MEMORY = None
        WATCHLIST_MEMORY = None
        store_cached_analysis(
            "https://sushiscan.net/catalogue/test/",
            "UA",
            "Titre",
            [("Chapitre 1", "https://sushiscan.net/test/1/")],
            {"https://sushiscan.net/test/1/": {"premium": False}},
            {"series": "Titre"},
            "<html></html>",
        )
        cached = get_cached_analysis("https://sushiscan.net/catalogue/test/", "UA")
        check("cache analyse roundtrip", bool(cached and cached.get("title") == "Titre" and cached.get("pairs")))
        stale_key = _analysis_cache_key("https://sushiscan.net/catalogue/stale/", "UA")
        ANALYSIS_CACHE_MEMORY = {
            stale_key: {
                "schema_version": ANALYSIS_CACHE_SCHEMA_VERSION - 1,
                "url": "https://sushiscan.net/catalogue/stale/",
                "timestamp": time.time(),
                "title": "Ancien",
                "pairs": [["Chapitre 1", "https://sushiscan.net/stale/1/"]],
                "series_metadata": {"series": "Ancien"},
            }
        }
        check("cache analyse ancien schema ignore", get_cached_analysis("https://sushiscan.net/catalogue/stale/", "UA") is None)
        first_state = update_catalog_state(
            "https://sushiscan.net/catalogue/test/",
            "Titre",
            [("Chapitre 1", "https://sushiscan.net/test/1/")],
            domain="net",
        )
        check("etat catalogue premiere analyse", bool(first_state.get("first_seen") and first_state.get("current_count") == 1))
        second_state = update_catalog_state(
            "https://sushiscan.net/catalogue/test/",
            "Titre",
            [("Chapitre 1", "https://sushiscan.net/test/1/")],
            domain="net",
        )
        check("etat catalogue sans nouveaute", bool(not second_state.get("first_seen") and second_state.get("new_count") == 0))
        third_state = update_catalog_state(
            "https://sushiscan.net/catalogue/test/",
            "Titre",
            [
                ("Chapitre 1", "https://sushiscan.net/test/1/"),
                ("Chapitre 2", "https://sushiscan.net/test/2/"),
            ],
            domain="net",
        )
        check("etat catalogue nouveaute", bool(third_state.get("new_count") == 1 and third_state.get("new_items", [{}])[0].get("label") == "Chapitre 2"))
        check("watchlist ajout url", add_or_update_watchlist_url("https://sushiscan.net/catalogue/test/", "Titre", enabled=True))
        watch_entries = get_watchlist_entries_with_state()
        check("watchlist etat enrichi", bool(watch_entries and watch_entries[0].get("last_count") == 2 and watch_entries[0].get("last_new_count") == 1))
        check("watchlist suppression url", remove_watchlist_url("https://sushiscan.net/catalogue/test/") and not get_watchlist_entries_with_state())
    ANALYSIS_CACHE_PATH = old_cache_path
    ANALYSIS_CACHE_MEMORY = old_cache_memory
    CATALOG_STATE_PATH = old_catalog_state_path
    CATALOG_STATE_MEMORY = old_catalog_state_memory
    WATCHLIST_PATH = old_watchlist_path
    WATCHLIST_MEMORY = old_watchlist_memory

    class FakeResponse:
        status_code = 200
        url = "https://sushiscan.net/catalogue/test/"
        text = "<html></html>"

    old_make = globals()["make_request"]
    old_parse = globals()["parse_manga_data_from_html"]
    old_meta = globals()["extract_series_metadata_from_html"]
    try:
        globals()["make_request"] = lambda url, cookie, ua: FakeResponse()
        globals()["parse_manga_data_from_html"] = (
            lambda url, html, emit_logs=True: (
                "Titre",
                [("Chapitre 1", "https://sushiscan.net/test/1/")],
                {"https://sushiscan.net/test/1/": {"premium": False}},
            )
        )
        globals()["extract_series_metadata_from_html"] = lambda url, html, title="": {
            "series": title,
            "cover_url": "https://sushiscan.net/cover.jpg",
        }
        analysis = fetch_manga_analysis("https://sushiscan.net/catalogue/test/", "", "", emit_logs=False)
        check("analyse structuree titre", analysis.title == "Titre")
        check("analyse structuree metadata", analysis.series_metadata.get("cover_url") == "https://sushiscan.net/cover.jpg")
    finally:
        globals()["make_request"] = old_make
        globals()["parse_manga_data_from_html"] = old_parse
        globals()["extract_series_metadata_from_html"] = old_meta

    scanmanga_html = """
    <div class="volume_manga"><div class="titre_volume_manga"><h3>Volume 2</h3></div>
      <div class="chapitre_nom"><a href="https://www.scan-manga.com/lecture-en-ligne/Test-Chapitre-Extra-FR_102.html">Chapitre Extra</a> : Bonus 2</div>
      <div class="chapitre_nom"><a href="https://www.scan-manga.com/lecture-en-ligne/Test-Chapitre-1-1-FR_101.html">Chapitre 1-1</a></div>
    </div>
    <div class="volume_manga"><div class="titre_volume_manga"><h3>Volume 1</h3></div>
      <div class="chapitre_nom"><a href="https://www.scan-manga.com/lecture-en-ligne/Test-Chapitre-1-1-FR_100.html">Chapitre 1-1</a></div>
    </div>
    """
    scanmanga_pairs, scanmanga_meta = parse_scanmanga_chapters_from_html(
        "https://www.scan-manga.com/1/Test.html",
        BeautifulSoup(scanmanga_html, "html.parser"),
        scanmanga_html,
    )
    scanmanga_labels = [label for label, _link in scanmanga_pairs]
    check(
        "scan-manga labels tome chapitre",
        scanmanga_labels
        == [
            "Tome 2 - Chap Extra",
            "Tome 2 - Chap 1-1",
            "Tome 1 - Chap 1-1",
        ],
    )
    check("scan-manga labels uniques", len(scanmanga_labels) == len(set(scanmanga_labels)))
    first_extra_url = scanmanga_pairs[0][1]
    check(
        "scan-manga archive label complet",
        scanmanga_meta.get(first_extra_url, {}).get("archive_label") == "Tome 2 - Chap Extra : Bonus 2",
    )
    dummy_app = object.__new__(MangaApp)
    check("scan-manga groupe webtoon", dummy_app._volume_group_label_from_text("Webtoon 1 - Chap 12") == "Webtoon 1")
    check("scan-manga compact webtoon", dummy_app._compact_display_label("Webtoon 1 - Chap 12") == "W1 C12")
    crunchy_html = """
    <html><head><title>Lire Shadows House en scan VF / FR - Manga Gratuit | Crunchyscan</title></head>
    <body>
      <img class="manga_cover" src="/upload/manga/shadows-house/cover.jpg?v=1">
      <div aria-labelledby="status-section"><h3>🚦 Status</h3><p>En cours</p></div>
      <div aria-labelledby="sortie-section"><h3>📅 Sortie</h3><p>2018</p></div>
      <div aria-labelledby="author-section"><h3>✍ Auteur(s)</h3><a href="/catalog/author/somato">Somato</a></div>
      <div aria-labelledby="artist-section"><h3>🎨 Artiste(s)</h3><a href="/catalog/artist/somato">Somato</a></div>
      <div aria-labelledby="type-section"><h3>📜 Type</h3><p>Manga</p></div>
      <div aria-labelledby="genre-section"><h3>🎭 Genre(s)</h3><a href="/catalog/genre/surnaturel">Surnaturel</a><a href="/catalog/genre/horreur">Horreur</a></div>
      <a class="chapterName chapter-link" title="Lire Chapitre 228" href="/lecture-en-ligne/shadows-house/read/chapitre-228">Chapitre 228</a>
      <a class="chapterName chapter-link" title="Lire Chapitre 227" href="/lecture-en-ligne/shadows-house/read/chapitre-227">Chapitre 227</a>
    </body></html>
    """
    crunchy_pairs, crunchy_meta = parse_crunchy_family_chapters_from_html(
        "https://crunchyscan.fr/lecture-en-ligne/shadows-house",
        BeautifulSoup(crunchy_html, "html.parser"),
        crunchy_html,
    )
    check("crunchyscan parser chapitres", [label for label, _ in crunchy_pairs] == ["Chapitre 228", "Chapitre 227"])
    check("crunchyscan metadata domaine", bool(crunchy_pairs and crunchy_meta.get(crunchy_pairs[0][1], {}).get("domain") == "crunchyscan"))
    check(
        "crunchyscan titre nettoye",
        extract_manga_title_from_html("https://crunchyscan.fr/lecture-en-ligne/shadows-house", crunchy_html) == "Shadows House",
    )
    check(
        "crunchyscan couverture",
        extract_cover_url_from_html("https://crunchyscan.fr/lecture-en-ligne/shadows-house", crunchy_html).startswith("https://crunchyscan.fr/upload/manga/"),
    )
    crunchy_series_meta = extract_series_metadata_from_html(
        "https://crunchyscan.fr/lecture-en-ligne/shadows-house",
        crunchy_html,
        "Shadows House",
    )
    check("crunchyscan metadata annee", crunchy_series_meta.get("year") == "2018")
    check("crunchyscan metadata statut", crunchy_series_meta.get("status") == "En cours")
    check("crunchyscan metadata genres", crunchy_series_meta.get("genre") == "Manga, Surnaturel, Horreur")
    check("crunchyscan metadata auteur", crunchy_series_meta.get("writer") == "Somato")
    check("crunchyscan metadata artiste", crunchy_series_meta.get("penciller") == "Somato")
    crunchy_title, crunchy_sorted_pairs, _crunchy_sorted_meta = parse_manga_data_from_html(
        "https://crunchyscan.fr/lecture-en-ligne/shadows-house",
        crunchy_html,
        emit_logs=False,
    )
    check("crunchyscan tri naturel", [label for label, _ in crunchy_sorted_pairs] == ["Chapitre 227", "Chapitre 228"])
    hentai_html = """
    <div aria-labelledby="status-section"><h3>🚦 Status</h3><p>En cours</p></div>
    <div aria-labelledby="sortie-section"><h3>📅 Sortie</h3><p>2026</p></div>
    <div aria-labelledby="author-section"><h3>✍ Auteur(s)</h3><a href="/catalog/author/test-author">Test Author</a></div>
    <div aria-labelledby="artist-section"><h3>🎨 Artiste(s)</h3><a href="/catalog/artist/test-artist">Test Artist</a></div>
    <div aria-labelledby="type-section"><h3>📜 Type</h3><p>Josei</p></div>
    <div aria-labelledby="genre-section"><h3>🎭 Genre(s)</h3><a href="/catalog/genre/drame">Drame</a><a href="/catalog/genre/mature">Mature</a><a href="/catalog/genre/romance">Romance</a></div>
    <a class="chapterName chapter-link" title="Lire Chapitre 4" href="/lecture-en-ligne/lies-are-planned/read/chapitre-4">Lire Chapitre 4</a>
    """
    hentai_pairs, hentai_meta = parse_crunchy_family_chapters_from_html(
        "https://scan-hentai.net/lecture-en-ligne/lies-are-planned",
        BeautifulSoup(hentai_html, "html.parser"),
        hentai_html,
    )
    check("scan-hentai parser chapitres", bool(hentai_pairs and hentai_pairs[0][0] == "Chapitre 4"))
    check("scan-hentai metadata domaine", bool(hentai_pairs and hentai_meta.get(hentai_pairs[0][1], {}).get("domain") == "scanhentai"))
    hentai_series_meta = extract_series_metadata_from_html(
        "https://scan-hentai.net/lecture-en-ligne/lies-are-planned",
        hentai_html,
        "Lies Are Planned",
    )
    check("scan-hentai metadata annee", hentai_series_meta.get("year") == "2026")
    check("scan-hentai metadata statut", hentai_series_meta.get("status") == "En cours")
    check("scan-hentai metadata genres", hentai_series_meta.get("genre") == "Josei, Drame, Mature, Romance")
    check("scan-hentai metadata auteur", hentai_series_meta.get("writer") == "Test Author")
    check("scan-hentai metadata artiste", hentai_series_meta.get("penciller") == "Test Artist")
    scanmanga_novel_html = """
    <article class="aLN">
      <h2 class="ln_c_title">Tome 18 - Chapitre 10-2 - Douleur partagée</h2>
      <div class="ln_c_content">
        <p>Premier paragraphe de test pour un chapitre Novel Scan-Manga.</p>
        <p>Deuxieme paragraphe assez long pour verifier le rendu en page image et le cache interne.</p>
        <p>Troisieme paragraphe avec assez de texte pour depasser le seuil minimal de detection du mode texte.</p>
      </div>
    </article>
    """
    novel_chapter = extract_scanmanga_novel_chapter(scanmanga_novel_html)
    check("scan-manga novel detecte", bool(novel_chapter and len(novel_chapter[1]) == 3))
    novel_urls = render_scanmanga_novel_pages(
        novel_chapter[0],
        novel_chapter[1],
        "https://www.scan-manga.com/lecture-en-ligne/Test-Chapitre-10-2-FR_1.html",
    ) if novel_chapter else []
    first_novel_page = get_text_page_bytes(novel_urls[0]) if novel_urls else None
    check("scan-manga novel page jpg", bool(first_novel_page and first_novel_page[:2] == b"\xff\xd8"))
    scanmanga_center_html = """
    <article class="aLN">
      <h2 class="ln_c_title">Titre</h2>
      <div class="ln_c_content"><center><p>☆☆☆</p></center><p>Texte de test suffisamment long pour valider le mode Novel et conserver l'alignement HTML dans le rendu final.</p></div>
    </article>
    """
    centered_chapter = extract_scanmanga_novel_chapter(scanmanga_center_html)
    check(
        "scan-manga novel alignement centre",
        bool(centered_chapter and centered_chapter[1][0].get("align") == "center"),
    )
    punctuation_chapter = extract_scanmanga_novel_chapter(
        """
        <article class="aLN"><h2 class="ln_c_title">Titre ③</h2><div class="ln_c_content">
          <p>Phrase avant citation :</p><p>Texte assez long pour que le mode Novel soit valide et conserve la ponctuation finale.</p>
        </div></article>
        """
    )
    check(
        "scan-manga novel ponctuation conservee",
        bool(punctuation_chapter and punctuation_chapter[1][0].get("text", "").endswith(":")),
    )
    image_buffer = BytesIO()
    Image.new("RGB", (320, 160), (40, 120, 220)).save(image_buffer, "PNG")
    data_image = "data:image/png;base64," + base64.b64encode(image_buffer.getvalue()).decode("ascii")
    scanmanga_novel_image_html = f"""
    <article class="aLN">
      <h2 class="ln_c_title">Titre image</h2>
      <div class="ln_c_content"><p>Intro avant image suffisamment longue pour activer le rendu Novel.</p><center><img src="{data_image}" alt="image test"/></center></div>
    </article>
    """
    image_chapter = extract_scanmanga_novel_chapter(scanmanga_novel_image_html)
    check("scan-manga novel image detectee", bool(image_chapter and image_chapter[1][-1].get("kind") == "image"))
    check("scan-manga novel image centree", bool(image_chapter and image_chapter[1][-1].get("align") == "center"))
    image_urls = render_scanmanga_novel_pages(image_chapter[0], image_chapter[1], "local-image-test") if image_chapter else []
    image_page = get_text_page_bytes(image_urls[0]) if image_urls else None
    check("scan-manga novel image rendue", bool(image_page and image_page[:2] == b"\xff\xd8"))
    limited_urls = render_scanmanga_novel_pages(
        "Titre limite",
        [{"kind": "text", "text": "Long texte de preview. " * 300, "align": "left"}],
        "local-preview-limit",
        max_pages=1,
    )
    check("scan-manga novel preview limite", len(limited_urls) == 1 and bool(get_text_page_bytes(limited_urls[0])))

    failed = [(name, detail) for name, ok, detail in checks if not ok]
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {name}{suffix}")
    print(f"\nSelf-test: {len(checks) - len(failed)}/{len(checks)} OK")
    return 1 if failed else 0


def build_diagnostic_snapshot(url=""):
    """Construit un diagnostic sans exposer cookies ni secrets."""
    try:
        (
            cookies,
            ua,
            cbz_enabled,
            comicinfo_enabled,
            chapter_cover_enabled,
            last_url,
            webp2jpg_enabled,
            smart_resume_enabled,
            verbose_logs,
            download_threads,
            _cookie_sources,
            _cookie_user_agents,
            _cookie_headers,
            cookie_updated_at,
        ) = load_cookie_cache()
    except Exception:
        cookies = {domain: "" for domain in COOKIE_DOMAINS}
        ua = DEFAULT_USER_AGENT
        cbz_enabled = True
        comicinfo_enabled = True
        chapter_cover_enabled = True
        last_url = ""
        webp2jpg_enabled = True
        smart_resume_enabled = True
        verbose_logs = True
        download_threads = DEFAULT_DOWNLOAD_THREADS
        cookie_updated_at = {domain: "" for domain in COOKIE_DOMAINS}

    safe_url = (url or last_url or "").strip()
    parsed = urlparse(safe_url) if safe_url else None
    domain = get_cookie_domain_from_url(safe_url) if safe_url else ""
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "url": {
            "provided": bool(url),
            "supported": bool(is_valid_catalogue_url(safe_url)) if safe_url else False,
            "site": get_supported_site_from_url(safe_url) if safe_url else "",
            "domain": domain,
            "scheme": parsed.scheme if parsed else "",
            "host": normalize_hostname(parsed.hostname) if parsed else "",
            "path": parsed.path if parsed else "",
        },
        "auth": {
            cookie_domain: {
                "cookie_present": bool((cookies.get(cookie_domain) or "").strip()),
                "updated_at": (cookie_updated_at.get(cookie_domain) or "").strip(),
            }
            for cookie_domain in COOKIE_DOMAINS
        },
        "settings": {
            "ua_present": bool((ua or "").strip()),
            "cbz_enabled": bool(cbz_enabled),
            "comicinfo_enabled": bool(comicinfo_enabled),
            "chapter_cover_enabled": bool(chapter_cover_enabled),
            "webp2jpg_enabled": bool(webp2jpg_enabled),
            "smart_resume_enabled": bool(smart_resume_enabled),
            "verbose_logs": bool(verbose_logs),
            "download_threads": clamp_download_threads(download_threads),
            "analysis_cache_ttl_seconds": get_analysis_cache_ttl_seconds(),
        },
        "files": {
            "config_exists": CONFIG_PATH.exists(),
            "cookie_cache_exists": COOKIE_CACHE_PATH.exists(),
            "analysis_cache_exists": ANALYSIS_CACHE_PATH.exists(),
        },
    }


def run_diagnostic_cli(url=""):
    """Affiche le diagnostic JSON sans secrets."""
    print(json.dumps(build_diagnostic_snapshot(url), indent=2, ensure_ascii=False))
    return 0


def run_batch_cli(argv, backend=None):
    """Mode terminal non interactif pour automatisation simple."""
    parser = argparse.ArgumentParser(
        prog="SushiDL.py --cli",
        description="Analyse et telecharge sans interface interactive.",
    )
    parser.add_argument("--url", action="append", default=[], help="URL catalogue a analyser. Peut etre repete.")
    parser.add_argument("--url-file", default="", help="Fichier texte contenant une URL par ligne.")
    parser.add_argument("--range", dest="selection_range", default="all", help="Selection: all, 1-20, 50+, 1,4,7.")
    parser.add_argument("--output", default="", help="Dossier de destination.")
    parser.add_argument("--download", action="store_true", help="Lance le telechargement apres analyse.")
    parser.add_argument("--dry-run", action="store_true", help="Analyse seulement et affiche la selection.")
    parser.add_argument("--no-cbz", action="store_true", help="Desactive la creation CBZ.")
    parser.add_argument("--no-comicinfo", action="store_true", help="Desactive ComicInfo.xml.")
    parser.add_argument("--no-cover", action="store_true", help="Desactive la couverture en premiere page des chapitres.")
    parser.add_argument("--no-webp2jpg", action="store_true", help="Desactive la conversion WEBP en JPG.")
    parser.add_argument("--no-resume", action="store_true", help="Desactive la reprise intelligente.")
    parser.add_argument("--threads", type=int, default=None, help="Nombre de telechargements paralleles (1-8).")
    parser.add_argument("--self-test", action="store_true", help="Execute les tests internes sans reseau.")
    parser.add_argument("--diagnostic", action="store_true", help="Affiche un diagnostic JSON sans secrets.")
    args = parser.parse_args([arg for arg in argv if arg != "--cli"])

    if args.self_test:
        return run_self_test()
    if args.diagnostic:
        diagnostic_url = (args.url or [""])[-1] if args.url else ""
        return run_diagnostic_cli(diagnostic_url)

    urls = [url.strip() for url in args.url if (url or "").strip()]
    if args.url_file:
        try:
            with open(args.url_file, "r", encoding="utf-8") as handle:
                urls.extend(line.strip() for line in handle if line.strip() and not line.strip().startswith("#"))
        except Exception as exc:
            print(f"Erreur lecture fichier URL: {exc}")
            return 2
    if not urls:
        parser.print_help()
        return 2

    from cli.actions import apply_range_selection, load_state
    from cli.download import CliDownloadController
    from cli.state import CliItem

    backend = backend or SushiCliBackend()
    state = load_state(backend)
    state.cbz_enabled = not args.no_cbz
    state.comicinfo_enabled = not args.no_comicinfo
    state.chapter_cover_enabled = not args.no_cover
    state.webp2jpg_enabled = not args.no_webp2jpg
    state.smart_resume_enabled = not args.no_resume
    if args.threads is not None:
        state.download_threads = clamp_download_threads(args.threads)
    output_dir = os.path.abspath(args.output or ROOT_FOLDER)
    os.makedirs(output_dir, exist_ok=True)

    exit_code = 0
    for url_index, url in enumerate(urls, start=1):
        print(f"\n[{url_index}/{len(urls)}] Analyse: {url}")
        try:
            title, domain, pairs, metadata, series_metadata = backend.analyze_url(
                url,
                state.cookies,
                state.user_agent,
            )
        except Exception as exc:
            print(f"Echec analyse: {exc}")
            exit_code = 1
            continue

        state.current_url = url
        state.current_title = title
        state.current_domain = domain
        state.series_metadata = dict(series_metadata or {})
        state.volume_metadata = dict(metadata or {})
        state.detected_items = [
            CliItem(
                index=idx + 1,
                label=(label or f"Element {idx + 1}").strip(),
                url=(item_url or "").strip(),
                premium=bool((metadata.get((item_url or "").strip()) or {}).get("premium")),
            )
            for idx, (label, item_url) in enumerate(pairs)
        ]
        state.filtered_indices = list(range(len(state.detected_items)))
        if args.selection_range.strip().lower() in ("", "all", "*"):
            state.selected_urls = {item.url for item in state.detected_items if not item.premium}
        else:
            state.selected_urls.clear()
            apply_range_selection(state, args.selection_range)
            state.selected_urls = {
                item.url for item in state.detected_items if item.url in state.selected_urls and not item.premium
            }

        premium_count = sum(1 for item in state.detected_items if item.premium)
        print(f"Titre: {title}")
        print(f"Domaine: {domain}")
        print(f"Elements detectes: {len(state.detected_items)} | selectionnes: {len(state.selected_urls)} | premium ignores: {premium_count}")
        if args.dry_run or not args.download:
            preview = [item for item in state.detected_items if item.url in state.selected_urls][:20]
            for item in preview:
                print(f"  - {item.index}. {item.label}")
            if len(state.selected_urls) > len(preview):
                print(f"  ... {len(state.selected_urls) - len(preview)} autre(s)")
            continue

        controller = CliDownloadController(backend, state, output_dir)
        controller.start()
        last_line = ""
        while True:
            snapshot = controller.snapshot()
            line = (
                f"{snapshot.global_percent:5.1f}% | "
                f"{snapshot.completed_volumes}/{snapshot.total_volumes} | "
                f"{snapshot.current_volume} | "
                f"{snapshot.current_images_done}/{snapshot.current_images_total} images | "
                f"ETA {snapshot.eta_global}"
            )
            if line != last_line:
                print(line)
                last_line = line
            if not snapshot.active:
                break
            time.sleep(0.8)
        if controller._thread:
            controller._thread.join(timeout=1)
        final = controller.snapshot()
        print(final.status_message)
        if final.errors:
            exit_code = 1
            print(f"Erreurs: {len(final.errors)}")
            for err in final.errors[:20]:
                http = f" HTTP {err.status_code}" if err.status_code else ""
                print(f"  - {err.tome} [{err.stage}{http}] {err.reason}")
            if len(final.errors) > 20:
                print(f"  ... {len(final.errors) - 20} autre(s)")
    return exit_code


# Point d'entrée de l'application
if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(run_self_test())
    if "--diagnostic" in sys.argv[1:]:
        diagnostic_url = ""
        for idx, arg in enumerate(sys.argv[1:]):
            if arg == "--url" and idx + 2 <= len(sys.argv[1:]):
                diagnostic_url = sys.argv[1:][idx + 1]
            elif arg.startswith("--url="):
                diagnostic_url = arg.split("=", 1)[1]
        sys.exit(run_diagnostic_cli(diagnostic_url))
    if "--cli" in sys.argv[1:] and any(arg.startswith("--url") or arg in {"--download", "--dry-run", "--url-file", "--self-test", "--diagnostic", "--help", "-h"} for arg in sys.argv[1:]):
        sys.exit(run_batch_cli(sys.argv[1:], SushiCliBackend()))
    elif "--cli" in sys.argv[1:]:
        from cli.app import run_cli_app

        run_cli_app(SushiCliBackend())
    else:
        runtime_log(f"Lancement de {APP_NAME} v{APP_VERSION}", level="info")
        MangaApp()
