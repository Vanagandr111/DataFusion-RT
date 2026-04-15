from __future__ import annotations

from dataclasses import dataclass
from tkinter import ttk


@dataclass(frozen=True, slots=True)
class PlotTheme:
    figure_bg: str
    axes_bg: str
    grid: str
    x_tick: str
    mass_line: str
    temp_line: str
    x_label: str
    mass_label: str
    temp_label: str
    spine: str
    title: str
    legend_bg: str
    legend_edge: str
    legend_text: str


@dataclass(frozen=True, slots=True)
class ThemePalette:
    name: str
    app_bg: str
    surface_bg: str
    card_bg: str
    card_alt_bg: str
    header_bg: str
    accent: str
    accent_dim: str
    heat: str
    heat_dim: str
    text: str
    subtext: str
    border: str
    success: str
    warning: str
    error: str
    disabled: str
    input_bg: str
    button_soft_bg: str
    button_soft_active: str
    tree_bg: str
    tree_selected: str
    plot: PlotTheme


LIGHT_PLOT = PlotTheme(
    figure_bg="#F6F8FB",
    axes_bg="#FFFFFF",
    grid="#CBD5E1",
    x_tick="#475467",
    mass_line="#0F766E",
    temp_line="#C2410C",
    x_label="#344054",
    mass_label="#0F766E",
    temp_label="#C2410C",
    spine="#D0D5DD",
    title="#101828",
    legend_bg="#FFFFFF",
    legend_edge="#D0D5DD",
    legend_text="#344054",
)

THEMES: dict[str, ThemePalette] = {
    "dark": ThemePalette(
        name="dark",
        app_bg="#0B1117",
        surface_bg="#121A22",
        card_bg="#161F29",
        card_alt_bg="#101820",
        header_bg="#0F1821",
        accent="#3ECFBC",
        accent_dim="#234A49",
        heat="#FF9B5E",
        heat_dim="#4B3528",
        text="#F4F7FA",
        subtext="#9AA7B6",
        border="#243140",
        success="#34D399",
        warning="#FBBF24",
        error="#F87171",
        disabled="#536273",
        input_bg="#0F1821",
        button_soft_bg="#1C2733",
        button_soft_active="#243142",
        tree_bg="#0F1821",
        tree_selected="#223447",
        plot=LIGHT_PLOT,
    ),
    "light": ThemePalette(
        name="light",
        app_bg="#F2F5F8",
        surface_bg="#E9EEF3",
        card_bg="#FFFFFF",
        card_alt_bg="#F8FAFC",
        header_bg="#E7EDF4",
        accent="#0F766E",
        accent_dim="#B8D8D2",
        heat="#C2410C",
        heat_dim="#E9C1AF",
        text="#111827",
        subtext="#667085",
        border="#D0D7E2",
        success="#15803D",
        warning="#B45309",
        error="#B42318",
        disabled="#98A2B3",
        input_bg="#FFFFFF",
        button_soft_bg="#EEF2F7",
        button_soft_active="#E2E8F0",
        tree_bg="#FFFFFF",
        tree_selected="#D9E8F7",
        plot=LIGHT_PLOT,
    ),
}


