# Changelog

Toutes les modifications notables de ce projet seront documentees dans ce fichier.

Le format de version suit la regle `X.Y.Z` :
- `X` = evolution majeure
- `Y` = amelioration / nouvelle fonctionnalite secondaire
- `Z` = correctif (bugfix)

## [11.2.8] - 2026-02-27

### Ameliorations
- Performance telechargement:
  - suppression des attentes artificielles dans les boucles workers,
  - annulation plus reactive pendant les retries d'extraction d'images.
- Logging optimise:
  - suppression du log par image telechargee,
  - reduction des blocages inter-threads sur le thread UI.

### Corrections
- Stabilite thread UI:
  - ajout d'un timeout de securite sur `run_on_ui(wait=True)` pour eviter les blocages indefinis.
- Securite cookies:
  - sanitation stricte des valeurs de cookies,
  - sanitation du header `Cookie` complet avant envoi HTTP.
- Validation cookies:
  - le probe cookie utilise des URLs fixes de demarrage (par domaine),
  - il ne depend plus de l'URL saisie dans le champ d'analyse.

## [11.2.7] - 2026-02-25

### Ameliorations
- Ajout du menu contextuel clic droit `Coller` sur les champs:
  - cookies (`.fr`, `.net`, `.origines`, `.hentai-origines`),
  - `User-Agent`,
  - `URL` source.

### Corrections
- Harmonisation des libelles et de la documentation:
  - suppression des mentions textuelles d'age,
  - conservation du seul marqueur `🔞` pour les contenus adultes.

## [11.2.6] - 2026-02-24

### Ameliorations
- UI Configuration:
  - refonte de la zone configuration en deux onglets principaux au meme niveau:
    - `Authentification`
    - `Options`
  - badge global `Authentification (x/5)` avec couleur d'etat selon le resultat des validations.
- Organisation des actions:
  - `Tester tout` et `Aide Cookie` restent dans l'onglet `Authentification`,
  - `Sauvegarder parametres` est repositionne dans l'onglet `Options`.
- Mise en page Options:
  - compactage des blocs pour reduire la hauteur consommee,
  - repartition harmonisee des encarts (`Journal et affichage` / `Sortie + Sauvegarder parametres`),
  - centrage du bouton `Sauvegarder parametres` dans l'espace disponible.
- Densite visuelle globale:
  - reduction des ecarts verticaux entre sections principales (`Authentification`, `Sources`, `Tomes / Chapitres`, `Journal`) pour augmenter la surface utile du journal.
- Fenetre:
  - ouverture par defaut en `1140x1040`,
  - hauteur minimale forcee a `1040 px` (max toujours `1070 px`).
- Couvertures:
  - extraction cover renforcee pour les sites Origines (support `data-src`, `data-lazy-src`, `srcset`, `data-srcset`, `background-image`, meta `og/twitter`),
  - support d'animation GIF reel dans la preview cover (boucle UI via Tkinter), avec arret propre lors du changement de cover/placeholder.

### Corrections
- Suppression des artefacts visuels autour des onglets configuration (trait de separation indesirable).
- Retrait du marqueur decoratif dans le titre de l'onglet `Authentification` pour un alignement propre.

## [11.2.5] - 2026-02-24

### Ameliorations
- Support multi-sites etendu:
  - ajout de `mangas-origines.fr` (format oeuvre: `/oeuvre/<slug>/`),
  - ajout de `hentai-origines.fr` (format manga: `/manga/<slug>/`, 🔞),
  - extraction des chapitres via endpoint AJAX Madara (`ajax/chapters/?t=`) quand la liste est chargee dynamiquement.
- Extraction images:
  - priorite forcee au mode `?style=list` pour les chapitres Origines,
  - fallback automatique depuis `?style=paged` et `/p/<n>/` vers la version `list`.
