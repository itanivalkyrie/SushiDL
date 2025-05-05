# 📚 SushiDL – Téléchargeur de mangas avec interface graphique

**SushiDL** est une application Python moderne avec interface Tkinter permettant de télécharger automatiquement des chapitres ou volumes de mangas depuis **[sushiscan.fr](https://sushiscan.fr)** et **[sushiscan.net](https://sushiscan.net)**.  
Pensé pour être simple, rapide et efficace, il offre des fonctionnalités avancées comme la gestion de cookies Cloudflare, la compatibilité FlareSolverr, la conversion en `.cbz`, et une interface filtrable dynamique.

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

## 🚀 Installation

1. Assurez-vous d’avoir **Python 3.10+**
2. Installez les dépendances :

```bash
pip install -r requirements.txt
```

> 💡 Sous Linux, ajoutez `tkinter` si besoin :  
> `sudo apt install python3-tk`

---

## 🔧 Utilisation

1. Lancez `SushiDL_V7.py`
2. Entrez une URL de manga depuis sushiscan.fr ou sushiscan.net
3. Cliquez sur **Analyser les volumes**
4. Filtrez, sélectionnez ou inversez les chapitres
5. Cliquez sur **Télécharger** pour générer vos `.cbz`

📁 Les fichiers seront placés dans le dossier `downloads/`.

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

## 🧠 Détails techniques

- Conversion automatique d’images `.webp` en `.jpg`
- Génération propre de `.cbz` avec suppression du dossier temporaire
- Interface fluide avec log d’activité intégré
- Sauvegarde persistante dans `cookie_cache.json`
- Prise en charge de `sushiscan.fr` **et** `sushiscan.net`
- Filtrage dynamique en temps réel
- Barre de progression remise à 0 à chaque volume

---

## ❤️ Remerciements


Merci à l’auteur de [21hsmw/flaresolverr:nodriver](https://hub.docker.com/r/21hsmw/flaresolverr) pour cette image optimisée.

---

## 🖼️ Aperçu

![interface](https://github.com/itanivalkyrie/SushiDL/raw/main/screenshots/sushidl_ui.png)



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
