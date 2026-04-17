from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
import re

import pandas as pd

from app.models import MeasurementRecord


def _next_session_key(directory: Path, day_stamp: str) -> str:
    pattern = re.compile(rf"autosave_{re.escape(day_stamp)}_(\d{{3}})\.json$", re.IGNORECASE)
    used: set[int] = set()
    for file in directory.glob(f"autosave_{day_stamp}_*.json"):
        match = pattern.match(file.name)
        if match:
            used.add(int(match.group(1)))
    candidate = 1
    while candidate in used:
        candidate += 1
    return f"{candidate:03d}"


def ensure_autosave_session_path(app) -> Path:
    current = getattr(app, "_autosave_session_path", None)
    if current is not None:
        return current
    now = datetime.now()
    day_stamp = now.strftime("%Y%m%d")
    session_key = _next_session_key(app.session_autosave_dir, day_stamp)
    app._autosave_session_key = session_key
    app._autosave_session_day = day_stamp
    app._autosave_session_path = (
        app.session_autosave_dir / f"autosave_{day_stamp}_{session_key}.json"
    )
    return app._autosave_session_path


def build_table_export_frame(app) -> pd.DataFrame:
    visible_columns = [
        key for key in app.table_column_order if bool(app.table_column_vars[key].get())
    ]
    rows: list[dict[str, object]] = []
    for row_index, item_id in enumerate(app.measurements_table.get_children(), start=1):
        values = app.measurements_table.item(item_id, "values")
        row_map = dict(zip(app.table_column_order, values))
        normalized: dict[str, object] = {"№": row_index}
        for key in visible_columns:
            header = next(
                (
                    label
                    for column_key, label, _width, _anchor in app.TABLE_COLUMN_SPECS
                    if column_key == key
                ),
                key,
            )
            normalized[header] = row_map.get(key, "")
        rows.append(normalized)
    return pd.DataFrame(rows)


def build_session_data(app) -> dict:
    plotter = app.plotter
    autosave_path = getattr(app, "_autosave_session_path", None)
    return {
        "metadata": {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "records_count": len(app.measurement_records),
            "session_key": getattr(app, "_autosave_session_key", ""),
            "autosave_file": autosave_path.name if autosave_path else "",
        },
        "records": [record.as_dict() for record in app.measurement_records],
        "plot_state": {
            "view_mode": plotter.view_mode,
            "render_mode": plotter.render_mode,
            "normalization_enabled": plotter.normalization_enabled,
            "markers_enabled": plotter.markers_enabled,
            "heating_profile_enabled": plotter.heating_profile_enabled,
            "cursor_probe_enabled": plotter.cursor_probe_enabled,
            "stage_analysis_enabled": plotter.stage_analysis_enabled,
            "series_visibility": plotter._series_visibility,
        },
        "config": {
            "scale_port": app.scale_port_var.get(),
            "furnace_port": app.furnace_port_var.get(),
            "scale_enabled": app.config_data.scale.enabled,
            "furnace_enabled": app.config_data.furnace.enabled,
        },
    }


def save_session(app) -> None:
    target = filedialog.asksaveasfilename(
        parent=app,
        title="Сохранить сессию",
        defaultextension=".json",
        initialfile=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        filetypes=[("JSON", "*.json")],
    )
    if not target:
        return
    destination = Path(target)
    if not destination.suffix:
        destination = destination.with_suffix(".json")
    session_data = build_session_data(app)
    try:
        with open(destination, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)
        app._set_status(f"Сессия сохранена: {destination.name}", logging.INFO)
        messagebox.showinfo("Сохранение сессии", f"Сессия сохранена в {destination.name}", parent=app)
    except Exception as e:
        app.logger.exception("Ошибка сохранения сессии")
        app._set_status(f"Ошибка сохранения сессии: {e}", logging.ERROR)
        messagebox.showerror("Сохранение сессии", f"Ошибка: {e}", parent=app)


def autosave_session(app) -> Path | None:
    if not app.measurement_records:
        return None
    filename = ensure_autosave_session_path(app)
    session_data = build_session_data(app)
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)
        cleanup_autosaves(app, max_count=5)
        app.logger.info(f"Сессия автосохранена: {filename.name}")
        return filename
    except Exception:
        app.logger.exception("Ошибка автосохранения сессии")
        return None


