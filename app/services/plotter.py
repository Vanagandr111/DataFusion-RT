from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import Misc, ttk

import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from app.models import MeasurementRecord
from app.services.heating_profile import HeatingProfileResult, build_heating_profile
from app.theme import PlotTheme


class LivePlotter:
    VIEW_COMBINED = "combined"
    VIEW_SPLIT = "split"
    VIEW_MASS = "mass"
    VIEW_TEMP = "temp"
    VIEW_DELTA = "delta"
    VIEW_DTG = "dtg"

    RENDER_LINE = "line"
    RENDER_POINTS = "points"
    RENDER_SMOOTH = "smooth"

    def __init__(self, master: Misc, max_points: int, *, plot_theme: PlotTheme, scale: float = 1.0, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self._plot_theme = plot_theme
        self._scale = scale
        self._zoom_active = False
        self._pan_active = False
        self._view_mode = self.VIEW_COMBINED
        self._render_mode = self.RENDER_LINE
        self._normalization_enabled = False
        self._markers_enabled = False
        self._marker_indices: dict[str, int] = {}
        self._active_marker_label: str | None = None
        self._stage_analysis_enabled = False
        self._autoscale_enabled = True
        self._manual_x_seconds = 600.0
        self._manual_y_span = 250.0
        self._y_headroom = 50.0
        self._display_paused = False
        self._viewport_locked = False
        self._saved_xlim: tuple[float, float] | None = None
        self._saved_ylim_by_role: dict[str, tuple[float, float]] = {}
        self._sticky_y_limits: dict[str, tuple[float, float]] = {}
        self._x_pad_seconds = 2.0
        self._series_style_overrides: dict[str, dict[str, object]] = {}
        self._cursor_probe_enabled = False
        self._cursor_guides: dict[object, tuple[object, object, object]] = {}
        self._cursor_anchor_points: list[dict[str, object]] = []
        self._series_visibility = {"mass": True, "temperature": True, "thermocouple": True, "heating_profile": True}
        self._heating_profile_enabled = False
        self._heating_profile_dirty = True
        self._heating_profile_result = HeatingProfileResult([], [], "temperature", ())

        self.timestamps: deque[datetime] = deque(maxlen=max_points)
        self.mass_timestamps: deque[datetime] = deque(maxlen=max_points)
        self.temperature_timestamps: deque[datetime] = deque(maxlen=max_points)
        self.thermocouple_timestamps: deque[datetime] = deque(maxlen=max_points)
        self.masses: deque[float] = deque(maxlen=max_points)
        self.temperatures: deque[float] = deque(maxlen=max_points)
        self.thermocouple_temperatures: deque[float] = deque(maxlen=max_points)

        self.container = ttk.Frame(master, style="Card.TFrame")
        self.figure = Figure(figsize=(10.4, 5.9), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.container)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        self.canvas.mpl_connect("axes_leave_event", self._on_axes_leave)
        self.canvas.mpl_connect("button_press_event", self._on_mouse_click)
        self.canvas.mpl_connect("button_release_event", self._on_mouse_release)

        self._toolbar_host = ttk.Frame(self.container)
        self._toolbar = NavigationToolbar2Tk(self.canvas, self._toolbar_host, pack_toolbar=False)
        self._toolbar.update()

        self.ax_main = None
        self.ax_secondary = None
        self.axes: list = []
        self.apply_theme(plot_theme, scale=scale)

    def get_widget(self):
        return self.container

    @property
    def view_mode(self) -> str:
        return self._view_mode

    @property
    def render_mode(self) -> str:
        return self._render_mode

    @property
    def normalization_enabled(self) -> bool:
        return self._normalization_enabled

    @property
    def markers_enabled(self) -> bool:
        return self._markers_enabled

    @property
    def stage_analysis_enabled(self) -> bool:
        return self._stage_analysis_enabled

    @property
    def cursor_probe_enabled(self) -> bool:
        return self._cursor_probe_enabled

    @property
    def cursor_anchor_count(self) -> int:
        return len(self._cursor_anchor_points)

    @property
    def heating_profile_enabled(self) -> bool:
        return self._heating_profile_enabled

    def set_max_points(self, max_points: int) -> None:
        if max_points <= 0 or max_points == self.timestamps.maxlen:
            return
        self.timestamps = deque(self.timestamps, maxlen=max_points)
        self.mass_timestamps = deque(self.mass_timestamps, maxlen=max_points)
        self.temperature_timestamps = deque(self.temperature_timestamps, maxlen=max_points)
        self.thermocouple_timestamps = deque(self.thermocouple_timestamps, maxlen=max_points)
        self.masses = deque(self.masses, maxlen=max_points)
        self.temperatures = deque(self.temperatures, maxlen=max_points)
        self.thermocouple_temperatures = deque(self.thermocouple_temperatures, maxlen=max_points)
        self._render_current_view()

    def set_view_mode(self, view_mode: str) -> None:
        if view_mode == self._view_mode:
            return
        self._view_mode = view_mode
        self._reset_toolbar_modes()
        self._render_current_view()

    def set_render_mode(self, render_mode: str) -> None:
        if render_mode == self._render_mode:
            return
        self._render_mode = render_mode
        self._render_current_view()

    def toggle_normalization(self) -> bool:
        self._normalization_enabled = not self._normalization_enabled
        self._render_current_view()
        return self._normalization_enabled

    def toggle_markers(self) -> bool:
        self._markers_enabled = not self._markers_enabled
        if self._markers_enabled:
            self._marker_indices.clear()
            self._ensure_marker_indices()
        else:
            self._active_marker_label = None
            self._marker_indices.clear()
        self._render_current_view()
        return self._markers_enabled

    def toggle_stage_analysis(self) -> bool:
        self._stage_analysis_enabled = not self._stage_analysis_enabled
        self._render_current_view()
        return self._stage_analysis_enabled

    def toggle_cursor_probe(self) -> bool:
        self._cursor_probe_enabled = not self._cursor_probe_enabled
        if not self._cursor_probe_enabled:
            self._hide_cursor_guides()
        self.canvas.draw_idle()
        return self._cursor_probe_enabled

    def clear_cursor_anchors(self) -> None:
        self._cursor_anchor_points.clear()
        self._render_current_view()

    def toggle_heating_profile(self) -> bool:
        self._heating_profile_enabled = not self._heating_profile_enabled
        if self._heating_profile_enabled:
            self._series_visibility["heating_profile"] = True
            self._heating_profile_dirty = True
        self._render_current_view()
        return self._heating_profile_enabled

    def apply_series_styles(self, overrides: dict[str, dict[str, object]]) -> None:
        self._series_style_overrides = {key: self._sanitize_series_style(value) for key, value in overrides.items()}
        self._render_current_view()

    def configure_scale_mode(
        self,
        *,
        autoscale_enabled: bool,
        manual_x_seconds: float | None = None,
        manual_y_span: float | None = None,
        y_headroom: float | None = None,
    ) -> None:
        self._autoscale_enabled = autoscale_enabled
        if manual_x_seconds is not None:
            self._manual_x_seconds = max(10.0, float(manual_x_seconds))
        if manual_y_span is not None:
            self._manual_y_span = max(1.0, float(manual_y_span))
        if y_headroom is not None:
            self._y_headroom = max(0.0, float(y_headroom))
        self._viewport_locked = False
        self._sticky_y_limits.clear()
        self._render_current_view()

    @property
    def display_paused(self) -> bool:
        return self._display_paused

    def toggle_display_pause(self) -> bool:
        self._display_paused = not self._display_paused
        if self._display_paused:
            self._viewport_locked = True
            self._capture_current_viewport()
        else:
            self.resume_live_view()
        return self._display_paused

    def resume_live_view(self) -> None:
        self._display_paused = False
        self._viewport_locked = False
        self._saved_xlim = None
        self._saved_ylim_by_role.clear()
        self._sticky_y_limits.clear()
        self._render_current_view()

    def get_series_styles(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for key in ("mass", "temperature", "thermocouple"):
            style = self._series_style(key)
            result[key] = {"color": style["color"], "linestyle": style["linestyle"], "linewidth": style["linewidth"]}
        return result

    def calculation_summary(self) -> dict[str, str]:
        markers = self._marker_metrics()
        return {
            "delta_mass": markers.get("delta_mass", "--"),
            "delta_mass_percent": markers.get("delta_mass_percent", "--"),
            "delta_temperature": markers.get("delta_temperature", "--"),
            "delta_time": markers.get("delta_time", "--"),
            "max_dtg": self._dtg_extreme(),
            "stage_range": self._stage_window(),
        }

    def update(self, record: MeasurementRecord) -> None:
        self.timestamps.append(datetime.fromisoformat(record.timestamp))
        self._append_series_sample(self.mass_timestamps, self.masses, record.mass_timestamp, record.mass)
        previous_temp_points = len(self.temperature_timestamps)
        previous_thermocouple_points = len(self.thermocouple_timestamps)
        self._append_series_sample(self.temperature_timestamps, self.temperatures, record.furnace_pv_timestamp, record.furnace_pv)
        self._append_series_sample(self.thermocouple_timestamps, self.thermocouple_temperatures, record.furnace_sv_timestamp, record.furnace_sv)
        if len(self.temperature_timestamps) != previous_temp_points or len(self.thermocouple_timestamps) != previous_thermocouple_points:
            self._heating_profile_dirty = True
        if self._display_paused:
            return
        self._render_current_view()

    def clear(self) -> None:
        self.timestamps.clear()
        self.mass_timestamps.clear()
        self.temperature_timestamps.clear()
        self.thermocouple_timestamps.clear()
        self.masses.clear()
        self.temperatures.clear()
        self.thermocouple_temperatures.clear()
        self._heating_profile_dirty = True
        self._heating_profile_result = HeatingProfileResult([], [], "temperature", ())
        self._cursor_anchor_points.clear()
        self._marker_indices.clear()
        self._active_marker_label = None
        self._render_current_view()

    def set_series_visible(self, series_key: str, visible: bool) -> None:
        if series_key in self._series_visibility:
            self._series_visibility[series_key] = visible
            self._render_current_view()

    def is_series_visible(self, series_key: str) -> bool:
        return bool(self._series_visibility.get(series_key, True))

    def legend_items(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for key in self.available_series_keys():
            style = self._series_style(key)
            items.append({"key": key, "label": self._series_display_name(key), "color": style["color"], "linestyle": style["linestyle"], "linewidth": style["linewidth"], "visible": self.is_series_visible(key)})
        return items

    def available_series_keys(self) -> list[str]:
        if self._view_mode in {self.VIEW_MASS, self.VIEW_DTG}:
            return ["mass"]
        if self._view_mode in {self.VIEW_TEMP, self.VIEW_DELTA}:
            keys = ["temperature", "thermocouple"]
            if self._view_mode != self.VIEW_DELTA and self._has_visible_heating_profile():
                keys.append("heating_profile")
            return keys
        keys = ["mass", "temperature", "thermocouple"]
        if self._has_visible_heating_profile():
            keys.append("heating_profile")
        return keys

    def apply_theme(self, plot_theme: PlotTheme, *, scale: float | None = None) -> None:
        self._plot_theme = plot_theme
        if scale is not None:
            self._scale = scale
        self._render_current_view()

    def toggle_zoom(self) -> bool:
        if self._pan_active:
            self._toolbar.pan()
            self._pan_active = False
        self._viewport_locked = True
        self._capture_current_viewport()
        self._toolbar.zoom()
        self._zoom_active = not self._zoom_active
        return self._zoom_active

    def toggle_pan(self) -> bool:
        if self._zoom_active:
            self._toolbar.zoom()
            self._zoom_active = False
        self._viewport_locked = True
        self._capture_current_viewport()
        self._toolbar.pan()
        self._pan_active = not self._pan_active
        return self._pan_active

    def zoom_in(self) -> None:
        self._zoom_axes(0.8)

    def zoom_out(self) -> None:
        self._zoom_axes(1.25)

    def reset_view(self) -> None:
        self._reset_toolbar_modes()
        self._viewport_locked = False
        self._saved_xlim = None
        self._saved_ylim_by_role.clear()
        self._sticky_y_limits.clear()
        self._toolbar.home()
        self.autoscale(draw=True)

    def autoscale(self, *, draw: bool = True) -> None:
        if not self.axes:
            return
        self._viewport_locked = False
        self._saved_xlim = None
        self._saved_ylim_by_role.clear()
        for axis in self.axes:
            axis.relim()
            axis.autoscale_view()
        self._apply_scale_limits()
        if draw:
            self.canvas.draw_idle()

    def save_image(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.figure.savefig(destination, dpi=160, bbox_inches="tight")

    def close(self) -> None:
        try:
            self.canvas.get_tk_widget().destroy()
        except Exception:
            self.logger.debug("Ignoring plot widget close error.", exc_info=True)

    def _render_current_view(self) -> None:
        if self._viewport_locked:
            self._capture_current_viewport()
        self.figure.clear()
        self.axes = []
        self.ax_main = None
        self.ax_secondary = None
        self._cursor_guides.clear()
        if self._view_mode == self.VIEW_COMBINED:
            self._draw_combined_view()
        elif self._view_mode == self.VIEW_SPLIT:
            self._draw_split_view()
        elif self._view_mode == self.VIEW_MASS:
            self._draw_single_view(series="mass")
        elif self._view_mode == self.VIEW_TEMP:
            self._draw_single_view(series="temp")
        elif self._view_mode == self.VIEW_DTG:
            self._draw_dtg_view()
        else:
            self._draw_delta_view()
        self.figure.subplots_adjust(left=0.08, right=0.9, top=0.93, bottom=0.08, hspace=0.12)
        if self._viewport_locked:
            self._apply_saved_viewport()
        self.canvas.draw_idle()

    def _draw_combined_view(self) -> None:
        ax_mass = self.figure.add_subplot(111)
        ax_temp = ax_mass.twinx()
        self.axes = [ax_mass, ax_temp]
        self.ax_main = ax_mass
        self.ax_secondary = ax_temp
        self._set_axis_role(ax_mass, "mass")
        self._set_axis_role(ax_temp, "temp")
        self._style_axis(ax_mass, ylabel=self._mass_axis_label(), color=self._plot_theme.mass_label)
        self._style_axis(ax_temp, ylabel="Температура (°C)", color=self._plot_theme.temp_label, secondary=True)
        ax_mass.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        handles = []
        mass_values = self.mass_series_values()
        temp_values = self._series_values(list(self.temperatures))
        sv_values = self._series_values(list(self.thermocouple_temperatures))
        if self.is_series_visible("mass"):
            handles.append(self._draw_series(ax_mass, list(self.mass_timestamps), mass_values, "mass", self._legend_label("Масса", mass_values, self._mass_unit())))
        if self.is_series_visible("temperature"):
            handles.append(self._draw_series(ax_temp, list(self.temperature_timestamps), temp_values, "temperature", self._legend_label("Камера", temp_values, "°C")))
        if self.is_series_visible("thermocouple"):
            handles.append(self._draw_series(ax_temp, list(self.thermocouple_timestamps), sv_values, "thermocouple", self._legend_label("Термопара", sv_values, "°C")))
        heating_profile_handle = self._draw_heating_profile(ax_temp)
        if heating_profile_handle is not None:
            handles.append(heating_profile_handle)
        self._style_time_axis(ax_mass)
        self._style_legend(ax_mass, handles)
        self._apply_analysis_overlays(ax_mass, ax_temp)
        self._draw_cursor_anchors()
        self.figure.suptitle("Общий график измерений", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_split_view(self) -> None:
        ax_mass = self.figure.add_subplot(211)
        ax_temp = self.figure.add_subplot(212, sharex=ax_mass)
        self.axes = [ax_mass, ax_temp]
        self.ax_main = ax_mass
        self.ax_secondary = ax_temp
        self._set_axis_role(ax_mass, "mass")
        self._set_axis_role(ax_temp, "temp")
        self._style_axis(ax_mass, ylabel=self._mass_axis_label(), color=self._plot_theme.mass_label)
        self._style_axis(ax_temp, ylabel="Температура (°C)", color=self._plot_theme.temp_label)
        ax_temp.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        mass_values = self.mass_series_values()
        temp_values = self._series_values(list(self.temperatures))
        sv_values = self._series_values(list(self.thermocouple_temperatures))
        mass_handles = []
        temp_handles = []
        if self.is_series_visible("mass"):
            mass_handles.append(self._draw_series(ax_mass, list(self.mass_timestamps), mass_values, "mass", self._legend_label("Масса", mass_values, self._mass_unit())))
        if self.is_series_visible("temperature"):
            temp_handles.append(self._draw_series(ax_temp, list(self.temperature_timestamps), temp_values, "temperature", self._legend_label("Камера", temp_values, "°C")))
        if self.is_series_visible("thermocouple"):
            temp_handles.append(self._draw_series(ax_temp, list(self.thermocouple_timestamps), sv_values, "thermocouple", self._legend_label("Термопара", sv_values, "°C")))
        heating_profile_handle = self._draw_heating_profile(ax_temp)
        if heating_profile_handle is not None:
            temp_handles.append(heating_profile_handle)
        self._style_time_axis(ax_temp)
        ax_mass.tick_params(labelbottom=False)
        self._style_legend(ax_mass, mass_handles)
        self._style_legend(ax_temp, temp_handles)
        self._apply_analysis_overlays(ax_mass, ax_temp)
        self._draw_cursor_anchors()
        self.figure.suptitle("Раздельные графики: масса и температура", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_single_view(self, *, series: str) -> None:
        axis = self.figure.add_subplot(111)
        self.axes = [axis]
        self.ax_main = axis
        self._set_axis_role(axis, "mass" if series == "mass" else "temp")
        axis.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        handles = []
        if series == "mass":
            values = self.mass_series_values()
            self._style_axis(axis, ylabel=self._mass_axis_label(), color=self._plot_theme.mass_label)
            if self.is_series_visible("mass"):
                handles.append(self._draw_series(axis, list(self.mass_timestamps), values, "mass", self._legend_label("Масса", values, self._mass_unit())))
            self._apply_analysis_overlays(axis, None)
            title = "График массы"
        else:
            values = self._series_values(list(self.temperatures))
            sv_values = self._series_values(list(self.thermocouple_temperatures))
            self._style_axis(axis, ylabel="Температура (°C)", color=self._plot_theme.temp_label)
            if self.is_series_visible("temperature"):
                handles.append(self._draw_series(axis, list(self.temperature_timestamps), values, "temperature", self._legend_label("Камера", values, "°C")))
            if self.is_series_visible("thermocouple"):
                handles.append(self._draw_series(axis, list(self.thermocouple_timestamps), sv_values, "thermocouple", self._legend_label("Термопара", sv_values, "°C")))
            heating_profile_handle = self._draw_heating_profile(axis)
            if heating_profile_handle is not None:
                handles.append(heating_profile_handle)
            self._apply_analysis_overlays(None, axis)
            title = "График температуры"
        self._style_time_axis(axis)
        self._style_legend(axis, handles)
        self._draw_cursor_anchors()
        self.figure.suptitle(title, fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_delta_view(self) -> None:
        ax_mass = self.figure.add_subplot(211)
        ax_temp = self.figure.add_subplot(212, sharex=ax_mass)
        self.axes = [ax_mass, ax_temp]
        self.ax_main = ax_mass
        self.ax_secondary = ax_temp
        self._set_axis_role(ax_mass, "delta_mass")
        self._set_axis_role(ax_temp, "delta_temp")
        mass_values = self._delta_values(self.mass_series_values())
        temp_values = self._delta_values(list(self.temperatures))
        sv_values = self._delta_values(list(self.thermocouple_temperatures))
        self._style_axis(ax_mass, ylabel=f"ΔМасса ({self._mass_unit()})", color=self._plot_theme.mass_label)
        self._style_axis(ax_temp, ylabel="ΔТемпература (°C)", color=self._plot_theme.temp_label)
        ax_temp.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        mass_handles = []
        temp_handles = []
        if self.is_series_visible("mass"):
            mass_handles.append(self._draw_series(ax_mass, list(self.mass_timestamps), mass_values, "mass", self._legend_label("ΔМасса", mass_values, self._mass_unit())))
        if self.is_series_visible("temperature"):
            temp_handles.append(self._draw_series(ax_temp, list(self.temperature_timestamps), temp_values, "temperature", self._legend_label("ΔТемпература камеры", temp_values, "°C")))
        if self.is_series_visible("thermocouple"):
            temp_handles.append(self._draw_series(ax_temp, list(self.thermocouple_timestamps), sv_values, "thermocouple", self._legend_label("ΔТермопара", sv_values, "°C")))
        ax_mass.axhline(0.0, color=self._plot_theme.spine, linewidth=1.0, linestyle="--")
        ax_temp.axhline(0.0, color=self._plot_theme.spine, linewidth=1.0, linestyle="--")
        self._style_time_axis(ax_temp)
        ax_mass.tick_params(labelbottom=False)
        self._style_legend(ax_mass, mass_handles)
        self._style_legend(ax_temp, temp_handles)
        self._apply_analysis_overlays(ax_mass, ax_temp)
        self._draw_cursor_anchors()
        self.figure.suptitle("Изменение параметров во времени", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_dtg_view(self) -> None:
        axis = self.figure.add_subplot(111)
        self.axes = [axis]
        self.ax_main = axis
        self._set_axis_role(axis, "dtg")
        values = self._dtg_values(list(self.mass_timestamps), self.mass_series_values())
        self._style_axis(axis, ylabel=f"DTG ({self._dtg_unit()})", color=self._plot_theme.mass_label)
        axis.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        handles = []
        if self.is_series_visible("mass"):
            handles.append(self._draw_series(axis, list(self.mass_timestamps), values, "mass", self._legend_label("DTG", values, self._dtg_unit())))
        axis.axhline(0.0, color=self._plot_theme.spine, linewidth=1.0, linestyle="--")
        self._style_time_axis(axis)
        self._style_legend(axis, handles)
        self._apply_analysis_overlays(axis, None)
        self._draw_cursor_anchors()
        self.figure.suptitle("DTG: скорость изменения массы", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_series(self, axis, timestamps: list[datetime], values: list[float], series_key: str, label: str):
        if not timestamps or not values:
            return axis.plot([], [], label=label)[0]
        render = {"marker": "", "markersize": 0.0, "alpha": 0.98} if series_key == "heating_profile" else self._render_style()
        style = self._series_style(series_key)
        (line,) = axis.plot(
            timestamps,
            values,
            color=style["color"],
            label=label,
            linewidth=style["linewidth"] if render["marker"] == "" else max(0.0, float(style["linewidth"]) - 0.2),
            linestyle=style["linestyle"],
            marker=render["marker"],
            markersize=render["markersize"],
            markerfacecolor=style["color"],
            markeredgewidth=0.0,
            alpha=render["alpha"],
        )
        return line

    def _style_axis(self, axis, *, ylabel: str, color: str, secondary: bool = False) -> None:
        axis.set_facecolor(self._plot_theme.axes_bg)
        axis.set_ylabel(ylabel, color=color, fontsize=self._label_size, labelpad=22 if secondary else 10)
        axis.tick_params(axis="y", colors=color, labelsize=self._tick_size)
        axis.tick_params(axis="x", colors=self._plot_theme.x_tick, labelsize=self._tick_size)
        axis.grid(True, color=self._plot_theme.grid, linewidth=0.8, alpha=0.72)
        for spine in axis.spines.values():
            spine.set_color(self._plot_theme.spine)
        if secondary:
            axis.yaxis.set_label_coords(1.09, 0.5)

    def _style_time_axis(self, axis) -> None:
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    def _style_legend(self, axis, handles) -> None:
        if not handles:
            return
        legend = axis.legend(handles=handles, loc="upper left", facecolor=self._plot_theme.legend_bg, edgecolor=self._plot_theme.legend_edge, framealpha=0.95)
        for text in legend.get_texts():
            text.set_color(self._plot_theme.legend_text)

    def _apply_analysis_overlays(self, mass_axis, temp_axis) -> None:
        if self._stage_analysis_enabled:
            self._draw_stage_overlay(mass_axis, temp_axis)
        if self._markers_enabled and mass_axis is not None:
            self._draw_markers(mass_axis)

    def _legend_label(self, base: str, values: list[float], unit: str) -> str:
        last_value = self._last_finite(values)
        if last_value is None:
            return f"{base}: -- {unit}"
        digits = 1 if unit == "°C" else 2 if "%" in unit else 3
        return f"{base}: {last_value:.{digits}f} {unit}"

    def _series_display_name(self, series_key: str) -> str:
        if self._view_mode == self.VIEW_DTG and series_key == "mass":
            return "DTG"
        names = {
            "mass": "Масса, %" if self._normalization_enabled else "Масса",
            "temperature": "Камера",
            "thermocouple": "Термопара",
            "heating_profile": "Профиль нагрева",
        }
        return names.get(series_key, series_key)

    def _series_style(self, series_key: str) -> dict[str, object]:
        styles = {
            "mass": {"color": self._plot_theme.mass_line, "linestyle": "-", "linewidth": 2.15},
            "temperature": {"color": self._plot_theme.temp_line, "linestyle": "-", "linewidth": 2.15},
            "thermocouple": {"color": "#F6A04D", "linestyle": "-", "linewidth": 2.15},
            "heating_profile": {"color": "#101010", "linestyle": "-", "linewidth": 2.85},
        }
        result = styles.get(series_key, styles["temperature"]).copy()
        if series_key != "heating_profile":
            result.update(self._sanitize_series_style(self._series_style_overrides.get(series_key, {})))
        return result

    def _sanitize_series_style(self, style: dict[str, object] | None) -> dict[str, object]:
        if not style:
            return {}
        sanitized: dict[str, object] = {}
        color = str(style.get("color", "")).strip()
        if color:
            sanitized["color"] = color
        linestyle = str(style.get("linestyle", "")).strip().lower()
        if linestyle in {"-", "--", "solid", "dashed"}:
            sanitized["linestyle"] = "-" if linestyle in {"-", "solid"} else "--"
        try:
            linewidth = float(style.get("linewidth", 2.15))
        except (TypeError, ValueError):
            linewidth = 2.15
        sanitized["linewidth"] = min(4.5, max(0.8, linewidth))
        return sanitized

    def _mass_unit(self) -> str:
        return "%" if self._normalization_enabled else "г"

    def _mass_axis_label(self) -> str:
        return "Масса (%)" if self._normalization_enabled else "Масса (г)"

    def _dtg_unit(self) -> str:
        return "%/мин" if self._normalization_enabled else "г/мин"

    def _has_visible_heating_profile(self) -> bool:
        if not self._heating_profile_enabled:
            return False
        if self._view_mode in {self.VIEW_MASS, self.VIEW_DTG, self.VIEW_DELTA}:
            return False
        return self._get_heating_profile().has_data

    def _temperature_source_data(self) -> tuple[list[datetime], list[float], str]:
        thermocouple_points = [
            (timestamp, value)
            for timestamp, value in zip(self.thermocouple_timestamps, self.thermocouple_temperatures)
            if not math.isnan(value)
        ]
        if len(thermocouple_points) >= 6:
            return [item[0] for item in thermocouple_points], [item[1] for item in thermocouple_points], "thermocouple"
        camera_points = [
            (timestamp, value)
            for timestamp, value in zip(self.temperature_timestamps, self.temperatures)
            if not math.isnan(value)
        ]
        if len(camera_points) >= 6:
            return [item[0] for item in camera_points], [item[1] for item in camera_points], "temperature"
        return [], [], "temperature"

    def _get_heating_profile(self) -> HeatingProfileResult:
        if not self._heating_profile_enabled:
            return HeatingProfileResult([], [], "temperature", ())
        if not self._heating_profile_dirty:
            return self._heating_profile_result
        source_timestamps, source_values, source_series = self._temperature_source_data()
        self._heating_profile_result = build_heating_profile(
            source_timestamps,
            source_values,
            source_series=source_series,
        )
        self._heating_profile_dirty = False
        return self._heating_profile_result

    def _draw_heating_profile(self, axis):
        if not self.is_series_visible("heating_profile"):
            return None
        profile = self._get_heating_profile()
        if not profile.has_data:
            return None
        return self._draw_series(
            axis,
            list(profile.timestamps),
            list(profile.temperatures),
            "heating_profile",
            "Профиль нагрева",
        )

    def _normalize_mass_values(self, values: list[float]) -> list[float]:
        baseline = self._first_finite(values)
        if baseline in {None, 0.0}:
            return values
        return [math.nan if math.isnan(value) else (value / baseline) * 100.0 for value in values]

    def _draw_markers(self, axis) -> None:
        marker_data = self._marker_positions()
        if marker_data is None:
            return
        if len(marker_data) == 2:
            (_, start_time, start_value), (_, end_time, end_value) = marker_data
            axis.plot(
                [start_time, end_time],
                [start_value, end_value],
                linestyle="--",
                linewidth=max(1.2, 1.5 * self._scale),
                color=self._plot_theme.grid,
                alpha=0.9,
                zorder=4,
            )
        colors = {"A": "#34C759", "B": "#FF9F0A"}
        for label, timestamp, value in marker_data:
            color = colors.get(label, self._plot_theme.title)
            marker_line = axis.axvline(timestamp, linestyle=":", linewidth=max(0.9, 1.1 * self._scale), color=color, alpha=0.55, zorder=3)
            setattr(marker_line, "_df_ignore_scale", True)
            axis.scatter([timestamp], [value], color=color, edgecolors=self._plot_theme.legend_edge, linewidths=0.8, s=max(62, 78 * self._scale), zorder=6)
            axis.annotate(
                label,
                xy=(timestamp, value),
                xytext=(0, 12 if label == "A" else -18),
                textcoords="offset points",
                ha="center",
                fontsize=max(10, self._tick_size + 1),
                fontweight="bold",
                color=self._plot_theme.title,
                bbox={"boxstyle": "round,pad=0.25", "fc": self._plot_theme.legend_bg, "ec": color, "alpha": 0.98},
            )

    def _draw_stage_overlay(self, mass_axis, temp_axis) -> None:
        bounds = self._stage_bounds()
        if bounds is None:
            return
        start_idx, end_idx = bounds
        start_time = self.mass_timestamps[start_idx]
        end_time = self.mass_timestamps[end_idx]
        for axis in (mass_axis, temp_axis):
            if axis is not None:
                axis.axvspan(start_time, end_time, color="#E8B04A", alpha=0.12)

    def _marker_positions(self) -> list[tuple[str, datetime, float]] | None:
        values = self.mass_series_values()
        self._ensure_marker_indices()
        first_idx = self._marker_indices.get("A")
        last_idx = self._marker_indices.get("B")
        if first_idx is None or last_idx is None:
            return None
        if not (0 <= first_idx < len(values)) or math.isnan(values[first_idx]):
            first_idx = self._first_finite_index(values)
        if not (0 <= last_idx < len(values)) or math.isnan(values[last_idx]):
            last_idx = self._last_finite_index(values)
        if first_idx is None or last_idx is None:
            return None
        self._marker_indices["A"] = first_idx
        self._marker_indices["B"] = last_idx
        if first_idx == last_idx:
            return [("A", self.mass_timestamps[first_idx], values[first_idx])]
        return [("A", self.mass_timestamps[first_idx], values[first_idx]), ("B", self.mass_timestamps[last_idx], values[last_idx])]

    def _marker_metrics(self) -> dict[str, str]:
        raw_mass = list(self.masses)
        temps = list(self.temperatures)
        self._ensure_marker_indices()
        first_idx = self._marker_indices.get("A")
        last_idx = self._marker_indices.get("B")
        if first_idx is not None and (first_idx >= len(raw_mass) or math.isnan(raw_mass[first_idx])):
            first_idx = self._first_finite_index(raw_mass)
        if last_idx is not None and (last_idx >= len(raw_mass) or math.isnan(raw_mass[last_idx])):
            last_idx = self._last_finite_index(raw_mass)
        if first_idx is None or last_idx is None or first_idx == last_idx:
            return {}
        start_mass = raw_mass[first_idx]
        end_mass = raw_mass[last_idx]
        start_temp = temps[first_idx] if first_idx < len(temps) else math.nan
        end_temp = temps[last_idx] if last_idx < len(temps) else math.nan
        start_time = self.mass_timestamps[first_idx]
        end_time = self.mass_timestamps[last_idx]
        delta_mass = end_mass - start_mass
        delta_percent = ((delta_mass / start_mass) * 100.0) if start_mass and not math.isnan(start_mass) else math.nan
        delta_temp = end_temp - start_temp if not math.isnan(start_temp) and not math.isnan(end_temp) else math.nan
        delta_seconds = abs((end_time - start_time).total_seconds())
        return {
            "delta_mass": f"{delta_mass:.3f} г",
            "delta_mass_percent": f"{delta_percent:.2f} %",
            "delta_temperature": f"{delta_temp:.1f} °C" if not math.isnan(delta_temp) else "--",
            "delta_time": f"{delta_seconds:.1f} с",
        }

    def _ensure_marker_indices(self) -> None:
        values = list(self.masses)
        first_idx = self._first_finite_index(values)
        last_idx = self._last_finite_index(values)
        if first_idx is None or last_idx is None:
            self._marker_indices.clear()
            return
        self._marker_indices.setdefault("A", first_idx)
        self._marker_indices.setdefault("B", last_idx)
        for label in ("A", "B"):
            index = self._marker_indices.get(label)
            if index is None or index >= len(values) or math.isnan(values[index]):
                self._marker_indices[label] = first_idx if label == "A" else last_idx

    def _nearest_mass_index_by_x(self, x_value: float) -> int | None:
        if not self.mass_timestamps:
            return None
        target = mdates.num2date(x_value).replace(tzinfo=None)
        candidates = [
            (idx, abs((timestamp.replace(tzinfo=None) - target).total_seconds()))
            for idx, timestamp in enumerate(self.mass_timestamps)
            if idx < len(self.masses) and not math.isnan(self.masses[idx])
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[1])[0]

    def _pick_marker_label(self, event) -> str | None:
        marker_data = self._marker_positions()
        if marker_data is None or event.inaxes is None:
            return None
        for label, timestamp, value in marker_data:
            px, py = event.inaxes.transData.transform((mdates.date2num(timestamp), value))
            distance = math.hypot(px - event.x, py - event.y)
            if distance <= max(22.0, 26.0 * self._scale):
                return label
        return None

    def _dtg_extreme(self) -> str:
        dtg = self._dtg_values(list(self.mass_timestamps), self.mass_series_values())
        finite = [value for value in dtg if not math.isnan(value)]
        if not finite:
            return "--"
        peak = max(finite, key=lambda item: abs(item))
        return f"{peak:.4f} {self._dtg_unit()}"

    def _stage_bounds(self) -> tuple[int, int] | None:
        dtg = self._dtg_values(list(self.mass_timestamps), self.mass_series_values())
        finite = [(index, abs(value)) for index, value in enumerate(dtg) if not math.isnan(value)]
        if len(finite) < 3:
            return None
        peak_index, peak_value = max(finite, key=lambda item: item[1])
        threshold = peak_value * 0.18
        start = peak_index
        end = peak_index
        while start > 0 and not math.isnan(dtg[start - 1]) and abs(dtg[start - 1]) >= threshold:
            start -= 1
        while end < len(dtg) - 1 and not math.isnan(dtg[end + 1]) and abs(dtg[end + 1]) >= threshold:
            end += 1
        return start, end

    def _stage_window(self) -> str:
        bounds = self._stage_bounds()
        if bounds is None:
            return "--"
        start, end = bounds
        return f"{self.mass_timestamps[start].strftime('%H:%M:%S')} - {self.mass_timestamps[end].strftime('%H:%M:%S')}"

    def _first_finite(self, values: list[float]) -> float | None:
        for value in values:
            if not math.isnan(value):
                return value
        return None

    def _first_finite_index(self, values: list[float]) -> int | None:
        for index, value in enumerate(values):
            if not math.isnan(value):
                return index
        return None

    def _last_finite_index(self, values: list[float]) -> int | None:
        for index in range(len(values) - 1, -1, -1):
            if not math.isnan(values[index]):
                return index
        return None

    def _sync_time_limits(self) -> None:
        if not self.axes:
            return
        if len(self.timestamps) == 1:
            center = self.timestamps[0]
            left = center - timedelta(seconds=max(1.0, self._x_pad_seconds))
            right = center + timedelta(seconds=max(1.0, self._x_pad_seconds))
        elif len(self.timestamps) > 1:
            left = self.timestamps[0] - timedelta(seconds=self._x_pad_seconds)
            right = self.timestamps[-1] + timedelta(seconds=self._x_pad_seconds)
        else:
            return
        for axis in self.axes:
            axis.set_xlim(left, right)

    def _apply_scale_limits(self) -> None:
        self._sync_time_limits()
        if not self.axes:
            return
        if self._autoscale_enabled:
            for axis in self.axes:
                self._apply_y_headroom(axis)
            return
        self._apply_manual_limits()

    def _apply_y_headroom(self, axis) -> None:
        role = self._axis_role(axis)
        lines = [line for line in axis.get_lines() if not getattr(line, "_df_ignore_scale", False)]
        finite_values: list[float] = []
        for line in lines:
            for value in line.get_ydata():
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isnan(numeric):
                    finite_values.append(numeric)
        if not finite_values:
            return
        ymin = min(finite_values)
        ymax = max(finite_values)
        headroom = self._y_headroom
        if ymin == ymax:
            ymin -= max(1.0, headroom * 0.2)
            ymax += max(1.0, headroom)
        else:
            ymin -= max(0.0, headroom * 0.05)
            ymax += headroom
        ymin, ymax = self._stabilize_y_limits(role, ymin, ymax)
        axis.set_ylim(ymin, ymax)

    def _apply_manual_limits(self) -> None:
        if not self.timestamps:
            return
        right = self.timestamps[-1] + timedelta(seconds=self._x_pad_seconds)
        left = right - timedelta(seconds=self._manual_x_seconds)
        for axis in self.axes:
            axis.set_xlim(left, right)
            lines = [line for line in axis.get_lines() if not getattr(line, "_df_ignore_scale", False)]
            finite_values: list[float] = []
            for line in lines:
                x_values = line.get_xdata()
                y_values = line.get_ydata()
                for x_val, y_val in zip(x_values, y_values):
                    try:
                        x_num = float(x_val)
                        y_num = float(y_val)
                    except (TypeError, ValueError):
                        continue
                    if math.isnan(y_num):
                        continue
                    point_time = mdates.num2date(x_num).replace(tzinfo=None)
                    if left <= point_time <= right:
                        finite_values.append(y_num)
            if not finite_values:
                continue
            ymax = max(finite_values) + self._y_headroom
            ymin = ymax - self._manual_y_span
            if ymin >= ymax:
                ymin = ymax - max(1.0, self._manual_y_span)
            axis.set_ylim(ymin, ymax)

    def _series_values(self, values: list[float]) -> list[float]:
        return self._smooth_values(values) if self._render_mode == self.RENDER_SMOOTH else values

    def mass_series_values(self) -> list[float]:
        values = list(self.masses)
        if self._normalization_enabled:
            values = self._normalize_mass_values(values)
        return self._smooth_values(values) if self._render_mode == self.RENDER_SMOOTH else values

    def _delta_values(self, values: list[float]) -> list[float]:
        result: list[float] = []
        previous: float | None = None
        for value in values:
            if math.isnan(value):
                result.append(math.nan)
                previous = None
                continue
            result.append(math.nan if previous is None else value - previous)
            previous = value
        return self._smooth_values(result) if self._render_mode == self.RENDER_SMOOTH else result

    def _dtg_values(self, timestamps: list[datetime], values: list[float]) -> list[float]:
        result: list[float] = []
        previous_value: float | None = None
        previous_time: datetime | None = None
        for timestamp, value in zip(timestamps, values):
            if math.isnan(value):
                result.append(math.nan)
                previous_value = None
                previous_time = None
                continue
            if previous_value is None or previous_time is None:
                result.append(math.nan)
            else:
                delta_minutes = (timestamp - previous_time).total_seconds() / 60.0
                result.append(math.nan if delta_minutes <= 0 else (value - previous_value) / delta_minutes)
            previous_value = value
            previous_time = timestamp
        return self._smooth_values(result) if self._render_mode == self.RENDER_SMOOTH else result

    def _append_series_sample(
        self,
        target_timestamps: deque[datetime],
        target_values: deque[float],
        raw_timestamp: str | None,
        raw_value: float | None,
    ) -> None:
        if raw_timestamp is None:
            return
        try:
            timestamp = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            return
        if target_timestamps and target_timestamps[-1] == timestamp:
            return
        target_timestamps.append(timestamp)
        target_values.append(raw_value if raw_value is not None else math.nan)

    def _smooth_values(self, values: list[float], window: int = 5) -> list[float]:
        if window <= 1:
            return values
        result: list[float] = []
        for index in range(len(values)):
            chunk = [value for value in values[max(0, index - window + 1): index + 1] if not math.isnan(value)]
            result.append(sum(chunk) / len(chunk) if chunk else math.nan)
        return result

    def _render_style(self) -> dict[str, float | str]:
        if self._render_mode == self.RENDER_POINTS:
            return {"marker": "o", "markersize": max(3.8, 4.6 * self._scale), "alpha": 0.9}
        return {"marker": "", "markersize": 0.0, "alpha": 0.95}

    def _zoom_axes(self, factor: float) -> None:
        if factor <= 0 or not self.axes:
            return
        self._viewport_locked = True
        for axis in self.axes:
            low, high = axis.get_ylim()
            delta = 1.0 if low == high else (high - low) * factor / 2.0
            center = (low + high) / 2.0
            axis.set_ylim(center - delta, center + delta)
        if self.ax_main is not None:
            left_num, right_num = self.ax_main.get_xlim()
            center_num = (left_num + right_num) / 2.0
            half_span = max((right_num - left_num) * factor / 2.0, 1e-6)
            self.ax_main.set_xlim(center_num - half_span, center_num + half_span)
        self._capture_current_viewport()
        self.canvas.draw_idle()

    def _on_scroll(self, event) -> None:
        if not self._zoom_active or event.inaxes is None:
            return
        if getattr(event, "button", "") == "up":
            self._zoom_axes(0.88)
        elif getattr(event, "button", "") == "down":
            self._zoom_axes(1.14)

    def _set_axis_role(self, axis, role: str) -> None:
        setattr(axis, "_df_role", role)

    def _axis_role(self, axis) -> str:
        return str(getattr(axis, "_df_role", "mass"))

    def _ensure_cursor_guides(self, axis):
        guides = self._cursor_guides.get(axis)
        if guides is not None:
            return guides
        vline = axis.axvline(0.0, color=self._plot_theme.spine, linestyle=":", linewidth=1.0, alpha=0.9, visible=False)
        hline = axis.axhline(0.0, color=self._plot_theme.spine, linestyle=":", linewidth=1.0, alpha=0.9, visible=False)
        setattr(vline, "_df_ignore_scale", True)
        setattr(hline, "_df_ignore_scale", True)
        label = axis.annotate("", xy=(0, 0), xytext=(12, 12), textcoords="offset points", fontsize=max(8, self._tick_size), color=self._plot_theme.legend_text, bbox={"boxstyle": "round,pad=0.2", "fc": self._plot_theme.legend_bg, "ec": self._plot_theme.legend_edge, "alpha": 0.96}, visible=False)
        self._cursor_guides[axis] = (vline, hline, label)
        return self._cursor_guides[axis]

    def _hide_cursor_guides(self) -> None:
        for vline, hline, label in self._cursor_guides.values():
            vline.set_visible(False)
            hline.set_visible(False)
            label.set_visible(False)

    def _cursor_text(self, axis, x_value: float, y_value: float) -> str:
        timestamp = mdates.num2date(x_value)
        role = self._axis_role(axis)
        if role == "dtg":
            unit = self._dtg_unit()
        elif "temp" in role:
            unit = "°C"
        else:
            unit = self._mass_unit()
        return f"{timestamp.strftime('%H:%M:%S')}\n{y_value:.3f} {unit}"

    def _draw_cursor_anchors(self) -> None:
        for anchor in self._cursor_anchor_points:
            axis = next((item for item in self.axes if self._axis_role(item) == anchor["role"]), None)
            if axis is None:
                continue
            axis.scatter([anchor["x"]], [anchor["y"]], color=self._plot_theme.title, s=18, zorder=6)
            axis.annotate(str(anchor["text"]), xy=(anchor["x"], anchor["y"]), xytext=(10, -10), textcoords="offset points", fontsize=max(8, self._tick_size), color=self._plot_theme.legend_text, bbox={"boxstyle": "round,pad=0.2", "fc": self._plot_theme.legend_bg, "ec": self._plot_theme.legend_edge, "alpha": 0.96})

    def _on_mouse_move(self, event) -> None:
        if self._markers_enabled and event.inaxes is not None and event.x is not None and event.y is not None:
            picked = self._pick_marker_label(event)
            try:
                self.canvas_widget.configure(cursor="hand2" if picked is not None or self._active_marker_label else "")
            except Exception:
                pass
        if self._markers_enabled and self._active_marker_label and event.inaxes is not None and event.xdata is not None:
            nearest_idx = self._nearest_mass_index_by_x(event.xdata)
            if nearest_idx is not None:
                self._marker_indices[self._active_marker_label] = nearest_idx
                self._render_current_view()
            return
        elif not self._cursor_probe_enabled:
            try:
                self.canvas_widget.configure(cursor="")
            except Exception:
                pass
        if not self._cursor_probe_enabled or event.inaxes is None or event.xdata is None or event.ydata is None:
            self._hide_cursor_guides()
            self.canvas.draw_idle()
            return
        self._hide_cursor_guides()
        vline, hline, label = self._ensure_cursor_guides(event.inaxes)
        vline.set_xdata([event.xdata, event.xdata])
        hline.set_ydata([event.ydata, event.ydata])
        label.xy = (event.xdata, event.ydata)
        label.set_text(self._cursor_text(event.inaxes, event.xdata, event.ydata))
        vline.set_visible(True)
        hline.set_visible(True)
        label.set_visible(True)
        self.canvas.draw_idle()

    def _on_axes_leave(self, _event) -> None:
        if self._cursor_probe_enabled:
            self._hide_cursor_guides()
            self.canvas.draw_idle()

    def _on_mouse_click(self, event) -> None:
        if self._markers_enabled and event.inaxes is not None and event.xdata is not None:
            picked = self._pick_marker_label(event)
            if getattr(event, "button", None) == 3 and picked is not None:
                self._marker_indices.clear()
                self._active_marker_label = None
                self._ensure_marker_indices()
                self._render_current_view()
                return
            if picked is not None:
                self._active_marker_label = picked
                nearest_idx = self._nearest_mass_index_by_x(event.xdata)
                if nearest_idx is not None:
                    self._marker_indices[picked] = nearest_idx
                    self._render_current_view()
                return
        if not self._cursor_probe_enabled or event.inaxes is None or event.xdata is None or event.ydata is None:
            return
        self._cursor_anchor_points.append({"role": self._axis_role(event.inaxes), "x": event.xdata, "y": event.ydata, "text": self._cursor_text(event.inaxes, event.xdata, event.ydata).replace("\n", " | ")})
        self._render_current_view()

    def _on_mouse_release(self, _event) -> None:
        self._active_marker_label = None
        if self._zoom_active or self._pan_active or self._viewport_locked:
            self._capture_current_viewport()

    def _reset_toolbar_modes(self) -> None:
        if self._zoom_active:
            self._toolbar.zoom()
            self._zoom_active = False
        if self._pan_active:
            self._toolbar.pan()
            self._pan_active = False

    def _capture_current_viewport(self) -> None:
        if self.ax_main is not None:
            left, right = self.ax_main.get_xlim()
            self._saved_xlim = (float(left), float(right))
        self._saved_ylim_by_role = {}
        for axis in self.axes:
            low, high = axis.get_ylim()
            self._saved_ylim_by_role[self._axis_role(axis)] = (float(low), float(high))

    def _apply_saved_viewport(self) -> None:
        if self._saved_xlim is not None:
            for axis in self.axes:
                axis.set_xlim(*self._saved_xlim)
        for axis in self.axes:
            saved = self._saved_ylim_by_role.get(self._axis_role(axis))
            if saved is not None:
                axis.set_ylim(*saved)

    def _stabilize_y_limits(self, role: str, ymin: float, ymax: float) -> tuple[float, float]:
        current = self._sticky_y_limits.get(role)
        if current is None:
            self._sticky_y_limits[role] = (ymin, ymax)
            return ymin, ymax
        low, high = current
        margin = max(1.0, self._y_headroom * 0.35)
        shrink_margin = margin * 3.0
        if ymax > high - margin:
            high = ymax + margin * 0.5
        elif ymax < high - shrink_margin:
            high = ymax + margin
        if ymin < low + margin:
            low = ymin - margin * 0.5
        elif ymin > low + shrink_margin:
            low = ymin - margin
        if low >= high:
            high = low + max(1.0, self._manual_y_span)
        self._sticky_y_limits[role] = (low, high)
        return low, high

    @property
    def _label_size(self) -> int:
        return max(10, int(10 * self._scale))

    @property
    def _tick_size(self) -> int:
        return max(9, int(9 * self._scale))

    @property
    def _title_size(self) -> int:
        return max(13, int(15 * self._scale))

    @staticmethod
    def _last_finite(values: list[float]) -> float | None:
        for value in reversed(values):
            if not math.isnan(value):
                return value
        return None