- Authentification manuelle:
  - ajout des cookies dedies `.origines` et `.hentai-origines` (🔞) dans l'UI,
  - persistance complete dans `cookie_cache.json` (sources, headers, timestamps),
  - probes de validation cookie au demarrage pour les nouveaux domaines:
    - `https://mangas-origines.fr/oeuvre/826-solo-leveling/`
    - `https://hentai-origines.fr/manga/stop-smoking/` (🔞).
- Couverture:
  - support et affichage des covers GIF (et sources `data-src` / `data-lazy-src`),
  - fallback meta etendu (`og:image`, `twitter:image`).

### Corrections
- Filtrage images Origines:
  - suppression des doublons,
  - suppression conditionnelle des publicites probables en debut/fin de chapitre selon la resolution detectee.
- Validation URL:
  - prise en charge des formats:
    - `https://sushiscan.fr|net/catalogue/<slug>/`
    - `https://mangas-origines.fr/oeuvre/<slug>/`
    - `https://hentai-origines.fr/manga/<slug>/` (🔞).

## [11.2.4] - 2026-02-24

### Ameliorations
- Telechargement:
  - selection d'un dossier de destination au clic sur `Telecharger la selection`,
  - memorisation du dernier dossier choisi pendant la session,
  - application du dossier cible aux sorties `.cbz` et aux images.
- Arborescence de sortie:
  - creation automatique de `Nom du manga / Tome|Chapitre` dans le dossier selectionne.
- Onglet `Erreurs`:
  - ajout d'un bouton `Copier` (copie tabulee des erreurs dans le presse-papiers),
  - harmonisation de la barre d'actions avec l'onglet `Journal`.

### Corrections
- UI:
  - suppression du titre redondant `Erreurs par tome` dans l'onglet `Erreurs`,
  - alignement propre de la zone tableau + scrollbar.
- Style:
  - en-tetes de sections principales (`Configuration`, `Sources`, `Tomes / Chapitres`) uniformises avec un rendu "onglet" tout en conservant l'empilement vertical.

## [11.2.3] - 2026-02-24

### Ameliorations
- Barre de progression reorganisee sur une seule ligne, avec ordre explicite:
  - `Tome/Chapitre en cours`
  - `Images`
  - `ETA Tome` et `ETA Global`
  - barre de progression + pourcentage.
- Ajout d'un indicateur dynamique du tome/chapitre en cours pendant le telechargement (et reset en fin de traitement).

## [11.2.2] - 2026-02-24

### Corrections
- Couverture:
  - integration du placeholder `assets/sushidl.png` avant analyse,
  - ratio fixe 2:3 pour toutes les couvertures (placeholder + couverture recuperee),
  - hauteur conservee et largeur adaptee automatiquement,
  - ajout d'un effet de profondeur (cadre en relief `sunken`, epaisseur augmentee).
- Boutons:
  - style `Primary` en relief (raised/sunken au clic) pour un rendu coherent,
  - renommage `Analyser la source` -> `Analyser le lien`.
- UI:
  - suppression de l'affichage des raccourcis dans l'en-tete tomes/chapitres.
- Encodage code:
  - correction automatique des commentaires/docstrings mojibake dans `SushiDL.py`.

### Assets
- Ajout de nouvelles images:
  - `assets/sushidl.png`
  - `assets/sushidl_full.png`

## [11.2.1] - 2026-02-24

### Corrections
- Badges auth cookies `.fr/.net`:
  - etat initial explicite `Validation en cours` (orange),
  - etats normalises en `Valide` / `A verifier`.
- Probe cookie listing plus discret dans le journal:
  - suppression de la mention `One Piece`,
  - messages courts `Test cookie .fr/.net : Reussite` ou `Echec`.
- Correctifs UI/UX tomes:
  - correction de l'etat vide (centrage horizontal/vertical),
  - suppression de l'artefact visuel (rectangle blanc) dans la zone `Tomes / Chapitres`,
  - suppression de l'affichage des raccourcis (`Ctrl+...`) dans l'en-tete de selection.
