from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CliItem:
    index: int
    label: str
    url: str


@dataclass(slots=True)
class CliState:
    cookies: dict[str, str]
    user_agent: str
    cbz_enabled: bool = True
    webp2jpg_enabled: bool = True
    smart_resume_enabled: bool = True
    verbose_logs: bool = True
    current_url: str = ""
    current_title: str = ""
    current_domain: str = ""
    detected_items: list[CliItem] = field(default_factory=list)
    filtered_indices: list[int] = field(default_factory=list)
    selected_urls: set[str] = field(default_factory=set)
    cookie_status: dict[str, str] = field(default_factory=dict)
    status_message: str = "Pret."
    unsaved_changes: bool = False

    def reset_analysis(self) -> None:
        self.current_title = ""
        self.current_domain = ""
        self.detected_items = []
        self.filtered_indices = []
        self.selected_urls.clear()

    @property
    def selection_summary(self) -> str:
        total = len(self.detected_items)
        selected = len(self.selected_urls)
        if total <= 0:
            return "0 element"
        if selected <= 0:
            suffix = "element" if total == 1 else "elements"
            return f"{total} {suffix}"
        suffix = "element" if total == 1 else "elements"
        return f"{selected}/{total} {suffix}"

