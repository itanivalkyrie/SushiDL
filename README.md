<p align="center">
  <img alt="Banniere SushiDL" src="assets/banner.png" />
</p>

# SushiDL

SushiDL est une application Python avec interface graphique Tkinter / CustomTkinter pour analyser et telecharger des chapitres ou tomes de mangas depuis plusieurs domaines compatibles, avec gestion manuelle de l'authentification Cloudflare, telechargement multi-thread, conversion d'images et creation d'archives CBZ.

## Resume

SushiDL cible un usage simple :
- renseigner les cookies et le `User-Agent`
- analyser une URL catalogue compatible
- selectionner les tomes ou chapitres
- telecharger les pages dans un dossier local
- generer des archives `.cbz` si souhaite

Version actuelle : `11.9.0`

## Ce qui change sur `main`

La branche `main` embarque maintenant la refonte `CustomTkinter` par defaut.

Concretement :
- nouvelle interface avec barre d'onglets unique :
  - `Telechargement`
  - `Journal`
  - `Erreurs`
  - `Authentification`
  - `Options`
- onglet `Telechargement` unifie :
  - source,
  - liste des tomes / chapitres,
  - barre d'actions,
  - progression runtime
- rendu dense optimise et virtualise pour les gros catalogues
- popup de preview rapide `3 a 5` pages depuis le listing
- indicateurs de chargement pendant l'analyse, le rendu de liste et la preview
- generation optionnelle de `ComicInfo.xml` dans les archives CBZ pour Komga
- `requirements.txt` inclut maintenant :
  - `customtkinter>=5.2.2`
  - `textual>=0.82.0`

## Mode terminal

`main` embarque maintenant aussi une interface terminal interactive basee sur `Textual`.

Objectif :
- utiliser SushiDL sans GUI
- piloter les cookies, l'analyse, la selection et le telechargement uniquement au clavier
- garder une interface dense mais lisible sur gros catalogues

Lancement :

```bash
python SushiDL.py --cli
```

Fonctionnalites CLI disponibles :
- menu principal terminal
- ecran `Options / Cookies`
- edition et test des cookies par domaine
- edition du `User-Agent`
- sauvegarde des options runtime
- ecran `URL / Chapitres / Telechargement`
- analyse d'URL reelle via le backend existant
- filtre texte dans la liste
- selection / deselection / inversion
- selection par plage (`1-20`, `50+`, `1,4,7`, etc.)
- telechargement terminal reel avec progression
- annulation du telechargement
- ecran d'erreurs dedie
- copie et export des erreurs
- aides contextuelles et modales de confirmation
- adaptation partielle aux terminaux plus petits avec mode compact et avertissement de taille

Navigation terminal :
- `Tab` / `Shift+Tab` : changer de zone
- `Fleches` ou `J/K` : naviguer
- `Entree` / clic souris : selectionner ou activer
- `Espace` : basculer la ligne courante dans la liste
- `F5` : lancer l'analyse
- `/` : focus filtre
- `A` : tout cocher
- `N` : tout decocher
- `I` : inverser
- `R` : selection par plage
- `T` : telecharger
- `Esc` : retour
- `Q` : quitter
- `H` : aide

Dependance supplementaire :
- `textual>=0.82.0`

## Apercu visuel

Captures d'ecran :

<p align="center">
  <img alt="Capture 1" src="assets/screenshot1.png" width="48%" />
  <img alt="Capture 2" src="assets/screenshot2_v2.png" width="48%" />
</p>
<p align="center">
  <img alt="Capture 4" src="assets/screenshot4.png" width="48%" />
  <img alt="Capture terminal" src="assets/screenshot_terminal.png" width="48%" />
</p>

## Nouveautes recentes

### 11.9.0
- ComicInfo.xml :
  - enrichissement automatique avec les metadonnees detectees sur la fiche catalogue,
  - prise en charge de `Summary`, `Year`, `Month`, `Day`, `Writer`, `Penciller`, `Translator`, `Genre`, `Publisher`, `Tags` et `ScanInformation`,
  - extraction best effort depuis les metas HTML, les blocs WordPress/Madara, les liens de genre et les donnees embarquees Ortega.

### 11.8.0
- Sortie CBZ :
  - ajout d'une option `ComicInfo.xml` dans l'onglet `Options`,
  - generation de metadonnees compatibles Komga dans chaque archive CBZ,
  - champs inclus selon les donnees disponibles : serie, titre, numero, nombre de pages, URL source, langue et tags.