def cleanup_autosaves(app, max_count: int = 5) -> None:
    try:
        files = list(app.session_autosave_dir.glob("autosave_*.json"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for file in files[max_count:]:
            file.unlink()
            app.logger.debug(f"Удалён старый автосейв: {file.name}")
    except Exception:
        app.logger.exception("Ошибка очистки автосейвов")


def autosave_timer(app) -> None:
    if app._autosave_timer_id is not None:
        app.after_cancel(app._autosave_timer_id)
    if app.controller.running and app.measurement_records:
        saved = autosave_session(app)
        if saved is not None:
            app.logger.debug(f"Периодическое автосохранение: {saved.name}")
    app._autosave_timer_id = app.after(60000, app._autosave_timer)


def load_session(app, filepath: Path) -> bool:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            session_data = json.load(f)
        if app.controller.running:
            app.controller.stop()

        records_data = session_data.get("records", [])
        records: list[MeasurementRecord] = []
        for rec_dict in records_data:
            records.append(
                MeasurementRecord(
                timestamp=rec_dict.get("timestamp"),
                mass=rec_dict.get("mass"),
                furnace_pv=rec_dict.get("furnace_pv"),
                furnace_sv=rec_dict.get("furnace_sv"),
                mass_timestamp=rec_dict.get("mass_timestamp"),
                furnace_pv_timestamp=rec_dict.get("furnace_pv_timestamp"),
                furnace_sv_timestamp=rec_dict.get("furnace_sv_timestamp"),
            )
            )
        app._load_records_into_ui(records)

        plot_state = session_data.get("plot_state", {})
        if plot_state:
            view_mode = plot_state.get("view_mode")
            if view_mode and hasattr(app.plotter, "set_view_mode"):
                app.plotter.set_view_mode(view_mode)
            render_mode = plot_state.get("render_mode")
            if render_mode and hasattr(app.plotter, "set_render_mode"):
                app.plotter.set_render_mode(render_mode)
            normalization = plot_state.get("normalization_enabled")
            if normalization and app.plotter.normalization_enabled != normalization:
                app.plotter.toggle_normalization()
            markers = plot_state.get("markers_enabled")
            if markers and app.plotter.markers_enabled != markers:
                app.plotter.toggle_markers()
            heating_profile_enabled = plot_state.get("heating_profile_enabled")
            if heating_profile_enabled and app.plotter.heating_profile_enabled != heating_profile_enabled:
                app.plotter.toggle_heating_profile()
            cursor_probe = plot_state.get("cursor_probe_enabled")
            if cursor_probe and app.plotter.cursor_probe_enabled != cursor_probe:
                app.plotter.toggle_cursor_probe()
            stage_analysis = plot_state.get("stage_analysis_enabled")
            if stage_analysis and app.plotter.stage_analysis_enabled != stage_analysis:
                app.plotter.toggle_stage_analysis()
            for series_key, visible in plot_state.get("series_visibility", {}).items():
                app.plotter.set_series_visible(series_key, visible)

        app._set_status(f"Сессия загружена: {filepath.name} ({len(records_data)} записей)", logging.INFO)
        messagebox.showinfo("Загрузка сессии", f"Сессия загружена: {len(records_data)} записей", parent=app)
        return True
    except Exception as e:
        app.logger.exception(f"Ошибка загрузки сессии: {filepath}")
        app._set_status(f"Ошибка загрузки сессии: {e}", logging.ERROR)
        messagebox.showerror("Загрузка сессии", f"Ошибка: {e}", parent=app)
        return False


def update_restore_session_menu(app) -> None:
    if not hasattr(app, "restore_session_menu"):
        return
    menu = app.restore_session_menu
    menu.delete(0, "end")
    try:
        files = list(app.session_autosave_dir.glob("autosave_*.json"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            menu.add_command(label="(пусто)", state="disabled")
            return
        for file in files[:5]:
            raw = file.stem.replace("autosave_", "")
            try:
                if re.fullmatch(r"\d{8}_\d{3}", raw):
                    day_stamp, session_key = raw.split("_")
                    dt = datetime.strptime(day_stamp, "%Y%m%d")
                    label = f"{dt.strftime('%Y-%m-%d')} | сессия {session_key}"
                else:
                    dt = datetime.strptime(raw, "%Y%m%d_%H%M%S")
                    label = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                label = file.name
            menu.add_command(label=label, command=lambda path=file: app.load_session(path))
    except Exception:
        app.logger.exception("Ошибка обновления меню восстановления сессий")
