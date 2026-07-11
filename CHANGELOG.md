# Changelog

Toutes les modifications notables de ce projet seront documentees dans ce fichier.

Le format de version suit la regle `X.Y.Z` :
- `X` = evolution majeure
- `Y` = amelioration / nouvelle fonctionnalite secondaire
- `Z` = correctif (bugfix)

## [11.16.7] - 2026-07-11

### Corrections
- CrunchyScan / Scan-Hentai :
  - attente bornée de la création initiale des blobs du lecteur pour éviter les attentes très longues sans téléchargement,
  - canvas ignoré pour les images cassées afin d'éviter l'erreur `InvalidStateError`,
  - diagnostic des erreurs JavaScript du lecteur et logs Playwright placés dans le pipeline de téléchargement.

## [11.16.6] - 2026-07-11

### Ameliorations
- Journal :
  - ajout des étapes Playwright visibles pour Scan-Manga, CrunchyScan et Scan-Hentai,
  - distinction entre l'analyse API du lecteur Scan-Manga et la récupération des fichiers via Playwright,
  - affichage de l'initialisation de session, du nombre d'images détectées et des reprises de contexte lecteur.

## [11.16.5] - 2026-07-11

### Corrections
- CrunchyScan / Scan-Hentai :
  - chargement plus robuste des pages lazy du lecteur avant lecture du blob,
  - seconde tentative dans un contexte Chromium propre après un échec lecteur transitoire,
  - récupération des auteurs et artistes dans les métadonnées `ComicInfo.xml`.

## [11.16.4] - 2026-07-11

### Corrections
- CrunchyScan / Scan-Hentai :
  - ajout d'un fallback canvas quand `fetch(blob:)` échoue,
  - conversion JPEG depuis l'image rendue dans le lecteur navigateur.

## [11.16.3] - 2026-07-11

### Corrections
- CrunchyScan / Scan-Hentai :
  - attente explicite des images du lecteur au lieu de partir dès la présence de `data-meta`,
  - tentative de validation des overlays simples avant extraction,
  - diagnostic détaillé si aucune image n'est créée par le lecteur.

## [11.16.2] - 2026-07-11

### Corrections
- CrunchyScan / Scan-Hentai :
  - prise en charge des cookies bruts `cf_clearance` dans le contexte Playwright,
  - attente plus robuste de l'initialisation du lecteur avant comptage des images,
  - erreur explicite quand Cloudflare bloque la page chapitre `/read/...`.

## [11.16.1] - 2026-07-11

### Corrections
- CrunchyScan / Scan-Hentai :
  - extraction de la date de sortie depuis le bloc `Sortie`,
  - extraction du statut depuis le bloc `Status`,
  - extraction des genres et du type depuis les blocs `Genre(s)` et `Type`.

## [11.16.0] - 2026-07-11

### Ameliorations
- Ajout du support `crunchyscan.fr` :
  - validation d'URL catalogue `/lecture-en-ligne/...`,
  - parsing des chapitres,
  - cookie dédié,
  - extraction des couvertures.
- Ajout du support `scan-hentai.net` :
  - validation d'URL catalogue `/lecture-en-ligne/...`,
  - parsing des chapitres,
  - cookie dédié,
  - extraction des couvertures.
- Téléchargement CrunchyScan / Scan-Hentai via Playwright obligatoire :
  - le lecteur chiffre les images et les expose sous forme de blobs côté navigateur,
  - SushiDL réutilise une session Chromium dédiée pour limiter les ouvertures multiples,
  - les tentatives directes HTTP ne sont pas utilisées pour ces deux domaines.

## [11.15.39] - 2026-06-27

### Ameliorations
- Scan-Manga :
  - détection des groupes `Webtoon X` dans la liste des chapitres,
  - affichage compact `W1 C12` pour les chapitres webtoon.

## [11.15.38] - 2026-06-27

### Ameliorations
- Interface :
  - rendu canvas virtualisé activé pour toutes les listes de tomes/chapitres,
  - prise en charge canvas du mode `Confort/card`,
  - suppression du seuil qui faisait encore utiliser des widgets Tk sur les petites listes,
  - statut de sélection adapté au rendu canvas permanent.

## [11.15.37] - 2026-06-27

### Ameliorations
- Suivi :
  - ajout d'un onglet `Suivi` pour gérer les catalogues à surveiller,
  - ajout/suppression d'URLs dans `watchlist.json` depuis l'interface,
  - vérification manuelle d'une entrée ou de toute la liste sans téléchargement automatique,
  - affichage du dernier nombre connu, de la date de vérification et des nouveautés détectées,
  - rafraîchissement du cache d'analyse après une vérification réseau fraîche.

## [11.15.36] - 2026-06-27