- Mode terminal :
  - l'option `ComicInfo.xml` est aussi disponible dans `Options / Cookies`.

### 11.7.2
- Telechargement :
  - correction d'un retry inutile sur les pages renvoyant un contenu non image,
  - ces pages sont maintenant marquees comme invalides/ignorees immediatement,
  - la finalisation du `CBZ` conserve les chapitres avec pages valides.
- Hentaizone :
  - support complet du catalogue, des chapitres et des images `scanscloud.xyz`,
  - validation cookie dediee depuis la GUI et le mode terminal.

### Resume des versions precedentes
- Interface `CustomTkinter` par defaut avec onglet `Telechargement` unifie.
- Mode terminal interactif via `python SushiDL.py --cli`.
- Support recent de Toonfr, OrtegaScans et Hentaizone.
- Preview rapide des chapitres/tomes avec popup dediee.
- Rendu dense virtualise pour gros catalogues et filtre rapide.
- Reprise intelligente, erreurs detaillees, relance cookie sur blocage et logs plus lisibles.

Pour le detail complet des versions : voir `CHANGELOG.md`.

## Sites supportes

- `https://sushiscan.fr`
- `https://sushiscan.net`
- `https://mangas-origines.fr`
- `https://hentai-origines.fr`
- `https://toonfr.com`
- `https://ortegascans.fr`
- `https://hentaizone.xyz`

Formats d'URL catalogue attendus :
- `https://sushiscan.fr/catalogue/<slug>/`
- `https://sushiscan.net/catalogue/<slug>/`
- `https://mangas-origines.fr/oeuvre/<slug>/`
- `https://hentai-origines.fr/manga/<slug>/`
- `https://toonfr.com/webtoon/<slug>/`
- `https://ortegascans.fr/serie/<slug>/`
- `https://hentaizone.xyz/manga/<slug>/`

## Fonctionnalites principales

- Authentification manuelle par cookies `cf_clearance` et `User-Agent`.
- Champs separes par domaine pour `.fr`, `.net`, `.origines`, `.hentai-origines`, `.toonfr`, `.ortegascans` et `.hentaizone`.
- Pour `.toonfr`, tu peux coller soit `cf_clearance`, soit un header `Cookie` complet si le site devient plus strict.
- Pour `.ortegascans`, tu peux coller soit `cf_clearance`, soit un header `Cookie` complet.
- Detection automatique du domaine a utiliser pour les pages, images et couvertures.
- Telechargement multi-thread des images avec retries et classification des erreurs.
- Annulation possible pendant l'execution.
- Reprise intelligente sur les pages deja presentes.
- Conversion optionnelle WebP vers JPG.
- Creation optionnelle d'archives CBZ.
- Generation optionnelle de `ComicInfo.xml` compatible Komga dans les archives CBZ.
- Journal unifie GUI + terminal avec filtres.
- Tableau d'erreurs par tome avec raison technique et action recommande.
- Interface `CustomTkinter` avec onglet `Telechargement` unifie et rendu dense optimise.
- Affichage optimise des tres grands catalogues avec filtre rapide, rendu mutualise sur canvas et scroll stabilise.
- Preview rapide integree par chapitre/tome via popup dediee et loupe dans le listing.
- Normalisation automatique des libelles `Episode` / `Ep` / `Chapter` vers `Chapitre`.
- Mode terminal interactif `--cli` avec gestion des cookies, analyse, selection, telechargement et erreurs.
- Sauvegarde persistante des parametres dans `cookie_cache.json`.

## Prerequis

- Python 3.10 ou plus
- Dependances Python de `requirements.txt`
- Tkinter disponible dans l'installation Python

Verification rapide :

Sous Windows :

```bash
python --version
```

Sous Linux (Debian/Ubuntu) :

```bash
sudo apt update
sudo apt install python3 python3-pip python3-tk
python3 --version
```

## Installation

```bash
git clone https://github.com/itanivalkyrie/SushiDL.git
cd SushiDL
pip install -r requirements.txt
```

Si `pip` ne pointe pas vers la bonne version de Python, utilise `python -m pip install -r requirements.txt` ou `python3 -m pip install -r requirements.txt`.

