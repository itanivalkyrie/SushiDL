from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static

from .modals import HelpModal


class MainMenuScreen(Screen):
    BINDINGS = [
        ("1", "open_settings", "Options"),
        ("2", "open_workflow", "Téléchargement"),
        ("3", "quit", "Quitter"),
        ("up", "focus_prev", "Prec."),
        ("down", "focus_next", "Suiv."),
        ("q", "quit", "Quitter"),
        ("h", "show_help", "Aide"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("SushiDL CLI", id="app-title")
        yield Static("Mode terminal interactif", id="app-subtitle")
        yield Label("", id="main-warning", classes="terminal-warning")
        with Vertical(id="main-menu"):
            yield Button("1. Options / Cookies", id="open-settings", variant="primary")
            yield Button("2. URL / Chapitres / Téléchargement", id="open-workflow")
            yield Button("3. Quitter", id="quit-app")
        yield Label("", id="main-status")

    def on_mount(self) -> None:
        self.query_one("#open-settings", Button).focus()
        self.refresh_status()
        self.apply_terminal_mode()

    def on_show(self) -> None:
        self.refresh_status()
        self.apply_terminal_mode()
        self.query_one("#open-settings", Button).focus()

    def on_resize(self, _event=None) -> None:
        self.apply_terminal_mode()

    def refresh_status(self) -> None:
        app_state = self.app.cli_state
        auth_ready = sum(1 for value in app_state.cookies.values() if (value or "").strip())
        cbz = "ON" if app_state.cbz_enabled else "OFF"
        comicinfo = "ON" if app_state.comicinfo_enabled else "OFF"
        cover = "ON" if app_state.chapter_cover_enabled else "OFF"
        resume = "ON" if app_state.smart_resume_enabled else "OFF"
        threads = getattr(app_state, "download_threads", 3)
        self.query_one("#main-status", Label).update(
            f"Etat rapide : Auth {auth_ready}/{len(app_state.cookies)} | User-Agent {'renseigne' if app_state.user_agent else 'vide'} | CBZ {cbz} | ComicInfo {comicinfo} | Cover {cover} | Reprise {resume} | Threads {threads}"
        )

    def apply_terminal_mode(self) -> None:
        self.query_one("#main-warning", Label).update(self.app.terminal_warning_message(self))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-settings":
            self.action_open_settings()
        elif event.button.id == "open-workflow":
            self.action_open_workflow()
        elif event.button.id == "quit-app":
            self.app.exit()

    def _menu_buttons(self) -> list[Button]:
        return [
            self.query_one("#open-settings", Button),
            self.query_one("#open-workflow", Button),
            self.query_one("#quit-app", Button),
        ]

    def _focused_menu_index(self) -> int:
        focused = self.app.focused
        buttons = self._menu_buttons()
        for idx, button in enumerate(buttons):
            if focused is button:
                return idx
        return 0

    def action_focus_prev(self) -> None:
        buttons = self._menu_buttons()
        index = max(0, self._focused_menu_index() - 1)
        buttons[index].focus()

    def action_focus_next(self) -> None:
        buttons = self._menu_buttons()
        index = min(len(buttons) - 1, self._focused_menu_index() + 1)
        buttons[index].focus()

    def action_open_settings(self) -> None:
        self.app.push_screen("settings")

    def action_open_workflow(self) -> None:
        self.app.push_screen("workflow")

    def action_quit(self) -> None:
        self.app.exit()

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Aide menu principal",
                "1. Options / Cookies : réglage des cookies, du User-Agent et des options runtime.\n"
                "2. URL / Chapitres / Téléchargement : analyse d'une source, sélection des éléments et téléchargement.\n"
                "Q : quitter l'application.",
            )
        )