- Journal/Erreurs:
  - suppression du titre redondant `Journal` dans l'onglet Journal,
  - alignement visuel des onglets `Journal` / `Erreurs`,
  - fond blanc pour l'onglet actif.
- Theme visuel:
  - palette adoucie (moins de contraste entre fond global et cartes),
  - boutons secondaires en fond blanc (`Aide Cookie`, `Tout decocher`, `Inverser`, `Exporter`, `Copier`, `Effacer`).
- Normalisation des libelles FR en interface (accents/casse) sur les boutons et statuts.

## [11.2.0] - 2026-02-22

### Ameliorations
- Amelioration visuelle du badge `En attente`:
  - teinte orange plus douce,
  - texte plus fonce pour une meilleure lisibilite.
- Ajout d'un micro-test automatique du `User-Agent` au lancement:
  - requete legere sur le domaine actif (`.fr` ou `.net`),
  - badge `User-Agent` passe a `Validee` quand une reponse HTTP est obtenue.
- Lors de la modification du champ `User-Agent`, son statut repasse proprement en `En attente` (ou `A verifier` si vide) jusqu'au prochain test/analyse.
- Analyse detaillee:
  - etat `Analyse en cours` decoupe en etapes (`validation URL`, `recuperation catalogue`, `parsing`, `couverture`).
- Telechargement:
  - ajout d'une ETA `Tome` + `Globale` mise a jour en temps reel.
  - ajout d'un mode `Reprise intelligente` (reprise sur pages manquantes uniquement).
- Observabilite:
  - ajout d'un panneau `Erreurs par tome` avec `etape`, `code HTTP`, `raison technique`, `action recommandee`.
  - export CSV des erreurs par tome.
- UI/UX (phase 1):
  - cookies masques par defaut + option `Afficher cookies`,
  - raccourcis clavier (`Ctrl+Entree`, `Ctrl+D`, `Ctrl+F`, `Ctrl+L`),
  - resume de selection en temps reel (`Selection: x/y`),
  - detail de progression images (`Images: done/total`) en plus du pourcentage et ETA,
  - journal en `wrap=none` avec barre de scroll horizontale.
- UI/UX (phase 2):
  - compactage de l'interface (suppression des elements d'etapes trop verbeux),
  - etats vides guides pour la zone tomes (aucune source chargee / aucun resultat filtre / liste vide),
  - separation `Journal` / `Erreurs` via onglets avec compteur d'erreurs dynamique,
  - correction de detection de visibilite des tomes filtres (plus de faux `Aucun resultat avec ce filtre`).

### Corrections
- Normalisation des textes FR affiches:
  - correction des libelles UI les plus visibles (accents),
  - correction des logs GUI (texte mojibake) pour alignement avec les logs terminal.
- Nettoyage encodage global de `SushiDL.py` (mojibake) + ajout d'un `.editorconfig` UTF-8/LF.

## [11.1.9] - 2026-02-22

### Corrections
- Echec d'analyse auth sur un domaine (`.fr`/`.net`) ne force plus l'invalidation du badge `User-Agent`.
- Les messages d'erreur d'auth orientent d'abord vers le cookie `cf_clearance` du domaine en echec.
- Le statut runtime en echec devient prioritairement `verifier cookie .fr/.net` (et `+ User-Agent` seulement si UA vide).
- Le reset d'analyse au lancement de `Analyser` ne reinitialise plus l'etat UA a `A verifier`.
- Sur `HTTP 403`, le detail `fetch_manga_data` cible maintenant le cookie domaine plutot que le User-Agent.

## [11.1.8] - 2026-02-22

### Corrections
- Suppression de la logique d'anciennete des cookies dans le statut auth (plus d'alerte automatique basee sur le temps).
- Nouveau flux de badges auth:
  - etat initial `En attente` (orange),
  - apres analyse reussie: `Validee` (vert) pour le cookie du domaine analyse et le User-Agent,
  - apres analyse echouee: `A verifier` (rouge) avec message explicite pour verifier cookie `.fr/.net` + User-Agent.