### Corrections
- Interface :
  - rendu virtualisé des catalogues Scan-Manga groupés par tome pour éviter les traînées visuelles pendant le scroll rapide,
  - ajout d'un pool d'en-têtes de tome dessiné sur canvas,
  - cadence de rafraîchissement canvas plus régulière pendant les scrolls rapides,
  - cache du découpage virtuel des tomes pour réduire les recalculs,
  - nettoyage renforcé des éléments canvas au rechargement de la liste.

## [11.15.35] - 2026-06-14

### Ameliorations
- Suivi :
  - ajout de `catalog_state.json` pour conserver l'état connu des catalogues entre deux démarrages,
  - comparaison automatique des chapitres/tomes après chaque analyse réussie,
  - affichage/log des nouveautés détectées dans le flux d'analyse classique,
  - ajout du socle `watchlist.json` pour le futur planificateur.

## [11.15.34] - 2026-05-31

### Corrections
- Couvertures :
  - conservation des suffixes numériques Scan-Manga comme `_7111` dans les URLs de couverture,
  - choix de la meilleure variante selon les dimensions réelles téléchargées pour éviter une image parasite plus petite.

## [11.15.33] - 2026-05-25

### Ameliorations
- Interface :
  - virtualisation déclenchée plus tôt sur les gros catalogues,
  - lots UI plus courts pour améliorer la réactivité,
  - compactage des logs répétitifs dans le journal GUI.
- Scan-Manga :
  - preview Novel limitée à une page pour éviter un rendu complet inutile,
  - annulation plus réactive pendant l'extraction lecteur/Novel.
- Couvertures :
  - extraction améliorée des meilleures sources (`srcset`, lazy-load, `og:image`, `twitter:image`),
  - tentative de variante haute résolution avec fallback sur l'URL originale.

## [11.15.32] - 2026-05-25

### Corrections
- Scan-Manga :
  - activation de la preview pour les chapitres Novel en texte,
  - génération limitée à la première page Novel pour garder l'aperçu rapide,
  - prise en charge des URLs internes `sushidl-textpage://` dans la fenêtre de preview.

## [11.15.31] - 2026-05-25

### Corrections
- Scan-Manga :
  - conservation de la ponctuation source dans le rendu Novel, notamment les deux-points,
  - ajout d'une police de secours pour les symboles (`☆☆☆`, `◇`, chiffres cerclés),
  - centrage par défaut des images intégrées aux chapitres Novel.

## [11.15.30] - 2026-05-25

### Corrections
- Scan-Manga :
  - correction de l'extraction Novel sur les pages live dont le HTML est mal imbriqué après des balises `<br>`,
  - évite le fallback erroné vers le lecteur image et l'erreur `Variables lecteur Scan-Manga introuvables`.

## [11.15.29] - 2026-05-25

### Corrections
- Scan-Manga :
  - amélioration de la mise en page des chapitres Novel générés en JPG,
  - retour à une police serif plus proche du lecteur Novel,
  - conservation de certains espacements HTML (`br`, paragraphes vides),
  - évite les pages de titre isolées quand le contenu peut tenir sur la première page.

## [11.15.28] - 2026-05-25

### Corrections
- Scan-Manga :
  - validation du rendu Novel sur un chapitre réel contenant une image intégrée,
  - support des chemins d'images réécrits par les exports HTML locaux (`*_files/...`), utile pour les diagnostics.

## [11.15.27] - 2026-05-25

### Ameliorations
- Scan-Manga :
  - intégration des images contenues dans les chapitres Novel au rendu JPG/CBZ,
  - prise en charge des images relatives, lazy-load et `srcset` dans `.ln_c_content`,
  - conservation de l'alignement des images dans le flux texte.

## [11.15.26] - 2026-05-25

### Ameliorations
- Scan-Manga :
  - conservation de l'alignement HTML des chapitres Novel, notamment les blocs centrés,
  - rendu avec une police Unicode plus complète pour limiter les caractères absents ou mal affichés.

## [11.15.25] - 2026-05-25

### Ameliorations
- Scan-Manga :
  - détection des chapitres Novel dont le lecteur contient du texte au lieu d'images,
  - rendu automatique du texte en pages JPG pour conserver la sortie CBZ habituelle,
  - évite les erreurs de récupération d'images sur les chapitres sans galerie image.

## [11.15.24] - 2026-05-25

### Corrections
- Interface :
  - affichage compact des labels Scan-Manga dans la grille pour éviter les retours à la ligne,
  - conservation des labels complets pour le filtre, le téléchargement et le nommage CBZ.

## [11.15.23] - 2026-05-25

### Ameliorations
- Scan-Manga :
  - séparation du label d'affichage et du label d'archive,
  - affichage court en GUI (`Tome X - Chap Y-Z`) tout en gardant le titre complet uniquement pour le nom CBZ,
  - invalidation du cache d'analyse pour recalculer les métadonnées d'archive.

## [11.15.22] - 2026-05-25

