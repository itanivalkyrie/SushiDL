# 🍣 SushiDL – Téléchargeur de mangas avec interface graphique

**SushiDL** est une application Python moderne avec interface Tkinter permettant de télécharger automatiquement des chapitres ou volumes de mangas depuis **[sushiscan.fr](https://sushiscan.fr)** et **[sushiscan.net](https://sushiscan.net)**.  
Pensé pour être simple, rapide et efficace, il offre des fonctionnalités avancées comme la gestion de cookies Cloudflare, la compatibilité FlareSolverr, la conversion en `.cbz`, et une interface filtrable dynamique.

---

## ✨ Fonctionnalités
- 🧠 Analyse des chapitres améliorée : prise en charge des chapitres sans `ts_reader.run(...)` via parsing du DOM `#readerarea`
- 🛡️ Détection automatique de `sushiscan.fr` pour activer FlareSolverr
- 🔁 Analyse lancée en thread : interface non bloquante pendant le chargement
- ⏳ Affichage dynamique du message “Chargement de la couverture...” sous le bouton Analyser
- 🧼 Suppression automatique de l’image de couverture précédente avant affichage de la nouvelle
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
- 💖 Merci à l’auteur de [21hsmw/flaresolverr:nodriver](https://hub.docker.com/r/21hsmw/flaresolverr) pour cette image optimisée.
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

## 🔐 Récupérer `User-Agent` et `cf_clearance`

### 📎 Depuis Google Chrome

1. Visitez [https://sushiscan.fr](https://sushiscan.fr) ou [https://sushiscan.net](https://sushiscan.net)
2. Ouvrez les outils de développement `F12` → **Réseau**
3. Rechargez la page
4. Cliquez sur la première ligne (document)
5. Dans **En-têtes (Headers)** :
   - Copiez le champ `User-Agent`
   - Recherchez `cf_clearance` dans les cookies

### 🦊 Depuis Firefox

1. Rendez-vous sur [https://sushiscan.fr](https://sushiscan.fr) ou [https://sushiscan.net](https://sushiscan.net)
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

## 🧹 Script complémentaire : suppression automatique des dernières images `.cbz`

Le script `remove_last_images_cbz_loop.py` permet de nettoyer automatiquement les fichiers `.cbz` contenant des images publicitaires ou parasites ajoutées en fin de chapitre (notamment sur **sushiscan.fr**).

---

### ✨ Fonctionnalités :

- ✅ Suppression automatique d’un nombre défini d’images en fin de fichier
- 🖱️ Compatible glisser-déposer d’un **dossier** ou d’un **fichier unique**
- 🔁 Traitement en boucle : possibilité d’enchaîner plusieurs nettoyages sans redémarrer
- 🧠 Détection automatique : fichier `.cbz` unique ou dossier contenant plusieurs `.cbz`
- 📦 Création automatique d’une sauvegarde `.bak` de l’ancien fichier
- 🧾 Résumé final du nombre total d’images supprimées

---

### 📌 Exemple d’utilisation :

1. Lancez le script :
   ```bash
   python remove_last_images_cbz_loop.py

2. Entrez (ou glissez) un fichier .cbz ou un dossier

3. Indiquez le nombre d’images à supprimer (défaut : 7)

4. Laissez le script agir. Une sauvegarde .bak est créée.

Vous pouvez relancer l’opération autant de fois que nécessaire

---

## 🖼️ Aperçu
![image](https://github.com/user-attachments/assets/1294a0b6-2cf7-4970-acc3-94c25af1255c)
![python_CdNwdt1K8q](https://github.com/user-attachments/assets/d56b7729-7d94-42b9-947b-aa7331cc4797)
![WindowsTerminal_IIhxDGIC40](https://github.com/user-attachments/assets/bebd0903-482d-4164-977c-12bd4d87d3f3)
![image](https://github.com/user-attachments/assets/1267f0dc-531e-4ac2-864a-272c01a59e54)
