from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from .actions import analyze_current_url, apply_range_selection, apply_text_filter, deselect_all, invert_selection, select_all, toggle_item_selection
from .download import CliDownloadController
from .modals import MessageModal, TextPromptModal


class WorkflowScreen(Screen):
    BINDINGS = [
        ("escape", "go_back", "Retour"),
        ("/", "focus_filter", "Filtre"),
        ("space", "toggle_current", "Toggle"),
        ("a", "select_all", "Tout"),
        ("n", "select_none", "Rien"),
        ("i", "invert", "Inverser"),
        ("r", "select_range", "Plage"),
        ("t", "download", "Telecharger"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("URL / Chapitres / Telechargement", id="screen-title")
        with Vertical(id="workflow-layout"):
            with Vertical(classes="panel"):
                yield Label("Source", classes="panel-title")
                with Horizontal(classes="button-row"):
                    yield Input(value="", placeholder="https://sushiscan.fr/catalogue/slug/", id="workflow-url")
                    yield Button("Analyser", id="analyze", variant="primary")
            with Vertical(classes="panel"):
                yield Label("Resume analyse", classes="panel-title")
                yield Label("Titre : --", id="workflow-title")
                yield Label("Domaine : --", id="workflow-domain")
                yield Label("Selection : 0 element", id="workflow-selection")
            with Vertical(classes="panel", id="workflow-list-panel"):
                with Horizontal(classes="button-row"):
                    yield Input(value="", placeholder="Filtre", id="workflow-filter")
                    yield Button("Tout cocher", id="sel-all")
                    yield Button("Tout decocher", id="sel-none")
                    yield Button("Inverser", id="sel-invert")
                    yield Button("Plage", id="sel-range")
                self.selection_list = ListView(id="workflow-list")
                yield self.selection_list
            with Horizontal(classes="button-row"):
                yield Button("Telecharger", id="download", variant="success")
                yield Button("Retour", id="back")
        yield Label("", id="workflow-status")

    def on_mount(self) -> None:
        self.refresh_from_state()
        self.query_one("#workflow-url", Input).focus()

    def on_show(self) -> None:
        self.refresh_from_state()

    def refresh_from_state(self) -> None:
        state = self.app.cli_state
        self.query_one("#workflow-url", Input).value = state.current_url
        self.query_one("#workflow-title", Label).update(f"Titre : {state.current_title or '--'}")
        self.query_one("#workflow-domain", Label).update(f"Domaine : {state.current_domain or '--'}")
        self.query_one("#workflow-selection", Label).update(f"Selection : {state.selection_summary}")
        self.query_one("#workflow-status", Label).update(state.status_message)

        self.selection_list.clear()
        for item_idx in state.filtered_indices:
            item = state.detected_items[item_idx]
            marker = "[x]" if item.url in state.selected_urls else "[ ]"
            self.selection_list.append(ListItem(Label(f"{marker} {item.label}"), id=f"item-{item_idx}"))

    def _selected_visible_index(self) -> int | None:
        highlighted = self.selection_list.highlighted_child
        if highlighted is None or not highlighted.id or not highlighted.id.startswith("item-"):
            return None
        try:
            return int(highlighted.id.split("-", 1)[1])
        except Exception:
            return None

    def _toggle_selected(self) -> None:
        item_index = self._selected_visible_index()
        if item_index is None:
            return
        if item_index not in self.app.cli_state.filtered_indices:
            return
        visible_index = self.app.cli_state.filtered_indices.index(item_index)
        toggle_item_selection(self.app.cli_state, visible_index)
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
        elif event.button.id == "download":
            self.action_download()
        elif event.button.id == "back":
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

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_focus_filter(self) -> None:
        self.query_one("#workflow-filter", Input).focus()

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
            self.app.push_screen(MessageModal("Telechargement", "Aucun element selectionne."))
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
