from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, ProgressBar, Static


class DownloadScreen(Screen):
    BINDINGS = [
        ("a", "cancel_download", "Annuler"),
        ("e", "show_errors", "Erreurs"),
        ("escape", "go_back", "Retour"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Telechargement en cours", id="screen-title")
        with Vertical(id="download-layout"):
            with Vertical(classes="panel"):
                yield Label("Titre : --", id="dl-title")
                yield Label("Selection : --", id="dl-selection")
                yield Label("Sortie : --", id="dl-output")
            with Vertical(classes="panel"):
                yield Label("En cours : --", id="dl-current")
                yield Label("Images : --/--", id="dl-images")
                yield Label("ETA chap. : --:--", id="dl-eta-volume")
                yield Label("ETA global : --:--", id="dl-eta-global")
                yield ProgressBar(total=100, id="dl-progress")
            with Vertical(classes="panel"):
                yield Label("Journal recent", classes="panel-title")
                yield Static("", id="dl-logs")
            with Horizontal(classes="button-row"):
                yield Button("Annuler", id="cancel-download", variant="error")
                yield Button("Voir erreurs", id="show-errors")
                yield Button("Retour", id="back")
        yield Label("", id="download-status")

    def on_mount(self) -> None:
        self.set_interval(0.2, self.refresh_from_controller)
        self.refresh_from_controller()

    def on_show(self) -> None:
        self.refresh_from_controller()

    def refresh_from_controller(self) -> None:
        controller = getattr(self.app, "download_controller", None)
        status = controller.snapshot() if controller else self.app.cli_state.download_status
        self.query_one("#dl-title", Label).update(f"Titre : {self.app.cli_state.current_title or '--'}")
        self.query_one("#dl-selection", Label).update(
            f"Selection : {status.completed_volumes}/{max(0, status.total_volumes)} termines"
        )
        self.query_one("#dl-output", Label).update(f"Sortie : {status.output_dir or '--'}")
        self.query_one("#dl-current", Label).update(f"En cours : {status.current_volume or '--'}")
        self.query_one("#dl-images", Label).update(f"Images : {status.current_images_done}/{status.current_images_total}")
        self.query_one("#dl-eta-volume", Label).update(f"ETA chap. : {status.eta_volume}")
        self.query_one("#dl-eta-global", Label).update(f"ETA global : {status.eta_global}")
        self.query_one("#dl-progress", ProgressBar).update(progress=max(0.0, min(100.0, status.global_percent)))
        self.query_one("#dl-logs", Static).update("\n".join(status.logs[-8:]) if status.logs else "Aucun evenement.")
        self.query_one("#download-status", Label).update(status.status_message)
        cancel_button = self.query_one("#cancel-download", Button)
        cancel_button.disabled = not status.active

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
            controller.cancel()
            self.refresh_from_controller()

    def action_show_errors(self) -> None:
        self.app.push_screen("errors")

    def action_go_back(self) -> None:
        controller = getattr(self.app, "download_controller", None)
        if controller and controller.snapshot().active:
            return
        self.app.pop_screen()
