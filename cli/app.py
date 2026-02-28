from __future__ import annotations

from textual.app import App
from textual.binding import Binding
from textual.widgets import Footer, Header

from .actions import load_state
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

    def compose(self):
        yield Header(show_clock=False)
        yield Footer()

    def on_mount(self) -> None:
        self.install_screen(MainMenuScreen(), name="main")
        self.install_screen(SettingsScreen(), name="settings")
        self.install_screen(WorkflowScreen(), name="workflow")
        self.push_screen("main")

    def action_request_quit(self) -> None:
        self.exit()


def run_cli_app(backend) -> None:
    app = SushiTerminalApp(backend)
    app.run()