class ThemeManager:
    def __init__(self, theme_name: str = "dark") -> None:
        self._theme_name = self.normalize_theme_name(theme_name)

    @property
    def palette(self) -> ThemePalette:
        return THEMES[self._theme_name]

    @property
    def theme_name(self) -> str:
        return self._theme_name

    def set_theme(self, theme_name: str) -> ThemePalette:
        self._theme_name = self.normalize_theme_name(theme_name)
        return self.palette

    def normalize_theme_name(self, theme_name: str | None) -> str:
        value = (theme_name or "dark").strip().lower()
        return value if value in THEMES else "dark"

    def apply_ttk_styles(self, root, *, scale: float) -> ttk.Style:
        palette = self.palette
        style = ttk.Style(root)
        style.theme_use("clam")

        def size(value: int) -> int:
            return max(8, int(value * scale))

        style.configure("App.TFrame", background=palette.app_bg)
        style.configure("Header.TFrame", background=palette.header_bg)
        style.configure("Card.TFrame", background=palette.card_bg, relief="flat", borderwidth=0)
        style.configure("CardAlt.TFrame", background=palette.card_alt_bg, relief="flat", borderwidth=0)
        style.configure(
            "Headline.TLabel",
            background=palette.header_bg,
            foreground=palette.text,
            font=("Bahnschrift SemiBold", size(24)),
        )
        style.configure(
            "Subtitle.TLabel",
            background=palette.header_bg,
            foreground=palette.subtext,
            font=("Segoe UI", size(11)),
        )
        style.configure(
            "CardTitle.TLabel",
            background=palette.card_bg,
            foreground=palette.text,
            font=("Segoe UI Semibold", size(13)),
        )
        style.configure(
            "CardText.TLabel",
            background=palette.card_bg,
            foreground=palette.subtext,
            font=("Segoe UI", size(11)),
        )
        style.configure(
            "CardAltText.TLabel",
            background=palette.card_alt_bg,
            foreground=palette.subtext,
            font=("Segoe UI", size(11)),
        )
        style.configure(
            "Accent.TButton",
            font=("Segoe UI Semibold", size(10)),
            padding=(size(12), size(8)),
            background=palette.accent,
            foreground="#081016" if palette.name == "dark" else "#FFFFFF",
            borderwidth=0,
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#55E2CF" if palette.name == "dark" else "#0B8A80"), ("disabled", palette.accent_dim)],
            foreground=[("disabled", palette.disabled)],
        )
        style.configure(
            "Warm.TButton",
            font=("Segoe UI Semibold", size(10)),
            padding=(size(12), size(8)),
            background=palette.heat,
            foreground="#1B120D" if palette.name == "dark" else "#FFFFFF",
            borderwidth=0,
        )
        style.map(
            "Warm.TButton",
            background=[("active", "#FFB27A" if palette.name == "dark" else "#D65A21"), ("disabled", palette.heat_dim)],
            foreground=[("disabled", palette.disabled)],
        )
        style.configure(
            "Soft.TButton",
            font=("Segoe UI", size(11)),
            padding=(size(10), size(6)),
            background=palette.button_soft_bg,
            foreground=palette.text,
            bordercolor=palette.border,
        )
        style.map(
            "Soft.TButton",
            background=[("active", palette.button_soft_active), ("disabled", palette.button_soft_bg)],
            foreground=[("disabled", palette.disabled)],
            bordercolor=[("active", palette.accent), ("disabled", palette.border)],
        )
        style.configure(
            "SelectedSoft.TButton",
            font=("Segoe UI Semibold", size(10)),
            padding=(size(12), size(8)),
            background=palette.accent if palette.name == "dark" else "#D6F2EE",
            foreground="#081016" if palette.name == "dark" else "#0F3F3A",
            bordercolor=palette.accent,
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "SelectedSoft.TButton",
            background=[("active", "#55E2CF" if palette.name == "dark" else "#C6ECE6"), ("disabled", palette.accent_dim)],
            foreground=[("disabled", palette.disabled)],
            bordercolor=[("active", palette.accent), ("disabled", palette.border)],
        )
        style.configure(
            "SettingsAccent.TButton",
            font=("Segoe UI Semibold", size(12)),
            padding=(size(16), size(11)),
            background=palette.accent,
            foreground="#081016" if palette.name == "dark" else "#FFFFFF",
            borderwidth=0,
        )
        style.map(
            "SettingsAccent.TButton",
            background=[("active", "#55E2CF" if palette.name == "dark" else "#0B8A80"), ("disabled", palette.accent_dim)],
            foreground=[("disabled", palette.disabled)],
        )
        style.configure(
            "SettingsSoft.TButton",
            font=("Segoe UI Semibold", size(12)),
            padding=(size(16), size(11)),
            background=palette.button_soft_bg,
            foreground=palette.text,
            bordercolor=palette.border,
        )
        style.map(
            "SettingsSoft.TButton",
            background=[("active", palette.button_soft_active), ("disabled", palette.button_soft_bg)],
            foreground=[("disabled", palette.disabled)],
            bordercolor=[("active", palette.accent), ("disabled", palette.border)],
        )
        style.configure(
            "WindowIcon.TButton",
            font=("Segoe UI Symbol", size(11)),
            padding=(size(8), size(7)),
            background=palette.button_soft_bg,
            foreground=palette.text,
            bordercolor=palette.border,
        )
        style.map(
            "WindowIcon.TButton",
            background=[("active", palette.button_soft_active), ("disabled", palette.button_soft_bg)],
            foreground=[("disabled", palette.disabled)],
            bordercolor=[("active", palette.accent), ("disabled", palette.border)],
        )
        style.configure(
            "TNotebook",
            background=palette.app_bg,
            borderwidth=0,
            tabmargins=(size(4), size(4), size(4), 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=palette.button_soft_bg,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            padding=(size(18), size(10)),
            font=("Segoe UI Semibold", size(12)),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", palette.accent if palette.name == "dark" else "#DCEFEB"),
                ("active", palette.button_soft_active),
            ],
            foreground=[
                ("selected", "#081016" if palette.name == "dark" else "#0F3F3A"),
                ("active", palette.text),
            ],
            padding=[
                ("selected", (size(18), size(10))),
                ("active", (size(18), size(10))),
            ],
            bordercolor=[
                ("selected", palette.accent),
                ("active", palette.accent),
            ],
            lightcolor=[
                ("selected", palette.accent),
                ("active", palette.border),
            ],
            darkcolor=[
                ("selected", palette.accent),
                ("active", palette.border),
            ],
        )
        style.configure(
            "Compact.TNotebook",
            background=palette.app_bg,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "Compact.TNotebook.Tab",
            background=palette.button_soft_bg,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            padding=(size(14), size(4)),
            font=("Segoe UI Semibold", size(10)),
        )
        style.map(
            "Compact.TNotebook.Tab",
            background=[
                ("selected", palette.accent if palette.name == "dark" else "#DCEFEB"),
                ("active", palette.button_soft_active),
            ],
            foreground=[
                ("selected", "#081016" if palette.name == "dark" else "#0F3F3A"),
                ("active", palette.text),
            ],
            bordercolor=[
                ("selected", palette.accent),
                ("active", palette.accent),
            ],
            lightcolor=[
                ("selected", palette.accent),
                ("active", palette.border),
            ],
            darkcolor=[
                ("selected", palette.accent),
                ("active", palette.border),
            ],
        )
        style.configure(
            "Card.TCheckbutton",
            background=palette.card_bg,
            foreground=palette.text,
            font=("Segoe UI", size(11)),
            indicatorcolor=palette.input_bg,
            indicatorbackground=palette.input_bg,
            focuscolor=palette.card_bg,
            lightcolor=palette.card_bg,
            darkcolor=palette.card_bg,
            bordercolor=palette.border,
        )
        style.map(
            "Card.TCheckbutton",
            background=[
                ("active", palette.card_bg),
                ("selected", palette.card_bg),
                ("disabled", palette.card_bg),
            ],
            foreground=[
                ("disabled", palette.disabled),
                ("active", palette.text),
                ("selected", palette.text),
            ],
            indicatorcolor=[
                ("selected", palette.accent),
                ("disabled", palette.disabled),
                ("!selected", palette.input_bg),
            ],
            indicatorbackground=[
                ("selected", palette.accent),
                ("disabled", palette.disabled),
                ("!selected", palette.input_bg),
            ],
            focuscolor=[
                ("focus", palette.card_bg),
                ("!focus", palette.card_bg),
            ],
        )
        style.configure(
            "TEntry",
            fieldbackground=palette.input_bg,
            foreground=palette.text,
            insertcolor=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            font=("Segoe UI", size(11)),
            padding=(size(8), size(6)),
        )
        style.configure(
            "TCombobox",
            fieldbackground=palette.input_bg,
            background=palette.input_bg,
            foreground=palette.text,
            arrowcolor=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            font=("Segoe UI", size(11)),
            padding=(size(8), size(6)),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette.input_bg)],
            foreground=[("readonly", palette.text), ("disabled", palette.disabled)],
        )
        style.configure(
            "Treeview",
            rowheight=size(28),
            font=("Segoe UI", size(10)),
            fieldbackground=palette.tree_bg,
            background=palette.tree_bg,
            foreground=palette.text,
            bordercolor=palette.border,
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI Semibold", size(11)),
            background=palette.button_soft_bg,
            foreground=palette.text,
            bordercolor=palette.border,
        )
        style.configure(
            "Section.TLabelframe.Label",
            background=palette.card_bg,
            foreground=palette.text,
            font=("Segoe UI Semibold", size(12)),
        )
        style.map(
            "Treeview",
            background=[("selected", palette.tree_selected)],
            foreground=[("selected", palette.text)],
        )
        style.configure(
            "TScrollbar",
            background=palette.button_soft_bg,
            troughcolor=palette.card_alt_bg,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            arrowcolor=palette.text,
            gripcount=0,
            arrowsize=size(14),
            width=size(18),
            relief="flat",
        )
        style.map(
            "TScrollbar",
            background=[
                ("active", palette.accent if palette.name == "dark" else palette.button_soft_active),
                ("pressed", palette.accent if palette.name == "dark" else palette.accent),
            ],
            arrowcolor=[
                ("active", "#081016" if palette.name == "dark" else "#FFFFFF"),
                ("pressed", "#081016" if palette.name == "dark" else "#FFFFFF"),
            ],
        )
        return style
