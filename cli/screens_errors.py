from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Label, Static

from .modals import HelpModal, MessageModal, TextPromptModal


class ErrorsScreen(Screen):
    BINDINGS = [
        ("c", "copy_errors", "Copier"),
        ("x", "export_errors", "Exporter"),
        ("escape", "go_back", "Retour"),
        ("h", "show_help", "Aide"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Erreurs", id="screen-title")
        yield Label("", id="errors-warning", classes="terminal-warning")
        self.table = DataTable(id="errors-table")
        yield self.table
        yield Label("", id="errors-status")
        with Horizontal(classes="button-row"):
            yield Button("Copier", id="copy")
            yield Button("Exporter", id="export")
            yield Button("Retour", id="back")

    def on_mount(self) -> None:
        self.table.add_columns("Tome/Chapitre", "Etape", "Code", "Raison", "Action")
        self.refresh_from_controller()
        self.apply_terminal_mode()

    def on_show(self) -> None:
        self.refresh_from_controller()
        self.apply_terminal_mode()
        self.query_one("#back", Button).focus()

    def on_resize(self, _event=None) -> None:
        self.apply_terminal_mode()

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

    def apply_terminal_mode(self) -> None:
        self.query_one("#errors-warning", Label).update(self.app.terminal_warning_message(self))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy":
            self.action_copy_errors()
        elif event.button.id == "export":
            self.action_export_errors()
        elif event.button.id == "back":
            self.action_go_back()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def _format_errors_text(self) -> str:
        controller = getattr(self.app, "download_controller", None)
        status = controller.snapshot() if controller else self.app.cli_state.download_status
        if not status.errors:
            return "Aucune erreur."
        lines = []
        for idx, err in enumerate(status.errors, start=1):
            code = "" if err.status_code is None else str(err.status_code)
            lines.append(
                f"{idx}. Tome/Chapitre: {err.tome}\n"
                f"   Etape: {err.stage}\n"
                f"   Code: {code or '-'}\n"
                f"   Raison: {err.reason}\n"
                f"   Action: {err.action or '-'}"
            )
        return "\n\n".join(lines)

    def action_copy_errors(self) -> None:
        text = self._format_errors_text()
        if text == "Aucune erreur.":
            self.query_one("#errors-status", Label).update("Aucune erreur a copier.")
            return
        try:
            self.app.copy_to_clipboard(text)
        except Exception as exc:
            self.query_one("#errors-status", Label).update(f"Copie impossible : {exc}")
            self.app.push_screen(MessageModal("Copie impossible", str(exc)))
            return
        self.query_one("#errors-status", Label).update("Erreurs copiees dans le presse-papiers.")

    def action_export_errors(self) -> None:
        text = self._format_errors_text()
        if text == "Aucune erreur.":
            self.query_one("#errors-status", Label).update("Aucune erreur a exporter.")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = str(Path.cwd() / f"sushidl_errors_{timestamp}.txt")

        def export_to_path(value: str | None) -> None:
            if value is None:
                return
            target = Path((value or "").strip() or default_path)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text + "\n", encoding="utf-8")
            except Exception as exc:
                self.query_one("#errors-status", Label).update(f"Export impossible : {exc}")
                self.app.push_screen(MessageModal("Export impossible", str(exc)))
                return
            self.query_one("#errors-status", Label).update(f"Erreurs exportees vers {target}")
            self.app.push_screen(MessageModal("Export termine", f"Fichier ecrit :\n{target}"))

        self.app.push_screen(TextPromptModal("Exporter les erreurs", value=default_path), export_to_path)

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Aide erreurs",
                "Cette vue affiche les erreurs remontées par le téléchargement terminal :\n"
                "tome/chapitre, étape, code HTTP, raison et action recommandée.\n"
                "C copie toutes les erreurs. X exporte un fichier texte.",
            )
        )
