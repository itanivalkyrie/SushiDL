from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, ProgressBar, Static

from .modals import ConfirmModal, HelpModal, MessageModal


class DownloadScreen(Screen):
    BINDINGS = [
        ("a", "cancel_download", "Annuler"),
        ("e", "show_errors", "Erreurs"),
        ("escape", "go_back", "Retour"),
        ("h", "show_help", "Aide"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Téléchargement en cours", id="screen-title")
        yield Label("", id="download-warning", classes="terminal-warning")
        with Vertical(id="download-layout"):
            with Vertical(classes="panel", id="download-main-panel"):
                yield Label("Titre : --", id="dl-title")
                yield Label("Sélection : --", id="dl-selection")
                yield Label("Sortie : --", id="dl-output")
                yield Label("En cours : --", id="dl-current")
                yield Label("Images : --/--", id="dl-images")
                yield Label("ETA chap. : --:--", id="dl-eta-volume")
                yield Label("ETA global : --:--", id="dl-eta-global")
                yield Label("Durée : --:--", id="dl-elapsed")
                yield ProgressBar(total=100, id="dl-progress")
            with Vertical(classes="panel", id="download-summary-panel"):
                yield Label("Résumé", classes="panel-title", id="download-summary-title")
                yield Label("Etat : --", id="dl-state")
                yield Label("Succès : 0/0", id="dl-success")
                yield Label("Erreurs : 0", id="dl-errors")
            with Vertical(classes="panel", id="download-logs-panel"):
                yield Label("Journal récent", classes="panel-title", id="download-logs-title")
                yield Static("", id="dl-logs")
            with Horizontal(classes="button-row", id="download-actions-row"):
                yield Button("Annuler", id="cancel-download", variant="error")
                yield Button("Voir erreurs", id="show-errors")
                yield Button("Retour", id="back")
        yield Label("", id="download-status")

    def on_mount(self) -> None:
        self._completion_announced = False
        self.set_interval(0.2, self.refresh_from_controller)
        self.refresh_from_controller()

    def on_show(self) -> None:
        self.refresh_from_controller()
        controller = getattr(self.app, "download_controller", None)
        status = controller.snapshot() if controller else self.app.cli_state.download_status
        target = "#cancel-download" if status.active else "#back"
        self.query_one(target, Button).focus()

    def on_resize(self, _event=None) -> None:
        self.apply_terminal_mode()

    def refresh_from_controller(self) -> None:
        controller = getattr(self.app, "download_controller", None)
        status = controller.snapshot() if controller else self.app.cli_state.download_status
        self.query_one("#dl-title", Label).update(f"Titre : {self.app.cli_state.current_title or '--'}")
        self.query_one("#dl-selection", Label).update(
            f"Sélection : {status.completed_volumes}/{max(0, status.total_volumes)} terminés"
        )
        self.query_one("#dl-output", Label).update(f"Sortie : {status.output_dir or '--'}")
        self.query_one("#dl-current", Label).update(f"En cours : {status.current_volume or '--'}")
        self.query_one("#dl-images", Label).update(f"Images : {status.current_images_done}/{status.current_images_total}")
        self.query_one("#dl-eta-volume", Label).update(f"ETA chap. : {status.eta_volume}")
        self.query_one("#dl-eta-global", Label).update(f"ETA global : {status.eta_global}")
        self.query_one("#dl-elapsed", Label).update(f"Durée : {status.elapsed}")
        self.query_one("#dl-progress", ProgressBar).update(progress=max(0.0, min(100.0, status.global_percent)))
        self.query_one("#dl-logs", Static).update("\n".join(status.logs[-8:]) if status.logs else "Aucun evenement.")
        self.query_one("#download-status", Label).update(status.status_message)
        state_label = "En cours"
        if status.finished:
            if status.cancelled:
                state_label = "Annule"
            elif status.errors:
                state_label = "Termine avec erreurs"
            else:
                state_label = "Termine"
        self.query_one("#dl-state", Label).update(f"Etat : {state_label}")
        self.query_one("#dl-success", Label).update(
            f"Succès : {status.completed_volumes}/{max(0, status.total_volumes)}"
        )
        self.query_one("#dl-errors", Label).update(f"Erreurs : {len(status.errors)}")
        cancel_button = self.query_one("#cancel-download", Button)
        cancel_button.disabled = not status.active
        back_button = self.query_one("#back", Button)
        back_button.label = "Terminer" if status.finished else "Retour"
        self.apply_terminal_mode()

        if status.active:
            self._completion_announced = False
        elif status.finished and not self._completion_announced:
            self._completion_announced = True
            summary = (
                f"Etat : {state_label}\n"
                f"Succès : {status.completed_volumes}/{max(0, status.total_volumes)}\n"
                f"Erreurs : {len(status.errors)}\n"
                f"Durée : {status.elapsed}\n"
                f"Sortie : {status.output_dir or '--'}"
            )
            self.app.push_screen(MessageModal("Fin de téléchargement", summary))

    def apply_terminal_mode(self) -> None:
        mode = self.app.terminal_mode(self)
        warning = self.app.terminal_warning_message(self)
        warning_label = self.query_one("#download-warning", Label)
        warning_label.update(warning)

        compact = mode in {"compact", "too_small"}
        self.query_one("#show-errors", Button).label = "Erreurs" if compact else "Voir erreurs"
        self.query_one("#back", Button).label = "Retour" if not compact else "Fermer"
        self.query_one("#download-summary-title", Label).styles.display = "none" if compact else "block"
        self.query_one("#download-logs-title", Label).styles.display = "none" if compact else "block"
        self.query_one("#download-summary-panel", Vertical).styles.margin = (0, 1, 0, 1) if compact else (0, 1, 1, 1)
        self.query_one("#download-main-panel", Vertical).styles.margin = (0, 1, 0, 1) if compact else (0, 1, 1, 1)
        logs_widget = self.query_one("#dl-logs", Static)
        logs_widget.styles.display = "none" if compact else "block"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-download":
            self.action_cancel_download()
        elif event.button.id == "show-errors":
            self.action_show_errors()
        elif event.button.id == "back":
            self.action_go_back()

    def action_cancel_download(self) -> None:
        controller = getattr(self.app, "download_controller", None)
        if controller:
            def confirm(result: str | None) -> None:
                if result == "confirm":
                    controller.cancel()
                    self.refresh_from_controller()

            self.app.push_screen(
                ConfirmModal(
                    "Annuler le téléchargement",
                    "Le téléchargement en cours va être interrompu.",
                    confirm_label="Oui, annuler",
                ),
                confirm,
            )

    def action_show_errors(self) -> None:
        self.app.push_screen("errors")

    def action_go_back(self) -> None:
        controller = getattr(self.app, "download_controller", None)
        if controller and controller.snapshot().active:
            return
        self.app.pop_screen()

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Aide téléchargement",
                "A annule le job en cours.\n"
                "E ouvre le tableau des erreurs.\n"
                "Le retour est bloqué tant que le téléchargement est actif.",
            )
        )
