from __future__ import annotations

from textual.app import App
from textual.binding import Binding
from textual.widgets import Footer, Header

from .actions import load_state
from .download import CliDownloadController
from .modals import ConfirmModal
from .screens_download import DownloadScreen
from .screens_errors import ErrorsScreen
from .screens_main import MainMenuScreen
from .screens_settings import SettingsScreen
from .screens_workflow import WorkflowScreen


class SushiTerminalApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #app-title {
        text-style: bold;
        padding: 1 2 0 2;
    }

    #app-subtitle, #main-status, #settings-status, #workflow-status {
        padding: 0 2 1 2;
        color: $text-muted;
    }

    #screen-title {
        text-style: bold;
        padding: 1 2;
    }

    #main-menu {
        width: 60;
        padding: 1 2;
    }

    #settings-layout, #workflow-layout {
        padding: 0 1 1 1;
        height: 1fr;
    }

    .panel {
        border: round $panel;
        padding: 1;
        margin: 0 1 1 1;
    }

    .panel-title {
        text-style: bold;
        margin-bottom: 1;
    }

    .button-row {
        height: auto;
        margin-bottom: 1;
    }

    #cookie-list, #workflow-list {
        height: 1fr;
        border: round $boost;
    }

    #prompt-overlay {
        align: center middle;
        background: $background 60%;
    }

    #prompt-dialog {
        width: 72;
        max-width: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    .prompt-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #prompt-input {
        margin-bottom: 1;
    }

    #prompt-actions {
        height: auto;
    }

    #prompt-message {
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("q", "request_quit", "Quitter", show=True),
    ]

    def __init__(self, backend) -> None:
        super().__init__()
        self.backend = backend
        self.cli_state = load_state(backend)
        self.download_controller: CliDownloadController | None = None

    def compose(self):
        yield Header(show_clock=False)
        yield Footer()

    def on_mount(self) -> None:
        self.install_screen(MainMenuScreen(), name="main")
        self.install_screen(SettingsScreen(), name="settings")
        self.install_screen(WorkflowScreen(), name="workflow")
        self.install_screen(DownloadScreen(), name="download")
        self.install_screen(ErrorsScreen(), name="errors")
        self.push_screen("main")

    def action_request_quit(self) -> None:
        controller = self.download_controller
        if controller and controller.snapshot().active:
            def confirm_active(result: str | None) -> None:
                if result == "confirm":
                    controller.cancel()
                    self.exit()

            self.push_screen(
                ConfirmModal(
                    "Telechargement en cours",
                    "Un telechargement est actif. Confirmer l'annulation et quitter ?",
                    confirm_label="Annuler et quitter",
                ),
                confirm_active,
            )
            return

        if self.cli_state.unsaved_changes:
            def confirm_unsaved(result: str | None) -> None:
                if result == "confirm":
                    self.backend.save_settings(self.cli_state)
                    self.cli_state.unsaved_changes = False
                    self.exit()

            self.push_screen(
                ConfirmModal(
                    "Quitter",
                    "Des changements non sauvegardes existent. Sauvegarder puis quitter ?",
                    confirm_label="Sauvegarder et quitter",
                ),
                confirm_unsaved,
            )
            return
        self.exit()


def run_cli_app(backend) -> None:
    app = SushiTerminalApp(backend)
    app.run()
