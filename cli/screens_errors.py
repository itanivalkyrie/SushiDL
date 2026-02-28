from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, DataTable, Label, Static

from .modals import HelpModal


class ErrorsScreen(Screen):
    BINDINGS = [("escape", "go_back", "Retour"), ("h", "show_help", "Aide")]

    def compose(self) -> ComposeResult:
        yield Static("Erreurs", id="screen-title")
        self.table = DataTable(id="errors-table")
        yield self.table
        yield Label("", id="errors-status")
        yield Button("Retour", id="back")

    def on_mount(self) -> None:
        self.table.add_columns("Tome/Chapitre", "Etape", "Code", "Raison", "Action")
        self.refresh_from_controller()

    def on_show(self) -> None:
        self.refresh_from_controller()

    def refresh_from_controller(self) -> None:
        self.table.clear(columns=False)
        controller = getattr(self.app, "download_controller", None)
        status = controller.snapshot() if controller else self.app.cli_state.download_status
        if not status.errors:
            self.query_one("#errors-status", Label).update("Aucune erreur pour le moment.")
            return
        for err in status.errors:
            self.table.add_row(
                err.tome,
                err.stage,
                "" if err.status_code is None else str(err.status_code),
                err.reason,
                err.action,
            )
        self.query_one("#errors-status", Label).update(f"{len(status.errors)} erreur(s).")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_go_back()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Aide erreurs",
                "Cette vue affiche les erreurs remontees par le telechargement terminal :\n"
                "tome/chapitre, etape, code HTTP, raison et action recommandee.",
            )
        )
