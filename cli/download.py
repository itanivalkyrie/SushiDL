from __future__ import annotations

import threading
import time
from dataclasses import replace

from .state import CliDownloadError, CliDownloadStatus, CliState


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class CliDownloadController:
    def __init__(self, backend, state: CliState, output_dir: str):
        self.backend = backend
        self.state = state
        self.output_dir = (output_dir or "").strip()
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._start_time = 0.0

    def start(self) -> None:
        with self._lock:
            selected_items = [item for item in self.state.detected_items if item.url in self.state.selected_urls and not item.premium]
            premium_skipped = [item for item in self.state.detected_items if item.url in self.state.selected_urls and item.premium]
            self.state.download_status = CliDownloadStatus(
                active=True,
                finished=False,
                cancelled=False,
                output_dir=self.output_dir,
                total_volumes=len(selected_items),
                completed_volumes=0,
                current_volume="--",
                current_images_done=0,
                current_images_total=0,
                global_percent=0.0,
                logs=["Préparation du téléchargement..."],
                errors=[],
                status_message="Preparation...",
                eta_volume="--:--",
                eta_global="--:--",
                elapsed="00:00",
            )
        self._start_time = time.time()
        if premium_skipped:
            self.state.download_status.logs.append(f"{len(premium_skipped)} élément(s) premium ignoré(s).")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            self._append_log("Annulation demandee...")
            self.state.download_status.status_message = "Annulation demandee..."

    def snapshot(self) -> CliDownloadStatus:
        with self._lock:
            status = self.state.download_status
            return CliDownloadStatus(
                active=status.active,
                finished=status.finished,
                cancelled=status.cancelled,
                output_dir=status.output_dir,
                total_volumes=status.total_volumes,
                completed_volumes=status.completed_volumes,
                current_volume=status.current_volume,
                current_images_done=status.current_images_done,
                current_images_total=status.current_images_total,
                global_percent=status.global_percent,
                logs=list(status.logs),
                errors=list(status.errors),
                status_message=status.status_message,
                eta_volume=status.eta_volume,
                eta_global=status.eta_global,
                elapsed=status.elapsed,
            )

    def _append_log(self, message: str) -> None:
        if not message:
            return
        logs = self.state.download_status.logs
        logs.append(str(message).strip())
        if len(logs) > 12:
            del logs[:-12]

    def _refresh_eta(self, completed: int, total: int, done_images: int, total_images: int) -> None:
        elapsed = max(0.0, time.time() - self._start_time)
        status = self.state.download_status
        status.elapsed = _format_eta(elapsed)
        if total <= 0:
            status.eta_global = "--:--"
            status.eta_volume = "--:--"
            return
        current_fraction = 0.0
        if total_images > 0:
            current_fraction = max(0.0, min(1.0, float(done_images) / float(total_images)))
        progress_units = completed + current_fraction
        if progress_units <= 0:
            status.eta_global = "--:--"
        else:
            avg_unit = elapsed / progress_units
            remaining_units = max(0.0, total - progress_units)
            status.eta_global = _format_eta(avg_unit * remaining_units)
        if total_images > 0 and done_images > 0:
            avg_image = elapsed / max(1.0, progress_units * max(1, total_images))
            remaining_images = max(0, total_images - done_images)
            status.eta_volume = _format_eta(avg_image * remaining_images * max(1, total_images))
        else:
            status.eta_volume = "--:--"

    def _run(self) -> None:
        status = self.state.download_status
        selected_items = [item for item in self.state.detected_items if item.url in self.state.selected_urls and not item.premium]
        premium_skipped = [item for item in self.state.detected_items if item.url in self.state.selected_urls and item.premium]
        title = self.state.current_title or "Sans titre"
        ua = self.state.user_agent

        if not selected_items:
            with self._lock:
                status.active = False
                status.finished = True
                status.status_message = "Aucun element selectionne."
                self._append_log("Aucun element selectionne.")
            return

        for skipped in premium_skipped:
            with self._lock:
                self._append_log(f"Ignoré premium: {skipped.label}")

        for index, item in enumerate(selected_items, start=1):
            if self.cancel_event.is_set():
                break
            domain = self.backend.resolve_domain(item.url or self.state.current_url)
            cookie = (self.state.cookies.get(domain) or "").strip()
            with self._lock:
                status.current_volume = item.label
                status.current_images_done = 0
                status.current_images_total = 0
                status.status_message = f"Analyse images pour {item.label}..."
                self._append_log(f"Preparation: {item.label}")
                status.global_percent = ((index - 1) / max(1, len(selected_items))) * 100.0
                self._refresh_eta(index - 1, len(selected_items), 0, 0)

            try:
                image_urls = self.backend.get_images_for_download(item.url, cookie, ua, cancel_event=self.cancel_event)
            except Exception as exc:
                with self._lock:
                    status.errors.append(
                        CliDownloadError(
                            tome=item.label,
                            stage="get_images",
                            reason=str(exc),
                            action="Verifier le cookie, le User-Agent ou l'URL.",
                        )
                    )
                    self._append_log(f"Echec analyse images: {item.label}")
                continue

            if self.cancel_event.is_set():
                break

            if not image_urls:
                with self._lock:
                    status.errors.append(
                        CliDownloadError(
                            tome=item.label,
                            stage="get_images",
                            reason="Aucune image detectee.",
                            action="Verifier l'acces a la source ou l'authentification.",
                        )
                    )
                    self._append_log(f"Aucune image: {item.label}")
                continue

            def logger(message, level="info"):
                if not message:
                    return
                with self._lock:
                    self._append_log(message)

            def update_progress(done, total_images):
                with self._lock:
                    status.current_images_done = int(done or 0)
                    status.current_images_total = int(total_images or 0)
                    current_fraction = 0.0
                    if total_images:
                        current_fraction = max(0.0, min(1.0, float(done or 0) / float(total_images)))
                    status.global_percent = ((index - 1) + current_fraction) / max(1, len(selected_items)) * 100.0
                    status.status_message = f"Téléchargement {item.label}"
                    self._refresh_eta(index - 1, len(selected_items), int(done or 0), int(total_images or 0))

            def error_callback(payload):
                with self._lock:
                    status.errors.append(
                        CliDownloadError(
                            tome=(payload or {}).get("tome") or item.label,
                            stage=(payload or {}).get("stage") or "download",
                            reason=(payload or {}).get("reason") or "Erreur inconnue",
                            status_code=(payload or {}).get("status_code"),
                            action=(payload or {}).get("action") or "",
                        )
                    )

            result = self.backend.download_selected_volume(
                item=item,
                image_urls=image_urls,
                title=title,
                cookie=cookie,
                ua=ua,
                output_dir=self.output_dir,
                logger=logger,
                update_progress=update_progress,
                error_callback=error_callback,
                cancel_event=self.cancel_event,
                cbz_enabled=self.state.cbz_enabled,
                comicinfo_enabled=self.state.comicinfo_enabled,
                chapter_cover_enabled=self.state.chapter_cover_enabled,
                webp2jpg_enabled=self.state.webp2jpg_enabled,
                smart_resume_enabled=self.state.smart_resume_enabled,
                download_threads=getattr(self.state, "download_threads", 3),
                total_count=len(self.state.detected_items),
                series_metadata=self.state.series_metadata,
                volume_metadata=getattr(self.state, "volume_metadata", {}),
            )

            with self._lock:
                if result:
                    status.completed_volumes += 1
                    self._append_log(f"Termine: {item.label}")
                elif self.cancel_event.is_set():
                    self._append_log(f"Annule: {item.label}")
                else:
                    self._append_log(f"Echec: {item.label}")
                status.current_images_done = 0
                status.current_images_total = 0
                status.global_percent = (status.completed_volumes / max(1, len(selected_items))) * 100.0
                self._refresh_eta(status.completed_volumes, len(selected_items), 0, 0)

        with self._lock:
            status.active = False
            status.finished = True
            status.cancelled = self.cancel_event.is_set()
            status.current_volume = "--"
            status.current_images_done = 0
            status.current_images_total = 0
            status.eta_volume = "--:--"
            status.global_percent = (status.completed_volumes / max(1, len(selected_items))) * 100.0
            status.elapsed = _format_eta(max(0.0, time.time() - self._start_time))
            if status.cancelled:
                status.status_message = "Téléchargement annulé."
                self._append_log("Téléchargement annulé.")
            elif status.errors:
                status.status_message = "Téléchargement terminé avec erreurs."
                self._append_log("Téléchargement terminé avec erreurs.")
            else:
                status.status_message = "Téléchargement terminé."
                self._append_log("Téléchargement terminé.")
