from __future__ import annotations

from typing import Protocol

from .state import CliItem, CliState


class CliBackend(Protocol):
    def load_settings(self) -> CliState: ...
    def save_settings(self, state: CliState) -> None: ...
    def test_cookie(self, domain: str, cookie: str, ua: str) -> bool | None: ...
    def analyze_url(self, url: str, cookies: dict[str, str], ua: str) -> tuple[str, str, list[tuple[str, str]]]: ...


def load_state(backend: CliBackend) -> CliState:
    state = backend.load_settings()
    if not state.cookie_status:
        state.cookie_status = {domain: ("PRESENT" if (state.cookies.get(domain) or "").strip() else "VIDE") for domain in state.cookies}
    return state


def save_state(backend: CliBackend, state: CliState) -> None:
    backend.save_settings(state)
    state.unsaved_changes = False
    state.status_message = "Parametres sauvegardes."


def test_cookie_for_domain(backend: CliBackend, state: CliState, domain: str) -> str:
    cookie = (state.cookies.get(domain) or "").strip()
    if not cookie:
        state.cookie_status[domain] = "VIDE"
        state.status_message = f"Cookie .{domain} vide."
        return "VIDE"
    result = backend.test_cookie(domain, cookie, state.user_agent)
    if result is True:
        state.cookie_status[domain] = "VALIDE"
        state.status_message = f"Cookie .{domain} valide."
    elif result is False:
        state.cookie_status[domain] = "A_VERIFIER"
        state.status_message = f"Cookie .{domain} a verifier."
    else:
        state.cookie_status[domain] = "INCONNU"
        state.status_message = f"Test cookie .{domain} non concluant."
    return state.cookie_status[domain]


def test_all_cookies(backend: CliBackend, state: CliState) -> None:
    for domain in list(state.cookies):
        test_cookie_for_domain(backend, state, domain)
    state.status_message = "Verification cookies terminee."


def analyze_current_url(backend: CliBackend, state: CliState) -> None:
    url = (state.current_url or "").strip()
    if not url:
        state.status_message = "Aucune URL fournie."
        return
    title, domain, pairs = backend.analyze_url(url, state.cookies, state.user_agent)
    state.current_title = title
    state.current_domain = domain
    state.detected_items = [
        CliItem(index=idx + 1, label=(label or f"Element {idx + 1}").strip(), url=(item_url or "").strip())
        for idx, (label, item_url) in enumerate(pairs)
    ]
    state.filtered_indices = list(range(len(state.detected_items)))
    state.selected_urls = {item.url for item in state.detected_items}
    state.status_message = f"{len(state.detected_items)} element(s) detecte(s) pour {title}."


def apply_text_filter(state: CliState, text: str) -> None:
    needle = (text or "").strip().lower()
    if not needle:
        state.filtered_indices = list(range(len(state.detected_items)))
        return
    visible = []
    for idx, item in enumerate(state.detected_items):
        if needle in item.label.lower():
            visible.append(idx)
    state.filtered_indices = visible


def toggle_item_selection(state: CliState, visible_index: int) -> None:
    if visible_index < 0 or visible_index >= len(state.filtered_indices):
        return
    item = state.detected_items[state.filtered_indices[visible_index]]
    if item.url in state.selected_urls:
        state.selected_urls.remove(item.url)
    else:
        state.selected_urls.add(item.url)


def select_all(state: CliState) -> None:
    state.selected_urls = {item.url for item in state.detected_items}


def deselect_all(state: CliState) -> None:
    state.selected_urls.clear()


def invert_selection(state: CliState) -> None:
    selected = set(state.selected_urls)
    state.selected_urls = {item.url for item in state.detected_items if item.url not in selected}


def apply_range_selection(state: CliState, expr: str) -> None:
    raw = (expr or "").strip()
    if not raw:
        return
    selected_positions: set[int] = set()
    for chunk in raw.split(","):
        part = chunk.strip()
        if not part:
            continue
        if part.endswith("+") and part[:-1].isdigit():
            start = max(1, int(part[:-1]))
            selected_positions.update(range(start, len(state.detected_items) + 1))
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            if left.strip().isdigit() and right.strip().isdigit():
                start = int(left.strip())
                end = int(right.strip())
                if end < start:
                    start, end = end, start
                selected_positions.update(range(max(1, start), min(len(state.detected_items), end) + 1))
                continue
        if part.isdigit():
            selected_positions.add(int(part))
    state.selected_urls = {
        item.url
        for item in state.detected_items
        if item.index in selected_positions
    }