### Ameliorations
- Scan-Manga :
  - labels de chapitres volumés raccourcis au format `Tome X - Chap Y-Z`,
  - conservation des tirets de sous-chapitres pour rendre les parties visibles dans la grille compacte,
  - invalidation du cache d'analyse pour recalculer les labels.

## [11.15.21] - 2026-05-25

### Corrections
- Interface :
  - conservation de la virtualisation sur les très gros catalogues Scan-Manga au lieu de créer tous les widgets pour les séparateurs par tome,
  - réduction du freeze après parsing et du lag au scroll dans la fenêtre de choix des chapitres.

## [11.15.20] - 2026-05-25

### Ameliorations
- Scan-Manga :
  - conservation du titre complet des chapitres dans les labels et les noms CBZ,
  - séparation visuelle par tome dans la grille GUI après analyse,
  - invalidation du cache d'analyse pour recalculer les labels complets.

## [11.15.19] - 2026-05-25

### Corrections
- Scan-Manga :
  - ajout du volume parent dans les libellés de chapitres (`Tome X - Chapitre Y`) pour éviter les collisions de noms CBZ,
  - conservation du détail des chapitres ambigus comme `Chapitre Extra`,
  - invalidation du cache d'analyse pour recalculer les anciens libellés Scan-Manga.

## [11.15.18] - 2026-05-25

### Corrections
- Interface :
  - correction du titre tronqué dans la fenêtre `File d'attente` en séparant le titre et le texte d'aide dans un en-tête dédié.

## [11.15.17] - 2026-05-24

### Corrections
- Scan-Manga :
  - recyclage du contexte Playwright entre les tentatives d'une même image quand le CDN renvoie un blocage,
  - limitation des blocages en chaîne lors des téléchargements de plusieurs chapitres consécutifs.

## [11.15.16] - 2026-05-24

### Corrections
- Scan-Manga :
  - recyclage du contexte Playwright entre deux chapitres pour éviter les blocages qui disparaissaient après redémarrage,
  - conservation d'un seul navigateur Chrome afin de garder les téléchargements en série plus stables sans rouvrir des sessions en parallèle.

## [11.15.15] - 2026-05-24

### Corrections
- Scan-Manga :
  - validation automatique de l'avertissement lecteur dans le contexte Playwright avant de récupérer les images CDN,
  - stabilisation des téléchargements de chapitres avec avertissement quand la session navigateur passe d'un chapitre au suivant.

## [11.15.14] - 2026-05-24

### Corrections
- Scan-Manga :
  - correction de l'extraction des URLs d'images sur les chapitres protégés par l'avertissement public averti,
  - isolation de l'appel API lecteur dans une session HTTP vierge pour éviter les réponses `HTTP 500` causées par l'état de navigation du lecteur.

## [11.15.13] - 2026-05-24

### Ameliorations
- Réactivité GUI :
  - batching des logs dans le journal Tkinter pour éviter une mise à jour widget par message,
  - traitement de la file UI avec limite de lot et budget temps afin de réduire les blocages courts,
  - regroupement des mises à jour progression / compteur images / ETA,
  - suppression des reconfigurations de widgets quand la valeur affichée ne change pas,
  - application différée du filtre de chapitres pour fluidifier la saisie sur gros catalogues.

## [11.15.12] - 2026-05-24

### Corrections
- Scan-Manga :
  - ajout d'un fallback Playwright via `BrowserContext.request` quand `page.evaluate(fetch)` retourne `Failed to fetch`,
  - amélioration de la récupération des images `cdn.scan-manga.com` instables sans repasser par le téléchargement HTTP direct classique,
  - validation par contenu réel quand le CDN ne renvoie pas de `content-type`.

## [11.15.11] - 2026-05-24

### Corrections
- Scan-Manga :
  - ajout de `cdn.scan-manga.com` à la liste des hôtes image traités exclusivement via Playwright,
  - correction des chapitres dont les pages ne viennent pas de `data*.scan-manga.com` et échouaient en HTTP direct avec 403.

## [11.15.10] - 2026-05-24

### Ameliorations
- Scan-Manga :
  - remplacement des contextes Playwright par thread par un thread navigateur unique,
  - les images et previews passent par une file interne pour réutiliser une seule session Chrome,
  - réduction forte du nombre de processus Chrome ouverts lors du téléchargement de plusieurs chapitres.

## [11.15.9] - 2026-05-24

### Corrections
- Scan-Manga :
  - isolation du contexte Playwright par thread pour éviter l'erreur `cannot switch to a different thread`,
  - la preview peut ouvrir son navigateur sans casser le téléchargement lancé ensuite depuis un worker.

## [11.15.8] - 2026-05-24

### Corrections
- Scan-Manga :
  - invalidation automatique des anciens caches d'analyse pour forcer une nouvelle extraction des métadonnées `ComicInfo.xml`,
  - évite que la popup affiche encore auteur, dessinateur, année, genres et statut vides après une mise à jour du parseur.

## [11.15.7] - 2026-05-24

