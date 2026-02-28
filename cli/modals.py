from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class TextPromptModal(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Annuler")]

    def __init__(self, title: str, value: str = "", password: bool = False, placeholder: str = ""):
        super().__init__()
        self.prompt_title = title
        self.value = value
        self.password = password
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Container(id="prompt-overlay"):
            with Vertical(id="prompt-dialog"):
                yield Static(self.prompt_title, classes="prompt-title")
                yield Input(value=self.value, password=self.password, placeholder=self.placeholder, id="prompt-input")
                with Container(id="prompt-actions"):
                    yield Button("Valider", variant="primary", id="confirm")
                    yield Button("Annuler", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(self.query_one("#prompt-input", Input).value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.dismiss(self.query_one("#prompt-input", Input).value)


class MessageModal(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss(None)", "Fermer")]

    def __init__(self, title: str, message: str):
        super().__init__()
        self.message_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="prompt-overlay"):
            with Vertical(id="prompt-dialog"):
                yield Label(self.message_title, classes="prompt-title")
                yield Static(self.message, id="prompt-message")
                yield Button("Fermer", variant="primary", id="close")

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

