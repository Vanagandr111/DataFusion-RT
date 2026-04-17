from __future__ import annotations

import tkinter as tk


class ToolTip:
    def __init__(self, widget, text: str, *, placement: str = "below") -> None:
        self.widget = widget
        self.text = text
        self.placement = placement
        self.tip_window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None) -> None:
        if self.tip_window or not self.text:
            return
        if self.placement == "left":
            x = self.widget.winfo_rootx() - 382
            y = self.widget.winfo_rooty()
        else:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip_window,
            text=self.text,
            justify="left",
            wraplength=360,
            bg="#FFFBEA",
            fg="#1F2937",
            relief="solid",
            bd=1,
            padx=12,
            pady=8,
            font=("Segoe UI", 10),
        )
        label.pack()

    def _hide(self, _event=None) -> None:
        if self.tip_window is not None:
            self.tip_window.destroy()
            self.tip_window = None
