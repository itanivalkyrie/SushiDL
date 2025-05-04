## 🎉 SushiDL v4 - Mise à jour majeure | Major Update

### 🇫🇷 Nouveautés | 🇬🇧 What's New:
- ✅ **Création de fichier .CBZ optionnelle**  
  🇫🇷 Vous pouvez activer ou désactiver la génération automatique d’un fichier `.cbz` après le téléchargement.  
  🇬🇧 You can now enable or disable automatic `.cbz` archive creation after downloading.

- 💾 **Sauvegarde automatique des préférences**  
  🇫🇷 Le cookie `cf_clearance`, le `User-Agent` **et** l’état de l’option `.cbz` sont sauvegardés dans `cookie_cache.json`.  
  🇬🇧 The `cf_clearance` cookie, `User-Agent`, **and** CBZ option state are now saved in `cookie_cache.json`.

- 🔁 **Préférence CBZ persistante**  
  🇫🇷 L’option reste mémorisée entre chaque session.  
  🇬🇧 The CBZ option is preserved across sessions.

- 👁️‍🗨️ **Nouvelle case à cocher dans l’interface**  
  🇫🇷 Activez ou désactivez facilement la création du `.cbz`.  
  🇬🇧 A new checkbox lets you easily toggle CBZ creation.

- 📝 **Logs améliorés**  
  🇫🇷 Affichage clair de l’état CBZ (activée ✅ / désactivée ❌) + sauvegarde automatique visible.  
  🇬🇧 Log clearly shows CBZ status (enabled ✅ / disabled ❌) and confirms auto-saving.

### 🎯 Objectif | Goal:
🇫🇷 Offrir plus de contrôle à l’utilisateur tout en maintenant une expérience stable et intuitive.  
🇬🇧 Give users more control while ensuring a smooth and reliable experience.

🙏 Merci d’utiliser SushiDL !  
🙏 Thanks for using SushiDL! 🍣

---

# SushiDL - Sushiscan.net Manga Downloader

&#x20;

---

## 🇫🇷 Français

### Introduction

SushiDL est un script Python pour télécharger automatiquement les volumes de manga depuis SushiScan.net.

Basé sur [SushiScan-DLer](http://github.com/zyioump/SushiScan-DLer).

### Fonctionnalités principales

* **Analyse des volumes** disponibles d'une série manga à partir d'une URL donnée.
* **Sélection interactive** des chapitres/tomes à télécharger via une interface Tkinter.
* **Téléchargement multi-threads** des images de chaque chapitre.
* **Conversion** des images WebP en JPEG.
* **Création d'archives CBZ** pour chaque volume téléchargé.
* **Gestion automatique** du cookie `cf_clearance` et du User-Agent, avec mise en cache et vérification d'expiration.
* **Journalisation en temps réel** des opérations avec indicateurs de progression.
* **Annulation** du téléchargement en cours à tout moment.

### Prérequis

* Python 3.7+
* Modules listés dans `requirements.txt` :

  ```bash
  pip install -r requirements.txt
  ```

  * `Pillow`
  * `curl-cffi`
  * `tk` (soit via `pip install tk`, soit via le gestionnaire de paquets de votre OS, p.ex. `sudo apt install python3-tk`)

Note : sur certains systèmes, il peut être nécessaire d'installer `tkinter` via le gestionnaire de paquets OS.

### Installation

```bash
git clone https://github.com/toniohc/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

### Utilisation

```bash
python SushiDL_V4.py
```

1. Saisir l'URL du manga (ex : `https://sushiscan.net/catalogue/.../`).
2. Entrer votre cookie `cf_clearance` et User-Agent.
3. Cliquer sur « Analyser les volumes ».
4. Sélectionner les chapitres/tomes à télécharger.
5. Cliquer sur « Télécharger la sélection ».
6. Les archives CBZ seront créées dans le dossier **DL SushiScan**.

### Configuration

* Modifier les constantes (`ROOT_FOLDER`, `THREADS`, etc.) directement dans le script si nécessaire.
* Le cache du cookie est stocké dans `cookie_cache.json`.

### Licence

Ce projet est sous licence MIT. Voir le fichier [LICENSE](LICENSE) pour plus d'informations.

---

## 🇬🇧 English

### Introduction

SushiDL is a Python script for automatically downloading manga volumes from SushiScan.net.

Based on [SushiScan-DLer](http://github.com/zyioump/SushiScan-DLer).

### Key Features

* **Volume parsing** from a given manga URL on SushiScan.
* **Interactive selection** of chapters/volumes via a Tkinter GUI.
* **Multi-threaded downloads** of chapter images.
* **WebP to JPEG conversion** for downloaded images.
* **CBZ archive creation** for each downloaded volume.
* **Automatic management** of `cf_clearance` cookie and User-Agent, with caching and expiration checks.
* **Real-time logging** with progress indicators.
* **Cancel downloads** at any time.

### Requirements

* Python 3.7+
* Modules listed in `requirements.txt`:

  ```bash
  pip install -r requirements.txt
  ```

  * `Pillow`
  * `curl-cffi`
  * `tk` (via `pip install tk`, or use your OS package manager, e.g., `sudo apt install python3-tk`)

Note: on some systems you may need to install `tkinter` through your OS package manager.

### Installation

```bash
git clone https://github.com/toniohc/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

### Usage

```bash
python SushiDL_V4.py
```

1. Enter the manga URL (e.g., `https://sushiscan.net/catalogue/.../`).
2. Input your `cf_clearance` cookie and User-Agent.
3. Click **"Analyser les volumes"** (Analyze volumes).
4. Select the chapters/volumes to download.
5. Click **"Télécharger la sélection"** (Download selection).
6. CBZ archives will be created in the **DL SushiScan** folder.

### Configuration

* Adjust script constants (`ROOT_FOLDER`, `THREADS`, etc.) as needed.
* Cookie cache is stored in `cookie_cache.json`.

### License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