- Les badges ne se reinitialisent plus automatiquement lors d'une simple modification de champ; le verdict depend du resultat d'analyse.
- Barre de statut runtime alignee sur ce flux: `en attente d'analyse` / `validee par analyse` / `echec: verifier cookie + User-Agent`.

## [11.1.6] - 2026-02-22

### Corrections
- Badges cookies `.fr` / `.net` au demarrage:
  - si un cookie est renseigne mais non encore teste par `Analyser`, l'etat n'apparait plus `Invalide` par defaut,
  - l'etat devient provisoire (puis confirme par le resultat d'analyse),
  - un echec d'analyse continue de forcer l'etat `Invalide` jusqu'a nouvelle saisie ou nouvelle analyse.

## [11.1.5] - 2026-02-22

### Corrections
- Retour utilisateur auth ameliore lors de `Analyser`:
  - si la liste tomes/chapitres est chargee, le cookie et le User-Agent du domaine actif sont marques valides,
  - en echec d'auth (`403`, challenge, acces refuse), badges mis en echec et message explicite dans les logs.
- Ajout d'un message de statut contextuel a cote du bouton `Analyser`:
  - `Analyse en cours...`,
  - `Auth .fr/.net validee (liste chargee)`,
  - ou message d'echec/non conclusion.
- Barre d'etat runtime enrichie avec etat auth:
  - `validee par analyse`,
  - `en echec (analyse)`,
  - `manuel (non teste)`.

## [11.1.4] - 2026-02-20

### Corrections
- Placeholders cookies `.fr` et `.net` modifies en texte guide non cliquable:
  - `Coller ici votre cookie cf_clearance. Cliquer sur "Aide Cookie" si besoin.`
- Placeholder `User-Agent` conserve en mode cliquable.
- README aligne avec ce comportement.

## [11.1.3] - 2026-02-20

### Corrections
- Renommage du bouton `Aide Cloudflare` en `Aide Cookie`.
- Placeholder `User-Agent` rendu plus explicite:
  - `Cliquer ici pour acceder a : Votre User-Agent ( Copier/Coller seulement la partie a droite entre les "" )`
- Ajout d'un placeholder d'exemple pour l'URL manga:
  - `https://www.sushiscan.fr|net/catalogue/xxx`
- Configuration manuelle: introduction de `manual_links.cookie_help` (avec fallback retro sur `cloudflare_help`).

## [11.1.2] - 2026-02-20

### Corrections
- Validation auth simplifiee dans la GUI:
  - suppression des etats `Sans challenge` et `CF requis` pour les cookies,
  - badges cookies limites a `Valide` / `Invalide` / `A controler` (si cookie > 24h).
- `Analyser`: en cas d'erreur `HTTP 403`, ajout d'un message explicite demandant de verifier le `User-Agent`.
- Evaluation du badge `User-Agent` amelioree:
  - `Valide` apres analyse reussie,
  - `Invalide` en cas de `HTTP 403` pendant l'analyse.
- Placeholders cookies `.fr` / `.net` rendus plus explicites:
  - `Cliquer ici pour acceder a : sushiscan.fr`
  - `Cliquer ici pour acceder a : sushiscan.net`

## [11.1.1] - 2026-02-20

### Corrections
- Purge finale du legacy FlareSolverr/Playwright dans `SushiDL.py`:
  - suppression des methodes de compatibilite restantes,
  - suppression des references cache `flaresolverr_url`,
  - suppression des hooks retro (`update_flaresolverr_url`, `auto_detect_auth`, UA FlareSolverr).
- Correction d'un crash au chargement du module (`NameError` sur `normalize_flaresolverr_url`) apres migration en mode manuel.
- Demarrage et persistance alignes sur un mode 100% manuel sans fallback legacy.

