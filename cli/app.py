from __future__ import annotations

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header

from .actions import load_state
from .download import CliDownloadController
from .modals import ConfirmModal
from .screens_download import DownloadScreen
from .screens_errors import ErrorsScreen
from .screens_main import MainMenuScreen
from .screens_settings import SettingsScreen
from .screens_workflow import WorkflowScreen


class SushiTerminalApp(App):
    MIN_TERMINAL_WIDTH = 110
    MIN_TERMINAL_HEIGHT = 30
    COMPACT_TERMINAL_WIDTH = 136
    COMPACT_TERMINAL_HEIGHT = 40

    CSS = """
    Screen {
        layout: vertical;
    }

    #app-title {
        text-style: bold;
        padding: 1 2 0 2;
    }

    #app-subtitle, #main-status, #settings-status, #workflow-status {
        padding: 0 2 0 2;
        color: $text-muted;
    }

    #screen-title {
        text-style: bold;
        padding: 0 2 1 2;
    }

    .terminal-warning {
        padding: 0 2;
        color: $warning;
        text-style: bold;
        height: auto;
    }

    #main-menu {
        width: 60;
        padding: 1 2;
    }

    #settings-layout, #workflow-layout {
        padding: 0 1;
        height: 1fr;
    }

    #workflow-layout {
        padding-top: 0;
    }

    #workflow-source-panel,
    #workflow-summary-panel,
    #workflow-actions-row,
    #workflow-status {
        height: auto;
    }

    #workflow-list-panel {
        height: 1fr;
        min-height: 12;
    }

    .panel {
        border: round $panel;
        padding: 0 1;
        margin: 0 1 1 1;
    }

    .panel-title {
        text-style: bold;
        margin-bottom: 0;
    }

    .button-row {
        height: auto;
        margin-bottom: 0;
    }

    #workflow-source-row, #workflow-filter-row {
        height: auto;
    }

    #workflow-url, #workflow-filter {
        width: 1fr;
    }

    .workflow-summary-row {
        height: auto;
        margin-bottom: 0;
    }

    .workflow-summary-row Label,
    #workflow-current,
    #workflow-shortcuts {
        padding: 0 1;
    }

    .workflow-summary-cell {
        width: 1fr;
    }

    #workflow-current {
        height: auto;
        margin-top: 1;
        color: $accent;
        text-style: bold;
    }

    #workflow-shortcuts {
        height: auto;
        color: $text-muted;
    }

    #analyze {
        width: 16;
        min-width: 12;
    }

    #sel-all, #sel-none, #sel-invert, #sel-range {
        width: 14;
        min-width: 10;
    }

    #cookie-list, #workflow-list {
        height: 1fr;
        border: round $boost;
    }

    #workflow-list {
        margin-top: 0;
        min-height: 6;
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

    def on_mount(self) -> None:
        self.install_screen(MainMenuScreen(), name="main")
        self.install_screen(SettingsScreen(), name="settings")
        self.install_screen(WorkflowScreen(), name="workflow")
        self.install_screen(DownloadScreen(), name="download")
        self.install_screen(ErrorsScreen(), name="errors")
        self.push_screen("main")

    def terminal_dimensions(self, screen: Screen | None = None) -> tuple[int, int]:
        target = screen or self.screen
        if target is not None:
            width = getattr(target.size, "width", 0) or 0
            height = getattr(target.size, "height", 0) or 0
            if width > 0 and height > 0:
                return width, height
            container = getattr(target, "container_size", None)
            if container is not None:
                width = getattr(container, "width", 0) or 0
                height = getattr(container, "height", 0) or 0
                if width > 0 and height > 0:
                    return width, height
        return self.size.width, self.size.height

    def terminal_mode(self, screen: Screen | None = None) -> str:
        width, height = self.terminal_dimensions(screen)
        if width < self.MIN_TERMINAL_WIDTH or height < self.MIN_TERMINAL_HEIGHT:
            return "too_small"
        if width < self.COMPACT_TERMINAL_WIDTH or height < self.COMPACT_TERMINAL_HEIGHT:
            return "compact"
        return "normal"

    def terminal_warning_message(self, screen: Screen | None = None) -> str:
        width, height = self.terminal_dimensions(screen)
        mode = self.terminal_mode(screen)
        if mode == "too_small":
            return (
                f"Terminal trop petit ({width}x{height}). "
                f"Taille recommandée : {self.COMPACT_TERMINAL_WIDTH}x{self.COMPACT_TERMINAL_HEIGHT}+."
            )
        if mode == "compact":
            return f"Mode compact actif ({width}x{height}). Certains blocs sont simplifiés."
        return ""

    def action_request_quit(self) -> None:
        controller = self.download_controller
        if controller and controller.snapshot().active:
            def confirm_active(result: str | None) -> None:
                if result == "confirm":
                    controller.cancel()
                    self.exit()

            self.push_screen(
                ConfirmModal(
                    "Téléchargement en cours",
                    "Un téléchargement est actif. Confirmer l'annulation et quitter ?",
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
