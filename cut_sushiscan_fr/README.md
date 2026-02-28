# cut_sushiscan_fr

Script Python pour reconstruire des pages manga depuis des images decoupees (JPG/JPEG/PNG/WEBP), avec workflow adapte au format SushiScan FR.

## Fonctionnalites

- Trim pub:
- haut de la 1re image (`786px` par defaut)
- bas de la derniere image (`786px` par defaut)
- Concatenation verticale de toutes les images source.
- Nettoyage auto des chevauchements entre images source (OpenCV), avant concatenation.
- Decoupage en pages de hauteur fixe (`2132px` par defaut).
- Nettoyage auto des chevauchements a la frontiere des pages de sortie.
- Modes de sortie:
- `images` (JPG uniquement)
- `cbz` (CBZ, avec option suppression des JPG apres creation)
- `both` (JPG + CBZ)
- Mode interactif au lancement (selection source, destination, hauteur, options CBZ, verbose, suppressions).

## Prerequis

- Python 3.10+
- Dependances du `requirements.txt`
- OpenCV est utilise par defaut pour une detection plus robuste (fallback PIL si indisponible).

## Installation

```bash
cd cut_sushiscan_fr
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Utilisation

### 1) Mode interactif (recommande)

Si tu lances sans argument, le script passe en interactif:

```bash
python cut.py
```

Tu pourras choisir:
- dossier source
- dossier destination
- hauteur de page
- mode (`images`, `cbz`, `both`)
- verbose
- suppression des pages coupees apres creation du CBZ
- suppression des images source apres creation du CBZ

### 2) Mode ligne de commande

```bash
python cut.py "C:\chemin\vers\images" --output-folder "C:\chemin\vers\sortie"
```

Commande type SushiScan FR:

```bash
python cut.py "C:\chemin\vers\images" ^
  --trim-first-top 786 ^
  --trim-last-bottom 786 ^
  --page-height 2132 ^
  --mode both ^
  --verbose
```

## Destination par defaut

Si `--output-folder` n'est pas fourni, la sortie est:

```text
<source>\<nom_du_dossier_source>_cut
```

Exemple:

```text
...\Volume 1\Volume 1_cut
```

## Options principales

- `input_folder`
- dossier source des images

- `--interactive`
- force le mode interactif

- `--output-folder`
- dossier destination (sinon dossier `<source>_cut` dans la source)

- `--width-mode {auto,max,min,mode}` (defaut `auto`)
- strategie largeur: auto prend la largeur dominante et compresse automatiquement les images x2 (ex: `3248 -> 1624`) si ce ne sont pas de vraies doubles pages

- `--trim-first-top` (defaut `786`)
- `--trim-last-bottom` (defaut `786`)

- `--auto-banner-detect` / `--no-auto-banner-detect`
- ajuste automatiquement les trims de la 1re/derniere image en detectant le bandeau pub orange SushiScan

- `--page-height` (defaut `2132`)
- `0` active l'auto-detection

- `--carry-next-top-px` (defaut `0`)
- applique un decalage fixe des frontieres: les `N` px du haut de la page suivante sont rattaches a la precedente (`105` pour ton cas)

- `--page-bottom-trim`
- retire N px en bas de chaque page generee (defaut `0`)

- `--mode {images,cbz,both}`
- mode de sortie

- `--cbz`
- raccourci retrocompatible pour `mode=both`

- `--cbz-name`
- nom du CBZ

- `--delete-pages-after-cbz`
- supprime les pages JPG apres creation du CBZ

- `--keep-pages-after-cbz`
- conserve les pages JPG apres creation du CBZ

- `--delete-source-after-cbz`
- supprime les images source apres creation reussie du CBZ

- `--fix-bottom-overlap` / `--no-fix-bottom-overlap`
- active/desactive le nettoyage auto des micro chevauchements aux frontieres

- `--max-overlap-fix-px` (defaut `24`)
- max de pixels retire par frontiere

- `--overlap-fix-threshold` (defaut `4.0`)
- seuil de similarite (MAD grayscale) pour detecter un chevauchement

- `--overlap-fix-min-std` (defaut `2.5`)
- filtre texture minimal (0 desactive)

- `--fix-source-overlap` / `--no-fix-source-overlap`
- active/desactive la correction de chevauchement entre images source (avant concat)

- `--max-source-overlap-px` (defaut `300`)
- limite max retiree a chaque jonction source

- `--max-source-overlap-ratio` (defaut `0.4`)
- garde-fou proportionnel a la hauteur de l'image source

- `--source-overlap-threshold` (defaut `5.0`)
- seuil de similarite (MAD grayscale) cote source

- `--source-overlap-min-std` (defaut `1.5`)
- filtre texture minimal cote source

- `--source-overlap-skip-uniform` / `--no-source-overlap-skip-uniform`
- ignore les jonctions source majoritairement blanches/noires ou tres peu texturees (evite les faux positifs)

- `--overlap-fix-skip-white` / `--no-overlap-fix-skip-white`
- ignore les jonctions de sortie majoritairement blanches/noires ou tres peu texturees (evite des trims faux positifs)

- `--overlap-detector {cv,pil}`
- moteur de detection de chevauchement (defaut `cv` si OpenCV present)

- `--overlap-scan-width`, `--overlap-scan-step`
- reglages OpenCV pour la recherche coarse-to-fine

- `--save-strip`
- sauve la grande image concatenee en `_strip.jpg` (ou `_strip.png` si trop grande pour JPEG)

- `--skip-mostly-white-pages`
- ignore les pages majoritairement blanches

- `--verbose`
- logs detailles

## Exemples

CBZ uniquement (et suppression automatique des pages coupees):

```bash
python cut.py "C:\images" --mode cbz
```

CBZ + suppression sources apres creation:

```bash
python cut.py "C:\images" --mode cbz --delete-source-after-cbz --verbose
```

Conserver JPG + CBZ:

```bash
python cut.py "C:\images" --mode both --keep-pages-after-cbz
```

Diminuer legerement les micro parasites:

```bash
python cut.py "C:\images" --max-overlap-fix-px 12 --overlap-fix-threshold 4.5
```

Preset conseille pour cas "Chiruran" (images morcelees avec derive cumulative):

```bash
python cut.py "C:\images" ^
  --trim-first-top 786 ^
  --trim-last-bottom 786 ^
  --page-height 2132 ^
  --width-mode auto ^
  --page-bottom-trim 0 ^
  --fix-source-overlap ^
  --source-overlap-threshold 5.0 ^
  --source-overlap-min-std 1.5 ^
  --max-source-overlap-px 300 ^
  --no-fix-bottom-overlap ^
  --mode both ^
  --verbose
```

## Sorties

- `page_001.jpg`, `page_002.jpg`, ...
- (optionnel) `_strip.jpg`
- (optionnel) `<nom>.cbz`

## Notes

- Le script ne retire pas les watermarks/logos inclus dans les images source.
