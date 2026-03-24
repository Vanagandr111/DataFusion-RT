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
from app.theme import PlotTheme


class LivePlotter:
    VIEW_COMBINED = "combined"
    VIEW_SPLIT = "split"
    VIEW_MASS = "mass"
    VIEW_TEMP = "temp"
    VIEW_DELTA = "delta"

    RENDER_LINE = "line"
    RENDER_POINTS = "points"
    RENDER_SMOOTH = "smooth"

    def __init__(
        self,
        master: Misc,
        max_points: int,
        *,
        plot_theme: PlotTheme,
        scale: float = 1.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self._plot_theme = plot_theme
        self._scale = scale
        self._zoom_active = False
        self._pan_active = False
        self._view_mode = self.VIEW_COMBINED
        self._render_mode = self.RENDER_LINE

        self.timestamps: deque[datetime] = deque(maxlen=max_points)
        self.masses: deque[float] = deque(maxlen=max_points)
        self.temperatures: deque[float] = deque(maxlen=max_points)

        self.container = ttk.Frame(master, style="Card.TFrame")
        self.figure = Figure(figsize=(10.4, 5.9), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.container)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.pack(fill="both", expand=True)

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

    def set_max_points(self, max_points: int) -> None:
        if max_points <= 0 or max_points == self.timestamps.maxlen:
            return

        self.timestamps = deque(self.timestamps, maxlen=max_points)
        self.masses = deque(self.masses, maxlen=max_points)
        self.temperatures = deque(self.temperatures, maxlen=max_points)
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

    def update(self, record: MeasurementRecord) -> None:
        timestamp = datetime.fromisoformat(record.timestamp)
        self.timestamps.append(timestamp)
        self.masses.append(record.mass if record.mass is not None else math.nan)
        self.temperatures.append(record.furnace_pv if record.furnace_pv is not None else math.nan)
        self._render_current_view()

    def clear(self) -> None:
        self.timestamps.clear()
        self.masses.clear()
        self.temperatures.clear()
        self._render_current_view()

    def apply_theme(self, plot_theme: PlotTheme, *, scale: float | None = None) -> None:
        self._plot_theme = plot_theme
        if scale is not None:
            self._scale = scale
        self._render_current_view()

    def toggle_zoom(self) -> bool:
        if self._pan_active:
            self._toolbar.pan()
            self._pan_active = False
        self._toolbar.zoom()
        self._zoom_active = not self._zoom_active
        return self._zoom_active

    def toggle_pan(self) -> bool:
        if self._zoom_active:
            self._toolbar.zoom()
            self._zoom_active = False
        self._toolbar.pan()
        self._pan_active = not self._pan_active
        return self._pan_active

    def zoom_in(self) -> None:
        self._zoom_axes(0.8)

    def zoom_out(self) -> None:
        self._zoom_axes(1.25)

    def reset_view(self) -> None:
        self._reset_toolbar_modes()
        self._toolbar.home()
        self.autoscale(draw=True)

    def autoscale(self, *, draw: bool = True) -> None:
        if not self.axes:
            return

        for axis in self.axes:
            axis.relim()
            axis.autoscale_view()

        self._sync_time_limits()
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
        self.figure.clear()
        self.axes = []
        self.ax_main = None
        self.ax_secondary = None

        if self._view_mode == self.VIEW_COMBINED:
            self._draw_combined_view()
        elif self._view_mode == self.VIEW_SPLIT:
            self._draw_split_view()
        elif self._view_mode == self.VIEW_MASS:
            self._draw_single_view(series="mass")
        elif self._view_mode == self.VIEW_TEMP:
            self._draw_single_view(series="temp")
        else:
            self._draw_delta_view()

        self.figure.subplots_adjust(left=0.08, right=0.88, top=0.9, bottom=0.12, hspace=0.14)
        self.canvas.draw_idle()

    def _draw_combined_view(self) -> None:
        ax_mass = self.figure.add_subplot(111)
        ax_temp = ax_mass.twinx()
        self.ax_main = ax_mass
        self.ax_secondary = ax_temp
        self.axes = [ax_mass, ax_temp]

        self._style_axis(ax_mass, ylabel="Масса (г)", color=self._plot_theme.mass_label)
        self._style_axis(ax_temp, ylabel="Температура (°C)", color=self._plot_theme.temp_label, secondary=True)
        ax_mass.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)

        mass_values = self._series_values(list(self.masses))
        temp_values = self._series_values(list(self.temperatures))
        handles = [
            self._draw_series(ax_mass, self.timestamps, mass_values, color=self._plot_theme.mass_line, label=self._legend_label("Масса", mass_values, "г")),
            self._draw_series(ax_temp, self.timestamps, temp_values, color=self._plot_theme.temp_line, label=self._legend_label("Температура", temp_values, "°C")),
        ]
        self._style_time_axis(ax_mass)
        self._style_legend(ax_mass, handles)
        self.figure.suptitle("Общий график измерений", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_split_view(self) -> None:
        ax_mass = self.figure.add_subplot(211)
        ax_temp = self.figure.add_subplot(212, sharex=ax_mass)
        self.ax_main = ax_mass
        self.ax_secondary = ax_temp
        self.axes = [ax_mass, ax_temp]

        self._style_axis(ax_mass, ylabel="Масса (г)", color=self._plot_theme.mass_label)
        self._style_axis(ax_temp, ylabel="Температура (°C)", color=self._plot_theme.temp_label)
        ax_temp.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)

        mass_values = self._series_values(list(self.masses))
        temp_values = self._series_values(list(self.temperatures))
        handle_mass = self._draw_series(ax_mass, self.timestamps, mass_values, color=self._plot_theme.mass_line, label=self._legend_label("Масса", mass_values, "г"))
        handle_temp = self._draw_series(ax_temp, self.timestamps, temp_values, color=self._plot_theme.temp_line, label=self._legend_label("Температура", temp_values, "°C"))
        self._style_time_axis(ax_temp)
        ax_mass.tick_params(labelbottom=False)
        self._style_legend(ax_mass, [handle_mass])
        self._style_legend(ax_temp, [handle_temp])
        self.figure.suptitle("Раздельные графики: масса и температура", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_single_view(self, *, series: str) -> None:
        axis = self.figure.add_subplot(111)
        self.ax_main = axis
        self.axes = [axis]
        if series == "mass":
            values = self._series_values(list(self.masses))
            color = self._plot_theme.mass_line
            ylabel = "Масса (г)"
            title = "График массы"
            label = self._legend_label("Масса", values, "г")
        else:
            values = self._series_values(list(self.temperatures))
            color = self._plot_theme.temp_line
            ylabel = "Температура (°C)"
            title = "График температуры"
            label = self._legend_label("Температура", values, "°C")

        self._style_axis(axis, ylabel=ylabel, color=color)
        axis.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        handle = self._draw_series(axis, self.timestamps, values, color=color, label=label)
        self._style_time_axis(axis)
        self._style_legend(axis, [handle])
        self.figure.suptitle(title, fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_delta_view(self) -> None:
        ax_mass = self.figure.add_subplot(211)
        ax_temp = self.figure.add_subplot(212, sharex=ax_mass)
        self.ax_main = ax_mass
        self.ax_secondary = ax_temp
        self.axes = [ax_mass, ax_temp]

        mass_delta = self._delta_values(list(self.masses))
        temp_delta = self._delta_values(list(self.temperatures))
        self._style_axis(ax_mass, ylabel="ΔМасса (г)", color=self._plot_theme.mass_label)
        self._style_axis(ax_temp, ylabel="ΔТемпература (°C)", color=self._plot_theme.temp_label)
        ax_temp.set_xlabel("Время", color=self._plot_theme.x_label, fontsize=self._label_size)
        handle_mass = self._draw_series(ax_mass, self.timestamps, mass_delta, color=self._plot_theme.mass_line, label=self._legend_label("ΔМасса", mass_delta, "г"))
        handle_temp = self._draw_series(ax_temp, self.timestamps, temp_delta, color=self._plot_theme.temp_line, label=self._legend_label("ΔТемпература", temp_delta, "°C"))
        self._style_time_axis(ax_temp)
        ax_mass.axhline(0.0, color=self._plot_theme.spine, linewidth=1.0, linestyle="--")
        ax_temp.axhline(0.0, color=self._plot_theme.spine, linewidth=1.0, linestyle="--")
        ax_mass.tick_params(labelbottom=False)
        self._style_legend(ax_mass, [handle_mass])
        self._style_legend(ax_temp, [handle_temp])
        self.figure.suptitle("Изменение параметров во времени", fontsize=self._title_size, fontweight="bold", color=self._plot_theme.title)
        self.autoscale(draw=False)

    def _draw_series(self, axis, timestamps, values, *, color: str, label: str):
        style = self._render_style()
        line_kwargs = {
            "color": color,
            "label": label,
            "linewidth": style["linewidth"],
            "linestyle": style["linestyle"],
            "marker": style["marker"],
            "markersize": style["markersize"],
            "markerfacecolor": color,
            "markeredgewidth": 0.0,
            "alpha": style["alpha"],
        }
        (line,) = axis.plot(list(timestamps), values, **line_kwargs)
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
        legend = axis.legend(
            handles=handles,
            loc="upper left",
            facecolor=self._plot_theme.legend_bg,
            edgecolor=self._plot_theme.legend_edge,
            framealpha=0.95,
        )
        for text in legend.get_texts():
            text.set_color(self._plot_theme.legend_text)

    def _legend_label(self, base: str, values: list[float], unit: str) -> str:
        last_value = self._last_finite(values)
        if last_value is None:
            return f"{base}: -- {unit}"
        digits = 3 if unit == "г" else 1
        return f"{base}: {last_value:.{digits}f} {unit}"

    def _sync_time_limits(self) -> None:
        if not self.axes:
            return
        if len(self.timestamps) == 1:
            center = self.timestamps[0]
            left = center - timedelta(seconds=1)
            right = center + timedelta(seconds=1)
        elif len(self.timestamps) > 1:
            left = self.timestamps[0]
            right = self.timestamps[-1]
        else:
            return

        for axis in self.axes:
            axis.set_xlim(left, right)

    def _series_values(self, values: list[float]) -> list[float]:
        if self._render_mode == self.RENDER_SMOOTH:
            return self._smooth_values(values)
        return values

    def _delta_values(self, values: list[float]) -> list[float]:
        result: list[float] = []
        previous: float | None = None
        for value in values:
            if math.isnan(value):
                result.append(math.nan)
                previous = None
                continue
            if previous is None:
                result.append(math.nan)
            else:
                result.append(value - previous)
            previous = value
        if self._render_mode == self.RENDER_SMOOTH:
            return self._smooth_values(result)
        return result

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
            return {
                "linewidth": 0.0,
                "linestyle": "None",
                "marker": "o",
                "markersize": max(3.8, 4.6 * self._scale),
                "alpha": 0.9,
            }
        if self._render_mode == self.RENDER_SMOOTH:
            return {
                "linewidth": 2.6,
                "linestyle": "-",
                "marker": "",
                "markersize": 0.0,
                "alpha": 0.95,
            }
        return {
            "linewidth": 2.15,
            "linestyle": "-",
            "marker": "",
            "markersize": 0.0,
            "alpha": 0.95,
        }

    def _zoom_axes(self, factor: float) -> None:
        if factor <= 0 or not self.axes:
            return

        for axis in self.axes:
            low, high = axis.get_ylim()
            if low == high:
                delta = 1.0
            else:
                delta = (high - low) * factor / 2.0
            center = (low + high) / 2.0
            axis.set_ylim(center - delta, center + delta)

        if self.ax_main is not None:
            left_num, right_num = self.ax_main.get_xlim()
            center_num = (left_num + right_num) / 2.0
            half_span = max((right_num - left_num) * factor / 2.0, 1e-6)
            self.ax_main.set_xlim(center_num - half_span, center_num + half_span)
        self.canvas.draw_idle()

    def _reset_toolbar_modes(self) -> None:
        if self._zoom_active:
            self._toolbar.zoom()
            self._zoom_active = False
        if self._pan_active:
            self._toolbar.pan()
            self._pan_active = False

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