### Corrections
- Scan-Manga :
  - les images et les previews passent maintenant exclusivement par Playwright,
  - les tentatives HTTP directes sur `data*.scan-manga.com` sont ignorées pour éviter les 403 répétés et les délais inutiles,
  - le contexte navigateur recharge le lecteur et retente la récupération quand certaines pages échouent après les premières images,
  - extraction corrigée des métadonnées `ComicInfo.xml` depuis la fiche technique : auteurs, dessinateurs, genres, année, éditeur, statut et résumé.

### Documentation
- README mis à jour pour expliquer que Playwright est obligatoire pour les images Scan-Manga.
- Documentation de la dépendance `playwright>=1.52.0` et de la commande `python -m playwright install chromium` en cas de navigateur manquant.

## [11.15.6] - 2026-05-24

### Ameliorations
- Ajout du support Scan-Manga :
  - analyse des pages catalogue `scan-manga.com`,
  - champ cookie `.scanmanga` dans l'onglet Authentification,
  - extraction des chapitres, couvertures et URLs du lecteur.

## [11.15.3] - 2026-05-22

### Ameliorations
- Popup `Cookie à renouveler` :
  - ajout d'un champ de saisie/collage du nouveau cookie directement dans la popup,
  - mise à jour et sauvegarde automatique du cookie au clic sur `OK et relancer`,
  - relance de l'analyse ou du téléchargement sans basculer vers l'onglet Authentification.

## [11.15.2] - 2026-05-22

### Corrections
- Champ URL catalogue :
  - interception des collages clavier et clic droit,
  - extraction automatique d'une URL supportée depuis un texte multi-lignes,
  - rejet propre des contenus non texte ou sans URL exploitable pour éviter un champ bloqué.

## [11.15.1] - 2026-05-22

### Corrections
- Popup `Cookie à renouveler` :
  - suppression du bouton `Aller à Authentification`,
  - l'onglet Authentification reste ouvert automatiquement comme avant.

## [11.15.0] - 2026-05-22

### Ameliorations
- Ajout d'un cache disque des analyses catalogue avec TTL configurable via `config.json`.
- Ajout du raccourci `Ctrl+R` pour forcer une analyse sans cache.
- Ajout d'un préflight avant téléchargement :
  - sélection,
  - CBZ déjà présents,
  - premium ignorés,
  - espace disque,
  - sorties actives,
  - threads effectifs.
- Ajout de diagnostics cookie plus détaillés dans la popup de renouvellement.
- Ajout de profils `fragile_sites` configurables pour limiter automatiquement threads et délai entre volumes.
- Ajout d'un résumé performance en fin de traitement.

## [11.14.1] - 2026-05-22

### Corrections
- Analyse catalogue :
  - en cas d'erreur compatible avec un cookie expire ou invalide, SushiDL propose maintenant la mise a jour du cookie,
  - apres confirmation, l'analyse est relancee automatiquement une seule fois.

## [11.14.0] - 2026-05-22

### Ameliorations
- Telechargement adaptatif :
  - reduction automatique du nombre de threads sur erreurs serveur, timeout ou rate-limit,
  - reprise intelligente au point d'arret apres ralentissement,
  - relance apres renouvellement cookie avec 1 thread de securite.
- Diagnostic performance :
  - ajout de logs `[perf]` pour l'analyse catalogue, la couverture, l'extraction images, le telechargement images, l'archive CBZ et le temps volume complet.

## [11.13.0] - 2026-05-22

### Ameliorations
- Ajout d'un cache session des URLs d'images :
  - evite de reparser plusieurs fois le meme chapitre/tome,
  - reutilise une extraction complete pour les previews limitees.
- Preview plus rapide :
  - conservation stricte de la limite des premieres pages utiles,
  - reutilisation immediate des URLs deja connues en session.
- Interface plus fluide pendant les telechargements :
  - regroupement des mises a jour de progression,
  - reduction des appels au thread UI sur les gros lots.

## [11.12.0] - 2026-05-22

### Ameliorations
- Optimisation du telechargement image :
  - ecriture en flux dans un fichier temporaire `.part`,
  - validation image avant renommage final,
  - reduction des copies memoire pendant les gros lots.
- Ajout d'un reglage `Telechargements paralleles` dans l'onglet `Options` :
  - valeur persistante dans `cookie_cache.json`,
  - bornage entre 1 et 8 threads,
  - valeur par defaut conservee a 3.
- Ajout de `--threads 1-8` au mode CLI non interactif.
- Reduction de la memoire utilisee par les previews :
  - cache limite,
  - redimensionnement des images de preview.
- Plafonnement des animations GIF de couverture pour eviter les pics memoire.

## [11.11.2] - 2026-05-22

### Corrections
- ComicInfo.xml :
  - le champ `Publisher` conserve l'editeur detecte quand il existe,
  - en absence d'editeur, la source est maintenant le domaine complet (`sushiscan.fr`, `sushiscan.net`, etc.) au lieu de l'alias cookie (`fr`, `net`).

