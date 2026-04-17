from __future__ import annotations

from tkinter import ttk


def build_left_panel(app) -> None:
    ttk.Label(
        app.left_panel, text="Подключение устройств", style="CardTitle.TLabel"
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        app.left_panel, textvariable=app.port_status_var, style="CardText.TLabel"
    ).grid(row=1, column=0, sticky="w", pady=(app._pad_y(4), app._pad_y(10)))

    tree_frame = ttk.Frame(app.left_panel, style="Card.TFrame")
    tree_frame.grid(row=2, column=0, sticky="nsew")
    tree_frame.grid_rowconfigure(0, weight=1)
    tree_frame.grid_columnconfigure(0, weight=1)

    app.port_tree = ttk.Treeview(
        tree_frame, columns=("port", "kind", "desc"), show="headings", height=10
    )
    app.port_tree.heading("port", text="Порт")
    app.port_tree.heading("kind", text="Тип")
    app.port_tree.heading("desc", text="Устройство")
    app.port_tree.column("port", width=int(90 * app.ui_scale), anchor="w")
    app.port_tree.column("kind", width=int(150 * app.ui_scale), anchor="w")
    app.port_tree.column("desc", width=int(250 * app.ui_scale), anchor="w")
    app.port_tree.grid(row=0, column=0, sticky="nsew")
    app.port_tree.bind("<<TreeviewSelect>>", app._on_port_selected)

    scrollbar = ttk.Scrollbar(
        tree_frame, orient="vertical", command=app.port_tree.yview
    )
    scrollbar.grid(row=0, column=1, sticky="ns")
    app.port_tree.configure(yscrollcommand=scrollbar.set)

    app.assignment_label = ttk.Label(
        app.left_panel,
        textvariable=app.assignment_var,
        style="CardText.TLabel",
        wraplength=int(420 * app.ui_scale),
    )
    app.assignment_label.grid(row=3, column=0, sticky="w", pady=(app._pad_y(10), 0))

    ports_bar = ttk.Frame(app.left_panel, style="Card.TFrame")
    ports_bar.grid(row=4, column=0, sticky="ew", pady=(app._pad_y(12), 0))
    for idx in range(2):
        ports_bar.grid_columnconfigure(idx, weight=1)
    ttk.Button(
        ports_bar,
        text="Назначить как Весы",
        style="Soft.TButton",
        command=app.assign_selected_to_scale,
    ).grid(row=0, column=0, sticky="ew", padx=(0, app._pad_x(6)))
    ttk.Button(
        ports_bar,
        text="Назначить как Печь",
        style="Soft.TButton",
        command=app.assign_selected_to_furnace,
    ).grid(row=0, column=1, sticky="ew", padx=(app._pad_x(6), 0))

    probe_bar = ttk.Frame(app.left_panel, style="Card.TFrame")
    probe_bar.grid(row=5, column=0, sticky="ew", pady=(app._pad_y(10), 0))
    for idx in range(3):
        probe_bar.grid_columnconfigure(idx, weight=1)
    ttk.Button(
        probe_bar, text="Найти", style="Soft.TButton", command=app.refresh_ports
    ).grid(row=0, column=0, sticky="ew", padx=(0, app._pad_x(6)))
    ttk.Button(
        probe_bar,
        text="Проверить весы",
        style="Soft.TButton",
        command=app.probe_scale_device,
    ).grid(row=0, column=1, sticky="ew", padx=app._pad_pair(3))
    ttk.Button(
        probe_bar,
        text="Проверить печь",
        style="Soft.TButton",
        command=app.probe_furnace_device,
    ).grid(row=0, column=2, sticky="ew", padx=(app._pad_x(6), 0))