## [11.1.0] - 2026-02-20

### Ameliorations
- Ajout d'un bouton `Aide Cloudflare` dans la zone `Configuration` (a la place du flux Auto Detect retire).
- Le bouton ouvre directement la section README:
  - `https://github.com/itanivalkyrie/SushiDL?tab=readme-ov-file#-r%C3%A9cup%C3%A9rer-user-agent-et-cf_clearance`
- Remplacement de l'ancienne capture distante dans le README par l'image locale `assets/screenshot.jpg`.

### Maintenance
- Ajout de `manual_links.cloudflare_help` dans `config.json`.

## [11.0.0] - 2026-02-20

### Evolution majeure
- Suppression du workflow auto d'authentification:
  - retrait FlareSolverr,
  - retrait Playwright,
  - retrait import cookies navigateur (`browser-cookie3`).
- Application passee en mode auth manuel uniquement (`cf_clearance` `.fr` / `.net` + `User-Agent`).

### Ameliorations
- Ajout de placeholders cliquables dans la GUI:
  - champ cookie `.fr` -> `https://sushiscan.fr`
  - champ cookie `.net` -> `https://sushiscan.net`
  - champ User-Agent -> `https://httpbin.org/user-agent`
- Retrait du bouton `Auto detecter Auth`.
- Barre de statut simplifiee: `Auth: manuel`.

### Maintenance
- Nettoyage des dependances: suppression de `playwright` et `browser-cookie3` de `requirements.txt`.
- `config.json` simplifie autour du mode manuel.

## [10.11.4] - 2026-02-20

### Corrections
- `Auto detecter Auth` via Playwright ouvre maintenant systematiquement les deux domaines (`.fr` et `.net`) pour tenter de remplir les deux cookies.
- Renommage du badge `Challenge non present` en `Sans challenge`.
- Ajout d'un etat explicite `CF requis` quand le challenge est present et que le cookie `cf_clearance` est vide.
- Elargissement des badges de statut pour eviter la troncature des libelles.

## [10.11.3] - 2026-02-20

### Corrections
- Auto-detection Playwright: priorite au domaine de l'URL courante (`.fr`/`.net`) pour la premiere navigation.
- Ajout de logs de navigation Playwright par domaine et URL cible.
- Ouverture d'un nouvel onglet par domaine + `bring_to_front()` pour rendre la sequence `.fr`/`.net` visible.

## [10.11.2] - 2026-02-20

### Corrections
- Fallback Playwright renforce pour `sushiscan.net`:
  - essais multi-URLs plus robustes (`sushiscan`, `www`, `/`, `/catalogue/`, URL courante),
  - utilisation optionnelle d'un profil persistant (`.playwright_profile`) pour limiter les boucles de challenge,
  - support du canal navigateur (`channel=chrome`) avec repli automatique sur Chromium si indisponible,
  - patch anti-detection automation leger (`navigator.webdriver`, `languages`, `plugins`).

## [10.11.1] - 2026-02-20

### Corrections
- Badge `Challenge non present` aligne sur la meme couleur bleue que le bouton `Analyser` (accent UI).

## [10.11.0] - 2026-02-20

### Ameliorations
- Ajout d'un 4e etat de badge cookie: `Challenge non present`.
- Detection explicite du challenge Cloudflare par domaine (`.fr`/`.net`) lors de la validation, avec message de log dedie.
- Libelles de gauche simplifies (suppression de `Utilisateur`, conservation de `FlareSolverr` uniquement quand applicable).

## [10.10.2] - 2026-02-20

### Corrections
- Fallback Playwright renforce pour la recuperation `cf_clearance` `.net`:
  - essais multi-URLs (`sushiscan`, `www`, `/`, `/catalogue/`),
  - utilisation prioritaire de l'URL saisie dans l'application quand elle correspond au domaine cible,
  - isolation par contexte Playwright par domaine (`.fr`/`.net`).