## [11.11.1] - 2026-05-22

### Corrections
- ComicInfo.xml :
  - le telechargement GUI propose maintenant une verification/modification des metadonnees avant la creation des CBZ quand l'option `ComicInfo.xml` est activee.

## [11.11.0] - 2026-05-22

### Ameliorations
- Ajout d'une file d'attente GUI pour lancer plusieurs URLs catalogue a la suite.
- Ajout d'un mode CLI non interactif :
  - `--url`,
  - `--url-file`,
  - `--range`,
  - `--download`,
  - `--dry-run`,
  - options de sortie `--no-cbz`, `--no-comicinfo`, `--no-cover`, `--no-webp2jpg`, `--no-resume`.
- Ajout d'un editeur de metadonnees ComicInfo en GUI avant telechargement.
- Ajout d'un rapport `SushiDL_report.txt` dans les CBZ crees avec pages manquantes ou invalides.

## [11.10.0] - 2026-05-20

### Ameliorations
- Ajout de l'option `Couverture chapitres` dans les options de sortie.
- La couverture de la fiche catalogue est ajoutee en premiere page des CBZ de chapitres sous le nom `000_cover.jpg`.
- Les CBZ de tomes complets ne recoivent pas de couverture ajoutee.
- L'option est persistante dans `cookie_cache.json`.
- Le mode terminal expose aussi l'option `Couverture chapitres`.

## [11.9.5] - 2026-05-20

### Corrections
- ComicInfo.xml :
  - ajout de la variante Manga-Origines `/manga-genres/` pour recuperer les genres des fiches catalogue.

## [11.9.4] - 2026-05-20

### Corrections
- ComicInfo.xml :
  - ajout du selecteur ToonFR `/webtoon-genre/` pour recuperer les genres depuis les fiches webtoon.

## [11.9.3] - 2026-05-20

### Corrections
- ComicInfo.xml :
  - generalisation de l'extraction des genres pour `.fr`, `.net`, Origines, Hentaizone et OrtegaScans,
  - prise en charge des blocs `genres-content`, des URLs `manga-genre` et des liens Ortega `?tags=`.

## [11.9.2] - 2026-05-20

### Corrections
- ComicInfo.xml :
  - correction de l'extraction du genre sur `.net`,
  - ajout du selecteur cible `.seriestugenre` pour recuperer le vrai genre sans reprendre les tags globaux de la page.

## [11.9.1] - 2026-05-20

### Corrections
- ComicInfo.xml :
  - les genres ne sont plus alimentes par les tags globaux de page (`rel=tag`),
  - le resume visible complet est prefere aux metas HTML tronquees,
  - ajout de l'extraction depuis les tables HTML classiques pour les champs auteur, dessinateur, annee et genre.

## [11.9.0] - 2026-05-20

### Ameliorations
- Enrichissement du `ComicInfo.xml` avec les metadonnees disponibles sur les fiches catalogue.
- Ajout des champs ComicInfo suivants quand ils sont detectes :
  - `Summary`,
  - `Year`,
  - `Month`,
  - `Day`,
  - `Writer`,
  - `Penciller`,
  - `Translator`,
  - `Genre`,
  - `ScanInformation`.
- Extraction best effort depuis :
  - les balises HTML `meta`,
  - les blocs WordPress/Madara de type `post-content_item`,
  - les liens de genres/tags,
  - les donnees embarquees Ortega quand elles sont presentes.
- Propagation des metadonnees en GUI et en mode terminal.

## [11.8.0] - 2026-05-20

### Ameliorations
- Ajout d'une option `ComicInfo.xml` dans l'onglet `Options > Sortie`.
- Generation automatique d'un `ComicInfo.xml` dans chaque archive CBZ quand l'option est activee.
- Metadonnees compatibles Komga / ComicRack :
  - `Series`,
  - `Title`,
  - `Number`,
  - `Count`,
  - `PageCount`,
  - `LanguageISO`,
  - `Manga`,
  - `Web`,
  - `Publisher`,
  - `Tags`,
  - `Notes`.
- L'option est persistante dans `cookie_cache.json`.
- Le mode terminal expose aussi l'option `ComicInfo.xml`.

## [11.7.2] - 2026-03-01

### Corrections
- Telechargement :
  - les contenus non image (`invalid_image`) ne sont plus retentes en boucle,
  - ils sont marques comme pages ignorees des la premiere detection.
- Hentaizone :
  - evite les tentatives repetees sur des URLs parsees mais invalides cote serveur,
  - le `CBZ` reste genere avec les pages valides quand le reste est incomplet.

## [11.7.1] - 2026-03-01

### Corrections
- Hentaizone :
  - un chapitre peut maintenant etre finalise en `.cbz` meme si une ou plusieurs pages sont manquantes ou invalides.
