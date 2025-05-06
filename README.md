# 📚 SushiDL – Téléchargeur de mangas avec interface graphique

**SushiDL** est une application Python moderne avec interface Tkinter permettant de télécharger automatiquement des chapitres ou volumes de mangas depuis **[sushiscan.fr](https://sushiscan.fr)** et **[sushiscan.net](https://sushiscan.net)**.  
Pensé pour être simple, rapide et efficace, il offre des fonctionnalités avancées comme la gestion de cookies Cloudflare, la compatibilité FlareSolverr, la conversion en `.cbz`, et une interface filtrable dynamique.

---

## 🐍 Installer Python

### 🪟 Sur Windows

1. Rendez-vous sur le site officiel :  
   👉 [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/)
2. Téléchargez la dernière version **Python 3.10 ou supérieure**
3. **IMPORTANT** : cochez la case ✅ **"Add Python to PATH"** avant de cliquer sur "Install Now"
4. Une fois l'installation terminée, ouvrez l'invite de commandes (`cmd`) et vérifiez :

```bash
python --version
```

### 🐧 Sur Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-pip python3-tk
```

Puis vérifiez :

```bash
python3 --version
```

---

## 🚀 Installation

### 📥 Cloner le dépôt depuis GitHub

```bash
git clone https://github.com/itanivalkyrie/SushiDL.git
cd SushiDL
```


1. Assurez-vous d’avoir **Python 3.10+**
2. Installez les dépendances :

```bash
pip install -r requirements.txt
```

> 💡 Sous Linux, utilisez `pip3` si nécessaire

---

## ▶️ Exécution du script

### 🪟 Sous Windows

```bash
cd chemin\vers\le\dossier
python SushiDL_V8.py
```

Ou simplement : **double-cliquez** sur `SushiDL_V8.py`

### 🐧 Sous Linux

```bash
cd /chemin/vers/le/dossier
python3 SushiDL_V8.py
```

---

## ✨ Fonctionnalités

- 🖥️ Interface graphique claire (Tkinter)
- 🔍 Analyse automatique de volumes/chapitres depuis une URL
- 🎯 Filtrage instantané par mot-clé
- ✅ Boutons *Tout sélectionner*, *Inverser la sélection*
- 🧩 Téléchargement intelligent des images (Cloudflare-compatible)
- 🖼️ Conversion `.webp` → `.jpg`
- 📦 Génération automatique de fichiers `.cbz`
- 💾 Sauvegarde automatique de l'URL du dernier manga
- 🧠 Paramètres persistants (cookies, UA, FlareSolverr)
- 📊 Barre de progression par volume
- 🔐 Compatible FlareSolverr pour contourner Cloudflare

---

## 🔐 Récupérer `User-Agent` et `cf_clearance`

### 📎 Depuis Google Chrome

1. Visitez [https://sushiscan.fr](https://sushiscan.fr)
2. Ouvrez les outils de développement `F12` → **Réseau**
3. Rechargez la page
4. Cliquez sur la première ligne (document)
5. Dans **En-têtes (Headers)** :
   - Copiez le champ `User-Agent`
   - Recherchez `cf_clearance` dans les cookies

### 🦊 Depuis Firefox

1. Rendez-vous sur [https://sushiscan.net](https://sushiscan.net)
2. `Ctrl+Maj+I` → Onglet **Réseau**
3. Rechargez
4. Cliquez sur la première requête
5. Copiez :
   - Le `User-Agent`
   - Le cookie `cf_clearance`

🧠 Collez ces infos dans l'application → **Sauvegarder Paramètres**

---

## 🛡️ FlareSolverr – contournement Cloudflare (recommandé)

> ⚠️ Indispensable pour `sushiscan.fr` dans la plupart des cas.

### 🐳 Lancer FlareSolverr avec Docker

```bash
docker run -d --name flaresolverr -p 8191:8191 21hsmw/flaresolverr:nodriver
```

- Lancez-le en arrière-plan avec Docker
- Dans SushiDL, indiquez son URL (ex : `http://localhost:8191`)
- Cloudflare sera contourné automatiquement

---

## 🔧 Utilisation

1. Lancez `SushiDL_V8.py`
2. Entrez une URL de manga depuis sushiscan.fr ou sushiscan.net
3. Cliquez sur **Analyser les volumes**
4. Filtrez, sélectionnez ou inversez les chapitres
5. Cliquez sur **Télécharger** pour générer vos `.cbz`

📁 Les fichiers seront placés dans le dossier `DL SushiScan/`.

---

## 🧠 Détails techniques

- Conversion automatique d’images `.webp` en `.jpg`
- Génération propre de `.cbz` avec suppression du dossier temporaire
- Interface fluide avec log d’activité intégré
- Sauvegarde persistante dans `cookie_cache.json`
- Prise en charge de `sushiscan.fr` **et** `sushiscan.net`
- Filtrage dynamique en temps réel
- Barre de progression remise à 0 à chaque volume

---

## 🧹 Suppression automatique des images parasites

Il arrive que certains chapitres (notamment sur `sushiscan.fr`) incluent à la fin du fichier `.cbz` **7 images publicitaires ou hors contenu**.

Un script complémentaire est fourni dans ce dépôt : `remove_last_images_cbz.py`  
Il permet de **supprimer automatiquement les 7 dernières images de chaque fichier `.cbz`** dans un dossier.  
Il suffit de modifier ces deux lignes pour pouvoir choisir +/- le nombre d'images.

```bash
6 : def remove_last_images_from_cbz(cbz_path, num_to_remove=7):
45 : def process_folder(folder_path, num_to_remove=7):
```

---

## 🔧 Utilisation

1. Ouvrez un terminal
2. Lancez le script :

```bash
python remove_last_images_cbz.py
```

3. Entrez le chemin vers le dossier contenant vos `.cbz` ou faites glisser le répertoire contenant vos `.cbz`
4. Le script :
   - extrait temporairement le fichier
   - supprime les **7 dernières images**
   - sauvegarde l'ancien fichier sous `.bak`
   - recrée un `.cbz` propre

> 💡 Ce script est **optionnel**, mais très utile pour les chapitres affectés par des ajouts indésirables d'images.
---

## ❤️ Remerciements

Merci à l’auteur de [21hsmw/flaresolverr:nodriver](https://hub.docker.com/r/21hsmw/flaresolverr) pour cette image optimisée.

---

## 🖼️ Aperçu
<p align="center">
    <img src="https://github.com/user-attachments/assets/bd5cab4b-a143-4639-b065-4464dac24f6a">
    <img src="https://github.com/user-attachments/assets/bd5cab4b-a143-4639-b065-4464dac24f6a">
</p>
---

<p align="center">
  <strong>❤️ Si ce projet vous a été utile, vous pouvez le soutenir sur Ko-Fi | If this project was useful to you, consider supporting it on Ko-Fi ❤️</strong>
</p>

<p align="center">
  <a href="https://ko-fi.com/itanivalkyrie" target="_blank">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Ko-Fi" />
  </a>
</p>

<p align="center">
  🙏 Merci d'utiliser SushiDL ! | Thanks for using SushiDL! 🍣
</p>


---


