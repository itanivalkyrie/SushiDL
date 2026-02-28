from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CliItem:
    index: int
    label: str
    url: str


@dataclass(slots=True)
class CliDownloadError:
    tome: str
    stage: str
    reason: str
    status_code: int | None = None
    action: str = ""


@dataclass(slots=True)
class CliDownloadStatus:
    active: bool = False
    finished: bool = False
    cancelled: bool = False
    output_dir: str = ""
    total_volumes: int = 0
    completed_volumes: int = 0
    current_volume: str = "--"
    current_images_done: int = 0
    current_images_total: int = 0
    global_percent: float = 0.0
    logs: list[str] = field(default_factory=list)
    errors: list[CliDownloadError] = field(default_factory=list)
    status_message: str = "Pret."
    eta_volume: str = "--:--"
    eta_global: str = "--:--"


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
    download_status: CliDownloadStatus = field(default_factory=CliDownloadStatus)

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