- Telechargement :
  - les contenus recus non reconnus comme image sont traites comme pages ignorees au lieu de bloquer tout le chapitre,
  - la creation du `CBZ` est maintenue si au moins une partie valide du chapitre a ete telechargee.
- Logs :
  - ajout d'un message explicite quand un `CBZ` est cree avec des pages manquantes ou invalides.

## [11.7.0] - 2026-03-01

### Ameliorations
- Ajout du support de `https://hentaizone.xyz`.
- Nouveau format d'URL catalogue supporte :
  - `https://hentaizone.xyz/manga/<slug>/`
- Ajout d'un cookie manuel dedie `.hentaizone` dans l'onglet `Authentification` et dans le mode terminal.
- Validation du cookie `.hentaizone` sur :
  - `https://hentaizone.xyz/manga/stepmothers-friends/`
- Recuperation de la liste complete des chapitres HentaiZone directement depuis le HTML de la fiche.
- Prise en charge des pages de lecture `.../chapitre-146/` et des images servies depuis `scanscloud.xyz`.

### Corrections
- Alignement des probes automatiques de l'onglet `Authentification` avec le User-Agent effectif de chaque domaine.
- Validation Toonfr corrigee :
  - le badge peut maintenant passer en valide sans attendre une analyse d'URL si le cookie et le User-Agent sont deja bons.

## [11.6.1] - 2026-03-01

### Corrections
- OrtegaScans :
  - recuperation de la liste complete des chapitres depuis les donnees `initialData` embarquees dans la page,
  - plus de limitation a la tranche visible avant clic sur `Charger plus`.
- Telechargement GUI :
  - ajout d'une popup de relance quand un cookie expire ou qu'un `HTTP 403` bloque un chapitre,
  - apres mise a jour du cookie, la reprise se fait au meme chapitre sans reinitialiser tout le lot.
- Titres :
  - si un titre detecte ne contient aucune majuscule, SushiDL applique au moins une majuscule initiale.

## [11.6.0] - 2026-03-01

### Ameliorations
- Ajout du support de `https://ortegascans.fr`.
- Nouveau format d'URL catalogue supporte :
  - `https://ortegascans.fr/serie/<slug>/`
- Ajout d'un cookie manuel dedie `.ortegascans` dans l'onglet `Authentification`.
- Le champ `.ortegascans` accepte desormais :
  - un simple `cf_clearance`
  - ou un header `Cookie` complet
- Detection des chapitres premium Ortega :
  - affichage d'un badge `$` dore dans le listing,
  - preview desactivee,
  - telechargement ignore automatiquement meme si le chapitre est selectionne.

### Corrections
- Standardisation des libelles de chapitres :
  - `Ep 221 - Ma brute` devient `Chapitre 221`
  - les noms de dossiers et archives CBZ suivent maintenant ce format normalise.
- Mode terminal :
  - prise en charge du flag premium dans la liste et dans le telechargement,
  - cookie `.ortegascans` disponible dans l'ecran `Options / Cookies`.

## [11.5.0] - 2026-03-01

### Ameliorations
- Ajout du support de `https://toonfr.com`.
- Nouveau format d'URL catalogue supporte :
  - `https://toonfr.com/webtoon/<slug>/`
- Ajout d'un cookie manuel dedie `.toonfr` dans l'onglet `Authentification`.
- Le champ `.toonfr` accepte desormais :
  - un simple `cf_clearance`
  - ou un header `Cookie` complet
- Validation du cookie `.toonfr` depuis la GUI et le mode terminal.
- Recuperation des chapitres Toonfr via l'endpoint AJAX Madara du site (`base_url + ajax/chapters/`).

### Corrections
- Analyse catalogue :
  - detection correcte du domaine `toonfr`,
  - mapping cookie/URL ajoute dans le backend,
  - integration du domaine dans les sondes de verification auth.
- Lecture chapitres :
  - prise en charge des pages de lecture Toonfr dans le backend.

### Documentation
- README mis a jour avec le nouveau site supporte et le format d'URL `toonfr.com/webtoon/...`.

## [11.4.0] - 2026-03-01

### Ameliorations
- ajout d'un mode terminal interactif lance via `python SushiDL.py --cli`,
- menu principal terminal,
- ecran `Options / Cookies`,
- ecran `URL / Chapitres / Telechargement`,
- ecran `Telechargement`,
- ecran `Erreurs`,
- selection par plage, filtre texte, copie/export des erreurs,
- telechargement reel avec progression, annulation et resume final.
- TUI:
  - navigation clavier et souris amelioree,
  - mode compact automatique,
  - avertissement quand le terminal est trop petit,
  - ecran workflow compacte pour garder les actions visibles.

### Corrections
- Preview GUI:
  - limitation de l'extraction aux premieres pages utiles en mode preview,
  - suppression du warning `CTkLabel ... Given image is not CTkImage` via usage de `CTkImage`.
