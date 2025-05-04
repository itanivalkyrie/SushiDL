# 🌟 SushiDL v7

> Manga Downloader pour **SushiScan.fr** et **SushiScan.net** avec bypass Cloudflare ✨

---

## 🇫🇷 Nouveautés de la version 7 | 🇬🇧 What's New in v7

* ✅ **Support multi-domaine : sushiscan.fr & sushiscan.net**
  🇫🇷 Deux champs pour les cookies, détection automatique selon le domaine.
  🇬🇧 Two cookie fields, auto-selected based on URL.

* ⚡ **Contournement Cloudflare via FlareSolverr**
  🇫🇷 Compatible avec les challenges Cloudflare, URL personnalisable.
  🇬🇧 FlareSolverr integration, configurable URL.

* 🔍 **Fallback automatique HTML si JSON manquant**
  🇫🇷 Sécurise le téléchargement même si le script principal échoue.
  🇬🇧 Secure download even if main JSON parser fails.

* 📊 **Retry intelligent des échecs**
  🇫🇷 Deuxième tentative automatique pour les volumes en erreur.
  🇬🇧 Retry failed chapters automatically.

* 🔒 **Sauvegarde des cookies, User-Agent, CBZ & FlareSolverr URL**
  🇫🇷 Enregistré dans `cookie_cache.json` entre chaque session.
  🇬🇧 Stored in `cookie_cache.json` for reuse.

---

## 🇫🇷 Français

### 📚 Présentation

SushiDL est un utilitaire Python permettant de télécharger des mangas depuis **SushiScan.fr** ou **SushiScan.net**, avec interface graphique.

### ✨ Fonctionnalités principales

* Analyse automatique des volumes
* Interface Tkinter interactive
* Multi-threading pour téléchargement rapide
* Conversion WebP → JPEG
* Création de fichiers `.cbz` (optionnelle)
* Détection intelligente des images JSON et HTML
* FlareSolverr pour contourner les protections Cloudflare
* Sauvegarde automatique des préférences

### ⚖️ Prérequis

* Python 3.7+
* Modules : `Pillow`, `curl-cffi`, `tk`

```bash
pip install -r requirements.txt
```

### 📝 Installation

```bash
git clone https://github.com/toniohc/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

### 📖 Utilisation

```bash
python SushiDL_V7.py
```

1. Renseigner l'URL d'un manga
2. Entrer les cookies `.fr` & `.net`, User-Agent et URL FlareSolverr
3. Cliquer sur **Analyser les volumes**
4. Choisir les tomes souhaités
5. Lancer le téléchargement
6. Les fichiers `.cbz` seront dans `DL SushiScan/`

---

## 🇬🇧 English

### 📚 Overview

SushiDL is a Python GUI script to download manga volumes from **SushiScan.fr** and **SushiScan.net**.

### ✨ Main Features

* Automatic volume parsing from given URL
* User-friendly Tkinter interface
* Fast multithreaded downloads
* WebP to JPG conversion
* Optional `.cbz` file creation
* Cookie selection based on domain (.fr / .net)
* Cloudflare bypass using FlareSolverr
* Auto-retry failed chapters
* Preferences stored in cache file

### ⚖️ Requirements

* Python 3.7+
* Packages: `Pillow`, `curl-cffi`, `tk`

```bash
pip install -r requirements.txt
```

### 📁 Installation

```bash
git clone https://github.com/toniohc/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

### 📖 How to Use

```bash
python SushiDL_V7.py
```

1. Paste a manga URL (e.g., sushiscan.fr or sushiscan.net)
2. Fill `.fr` and `.net` cookies, User-Agent and FlareSolverr URL
3. Click **Analyser les volumes**
4. Select desired volumes
5. Start download
6. `.cbz` files will be stored in `DL SushiScan/`

---

### 💼 Licence

MIT License. See [LICENSE](LICENSE).

---

🙏 Merci d'utiliser SushiDL ! | Thanks for using SushiDL! 🍣