## [10.10.1] - 2026-02-20

### Corrections
- Correction Playwright fallback: remplacement de l'appel invalide `BrowserContext.user_agent` par une lecture compatible via `navigator.userAgent`.
- Suppression de l'erreur `playwright:echec global:'BrowserContext' object has no attribute 'user_agent'` lors de `Auto detecter Auth`.

## [10.10.0] - 2026-02-20

### Ameliorations
- Ajout d'un 3e etat visuel de statut cookie: `A controler` (orange `#FFC067`) quand le cookie est valide mais ancien.
- Seuil `A controler` base sur l'anciennete du cookie: 24h (`COOKIE_REVIEW_AGE_SECONDS=86400`).
- Conservation des timestamps `cookie_updated_at` entre sauvegardes tant que la valeur du cookie ne change pas.

## [10.9.0] - 2026-02-20

### Ameliorations
- Ajout d'un fallback Playwright dans `Auto detecter Auth` pour recuperer `cf_clearance` (`.fr`/`.net`) quand l'import navigateur classique echoue.
- Recuperation du User-Agent Playwright (applique automatiquement quand des cookies Playwright sont recuperes).
- Nouvelle section `auth_import.playwright_fallback` dans `config.json`:
  - `enabled`
  - `headless`
  - `timeout_seconds`
  - `challenge_wait_seconds`

## [10.8.4] - 2026-02-20

### Corrections
- Correction du statut cookie au demarrage: le domaine non actif (`.fr`/`.net`) ne passe plus en `Invalide` uniquement parce qu'il n'est pas encore revalide en reseau.
- Badge cookie du domaine non actif affiche desormais un etat valide provisoire tant qu'un cookie est present.

## [10.8.3] - 2026-02-20

### Corrections
- Ajout d'un pre-check permissions avant `Auto detecter Auth`:
  - verification du mode administrateur,
  - detection des navigateurs ouverts (risque de verrou cookies),
  - test d'acces `cookies.sqlite` Firefox.
- Ajout de messages de diagnostic actionnables dans le journal pour aider a debloquer l'import `cf_clearance`.

## [10.8.2] - 2026-02-20

### Corrections
- Badge User-Agent corrige: un champ User-Agent vide passe desormais en `Invalide`.
- Validation UA rendue plus stricte quand aucun cookie actif n'est present (pas de validation positive via source residuelle).

## [10.7.1] - 2026-02-20

### Corrections
- Placeholder `Filtre` restaure automatiquement apres `Analyser` quand le champ est vide.
- Bouton de reset filtre (`x`) retravaille visuellement en vrai bouton (`x`) avec bordure et survol.

## [10.8.0] - 2026-02-20

### Ameliorations
- Bloc FlareSolverr retire de la GUI quand `flaresolverr.enabled=false` (masquage complet, pas de champ grise).
- Configuration plus lisible en mode direct (sans FlareSolverr).

## [10.7.2] - 2026-02-20

### Corrections
- Import auto cookies: ajout d'un fallback Firefox direct via `cookies.sqlite` (sans decryption).
- Reduction du bruit de logs pour erreurs connues Chrome/Edge (`admin requis`, dechiffrement indisponible).
- Message utilisateur plus clair quand l'import auto echoue a cause des protections navigateur Windows.

## [10.7.0] - 2026-02-20

### Ameliorations
- FlareSolverr desactive par defaut via `config.json` (reste activable facilement).
- Ajout du bouton `Auto detecter Auth` dans la GUI:
  - detection du User-Agent local (registre Windows),
  - import automatique des cookies `cf_clearance` `.fr` / `.net` depuis les navigateurs locaux.
- Ajout de `config.json` versionne pour centraliser les options globales.