- TUI:
  - correction des `DuplicateIds` dans les listes,
  - correction du clic souris dans la liste chapitres/tomes,
  - durcissement des modales de confirmation et de l'export d'erreurs.

### Dependances
- `requirements.txt` inclut maintenant `textual>=0.82.0`.

## [11.3.0] - 2026-02-28

### Ameliorations
- Ajout d'une icone de fenetre native `assets/sushidl.ico` en multi-tailles (`16`, `24`, `32`, `48`, `64`, `128`, `256`).
- Chargement d'icone durci selon l'OS:
  - Windows: utilisation prioritaire de `iconbitmap(.ico)`,
  - Linux / macOS: fallback `iconphoto(...)` sans appel natif Windows inutile.

### Maintenance
- Les fichiers de travail de creation d'icone ne sont pas versionnes:
  - `assets/sushi_icon_source.png`
  - `assets/sushidl_icon_preview.png`

## [11.2.11] - 2026-02-28

### Ameliorations
- Preview integree dans le listing des tomes / chapitres:
  - ajout d'une loupe verte entre le titre et la coche bleue,
  - ouverture d'une popup dediee pour previsualiser rapidement un chapitre ou tome,
  - chargement limite aux `3 a 5` premieres pages pour rester reactif,
  - navigation simple `Precedent / Suivant`,
  - cache memoire court pour reouvrir rapidement une preview deja chargee.
- Chargement plus lisible dans l'interface:
  - ajout d'un spinner leger dans le statut d'analyse,
  - ajout d'un overlay de chargement sur la zone tomes / chapitres pendant l'analyse et le rendu,
  - ajout d'un spinner dedie dans la popup preview.

### Technique
- Reutilisation du pipeline de recuperation d'images existant pour la preview, sans ecriture sur disque ni creation de CBZ.
- Couverture des deux chemins de rendu du listing:
  - rendu canvas virtualise pour gros catalogues,
  - rendu widgets pour petites listes.
- `main` embarque maintenant la variante `CustomTkinter` comme interface principale.
- `requirements.txt` inclut desormais `customtkinter>=5.2.2`.

## [11.2.10] - 2026-02-28

### Ameliorations
- Interface `CustomTkinter`:
  - remplacement de la double structure d'onglets par une barre unique en haut,
  - nouvel ordre d'ecrans:
    - `Telechargement`
    - `Journal`
    - `Erreurs`
    - `Authentification`
    - `Options`
  - chaque onglet occupe maintenant toute la hauteur disponible de la fenetre.
- Onglet `Telechargement`:
  - fusion de `Sources` et `Tomes / Chapitres` dans un seul espace de travail,
  - suppression des titres intermediaires redondants,
  - barre d'actions retravaillee avec meilleure integration du filtre, des actions de selection et du bloc de telechargement,
  - barre de progression refaite avec presentation plus lisible des statuts, compteurs images, ETA et pourcentage,
  - bloc URL / cover / analyse harmonise avec le reste de l'interface.
- Options:
  - cases a cocher `CustomTkinter` harmonisees avec le style du listing,
  - reduction de la hauteur de l'onglet pour liberer plus de place au flux principal.
- Ergonomie:
  - suppression du mode `Auto` et du mode `Confort`,
  - adoption d'un rendu dense unique par defaut pour privilegier la stabilite et la fluidite.

### Performance
- Gros catalogues:
  - remplacement progressif des widgets lourds par un rendu mutualise sur `Canvas`,
  - pool de cartes dessinees reutilise au scroll pour les listes volumineuses,
  - correction du centrage de la grille et de la derniere ligne visible,
  - filtrage conserve rapide sur grands catalogues.
- Scroll:
  - rearchitecture de la zone tomes / chapitres autour d'un `Canvas` explicite et d'une scrollbar dediee,
  - abandon de l'ancienne logique de spacers dans la zone scrollable,
  - calcul du viewport base sur la position reelle de scroll,
  - scroll plus stable visuellement sur les catalogues volumineux.

### Corrections
- Suppression d'un bug ou les chapitres pouvaient disparaitre temporairement pendant la navigation sur les grosses listes puis reapparaitre apres filtrage.
- Correction du cas `rendu 0/20` dans le statut d'analyse apres certains rerenders internes.
- Correction de plusieurs regressions de layout introduites pendant la phase de migration `CustomTkinter`:
  - libelles d'onglets,
  - largeur des boutons,
  - alignement des badges,
  - lisibilite des textes.

### Documentation
- README mis a jour pour refleter:
  - la nouvelle structure d'onglets,
  - la suppression des vues `Auto / Confort`,
  - le flux principal unifie dans `Telechargement`.

## [11.2.9] - 2026-02-28

### Ameliorations
- Branche `CustomTkinter`:
  - refonte visuelle generale vers un rendu plus sobre, plus lisible et plus coherent,
  - harmonisation des onglets `Journal / Authentification / Options` et `Tomes / Chapitres / Erreurs`,
  - toolbar de selection reequilibree avec filtre, actions, compteurs, vues et telechargement mieux alignes,
  - compteur fusionne en un seul badge (`0 element`, `1000 elements`, `1/1000 elements`, `1000/1000 elements`),
  - controle de vue `Auto / Dense / Confort` rendu plus lisible et reserve sur une largeur fixe.