Dependances Python actuelles :
- `beautifulsoup4>=4.13.4`
- `customtkinter>=5.2.2`
- `curl_cffi>=0.10.0`
- `Pillow>=11.3.0`
- `requests>=2.32.3`
- `textual>=0.82.0`

## Lancement

Sous Windows :

```bash
python SushiDL.py
```

Mode terminal interactif :

```bash
python SushiDL.py --cli
```

Sous Linux :

```bash
python3 SushiDL.py
```

Mode terminal interactif :

```bash
python3 SushiDL.py --cli
```

## Authentification manuelle

SushiDL fonctionne en mode manuel pour l'authentification.
Le flux principal n'utilise pas FlareSolverr, Playwright, ni import automatique des cookies depuis le navigateur.

Tu dois fournir :
- un cookie `cf_clearance` pour chaque domaine que tu veux utiliser
- un `User-Agent` valide

Procedure conseillee :
1. Ouvre le site cible dans ton navigateur habituel.
2. Passe le challenge Cloudflare si necessaire.
3. Recupere la valeur du cookie `cf_clearance` sur le domaine concerne.
4. Recupere le `User-Agent` du navigateur.
5. Colle les valeurs dans l'onglet d'authentification de SushiDL.
6. Sauvegarde les parametres.

Lien pratique pour recuperer le `User-Agent` :
- `https://httpbin.org/user-agent`

## Configuration

Fichiers utilises par l'application :
- `config.json` : configuration globale et liens d'aide
- `cookie_cache.json` : preferences utilisateur, cookies, user-agent, options runtime

Exemple de structure `config.json` :

```json
{
  "auth_mode": "manual",
  "manual_links": {
    "cookie_fr": "https://sushiscan.fr",
    "cookie_net": "https://sushiscan.net",
    "cookie_origines": "https://mangas-origines.fr",
    "cookie_hentai": "https://hentai-origines.fr",
    "user_agent": "https://httpbin.org/user-agent",
    "cookie_help": "https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-recuperer-user-agent-et-cf_clearance"
  }
}
```

## Utilisation

Workflow standard :
1. Lance `SushiDL.py`.
2. Renseigne les cookies et le `User-Agent`.
3. Colle une URL catalogue supportee.
4. Clique sur `Analyser le lien`.
5. Controle la liste detectee.
6. Selectionne les tomes ou chapitres souhaites.
7. Clique sur `Telecharger la selection`.
8. Choisis le dossier de destination.

## Sortie des fichiers

Par defaut, les telechargements sont ranges dans `DL SushiScan/`, sauf si tu choisis un autre dossier de sortie pendant le telechargement.

Structure typique :

```text
<dossier_sortie>/
  <titre_manga>/
    <titre_manga> - <tome_ou_chapitre>.cbz
```

Si le mode CBZ est desactive, les images sont conservees dans des dossiers par tome ou chapitre.

## Gestion des erreurs

SushiDL distingue plusieurs familles d'erreurs :
- `404` / `410` : page absente cote serveur
- `403`, `429`, `5xx` : blocage, rate limit, erreur serveur ou probleme reseau
- page HTML a la place d'une image : challenge ou protection cote site

L'interface remonte aussi un tableau d'erreurs par tome avec :
- etape concernee
- code HTTP
- raison technique
- action conseillee

## Conseils de depannage

- `HTTP 403` : verifie le cookie `cf_clearance` du bon domaine et le `User-Agent`.
- Liste vide : controle le format de l'URL source et le domaine actif.
- Retry frequent sur images : renouvelle les donnees d'authentification ou attends avant de relancer.
- Couverture ou pages non chargees : controle que le cookie correspond bien au domaine de l'URL analysee.

## Outils complementaires

Le depot contient aussi :
- `tools/remove_last_images_cbz.py` : nettoyage automatique des dernieres pages parasites d'un CBZ
- `cut_sushiscan_fr/` : scripts annexes de coupe / reconstruction d'images

## Structure du projet

- `SushiDL.py` : application principale
- `README.md` : documentation generale
- `CHANGELOG.md` : historique des versions
- `requirements.txt` : dependances Python
- `assets/` : visuels et captures
- `tools/` : scripts utilitaires

## Changelog

Historique complet des versions : `CHANGELOG.md`

## Support

Si le projet t'est utile, tu peux soutenir le mainteneur sur Ko-fi :
- https://ko-fi.com/itanivalkyrie
