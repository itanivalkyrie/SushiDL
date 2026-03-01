from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from .actions import analyze_current_url, apply_range_selection, apply_text_filter, deselect_all, invert_selection, select_all, toggle_item_selection
from .download import CliDownloadController
from .modals import HelpModal, MessageModal, TextPromptModal


class WorkflowScreen(Screen):
    BINDINGS = [
        ("up,k", "nav_up", "Monter"),
        ("down,j", "nav_down", "Descendre"),
        ("left", "focus_prev_zone", "Zone prec."),
        ("right", "focus_next_zone", "Zone suiv."),
        ("u", "focus_url", "URL"),
        ("l", "focus_list", "Liste"),
        ("escape", "go_back", "Retour"),
        ("f5", "analyze", "Analyser"),
        ("/", "focus_filter", "Filtre"),
        ("space", "toggle_current", "Toggle"),
        ("a", "select_all", "Tout"),
        ("n", "select_none", "Rien"),
        ("i", "invert", "Inverser"),
        ("r", "select_range", "Plage"),
        ("t", "download", "Telecharger"),
        ("h", "show_help", "Aide"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("URL / Chapitres / Téléchargement", id="screen-title")
        yield Label("", id="workflow-warning", classes="terminal-warning")
        with Vertical(id="workflow-layout"):
            with Vertical(classes="panel", id="workflow-source-panel"):
                yield Label("Source", classes="panel-title", id="workflow-source-title")
                with Horizontal(classes="button-row", id="workflow-source-row"):
                    yield Input(value="", placeholder="https://sushiscan.fr/catalogue/slug/", id="workflow-url")
                    yield Button("Analyser", id="analyze", variant="primary")
            with Vertical(classes="panel", id="workflow-summary-panel"):
                yield Label("Résumé analyse", classes="panel-title", id="workflow-summary-title")
                with Horizontal(classes="workflow-summary-row"):
                    yield Label("Titre : --", id="workflow-title", classes="workflow-summary-cell")
                    yield Label("Domaine : --", id="workflow-domain", classes="workflow-summary-cell")
                with Horizontal(classes="workflow-summary-row"):
                    yield Label("Sélection : 0 élément", id="workflow-selection", classes="workflow-summary-cell")
                    yield Label("Visibles : 0", id="workflow-visible", classes="workflow-summary-cell")
                yield Label("Courant : --", id="workflow-current")
                yield Label("Raccourcis : F5 analyser | / filtre | Espace bascule | T télécharger", id="workflow-shortcuts")
            with Vertical(classes="panel", id="workflow-list-panel"):
                yield Label("Tomes / Chapitres", classes="panel-title", id="workflow-list-title")
                with Horizontal(classes="button-row", id="workflow-filter-row"):
                    yield Input(value="", placeholder="Filtre", id="workflow-filter")
                    yield Button("Tout cocher", id="sel-all")
                    yield Button("Tout decocher", id="sel-none")
                    yield Button("Inverser", id="sel-invert")
                    yield Button("Plage", id="sel-range")
                with Horizontal(classes="button-row", id="workflow-compact-actions"):
                    yield Button("Télécharger", id="download-compact", variant="success")
                    yield Button("Retour", id="back-compact")
                self.selection_list = ListView(id="workflow-list")
                yield self.selection_list
            with Horizontal(classes="button-row", id="workflow-actions-row"):
                yield Button("Télécharger", id="download", variant="success")
                yield Button("Retour", id="back")
        yield Label("", id="workflow-status")

    def on_mount(self) -> None:
        self.refresh_from_state()
        self.query_one("#workflow-url", Input).focus()

    def on_show(self) -> None:
        self.refresh_from_state()
        if self.app.cli_state.filtered_indices:
            self.selection_list.focus()
        else:
            self.query_one("#workflow-url", Input).focus()

    def on_resize(self, _event=None) -> None:
        self.apply_terminal_mode()

    def refresh_from_state(self) -> None:
        state = self.app.cli_state
        previous_index = getattr(self.selection_list, "index", None)
        self.query_one("#workflow-url", Input).value = state.current_url
        self.query_one("#workflow-title", Label).update(f"Titre : {state.current_title or '--'}")
        self.query_one("#workflow-domain", Label).update(f"Domaine : {state.current_domain or '--'}")
        self.query_one("#workflow-selection", Label).update(f"Sélection : {state.selection_summary}")
        self.query_one("#workflow-visible", Label).update(f"Visibles : {len(state.filtered_indices)} / {len(state.detected_items)}")
        self.query_one("#workflow-status", Label).update(state.status_message)

        self.selection_list.clear()
        index_width = max(2, len(str(max(1, len(state.detected_items)))))
        for item_idx in state.filtered_indices:
            item = state.detected_items[item_idx]
            self.selection_list.append(ListItem(Label(self._build_item_text(item, index_width))))
        if state.filtered_indices:
            safe_index = 0 if previous_index is None else max(0, min(int(previous_index), len(state.filtered_indices) - 1))
            self.selection_list.index = safe_index
        self._refresh_current_item_label()
        self.apply_terminal_mode()

    def _build_item_text(self, item, index_width: int) -> Text:
        is_selected = item.url in self.app.cli_state.selected_urls
        marker = "[x]" if is_selected else "[ ]"
        marker_style = "bold green" if is_selected else "dim"
        text = Text()
        text.append(f"{item.index:>{index_width}}  ", style="bold cyan")
        text.append(f"{marker}  ", style=marker_style)
        text.append(item.label or f"Element {item.index}", style="white")
        return text

    def _refresh_current_item_label(self) -> None:
        state = self.app.cli_state
        if not state.filtered_indices:
            self.query_one("#workflow-current", Label).update("Courant : --")
            return
        current_index = getattr(self.selection_list, "index", 0)
        if current_index is None or current_index < 0 or current_index >= len(state.filtered_indices):
            current_index = 0
        item = state.detected_items[state.filtered_indices[current_index]]
        selected = "selectionne" if item.url in state.selected_urls else "non selectionne"
        self.query_one("#workflow-current", Label).update(f"Courant : #{item.index} | {item.label} | {selected}")

    def apply_terminal_mode(self) -> None:
        mode = self.app.terminal_mode(self)
        warning = self.app.terminal_warning_message(self)
        warning_label = self.query_one("#workflow-warning", Label)
        warning_label.update(warning)

        compact = mode in {"compact", "too_small"}
        too_small = mode == "too_small"

        self.query_one("#analyze", Button).label = "Analyser" if not compact else "Analyse"
        self.query_one("#sel-all", Button).label = "Tout" if compact else "Tout cocher"
        self.query_one("#sel-none", Button).label = "Rien" if compact else "Tout decocher"
        self.query_one("#sel-invert", Button).label = "Inv." if compact else "Inverser"
        self.query_one("#sel-range", Button).label = "Plage"
        self.query_one("#download", Button).label = "Télécharger" if not compact else "Tél."
        self.query_one("#download-compact", Button).label = "Tél."

        self.query_one("#workflow-shortcuts", Label).styles.display = "none" if compact else "block"
        self.query_one("#workflow-current", Label).styles.display = "none" if compact else "block"
        self.query_one("#workflow-source-title", Label).styles.display = "none" if compact else "block"
        self.query_one("#workflow-summary-title", Label).styles.display = "none" if compact else "block"
        self.query_one("#workflow-list-title", Label).styles.display = "none" if compact else "block"

        compact_actions = self.query_one("#workflow-compact-actions", Horizontal)
        full_actions = self.query_one("#workflow-actions-row", Horizontal)
        compact_actions.styles.display = "block" if compact else "none"
        full_actions.styles.display = "none" if compact else "block"

        source_panel = self.query_one("#workflow-source-panel", Vertical)
        summary_panel = self.query_one("#workflow-summary-panel", Vertical)
        list_panel = self.query_one("#workflow-list-panel", Vertical)
        source_panel.styles.padding = (0, 1) if compact else (0, 1)
        summary_panel.styles.padding = (0, 1) if compact else (0, 1)
        list_panel.styles.padding = (0, 1) if compact else (0, 1)
        source_panel.styles.margin = (0, 1, 0, 1) if compact else (0, 1, 1, 1)
        summary_panel.styles.margin = (0, 1, 0, 1) if compact else (0, 1, 1, 1)
        list_panel.styles.margin = (0, 1, 1, 1)

        has_items = bool(self.app.cli_state.detected_items)
        if has_items:
            list_panel.styles.height = "1fr"
            list_panel.styles.min_height = 12 if not compact else 7
            self.selection_list.styles.height = "1fr"
            self.selection_list.styles.min_height = 8 if not compact else 4
        else:
            list_panel.styles.height = "auto"
            list_panel.styles.min_height = 4 if compact else 6
            self.selection_list.styles.height = 2 if compact else 4
            self.selection_list.styles.min_height = 2 if compact else 3

    def _selected_visible_index(self) -> int | None:
        current_index = getattr(self.selection_list, "index", None)
        if current_index is None:
            return None
        if current_index < 0 or current_index >= len(self.app.cli_state.filtered_indices):
            return None
        return self.app.cli_state.filtered_indices[current_index]

    def _focus_order(self):
        compact = self.app.terminal_mode(self) in {"compact", "too_small"}
        return [
            self.query_one("#workflow-url", Input),
            self.query_one("#analyze", Button),
            self.query_one("#workflow-filter", Input),
            self.query_one("#sel-all", Button),
            self.query_one("#sel-none", Button),
            self.query_one("#sel-invert", Button),
            self.query_one("#sel-range", Button),
            self.selection_list,
            self.query_one("#download-compact", Button) if compact else self.query_one("#download", Button),
            self.query_one("#back-compact", Button) if compact else self.query_one("#back", Button),
        ]

    def _focused_index(self) -> int:
        focused = self.app.focused
        for idx, widget in enumerate(self._focus_order()):
            if focused is widget:
                return idx
        return 0

    def _move_list_selection(self, delta: int) -> bool:
        if self.app.focused is not self.selection_list:
            return False
        visible = self.app.cli_state.filtered_indices
        if not visible:
            return False
        current = getattr(self.selection_list, "index", 0) or 0
        current = max(0, min(int(current) + delta, len(visible) - 1))
        self.selection_list.index = current
        self._refresh_current_item_label()
        return True

    def _toggle_selected(self) -> None:
        item_index = self._selected_visible_index()
        if item_index is None:
            return
        if item_index not in self.app.cli_state.filtered_indices:
            return
        toggle_item_selection(self.app.cli_state, getattr(self.selection_list, "index", 0))
        self.refresh_from_state()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "analyze":
            self._run_analysis()
        elif event.button.id == "sel-all":
            self.action_select_all()
        elif event.button.id == "sel-none":
            self.action_select_none()
        elif event.button.id == "sel-invert":
            self.action_invert()
        elif event.button.id == "sel-range":
            self.action_select_range()
        elif event.button.id in {"download", "download-compact"}:
            self.action_download()
        elif event.button.id in {"back", "back-compact"}:
            self.action_go_back()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "workflow-url":
            self._run_analysis()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "workflow-url":
            self.app.cli_state.current_url = event.value.strip()
        elif event.input.id == "workflow-filter":
            apply_text_filter(self.app.cli_state, event.value)
            self.refresh_from_state()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is not self.selection_list:
            return
        event.stop()
        self._toggle_selected()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_analyze(self) -> None:
        self._run_analysis()

    def action_focus_filter(self) -> None:
        self.query_one("#workflow-filter", Input).focus()

    def action_focus_url(self) -> None:
        self.query_one("#workflow-url", Input).focus()

    def action_focus_list(self) -> None:
        self.selection_list.focus()

    def action_toggle_current(self) -> None:
        self._toggle_selected()

    def action_select_all(self) -> None:
        select_all(self.app.cli_state)
        self.refresh_from_state()

    def action_select_none(self) -> None:
        deselect_all(self.app.cli_state)
        self.refresh_from_state()

    def action_invert(self) -> None:
        invert_selection(self.app.cli_state)
        self.refresh_from_state()

    def action_select_range(self) -> None:
        def apply_value(value: str | None) -> None:
            if value is None:
                return
            apply_range_selection(self.app.cli_state, value)
            self.refresh_from_state()

        self.app.push_screen(TextPromptModal("Selection par plage", placeholder="1-20,50+,100-120"), apply_value)

    def action_download(self) -> None:
        if not self.app.cli_state.selected_urls:
            self.app.push_screen(MessageModal("Téléchargement", "Aucun élément sélectionné."))
            return
        controller = getattr(self.app, "download_controller", None)
        if controller and controller.snapshot().active:
            self.app.push_screen("download")
            return

        default_output = self.app.cli_state.download_status.output_dir or "DL SushiScan"

        def start_with_output(value: str | None) -> None:
            if value is None:
                return
            output_dir = (value or "").strip() or "DL SushiScan"
            self.app.download_controller = CliDownloadController(self.app.backend, self.app.cli_state, output_dir)
            self.app.download_controller.start()
            self.app.push_screen("download")

        self.app.push_screen(TextPromptModal("Dossier de sortie", value=default_output, placeholder="DL SushiScan"), start_with_output)

    def _run_analysis(self) -> None:
        state = self.app.cli_state
        state.current_url = self.query_one("#workflow-url", Input).value.strip()
        try:
            analyze_current_url(self.app.backend, state)
        except Exception as exc:
            state.reset_analysis()
            state.status_message = f"Analyse impossible: {exc}"
        self.refresh_from_state()

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Aide workflow",
                "Entre une URL puis lance l'analyse.\n"
                "Espace ou Entrée bascule la ligne courante.\n"
                "/ active le filtre.\n"
                "A tout cocher, N tout décocher, I inverser, R sélection par plage.\n"
                "T ouvre le téléchargement.\n"
                "Fleches/J/K naviguent. U focus URL, L focus liste.",
            )
        )

    def action_nav_up(self) -> None:
        if self._move_list_selection(-1):
            return
        focusables = self._focus_order()
        focusables[max(0, self._focused_index() - 1)].focus()

    def action_nav_down(self) -> None:
        if self._move_list_selection(1):
            return
        focusables = self._focus_order()
        focusables[min(len(focusables) - 1, self._focused_index() + 1)].focus()

    def action_focus_prev_zone(self) -> None:
        focused = self.app.focused
        url_input = self.query_one("#workflow-url", Input)
        filter_input = self.query_one("#workflow-filter", Input)
        if focused in {
            filter_input,
            self.query_one("#sel-all", Button),
            self.query_one("#sel-none", Button),
            self.query_one("#sel-invert", Button),
            self.query_one("#sel-range", Button),
            self.query_one("#download-compact", Button),
            self.query_one("#back-compact", Button),
            self.selection_list,
        }:
            url_input.focus()
        else:
            target = "#download-compact" if self.app.terminal_mode(self) in {"compact", "too_small"} else "#download"
            self.query_one(target, Button).focus()

    def action_focus_next_zone(self) -> None:
        focused = self.app.focused
        url_widgets = {
            self.query_one("#workflow-url", Input),
            self.query_one("#analyze", Button),
        }
        list_widgets = {
            self.query_one("#workflow-filter", Input),
            self.query_one("#sel-all", Button),
            self.query_one("#sel-none", Button),
            self.query_one("#sel-invert", Button),
            self.query_one("#sel-range", Button),
            self.query_one("#download-compact", Button),
            self.query_one("#back-compact", Button),
            self.selection_list,
        }
        if focused in url_widgets:
            self.query_one("#workflow-filter", Input).focus()
        elif focused in list_widgets:
            target = "#download-compact" if self.app.terminal_mode(self) in {"compact", "too_small"} else "#download"
            self.query_one(target, Button).focus()
        else:
            self.query_one("#workflow-url", Input).focus()