### Corrections
- Pipeline de telechargement/couverture ajuste pour respecter la desactivation FlareSolverr.
- Messages UI/log plus explicites pour guider l'utilisateur quand FlareSolverr est desactive.
## [10.6.12] - 2026-02-20

### Corrections
- Correction de la detection de domaine SushiScan pour inclure les sous-domaines (*.sushiscan.fr / *.sushiscan.net).
- Application correcte des cookies et User-Agent sur les URLs d'images/couverture (ex: c1.sushiscan.net), reduisant les erreurs HTTP 403.
- Labels d'authentification dynamiques dans la configuration:
  - Cookie (.fr/.net) Utilisateur ou Cookie (.fr/.net) FlareSolverr
  - User-Agent Utilisateur ou User-Agent FlareSolverr
- Remplacement des anciennes coches de statut par des badges visuels rouges/verts (Invalide / Valide).
- Nettoyage des logs (sans emojis) pour limiter les problemes d'encodage en terminal Windows.
## [10.6.11] - 2026-02-20

### Corrections
- Renforcement anti-encodage:
  - en-tete source explicite UTF-8 dans `SushiDL.py`
  - configuration console Windows en UTF-8 conservee
  - nettoyage des caracteres emojis dans les logs terminal Windows
- Suppression de l'emoji dans le titre de fenetre pour eviter les affichages parasites.
- Logs GUI sans emojis sous Windows pour une lisibilite stable.

## [10.6.10] - 2026-02-20

### Corrections
- Correction d'encodage UTF-8 dans `SushiDL.py` (suppression des textes corrompus du type `telecharger`).
- Configuration explicite de la console Windows en UTF-8 au démarrage.
- Désactivation des emojis dans les logs terminal sous Windows pour éviter les caractères parasites.

## [10.6.9] - 2026-02-20

### Ameliorations
- README simplifie dans la section `Versioning & Changelog`.
- Suppression de la liste detaillee des versions du README pour laisser l'historique complet uniquement dans `CHANGELOG.md`.

## [10.6.8] - 2026-02-20

### Ameliorations
- README renforce sur la migration FlareSolverr:
  - ancienne image `21hsmw/flaresolverr:nodriver` depreciee
  - nouvelle image officielle `flaresolverr/flaresolverr:latest`
  - lien explicite Docker Hub: `https://hub.docker.com/r/flaresolverr/flaresolverr`

## [10.6.7] - 2026-02-20

### Ameliorations
- README mis a jour pour documenter explicitement FlareSolverr `3.4.6` (API v2).
- Remplacement des instructions Docker vers l'image officielle:
  - `flaresolverr/flaresolverr:latest`
  - Lien: `https://hub.docker.com/r/flaresolverr/flaresolverr`

## [10.6.6] - 2026-02-20

### Corrections
- Compatibilite FlareSolverr v2 (`3.4.6`): suppression du parametre `userAgent` dans les payloads `/v1`.
- Suppression des warnings FlareSolverr `Request parameter 'userAgent' was removed in FlareSolverr v2`.
- Conservation de la recuperation automatique de `cf_clearance` et du `userAgent` retourne par FlareSolverr.

## [10.6.5] - 2026-02-20

### Corrections
- Libelles bouton avec accents dans l'UI:
  - `Telecharger` -> `Télécharger`
  - `Telechargement...` -> `Téléchargement...`

## [10.6.4] - 2026-02-20

### Corrections
- Barre d'etat (`Domaine actif | Cookie | FlareSolverr`) ancree en bas de fenetre pour rester visible au lancement.
- Ajustement de la hauteur minimale du journal pour eviter d'avoir a redimensionner manuellement la fenetre.
- Leger compactage vertical du conteneur principal pour mieux tenir dans la hauteur par defaut.

## [10.6.3] - 2026-02-20

