from __future__ import annotations

from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


def build_right_panel(app) -> None:
    app.right_panel.grid_propagate(False)
    ttk.Label(
        app.right_panel, text="Лог и диагностика", style="CardTitle.TLabel"
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        app.right_panel,
        textvariable=app.diag_ports_var,
        style="CardText.TLabel",
        wraplength=int(360 * app.ui_scale),
    ).grid(row=1, column=0, sticky="w", pady=(app._pad_y(6), 0))
    ttk.Label(
        app.right_panel,
        textvariable=app.diag_last_sample_var,
        style="CardText.TLabel",
        wraplength=int(360 * app.ui_scale),
    ).grid(row=2, column=0, sticky="w", pady=(app._pad_y(6), 0))
    ttk.Label(
        app.right_panel,
        textvariable=app.diag_last_time_var,
        style="CardText.TLabel",
    ).grid(row=3, column=0, sticky="w", pady=(app._pad_y(6), 0))
    ttk.Label(
        app.right_panel,
        textvariable=app.diag_status_var,
        style="CardText.TLabel",
        wraplength=int(360 * app.ui_scale),
    ).grid(row=4, column=0, sticky="w", pady=(app._pad_y(6), app._pad_y(10)))

    log_actions = ttk.Frame(app.right_panel, style="Card.TFrame")
    log_actions.grid(row=5, column=0, sticky="ew", pady=(0, app._pad_y(8)))
    log_actions.grid_columnconfigure(0, weight=1)
    log_actions.grid_columnconfigure(1, weight=1)
    ttk.Button(
        log_actions,
        text="Сохранить TXT",
        style="Soft.TButton",
        command=app.save_runtime_log,
    ).grid(row=0, column=0, sticky="ew", padx=(0, app._pad_x(6)))
    ttk.Button(
        log_actions,
        text="Папка журналов",
        style="Soft.TButton",
        command=app.open_logs_folder,
    ).grid(row=0, column=1, sticky="ew", padx=(app._pad_x(6), 0))

    app.log_text = ScrolledText(
        app.right_panel,
        wrap="word",
        relief="flat",
        height=18,
    )
    app.log_text.grid(row=6, column=0, sticky="nsew")
    app.log_text.insert("end", "Журнал готов.\n")
    app.log_text.configure(state="disabled")
