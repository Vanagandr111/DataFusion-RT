from __future__ import annotations

import tkinter as tk

from app.theme import ThemePalette
from app.ui_support.widgets.tooltip import ToolTip


class MetricCard(tk.Frame):
    def __init__(
        self,
        master,
        title: str,
        accent_role: str,
        *,
        value_size: int,
        unit_size: int = 22,
    ) -> None:
        super().__init__(master, bd=0)
        self.title_text = title
        self.accent_role = accent_role
        self.value_size = value_size
        self.unit_size = unit_size
        self.default_border = "#243140"
        self._action_bg = ""
        self._action_fg = ""

        self.top_bar = tk.Frame(self, height=4, bd=0)
        self.top_bar.pack(fill="x", side="top")

        self.body = tk.Frame(self, bd=0)
        self.body.pack(fill="both", expand=True)

        self.title_label = tk.Label(self.body, anchor="w")
        self.title_label.pack(fill="x")

        self.value_row = tk.Frame(self.body, bd=0)
        self.value_row.pack(fill="x", pady=(6, 3))
        self.value_row.grid_columnconfigure(0, weight=1)
        self.value_label = tk.Label(self.value_row, text="--", anchor="w")
        self.value_label.grid(row=0, column=0, sticky="ew")
        self.unit_label = tk.Label(self.value_row, text="", anchor="sw")
        self.unit_label.grid(row=0, column=1, sticky="sw", padx=(6, 0), pady=(0, 2))

        self.subtitle_label = tk.Label(self.body, anchor="w", justify="left")
        self.subtitle_label.pack(fill="x")
        self.footer_row = tk.Frame(self.body, bd=0)
        self.footer_row.pack(fill="x", pady=(4, 0))
        self.footer_row.grid_columnconfigure(0, weight=1)
        self.secondary_label = tk.Label(self.footer_row, anchor="w", justify="left")
        self.secondary_label.grid(row=0, column=0, sticky="ew")
        self.action_button = tk.Button(
            self.footer_row,
            text="",
            bd=0,
            relief="flat",
            padx=4,
            pady=1,
            cursor="hand2",
            takefocus=0,
        )
        self.action_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.action_button.grid_remove()
        self._action_tooltip: ToolTip | None = None

    def apply_theme(self, palette: ThemePalette, scale: float) -> None:
        accent = getattr(palette, self.accent_role, palette.accent)
        self.default_border = palette.border
        self._action_bg = accent
        self._action_fg = palette.app_bg
        self.configure(
            bg=palette.card_bg, highlightthickness=1, highlightbackground=palette.border
        )
        self.top_bar.configure(bg=accent)
        self.body.configure(
            bg=palette.card_bg,
            padx=max(12, int(14 * scale)),
            pady=max(10, int(12 * scale)),
        )
        self.value_row.configure(bg=palette.card_bg)
        self.title_label.configure(
            bg=palette.card_bg,
            fg=palette.subtext,
            text=self.title_text,
            font=("Segoe UI Semibold", max(12, int(13 * scale))),
        )
        value_font_size = max(
            20, min(int(self.value_size * scale), self.value_size + 2)
        )
        unit_font_size = max(9, min(int(self.unit_size * scale), self.unit_size + 1))
        subtitle_font_size = max(10, min(int(12 * scale), 12))
        self.value_label.configure(
            bg=palette.card_bg,
            fg=palette.text,
            font=("Bahnschrift SemiBold", value_font_size),
        )
        self.unit_label.configure(
            bg=palette.card_bg,
            fg=palette.subtext,
            font=("Segoe UI Semibold", unit_font_size),
        )
        self.subtitle_label.configure(
            bg=palette.card_bg,
            fg=palette.subtext,
            font=("Segoe UI", subtitle_font_size),
        )
        self.footer_row.configure(bg=palette.card_bg)
        self.secondary_label.configure(
            bg=palette.card_bg,
            fg=palette.subtext,
            font=("Segoe UI", max(9, min(int(11 * scale), 11))),
        )
        self.action_button.configure(
            bg=palette.card_bg,
            fg=accent,
            activebackground=palette.card_bg,
            activeforeground=accent,
            font=("Segoe UI Symbol", max(10, min(int(13 * scale), 13)), "bold"),
            highlightthickness=0,
            highlightbackground=palette.card_bg,
            highlightcolor=palette.card_bg,
            bd=0,
            relief="flat",
        )
        if self.action_button.winfo_ismapped():
            self.action_button.configure(
                bg=palette.card_bg,
                fg=accent,
                activebackground=palette.card_bg,
                activeforeground=accent,
                padx=max(6, int(7 * scale)),
                pady=max(3, int(4 * scale)),
            )

    def set_value(self, value: str, *, unit: str = "", subtitle: str = "") -> None:
        self.value_label.configure(text=value)
        self.unit_label.configure(text=unit)
        self.subtitle_label.configure(text=subtitle)

    def set_secondary(self, text: str = "") -> None:
        self.secondary_label.configure(text=text)

    def configure_action(
        self,
        *,
        text: str = "",
        command=None,
        image=None,
        tooltip: str | None = None,
        visible: bool = False,
    ) -> None:
        self.action_button.configure(text=text, command=command, image=image, compound="center")
        self.action_button.image = image
        if tooltip:
            self._action_tooltip = ToolTip(self.action_button, tooltip)
        if visible:
            self.action_button.grid()
            self.action_button.configure(
                bg=self.cget("bg"),
                fg=self._action_bg or self.action_button.cget("fg"),
                activebackground=self.cget("bg"),
                activeforeground=self._action_bg or self.action_button.cget("activeforeground"),
            )
        else:
            self.action_button.grid_remove()

    def pulse(self, color: str) -> None:
        self.configure(highlightbackground=color)
        self.after(260, lambda: self.configure(highlightbackground=self.default_border))
