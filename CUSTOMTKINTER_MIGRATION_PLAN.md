# CustomTkinter Migration Plan

## Objectif

Moderniser l'interface de `SushiDL.py` avec `CustomTkinter` sans reecrire le backend
et sans casser la logique reseau / download / threading deja en place.

Objectif prioritaire:
- gain visuel rapide
- reduction du code de style manuel
- garder le comportement actuel
- limiter les regressions

Objectif non prioritaire dans la premiere passe:
- remplacer toute la logique UI
- remplacer `Treeview`
- refondre le backend

## Principe

La bonne strategie n'est pas de convertir tout `SushiDL.py` d'un coup.
Il faut migrer par couches:

1. la coque de l'application
2. les blocs les plus visibles
3. les widgets qui ont un equivalent direct dans `CustomTkinter`
4. les zones complexes ensuite

## Etat actuel de l'UI

Zones principales dans `SushiDL.py`:

- `configure_styles()` :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4097`
  - contient la palette, les styles `ttk`, les boutons, progressbars, treeview, tabs

- `setup_ui()` :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4293`
  - construit toute l'interface

- logs GUI :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4471`

- bloc authentification :
  - cookies / UA :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4501`

- bloc options :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4679`

- bloc source + cover :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4791`

- bloc selection / filtre / actions download :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4916`

- bloc progression :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:5072`

- tableau erreurs :
  - `/abs/path` : `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:5148`

## Ce qu'il faut garder au debut

Ces parties doivent rester stables dans la phase 1:

- `run_on_ui()` :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:2539`
- `process_ui_queue()` :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:2569`
- `load_volumes()` :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:5672`
- `download_selected()` :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:6021`
- `log()` :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:3758`

Conclusion:
- on change la presentation
- on ne change pas le moteur au debut

## Mapping des widgets

Remplacements simples:

- `tk.Tk` -> `customtkinter.CTk`
- `tk.Toplevel` -> `customtkinter.CTkToplevel`
- `tk.Frame` / `ttk.Frame` -> `customtkinter.CTkFrame`
- `tk.Label` / `ttk.Label` -> `customtkinter.CTkLabel`
- `tk.Entry` / `ttk.Entry` -> `customtkinter.CTkEntry`
- `tk.Button` / `ttk.Button` -> `customtkinter.CTkButton`
- `ttk.Checkbutton` -> `customtkinter.CTkCheckBox`
- `ttk.Progressbar` -> `customtkinter.CTkProgressBar`
- `ttk.Combobox` -> `customtkinter.CTkComboBox`
- `tk.Text` -> `customtkinter.CTkTextbox`
- faux tabs maison -> `customtkinter.CTkTabview` ou `CTkSegmentedButton`
- zone scrollable canvas -> `customtkinter.CTkScrollableFrame` si possible

Widgets a conserver temporairement:

- `ttk.Treeview`
- certaines `Scrollbar` si besoin
- `Canvas` si la migration rapide est prioritaire

## Ordre de migration recommande

### Phase 1 - Coque et theming global

But:
- retirer le gros du style `ttk` manuel
- poser une base moderne coherent

Travail:
- remplacer la fenetre racine par `CTk`
- definir `customtkinter.set_appearance_mode("light")`
- definir `customtkinter.set_default_color_theme(...)`
- garder temporairement `self.palette`, mais la simplifier
- remplacer les `App.TFrame` / `Card.TFrame` par de vrais `CTkFrame`

Zones ciblees:
- `configure_styles()`
- debut de `setup_ui()`

Impact:
- fort visuellement
- faible risque fonctionnel

Estimation:
- 0.5 a 1 jour

### Phase 2 - Bloc Auth + Source

But:
- moderniser la zone la plus visible
- gagner en lisibilite immediatement

Travail:
- remplacer les `Entry` cookies + UA + URL
- remplacer les boutons `Analyser`, `Tester tout`, `Aide Cookie`, `Sauvegarder`
- remplacer les badges de statut par des `CTkLabel` ou petites pills custom
- moderniser la zone cover autour de l'image existante

Zones ciblees:
- auth :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4501`
- source :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4791`

Impact:
- tres fort
- peu de dette technique supplementaire

Estimation:
- 0.5 a 1 jour

### Phase 3 - Onglets custom

But:
- supprimer les montages manuels `Frame + Label + mask`
- reduire le code fragile de layout

Travail:
- remplacer les onglets `Configuration` et `Selection` par `CTkTabview`
- supprimer progressivement:
  - `_layout_tab_row`
  - `_refresh_selection_tab_buttons`
  - `_refresh_config_tab_buttons`
  - masques visuels haut/bas

Zones ciblees:
- bloc config tabs :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4372`
- bloc selection tabs :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4847`

Impact:
- gain de code
- gain visuel fort
- baisse du cout de maintenance

Estimation:
- 0.5 a 1 jour

### Phase 4 - Zone selection / download / progression

But:
- rendre la zone d'action plus moderne
- clarifier la priorite visuelle

Travail:
- remplacer filtre, boutons d'action, labels de statut, progress bar
- revoir la hierarchie visuelle:
  - filtre a gauche
  - actions secondaires au centre
  - download principal a droite
  - progression en bloc compact

Zones ciblees:
- filtre / boutons :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4916`
- progression :
  - `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:5072`

