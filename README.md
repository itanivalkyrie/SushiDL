# 🌟 SushiDL v7

> Manga Downloader pour **SushiScan.fr** et **SushiScan.net** avec bypass Cloudflare ✨

---

## 🇫🇷 Nouveautés de la version 7 | 🇬🇧 What's New in v7

### ✅ Support multi-domaine : sushiscan.fr & sushiscan.net

* 🇫🇷 Deux champs pour les cookies, détection automatique selon le domaine.
* 🇬🇧 Two cookie fields, auto-selected based on URL.

### ⚡ Contournement Cloudflare via FlareSolverr

* 🇫🇷 Compatible avec les challenges Cloudflare, URL personnalisable.
* 🇬🇧 FlareSolverr integration, configurable URL.

### 🔍 Fallback automatique HTML si JSON manquant

* 🇫🇷 Sécurise le téléchargement même si le script principal échoue.
* 🇬🇧 Secure download even if main JSON parser fails.

### 📊 Retry intelligent des échecs

* 🇫🇷 Deuxième tentative automatique pour les volumes en erreur.
* 🇬🇧 Retry failed chapters automatically.

### 🔒 Sauvegarde des cookies, User-Agent, CBZ & FlareSolverr URL

* 🇫🇷 Enregistré dans `cookie_cache.json` entre chaque session.
* 🇬🇧 Stored in `cookie_cache.json` for reuse.

---
![image](https://github.com/user-attachments/assets/1052c2f2-3347-4048-b388-1b5f9078ffed)![image](https://github.com/user-attachments/assets/23511ad0-e047-48d3-a82a-5dfa94bbd89c)


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

#### ⚠️ FlareSolverr obligatoire dans certains cas

Certains chapitres ou volumes ne peuvent être téléchargés qu’en utilisant **FlareSolverr** pour contourner Cloudflare.

🔧 Guide d'installation :

* GitHub officiel : [https://github.com/FlareSolverr/FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)
* Image Docker recommandée : `21hsmw/flaresolverr:nodriver`

Assurez-vous que le service est démarré à l’adresse `http://localhost:8191` (modifiable dans SushiDL).

#### 🔐 Comment récupérer votre `cf_clearance` et `User-Agent`

1. Ouvrez votre navigateur (Chrome, Firefox, etc.) et allez sur `https://sushiscan.fr` ou `https://sushiscan.net`
2. Appuyez sur `F12` pour ouvrir les outils de développement
3. Allez dans l'onglet **Réseau (Network)** et rechargez la page (F5)
4. Cliquez sur une requête de type `document` (souvent la première de la liste)
5. Dans l'onglet **En-têtes (Headers)** :

   * Copiez la valeur du champ `User-Agent`
   * Cherchez les cookies, et copiez la valeur de `cf_clearance`

#### 🚀 Démarrer l'application et lancer le téléchargement

1. Clonez le dépôt :

   ```bash
   git clone https://github.com/itanivalkyrie/SushiDL.git
   cd SushiDL
   pip install -r requirements.txt
   ```
2. Lancez l'application avec la commande :

   ```bash
   python SushiDL_V7.py
   ```
3. Collez les valeurs de `cf_clearance` et `User-Agent` dans les champs prévus
4. Renseignez également l'adresse de FlareSolverr (ex. `http://localhost:8191`)
5. Cliquez sur le bouton **Sauver Cookies & UA**
6. Entrez l'URL du manga à télécharger
7. Cliquez sur **Analyser les volumes**
8. Sélectionnez les chapitres ou volumes souhaités
9. Lancez le téléchargement
10. Les fichiers `.cbz` seront disponibles dans le dossier `DL SushiScan/`

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

#### ⚠️ FlareSolverr is required for some downloads

Some chapters/volumes are only accessible via **FlareSolverr** to bypass Cloudflare protection.

🔧 Installation guide:

* Official GitHub: [https://github.com/FlareSolverr/FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)
* Recommended Docker image: `21hsmw/flaresolverr:nodriver`

Make sure the service is running at `http://localhost:8191` (customizable in SushiDL).

#### 🔐 How to get your `cf_clearance` and `User-Agent`

1. Open your browser (Chrome, Firefox, etc.) and go to `https://sushiscan.fr` or `https://sushiscan.net`
2. Press `F12` to open developer tools
3. Go to the **Network** tab and refresh the page (F5)
4. Click on the first `document`-type request
5. In the **Headers** section:

   * Copy the value of `User-Agent`
   * Look for cookies and copy the value of `cf_clearance`
6. Paste them into the SushiDL application in the appropriate fields
7. Click the **Sauver Cookies & UA** button to save your preferences

### 🚀 Getting Started

```bash
git clone https://github.com/toniohc/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

1. Launch the SushiDL application:

   ```bash
   python SushiDL_V7.py
   ```
2. Fill in the `.fr` and `.net` cookies, User-Agent and FlareSolverr URL
3. Click on **Sauver Cookies & UA** to save your preferences
4. Paste a manga URL (e.g., sushiscan.fr or sushiscan.net)
5. Click **Analyser les volumes**
6. Select desired volumes
7. Start download
8. `.cbz` files will be stored in `DL SushiScan/`

---

<p align="center">
  🔗 https://ko-fi.com/itanivalkyrie
</p>

<p align="center">
  <strong>❤️ Merci de votre soutien · Thank you for your support ❤️</strong>
</p>

<p align="center">
  <a href="https://ko-fi.com/itanivalkyrie" target="_blank">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Ko-Fi" />
  </a>
</p>

<p align="center">
  <strong>❤️ Si ce projet vous a été utile, vous pouvez le soutenir sur Ko-Fi | If this project was useful to you, consider supporting it on Ko-Fi ❤️</strong>
</p>

<p align="center">
  🙏 Merci d'utiliser SushiDL ! | Thanks for using SushiDL! 🍣
</p>

---

### 💼 Licence

MIT License. See [LICENSE](LICENSE).

---

