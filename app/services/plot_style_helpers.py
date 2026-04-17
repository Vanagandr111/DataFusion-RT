from __future__ import annotations


def sanitize_series_style(style: dict[str, object] | None) -> dict[str, object]:
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


def series_style(plot_theme, series_key: str, overrides: dict[str, dict[str, object]]) -> dict[str, object]:
    styles = {
        "mass": {"color": plot_theme.mass_line, "linestyle": "-", "linewidth": 2.15},
        "temperature": {"color": plot_theme.temp_line, "linestyle": "-", "linewidth": 2.15},
        "thermocouple": {"color": "#F6A04D", "linestyle": "-", "linewidth": 2.15},
        "heating_profile": {"color": "#101010", "linestyle": "-", "linewidth": 2.85},
    }
    result = styles.get(series_key, styles["temperature"]).copy()
    if series_key != "heating_profile":
        result.update(sanitize_series_style(overrides.get(series_key, {})))
    return result


def series_display_name(view_mode: str, normalization_enabled: bool, series_key: str, dtg_view: str) -> str:
    if view_mode == dtg_view and series_key == "mass":
        return "DTG"
    names = {
        "mass": "Масса, %" if normalization_enabled else "Масса",
        "temperature": "t Камера PV",
        "thermocouple": "Термопара",
        "heating_profile": "Профиль нагрева",
    }
    return names.get(series_key, series_key)


def legend_label(base: str, values: list[float], unit: str, last_value: float | None) -> str:
    if last_value is None:
        return f"{base}: -- {unit}"
    digits = 1 if unit == "°C" else 2 if "%" in unit else 3
    return f"{base}: {last_value:.{digits}f} {unit}"
