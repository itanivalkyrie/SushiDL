from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, ListItem, ListView, Static

from .actions import save_state, test_all_cookies, test_cookie_for_domain
from .modals import HelpModal, TextPromptModal


class SettingsScreen(Screen):
    BINDINGS = [
        ("up,k", "nav_up", "Monter"),
        ("down,j", "nav_down", "Descendre"),
        ("left", "focus_prev_zone", "Zone prec."),
        ("right", "focus_next_zone", "Zone suiv."),
        ("c", "focus_cookies", "Cookies"),
        ("u", "focus_user_agent", "User-Agent"),
        ("o", "focus_options", "Options"),
        ("escape", "go_back", "Retour"),
        ("s", "save", "Sauvegarder"),
        ("t", "test_selected", "Tester"),
        ("shift+t", "test_all", "Tester tout"),
        ("e", "edit_selected", "Editer"),
        ("h", "show_help", "Aide"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Options / Cookies", id="screen-title")
        yield Label("", id="settings-warning", classes="terminal-warning")
        with Horizontal(id="settings-layout"):
            with Vertical(id="cookies-panel", classes="panel"):
                yield Label("Cookies", classes="panel-title")
                self.cookie_list = ListView(id="cookie-list")
                yield self.cookie_list
                with Horizontal(classes="button-row", id="cookie-actions-row"):
                    yield Button("Editer", id="edit-cookie", variant="primary")
                    yield Button("Tester", id="test-cookie")
                with Horizontal(classes="button-row", id="cookie-actions-row-secondary"):
                    yield Button("Tester tout", id="test-all")
            with Vertical(id="ua-panel", classes="panel"):
                yield Label("User-Agent", classes="panel-title")
                yield Input(value="", id="user-agent")
                with Horizontal(classes="button-row"):
                    yield Button("Editer", id="edit-ua")
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
        self._refreshing = False
        self.refresh_from_state()
        self.query_one("#cookie-list", ListView).focus()

    def on_show(self) -> None:
        self.refresh_from_state()
        self.query_one("#cookie-list", ListView).focus()

    def on_resize(self, _event=None) -> None:
        self.apply_terminal_mode()

    def refresh_from_state(self) -> None:
        self._refreshing = True
        try:
            state = self.app.cli_state
            previous_index = getattr(self.cookie_list, "index", None)
            self.cookie_list.clear()
            self._cookie_domains = ["fr", "net", "origines", "hentai", "toonfr", "ortega", "hentaizone"]
            domain_labels = {
                "fr": ".fr",
                "net": ".net",
                "origines": ".origines",
                "hentai": ".hentai-origines",
                "toonfr": ".toonfr",
                "ortega": ".ortegascans",
                "hentaizone": ".hentaizone",
            }
            status_labels = {
                "PRESENT": "RENSEIGNE",
                "VIDE": "VIDE",
                "VALIDE": "VALIDE",
                "A_VERIFIER": "A VERIFIER",
                "INCONNU": "INCONNU",
            }
            for domain in self._cookie_domains:
                cookie_value = (state.cookies.get(domain) or "").strip()
                status = status_labels.get(state.cookie_status.get(domain, "VIDE"), state.cookie_status.get(domain, "VIDE"))
                label = f"{domain_labels.get(domain, '.' + domain):<24} [{status}] {'renseigne' if cookie_value else 'vide'}"
                self.cookie_list.append(ListItem(Label(label)))
            if self._cookie_domains:
                safe_index = 0 if previous_index is None else max(0, min(int(previous_index), len(self._cookie_domains) - 1))
                self.cookie_list.index = safe_index
            self.query_one("#user-agent", Input).value = state.user_agent
            self.query_one("#opt-cbz", Checkbox).value = bool(state.cbz_enabled)
            self.query_one("#opt-webp", Checkbox).value = bool(state.webp2jpg_enabled)
            self.query_one("#opt-resume", Checkbox).value = bool(state.smart_resume_enabled)
            self.query_one("#opt-logs", Checkbox).value = bool(state.verbose_logs)
            self.query_one("#settings-status", Label).update(state.status_message)
            self.apply_terminal_mode()
        finally:
            self._refreshing = False

    def apply_terminal_mode(self) -> None:
        mode = self.app.terminal_mode(self)
        warning = self.app.terminal_warning_message(self)
        warning_label = self.query_one("#settings-warning", Label)
        warning_label.update(warning)

        compact = mode in {"compact", "too_small"}
        layout = self.query_one("#settings-layout", Horizontal)
        layout.styles.layout = "vertical" if compact else "horizontal"
        for panel_id in ("#cookies-panel", "#ua-panel", "#options-panel"):
            panel = self.query_one(panel_id, Vertical)
            panel.styles.margin = (0, 1, 0, 1) if compact else (0, 1, 1, 1)
            panel.styles.padding = (0, 1)
        self.query_one("#edit-cookie", Button).label = "Edit." if compact else "Editer"
        self.query_one("#test-cookie", Button).label = "Tester"
        self.query_one("#test-all", Button).label = "Tout" if compact else "Tester tout"
        self.query_one("#edit-ua", Button).label = "UA" if compact else "Editer"
        self.query_one("#save-settings", Button).label = "Sauver" if compact else "Sauvegarder"

    def _selected_domain(self) -> str | None:
        current_index = getattr(self.cookie_list, "index", None)
        if current_index is None:
            return None
        if current_index < 0 or current_index >= len(getattr(self, "_cookie_domains", [])):
            return None
        return self._cookie_domains[current_index]

    def _focus_order(self):
        return [
            self.cookie_list,
            self.query_one("#edit-cookie", Button),
            self.query_one("#test-cookie", Button),
            self.query_one("#test-all", Button),
            self.query_one("#user-agent", Input),
            self.query_one("#edit-ua", Button),
            self.query_one("#clear-ua", Button),
            self.query_one("#opt-cbz", Checkbox),
            self.query_one("#opt-webp", Checkbox),
            self.query_one("#opt-resume", Checkbox),
            self.query_one("#opt-logs", Checkbox),
            self.query_one("#save-settings", Button),
            self.query_one("#back", Button),
        ]

    def _focused_index(self) -> int:
        focused = self.app.focused
        for idx, widget in enumerate(self._focus_order()):
            if focused is widget:
                return idx
        return 0

    def _move_cookie_selection(self, delta: int) -> bool:
        if self.app.focused is not self.cookie_list:
            return False
        if not getattr(self, "_cookie_domains", None):
            return False
        current = getattr(self.cookie_list, "index", 0) or 0
        current = max(0, min(int(current) + delta, len(self._cookie_domains) - 1))
        self.cookie_list.index = current
        return True

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
        if self._refreshing:
            return
        self._sync_state_from_widgets()
        self.refresh_from_state()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._refreshing:
            return
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
                "Sélectionne un domaine puis utilise E pour éditer ou T pour tester.\n"
                "Le User-Agent doit correspondre au navigateur qui a obtenu le cookie.\n"
                "S sauvegarde les parametres. Shift+T teste tous les domaines.\n"
                "Fleches/J/K naviguent. C/U/O changent de zone.",
            )
        )

    def action_nav_up(self) -> None:
        if self._move_cookie_selection(-1):
            return
        focusables = self._focus_order()
        focusables[max(0, self._focused_index() - 1)].focus()

    def action_nav_down(self) -> None:
        if self._move_cookie_selection(1):
            return
        focusables = self._focus_order()
        focusables[min(len(focusables) - 1, self._focused_index() + 1)].focus()

    def action_focus_prev_zone(self) -> None:
        focused = self.app.focused
        if focused in {
            self.query_one("#user-agent", Input),
            self.query_one("#edit-ua", Button),
            self.query_one("#clear-ua", Button),
        }:
            self.cookie_list.focus()
        else:
            self.query_one("#user-agent", Input).focus()

    def action_focus_next_zone(self) -> None:
        focused = self.app.focused
        option_focus = self.query_one("#opt-cbz", Checkbox)
        if focused is self.cookie_list or focused in {
            self.query_one("#edit-cookie", Button),
            self.query_one("#test-cookie", Button),
            self.query_one("#test-all", Button),
        }:
            self.query_one("#user-agent", Input).focus()
        elif focused in {
            self.query_one("#user-agent", Input),
            self.query_one("#edit-ua", Button),
            self.query_one("#clear-ua", Button),
        }:
            option_focus.focus()
        else:
            self.cookie_list.focus()

    def action_focus_cookies(self) -> None:
        self.cookie_list.focus()

    def action_focus_user_agent(self) -> None:
        self.query_one("#user-agent", Input).focus()

    def action_focus_options(self) -> None:
        self.query_one("#opt-cbz", Checkbox).focus()