- Performance gros catalogues:
  - ajout d'un mode liste legere pour les tres grandes listes,
  - virtualisation des widgets visibles dans la grille de tomes/chapitres,
  - cache des labels normalises pour accelerer le filtrage,
  - switches de vue et navigation plus fluides sur les catalogues volumineux.

### Corrections
- Analyse:
  - le rendu de la liste n'attend plus le thread worker de facon synchrone, ce qui evite les timeouts UI sur les tres gros catalogues.
- UX:
  - suppression du resume `Selection: x/y` separe au profit d'un compteur unique plus compact,
  - correction du statut d'analyse pour eviter les deplacements visuels pendant la mise a jour du texte.

### Mesures
- Validation reelle sur `https://sushiscan.fr/catalogue/one-piece/`:
  - recuperation du catalogue constatee a environ `2.1s` pour `1174` elements.
- Benchmark synthetique `1174` elements:
  - rendu initial proche de `1s`,
  - filtre et remise a zero autour de `0.1s`,
  - switch de vue `Dense / Confort / Auto` redescendu a une latence faible.

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

### Documentation
- README complet reecrit en format propre (UTF-8 stable) pour supprimer les problemes d'encodage.

## [11.2.7] - 2026-02-25

### Ameliorations
- Ajout du menu contextuel clic droit `Coller` sur les champs:
  - cookies (`.fr`, `.net`, `.origines`, `.hentai-origines`),
  - `User-Agent`,
  - `URL` source.

### Corrections
- Harmonisation des libelles et de la documentation:
  - suppression des mentions textuelles d'age,
  - conservation du seul marqueur `ðŸ”ž` pour les contenus adultes.

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
  - ajout de `hentai-origines.fr` (format manga: `/manga/<slug>/`, ðŸ”ž),
  - extraction des chapitres via endpoint AJAX Madara (`ajax/chapters/?t=`) quand la liste est chargee dynamiquement.
- Extraction images:
  - priorite forcee au mode `?style=list` pour les chapitres Origines,
  - fallback automatique depuis `?style=paged` et `/p/<n>/` vers la version `list`.
- Authentification manuelle:
  - ajout des cookies dedies `.origines` et `.hentai-origines` (ðŸ”ž) dans l'UI,
  - persistance complete dans `cookie_cache.json` (sources, headers, timestamps),
  - probes de validation cookie au demarrage pour les nouveaux domaines:
    - `https://mangas-origines.fr/oeuvre/826-solo-leveling/`
    - `https://hentai-origines.fr/manga/stop-smoking/` (ðŸ”ž).
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
    - `https://hentai-origines.fr/manga/<slug>/` (ðŸ”ž).

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
- Configuration explicite de la console Windows en UTF-8 au dÃ©marrage.
- DÃ©sactivation des emojis dans les logs terminal sous Windows pour Ã©viter les caractÃ¨res parasites.

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
  - `Telecharger` -> `TÃ©lÃ©charger`
  - `Telechargement...` -> `Telechargement...`

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

### AmÃ©liorations
- IntÃ©gration des actions `Tous/Aucun`, `Inverser`, `Telecharger`, `Annuler` dans lâ€™en-tÃªte de `Tomes / Chapitres`.
- Suppression du bloc `Actions` sÃ©parÃ© pour rÃ©duire la redondance visuelle et libÃ©rer de lâ€™espace pour le `Journal`.

## [10.1.1] - 2026-02-20

### Corrections
- AmÃ©lioration de la lisibilitÃ© du `Journal` en bas de fenÃªtre (zone log plus visible et extensible).
- Ajustement compact de la zone `Source` pour libÃ©rer de lâ€™espace vertical utile.
- RÃ©glage fin des hauteurs/paddings (`Tomes / Chapitres`, `Actions`, `Journal`) pour un affichage plus Ã©quilibrÃ© au dÃ©marrage.

## [10.1.0] - 2026-02-20

### AmÃ©liorations
- Renommage du script principal en `SushiDL.py`.
- Affichage explicite de la version au lancement (console + interface).
- Ajustements dâ€™ergonomie GUI (taille de fenÃªtre par dÃ©faut, lisibilitÃ© globale, zone `Tomes / Chapitres` limitÃ©e visuellement Ã  ~4 lignes avec scroll).
- Modernisation visuelle inspirÃ©e du thÃ¨me Breeze.

### Corrections
- Correction du crash Tkinter lors de lâ€™analyse (`ttk.Checkbutton` avec option `anchor` invalide).
- Restauration de la visibilitÃ© des zones basses (`Actions`, `Journal`) sans redimensionnement manuel excessif.
