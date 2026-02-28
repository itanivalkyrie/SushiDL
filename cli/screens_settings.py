from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, ListItem, ListView, Static

from .actions import save_state, test_all_cookies, test_cookie_for_domain
from .modals import HelpModal, TextPromptModal


class SettingsScreen(Screen):
    BINDINGS = [
        ("escape", "go_back", "Retour"),
        ("s", "save", "Sauvegarder"),
        ("t", "test_selected", "Tester"),
        ("shift+t", "test_all", "Tester tout"),
        ("e", "edit_selected", "Editer"),
        ("h", "show_help", "Aide"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Options / Cookies", id="screen-title")
        with Horizontal(id="settings-layout"):
            with Vertical(id="cookies-panel", classes="panel"):
                yield Label("Cookies", classes="panel-title")
                self.cookie_list = ListView(id="cookie-list")
                yield self.cookie_list
                with Horizontal(classes="button-row"):
                    yield Button("Editer", id="edit-cookie", variant="primary")
                    yield Button("Tester", id="test-cookie")
                    yield Button("Tester tout", id="test-all")
            with Vertical(id="ua-panel", classes="panel"):
                yield Label("User-Agent", classes="panel-title")
                yield Input(value="", id="user-agent")
                with Horizontal(classes="button-row"):
                    yield Button("Modifier", id="edit-ua")
                    yield Button("Vider", id="clear-ua")
            with Vertical(id="options-panel", classes="panel"):
                yield Label("Options", classes="panel-title")
                yield Checkbox("CBZ", value=True, id="opt-cbz")
                yield Checkbox("WEBP -> JPG", value=True, id="opt-webp")
                yield Checkbox("Reprise intelligente", value=True, id="opt-resume")
                yield Checkbox("Logs detailles", value=True, id="opt-logs")
                with Horizontal(classes="button-row"):
                    yield Button("Sauvegarder", id="save-settings", variant="success")
                    yield Button("Retour", id="back")
        yield Label("", id="settings-status")

    def on_mount(self) -> None:
        self.refresh_from_state()
        self.query_one("#cookie-list", ListView).focus()

    def on_show(self) -> None:
        self.refresh_from_state()

    def refresh_from_state(self) -> None:
        state = self.app.cli_state
        self.cookie_list.clear()
        for domain in ("fr", "net", "origines", "hentai"):
            cookie_value = (state.cookies.get(domain) or "").strip()
            status = state.cookie_status.get(domain, "VIDE")
            label = f".{domain:<16} [{status}] {'present' if cookie_value else 'vide'}"
            self.cookie_list.append(ListItem(Label(label), id=f"cookie-{domain}"))
        self.query_one("#user-agent", Input).value = state.user_agent
        self.query_one("#opt-cbz", Checkbox).value = bool(state.cbz_enabled)
        self.query_one("#opt-webp", Checkbox).value = bool(state.webp2jpg_enabled)
        self.query_one("#opt-resume", Checkbox).value = bool(state.smart_resume_enabled)
        self.query_one("#opt-logs", Checkbox).value = bool(state.verbose_logs)
        self.query_one("#settings-status", Label).update(state.status_message)

    def _selected_domain(self) -> str | None:
        highlighted = self.cookie_list.highlighted_child
        if highlighted is None or not highlighted.id or not highlighted.id.startswith("cookie-"):
            return None
        return highlighted.id.split("-", 1)[1]

    def _sync_state_from_widgets(self) -> None:
        state = self.app.cli_state
        state.user_agent = self.query_one("#user-agent", Input).value.strip()
        state.cbz_enabled = bool(self.query_one("#opt-cbz", Checkbox).value)
        state.webp2jpg_enabled = bool(self.query_one("#opt-webp", Checkbox).value)
        state.smart_resume_enabled = bool(self.query_one("#opt-resume", Checkbox).value)
        state.verbose_logs = bool(self.query_one("#opt-logs", Checkbox).value)
        state.unsaved_changes = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-cookie":
            self.action_edit_selected()
        elif event.button.id == "test-cookie":
            self.action_test_selected()
        elif event.button.id == "test-all":
            self.action_test_all()
        elif event.button.id == "edit-ua":
            self._sync_state_from_widgets()
            self.query_one("#user-agent", Input).focus()
        elif event.button.id == "clear-ua":
            self.query_one("#user-agent", Input).value = ""
            self._sync_state_from_widgets()
            self.refresh_from_state()
        elif event.button.id == "save-settings":
            self.action_save()
        elif event.button.id == "back":
            self.action_go_back()

    def on_checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._sync_state_from_widgets()
        self.refresh_from_state()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "user-agent":
            self._sync_state_from_widgets()

    def action_save(self) -> None:
        self._sync_state_from_widgets()
        save_state(self.app.backend, self.app.cli_state)
        self.refresh_from_state()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_test_selected(self) -> None:
        self._sync_state_from_widgets()
        domain = self._selected_domain()
        if not domain:
            self.app.cli_state.status_message = "Aucun domaine selectionne."
        else:
            test_cookie_for_domain(self.app.backend, self.app.cli_state, domain)
        self.refresh_from_state()

    def action_test_all(self) -> None:
        self._sync_state_from_widgets()
        test_all_cookies(self.app.backend, self.app.cli_state)
        self.refresh_from_state()

    def action_edit_selected(self) -> None:
        domain = self._selected_domain()
        if not domain:
            self.app.cli_state.status_message = "Aucun domaine selectionne."
            self.refresh_from_state()
            return
        current = self.app.cli_state.cookies.get(domain, "")

        def apply_value(value: str | None) -> None:
            if value is None:
                return
            self.app.cli_state.cookies[domain] = value.strip()
            self.app.cli_state.cookie_status[domain] = "PRESENT" if value.strip() else "VIDE"
            self.app.cli_state.unsaved_changes = True
            self.app.cli_state.status_message = f"Cookie .{domain} mis a jour."
            self.refresh_from_state()

        self.app.push_screen(TextPromptModal(f"Editer cookie .{domain}", value=current, password=True), apply_value)

    def action_show_help(self) -> None:
        self.app.push_screen(
            HelpModal(
                "Aide options / cookies",
                "Selectionne un domaine puis utilise E pour editer ou T pour tester.\n"
                "Le User-Agent doit correspondre au navigateur qui a obtenu le cookie.\n"
                "S sauvegarde les parametres. Shift+T teste tous les domaines.",
            )
        )
