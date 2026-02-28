from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Static


class MainMenuScreen(Screen):
    BINDINGS = [("q", "quit", "Quitter")]

    def compose(self) -> ComposeResult:
        yield Static("SushiDL CLI", id="app-title")
        yield Static("Mode terminal interactif", id="app-subtitle")
        with Vertical(id="main-menu"):
            yield Button("1. Options / Cookies", id="open-settings", variant="primary")
            yield Button("2. URL / Chapitres / Telechargement", id="open-workflow")
            yield Button("3. Quitter", id="quit-app")
        yield Label("", id="main-status")

    def on_mount(self) -> None:
        self.query_one("#open-settings", Button).focus()
        self.refresh_status()

    def refresh_status(self) -> None:
        app_state = self.app.cli_state
        auth_ready = sum(1 for value in app_state.cookies.values() if (value or "").strip())
        cbz = "ON" if app_state.cbz_enabled else "OFF"
        resume = "ON" if app_state.smart_resume_enabled else "OFF"
        self.query_one("#main-status", Label).update(
            f"Etat rapide : Auth {auth_ready}/4 | User-Agent {'present' if app_state.user_agent else 'vide'} | CBZ {cbz} | Resume {resume}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-settings":
            self.app.push_screen("settings")
        elif event.button.id == "open-workflow":
            self.app.push_screen("workflow")
        elif event.button.id == "quit-app":
            self.app.exit()

    def action_quit(self) -> None:
        self.app.exit()