### Corrections
- Normalisation systematique de l'URL FlareSolverr avant chaque appel reseau (analyse, refresh cookie, fallback).
- Correction automatique des fautes de saisie de type `host!8191` vers `host:8191`.
- Synchronisation du champ GUI avec l'URL corrigee pour eviter les erreurs `curl: (3) URL rejected: Bad hostname`.
- Cache `cookie_cache.json` desormais enregistre/recharge avec URL FlareSolverr normalisee.

## [10.6.1] - 2026-02-20

### Corrections
- Libelle de source cookie dans la barre de statut: `manual` devient `manuel` (affichage utilisateur).
- Champ renomme: `User-Agent direct` -> `User-Agent utilisateur`.

## [10.6.0] - 2026-02-20

### Ameliorations
- Les options `.CBZ`, `WEBP en JPG`, `Logs detailes`, `Logs terminal` sont maintenant integrees dans l'encart `Configuration`.
- Taille de fenetre ajustee a `1140x980` (min `940x760`) pour rester confortable sur ecrans 1920x1080.
- Barre de filtre compacte:
  - libelle `Filtre` integre dans le champ (placeholder),
  - bouton `x` integre dans la zone de saisie,
  - suppression du bouton `Effacer` separe.
- Ajustement de la hauteur du journal pour conserver une bonne lisibilite dans cette hauteur de fenetre.

## [10.5.0] - 2026-02-20

### Ameliorations
- Integration visuelle du pourcentage de progression (`0%`, `42%`, etc.) en fin de barre, sans coupure centrale de la ProgressBar.
- Lisibilite amelioree de la zone de progression pendant le telechargement.

## [10.4.0] - 2026-02-20

### Ameliorations
- Barre `Tomes / Chapitres` reorganisee selon le flux d'usage:
  - A gauche: `Filtre` + `Effacer`, puis bouton global de selection, puis `Inverser`
  - A droite: `Telecharger` et `Annuler`
- Remplacement de la checkbox `Tous/Aucun` par un bouton dynamique (`Tout cocher` / `Tout decocher`).
- Couleurs d'action explicites:
  - `Telecharger` en vert `#ADEBB3`
  - `Annuler` en rouge `#FA003F`
- Taille de fenetre par defaut legerement augmentee pour mieux afficher la ligne du bas.

## [10.3.0] - 2026-02-20

### Ameliorations
- Reorganisation ergonomique des controles dans `Tomes / Chapitres`.
- Nouveau regroupement: `Tous/Aucun` et `Inverser` a gauche, `Telecharger` et `Annuler` proches du filtre a droite.
- Ordre des actions revu pour accelerer le workflow selection -> telechargement.

## [10.2.0] - 2026-02-20

### Améliorations
- Intégration des actions `Tous/Aucun`, `Inverser`, `Telecharger`, `Annuler` dans l’en-tête de `Tomes / Chapitres`.
- Suppression du bloc `Actions` séparé pour réduire la redondance visuelle et libérer de l’espace pour le `Journal`.

## [10.1.1] - 2026-02-20

### Corrections
- Amélioration de la lisibilité du `Journal` en bas de fenêtre (zone log plus visible et extensible).
- Ajustement compact de la zone `Source` pour libérer de l’espace vertical utile.
- Réglage fin des hauteurs/paddings (`Tomes / Chapitres`, `Actions`, `Journal`) pour un affichage plus équilibré au démarrage.

## [10.1.0] - 2026-02-20

### Améliorations
- Renommage du script principal en `SushiDL.py`.
- Affichage explicite de la version au lancement (console + interface).
- Ajustements d’ergonomie GUI (taille de fenêtre par défaut, lisibilité globale, zone `Tomes / Chapitres` limitée visuellement à ~4 lignes avec scroll).
- Modernisation visuelle inspirée du thème Breeze.

### Corrections
- Correction du crash Tkinter lors de l’analyse (`ttk.Checkbutton` avec option `anchor` invalide).
- Restauration de la visibilité des zones basses (`Actions`, `Journal`) sans redimensionnement manuel excessif.