Impact:
- fort
- peu de risque metier

Estimation:
- 0.5 a 1 jour

### Phase 5 - Journal

But:
- moderniser une zone tres visible
- ameliorer la lisibilite globale

Travail:
- remplacer `tk.Text` par `CTkTextbox`
- garder la logique de logs actuelle
- conserver les filtres et boutons d'export/copie

Zone ciblee:
- `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:4471`

Impact:
- moyen a fort
- faible risque

Estimation:
- 0.5 jour

### Phase 6 - Liste des tomes

But:
- enlever l'effet Tk le plus visible
- simplifier le scroll si possible

Deux options:

Option rapide:
- garder `Canvas + Frame`
- moderniser seulement les checkboxes et l'entourage

Option propre:
- migrer vers `CTkScrollableFrame`
- reconstruire la liste de tomes dans ce conteneur

Zone ciblee:
- `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:5038`

Impact:
- fort
- cout variable selon l'option choisie

Estimation:
- option rapide : 0.5 jour
- option propre : 1 jour

### Phase 7 - Tableau erreurs

But:
- finaliser le facelift
- traiter la seule grosse zone encore en `ttk`

Recommandation:
- ne pas le traiter dans la premiere passe
- garder `ttk.Treeview` au debut
- eventuellement le re-styler

Pourquoi:
- `CustomTkinter` n'a pas de remplacement natif aussi pratique
- cette zone apporte moins de valeur visuelle immediate que les autres

Zone ciblee:
- `c:\Users\Antonio Di Fargas\Desktop\Sushiscan\SushiDL_clean\SushiDL.py:5148`

Estimation:
- simple restyling : 0.5 jour
- refonte plus poussee : 1 a 1.5 jour

## Ce qu'il faut supprimer ou simplifier

Code probablement simplifiable apres migration:

- `configure_styles()` :
  - une grande partie des styles `ttk` ne servira plus
- faux onglets custom
- masques visuels de bordure
- une partie des `tk.Frame` decoratifs
- une partie des couleurs stockees dans `self.palette`

## Architecture cible minimale

Sans faire une grosse refonte backend, je conseille au moins:

- laisser la logique metier dans les fonctions actuelles
- isoler l'UI CustomTkinter dans des sous-methodes:
  - `_build_config_area()`
  - `_build_auth_area()`
  - `_build_source_area()`
  - `_build_selection_area()`
  - `_build_log_area()`
  - `_build_error_area()`

Ca permettra:
- des diffs plus propres
- des tests manuels plus simples
- une migration etalee sans casser le reste

## Plan de livraison concret

### Version A - Relooking rapide

Contenu:
- phase 1
- phase 2
- phase 4
- phase 5

On garde:
- tabs custom temporaires
- `Treeview`
- `Canvas`

Temps:
- 2 a 3 jours

Resultat:
- gros gain visuel
- risque limite

### Version B - Relooking propre

Contenu:
- phase 1 a 6

On garde:
- `Treeview`

Temps:
- 4 a 5 jours

Resultat:
- interface nettement plus moderne
- code UI plus propre

### Version C - Relooking quasi complet

Contenu:
- phase 1 a 7

Temps:
- 5 a 6 jours

Resultat:
- tres bon rendu pour une base Tk
- mais on reste tout de meme dans les limites de Tk / ttk

## Recommandation finale

Le meilleur ratio pour ce projet:

1. Phase 1
2. Phase 2
3. Phase 4
4. Phase 5
5. Phase 6 plus tard

Donc:
- d'abord la coque
- ensuite auth + source
- ensuite actions / progression
- ensuite journal
- ensuite liste tomes

Ne commence pas par:
- `Treeview`
- les details du tableau erreurs
- les micro-finitions de style

## Point de depart technique

Pour lancer la migration, la premiere PR devrait faire seulement:

- ajout de `customtkinter` dans `requirements.txt`
- remplacement de la fenetre racine
- creation d'un theme global
- migration du bloc auth/source
- aucune modification du backend

Si cette PR est stable, la suite devient simple.
