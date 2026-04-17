from __future__ import annotations

import ctypes
import dataclasses
import json
import logging
import queue
import threading
import subprocess
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import pandas as pd

from app.config import load_config, resolve_path, save_config
from app.logger_setup import reconfigure_file_logging
from app.models import AcquisitionSnapshot, AppConfig, MeasurementRecord, PortInfo
from app.settings.schema import (
    DEFAULTS,
    SETTINGS_SECTIONS,
    TABLE_COLUMN_SPECS,
    TEST_MODE_SCOPE_LABELS,
    TEST_MODE_SCOPE_VALUES,
    TOOLTIP_DETAILS,
)
from app.services.acquisition import AcquisitionController
from app.services.device_probe import probe_furnace_port, probe_scale_port
from app.services.excel_support import read_excel_frame
from app.services.export_service import MeasurementExportService
from app.services.noise_filter import NoiseReductionConfig
from app.services.plotter import LivePlotter
from app.services.plot_series_helpers import coerce_timestamp
from app.theme import ThemeManager, ThemePalette
from app.ui_support.dialogs.help_dialogs import (
    show_about_dialog as open_about_dialog,
    show_help_dialog as open_help_dialog,
    show_tools_dialog as open_tools_dialog,
)
from app.ui_support.panels.left_panel import build_left_panel as build_left_panel_view
from app.ui_support.panels.right_panel import build_right_panel as build_right_panel_view
from app.ui_support.session_manager import (
    autosave_session as autosave_session_data,
    autosave_timer as autosave_session_timer,
    build_session_data as build_session_payload,
    build_table_export_frame as build_table_export_df,
    cleanup_autosaves as cleanup_autosave_files,
    load_session as load_session_data,
    save_session as save_session_data,
    update_restore_session_menu as update_restore_session_menu_data,
)
from app.ui_support.widgets import MetricCard, ToolTip
from app.utils.serial_tools import (
    detect_preferred_ports,
    guess_port_kind,
    list_available_ports,
    port_display_label,
)

def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _enable_windows_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _windows_work_area() -> tuple[int, int, int, int] | None:
    try:

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        SPI_GETWORKAREA = 48
        if ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        ):
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return None


class UILogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: queue.SimpleQueue[str] = queue.SimpleQueue()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.put(self.format(record))
        except Exception:
            self.handleError(record)


class LabForgeApp(tk.Tk):
    def __init__(
        self, config: AppConfig, config_path: Path, logger: logging.Logger
    ) -> None:
        _enable_windows_dpi_awareness()
        super().__init__()

        self.config_data = config
        self.config_path = config_path
        self.logger = logger
        self.TABLE_COLUMN_SPECS = TABLE_COLUMN_SPECS
        self.controller = AcquisitionController(
            config, logger=logger.getChild("acquisition")
        )
        self.export_service = MeasurementExportService(logger=logger.getChild("export"))
        self.log_handler = UILogHandler()
        self.log_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
        )
        logging.getLogger().addHandler(self.log_handler)

        self.theme_manager = ThemeManager(self.config_data.app.theme)
        self.ui_scale = self._compute_ui_scale()
        self.available_ports: list[PortInfo] = []
        self.port_map: dict[str, PortInfo] = {}
        self.port_display_map: dict[str, str] = {}
        self.left_panel_visible = False
        self.right_panel_visible = False
        self.last_scale_connected = False
        self.last_furnace_connected = False
        self._last_scale_seen_at = 0.0
        self._last_furnace_seen_at = 0.0
        self._device_indicator_hold_s = 6.0
        self.plot_side_panels_collapsed = False
        self.table_columns_collapsed = False
        self.table_column_vars: dict[str, tk.BooleanVar] = {}
        self.table_column_buttons: dict[str, ttk.Checkbutton] = {}
        self.table_column_order: list[str] = [spec[0] for spec in TABLE_COLUMN_SPECS]
        self._table_timestamp_map: dict[str, str] = {}
        self.plot_side_panels: dict[str, dict[str, object]] = {}
        self.measurement_records: list[MeasurementRecord] = []
        self._session_started_at: datetime | None = None
        self._baseline_mass: float | None = None
        self._baseline_mass_timestamp: str | None = None
        self._baseline_mass_samples: list[float] = []
        self._baseline_capture_seconds = 3.0
        self.session_autosave_dir = resolve_path("sessions/autosave")
        self.session_autosave_dir.mkdir(parents=True, exist_ok=True)
        self._autosave_timer_id = None
        self._autosave_session_key = ""
        self._autosave_session_day = ""
        self._autosave_session_path: Path | None = None
        self._summary_export_text = ""

        self._apply_tk_scaling()
        self.title("DataFusion RT")
        self.minsize(int(1320 * self.ui_scale), int(860 * self.ui_scale))
        self.configure(bg=self.theme_manager.palette.app_bg)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", self._exit_fullscreen)

        self.status_var = tk.StringVar(value="Готово к работе.")
        self.view_mode_var = tk.StringVar(value="basic")
        self.scale_port_var = tk.StringVar(value=self.config_data.scale.port)
        self.furnace_port_var = tk.StringVar(value=self.config_data.furnace.port)
        self.scale_port_display_var = tk.StringVar(value=self.config_data.scale.port)
        self.furnace_port_display_var = tk.StringVar(
            value=self.config_data.furnace.port
        )
        self.auto_detect_ports_var = tk.BooleanVar(
            value=self.config_data.app.auto_detect_ports
        )
        self.table_time_format_var = tk.StringVar(value="time_ms")
        self.table_time_suffix_var = tk.StringVar(value="none")
        self.assignment_var = tk.StringVar(value="Выберите COM-порт слева.")
        self.port_status_var = tk.StringVar(value="Нажмите «Найти COM-порты».")
        self.device_check_var = tk.StringVar(
            value="Проверка устройств ещё не выполнялась."
        )
        self.diag_status_var = tk.StringVar(value="Ошибок нет.")
        self.diag_ports_var = tk.StringVar(value="")
        self.diag_last_sample_var = tk.StringVar(value="Последний сэмпл: --")
        self.diag_last_time_var = tk.StringVar(value="Время: --")
        self.autosave_settings_var = tk.BooleanVar(
            value=self.config_data.app.autosave_settings
        )
        self.settings_mode_var = tk.StringVar(value="Ручное сохранение")
        self.settings_mode_hint_var = tk.StringVar(
            value="Изменения записываются только после нажатия кнопки «Сохранить»."
        )
        self._scale_command_in_progress = False
        self._acquisition_paused = False
        self._settings_lock_notified = False
        self._settings_input_widgets: list[object] = []
        self._suspend_settings_autosave = False
        self._plot_panel_canvases: list[tk.Canvas] = []
        self._plot_panel_bodies: list[ttk.Frame] = []

        self.setting_vars: dict[str, tk.Variable] = {}
        self._init_setting_vars()
        self._bind_settings_autosave()

        self._build_layout()
        self._load_settings_into_vars()
        self._apply_theme()
        self._apply_window_preferences()
        self.refresh_ports()
        self._run_port_autodetect(on_startup=True)
        self._reset_readouts()
        self._update_side_panels()
        self._animate_indicators()
        self._poll_runtime_queues()
        self._autosave_timer()

    def _compute_ui_scale(self) -> float:
        width = max(1280, self.winfo_screenwidth())
        height = max(800, self.winfo_screenheight())
        base_scale = _clamp(min(width / 1680.0, height / 1020.0), 0.95, 1.3)
        return _clamp(base_scale * float(self.config_data.app.font_scale), 0.9, 1.7)

    def _apply_tk_scaling(self) -> None:
        try:
            self.tk.call("tk", "scaling", max(1.0, 1.08 * self.ui_scale))
        except Exception:
            self.logger.debug("Unable to adjust tk scaling.", exc_info=True)

    def _apply_window_preferences(self) -> None:
        if self.config_data.app.fullscreen:
            try:
                self.attributes("-fullscreen", True)
                return
            except Exception:
                self.logger.warning(
                    "Fullscreen mode is not supported on this system.", exc_info=True
                )

        if self.config_data.app.start_maximized:
            try:
                self.state("zoomed")
                return
            except Exception:
                self.logger.debug(
                    "Zoomed window state is not available.", exc_info=True
                )
                area = _windows_work_area()
                if area is not None:
                    x, y, width, height = area
                    self.geometry(f"{width}x{height}+{x}+{y}")
                else:
                    width = self.winfo_screenwidth()
                    height = self.winfo_screenheight()
                    self.geometry(f"{width}x{height}+0+0")
                return

        area = _windows_work_area()
        if area is not None:
            x, y, width, height = area
            self.geometry(f"{int(width * 0.92)}x{int(height * 0.9)}+{x + 24}+{y + 24}")
        else:
            width = int(self.winfo_screenwidth() * 0.92)
            height = int(self.winfo_screenheight() * 0.9)
            self.geometry(f"{width}x{height}+24+24")

    def _init_setting_vars(self) -> None:
        for _section_title, fields in SETTINGS_SECTIONS:
            for key, _label, kind, _tooltip, _choices in fields:
                default = DEFAULTS[key]
                if kind == "bool":
                    self.setting_vars[key] = tk.BooleanVar(value=bool(default))
                else:
                    self.setting_vars[key] = tk.StringVar(value=str(default))

    def _bind_settings_autosave(self) -> None:
        for var in self.setting_vars.values():
            var.trace_add("write", self._on_setting_var_changed)
        self.scale_port_display_var.trace_add("write", self._on_setting_var_changed)
        self.furnace_port_display_var.trace_add("write", self._on_setting_var_changed)
        self.auto_detect_ports_var.trace_add("write", self._on_setting_var_changed)
        self.autosave_settings_var.trace_add("write", self._on_autosave_toggle)

    def _load_settings_into_vars(self) -> None:
        self._suspend_settings_autosave = True
        values = {
            "scale.enabled": self.config_data.scale.enabled,
            "scale.baudrate": self.config_data.scale.baudrate,
            "scale.timeout": self.config_data.scale.timeout,
            "scale.mode": self.config_data.scale.mode,
            "scale.request_command": self.config_data.scale.request_command,
            "scale.p1_polling_enabled": self.config_data.scale.p1_polling_enabled,
            "scale.p1_poll_interval_sec": self.config_data.scale.p1_poll_interval_sec,
            "furnace.enabled": self.config_data.furnace.enabled,
            "furnace.baudrate": self.config_data.furnace.baudrate,
            "furnace.bytesize": self.config_data.furnace.bytesize,
            "furnace.parity": self.config_data.furnace.parity,
            "furnace.stopbits": self.config_data.furnace.stopbits,
            "furnace.timeout": self.config_data.furnace.timeout,
            "furnace.slave_id": self.config_data.furnace.slave_id,
            "furnace.register_pv": self.config_data.furnace.register_pv,
            "furnace.register_sv": self.config_data.furnace.register_sv,
            "furnace.scale_factor": self.config_data.furnace.scale_factor,
            "furnace.driver": self.config_data.furnace.driver,
            "furnace.input_type_code": self.config_data.furnace.input_type_code,
            "furnace.input_type_name": self.config_data.furnace.input_type_name,
            "furnace.high_limit": self.config_data.furnace.high_limit,
            "furnace.high_alarm": self.config_data.furnace.high_alarm,
            "furnace.low_alarm": self.config_data.furnace.low_alarm,
            "furnace.pid_p": self.config_data.furnace.pid_p,
            "furnace.pid_t": self.config_data.furnace.pid_t,
            "furnace.ctrl_mode": self.config_data.furnace.ctrl_mode,
            "furnace.output_high_limit": self.config_data.furnace.output_high_limit,
            "furnace.display_decimals": self.config_data.furnace.display_decimals,
            "furnace.sensor_correction": self.config_data.furnace.sensor_correction,
            "furnace.opt_code": self.config_data.furnace.opt_code,
            "furnace.run_code": self.config_data.furnace.run_code,
            "furnace.alarm_output_code": self.config_data.furnace.alarm_output_code,
            "furnace.m5_value": self.config_data.furnace.m5_value,
            "app.poll_interval_sec": self.config_data.app.poll_interval_sec,
            "app.max_points_on_plot": self.config_data.app.max_points_on_plot,
            "app.auto_detect_ports": self.config_data.app.auto_detect_ports,
            "app.test_mode": self.config_data.app.test_mode,
            "app.test_mode_scope": self._test_mode_scope_to_label(
                self.config_data.app.test_mode_scope
            ),
            "app.start_maximized": self.config_data.app.start_maximized,
            "app.fullscreen": self.config_data.app.fullscreen,
            "app.theme": self.config_data.app.theme,
            "app.csv_path": self.config_data.app.csv_path,
            "app.log_path": self.config_data.app.log_path,
            "app.enable_file_logging": self.config_data.app.enable_file_logging,
        }
        for key, value in values.items():
            if key not in self.setting_vars:
                continue
            var = self.setting_vars[key]
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set(str(value))
        self.autosave_settings_var.set(self.config_data.app.autosave_settings)
        self._suspend_settings_autosave = False

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.toolbar = ttk.Frame(self, style="Header.TFrame", padding=self._pad(12, 8))
        self.toolbar.grid(row=0, column=0, sticky="nsew")
        self.toolbar.grid_columnconfigure(0, weight=1)

        left_tools = ttk.Frame(self.toolbar, style="Header.TFrame")
        left_tools.grid(row=0, column=0, sticky="w")
        self._build_top_menus(left_tools)

        right_tools = ttk.Frame(self.toolbar, style="Header.TFrame")
        right_tools.grid(row=0, column=1, sticky="e")
        self.scale_indicator = self._build_status_indicator(right_tools, "Весы")
        self.scale_indicator["frame"].grid(row=0, column=0, padx=(0, self._pad_x(10)))
        self.furnace_indicator = self._build_status_indicator(right_tools, "Печь")
        self.furnace_indicator["frame"].grid(row=0, column=1, padx=(0, self._pad_x(12)))
        self.help_button = tk.Menubutton(
            right_tools, text="Помощь", relief="flat", bd=0, direction="below"
        )
        self.help_button.grid(row=0, column=2, padx=(0, self._pad_x(8)))
        help_menu = tk.Menu(self.help_button, tearoff=False)
        help_menu.add_command(label="Инструкция", command=self.show_help_dialog)
        help_menu.add_command(label="Инструменты", command=self.show_tools_dialog)
        help_menu.add_command(label="Об авторе", command=self.show_about_dialog)
        self.help_button.configure(menu=help_menu)
        self.log_menu_button = tk.Menubutton(
            right_tools, text="Лог", relief="flat", bd=0, direction="below"
        )
        self.log_menu_button.grid(row=0, column=3, padx=(0, self._pad_x(8)))
        log_menu = tk.Menu(self.log_menu_button, tearoff=False)
        log_menu.add_command(label="Показать журнал", command=self.toggle_right_panel)
        log_menu.add_command(
            label="Сохранить журнал в TXT", command=self.save_runtime_log
        )
        log_menu.add_command(
            label="Открыть папку журналов", command=self.open_logs_folder
        )
        self.log_menu_button.configure(menu=log_menu)
        self.settings_button = ttk.Button(
            right_tools,
            text="Настройки",
            style="Soft.TButton",
            command=self.open_settings_window,
        )
        self.settings_button.grid(row=0, column=4)

        self.header_status = tk.Label(
            self.toolbar, textvariable=self.status_var, anchor="w", justify="left"
        )
        self.header_status.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(self._pad_y(3), 0)
        )

        self.body = ttk.Frame(self, style="App.TFrame", padding=self._pad(10, 10))
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(1, weight=1)

        self.left_panel = ttk.Frame(
            self.body, style="Card.TFrame", padding=self._pad(14, 14)
        )
        self.left_panel.grid_rowconfigure(2, weight=1)
        self.left_panel.grid_columnconfigure(0, weight=1)
        self._build_left_panel()

        self.center_panel = ttk.Frame(self.body, style="App.TFrame")
        self.center_panel.grid(row=0, column=1, sticky="nsew")
        self.center_panel.grid_rowconfigure(
            0, weight=8, minsize=int(500 * self.ui_scale)
        )
        self.center_panel.grid_rowconfigure(
            1, weight=1, minsize=int(116 * self.ui_scale)
        )
        self.center_panel.grid_rowconfigure(2, weight=0)
        self.center_panel.grid_columnconfigure(0, weight=1)
        self._build_center_panel()

        self.right_panel = ttk.Frame(
            self.body, style="Card.TFrame", padding=self._pad(14, 14)
        )
        self.right_panel.grid_rowconfigure(6, weight=1)
        self.right_panel.grid_columnconfigure(0, weight=1)
        self._build_right_panel()

    def _build_top_menus(self, parent) -> None:
        self.option_add(
            "*Menu.Font", f"{{Segoe UI}} {max(11, int(12 * self.ui_scale))}"
        )
        self.option_add(
            "*TCombobox*Listbox.Font",
            f"{{Segoe UI}} {max(11, int(12 * self.ui_scale))}",
        )
        self.file_menu_button = self._make_menu_button(parent, "Экспорт / импорт")
        file_menu = tk.Menu(self.file_menu_button, tearoff=False)
        file_menu.add_command(label="Загрузить сессию", command=self.import_data_from_file)
        file_menu.add_separator()
        file_menu.add_command(
            label="Экспорт CSV",
            command=lambda: self.export_measurements(default_ext=".csv"),
        )
        file_menu.add_command(
            label="Экспорт Excel",
            command=lambda: self.export_measurements(default_ext=".xlsx"),
        )
        file_menu.add_command(label="Изображение", command=self.save_plot_image)
        file_menu.add_command(label="Сохранить сессию", command=self.save_session)
        restore_menu = tk.Menu(
            file_menu, tearoff=False, postcommand=self.update_restore_session_menu
        )
        self.restore_session_menu = restore_menu
        # Инициализируем меню при старте
        self.after_idle(self.update_restore_session_menu)
        file_menu.add_cascade(label="История сессий", menu=restore_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self._on_close)
        self.file_menu_button.configure(menu=file_menu)

        self.theme_switch_button = ttk.Button(
            parent, text="", style="Soft.TButton", command=self.toggle_theme
        )
        self.theme_switch_button.pack(side="left", padx=(0, self._pad_x(8)))
        self.font_toolbar = ttk.Frame(parent, style="Header.TFrame")
        self.font_toolbar.pack(side="left", padx=(0, self._pad_x(8)))
        ttk.Label(
            self.font_toolbar, text="Размер шрифта", style="Subtitle.TLabel"
        ).grid(row=0, column=0, padx=(0, self._pad_x(6)))
        ttk.Button(
            self.font_toolbar,
            text="-",
            style="Soft.TButton",
            command=lambda: self.adjust_font_scale(-0.05),
            width=3,
        ).grid(row=0, column=1, padx=(0, self._pad_x(4)))
        self.font_scale_value_label = ttk.Label(
            self.font_toolbar, text="", style="Subtitle.TLabel", anchor="center"
        )
        self.font_scale_value_label.grid(row=0, column=2, padx=(0, self._pad_x(4)))
        ttk.Button(
            self.font_toolbar,
            text="+",
            style="Soft.TButton",
            command=lambda: self.adjust_font_scale(0.05),
            width=3,
        ).grid(row=0, column=3, padx=(0, self._pad_x(4)))
        ttk.Button(
            self.font_toolbar,
            text="Сброс",
            style="Soft.TButton",
            command=self.reset_font_scale,
        ).grid(row=0, column=4)

    def _make_menu_button(self, parent, text: str) -> tk.Menubutton:
        button = tk.Menubutton(
            parent, text=text, relief="flat", bd=0, direction="below"
        )
        button.pack(side="left", padx=(0, self._pad_x(8)))
        return button

    def _build_status_indicator(self, parent, title: str) -> dict[str, object]:
        frame = ttk.Frame(parent, style="Header.TFrame")
        canvas = tk.Canvas(
            frame,
            width=self._pad_x(18),
            height=self._pad_x(18),
            highlightthickness=0,
            bd=0,
        )
        canvas.grid(row=0, column=0, padx=(0, self._pad_x(6)))
        outer = canvas.create_oval(
            1, 1, self._pad_x(17), self._pad_x(17), outline="", fill="#2B3644"
        )
        inner = canvas.create_oval(
            4, 4, self._pad_x(14), self._pad_x(14), outline="", fill="#536273"
        )
        label = tk.Label(frame, text=title, anchor="w")
        label.grid(row=0, column=1, sticky="w")
        return {
            "frame": frame,
            "canvas": canvas,
            "outer": outer,
            "inner": inner,
            "label": label,
        }

    def _build_left_panel(self) -> None:
        build_left_panel_view(self)

    def _build_center_panel(self) -> None:
        content_tabs = ttk.Notebook(self.center_panel, style="Compact.TNotebook")
        content_tabs.grid(row=0, column=0, sticky="nsew")
        self.center_content_tabs = content_tabs

        graph_tab = ttk.Frame(content_tabs, style="App.TFrame")
        table_tab = ttk.Frame(content_tabs, style="App.TFrame")
        summary_tab = ttk.Frame(content_tabs, style="App.TFrame")
        graph_tab.grid_rowconfigure(0, weight=1)
        graph_tab.grid_columnconfigure(0, weight=1)
        table_tab.grid_rowconfigure(0, weight=1)
        table_tab.grid_columnconfigure(0, weight=1)
        summary_tab.grid_rowconfigure(0, weight=1)
        summary_tab.grid_columnconfigure(0, weight=1)
        content_tabs.add(graph_tab, text="График")
        content_tabs.add(table_tab, text="Таблица")
        content_tabs.add(summary_tab, text="Отчёт")
        self.summary_tab = summary_tab

        plot_card = ttk.Frame(graph_tab, style="Card.TFrame", padding=self._pad(8, 8))
        plot_card.grid(row=0, column=0, sticky="nsew")
        self.plot_card = plot_card
        plot_card.grid_rowconfigure(1, weight=1)
        plot_card.grid_rowconfigure(2, weight=0)
        plot_card.grid_columnconfigure(0, weight=1)
        plot_card.grid_columnconfigure(1, weight=0, minsize=int(42 * self.ui_scale))
        plot_card.grid_columnconfigure(2, weight=0, minsize=int(84 * self.ui_scale))
        plot_card.grid_columnconfigure(3, weight=0, minsize=int(76 * self.ui_scale))
        plot_card.grid_columnconfigure(4, weight=0, minsize=int(92 * self.ui_scale))

        plot_header = ttk.Frame(plot_card, style="Card.TFrame")
        plot_header.grid(row=0, column=0, columnspan=5, sticky="ew")
        plot_header.grid_columnconfigure(0, weight=1)
        ttk.Label(plot_header, text="График измерений", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )

        self.plotter = LivePlotter(
            plot_card,
            max_points=int(self.config_data.app.max_points_on_plot),
            plot_theme=self.theme_manager.palette.plot,
            scale=self.ui_scale,
            logger=self.logger.getChild("plotter"),
        )
        self.plotter.apply_series_styles(self.config_data.app.plot_styles)
        self.plotter.configure_scale_mode(
            autoscale_enabled=self.config_data.app.plot_autoscale_enabled,
            manual_x_seconds=self.config_data.app.plot_manual_x_seconds,
            manual_y_span=self.config_data.app.plot_manual_y_span,
            y_headroom=self.config_data.app.plot_y_headroom,
        )
        self.plotter.get_widget().grid(
            row=1, column=0, sticky="nsew", pady=(self._pad_y(4), 0)
        )
        self.legend_panel = ttk.Frame(
            plot_card,
            style="CardAlt.TFrame",
            padding=self._pad(8, 5),
        )
        self.legend_panel.grid(row=2, column=0, sticky="ew", pady=(self._pad_y(5), 0))
        for idx in range(5):
            self.legend_panel.grid_columnconfigure(idx, weight=1)
        self.legend_vars: dict[str, tk.BooleanVar] = {}
        self.legend_checkbuttons: dict[str, ttk.Checkbutton] = {}
        self._build_plot_side_toggle_strip(plot_card)

        tools_panel, tools_body = self._create_plot_button_panel(
            plot_card,
            title="Инструменты",
            column=2,
            width=int(156 * self.ui_scale),
            padx=(self._pad_x(10), self._pad_x(6)),
            panel_key="tools",
        )
        self.plot_pan_button = self._make_plot_tool_button(
            tools_body, "↔ Сдвиг", self.toggle_plot_pan, width=10
        )
        self.plot_pan_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.plot_pan_button, "Режим ручного сдвига графика мышью.")
        self.calc_cursor_button = self._make_plot_tool_button(
            tools_body, "Курсор", self.toggle_calc_cursor, width=10
        )
        self.calc_cursor_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.calc_cursor_clear_button = self._make_plot_tool_button(
            tools_body, "Сброс меток", self.clear_calc_cursor_marks, width=10
        )
        self.calc_cursor_clear_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_zoom_button = self._make_plot_tool_button(
            tools_body, "🔍", self.toggle_plot_zoom, width=10
        )
        self.plot_zoom_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.plot_zoom_button, "Режим увеличения области графика мышью.")
        zoom_row = ttk.Frame(tools_body, style="Card.TFrame")
        zoom_row.pack(fill="x", pady=(0, self._pad_y(5)))
        zoom_row.columnconfigure(0, weight=1)
        zoom_row.columnconfigure(1, weight=1)
        self.plot_plus_button = self._make_plot_tool_button(
            zoom_row, "+", self.zoom_in_plot, width=4
        )
        self.plot_plus_button.grid(
            row=0, column=0, sticky="ew", padx=(0, self._pad_x(3))
        )
        ToolTip(self.plot_plus_button, "Приблизить график по вертикали.")
        self.plot_minus_button = self._make_plot_tool_button(
            zoom_row, "-", self.zoom_out_plot, width=4
        )
        self.plot_minus_button.grid(
            row=0, column=1, sticky="ew", padx=(self._pad_x(3), 0)
        )
        ToolTip(self.plot_minus_button, "Отдалить график по вертикали.")
        self.plot_reset_button = self._make_plot_tool_button(
            tools_body, "Авто / сброс", self.reset_plot_view, width=14
        )
        self.plot_reset_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.plot_reset_button, "Сбросить ручной сдвиг и сразу вернуть автоматический масштаб графика.")
        self.plot_scale_button = self._make_plot_tool_button(
            tools_body, "Масштаб", self.open_plot_scale_dialog, width=10
        )
        self.plot_scale_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.plot_scale_button, "Открыть расширенные настройки масштаба.")
        self.plot_cut_button = self._make_plot_tool_button(
            tools_body, "Вырезать", self.open_cut_data_dialog, width=10
        )
        self.plot_cut_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(
            self.plot_cut_button,
            "Удалить выбранный интервал данных по одной оси или сразу по всем осям.",
        )
        views_panel, views_body = self._create_plot_button_panel(
            plot_card,
            title="Виды",
            column=3,
            width=int(198 * self.ui_scale),
            padx=(self._pad_x(6), 0),
            panel_key="views",
        )
        self.plot_mode_buttons: dict[str, ttk.Button] = {}
        mode_specs = [
            ("Общий", LivePlotter.VIEW_COMBINED, self.set_plot_mode_combined),
            ("Раздельно", LivePlotter.VIEW_SPLIT, self.set_plot_mode_split),
            ("Масса", LivePlotter.VIEW_MASS, self.set_plot_mode_mass),
            ("Темп.", LivePlotter.VIEW_TEMP, self.set_plot_mode_temp),
            ("Δ", LivePlotter.VIEW_DELTA, self.set_plot_mode_delta),
        ]
        for label, mode_key, command in mode_specs:
            button = self._make_plot_tool_button(views_body, label, command, width=10)
            button.pack(fill="x", pady=(0, self._pad_y(5)))
            self.plot_mode_buttons[mode_key] = button
            ToolTip(button, f"Переключить вид графика: {label.lower()}.")
        ttk.Separator(views_body, orient="horizontal").pack(
            fill="x", pady=(self._pad_y(2), self._pad_y(6))
        )
        self.plot_range_toggle_button = self._make_plot_tool_button(
            views_body, "Окно: текущее", self.toggle_plot_range_mode, width=10
        )
        self.plot_range_toggle_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(
            self.plot_range_toggle_button,
            "Переключить окно времени: текущий участок или весь график от начала записи.",
        )
        self.plot_time_axis_button = self._make_plot_tool_button(
            views_body, "Время: от начала", self.toggle_plot_time_axis_mode, width=12
        )
        self.plot_time_axis_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(
            self.plot_time_axis_button,
            "Переключить подписи оси времени между временем от начала записи и реальными часами.",
        )
        self.plot_render_toggle_button = self._make_plot_tool_button(
            views_body, "Кривая: линии", self.toggle_plot_render_mode, width=12
        )
        self.plot_render_toggle_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(
            self.plot_render_toggle_button,
            "Переключить стиль кривой между линиями и точками.",
        )

        calculations_panel, calculations_body = self._create_plot_button_panel(
            plot_card,
            title="Анализ",
            column=4,
            width=int(170 * self.ui_scale),
            padx=(self._pad_x(6), 0),
            panel_key="calculations",
        )

        self.calc_dtg_button = self._make_plot_tool_button(
            calculations_body, "DTG", self.activate_calc_dtg, width=12
        )
        self.calc_dtg_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.calc_dtg_button, "Показать DTG: скорость изменения массы.")
        self.calc_normalize_button = self._make_plot_tool_button(
            calculations_body, "Нормал.", self.toggle_calc_normalization, width=12
        )
        self.calc_normalize_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.calc_normalize_button, "Включить или отключить нормализацию массы.", placement="left")
        self.calc_markers_button = self._make_plot_tool_button(
            calculations_body, "Маркеры", self.open_calc_markers_menu, width=12
        )
        self.calc_markers_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.calc_markers_button, "Поставить и двигать маркеры A/B для расчётов.", placement="left")
        self.calc_heating_profile_button = self._make_plot_tool_button(
            calculations_body, "Профиль нагрева", self.toggle_calc_heating_profile, width=12
        )
        self.calc_heating_profile_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.calc_heating_profile_button, "Показать эталонный профиль нагрева по данным камеры.", placement="left")
        self.calc_noise_button = self._make_plot_tool_button(
            calculations_body, "Шум", self.open_noise_reduction_menu, width=12
        )
        self.calc_noise_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.calc_noise_button, "Открыть параметры шумоподавления для данных и графика.", placement="left")
        self.plot_smooth_button = self._make_plot_tool_button(
            calculations_body, "Сглаж.", self.set_plot_smooth, width=12
        )
        self.plot_smooth_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.plot_smooth_button, "Включить или отключить сглаженное отображение кривой.", placement="left")
        self.calc_stage_button = self._make_plot_tool_button(
            calculations_body, "Стадии", self.toggle_calc_stage_analysis, width=12
        )
        self.calc_stage_button.pack(fill="x", pady=(0, self._pad_y(5)))
        ToolTip(self.calc_stage_button, "Подсветить найденный диапазон стадий процесса.", placement="left")
        self._update_plot_mode_buttons()
        self._update_plot_range_buttons()
        self._update_plot_render_buttons()
        self._update_plot_time_axis_button()
        self._update_calc_buttons()
        self._refresh_plot_legend()
        self._update_plot_side_panels_state()

        self._build_summary_tab(summary_tab)

        table_card = ttk.Frame(
            table_tab, style="Card.TFrame", padding=self._pad(12, 12)
        )
        table_card.grid(row=0, column=0, sticky="nsew")
        table_card.grid_rowconfigure(2, weight=1)
        table_card.grid_columnconfigure(0, weight=1)
        ttk.Label(table_card, text="Таблица измерений", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        table_controls = ttk.Frame(
            table_card, style="CardAlt.TFrame", padding=self._pad(8, 6)
        )
        table_controls.grid(row=1, column=0, sticky="ew", pady=(self._pad_y(8), 0))
        table_controls.grid_columnconfigure(1, weight=1)
        table_controls.grid_columnconfigure(2, weight=0)
        table_controls.grid_columnconfigure(3, weight=0)
        table_controls.grid_columnconfigure(4, weight=0)
        self.table_columns_toggle_button = tk.Button(
            table_controls,
            text="◀",
            command=self.toggle_table_columns_panel,
            takefocus=False,
            relief="solid",
            bd=1,
            cursor="hand2",
            width=2,
        )
        self.table_columns_toggle_button.grid(
            row=0, column=0, sticky="ns", padx=(0, self._pad_x(8))
        )
        self.table_columns_panel = ttk.Frame(table_controls, style="CardAlt.TFrame")
        self.table_columns_panel.grid(row=0, column=1, sticky="ew")
        table_search_controls = ttk.Frame(table_controls, style="CardAlt.TFrame")
        table_search_controls.grid(row=0, column=2, sticky="e", padx=(self._pad_x(10), 0))
        ttk.Label(
            table_search_controls,
            text="Поиск",
            style="CardText.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, self._pad_x(6)))
        self.table_search_column_var = tk.StringVar(value="Время")
        self.table_search_value_var = tk.StringVar()
        self.table_search_column_combo = ttk.Combobox(
            table_search_controls,
            state="readonly",
            width=12,
            textvariable=self.table_search_column_var,
            values=[label for _key, label, _width, _anchor in TABLE_COLUMN_SPECS],
        )
        self.table_search_column_combo.grid(row=0, column=1, sticky="w", padx=(0, self._pad_x(6)))
        self.table_search_value_entry = ttk.Entry(
            table_search_controls,
            width=14,
            textvariable=self.table_search_value_var,
        )
        self.table_search_value_entry.grid(row=0, column=2, sticky="w", padx=(0, self._pad_x(6)))
        ttk.Button(
            table_search_controls,
            text="Найти",
            style="Soft.TButton",
            command=self.find_in_table,
        ).grid(row=0, column=3, sticky="w")
        table_time_controls = ttk.Frame(table_controls, style="CardAlt.TFrame")
        table_time_controls.grid(row=0, column=3, sticky="e", padx=(self._pad_x(10), 0))
        ttk.Label(
            table_time_controls,
            text="Время",
            style="CardText.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, self._pad_x(6)))
        self.table_time_format_combo = ttk.Combobox(
            table_time_controls,
            state="readonly",
            width=14,
            textvariable=self.table_time_format_var,
            values=("ЧЧ:ММ:СС", "ЧЧ:ММ:СС.мс", "Дата+время", "Дата+время.мс"),
        )
        self.table_time_format_combo.grid(
            row=0, column=1, sticky="w", padx=(0, self._pad_x(6))
        )
        self.table_time_suffix_combo = ttk.Combobox(
            table_time_controls,
            state="readonly",
            width=12,
            textvariable=self.table_time_suffix_var,
            values=("Без зоны", "местн.", "UTC+смещ."),
        )
        self.table_time_suffix_combo.grid(row=0, column=2, sticky="w")
        self.table_time_format_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_table_timestamps()
        )
        self.table_time_suffix_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_table_timestamps()
        )
        self.table_time_format_combo.set("ЧЧ:ММ:СС.мс")
        self.table_time_suffix_combo.set("Без зоны")
        ttk.Button(
            table_controls,
            text="Удалить не полные данные",
            style="Soft.TButton",
            command=self.delete_incomplete_rows,
            width=24,
        ).grid(row=0, column=4, sticky="e", padx=(self._pad_x(10), 0))
        for index, (key, label, _width, _anchor) in enumerate(TABLE_COLUMN_SPECS):
            var = tk.BooleanVar(value=True)
            self.table_column_vars[key] = var
            button = ttk.Checkbutton(
                self.table_columns_panel,
                text=label,
                variable=var,
                style="Card.TCheckbutton",
                takefocus=False,
                command=lambda column_key=key: self._toggle_table_column(column_key),
            )
            button.grid(row=0, column=index, sticky="w", padx=(0, self._pad_x(14)))
            self.table_column_buttons[key] = button
        table_frame = ttk.Frame(table_card, style="Card.TFrame")
        table_frame.grid(row=2, column=0, sticky="nsew", pady=(self._pad_y(10), 0))
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        columns = tuple(spec[0] for spec in TABLE_COLUMN_SPECS)
        self.measurements_table = ttk.Treeview(
            table_frame, columns=columns, show="headings", height=14
        )
        for key, label, width, anchor in TABLE_COLUMN_SPECS:
            self.measurements_table.heading(key, text=label)
            self.measurements_table.column(
                key, width=int(width * self.ui_scale), anchor=anchor
            )
        self.measurements_table.grid(row=0, column=0, sticky="nsew")
        table_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.measurements_table.yview
        )
        table_scroll.grid(row=0, column=1, sticky="ns")
        self.measurements_table.configure(yscrollcommand=table_scroll.set)
        self.measurements_table.bind("<Button-3>", self.open_table_context_menu)
        self._apply_table_column_visibility()

        cards = ttk.Frame(self.center_panel, style="App.TFrame")
        cards.grid(row=1, column=0, sticky="nsew", pady=(self._pad_y(8), 0))
        for idx in range(5):
            cards.grid_columnconfigure(idx, weight=1, uniform="metric")
        self.mass_card = MetricCard(cards, "Масса", "accent", value_size=40)
        self.mass_card.grid(row=0, column=0, sticky="nsew", padx=(0, self._pad_x(8)))
        self.temp_card = MetricCard(cards, "t Камера PV", "heat", value_size=40)
        self.temp_card.grid(row=0, column=1, sticky="nsew", padx=self._pad_pair(4))
        self.thermocouple_card = MetricCard(
            cards, "Термопара SV", "warning", value_size=40
        )
        self.thermocouple_card.grid(
            row=0, column=2, sticky="nsew", padx=self._pad_pair(4)
        )
        self.status_card = MetricCard(
            cards, "Статус", "success", value_size=24, unit_size=1
        )
        self.status_card.grid(row=0, column=3, sticky="nsew", padx=self._pad_pair(4))
        self.time_card = MetricCard(
            cards, "Время", "border", value_size=22, unit_size=1
        )
        self.time_card.grid(row=0, column=4, sticky="nsew", padx=(self._pad_x(8), 0))
        self.mass_card.configure_action(
            text="⟳",
            command=self.sync_measurement_baseline,
            image=None,
            tooltip="Синхронизировать начальную массу по текущему значению и перенести маркер A.",
            visible=True,
        )

        actions = ttk.Frame(self.center_panel, style="App.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(self._pad_y(8), 0))
        for idx in range(6):
            actions.grid_columnconfigure(idx, weight=1)
        self.start_button = ttk.Button(
            actions,
            text="Старт",
            style="Accent.TButton",
            command=self.start_acquisition,
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        self.stop_button = ttk.Button(
            actions, text="Стоп", style="Warm.TButton", command=self.stop_acquisition
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        self.pause_button = ttk.Button(
            actions, text="Пауза", style="Soft.TButton", command=self.pause_acquisition
        )
        self.pause_button.grid(row=0, column=2, sticky="ew", padx=self._pad_pair(3))
        self.reset_button = ttk.Button(
            actions, text="Сброс", style="Soft.TButton", command=self.clear_graph
        )
        self.reset_button.grid(
            row=0, column=3, sticky="ew", padx=(self._pad_x(3), self._pad_x(8))
        )
        self.tare_button = ttk.Button(
            actions, text="Тара", style="Soft.TButton", command=self.tare_scale
        )
        self.tare_button.grid(
            row=0, column=4, sticky="ew", padx=(self._pad_x(2), self._pad_x(3))
        )
        self.zero_button = ttk.Button(
            actions, text="Ноль", style="Soft.TButton", command=self.zero_scale
        )
        self.zero_button.grid(row=0, column=5, sticky="ew", padx=(self._pad_x(6), 0))

    def _build_right_panel(self) -> None:
        build_right_panel_view(self)

    def _refresh_plot_legend(self) -> None:
        if not hasattr(self, "legend_panel"):
            return
        for child in self.legend_panel.winfo_children():
            child.destroy()

        ttk.Label(
            self.legend_panel,
            text="Легенда:",
            style="CardText.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, self._pad_x(10)))
        items = self.plotter.legend_items()
        if not items:
            ttk.Label(
                self.legend_panel,
                text="Нет доступных кривых для текущего режима.",
                style="CardText.TLabel",
            ).grid(row=0, column=1, sticky="w")
            spacer_column = 2
        else:
            for index, item in enumerate(items):
                key = str(item["key"])
                var = self.legend_vars.get(key)
                if var is None:
                    var = tk.BooleanVar(value=bool(item["visible"]))
                    self.legend_vars[key] = var
                else:
                    var.set(bool(item["visible"]))
                button = ttk.Checkbutton(
                    self.legend_panel,
                    text=str(item["label"]),
                    variable=var,
                    style="Card.TCheckbutton",
                    takefocus=False,
                    command=lambda series_key=key, value_var=var: (
                        self._toggle_plot_series(series_key, value_var)
                    ),
                )
                button.grid(row=0, column=index + 1, sticky="w", padx=(0, self._pad_x(12)))
                self.legend_checkbuttons[key] = button
            spacer_column = len(items) + 1
        self.legend_panel.grid_columnconfigure(spacer_column, weight=1)
        legend_actions = ttk.Frame(self.legend_panel, style="Card.TFrame")
        legend_actions.grid(row=0, column=spacer_column + 1, sticky="e")
        paused = self.plotter.display_paused
        self.plot_pause_button = ttk.Button(
            legend_actions,
            text="⏸",
            width=3,
            style="Accent.TButton" if paused else "WindowIcon.TButton",
            command=self.toggle_plot_pause,
        )
        self.plot_pause_button.grid(
            row=0, column=0, sticky="e", padx=(0, self._pad_x(4))
        )
        ToolTip(
            self.plot_pause_button,
            "Остановить live-сдвиг графика. Запись данных продолжается.",
        )
        self.plot_live_button = ttk.Button(
            legend_actions,
            text="▶",
            width=3,
            style="WindowIcon.TButton",
            command=self.resume_plot_live_view,
        )
        self.plot_live_button.grid(
            row=0, column=1, sticky="e", padx=(0, self._pad_x(4))
        )
        ToolTip(
            self.plot_live_button,
            "Вернуться к текущему моменту и снова вести график за новыми данными.",
        )
        plot_style_button = ttk.Button(
            legend_actions,
            text="⚙",
            style="WindowIcon.TButton",
            command=self.open_plot_style_editor,
            width=3,
        )
        plot_style_button.grid(row=0, column=2, sticky="e")
        ToolTip(
            plot_style_button,
            "Настроить цвет, толщину и стиль линий.",
        )
        self.plot_live_button.state(["!disabled"] if paused else ["disabled"])

    def _toggle_plot_series(self, series_key: str, value_var: tk.BooleanVar) -> None:
        self.plotter.set_series_visible(series_key, bool(value_var.get()))

    def _toggle_table_column(self, column_key: str) -> None:
        if column_key not in self.table_column_vars:
            return
        visible_keys = [
            key for key, var in self.table_column_vars.items() if bool(var.get())
        ]
        if not visible_keys:
            self.table_column_vars[column_key].set(True)
            self._set_status(
                "В таблице должна остаться хотя бы одна колонка.", logging.WARNING
            )
            return
        self._apply_table_column_visibility()

    def _apply_table_column_visibility(self) -> None:
        if not hasattr(self, "measurements_table"):
            return
        display_columns = [
            key
            for key in self.table_column_order
            if bool(self.table_column_vars.get(key).get())
        ]
        self.measurements_table.configure(displaycolumns=display_columns)

    def _selected_table_record_index(self) -> int | None:
        if not hasattr(self, "measurements_table"):
            return None
        selection = self.measurements_table.selection()
        if not selection:
            return None
        children = list(self.measurements_table.get_children())
        try:
            return children.index(selection[0])
        except ValueError:
            return None

    def _reload_records_after_table_edit(self, records: list[MeasurementRecord], *, status: str) -> None:
        self._load_records_into_ui(records)
        self._set_status(status, emit_log=False)

    def delete_incomplete_rows(self) -> None:
        if not self.measurement_records:
            self._set_status("Нет данных для очистки.", logging.WARNING)
            return
        filtered = [
            record
            for record in self.measurement_records
            if record.timestamp
            and record.mass is not None
            and record.furnace_pv is not None
            and record.furnace_sv is not None
        ]
        removed = len(self.measurement_records) - len(filtered)
        if removed <= 0:
            self._set_status("Нечего удалять: не полные данные не найдены.", logging.WARNING)
            return
        confirmed = messagebox.askyesno(
            "Удаление данных",
            (
                f"Вы уверены, что хотите удалить не полные данные?\n\n"
                f"Будет удалено строк: {removed}.\n"
                "Это приведёт к невозможности восстановить их."
            ),
            parent=self,
        )
        if not confirmed:
            return
        self._reload_records_after_table_edit(
            filtered,
            status=f"Удалено неполных строк: {removed}.",
        )

    def find_in_table(self) -> None:
        if not hasattr(self, "measurements_table"):
            return
        query = self.table_search_value_var.get().strip().lower()
        if not query:
            self._set_status("Введите значение для поиска.", logging.WARNING)
            return
        selected_label = self.table_search_column_var.get().strip()
        column_key = next(
            (key for key, label, _width, _anchor in TABLE_COLUMN_SPECS if label == selected_label),
            "timestamp",
        )
        values_index = self.table_column_order.index(column_key)
        for item_id in self.measurements_table.get_children():
            row_values = self.measurements_table.item(item_id, "values")
            if values_index < len(row_values) and query in str(row_values[values_index]).lower():
                self.measurements_table.selection_set(item_id)
                self.measurements_table.focus(item_id)
                self.measurements_table.see(item_id)
                self._set_status(f"Найдена запись по колонке «{selected_label}».", emit_log=False)
                return
        self._set_status("Совпадений не найдено.", logging.WARNING)

    def open_table_context_menu(self, event) -> None:
        if not hasattr(self, "measurements_table"):
            return
        item_id = self.measurements_table.identify_row(event.y)
        if not item_id:
            return
        self.measurements_table.selection_set(item_id)
        self.measurements_table.focus(item_id)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Удалить строку", command=self.delete_selected_table_row)
        menu.add_command(label="Сгладить строку", command=self.smooth_selected_table_row)
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def delete_selected_table_row(self) -> None:
        index = self._selected_table_record_index()
        if index is None:
            self._set_status("Строка таблицы не выбрана.", logging.WARNING)
            return
        confirmed = messagebox.askyesno(
            "Удаление данных",
            (
                "Вы уверены, что хотите удалить выбранные данные?\n\n"
                "Это приведёт к невозможности восстановить их."
            ),
            parent=self,
        )
        if not confirmed:
            return
        records = list(self.measurement_records)
        del records[index]
        self._reload_records_after_table_edit(records, status="Строка удалена из таблицы и графика.")

    def smooth_selected_table_row(self) -> None:
        index = self._selected_table_record_index()
        if index is None:
            self._set_status("Строка таблицы не выбрана.", logging.WARNING)
            return
        if index <= 0 or index >= len(self.measurement_records) - 1:
            self._set_status("Сглаживание доступно только для внутренних строк.", logging.WARNING)
            return
        records = list(self.measurement_records)
        left = records[index - 1]
        current = records[index]
        right = records[index + 1]

        def average_optional(a, b):
            if a is None and b is None:
                return None
            if a is None:
                return b
            if b is None:
                return a
            return (float(a) + float(b)) / 2.0

        records[index] = MeasurementRecord(
            timestamp=current.timestamp,
            mass=average_optional(left.mass, right.mass),
            furnace_pv=average_optional(left.furnace_pv, right.furnace_pv),
            furnace_sv=average_optional(left.furnace_sv, right.furnace_sv),
            mass_timestamp=current.mass_timestamp,
            furnace_pv_timestamp=current.furnace_pv_timestamp,
            furnace_sv_timestamp=current.furnace_sv_timestamp,
        )
        self._reload_records_after_table_edit(records, status="Строка сглажена по соседним точкам.")

    def toggle_table_columns_panel(self) -> None:
        self.table_columns_collapsed = not self.table_columns_collapsed
        if self.table_columns_collapsed:
            self.table_columns_panel.grid_remove()
            self.table_columns_toggle_button.configure(text="▶")
        else:
            self.table_columns_panel.grid()
            self.table_columns_toggle_button.configure(text="◀")

    def toggle_plot_side_panels(self) -> None:
        self.plot_side_panels_collapsed = not self.plot_side_panels_collapsed
        self._update_plot_side_panels_state()

    def _build_plot_side_toggle_strip(self, parent) -> None:
        strip_width = max(self._pad_x(38), int(42 * self.ui_scale))
        self.plot_side_toggle_strip = tk.Canvas(
            parent,
            width=strip_width,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.plot_side_toggle_strip.grid(
            row=1,
            column=1,
            sticky="ns",
            padx=(self._pad_x(6), self._pad_x(4)),
            pady=(self._pad_y(4), 0),
        )
        self.plot_side_toggle_strip.bind(
            "<Button-1>", lambda _event: self.toggle_plot_side_panels()
        )
        self.plot_side_toggle_strip.bind(
            "<Configure>", lambda _event: self._redraw_plot_side_toggle_strip()
        )

    def _redraw_plot_side_toggle_strip(self) -> None:
        if not hasattr(self, "plot_side_toggle_strip"):
            return
        canvas = self.plot_side_toggle_strip
        canvas.delete("all")
        width = max(1, int(canvas.winfo_width()))
        height = max(1, int(canvas.winfo_height()))
        if height <= 1:
            return
        palette = self.theme_manager.palette
        fill = palette.accent if palette.name == "dark" else "#D6F2EE"
        text_color = "#081016" if palette.name == "dark" else "#0F3F3A"
        border = palette.accent
        arrow = "◀" if self.plot_side_panels_collapsed else "▶"
        canvas.create_rectangle(0, 0, width, height, fill=fill, outline=border, width=1)
        canvas.create_text(
            width // 2,
            max(self._pad_y(16), int(20 * self.ui_scale)),
            text=arrow,
            fill=text_color,
            font=("Segoe UI Symbol", max(10, int(12 * self.ui_scale))),
        )
        canvas.create_text(
            width // 2,
            height // 2 + self._pad_y(4),
            text="Вид / инструменты / расчёты",
            angle=90,
            fill=text_color,
            font=("Segoe UI Semibold", max(10, int(11 * self.ui_scale))),
            justify="center",
        )

    def _update_plot_side_panels_state(self) -> None:
        for panel_key in ("tools", "views", "calculations"):
            panel_info = self.plot_side_panels.get(panel_key)
            if not panel_info:
                continue
            panel = panel_info["panel"]
            title_label = panel_info["title_label"]
            body_canvas = panel_info["canvas"]
            scrollbar = panel_info["scrollbar"]
            width = int(panel_info["width"])
            if self.plot_side_panels_collapsed:
                panel.grid_remove()
            else:
                panel.grid()
                title_label.grid()
                body_canvas.grid()
                panel.configure(width=width, padding=self._pad(8, 8))
                if (
                    body_canvas.bbox("all")
                    and body_canvas.winfo_reqheight() < body_canvas.bbox("all")[3]
                ):
                    scrollbar.grid()
                else:
                    scrollbar.grid_remove()
        if self.plot_side_panels_collapsed:
            self.plot_card.grid_columnconfigure(2, minsize=0)
            self.plot_card.grid_columnconfigure(3, minsize=0)
            self.plot_card.grid_columnconfigure(4, minsize=0)
        else:
            self.plot_card.grid_columnconfigure(2, minsize=int(52 * self.ui_scale))
            self.plot_card.grid_columnconfigure(3, minsize=int(52 * self.ui_scale))
            self.plot_card.grid_columnconfigure(4, minsize=int(64 * self.ui_scale))
        self._redraw_plot_side_toggle_strip()
        if hasattr(self, "plotter"):
            self.after_idle(self.plotter.autoscale)

    def open_settings_window(self) -> None:
        if self.controller.running:
            message = "Во время записи настройки заблокированы. Сначала поставьте запись на паузу или остановите её."
            self._set_status(message, logging.WARNING)
            messagebox.showwarning("Настройки заблокированы", message, parent=self)
            return
        if hasattr(self, "_settings_window") and self._settings_window.winfo_exists():
            self._settings_window.focus_set()
            return

        self._restore_settings_from_disk()

        self._settings_window = tk.Toplevel(self)
        self._settings_window.title("Настройки")
        self._settings_window.grab_set()
        self._settings_window.protocol("WM_DELETE_WINDOW", self._close_settings_window)
        self._settings_window.grid_rowconfigure(0, weight=1)
        self._settings_window.grid_columnconfigure(0, weight=1)
        self._settings_window._datafusion_zoomed = False  # type: ignore[attr-defined]
        self._settings_window.resizable(True, True)

        area = _windows_work_area()
        if area is not None:
            x, y, width, height = area
            min_width = min(max(int(1160 * self.ui_scale), 1040), max(width - 24, 760))
            min_height = min(max(int(660 * self.ui_scale), 620), max(height - 24, 520))
            win_width = min(
                max(min_width, int(width * 0.72)), max(width - 24, min_width)
            )
            win_height = min(
                max(min_height, int(height * 0.72)), max(height - 28, min_height)
            )
            self._settings_window.minsize(min_width, min_height)
            self._settings_window.maxsize(width, height)
            self._settings_window.geometry(f"{win_width}x{win_height}+{x + 8}+{y + 8}")
        else:
            self._settings_window.minsize(
                int(1160 * self.ui_scale), int(680 * self.ui_scale)
            )
            width = int(self.winfo_screenwidth() * 0.72)
            height = int(self.winfo_screenheight() * 0.72)
            self._settings_window.geometry(f"{width}x{height}+30+30")

        outer = ttk.Frame(
            self._settings_window, style="Card.TFrame", padding=self._pad(16, 16)
        )
        outer.grid(row=0, column=0, sticky="nsew")
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)
        self._settings_hint_labels = []
        self._settings_warning_labels = []

        hero = ttk.Frame(outer, style="Card.TFrame", padding=self._pad(12, 8))
        hero.grid(row=0, column=0, sticky="ew", pady=(0, self._pad_y(10)))
        hero.grid_columnconfigure(0, weight=1)
        self.settings_title_label = ttk.Label(
            hero, text="⚙ Параметры подключения и оформления", style="CardTitle.TLabel"
        )
        self.settings_title_label.configure(
            font=("Segoe UI Semibold", max(15, int(17 * self.ui_scale)))
        )
        self.settings_title_label.grid(row=0, column=0, sticky="w")
        self.settings_intro_label = ttk.Label(
            hero,
            text="Выберите порты, проверьте связь с приборами, настройте тестовый режим и оформление интерфейса.",
            style="CardText.TLabel",
            wraplength=int(980 * self.ui_scale),
            justify="left",
        )
        self.settings_intro_label.configure(
            font=("Segoe UI", max(11, int(12 * self.ui_scale)))
        )
        self.settings_intro_label.grid(row=1, column=0, sticky="w")

        self.test_mode_banner = tk.Label(
            hero,
            text="Тестовый режим позволяет отдельно эмулировать весы, печь или оба устройства без реального стенда.",
            anchor="w",
            justify="left",
            padx=self._pad_x(12),
            pady=self._pad_y(8),
            bd=1,
            relief="solid",
            wraplength=int(980 * self.ui_scale),
        )
        self.test_mode_banner._datafusion_warning = True  # type: ignore[attr-defined]
        self._settings_warning_labels.append(self.test_mode_banner)
        self.test_mode_banner.grid(
            row=2, column=0, sticky="ew", pady=(self._pad_y(8), 0)
        )
        self._update_test_mode_banner()

        self.settings_actions_hint = ttk.Label(
            hero,
            text="Быстрые действия",
            style="Subtitle.TLabel",
        )
        self.settings_actions_hint.grid(
            row=3, column=0, sticky="w", pady=(self._pad_y(10), 0)
        )

        hero_controls = ttk.Frame(hero, style="Card.TFrame")
        hero_controls.grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(self._pad_y(6), 0)
        )
        for idx in range(3):
            hero_controls.grid_columnconfigure(idx, weight=1)
        self.settings_reset_button = ttk.Button(
            hero_controls,
            text="Сброс по умолчанию",
            style="SettingsSoft.TButton",
            command=self.reset_default_settings,
            width=18,
        )
        self.settings_reset_button.grid(
            row=0, column=0, sticky="ew", padx=(0, self._pad_x(6))
        )
        self.settings_save_button = ttk.Button(
            hero_controls,
            text="Сохранить",
            style="SettingsAccent.TButton",
            command=self.save_settings,
            width=18,
        )
        self.settings_save_button.grid(
            row=0, column=1, sticky="ew", padx=self._pad_pair(3)
        )
        self.settings_exit_button = ttk.Button(
            hero_controls,
            text="Выход",
            style="SettingsSoft.TButton",
            command=self._close_settings_window,
            width=18,
        )
        self.settings_exit_button.grid(
            row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0)
        )

        self.autosave_checkbox = ttk.Checkbutton(
            hero_controls,
            text="Автосохранение",
            variable=self.autosave_settings_var,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        self.autosave_checkbox.grid(
            row=1,
            column=1,
            sticky="w",
            pady=(self._pad_y(8), 0),
            padx=(0, self._pad_x(10)),
        )
        ToolTip(
            self.autosave_checkbox,
            self._build_setting_tooltip(
                "app.autosave_settings",
                "Автоматически сохраняет корректные изменения в config.yaml сразу после редактирования.",
            ),
        )
        self.settings_mode_badge = tk.Label(
            hero_controls,
            textvariable=self.settings_mode_var,
            anchor="w",
            padx=self._pad_x(12),
            pady=self._pad_y(6),
            bd=1,
            relief="solid",
        )
        self.settings_mode_badge.grid(
            row=1, column=2, sticky="ew", pady=(self._pad_y(8), 0)
        )
        self.settings_mode_hint = ttk.Label(
            hero_controls,
            textvariable=self.settings_mode_hint_var,
            style="CardText.TLabel",
            wraplength=int(620 * self.ui_scale),
            justify="left",
        )
        self.settings_mode_hint.configure(
            font=("Segoe UI", max(10, int(11 * self.ui_scale)))
        )
        self.settings_mode_hint.grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(self._pad_y(6), 0)
        )

        self._settings_tab_canvases = []
        notebook = ttk.Notebook(outer)
        notebook.grid(row=1, column=0, sticky="nsew")

        devices_tab, devices_body = self._create_settings_scroll_tab(notebook)
        scales_tab, scales_body = self._create_settings_scroll_tab(notebook)
        furnace_tab, furnace_body = self._create_settings_scroll_tab(notebook)
        app_tab, app_body = self._create_settings_scroll_tab(notebook)
        ui_tab, ui_body = self._create_settings_scroll_tab(notebook)

        notebook.add(devices_tab, text="Устройства")
        notebook.add(scales_tab, text="Весы")
        notebook.add(furnace_tab, text="Печь")
        notebook.add(app_tab, text="Приложение")
        notebook.add(ui_tab, text="Интерфейс")

        devices_frame = ttk.LabelFrame(
            devices_body,
            text="Устройства",
            style="Section.TLabelframe",
            padding=self._pad(12, 10),
        )
        devices_frame.grid(row=0, column=0, sticky="nsew")
        devices_frame.grid_columnconfigure(
            1, weight=3, minsize=int(520 * self.ui_scale)
        )
        devices_frame.grid_columnconfigure(
            2, weight=2, minsize=int(540 * self.ui_scale)
        )

        ttk.Label(devices_frame, text="Порт весов", style="CardText.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, self._pad_x(12)),
            pady=(self._pad_y(6), self._pad_y(6)),
        )
        self.settings_scale_combo = ttk.Combobox(
            devices_frame, textvariable=self.scale_port_display_var, width=96
        )
        self.settings_scale_combo.configure(
            font=("Segoe UI", max(11, int(12 * self.ui_scale)))
        )
        self.settings_scale_combo.grid(
            row=0, column=1, sticky="ew", pady=(self._pad_y(6), self._pad_y(6))
        )
        self._make_settings_note_label(
            devices_frame,
            "Формат списка: COM-порт - имя интерфейса в системе - тип устройства. Можно ввести вручную.",
            wraplength=int(640 * self.ui_scale),
        ).grid(row=0, column=2, sticky="nsew", padx=(self._pad_x(12), 0))

        ttk.Label(devices_frame, text="Порт печи", style="CardText.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, self._pad_x(12)),
            pady=(self._pad_y(6), self._pad_y(6)),
        )
        self.settings_furnace_combo = ttk.Combobox(
            devices_frame, textvariable=self.furnace_port_display_var, width=96
        )
        self.settings_furnace_combo.configure(
            font=("Segoe UI", max(11, int(12 * self.ui_scale)))
        )
        self.settings_furnace_combo.grid(
            row=1, column=1, sticky="ew", pady=(self._pad_y(6), self._pad_y(6))
        )
        self._make_settings_note_label(
            devices_frame,
            "Для печи обычно выбирается USB-RS485 адаптер. Ищите не только RS-485 в названии, но и системные имена драйвера адаптера: CH340, WCH, USB-SERIAL CH340, USB Serial, UART.",
            wraplength=int(640 * self.ui_scale),
        ).grid(row=1, column=2, sticky="nsew", padx=(self._pad_x(12), 0))

        ttk.Checkbutton(
            devices_frame,
            text="Автопоиск COM-портов при запуске",
            variable=self.auto_detect_ports_var,
            style="Card.TCheckbutton",
            takefocus=False,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(self._pad_y(6), 0))
        self._make_settings_note_label(
            devices_frame,
            "Автопоиск подбирает COM-порты отдельно для весов и печи и сразу показывает, что назначено, а что не найдено.",
            wraplength=int(980 * self.ui_scale),
        ).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(6), 0))

        device_buttons = ttk.Frame(devices_frame, style="Card.TFrame")
        device_buttons.grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(8), 0)
        )
        for idx in range(4):
            device_buttons.grid_columnconfigure(idx, weight=1)
        ttk.Button(
            device_buttons,
            text="Обновить порты",
            style="SettingsSoft.TButton",
            command=self.refresh_ports,
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            device_buttons,
            text="Автопоиск",
            style="SettingsSoft.TButton",
            command=self._run_port_autodetect,
        ).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(
            device_buttons,
            text="Проверить весы",
            style="SettingsSoft.TButton",
            command=self.probe_scale_device,
        ).grid(row=0, column=2, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(
            device_buttons,
            text="Проверить печь",
            style="SettingsSoft.TButton",
            command=self.probe_furnace_device,
        ).grid(row=0, column=3, sticky="ew", padx=(self._pad_x(6), 0))
        ttk.Label(
            devices_frame,
            textvariable=self.device_check_var,
            style="CardText.TLabel",
            wraplength=int(840 * self.ui_scale),
            justify="left",
        ).grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(self._pad_y(10), 0),
        )
        self._make_settings_note_label(
            devices_frame,
            "Драйвер для Adam Highland HCB: HCB Highland USB Driver 64 Bit.\n"
            "Страница загрузки: https://adamequipment.co.uk/support/software-downloads.html",
            wraplength=int(980 * self.ui_scale),
        ).grid(row=6, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(10), 0))
        ttk.Button(
            devices_frame,
            text="Открыть страницу драйвера Adam",
            style="SettingsSoft.TButton",
            command=lambda: webbrowser.open(
                "https://adamequipment.co.uk/support/software-downloads.html"
            ),
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(self._pad_y(8), 0))
        self._make_settings_note_label(
            devices_frame,
            "Быстрая настройка весов Adam для живого графика:\n"
            "1. Во время самотестирования нажмите [Mode].\n"
            "2. Дойдите до меню F3 SEr.\n"
            "3. Выберите интерфейс S USB.\n"
            "4. Выберите режим P2 Con для непрерывной передачи.\n"
            "5. Установите скорость b 9600.\n"
            "6. Установите формат 8n1.\n"
            "7. Установите формат For2.\n"
            "8. Вернитесь в режим взвешивания кнопкой [Print].\n"
            "9. В программе используйте continuous. Если поток нестабилен, включите P1 Prt и опрос командой P.",
            wraplength=int(980 * self.ui_scale),
        ).grid(row=8, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(10), 0))

        self._sync_port_display_vars()
        values = [self._settings_port_label(port) for port in self.available_ports]
        self.settings_scale_combo.configure(values=values)
        self.settings_furnace_combo.configure(values=values)

        tab_parent_map = {
            "Весы": scales_body,
            "Печь": furnace_body,
            "Приложение": app_body,
            "Интерфейс и файлы": ui_body,
        }

        font_controls_frame = ttk.LabelFrame(
            ui_body,
            text="Размер шрифта",
            style="Section.TLabelframe",
            padding=self._pad(12, 10),
        )
        font_controls_frame.grid(
            row=0, column=0, sticky="ew", pady=(0, self._pad_y(10))
        )
        for idx in range(5):
            font_controls_frame.grid_columnconfigure(
                idx, weight=1 if idx in {1, 3} else 0
            )
        ttk.Label(
            font_controls_frame, text="Масштаб интерфейса", style="CardText.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            font_controls_frame,
            text="-",
            style="SettingsSoft.TButton",
            command=lambda: self.adjust_font_scale(-0.05),
            width=4,
        ).grid(row=0, column=1, padx=(self._pad_x(10), self._pad_x(6)))
        self.settings_font_scale_value_label = ttk.Label(
            font_controls_frame, text="", style="CardTitle.TLabel", anchor="center"
        )
        self.settings_font_scale_value_label.grid(
            row=0, column=2, padx=(0, self._pad_x(6))
        )
        ttk.Button(
            font_controls_frame,
            text="+",
            style="SettingsSoft.TButton",
            command=lambda: self.adjust_font_scale(0.05),
            width=4,
        ).grid(row=0, column=3, padx=(0, self._pad_x(6)))
        ttk.Button(
            font_controls_frame,
            text="Сброс",
            style="SettingsSoft.TButton",
            command=self.reset_font_scale,
        ).grid(row=0, column=4)
        self._make_settings_note_label(
            font_controls_frame,
            "Этот блок дублирует управление размером шрифта из главного окна. Изменение применяется ко всей программе.",
            wraplength=int(820 * self.ui_scale),
        ).grid(row=1, column=0, columnspan=5, sticky="ew", pady=(self._pad_y(8), 0))

        for section_title, fields in SETTINGS_SECTIONS:
            tab_parent = tab_parent_map.get(section_title, app_tab)
            frame = ttk.LabelFrame(
                tab_parent,
                text=section_title,
                style="Section.TLabelframe",
                padding=self._pad(12, 10),
            )
            frame.grid(row=1 if tab_parent is ui_body else 0, column=0, sticky="nsew")
            frame.grid_columnconfigure(1, weight=1)
            frame.grid_columnconfigure(2, weight=0, minsize=int(360 * self.ui_scale))
            self._build_settings_section(frame, fields)

        self.furnace_access_warning_var = tk.StringVar(value="")
        self.furnace_access_warning_label = tk.Label(
            furnace_body,
            textvariable=self.furnace_access_warning_var,
            anchor="w",
            justify="left",
            padx=self._pad_x(12),
            pady=self._pad_y(8),
            relief="solid",
            bd=1,
            wraplength=int(900 * self.ui_scale),
        )
        self.furnace_access_warning_label._datafusion_warning = True  # type: ignore[attr-defined]
        self._settings_warning_labels.append(self.furnace_access_warning_label)
        self.furnace_access_warning_label.grid(
            row=1, column=0, sticky="ew", pady=(self._pad_y(10), 0)
        )
        self._apply_theme_to_toplevel(self._settings_window)
        self._update_settings_control_states()
        self._settings_window.update_idletasks()
        self._settings_window.focus_set()

    def _build_settings_section(self, parent, fields) -> None:
        parent.grid_columnconfigure(0, weight=0, minsize=int(210 * self.ui_scale))
        parent.grid_columnconfigure(1, weight=1, minsize=int(210 * self.ui_scale))
        parent.grid_columnconfigure(2, weight=0, minsize=int(360 * self.ui_scale))
        for row, (key, label, kind, tooltip, choices) in enumerate(fields):
            parent.grid_rowconfigure(row, weight=0)
            tooltip_text = self._build_setting_tooltip(key, tooltip)
            label_widget = ttk.Label(parent, text=label, style="CardText.TLabel")
            label_widget.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
            label_widget.grid(
                row=row,
                column=0,
                sticky="w",
                padx=(0, self._pad_x(10)),
                pady=(self._pad_y(4), self._pad_y(4)),
            )
            ToolTip(label_widget, tooltip_text)

            var = self.setting_vars[key]
            if kind == "bool":
                widget = ttk.Checkbutton(
                    parent, variable=var, style="Card.TCheckbutton", takefocus=False
                )
                widget.configure(text=" ")
            elif kind == "combo":
                combo_values = list(choices or ())
                if key == "app.test_mode_scope":
                    combo_values = list(TEST_MODE_SCOPE_VALUES)
                widget = ttk.Combobox(
                    parent, textvariable=var, state="readonly", values=combo_values
                )
            else:
                widget = ttk.Entry(parent, textvariable=var)
            if kind in {"combo", "entry"}:
                widget.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
            if kind == "bool":
                widget.configure(style="Card.TCheckbutton")
            self._settings_input_widgets.append(widget)
            widget.grid(
                row=row, column=1, sticky="ew", pady=(self._pad_y(4), self._pad_y(4))
            )
            hint_label = self._make_settings_note_label(
                parent,
                f"{tooltip} По умолчанию: {self._display_setting_default(key)}",
                wraplength=int(360 * self.ui_scale),
            )
            hint_label.grid(
                row=row,
                column=2,
                sticky="nsew",
                padx=(self._pad_x(10), 0),
                pady=(self._pad_y(4), self._pad_y(4)),
            )
            ToolTip(widget, tooltip_text)

    def refresh_ports(self) -> None:
        self.available_ports = list_available_ports()
        self.port_map = {port.device.upper(): port for port in self.available_ports}
        self.port_display_map = {
            port.device.upper(): self._settings_port_label(port)
            for port in self.available_ports
        }

        for item_id in self.port_tree.get_children():
            self.port_tree.delete(item_id)

        for port in self.available_ports:
            self.port_tree.insert(
                "",
                "end",
                iid=port.device,
                values=(port.device, guess_port_kind(port), port.description),
            )

        if self.available_ports:
            self.port_status_var.set(f"Найдено COM-портов: {len(self.available_ports)}")
            self._set_status("Список COM-портов обновлён.")
        else:
            self.port_status_var.set("COM-порты не найдены.")
            self._set_status("COM-порты не обнаружены.", logging.WARNING)

        if (
            hasattr(self, "settings_scale_combo")
            and self.settings_scale_combo.winfo_exists()
        ):
            values = [self._settings_port_label(port) for port in self.available_ports]
            self.settings_scale_combo.configure(values=values)
            self.settings_furnace_combo.configure(values=values)
            self._sync_port_display_vars()

        self._refresh_diagnostics()
        self._update_action_buttons()

    def _run_port_autodetect(self, on_startup: bool = False) -> None:
        if on_startup and not bool(self.auto_detect_ports_var.get()):
            return
        self.available_ports = list_available_ports()
        self.port_map = {port.device.upper(): port for port in self.available_ports}
        self.port_display_map = {
            port.device.upper(): self._settings_port_label(port)
            for port in self.available_ports
        }
        detected = detect_preferred_ports(self.available_ports)
        assigned: list[str] = []

        furnace_port = detected.get("furnace")
        if furnace_port is not None:
            self.furnace_port_var.set(furnace_port.device)
            assigned.append(f"печь: {furnace_port.device}")

        scale_port = detected.get("scale")
        if scale_port is not None:
            self.scale_port_var.set(scale_port.device)
            assigned.append(f"весы: {scale_port.device}")

        self._sync_port_display_vars()
        self._refresh_diagnostics()

        missing: list[str] = []
        if furnace_port is None:
            missing.append("печь")
        if scale_port is None:
            missing.append("весы")

        status_parts: list[str] = []
        if assigned:
            status_parts.append(f"назначил: {', '.join(assigned)}")
        if missing:
            status_parts.append(f"не нашёл: {', '.join(missing)}")
            message = f"Автопоиск не нашёл подходящие COM-порты для: {', '.join(missing)}."
            self.port_status_var.set(
                f"Автопоиск: {'; '.join(status_parts)}."
                if status_parts
                else message
            )
            self.device_check_var.set(
                f"Автопоиск: {'; '.join(status_parts)}."
                if status_parts
                else message
            )
            self._set_status(message, logging.WARNING)
            if on_startup:
                messagebox.showwarning("DataFusion RT", message, parent=self)
        elif assigned:
            success_message = f"Автопоиск: {'; '.join(status_parts)}."
            self.device_check_var.set(success_message)
            self.port_status_var.set(success_message)
            self._set_status(success_message, emit_log=not on_startup)
        else:
            message = "Автопоиск не нашёл подходящие COM-порты для весов и печи."
            self.device_check_var.set(message)
            self.port_status_var.set(message)
            self._set_status(message, logging.WARNING)

    def assign_selected_to_scale(self) -> None:
        device = self._get_selected_tree_device()
        if not device:
            self._set_status("Сначала выберите COM-порт.", logging.WARNING)
            return
        self.scale_port_var.set(device)
        self.config_data.scale.port = device
        save_config(self.config_data, self.config_path)
        self._refresh_assignment_summary(device, "Весы")
        self._refresh_diagnostics()
        self._set_status(f"Порт {device} назначен для весов.")

    def assign_selected_to_furnace(self) -> None:
        device = self._get_selected_tree_device()
        if not device:
            self._set_status("Сначала выберите COM-порт.", logging.WARNING)
            return
        self.furnace_port_var.set(device)
        self.config_data.furnace.port = device
        save_config(self.config_data, self.config_path)
        self._refresh_assignment_summary(device, "Печь")
        self._refresh_diagnostics()
        self._set_status(f"Порт {device} назначен для печи.")

    def probe_scale_device(self) -> None:
        if not self._commit_settings_to_config(show_errors=True):
            return
        self.config_data.scale.port = self.scale_port_var.get().strip()
        ok, message = probe_scale_port(
            self.config_data.scale,
            test_mode=self._scale_test_mode_active(),
            logger=self.logger.getChild("probe.scale"),
        )
        self.last_scale_connected = bool(ok)
        if ok:
            self._last_scale_seen_at = time.monotonic()
        self.device_check_var.set(f"Весы: {message}")
        self.diag_status_var.set(message)
        self._set_status(message, logging.INFO if ok else logging.WARNING)
        messagebox.showinfo(
            "Проверка весов", message, parent=self
        ) if ok else messagebox.showwarning("Проверка весов", message, parent=self)

    def probe_furnace_device(self) -> None:
        if not self._commit_settings_to_config(show_errors=True):
            return
        self.config_data.furnace.port = self.furnace_port_var.get().strip()
        ok, message = probe_furnace_port(
            self.config_data.furnace,
            test_mode=self._furnace_test_mode_active(),
            logger=self.logger.getChild("probe.furnace"),
        )
        self.last_furnace_connected = bool(ok)
        if ok:
            self._last_furnace_seen_at = time.monotonic()
        self.device_check_var.set(f"Печь: {message}")
        self.diag_status_var.set(message)
        self._set_status(message, logging.INFO if ok else logging.WARNING)
        messagebox.showinfo(
            "Проверка печи", message, parent=self
        ) if ok else messagebox.showwarning("Проверка печи", message, parent=self)

    def start_acquisition(self) -> None:
        if not self._commit_settings_to_config(show_errors=True):
            return
        if self.controller.running:
            self._set_status("Опрос уже запущен.")
            return

        missing: list[str] = []
        if (
            self.config_data.scale.enabled
            and not self._scale_test_mode_active()
            and not self.scale_port_var.get().strip()
        ):
            missing.append("весы")
        if (
            self.config_data.furnace.enabled
            and not self._furnace_test_mode_active()
            and not self.furnace_port_var.get().strip()
        ):
            missing.append("печь")
        if missing:
            message = f"Не назначен COM-порт для: {', '.join(missing)}."
            self._set_status(message, logging.WARNING)
            self.left_panel_visible = True
            self.view_mode_var.set("advanced")
            self._update_side_panels()
            messagebox.showwarning("DataFusion RT", message, parent=self)
            return

        self.controller.apply_runtime_settings(
            scale_port=self.scale_port_var.get().strip(),
            furnace_port=self.furnace_port_var.get().strip(),
            test_mode=self.config_data.app.test_mode,
            test_mode_scope=self.config_data.app.test_mode_scope,
            scale_enabled=self.config_data.scale.enabled,
            furnace_enabled=self.config_data.furnace.enabled,
        )
        save_config(self.config_data, self.config_path)
        self.plotter.set_max_points(self.config_data.app.max_points_on_plot)
        if not self._acquisition_paused:
            self._session_started_at = None
            self._baseline_mass = None
            self._baseline_mass_timestamp = None
            self._baseline_mass_samples.clear()
            self.plotter.clear()
        self.controller.start()
        self._acquisition_paused = False
        self._set_status("Измерение запущено.")
        self._update_action_buttons()

    def stop_acquisition(self) -> None:
        if not self.controller.running:
            self._set_status("Опрос уже остановлен.")
            return
        self.controller.stop()
        self._acquisition_paused = False
        self._set_status("Измерение остановлено.")
        self._update_action_buttons()
        saved = self.autosave_session()
        if saved is not None:
            self._set_status(f"Сессия автосохранена: {saved.name}", logging.INFO)

    def pause_acquisition(self) -> None:
        if not self.controller.running:
            self._set_status("Измерение уже остановлено.", logging.WARNING)
            return
        self.controller.stop()
        self._acquisition_paused = True
        self._set_status("Измерение поставлено на паузу. Данные не читаются до повторного старта.", logging.INFO)
        self._update_action_buttons()

    def clear_graph(self) -> None:
        self.plotter.clear()
        self._session_started_at = None
        self._baseline_mass = None
        self._baseline_mass_timestamp = None
        self._baseline_mass_samples.clear()
        if hasattr(self, "measurements_table"):
            for item_id in self.measurements_table.get_children():
                self._table_timestamp_map.pop(item_id, None)
                self.measurements_table.delete(item_id)
        self._reset_readouts()
        self._set_status("График и таблица очищены.")

    def export_with_default_format(self) -> None:
        self.export_measurements(default_ext=".csv")

    def export_measurements(self, *, default_ext: str = ".csv") -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Экспорт данных",
            defaultextension=default_ext,
            initialfile=f"measurements_export{default_ext}",
            filetypes=[("CSV", "*.csv"), ("Excel", "*.xlsx")],
        )
        if not target:
            return
        destination = Path(target)
        if not destination.suffix:
            destination = destination.with_suffix(default_ext)

        frame = self._build_table_export_frame()
        success, message = self.export_service.export_frame(frame, destination)
        self._set_status(message, logging.INFO if success else logging.WARNING)
        self.diag_status_var.set(message)
        if success:
            messagebox.showinfo("Экспорт данных", message, parent=self)
        else:
            messagebox.showwarning("Экспорт данных", message, parent=self)

    def _build_table_export_frame(self) -> pd.DataFrame:
        return build_table_export_df(self)

    def _build_session_data(self) -> dict:
        return build_session_payload(self)

    def save_session(self) -> None:
        save_session_data(self)

    def autosave_session(self) -> Path | None:
        return autosave_session_data(self)

    def _cleanup_autosaves(self, max_count: int = 5) -> None:
        cleanup_autosave_files(self, max_count=max_count)

    def _autosave_timer(self) -> None:
        autosave_session_timer(self)

    def load_session(self, filepath: Path) -> bool:
        return load_session_data(self, filepath)

    def update_restore_session_menu(self) -> None:
        update_restore_session_menu_data(self)

    def _normalize_imported_timestamps(self, values: list[object]) -> tuple[list[str], bool]:
        raw_values = ["" if value is None else str(value).strip() for value in values]
        if not raw_values:
            return [], False
        normalized: list[str] = []
        parsed_datetimes: list[datetime | None] = []
        all_clock_only = True
        for raw in raw_values:
            if not raw:
                parsed_datetimes.append(None)
                all_clock_only = False
                continue
            parsed_exact = coerce_timestamp(raw)
            if parsed_exact is not None and ("T" in raw or "-" in raw or "+" in raw):
                parsed_datetimes.append(parsed_exact)
                all_clock_only = False
                continue
            clock_dt = None
            for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    clock_dt = parsed
                    break
                except ValueError:
                    continue
            if clock_dt is None:
                parsed_datetimes.append(None)
                all_clock_only = False
            else:
                parsed_datetimes.append(clock_dt)
        if not all_clock_only:
            for raw, parsed in zip(raw_values, parsed_datetimes):
                normalized.append(raw if raw else (parsed.isoformat() if parsed is not None else ""))
            return normalized, False

        base_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        day_offset = 0
        previous_seconds: int | None = None
        for parsed in parsed_datetimes:
            if parsed is None:
                normalized.append("")
                continue
            seconds = parsed.hour * 3600 + parsed.minute * 60 + parsed.second
            if previous_seconds is not None and seconds < previous_seconds:
                day_offset += 1
            previous_seconds = seconds
            actual = base_day + timedelta(days=day_offset, seconds=seconds, microseconds=parsed.microsecond)
            normalized.append(actual.isoformat())
        return normalized, True

    def _build_import_records_from_frame(self, df: pd.DataFrame) -> tuple[list[MeasurementRecord], bool]:
        records: list[MeasurementRecord] = []
        timestamps_raw: list[object] = []
        mass_values_raw: list[object] = []
        furnace_pv_raw: list[object] = []
        furnace_sv_raw: list[object] = []

        for _, row in df.iterrows():
            timestamp = None
            mass = None
            furnace_pv = None
            furnace_sv = None
            for col in df.columns:
                col_lower = str(col).lower()
                if "timestamp" in col_lower or "время" in col_lower or "дата" in col_lower:
                    timestamp = row[col]
                elif "mass" in col_lower or "масса" in col_lower or "вес" in col_lower:
                    mass = row[col]
                elif "furnace_pv" in col_lower or "pv" in col_lower or "камера" in col_lower:
                    furnace_pv = row[col]
                elif "furnace_sv" in col_lower or "sv" in col_lower or "термопара" in col_lower:
                    furnace_sv = row[col]
            if timestamp is None and len(df.columns) > 0:
                timestamp = row[df.columns[0]]
            if isinstance(timestamp, pd.Timestamp):
                timestamp = timestamp.isoformat()
            timestamps_raw.append(timestamp)
            mass_values_raw.append(mass)
            furnace_pv_raw.append(furnace_pv)
            furnace_sv_raw.append(furnace_sv)

        timestamps, compatibility_mode = self._normalize_imported_timestamps(timestamps_raw)
        for idx, timestamp in enumerate(timestamps):
            records.append(
                MeasurementRecord(
                    timestamp=timestamp or "",
                    mass=float(mass_values_raw[idx]) if mass_values_raw[idx] is not None and not pd.isna(mass_values_raw[idx]) else None,
                    furnace_pv=float(furnace_pv_raw[idx]) if furnace_pv_raw[idx] is not None and not pd.isna(furnace_pv_raw[idx]) else None,
                    furnace_sv=float(furnace_sv_raw[idx]) if furnace_sv_raw[idx] is not None and not pd.isna(furnace_sv_raw[idx]) else None,
                )
            )
        return records, compatibility_mode

    def import_data_from_file(self) -> None:
        filetypes = [
            ("Все поддерживаемые файлы", "*.json *.csv *.xlsx *.xls"),
            ("Сессия JSON", "*.json"),
            ("Данные CSV", "*.csv"),
            ("Данные Excel", "*.xlsx *.xls"),
        ]
        filepath = filedialog.askopenfilename(
            parent=self,
            title="Импорт данных из файла",
            filetypes=filetypes,
        )
        if not filepath:
            return

        path = Path(filepath)
        if path.suffix.lower() == ".json":
            self.load_session(path)
            return

        # Импорт CSV/Excel
        try:
            if path.suffix.lower() in [".csv"]:
                df = pd.read_csv(path)
            elif path.suffix.lower() in [".xlsx", ".xls"]:
                df = read_excel_frame(path)
            else:
                self._set_status(
                    f"Неподдерживаемый формат файла: {path.suffix}", logging.ERROR
                )
                return

            records, compatibility_mode = self._build_import_records_from_frame(df)

            if not records:
                self._set_status("Не удалось извлечь данные из файла", logging.WARNING)
                messagebox.showwarning(
                    "Импорт данных", "Не удалось извлечь данные из файла", parent=self
                )
                return

            if self.controller.running:
                self.controller.stop()
            self._load_records_into_ui(records)

            self._set_status(
                f"Импортировано {len(records)} записей из {path.name}", logging.INFO
            )
            if compatibility_mode:
                self.logger.info(
                    "Для файла %s применён режим совместимости времени: значения ЧЧ:ММ[:СС] пересчитаны в единую шкалу от первой записи.",
                    path.name,
                )
            messagebox.showinfo(
                "Импорт данных",
                (
                    f"Успешно импортировано {len(records)} записей.\n"
                    "Режим совместимости времени применён."
                    if compatibility_mode
                    else f"Успешно импортировано {len(records)} записей"
                ),
                parent=self,
            )

        except Exception as e:
            self.logger.exception(f"Ошибка импорта данных из файла: {path}")
            self._set_status(f"Ошибка импорта: {e}", logging.ERROR)
            messagebox.showerror("Импорт данных", f"Ошибка: {e}", parent=self)

    def open_cut_data_dialog(self) -> None:
        if not self.measurement_records:
            self._set_status("Нет данных для вырезания.", logging.WARNING)
            return
        window = tk.Toplevel(self)
        window.title("Вырезать интервал")
        window.transient(self)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", lambda: (self.plotter.cancel_interactive_pick(), window.destroy()))
        window.bind("<Destroy>", lambda _event: self.plotter.cancel_interactive_pick(), add="+")
        window.geometry(f"{int(580 * self.ui_scale)}x{int(380 * self.ui_scale)}")
        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Вырезать интервал данных", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Введите границы в формате ЧЧ:ММ:СС или как время от старта ЧЧ:ММ:СС.",
            style="CardText.TLabel",
            wraplength=int(400 * self.ui_scale),
            justify="left",
        ).pack(anchor="w", pady=(self._pad_y(6), self._pad_y(12)))

        form = ttk.Frame(outer, style="Card.TFrame")
        form.pack(fill="x")
        start_var = tk.StringVar()
        end_var = tk.StringVar()
        pick_status_var = tk.StringVar(value="Можно ввести время вручную или отметить диапазон на графике.")
        axis_var = tk.StringVar(value="all")
        smooth_var = tk.BooleanVar(value=True)
        axis_choices = {
            "all": "Все оси",
            "mass": "Масса",
            "temperature": "Камера PV",
            "thermocouple": "Термопара SV",
        }
        for row, (label, variable) in enumerate((("Начало", start_var), ("Конец", end_var))):
            ttk.Label(form, text=label, style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=(0, self._pad_y(8)))
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=(0, self._pad_y(8)))
        ttk.Label(form, text="Ось", style="CardText.TLabel").grid(row=2, column=0, sticky="w", pady=(0, self._pad_y(8)))
        axis_combo = ttk.Combobox(form, state="readonly", values=list(axis_choices.values()), width=22)
        axis_combo.grid(row=2, column=1, sticky="ew", pady=(0, self._pad_y(8)))
        axis_combo.set(axis_choices["all"])
        try:
            axis_combo.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
        except Exception:
            pass
        form.grid_columnconfigure(1, weight=1)
        ttk.Checkbutton(
            outer,
            text="Сгладить после вырезания",
            variable=smooth_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(self._pad_y(10), 0))
        ttk.Label(
            outer,
            textvariable=pick_status_var,
            style="CardText.TLabel",
            wraplength=int(400 * self.ui_scale),
            justify="left",
        ).pack(anchor="w", pady=(self._pad_y(8), 0))

        def finish_pick(start_dt: datetime, end_dt: datetime) -> None:
            start_var.set(self._format_pick_time(start_dt))
            end_var.set(self._format_pick_time(end_dt))
            pick_status_var.set("Диапазон выбран на графике. Проверьте ось и нажмите «Применить».")
            try:
                window.lift()
                window.grab_set()
                window.focus_force()
            except Exception:
                pass

        def start_graph_pick() -> None:
            self.plotter.begin_time_span_pick(finish_pick)
            pick_status_var.set("На графике щёлкните начало и конец диапазона для удаления.")
            try:
                window.grab_release()
                self.focus_force()
            except Exception:
                pass

        def apply_cut() -> None:
            reverse_axis = {label: key for key, label in axis_choices.items()}
            axis_key = reverse_axis.get(axis_combo.get(), "all")
            try:
                changed = self._cut_records_by_time(
                    start_var.get().strip(),
                    end_var.get().strip(),
                    axis_key=axis_key,
                    smooth=bool(smooth_var.get()),
                )
            except ValueError as exc:
                messagebox.showwarning("Вырезать интервал", str(exc), parent=window)
                return
            if changed <= 0:
                self._set_status("Для указанного интервала ничего не найдено.", logging.WARNING)
            else:
                self._set_status(f"Обновлено записей: {changed}.", logging.INFO)
            self.plotter.cancel_interactive_pick()
            window.destroy()

        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", side="bottom", pady=(self._pad_y(14), 0))
        for idx in range(3):
            buttons.grid_columnconfigure(idx, weight=1)
        ttk.Button(buttons, text="Отметить на графике", style="Soft.TButton", command=start_graph_pick).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(buttons, text="Применить", style="Accent.TButton", command=apply_cut).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(buttons, text="Закрыть", style="Soft.TButton", command=lambda: (self.plotter.cancel_interactive_pick(), window.destroy())).grid(row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0))

    def _parse_cut_boundary(self, raw_value: str, *, base_time: datetime) -> datetime:
        if not raw_value:
            raise ValueError("Нужно указать начало и конец интервала.")
        if self.plotter.relative_time_axis_enabled:
            for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
                try:
                    parsed = datetime.strptime(raw_value, fmt)
                    return base_time + timedelta(
                        hours=parsed.hour,
                        minutes=parsed.minute,
                        seconds=parsed.second,
                        microseconds=parsed.microsecond,
                    )
                except ValueError:
                    continue
        for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(raw_value, fmt)
                return base_time.replace(
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=parsed.second,
                    microsecond=parsed.microsecond,
                )
            except ValueError:
                continue
        try:
            parsed = coerce_timestamp(raw_value)
            if parsed is not None:
                return parsed
        except Exception:
            pass
        raise ValueError("Неверный формат времени. Используйте ЧЧ:ММ:СС.")

    def _format_pick_time(self, timestamp: datetime) -> str:
        if self.plotter.relative_time_axis_enabled and self.measurement_records:
            base_time = coerce_timestamp(self.measurement_records[0].timestamp)
            if base_time is not None:
                seconds = max(0, int(round((timestamp - base_time).total_seconds())))
                hours, rem = divmod(seconds, 3600)
                minutes, seconds = divmod(rem, 60)
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return timestamp.strftime("%H:%M:%S")

    def _cut_records_by_time(self, start_raw: str, end_raw: str, *, axis_key: str, smooth: bool) -> int:
        if not self.measurement_records:
            return 0
        try:
            base_time = coerce_timestamp(self.measurement_records[0].timestamp)
            if base_time is None:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Не удалось разобрать время первой записи.") from exc
        start_dt = self._parse_cut_boundary(start_raw, base_time=base_time)
        end_dt = self._parse_cut_boundary(end_raw, base_time=base_time)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt

        def in_window(record: MeasurementRecord) -> bool:
            try:
                ts = coerce_timestamp(record.timestamp)
                if ts is None:
                    raise ValueError
            except ValueError:
                return False
            return start_dt <= ts <= end_dt

        affected_indices = [idx for idx, record in enumerate(self.measurement_records) if in_window(record)]
        if not affected_indices:
            return 0

        updated_records = list(self.measurement_records)
        if axis_key == "all":
            removed_duration = end_dt - start_dt
            updated_records = [record for record in updated_records if not in_window(record)]
            if smooth and removed_duration.total_seconds() > 0:
                shifted: list[MeasurementRecord] = []
                for record in updated_records:
                    try:
                        ts = coerce_timestamp(record.timestamp)
                        if ts is None:
                            raise ValueError
                    except ValueError:
                        shifted.append(record)
                        continue
                    if ts > end_dt:
                        shifted.append(
                            MeasurementRecord(
                                timestamp=(ts - removed_duration).isoformat(),
                                mass=record.mass,
                                furnace_pv=record.furnace_pv,
                                furnace_sv=record.furnace_sv,
                                mass_timestamp=self._shift_optional_timestamp(record.mass_timestamp, removed_duration, end_dt),
                                furnace_pv_timestamp=self._shift_optional_timestamp(record.furnace_pv_timestamp, removed_duration, end_dt),
                                furnace_sv_timestamp=self._shift_optional_timestamp(record.furnace_sv_timestamp, removed_duration, end_dt),
                            )
                        )
                    else:
                        shifted.append(record)
                updated_records = shifted
        else:
            field_name = "furnace_pv" if axis_key == "temperature" else "furnace_sv" if axis_key == "thermocouple" else "mass"
            left_index = next((idx for idx in range(affected_indices[0] - 1, -1, -1) if getattr(updated_records[idx], field_name) is not None), None)
            right_index = next((idx for idx in range(affected_indices[-1] + 1, len(updated_records)) if getattr(updated_records[idx], field_name) is not None), None)
            for idx in affected_indices:
                record = updated_records[idx]
                value = None
                if smooth and left_index is not None and right_index is not None:
                    left_record = updated_records[left_index]
                    right_record = updated_records[right_index]
                    left_value = getattr(left_record, field_name)
                    right_value = getattr(right_record, field_name)
                    if left_value is not None and right_value is not None:
                        span = max(1, right_index - left_index)
                        ratio = (idx - left_index) / span
                        value = float(left_value) + (float(right_value) - float(left_value)) * ratio
                updated_records[idx] = MeasurementRecord(
                    timestamp=record.timestamp,
                    mass=value if field_name == "mass" else record.mass,
                    furnace_pv=value if field_name == "furnace_pv" else record.furnace_pv,
                    furnace_sv=value if field_name == "furnace_sv" else record.furnace_sv,
                    mass_timestamp=record.mass_timestamp,
                    furnace_pv_timestamp=record.furnace_pv_timestamp,
                    furnace_sv_timestamp=record.furnace_sv_timestamp,
                )

        self._load_records_into_ui(updated_records)
        return len(affected_indices)

    def _shift_optional_timestamp(self, raw_timestamp: str | None, delta: timedelta, border: datetime) -> str | None:
        if not raw_timestamp:
            return None
        try:
            timestamp = coerce_timestamp(raw_timestamp)
            if timestamp is None:
                raise ValueError
        except ValueError:
            return raw_timestamp
        if timestamp > border:
            return (timestamp - delta).isoformat()
        return raw_timestamp

    def tare_scale(self) -> None:
        if not self._scale_actions_allowed():
            self._set_status("Тара недоступна: нет связи с весами.", logging.WARNING)
            return
        self._run_scale_command_async("тары", self.controller.tare_scale)

    def zero_scale(self) -> None:
        if not self._scale_actions_allowed():
            self._set_status(
                "Команда нуля недоступна: нет связи с весами.", logging.WARNING
            )
            return
        self._run_scale_command_async("нуля", self.controller.zero_scale)

    def _run_scale_command_async(self, action_name: str, action) -> None:
        if self._scale_command_in_progress:
            self._set_status("Команда весам уже выполняется. Дождитесь завершения.", logging.WARNING)
            return
        self._scale_command_in_progress = True
        self._update_action_buttons()
        self._set_status(f"Выполняется команда {action_name}...", emit_log=False)

        def worker() -> None:
            try:
                result = bool(action())
            except Exception:
                self.logger.exception("Ошибка выполнения команды весам: %s", action_name)
                result = False
            self.after(0, lambda: self._finish_scale_command(action_name, result))

        threading.Thread(
            target=worker,
            name=f"scale-{action_name}-worker",
            daemon=True,
        ).start()

    def _finish_scale_command(self, action_name: str, result: bool) -> None:
        self._scale_command_in_progress = False
        self._update_action_buttons()
        self._set_status(
            f"Команда {action_name} отправлена." if result else f"Не удалось выполнить команду {action_name}.",
            logging.INFO if result else logging.WARNING,
        )

    def toggle_left_panel(self) -> None:
        self.open_settings_window()

    def toggle_right_panel(self) -> None:
        self.right_panel_visible = not self.right_panel_visible
        self.view_mode_var.set(
            "advanced"
            if self.left_panel_visible or self.right_panel_visible
            else self.view_mode_var.get()
        )
        self._update_side_panels()

    def _apply_view_mode(self) -> None:
        self.left_panel_visible = False
        if self.view_mode_var.get() == "basic":
            self.right_panel_visible = False
        self._update_side_panels()

    def toggle_theme(self) -> None:
        self.set_theme("light" if self.theme_manager.theme_name == "dark" else "dark")

    def set_theme(self, theme_name: str) -> None:
        self.theme_manager.set_theme(theme_name)
        self.config_data.app.theme = self.theme_manager.theme_name
        if "app.theme" in self.setting_vars:
            self.setting_vars["app.theme"].set(self.config_data.app.theme)
        save_config(self.config_data, self.config_path)
        self._apply_theme()

    def show_help_dialog(self) -> None:
        open_help_dialog(self)

    def show_tools_dialog(self) -> None:
        open_tools_dialog(self)

    def show_about_dialog(self) -> None:
        open_about_dialog(self)

    def _apply_theme(self) -> None:
        palette = self.theme_manager.palette
        style = self.theme_manager.apply_ttk_styles(self, scale=self.ui_scale)
        style.configure(
            "Section.TLabelframe",
            background=palette.card_bg,
            bordercolor=palette.border,
        )
        style.configure(
            "Section.TLabelframe.Label",
            background=palette.card_bg,
            foreground=palette.text,
        )
        self.configure(bg=palette.app_bg)
        self.header_status.configure(
            bg=palette.header_bg,
            fg=palette.subtext,
            font=("Segoe UI", max(11, int(12 * self.ui_scale))),
        )
        self._style_menu_button(self.file_menu_button, palette)
        self._style_menu_button(self.help_button, palette)
        self._style_menu_button(self.log_menu_button, palette)
        self.theme_switch_button.configure(
            text="Тема: светлая" if palette.name == "light" else "Тема: тёмная"
        )
        self.log_text.configure(
            background=palette.input_bg,
            foreground=palette.text,
            insertbackground=palette.text,
            font=("Consolas", max(10, int(11 * self.ui_scale))),
        )
        if hasattr(self, "table_columns_toggle_button"):
            self._style_edge_button(self.table_columns_toggle_button, palette)
        if hasattr(self, "plot_side_toggle_strip"):
            self._redraw_plot_side_toggle_strip()
        if (
            hasattr(self, "settings_mode_badge")
            and self.settings_mode_badge.winfo_exists()
        ):
            self._update_settings_control_states()
        if (
            hasattr(self, "font_scale_value_label")
            and self.font_scale_value_label.winfo_exists()
        ):
            self.font_scale_value_label.configure(
                text=f"{int(round(self.config_data.app.font_scale * 100))}%"
            )
        if (
            hasattr(self, "settings_font_scale_value_label")
            and self.settings_font_scale_value_label.winfo_exists()
        ):
            self.settings_font_scale_value_label.configure(
                text=f"{int(round(self.config_data.app.font_scale * 100))}%"
            )
        for card in (
            self.mass_card,
            self.temp_card,
            self.thermocouple_card,
            self.status_card,
            self.time_card,
        ):
            card.apply_theme(palette, self.ui_scale)
        self._style_indicator(self.scale_indicator, palette)
        self._style_indicator(self.furnace_indicator, palette)
        self.plotter.apply_theme(palette.plot, scale=self.ui_scale)
        for canvas in self._plot_panel_canvases:
            try:
                canvas.configure(background=palette.card_alt_bg)
            except Exception:
                pass
        for body in self._plot_panel_bodies:
            try:
                body.configure(style="CardAlt.TFrame")
            except Exception:
                pass
        if hasattr(self, "_settings_window") and self._settings_window.winfo_exists():
            self._apply_theme_to_toplevel(self._settings_window)
        self._update_action_buttons()

    def _apply_theme_to_toplevel(
        self, window: tk.Toplevel, *, text_widget: ScrolledText | None = None
    ) -> None:
        palette = self.theme_manager.palette
        window.configure(bg=palette.app_bg)
        for canvas in getattr(self, "_settings_tab_canvases", []):
            try:
                canvas.configure(bg=palette.app_bg)
            except Exception:
                continue
        for label in getattr(self, "_settings_hint_labels", []):
            try:
                wraplength = getattr(
                    label, "_datafusion_wraplength", int(360 * self.ui_scale)
                )
                label.configure(
                    bg=palette.card_alt_bg,
                    fg=palette.subtext,
                    font=("Segoe UI", max(11, int(12 * self.ui_scale))),
                    wraplength=wraplength,
                )
            except Exception:
                continue
        for label in getattr(self, "_settings_warning_labels", []):
            try:
                label.configure(
                    bg=palette.card_alt_bg,
                    fg=palette.warning,
                    font=("Segoe UI Semibold", max(11, int(12 * self.ui_scale))),
                    highlightbackground=palette.border,
                    highlightcolor=palette.border,
                )
            except Exception:
                continue
        if text_widget is not None:
            text_widget.configure(
                background=palette.input_bg,
                foreground=palette.text,
                insertbackground=palette.text,
                font=("Segoe UI", max(12, int(13 * self.ui_scale))),
            )

    def _make_settings_note_label(
        self, parent, text: str, *, wraplength: int
    ) -> tk.Label:
        palette = self.theme_manager.palette
        label = tk.Label(
            parent,
            text=text,
            bg=palette.card_alt_bg,
            fg=palette.subtext,
            justify="left",
            anchor="nw",
            wraplength=wraplength,
            padx=self._pad_x(8),
            pady=self._pad_y(6),
            font=("Segoe UI", max(11, int(12 * self.ui_scale))),
            borderwidth=0,
            highlightthickness=0,
        )
        label._datafusion_wraplength = wraplength  # type: ignore[attr-defined]
        self._settings_hint_labels.append(label)
        return label

    def _create_settings_scroll_tab(
        self, notebook: ttk.Notebook
    ) -> tuple[ttk.Frame, ttk.Frame]:
        tab = ttk.Frame(notebook, style="App.TFrame")
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(
            tab,
            highlightthickness=0,
            bd=0,
            background=self.theme_manager.palette.app_bg,
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        body = ttk.Frame(canvas, style="App.TFrame", padding=self._pad(8, 8))
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.grid_columnconfigure(0, weight=1)

        def refresh(_event=None) -> None:
            try:
                canvas.itemconfigure(window_id, width=max(1, canvas.winfo_width() - 2))
                bbox = canvas.bbox("all")
                if bbox:
                    canvas.configure(scrollregion=bbox)
                if body.winfo_reqheight() > canvas.winfo_height() + 4:
                    scrollbar.grid()
                else:
                    scrollbar.grid_remove()
            except tk.TclError:
                return

        body.bind("<Configure>", lambda e: self.after_idle(refresh), add="+")
        canvas.bind("<Configure>", lambda e: self.after_idle(refresh), add="+")
        self._bind_mousewheel_to_canvas(canvas)
        self._settings_tab_canvases.append(canvas)
        self.after_idle(refresh)
        return tab, body

    def _schedule_settings_layout_update(self, _event=None) -> None:
        if getattr(self, "_settings_layout_after_id", None):
            return
        try:
            self._settings_layout_after_id = self.after_idle(
                self._update_settings_canvas_layout
            )
        except Exception:
            self._settings_layout_after_id = None

    def _update_settings_canvas_layout(self) -> None:
        self._settings_layout_after_id = None
        canvas = getattr(self, "_settings_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        try:
            window_id = getattr(self, "_settings_canvas_window_id", None)
            if window_id is not None:
                canvas.itemconfigure(window_id, width=max(1, canvas.winfo_width() - 2))
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)
        except tk.TclError:
            return

    def _style_menu_button(self, button: tk.Menubutton, palette: ThemePalette) -> None:
        button.configure(
            bg=palette.button_soft_bg,
            fg=palette.text,
            activebackground=palette.button_soft_active,
            activeforeground=palette.text,
            padx=self._pad_x(12),
            pady=self._pad_y(8),
            font=("Segoe UI Semibold", max(11, int(12 * self.ui_scale))),
            highlightthickness=1,
            highlightbackground=palette.border,
        )

    def _style_edge_button(self, button: tk.Button, palette: ThemePalette) -> None:
        button.configure(
            bg=palette.button_soft_bg,
            fg=palette.text,
            activebackground=palette.button_soft_active,
            activeforeground=palette.text,
            disabledforeground=palette.disabled,
            highlightthickness=1,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
            font=("Segoe UI Semibold", max(10, int(11 * self.ui_scale))),
        )

    def _bind_mousewheel_to_canvas(self, canvas: tk.Canvas) -> None:
        def activate(_event=None) -> None:
            self._active_scroll_canvas = canvas

        def deactivate(_event=None) -> None:
            if getattr(self, "_active_scroll_canvas", None) is canvas:
                self._active_scroll_canvas = None

        if not getattr(self, "_global_scroll_binding_ready", False):
            self.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
            self.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
            self.bind_all("<Button-5>", self._on_global_mousewheel, add="+")
            self._global_scroll_binding_ready = True

        canvas.bind("<Enter>", activate, add="+")
        canvas.bind("<Leave>", deactivate, add="+")
        canvas.bind("<Destroy>", deactivate, add="+")

    def _on_global_mousewheel(self, event) -> None:
        canvas = getattr(self, "_active_scroll_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return

        delta = 0
        if getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        elif getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        if not delta:
            return

        try:
            top, bottom = canvas.yview()
            span = max(0.0, bottom - top)
            max_top = max(0.0, 1.0 - span)
            step = 0.022
            new_top = min(max_top, max(0.0, top + (step * delta)))
            canvas.yview_moveto(new_top)
        except tk.TclError:
            return

    def _style_indicator(
        self, indicator: dict[str, object], palette: ThemePalette
    ) -> None:
        frame = indicator["frame"]
        canvas = indicator["canvas"]
        label = indicator["label"]
        if isinstance(frame, ttk.Frame):
            frame.configure(style="Header.TFrame")
        canvas.configure(bg=palette.header_bg)
        label.configure(
            bg=palette.header_bg,
            fg=palette.text,
            font=("Segoe UI Semibold", max(10, int(11 * self.ui_scale))),
        )

    def _animate_indicators(self) -> None:
        palette = self.theme_manager.palette
        self._indicator_phase = (getattr(self, "_indicator_phase", 0) + 1) % 24
        intensity = 0.45 + abs(12 - self._indicator_phase) / 18.0
        now = time.monotonic()

        def update(
            indicator: dict[str, object],
            *,
            connected: bool,
            enabled: bool,
            base_color: str,
        ) -> None:
            canvas = indicator["canvas"]
            outer = indicator["outer"]
            inner = indicator["inner"]
            if connected:
                outer_fill = _blend_color(
                    base_color, palette.card_bg, 0.45 + (intensity * 0.15)
                )
                inner_fill = base_color
            elif enabled:
                outer_fill = _blend_color(
                    palette.error, palette.card_bg, 0.42 + (intensity * 0.18)
                )
                inner_fill = palette.error
            else:
                outer_fill = _blend_color(palette.border, palette.header_bg, 0.4)
                inner_fill = palette.disabled
            canvas.itemconfig(outer, fill=outer_fill)
            canvas.itemconfig(inner, fill=inner_fill)

        update(
            self.scale_indicator,
            connected=self._scale_test_mode_active()
            or (now - self._last_scale_seen_at) <= self._device_indicator_hold_s,
            enabled=self._scale_indicator_expected(),
            base_color=palette.accent,
        )
        update(
            self.furnace_indicator,
            connected=self._furnace_test_mode_active()
            or (now - self._last_furnace_seen_at) <= self._device_indicator_hold_s,
            enabled=self._furnace_indicator_expected(),
            base_color=palette.success,
        )
        self.after(140, self._animate_indicators)

    def _update_side_panels(self) -> None:
        self.body.grid_columnconfigure(0, weight=0, minsize=0)
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_columnconfigure(2, weight=0, minsize=0)

        if self.left_panel_visible:
            self.left_panel.grid(
                row=0, column=0, sticky="nsew", padx=(0, self._pad_x(12))
            )
            self.body.grid_columnconfigure(0, minsize=int(420 * self.ui_scale))
        else:
            self.left_panel.grid_remove()

        self.center_panel.grid(row=0, column=1, sticky="nsew")

        if self.right_panel_visible:
            self.right_panel.grid(
                row=0, column=2, sticky="nsew", padx=(self._pad_x(12), 0)
            )
            self.body.grid_columnconfigure(2, minsize=int(390 * self.ui_scale))
        else:
            self.right_panel.grid_remove()
        self._set_log_button_text()

    def _set_log_button_text(self) -> None:
        if hasattr(self, "log_menu_button"):
            self.log_menu_button.configure(
                text="Лог ▾" if self.right_panel_visible else "Лог"
            )

    def _close_settings_window(self) -> None:
        window = getattr(self, "_settings_window", None)
        if window is None or not window.winfo_exists():
            return

        if not bool(self.autosave_settings_var.get()):
            self._restore_settings_from_disk()

        try:
            window.destroy()
        except Exception:
            return

    def _toggle_settings_window_maximize(self) -> None:
        window = getattr(self, "_settings_window", None)
        if window is None or not window.winfo_exists():
            return

        zoomed = bool(getattr(window, "_datafusion_zoomed", False))
        if zoomed:
            area = _windows_work_area()
            if area is not None:
                x, y, width, height = area
                width = max(int(width * 0.9), 920)
                height = max(int(height * 0.88), 680)
                pos_x = x + max(8, (area[2] - width) // 2)
                pos_y = y + max(8, (area[3] - height) // 2)
                window.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
            else:
                window.state("normal")
            window._datafusion_zoomed = False  # type: ignore[attr-defined]
        else:
            area = _windows_work_area()
            if area is not None:
                x, y, width, height = area
                window.geometry(
                    f"{max(640, width - 16)}x{max(480, height - 16)}+{x + 8}+{y + 8}"
                )
            else:
                try:
                    window.state("zoomed")
                except Exception:
                    window.geometry(
                        f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0"
                    )
            window._datafusion_zoomed = True  # type: ignore[attr-defined]

        self._update_settings_zoom_button()

    def _update_settings_zoom_button(self) -> None:
        button = getattr(self, "settings_zoom_button", None)
        window = getattr(self, "_settings_window", None)
        if button is None or window is None or not button.winfo_exists():
            return
        zoomed = bool(getattr(window, "_datafusion_zoomed", False))
        button.configure(text="❐" if zoomed else "▢")

    def _poll_runtime_queues(self) -> None:
        for snapshot in self.controller.drain_snapshots():
            self._apply_snapshot(snapshot)

        while True:
            try:
                message = self.log_handler.messages.get_nowait()
            except queue.Empty:
                break
            else:
                self._append_log_line(message)

        self.after(120, self._poll_runtime_queues)

    def _apply_snapshot(self, snapshot: AcquisitionSnapshot) -> None:
        prev_scale_connected = self.last_scale_connected
        prev_furnace_connected = self.last_furnace_connected
        self.last_scale_connected = snapshot.scale_connected
        self.last_furnace_connected = snapshot.furnace_connected
        if snapshot.scale_connected:
            self._last_scale_seen_at = time.monotonic()
        if snapshot.furnace_connected:
            self._last_furnace_seen_at = time.monotonic()
        self._capture_measurement_baseline(snapshot.record)
        self.plotter.update(snapshot.record)
        self.mass_card.set_value(
            _format_value(snapshot.record.mass, 3),
            unit="g",
            subtitle="Текущее значение",
        )
        self.mass_card.set_secondary(
            f"Начальная: {_format_value(self._baseline_mass, 3)} г"
            if self._baseline_mass is not None
            else "Начальная: идёт фиксация"
        )
        self.temp_card.set_value(
            _format_value(snapshot.record.furnace_pv, 1), unit="°C", subtitle="Камера"
        )
        self.thermocouple_card.set_value(
            _format_value(snapshot.record.furnace_sv, 1),
            unit="°C",
            subtitle="Термопара",
        )
        status_main, status_sub = self._status_text(snapshot)
        self.status_card.set_value(status_main, subtitle=status_sub)
        self.time_card.set_value(
            self._format_elapsed_time(snapshot.record.timestamp),
            subtitle=self._format_card_timestamp(snapshot.record.timestamp),
        )

        if snapshot.scale_connected:
            self.mass_card.pulse(self.theme_manager.palette.success)
        if snapshot.furnace_connected:
            self.temp_card.pulse(self.theme_manager.palette.success)
            self.thermocouple_card.pulse(self.theme_manager.palette.warning)

        self._append_measurement_row(snapshot.record)
        self.measurement_records.append(snapshot.record)

        self.diag_last_sample_var.set(
            f"Последний сэмпл: масса={_format_value(snapshot.record.mass, 3)} g, PV={_format_value(snapshot.record.furnace_pv, 1)} °C, SV={_format_value(snapshot.record.furnace_sv, 1)} °C"
        )
        self.diag_last_time_var.set(
            f"Время: {snapshot.record.timestamp.replace('T', ' ')}"
        )
        self.diag_status_var.set(status_sub)
        self._log_connection_transitions(
            prev_scale_connected=prev_scale_connected,
            prev_furnace_connected=prev_furnace_connected,
            snapshot=snapshot,
        )
        self._set_status("Измерение выполняется.", emit_log=False)
        self._update_action_buttons()

    def _append_measurement_row(self, record, *, autoscroll: bool = True) -> None:
        if not hasattr(self, "measurements_table"):
            return
        item_id = self.measurements_table.insert(
            "",
            "end",
            values=(
                len(self.measurement_records) + 1,
                self._format_table_timestamp(record.timestamp),
                _format_value(record.mass, 3),
                _format_value(record.furnace_pv, 1),
                _format_value(record.furnace_sv, 1),
            ),
        )
        self._table_timestamp_map[item_id] = record.timestamp
        if autoscroll:
            children = self.measurements_table.get_children()
            if children:
                self.measurements_table.see(children[-1])

    def _clear_loaded_measurements(self) -> None:
        self.measurement_records.clear()
        self.plotter.clear()
        self._table_timestamp_map.clear()
        self._baseline_mass = None
        self._baseline_mass_timestamp = None
        self._baseline_mass_samples.clear()
        self._session_started_at = None
        if hasattr(self, "measurements_table"):
            for item_id in self.measurements_table.get_children():
                self.measurements_table.delete(item_id)

    def _load_records_into_ui(self, records: list[MeasurementRecord]) -> None:
        self._clear_loaded_measurements()
        if not records:
            return
        records = sorted(
            records,
            key=lambda record: coerce_timestamp(record.timestamp) or datetime.max,
        )
        self.measurements_table.configure(height=min(24, max(8, len(records))))
        for record in records:
            self.measurement_records.append(record)
            self._append_measurement_row(record, autoscroll=False)
        self.plotter.extend(records)
        last_children = self.measurements_table.get_children()
        if last_children:
            self.measurements_table.see(last_children[-1])
        last_record = records[-1]
        self.sync_measurement_baseline_from_record(last_record, emit_status=False)

    def sync_measurement_baseline_from_record(self, record, *, emit_status: bool = True) -> None:
        if record.mass is None:
            return
        self._apply_measurement_baseline(float(record.mass), record.timestamp, emit_status=emit_status)

    def _build_summary_tab(self, parent) -> None:
        summary_card = ttk.Frame(parent, style="Card.TFrame", padding=self._pad(12, 12))
        summary_card.grid(row=0, column=0, sticky="nsew")
        summary_card.grid_rowconfigure(1, weight=1)
        summary_card.grid_columnconfigure(0, weight=1)
        ttk.Label(summary_card, text="Отчёт измерений", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")

        notebook = ttk.Notebook(summary_card)
        notebook.grid(row=1, column=0, sticky="nsew", pady=(self._pad_y(10), 0))
        self.summary_notebook = notebook
        self.summary_analysis_tab = self._build_scrollable_summary_page(notebook)
        self.summary_furnace_tab = self._build_scrollable_summary_page(notebook)
        self.summary_mass_tab = self._build_scrollable_summary_page(notebook)
        notebook.add(self.summary_analysis_tab["container"], text="Расчёты")
        notebook.add(self.summary_furnace_tab["container"], text="Данные печи")
        notebook.add(self.summary_mass_tab["container"], text="Данные весов")

        buttons = ttk.Frame(summary_card, style="Card.TFrame")
        buttons.grid(row=2, column=0, sticky="ew", pady=(self._pad_y(10), 0))
        for idx in range(2):
            buttons.grid_columnconfigure(idx, weight=1)
        self.summary_refresh_button = ttk.Button(
            buttons,
            text="Обновить данные",
            style="Soft.TButton",
            command=self._refresh_summary_tab,
        )
        self.summary_refresh_button.grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        self.summary_save_button = ttk.Button(
            buttons,
            text="Сохранить TXT",
            style="Soft.TButton",
            command=self._save_current_summary_tab_to_txt,
        )
        self.summary_save_button.grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))
        self._refresh_summary_tab()

    def _build_scrollable_summary_page(self, parent):
        container = ttk.Frame(parent, style="Card.TFrame")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(
            container,
            highlightthickness=0,
            bd=0,
            background=self.theme_manager.palette.card_bg,
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        body = ttk.Frame(canvas, style="Card.TFrame")
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def update_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            needs_scroll = body.winfo_reqheight() > canvas.winfo_height()
            if needs_scroll:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()

        body.bind("<Configure>", update_scrollregion)
        canvas.bind(
            "<Configure>",
            lambda event, c=canvas, w=window_id: (
                c.itemconfigure(w, width=event.width),
                update_scrollregion(),
            ),
        )
        return {
            "container": container,
            "canvas": canvas,
            "body": body,
            "scrollbar": scrollbar,
        }

    def _append_log_line(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 320:
            self.log_text.delete("1.0", "70.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _get_selected_tree_device(self) -> str | None:
        selected = self.port_tree.selection()
        if not selected:
            return None
        return str(selected[0])

    def _on_port_selected(self, _event=None) -> None:
        device = self._get_selected_tree_device()
        if not device:
            self.assignment_var.set("Выберите COM-порт слева.")
            self._update_action_buttons()
            return
        port = self.port_map.get(device.upper())
        if port is None:
            self.assignment_var.set(device)
        else:
            self.assignment_var.set(
                f"{port_display_label(port)} | {guess_port_kind(port)} | {port.hwid}"
            )
        self._update_action_buttons()

    def _refresh_assignment_summary(self, device: str, label: str) -> None:
        port = self.port_map.get(device.upper())
        detail = port_display_label(port) if port else device
        self.assignment_var.set(f"{label}: {detail}")

    def _refresh_diagnostics(self) -> None:
        scale_text = self.scale_port_var.get().strip() or "не назначены"
        furnace_text = self.furnace_port_var.get().strip() or "не назначена"
        self.diag_ports_var.set(f"Весы: {scale_text} | Печь: {furnace_text}")

    def _sync_port_display_vars(self) -> None:
        self.scale_port_display_var.set(
            self._display_for_port(self.scale_port_var.get().strip())
        )
        self.furnace_port_display_var.set(
            self._display_for_port(self.furnace_port_var.get().strip())
        )

    def _display_for_port(self, port_name: str) -> str:
        if not port_name:
            return ""
        return self.port_display_map.get(port_name.upper(), port_name)

    def _build_setting_tooltip(self, key: str, short_text: str) -> str:
        extra = TOOLTIP_DETAILS.get(key, "")
        default_text = f"По умолчанию: {self._display_setting_default(key)}"
        if extra:
            return f"{short_text}\n\nПодсказка:\n{extra}\n\n{default_text}"
        return f"{short_text}\n\n{default_text}"

    def _update_test_mode_banner(self) -> None:
        banner = getattr(self, "test_mode_banner", None)
        if banner is None or not banner.winfo_exists():
            return
        enabled_var = self.setting_vars.get("app.test_mode")
        scope_var = self.setting_vars.get("app.test_mode_scope")
        enabled = (
            bool(enabled_var.get())
            if enabled_var is not None
            else bool(self.config_data.app.test_mode)
        )
        scope_value = (
            str(scope_var.get()).strip()
            if scope_var is not None
            else self._test_mode_scope_to_label(self.config_data.app.test_mode_scope)
        )
        scope_label = (
            scope_value
            if scope_value in TEST_MODE_SCOPE_VALUES
            else self._test_mode_scope_to_label(scope_value)
        )
        if enabled:
            banner.configure(
                text=f"Тестовый режим включён. Сейчас эмулируется: {scope_label.lower()}."
            )
        else:
            banner.configure(
                text="Тестовый режим выключен. Программа ожидает реальные весы и/или печь."
            )

    def _restore_settings_from_disk(self) -> None:
        self._suspend_settings_autosave = True
        try:
            self.config_data = load_config(self.config_path)
            self.scale_port_var.set(self.config_data.scale.port)
            self.furnace_port_var.set(self.config_data.furnace.port)
            self._load_settings_into_vars()
            self._sync_port_display_vars()
            self.theme_manager.set_theme(self.config_data.app.theme)
            self.plotter.set_max_points(self.config_data.app.max_points_on_plot)
            self._apply_theme()
        finally:
            self._suspend_settings_autosave = False

    def _on_setting_var_changed(self, *_args) -> None:
        if self._suspend_settings_autosave:
            return
        if self.controller.running:
            self._suspend_settings_autosave = True
            try:
                self._load_settings_into_vars()
            finally:
                self._suspend_settings_autosave = False
            if not self._settings_lock_notified:
                self._settings_lock_notified = True
                message = "Во время записи настройки менять нельзя."
                self._set_status(message, logging.WARNING)
                try:
                    messagebox.showwarning("Настройки заблокированы", message, parent=getattr(self, "_settings_window", self))
                except Exception:
                    pass
                self.after(1200, lambda: setattr(self, "_settings_lock_notified", False))
            return
        if (
            "app.start_maximized" in self.setting_vars
            and "app.fullscreen" in self.setting_vars
        ):
            start_maximized = bool(self.setting_vars["app.start_maximized"].get())
            fullscreen_var = self.setting_vars["app.fullscreen"]
            if not start_maximized and bool(fullscreen_var.get()):
                self._suspend_settings_autosave = True
                try:
                    fullscreen_var.set(False)
                finally:
                    self._suspend_settings_autosave = False
        self._update_furnace_access_warning()
        self._update_test_mode_banner()
        if bool(self.autosave_settings_var.get()):
            self._autosave_settings_silent()

    def _on_autosave_toggle(self, *_args) -> None:
        if self._suspend_settings_autosave:
            return
        enabled = bool(self.autosave_settings_var.get())
        if enabled:
            # When autosave is enabled after manual edits, persist the current UI state immediately.
            self._autosave_settings_silent()
            self.config_data.app.autosave_settings = True
            save_config(self.config_data, self.config_path)
            self._update_settings_control_states()
            self._set_status("Автосохранение настроек включено.", emit_log=False)
        else:
            self.config_data.app.autosave_settings = False
            save_config(self.config_data, self.config_path)
            self._update_settings_control_states()
            self._set_status("Автосохранение настроек выключено.", emit_log=False)

    def _autosave_settings_silent(self) -> None:
        before = dataclasses.asdict(self.config_data)
        if not self._commit_settings_to_config(show_errors=False):
            return
        save_config(self.config_data, self.config_path)
        reconfigure_file_logging(
            resolve_path(self.config_data.app.log_path),
            enable_file_logging=self.config_data.app.enable_file_logging,
        )
        self._sync_port_display_vars()
        self.plotter.set_max_points(self.config_data.app.max_points_on_plot)
        self.theme_manager.set_theme(self.config_data.app.theme)
        self.ui_scale = self._compute_ui_scale()
        self._apply_tk_scaling()
        self._apply_theme()
        self.diag_status_var.set("Настройки автоматически сохранены.")
        self._log_settings_changes(before, dataclasses.asdict(self.config_data))
        self._update_settings_control_states()
        self._pulse_autosave_badge()

    def _update_settings_control_states(self) -> None:
        autosave_enabled = bool(self.autosave_settings_var.get())
        settings_locked = self.controller.running
        if (
            hasattr(self, "settings_save_button")
            and self.settings_save_button.winfo_exists()
        ):
            self.settings_save_button.state(
                ["disabled"] if (autosave_enabled or settings_locked) else ["!disabled"]
            )
        if hasattr(self, "settings_reset_button") and self.settings_reset_button.winfo_exists():
            self.settings_reset_button.state(["disabled"] if settings_locked else ["!disabled"])
        if hasattr(self, "settings_scale_combo") and self.settings_scale_combo.winfo_exists():
            combo_state = "disabled" if settings_locked else "normal"
            try:
                self.settings_scale_combo.configure(state=combo_state)
                self.settings_furnace_combo.configure(state=combo_state)
            except Exception:
                pass
        for widget in getattr(self, "_settings_input_widgets", []):
            if not hasattr(widget, "winfo_exists") or not widget.winfo_exists():
                continue
            try:
                if settings_locked:
                    widget.state(["disabled"])
                else:
                    if isinstance(widget, ttk.Combobox):
                        widget.state(["!disabled", "readonly"])
                    else:
                        widget.state(["!disabled"])
            except Exception:
                try:
                    widget.configure(state="disabled" if settings_locked else "normal")
                except Exception:
                    pass
        if (
            hasattr(self, "settings_mode_badge")
            and self.settings_mode_badge.winfo_exists()
        ):
            palette = self.theme_manager.palette
            if autosave_enabled:
                self.settings_mode_var.set("💾 Автосохранение включено")
                self.settings_mode_hint_var.set(
                    "Корректные изменения применяются и записываются в config.yaml сразу после редактирования."
                )
                badge_bg = palette.accent
                badge_fg = "#081016" if palette.name == "dark" else "#FFFFFF"
                badge_border = _blend_color(palette.accent, palette.border, 0.6)
            else:
                self.settings_mode_var.set("✍ Ручное сохранение")
                self.settings_mode_hint_var.set(
                    "Изменения записываются только после нажатия кнопки «Сохранить»."
                )
                badge_bg = palette.warning
                badge_fg = "#1F1308" if palette.name == "dark" else "#FFFFFF"
                badge_border = _blend_color(palette.warning, palette.border, 0.6)
            self.settings_mode_badge.configure(
                bg=badge_bg,
                fg=badge_fg,
                font=("Segoe UI Semibold", max(10, int(11 * self.ui_scale))),
                highlightbackground=badge_border,
                highlightcolor=badge_border,
                highlightthickness=1,
                borderwidth=1,
            )
        self._update_furnace_access_warning()
        self._update_test_mode_banner()

    def _update_furnace_access_warning(self) -> None:
        if not hasattr(self, "furnace_access_warning_var") or not hasattr(
            self, "furnace_access_warning_label"
        ):
            return
        label = self.furnace_access_warning_label
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        access_var = self.setting_vars.get("furnace.access_mode")
        enabled_var = self.setting_vars.get("furnace.enabled")
        if access_var is None or enabled_var is None:
            try:
                label.grid_remove()
            except tk.TclError:
                pass
            return
        enabled = self._bool_from_var(enabled_var)
        access_mode = str(access_var.get()).strip().lower()
        text = (
            "Режим печи только для чтения. Отправка команд в контроллер из программы отключена, "
            "чтобы не конфликтовать с MCGS и не рисковать управлением печью."
        )
        self.furnace_access_warning_var.set(text)
        should_show = enabled and access_mode == "read_only"
        try:
            if should_show:
                if not label.winfo_ismapped():
                    label.grid()
            else:
                label.grid_remove()
        except tk.TclError:
            return

    def _pulse_autosave_badge(self) -> None:
        if (
            not hasattr(self, "settings_mode_badge")
            or not self.settings_mode_badge.winfo_exists()
        ):
            return
        if not bool(self.autosave_settings_var.get()):
            return
        base_bg = str(self.settings_mode_badge.cget("bg"))
        accent = "#8AF3E5" if self.theme_manager.palette.name == "dark" else "#11A799"
        self.settings_mode_badge.configure(bg=accent)
        self.after(180, lambda: self._restore_badge_color(base_bg))

    def _restore_badge_color(self, color: str) -> None:
        if (
            hasattr(self, "settings_mode_badge")
            and self.settings_mode_badge.winfo_exists()
        ):
            self.settings_mode_badge.configure(bg=color)

    def _settings_port_label(self, port: PortInfo) -> str:
        return f"{port.device} - {port.description} - {guess_port_kind(port)}"

    def _bool_from_var(self, variable) -> bool:
        try:
            value = variable.get()
        except Exception:
            value = variable
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _extract_port_name(self, raw_value: str) -> str:
        if not raw_value:
            return ""
        if " - " in raw_value:
            return raw_value.split(" - ", 1)[0].strip()
        return raw_value.strip()

    def _test_mode_scope_to_label(self, value: str) -> str:
        return TEST_MODE_SCOPE_LABELS.get(
            str(value).lower(), TEST_MODE_SCOPE_LABELS["all"]
        )

    def _test_mode_scope_from_label(self, value: str) -> str:
        raw = str(value).strip()
        for key, label in TEST_MODE_SCOPE_LABELS.items():
            if raw == label:
                return key
        lowered = raw.lower()
        return lowered if lowered in TEST_MODE_SCOPE_LABELS else "all"

    def _display_setting_default(self, key: str) -> object:
        value = DEFAULTS[key]
        if key == "app.test_mode_scope":
            return self._test_mode_scope_to_label(str(value))
        return value

    def _commit_settings_to_config(self, *, show_errors: bool) -> bool:
        try:
            self.scale_port_var.set(
                self._extract_port_name(self.scale_port_display_var.get().strip())
            )
            self.furnace_port_var.set(
                self._extract_port_name(self.furnace_port_display_var.get().strip())
            )
            self.config_data.scale.port = self.scale_port_var.get().strip()
            self.config_data.furnace.port = self.furnace_port_var.get().strip()
            self.config_data.scale.enabled = self._get_bool("scale.enabled")
            self.config_data.scale.baudrate = self._get_int("scale.baudrate")
            self.config_data.scale.timeout = self._get_float("scale.timeout")
            self.config_data.scale.mode = self._get_str("scale.mode").lower()
            self.config_data.scale.request_command = self._get_str(
                "scale.request_command"
            )
            self.config_data.scale.p1_polling_enabled = self._get_bool(
                "scale.p1_polling_enabled"
            )
            self.config_data.scale.p1_poll_interval_sec = self._get_float(
                "scale.p1_poll_interval_sec"
            )
            self.config_data.furnace.enabled = self._get_bool("furnace.enabled")
            self.config_data.furnace.baudrate = self._get_int("furnace.baudrate")
            self.config_data.furnace.bytesize = self._get_int("furnace.bytesize")
            self.config_data.furnace.parity = (
                self._get_str("furnace.parity").upper()[:1] or "N"
            )
            self.config_data.furnace.stopbits = self._get_float("furnace.stopbits")
            self.config_data.furnace.timeout = self._get_float("furnace.timeout")
            self.config_data.furnace.slave_id = self._get_int("furnace.slave_id")
            self.config_data.furnace.register_pv = self._get_int("furnace.register_pv")
            self.config_data.furnace.register_sv = self._get_int("furnace.register_sv")
            self.config_data.furnace.scale_factor = self._get_float(
                "furnace.scale_factor"
            )
            self.config_data.furnace.register_mode_pv = self._get_int("furnace.register_mode_pv")
            self.config_data.furnace.register_mode_sv = self._get_int("furnace.register_mode_sv")
            self.config_data.furnace.driver = self._get_str("furnace.driver").lower()
            self.config_data.furnace.access_mode = self._get_str("furnace.access_mode").lower()
            self.config_data.furnace.window_enabled = False
            self.config_data.furnace.window_period_ms = 1000
            self.config_data.furnace.window_open_ms = 120
            self.config_data.furnace.window_offset_ms = 0
            self.config_data.furnace.experimental_write_enabled = False
            if self.config_data.furnace.driver == "dk518":
                self.config_data.furnace.baudrate = 9600
                self.config_data.furnace.bytesize = 7
                self.config_data.furnace.parity = "E"
                self.config_data.furnace.stopbits = 1.0
                self.config_data.furnace.slave_id = 1
                self.config_data.furnace.register_pv = 90
                self.config_data.furnace.register_sv = 91
                self.config_data.furnace.register_mode_pv = 4
                self.config_data.furnace.register_mode_sv = 4
                self.config_data.furnace.scale_factor = 0.1
                self.config_data.furnace.access_mode = "read_only"
                self.config_data.furnace.read_groups = [
                    {
                        "name": "input_temperature_block",
                        "function": 4,
                        "address": 90,
                        "count": 2,
                        "scale": 0.1,
                        "pv_index": 0,
                        "sv_index": 1,
                    },
                    {
                        "name": "hold_0x0015",
                        "function": 3,
                        "address": 21,
                        "count": 3,
                        "scale": 1.0,
                    },
                    {
                        "name": "hold_0x0056",
                        "function": 3,
                        "address": 86,
                        "count": 3,
                        "scale": 1.0,
                    },
                    {
                        "name": "hold_0x0006",
                        "function": 3,
                        "address": 6,
                        "count": 3,
                        "scale": 1.0,
                    },
                ]
            else:
                self.config_data.furnace.read_groups = []
                if self.config_data.furnace.access_mode not in {"read_only", "active_modbus"}:
                    self.config_data.furnace.access_mode = "active_modbus"
            self.config_data.furnace.input_type_code = self._get_int(
                "furnace.input_type_code"
            )
            self.config_data.furnace.input_type_name = self._get_str(
                "furnace.input_type_name"
            )
            self.config_data.furnace.high_limit = self._get_float("furnace.high_limit")
            self.config_data.furnace.high_alarm = self._get_float("furnace.high_alarm")
            self.config_data.furnace.low_alarm = self._get_float("furnace.low_alarm")
            self.config_data.furnace.pid_p = self._get_float("furnace.pid_p")
            self.config_data.furnace.pid_t = self._get_float("furnace.pid_t")
            self.config_data.furnace.ctrl_mode = self._get_int("furnace.ctrl_mode")
            self.config_data.furnace.output_high_limit = self._get_float(
                "furnace.output_high_limit"
            )
            self.config_data.furnace.display_decimals = self._get_int(
                "furnace.display_decimals"
            )
            self.config_data.furnace.sensor_correction = self._get_float(
                "furnace.sensor_correction"
            )
            self.config_data.furnace.opt_code = self._get_int("furnace.opt_code")
            self.config_data.furnace.run_code = self._get_int("furnace.run_code")
            self.config_data.furnace.alarm_output_code = self._get_int(
                "furnace.alarm_output_code"
            )
            self.config_data.furnace.m5_value = self._get_float("furnace.m5_value")
            self.config_data.app.poll_interval_sec = self._get_float(
                "app.poll_interval_sec"
            )
            self.config_data.app.max_points_on_plot = self._get_int(
                "app.max_points_on_plot"
            )
            self.config_data.app.auto_detect_ports = bool(
                self.auto_detect_ports_var.get()
            )
            self.config_data.app.test_mode = self._get_bool("app.test_mode")
            self.config_data.app.test_mode_scope = self._test_mode_scope_from_label(
                self._get_str("app.test_mode_scope")
            )
            self.config_data.app.autosave_settings = bool(
                self.autosave_settings_var.get()
            )
            self.config_data.app.start_maximized = self._get_bool("app.start_maximized")
            self.config_data.app.fullscreen = (
                self._get_bool("app.fullscreen")
                if self.config_data.app.start_maximized
                else False
            )
            self.config_data.app.theme = self._get_str("app.theme").lower()
            self.config_data.app.csv_path = self._get_str("app.csv_path")
            self.config_data.app.log_path = self._get_str("app.log_path")
            self.config_data.app.enable_file_logging = self._get_bool(
                "app.enable_file_logging"
            )
            return True
        except ValueError as exc:
            self._set_status(str(exc), logging.WARNING)
            self.diag_status_var.set(str(exc))
            if show_errors:
                messagebox.showwarning("Настройки", str(exc), parent=self)
            return False

    def save_settings(self) -> None:
        if bool(self.autosave_settings_var.get()):
            self._update_settings_control_states()
            return
        before = dataclasses.asdict(self.config_data)
        if not self._commit_settings_to_config(show_errors=True):
            return
        save_config(self.config_data, self.config_path)
        reconfigure_file_logging(
            resolve_path(self.config_data.app.log_path),
            enable_file_logging=self.config_data.app.enable_file_logging,
        )
        self._sync_port_display_vars()
        self.plotter.set_max_points(self.config_data.app.max_points_on_plot)
        self.theme_manager.set_theme(self.config_data.app.theme)
        self.ui_scale = self._compute_ui_scale()
        self._apply_tk_scaling()
        self._apply_theme()
        self._set_status("Настройки сохранены.")
        self.diag_status_var.set("Настройки сохранены.")
        self._log_settings_changes(before, dataclasses.asdict(self.config_data))
        self._update_settings_control_states()

    def reset_default_settings(self) -> None:
        self._suspend_settings_autosave = True
        try:
            for key, value in DEFAULTS.items():
                if key not in self.setting_vars:
                    continue
                var = self.setting_vars[key]
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                else:
                    display_value = (
                        self._display_setting_default(key)
                        if key == "app.test_mode_scope"
                        else value
                    )
                    var.set(str(display_value))
            self.autosave_settings_var.set(bool(DEFAULTS["app.autosave_settings"]))
        finally:
            self._suspend_settings_autosave = False
        if bool(self.autosave_settings_var.get()):
            self._autosave_settings_silent()
        self._set_status("Значения по умолчанию восстановлены.")
        self._update_settings_control_states()

    def _make_plot_tool_button(
        self, parent, text: str, command, *, width: int | None = None
    ) -> ttk.Button:
        button = ttk.Button(
            parent, text=text, command=command, style="Soft.TButton", width=width
        )
        return button

    def _create_plot_button_panel(
        self,
        parent,
        *,
        title: str,
        column: int,
        width: int,
        padx: tuple[int, int],
        panel_key: str,
    ) -> tuple[ttk.Frame, ttk.Frame]:
        panel = ttk.Frame(
            parent, style="CardAlt.TFrame", padding=self._pad(8, 8), width=width
        )
        panel.grid(
            row=1, column=column, sticky="ns", pady=(self._pad_y(10), 0), padx=padx
        )
        panel.grid_propagate(False)
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        title_label = ttk.Label(
            panel, text=title, style="CardTitle.TLabel", anchor="center", justify="center"
        )
        title_label.grid(row=0, column=0, sticky="ew", pady=(0, self._pad_y(6)))

        canvas = tk.Canvas(
            panel,
            highlightthickness=0,
            bd=0,
            width=width,
            background=self.theme_manager.palette.card_alt_bg,
        )
        canvas.grid(row=1, column=0, sticky="ns")
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        body = ttk.Frame(canvas, style="CardAlt.TFrame")
        self._plot_panel_canvases.append(canvas)
        self._plot_panel_bodies.append(body)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def update_scrollbar(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            needs_scroll = body.winfo_reqheight() > canvas.winfo_height()
            if needs_scroll:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()

        body.bind("<Configure>", update_scrollbar)
        canvas.bind(
            "<Configure>",
            lambda e, c=canvas, w=window_id: (
                c.itemconfigure(w, width=e.width),
                update_scrollbar(),
            ),
        )
        self.plot_side_panels[panel_key] = {
            "panel": panel,
            "title_label": title_label,
            "canvas": canvas,
            "scrollbar": scrollbar,
            "width": width,
        }
        return panel, body

    def _set_plot_button_selected(self, button: ttk.Button, selected: bool) -> None:
        button.configure(style="SelectedSoft.TButton" if selected else "Soft.TButton")

    def _update_plot_mode_buttons(self) -> None:
        if not hasattr(self, "plot_mode_buttons"):
            return
        current = self.plotter.view_mode
        for mode_key, button in self.plot_mode_buttons.items():
            self._set_plot_button_selected(button, mode_key == current)

    def _update_plot_range_buttons(self) -> None:
        if not hasattr(self, "plot_range_toggle_button"):
            return
        current = self.plotter.plot_range_mode
        self.plot_range_toggle_button.configure(
            text="Окно: текущее"
            if current == LivePlotter.RANGE_SEGMENT
            else "Окно: всё"
        )
        self._set_plot_button_selected(
            self.plot_range_toggle_button, current == LivePlotter.RANGE_FULL
        )

    def _update_plot_render_buttons(self) -> None:
        if not hasattr(self, "plot_render_toggle_button"):
            return
        self.plot_render_toggle_button.configure(
            text="Кривая: точки"
            if self.plotter.points_enabled
            else "Кривая: линии"
        )
        self._set_plot_button_selected(
            self.plot_render_toggle_button, self.plotter.points_enabled
        )
        self._set_plot_button_selected(
            self.plot_smooth_button, self.plotter.smooth_enabled
        )

    def _update_plot_time_axis_button(self) -> None:
        if not hasattr(self, "plot_time_axis_button"):
            return
        relative = self.plotter.relative_time_axis_enabled
        self.plot_time_axis_button.configure(
            text="Время: от начала" if relative else "Время: часы"
        )
        self._set_plot_button_selected(self.plot_time_axis_button, relative)

    def _update_calc_buttons(self) -> None:
        if not hasattr(self, "calc_dtg_button"):
            return
        self._set_plot_button_selected(
            self.calc_dtg_button, self.plotter.view_mode == LivePlotter.VIEW_DTG
        )
        self._set_plot_button_selected(
            self.calc_normalize_button, self.plotter.normalization_enabled
        )
        self._set_plot_button_selected(
            self.calc_markers_button, self.plotter.markers_enabled
        )
        if hasattr(self, "calc_heating_profile_button"):
            self._set_plot_button_selected(
                self.calc_heating_profile_button, self.plotter.heating_profile_enabled
            )
        if hasattr(self, "calc_noise_button"):
            self._set_plot_button_selected(
                self.calc_noise_button, self.plotter.noise_reduction_config().enabled
            )
        if hasattr(self, "calc_cursor_button"):
            self._set_plot_button_selected(
                self.calc_cursor_button, self.plotter.cursor_probe_enabled
            )
        self._set_plot_button_selected(
            self.calc_stage_button, self.plotter.stage_analysis_enabled
        )

    def set_plot_mode_combined(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_COMBINED)
        self._update_plot_mode_buttons()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status(
            "Режим общего графика: масса и температурные кривые.", emit_log=False
        )

    def set_plot_mode_split(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_SPLIT)
        self._update_plot_mode_buttons()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status(
            "Раздельный режим графика: масса, камера PV и термопара SV.", emit_log=False
        )

    def set_plot_mode_mass(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_MASS)
        self._update_plot_mode_buttons()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status("Показан только график массы.", emit_log=False)

    def set_plot_mode_temp(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_TEMP)
        self._update_plot_mode_buttons()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status(
            "Показаны только температурные кривые: камера PV и термопара SV.",
            emit_log=False,
        )

    def set_plot_mode_delta(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_DELTA)
        self._update_plot_mode_buttons()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status(
            "Показана температурная дельта: масса, ΔPV и ΔSV.", emit_log=False
        )

    def toggle_plot_range_mode(self) -> None:
        target_mode = (
            LivePlotter.RANGE_FULL
            if self.plotter.plot_range_mode == LivePlotter.RANGE_SEGMENT
            else LivePlotter.RANGE_SEGMENT
        )
        self.plotter.set_plot_range_mode(target_mode)
        self._update_plot_range_buttons()
        self._set_status(
            "Режим окна графика: показывается вся кривая от начала записи."
            if target_mode == LivePlotter.RANGE_FULL
            else "Режим окна графика: показывается текущий сегмент по времени.",
            emit_log=False,
        )

    def toggle_plot_render_mode(self) -> None:
        points_enabled = self.plotter.toggle_points()
        self._update_plot_render_buttons()
        self._update_calc_buttons()
        self._set_status(
            "Включён режим точек для просмотра отдельных измерений."
            if points_enabled
            else "Включён обычный режим линий.",
            emit_log=False,
        )

    def toggle_plot_time_axis_mode(self) -> None:
        relative = self.plotter.toggle_time_axis_mode()
        self._update_plot_time_axis_button()
        self._set_status(
            "По оси X показывается время от начала записи."
            if relative
            else "По оси X показывается реальное время измерений.",
            emit_log=False,
        )

    def set_plot_smooth(self) -> None:
        smooth_enabled = self.plotter.toggle_smoothing()
        self._update_plot_render_buttons()
        self._update_calc_buttons()
        self._set_status(
            "Включён сглаженный режим отображения."
            if smooth_enabled
            else "Сглаженный режим отображения отключён.",
            emit_log=False,
        )

    def open_noise_reduction_menu(self) -> None:
        window = tk.Toplevel(self)
        window.title("Шумоподавление")
        window.transient(self)
        window.grab_set()
        window.geometry(f"{int(500 * self.ui_scale)}x{int(360 * self.ui_scale)}")
        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)
        config = self.plotter.noise_reduction_config()
        enabled_var = tk.BooleanVar(value=config.enabled)
        median_var = tk.IntVar(value=config.median_window)
        spike_var = tk.DoubleVar(value=config.spike_threshold)
        step_var = tk.DoubleVar(value=config.step_threshold)
        target_map = {
            "all": "Все линии",
            "mass": "Масса",
            "temperature": "Камера PV",
            "thermocouple": "Термопара SV",
        }
        target_var = tk.StringVar(value=target_map.get(config.target_series, "Все линии"))
        ttk.Checkbutton(
            outer,
            text="Включить шумоподавление",
            variable=enabled_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w")
        form = ttk.Frame(outer, style="Card.TFrame")
        form.pack(fill="x", pady=(self._pad_y(12), self._pad_y(8)))
        fields = [
            ("Линия", target_var),
            ("Медианное окно", median_var),
            ("Порог выброса", spike_var),
            ("Порог шага", step_var),
        ]
        for row, (label, var) in enumerate(fields):
            ttk.Label(form, text=label, style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=(0, self._pad_y(8)))
            if label == "Линия":
                combo = ttk.Combobox(
                    form,
                    state="readonly",
                    values=list(target_map.values()),
                    width=20,
                    textvariable=target_var,
                )
                try:
                    combo.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
                except Exception:
                    pass
                combo.grid(row=row, column=1, sticky="ew", padx=(self._pad_x(12), 0), pady=(0, self._pad_y(8)))
            else:
                ttk.Entry(form, textvariable=var, width=14).grid(row=row, column=1, sticky="ew", padx=(self._pad_x(12), 0), pady=(0, self._pad_y(8)))
        form.grid_columnconfigure(1, weight=1)

        def apply_settings() -> None:
            reverse_target_map = {value: key for key, value in target_map.items()}
            new_config = NoiseReductionConfig(
                enabled=bool(enabled_var.get()),
                median_window=max(1, int(median_var.get())),
                spike_threshold=max(0.01, float(spike_var.get())),
                step_threshold=max(0.0, float(step_var.get())),
                target_series=reverse_target_map.get(target_var.get(), "all"),
            )
            self.plotter.set_noise_reduction(new_config)
            self._set_status("Параметры шумоподавления обновлены.", emit_log=False)
            window.destroy()

        def auto_settings() -> None:
            reverse_target_map = {value: key for key, value in target_map.items()}
            auto_config = self.plotter.auto_configure_noise_reduction(
                reverse_target_map.get(target_var.get(), "all")
            )
            enabled_var.set(auto_config.enabled)
            median_var.set(auto_config.median_window)
            spike_var.set(auto_config.spike_threshold)
            step_var.set(auto_config.step_threshold)

        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text="Авто", style="Soft.TButton", command=auto_settings).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(buttons, text="Применить", style="Accent.TButton", command=apply_settings).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(buttons, text="Закрыть", style="Soft.TButton", command=window.destroy).grid(row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0))
        for idx in range(3):
            buttons.grid_columnconfigure(idx, weight=1)

    def activate_calc_dtg(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_DTG)
        self._update_plot_mode_buttons()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status(
            "Режим DTG: скорость изменения массы во времени.", emit_log=False
        )

    def toggle_calc_normalization(self) -> None:
        active = self.plotter.toggle_normalization()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        self._set_status(
            "Нормализация массы включена."
            if active
            else "Нормализация массы отключена.",
            emit_log=False,
        )

    def open_calc_markers_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(
            label="Выключить маркеры" if self.plotter.markers_enabled else "Включить маркеры",
            command=self.toggle_calc_markers,
        )
        menu.add_command(label="Задать промежуток...", command=self.open_marker_range_dialog)
        x = self.calc_markers_button.winfo_rootx()
        y = self.calc_markers_button.winfo_rooty() + self.calc_markers_button.winfo_height()
        menu.tk_popup(x, y)
        menu.grab_release()

    def open_marker_range_dialog(self) -> None:
        if not self.measurement_records:
            self._set_status("Нет данных для задания маркеров.", logging.WARNING)
            return
        window = tk.Toplevel(self)
        window.title("Промежуток маркеров")
        window.transient(self)
        window.grab_set()
        window.geometry(f"{int(420 * self.ui_scale)}x{int(220 * self.ui_scale)}")
        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Задать промежуток A/B", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Введите время начала и конца в формате ЧЧ:ММ:СС. Формат понимает и старые, и новые сессии.",
            style="CardText.TLabel",
            wraplength=int(360 * self.ui_scale),
            justify="left",
        ).pack(anchor="w", pady=(self._pad_y(6), self._pad_y(10)))
        form = ttk.Frame(outer, style="Card.TFrame")
        form.pack(fill="x")
        start_var = tk.StringVar()
        end_var = tk.StringVar()
        for row, (label, var) in enumerate((("A", start_var), ("B", end_var))):
            ttk.Label(form, text=label, style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=(0, self._pad_y(8)))
            ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew", pady=(0, self._pad_y(8)))
        form.grid_columnconfigure(1, weight=1)

        def apply_marker_range() -> None:
            try:
                self._set_marker_range_by_time(start_var.get().strip(), end_var.get().strip())
            except ValueError as exc:
                messagebox.showwarning("Промежуток маркеров", str(exc), parent=window)
                return
            window.destroy()

        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", side="bottom", pady=(self._pad_y(12), 0))
        for idx in range(2):
            buttons.grid_columnconfigure(idx, weight=1)
        ttk.Button(buttons, text="Применить", style="Accent.TButton", command=apply_marker_range).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(buttons, text="Закрыть", style="Soft.TButton", command=window.destroy).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

    def _set_marker_range_by_time(self, start_raw: str, end_raw: str) -> None:
        if not self.measurement_records:
            raise ValueError("Нет данных для маркеров.")
        base_time = coerce_timestamp(self.measurement_records[0].timestamp)
        if base_time is None:
            raise ValueError("Не удалось разобрать время первой записи.")
        start_dt = self._parse_cut_boundary(start_raw, base_time=base_time)
        end_dt = self._parse_cut_boundary(end_raw, base_time=base_time)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt
        if not self.plotter.markers_enabled:
            self.plotter.toggle_markers()
        self.plotter.sync_marker_a_to_timestamp(start_dt.isoformat())
        self.plotter.sync_marker_b_to_timestamp(end_dt.isoformat())
        if self.plotter.view_mode == LivePlotter.VIEW_TEMP:
            self.plotter.set_view_mode(LivePlotter.VIEW_COMBINED)
            self._update_plot_mode_buttons()
            self._refresh_plot_legend()
        self._update_calc_buttons()
        summary = self.plotter.calculation_summary()
        self.diag_status_var.set(
            f"Маркеры A/B: Δm={summary['delta_mass']} | Δm%={summary['delta_mass_percent']} | ΔT={summary['delta_temperature']} | Δt={summary['delta_time']}"
        )
        self._set_status("Маркеры A/B выставлены по заданному промежутку.", emit_log=False)

    def toggle_calc_markers(self) -> None:
        active = self.plotter.toggle_markers()
        if active and self.plotter.view_mode == LivePlotter.VIEW_TEMP:
            self.plotter.set_view_mode(LivePlotter.VIEW_COMBINED)
            self._update_plot_mode_buttons()
            self._refresh_plot_legend()
        self._update_calc_buttons()
        if active:
            summary = self.plotter.calculation_summary()
            self.diag_status_var.set(
                f"Маркеры A/B: Δm={summary['delta_mass']} | Δm%={summary['delta_mass_percent']} | ΔT={summary['delta_temperature']} | Δt={summary['delta_time']}"
            )
            self._set_status(
                "Маркеры A/B включены: перетаскивайте точки A и B мышью по графику массы.",
                emit_log=False,
            )
        else:
            self._set_status("Маркеры A/B отключены.", emit_log=False)

    def toggle_calc_heating_profile(self) -> None:
        active = self.plotter.toggle_heating_profile()
        self._refresh_plot_legend()
        self._update_calc_buttons()
        if active:
            self._set_status(
                "Профиль нагрева включён: эталонная линия строится по данным камеры и обновляется автоматически.",
                emit_log=False,
            )
        else:
            self._set_status("Профиль нагрева отключён.", emit_log=False)

    def toggle_calc_cursor(self) -> None:
        active = self.plotter.toggle_cursor_probe()
        self._update_calc_buttons()
        self._set_status(
            "Режим курсора включён: наведите на график, чтобы видеть координаты."
            if active
            else "Режим курсора отключён.",
            emit_log=False,
        )

    def clear_calc_cursor_marks(self) -> None:
        self.plotter.clear_cursor_anchors()
        self._update_calc_buttons()
        self._set_status("Все метки курсора удалены.", emit_log=False)

    def toggle_calc_stage_analysis(self) -> None:
        active = self.plotter.toggle_stage_analysis()
        self._update_calc_buttons()
        self._set_status(
            "Анализ стадий включён." if active else "Анализ стадий отключён.",
            emit_log=False,
        )

    def show_calc_summary(self) -> None:
        self._refresh_summary_tab()
        if hasattr(self, "center_content_tabs"):
            self.center_content_tabs.select(self.summary_tab)
        self._set_status("Открыта вкладка отчёта измерений.", emit_log=False)

    def _build_summary_text(self, summary: dict[str, str], furnace: dict[str, str], mass: dict[str, str]) -> str:
        analysis_lines = [
            "Сводка анализа",
            f"Изменение массы от начальной точки до конца: {summary['run_delta_mass']}",
            f"Изменение массы от начальной точки до конца, %: {summary['run_delta_mass_percent']}",
            f"Изменение температуры от начальной точки до конца: {summary['run_delta_temperature']}",
            f"Время от начальной точки до конца: {summary['run_delta_time']}",
            "",
            "Расчёты по маркерам A/B",
            f"Изменение массы между A и B: {summary['delta_mass']}",
            f"Изменение массы между A и B, %: {summary['delta_mass_percent']}",
            f"Изменение температуры между A и B: {summary['delta_temperature']}",
            f"Время между A и B: {summary['delta_time']}",
            f"Макс. DTG: {summary['max_dtg']}",
            f"Стадии: {summary['stage_range']}",
            "",
            "Данные печи",
            f"Источник: {furnace['source']}",
            f"Старт нагрева: {furnace['heat_start']}",
            f"До пика: {furnace['time_to_peak']}",
            f"Пик: {furnace['peak_temperature']}",
            f"Остывание до стабильности: {furnace['cooldown_to_stable']}",
            f"Стабильная температура: {furnace['stable_temperature']}",
            f"Минимум: {furnace['minimum_temperature']}",
            f"Общее время: {furnace['elapsed']}",
            "",
            "Данные весов",
            f"Начальная масса: {mass['initial_mass']}",
            f"Максимальная масса: {mass['max_mass']}",
            f"Время максимума: {mass['max_mass_time']}",
            f"Температура при максимуме: {mass['max_mass_temp']}",
            f"Время до максимума: {mass['max_mass_delta']}",
            f"Минимальная масса: {mass['min_mass']}",
            f"Время минимума: {mass['min_mass_time']}",
            f"Температура при минимуме: {mass['min_mass_temp']}",
            f"Время до минимума: {mass['min_mass_delta']}",
        ]
        return "\n".join(analysis_lines)

    def _build_run_summary(self) -> dict[str, str]:
        valid_rows = [
            (record.timestamp, float(record.mass), record.furnace_pv)
            for record in self.measurement_records
            if record.mass is not None
        ]
        if not valid_rows:
            return {
                "run_delta_mass": "идёт анализ",
                "run_delta_mass_percent": "идёт анализ",
                "run_delta_temperature": "идёт анализ",
                "run_delta_time": "идёт анализ",
            }
        baseline_mass = self._baseline_mass if self._baseline_mass is not None else valid_rows[0][1]
        baseline_timestamp = self._baseline_mass_timestamp or valid_rows[0][0]
        baseline_temp = next(
            (
                row[2]
                for row in valid_rows
                if row[0] == baseline_timestamp and row[2] is not None
            ),
            valid_rows[0][2],
        )
        end_mass = valid_rows[-1][1]
        end_temp = valid_rows[-1][2]
        delta_mass = end_mass - baseline_mass
        delta_percent = (delta_mass / baseline_mass * 100.0) if baseline_mass not in (0, None) else None
        delta_temp = (
            float(end_temp) - float(baseline_temp)
            if end_temp is not None and baseline_temp is not None
            else None
        )
        return {
            "run_delta_mass": f"{delta_mass:.3f} г",
            "run_delta_mass_percent": f"{delta_percent:.2f} %" if delta_percent is not None else "идёт анализ",
            "run_delta_temperature": f"{delta_temp:.1f} °C" if delta_temp is not None else "идёт анализ",
            "run_delta_time": self._format_time_delta(baseline_timestamp, valid_rows[-1][0]),
        }

    def _build_mass_summary(self) -> dict[str, str]:
        valid_rows = [
            (record.timestamp, float(record.mass), record.furnace_pv)
            for record in self.measurement_records
            if record.mass is not None
        ]
        if not valid_rows:
            return {
                "initial_mass": "идёт анализ",
                "max_mass": "идёт анализ",
                "max_mass_time": "идёт анализ",
                "max_mass_temp": "идёт анализ",
                "max_mass_delta": "идёт анализ",
                "min_mass": "идёт анализ",
                "min_mass_time": "идёт анализ",
                "min_mass_temp": "идёт анализ",
                "min_mass_delta": "идёт анализ",
            }

        baseline_mass = self._baseline_mass if self._baseline_mass is not None else valid_rows[0][1]
        baseline_timestamp = self._baseline_mass_timestamp or valid_rows[0][0]
        max_row = max(valid_rows, key=lambda item: item[1])
        min_row = min(valid_rows, key=lambda item: item[1])
        return {
            "initial_mass": f"{baseline_mass:.3f} г",
            "max_mass": f"{max_row[1]:.3f} г",
            "max_mass_time": self._format_card_timestamp(max_row[0]).replace("Текущее: ", ""),
            "max_mass_temp": f"{_format_value(max_row[2], 1)} °C",
            "max_mass_delta": self._format_time_delta(baseline_timestamp, max_row[0]),
            "min_mass": f"{min_row[1]:.3f} г",
            "min_mass_time": self._format_card_timestamp(min_row[0]).replace("Текущее: ", ""),
            "min_mass_temp": f"{_format_value(min_row[2], 1)} °C",
            "min_mass_delta": self._format_time_delta(baseline_timestamp, min_row[0]),
        }

    def _format_time_delta(self, left_raw: str, right_raw: str) -> str:
        try:
            left_dt = coerce_timestamp(left_raw)
            right_dt = coerce_timestamp(right_raw)
            if left_dt is None or right_dt is None:
                raise ValueError
        except ValueError:
            return "--"
        seconds = max(0, int((right_dt - left_dt).total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _refresh_summary_tab(self) -> None:
        if not hasattr(self, "summary_analysis_tab"):
            return
        summary = self.plotter.calculation_summary()
        run_summary = self._build_run_summary()
        furnace = self.plotter.furnace_summary()
        mass = self._build_mass_summary()
        self.diag_status_var.set(
            f"Δm={summary['delta_mass']} | Δm%={summary['delta_mass_percent']} | ΔT={summary['delta_temperature']} | Δt={summary['delta_time']}"
        )

        analysis_sections = [
            (
                "Общие расчёты",
                "Показатели всей записи от базовой точки до конца текущих данных.",
                [
                    ("Изменение массы от начальной точки до конца", run_summary["run_delta_mass"], "Разница между начальной или синхронизированной точкой и последней доступной записью."),
                    ("Изменение массы от начальной точки до конца, %", run_summary["run_delta_mass_percent"], "Процентное изменение массы от базовой точки до конца записи."),
                    ("Изменение температуры от начальной точки до конца", run_summary["run_delta_temperature"], "Изменение температуры камеры от базовой точки до последней записи."),
                    ("Время от начальной точки до конца", run_summary["run_delta_time"], "Длительность от базовой точки до конца текущей записи."),
                ],
            ),
            (
                "Расчёты по маркерам A/B",
                "Этот блок зависит от установленных маркеров. Если маркеры не заданы, расчёты не выполняются.",
                [
                    ("Изменение массы между A и B", summary["delta_mass"] if self.plotter.markers_enabled else "не задано", "Разница массы между маркерами A и B."),
                    ("Изменение массы между A и B, %", summary["delta_mass_percent"] if self.plotter.markers_enabled else "не задано", "Процентное изменение массы между маркерами A и B."),
                    ("Изменение температуры между A и B", summary["delta_temperature"] if self.plotter.markers_enabled else "не задано", "Изменение температуры камеры между маркерами A и B."),
                    ("Время между A и B", summary["delta_time"] if self.plotter.markers_enabled else "не задано", "Временной интервал между маркерами A и B."),
                ],
            ),
            (
                "Дополнительный анализ",
                "Расчёты, которые не зависят от маркеров и показывают форму процесса.",
                [
                    ("Макс. DTG", summary["max_dtg"], "Пиковая скорость изменения массы."),
                    ("Стадии", summary["stage_range"], "Найденный диапазон стадий процесса."),
                ],
            ),
        ]
        furnace_specs = [
            ("Источник", furnace["source"], "Для профиля нагрева используется камера."),
            ("Старт нагрева", furnace["heat_start"], "Момент выхода камеры в устойчивый нагрев."),
            ("До пика", furnace["time_to_peak"], "Время нагрева от старта до пикового перегрева."),
            ("Пик", furnace["peak_temperature"], "Максимальная температура камеры."),
            ("Перегрев над стабильной зоной (инерция)", furnace["overheat_above_stable"], "Насколько камера или объект перелетели выше рабочей стабильной температуры."),
            ("Остывание до стабильности", furnace["cooldown_to_stable"], "Время от пика до стабильной зоны."),
            ("Стабильная температура", furnace["stable_temperature"], "Оценка рабочей стабильной зоны."),
            ("Минимум", furnace["minimum_temperature"], "Минимум по текущей сессии камеры."),
            ("Общее время", furnace["elapsed"], "Время процесса с начала нагрева."),
        ]
        mass_specs = [
            ("Начальная масса", mass["initial_mass"], "Исходная масса объекта или синхронизированная базовая точка."),
            ("Максимальная масса", mass["max_mass"], "Наибольшее значение массы за всю текущую запись."),
            ("Время максимума", mass["max_mass_time"], "Момент, в который была зафиксирована максимальная масса."),
            ("Температура при максимуме", mass["max_mass_temp"], "Температура камеры в момент максимальной массы."),
            ("Время до максимума", mass["max_mass_delta"], "Интервал от начальной точки до максимума."),
            ("Минимальная масса", mass["min_mass"], "Наименьшее значение массы за всю текущую запись."),
            ("Время минимума", mass["min_mass_time"], "Момент, в который была зафиксирована минимальная масса."),
            ("Температура при минимуме", mass["min_mass_temp"], "Температура камеры в момент минимальной массы."),
            ("Время до минимума", mass["min_mass_delta"], "Интервал от начальной точки до минимума."),
        ]
        self._render_summary_sections(self.summary_analysis_tab, analysis_sections)
        self._render_summary_sections(
            self.summary_furnace_tab,
            [("Параметры печи", "Сводка нагрева и стабилизации камеры.", furnace_specs)],
        )
        self._render_summary_sections(
            self.summary_mass_tab,
            [("Параметры массы", "Показатели массы по всей загруженной записи.", mass_specs)],
        )
        summary_payload = dict(summary)
        summary_payload.update(run_summary)
        self._summary_export_text = self._build_summary_text(summary_payload, furnace, mass)

    def _render_summary_cards(self, parent, specs: list[tuple[str, str, str]]) -> None:
        host = parent["body"] if isinstance(parent, dict) else parent
        for child in host.winfo_children():
            child.destroy()
        metrics = ttk.Frame(host, style="Card.TFrame")
        metrics.pack(fill="both", expand=True)
        for idx in range(2):
            metrics.grid_columnconfigure(idx, weight=1)
        for idx, (title, value, hint) in enumerate(specs):
            card = ttk.Frame(metrics, style="CardAlt.TFrame", padding=self._pad(12, 10))
            row = idx // 2
            column = idx % 2
            card.grid(row=row, column=column, sticky="nsew", padx=self._pad_pair(4), pady=(0, self._pad_y(10)))
            ttk.Label(card, text=title, style="Subtitle.TLabel").pack(anchor="w")
            value_label = ttk.Label(card, text=str(value), style="CardTitle.TLabel", justify="left", wraplength=int(320 * self.ui_scale))
            value_label.configure(font=("Segoe UI Semibold", max(16, int(19 * self.ui_scale))))
            value_label.pack(anchor="w", pady=(self._pad_y(4), self._pad_y(6)))
            ttk.Label(card, text=hint, style="CardText.TLabel", justify="left", wraplength=int(320 * self.ui_scale)).pack(anchor="w")
        if isinstance(parent, dict):
            parent["canvas"].configure(scrollregion=parent["canvas"].bbox("all"))

    def _render_summary_sections(self, parent, sections: list[tuple[str, str, list[tuple[str, str, str]]]]) -> None:
        host = parent["body"] if isinstance(parent, dict) else parent
        for child in host.winfo_children():
            child.destroy()
        for section_title, section_hint, specs in sections:
            shell = ttk.Frame(host, style="Card.TFrame", padding=self._pad(10, 10))
            shell.pack(fill="x", expand=True, pady=(0, self._pad_y(10)))
            ttk.Label(shell, text=section_title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(
                shell,
                text=section_hint,
                style="CardText.TLabel",
                justify="left",
                wraplength=int(720 * self.ui_scale),
            ).pack(anchor="w", pady=(self._pad_y(4), self._pad_y(10)))
            cards_host = ttk.Frame(shell, style="Card.TFrame")
            cards_host.pack(fill="x", expand=True)
            self._render_summary_cards(cards_host, specs)
        if isinstance(parent, dict):
            parent["canvas"].configure(scrollregion=parent["canvas"].bbox("all"))

    def _save_current_summary_tab_to_txt(self) -> None:
        self._refresh_summary_tab()
        self._save_calc_summary_to_txt(self._summary_export_text, parent=self)

    def _save_calc_summary_to_txt(self, message: str, *, parent) -> None:
        target = filedialog.asksaveasfilename(
            parent=parent,
            title="Сохранить сводку",
            defaultextension=".txt",
            initialfile="analysis_summary.txt",
            filetypes=[("TXT", "*.txt")],
        )
        if not target:
            return
        Path(target).write_text(message, encoding="utf-8")
        self._set_status("Сводка анализа сохранена в TXT.", emit_log=False)

    def open_plot_scale_dialog(self) -> None:
        window = tk.Toplevel(self)
        window.title("Масштаб графика")
        window.transient(self)
        window.grab_set()
        window.geometry(f"{int(620 * self.ui_scale)}x{int(470 * self.ui_scale)}")

        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Параметры масштаба", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            outer,
            text="Настройки применяются ко всем режимам графика и сохраняются для PNG.",
            style="CardText.TLabel",
            justify="left",
            wraplength=int(520 * self.ui_scale),
        ).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(self._pad_y(4), self._pad_y(12)),
        )

        autoscale_var = tk.BooleanVar(
            value=bool(self.config_data.app.plot_autoscale_enabled)
        )
        x_seconds_var = tk.StringVar(
            value=str(int(round(self.config_data.app.plot_manual_x_seconds)))
        )
        y_span_var = tk.StringVar(
            value=f"{self.config_data.app.plot_manual_y_span:.0f}"
        )
        headroom_var = tk.StringVar(value=f"{self.config_data.app.plot_y_headroom:.0f}")

        ttk.Checkbutton(
            outer,
            text="Автомасштабирование",
            variable=autoscale_var,
            style="Card.TCheckbutton",
            takefocus=False,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, self._pad_y(10)))

        fields = [
            (
                "Длина оси X, сек",
                x_seconds_var,
                "Сколько последних секунд держать в окне в ручном режиме.",
            ),
            (
                "Длина оси Y",
                y_span_var,
                "Высота видимого диапазона по Y в ручном режиме.",
            ),
            (
                "Запас сверху",
                headroom_var,
                "Сколько добавлять сверху от текущего максимума.",
            ),
        ]

        for row, (label, var, hint) in enumerate(fields, start=3):
            ttk.Label(outer, text=label, style="CardText.TLabel").grid(
                row=row, column=0, sticky="w", pady=(0, self._pad_y(8))
            )
            ttk.Entry(outer, textvariable=var).grid(
                row=row, column=1, sticky="ew", pady=(0, self._pad_y(8))
            )
            ttk.Label(
                outer,
                text=hint,
                style="CardText.TLabel",
                justify="left",
                wraplength=int(520 * self.ui_scale),
            ).grid(
                row=row + 1,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(0, self._pad_y(8)),
            )

        outer.grid_columnconfigure(1, weight=1)
        footer = ttk.Frame(outer, style="Card.TFrame")
        footer.grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=(self._pad_y(10), 0)
        )
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_columnconfigure(1, weight=1)

        def apply_scale() -> None:
            try:
                self.config_data.app.plot_autoscale_enabled = bool(autoscale_var.get())
                self.config_data.app.plot_manual_x_seconds = max(
                    10.0, float(x_seconds_var.get().replace(",", "."))
                )
                self.config_data.app.plot_manual_y_span = max(
                    1.0, float(y_span_var.get().replace(",", "."))
                )
                self.config_data.app.plot_y_headroom = max(
                    0.0, float(headroom_var.get().replace(",", "."))
                )
            except ValueError:
                messagebox.showwarning(
                    "Масштаб графика",
                    "Поля масштаба должны быть числами.",
                    parent=window,
                )
                return

            self.plotter.configure_scale_mode(
                autoscale_enabled=self.config_data.app.plot_autoscale_enabled,
                manual_x_seconds=self.config_data.app.plot_manual_x_seconds,
                manual_y_span=self.config_data.app.plot_manual_y_span,
                y_headroom=self.config_data.app.plot_y_headroom,
            )
            save_config(self.config_data, self.config_path)
            self._set_status("Параметры масштаба графика обновлены.", emit_log=False)
            window.destroy()

        ttk.Button(
            footer, text="Применить", style="Accent.TButton", command=apply_scale
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            footer, text="Закрыть", style="Soft.TButton", command=window.destroy
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

    def open_plot_style_editor(self) -> None:
        window = tk.Toplevel(self)
        window.title("Стиль кривых")
        window.transient(self)
        window.grab_set()
        window.geometry(f"{int(520 * self.ui_scale)}x{int(300 * self.ui_scale)}")
        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)
        rows: dict[str, dict[str, object]] = {}
        styles = self.plotter.get_series_styles()
        labels = {
            "mass": "Масса",
            "temperature": "Камера PV",
            "thermocouple": "Термопара SV",
        }
        for row, key in enumerate(("mass", "temperature", "thermocouple")):
            ttk.Label(outer, text=labels[key], style="CardText.TLabel").grid(
                row=row, column=0, sticky="w", pady=(0, self._pad_y(8))
            )
            color_var = tk.StringVar(value=str(styles[key]["color"]))
            width_var = tk.StringVar(value=str(styles[key]["linewidth"]))
            line_var = tk.StringVar(
                value="solid" if styles[key]["linestyle"] == "-" else "dashed"
            )
            ttk.Entry(outer, textvariable=color_var, width=12).grid(
                row=row, column=1, sticky="ew", padx=self._pad_pair(4)
            )
            ttk.Button(
                outer,
                text="Цвет",
                style="Soft.TButton",
                command=lambda var=color_var: var.set(
                    colorchooser.askcolor(color=var.get(), parent=window)[1]
                    or var.get()
                ),
            ).grid(row=row, column=2, sticky="ew", padx=self._pad_pair(4))
            ttk.Combobox(
                outer,
                textvariable=width_var,
                values=["1.0", "1.5", "2.0", "2.5", "3.0", "4.0"],
                width=6,
                state="readonly",
            ).grid(row=row, column=3, sticky="ew", padx=self._pad_pair(4))
            ttk.Combobox(
                outer,
                textvariable=line_var,
                values=["solid", "dashed"],
                width=8,
                state="readonly",
            ).grid(row=row, column=4, sticky="ew", padx=(self._pad_x(4), 0))
            rows[key] = {
                "color": color_var,
                "linewidth": width_var,
                "linestyle": line_var,
            }
        for col in range(5):
            outer.grid_columnconfigure(col, weight=1)
        footer = ttk.Frame(outer, style="Card.TFrame")
        footer.grid(
            row=4, column=0, columnspan=5, sticky="ew", pady=(self._pad_y(16), 0)
        )
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_columnconfigure(1, weight=1)

        def save_styles() -> None:
            payload = {}
            for key, values in rows.items():
                payload[key] = {
                    "color": values["color"].get(),
                    "linewidth": float(values["linewidth"].get()),
                    "linestyle": "-" if values["linestyle"].get() == "solid" else "--",
                }
            self.config_data.app.plot_styles = payload
            self.plotter.apply_series_styles(payload)
            save_config(self.config_data, self.config_path)
            self._refresh_plot_legend()
            self._set_status("Стиль кривых сохранён.", emit_log=False)
            window.destroy()

        ttk.Button(
            footer, text="Сохранить", style="Accent.TButton", command=save_styles
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            footer, text="Отмена", style="Soft.TButton", command=window.destroy
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

    def _scale_test_mode_active(self) -> bool:
        scope = self._test_mode_scope_from_label(self.config_data.app.test_mode_scope)
        return bool(self.config_data.app.test_mode and scope in {"all", "scale"})

    def _furnace_test_mode_active(self) -> bool:
        scope = self._test_mode_scope_from_label(self.config_data.app.test_mode_scope)
        return bool(self.config_data.app.test_mode and scope in {"all", "furnace"})

    def _scale_indicator_expected(self) -> bool:
        if self._scale_test_mode_active():
            return True
        if self.config_data.app.test_mode:
            return False
        return bool(self.config_data.scale.enabled)

    def _furnace_indicator_expected(self) -> bool:
        if self._furnace_test_mode_active():
            return True
        if self.config_data.app.test_mode:
            return False
        return bool(self.config_data.furnace.enabled)

    def _log_connection_transitions(
        self,
        *,
        prev_scale_connected: bool,
        prev_furnace_connected: bool,
        snapshot: AcquisitionSnapshot,
    ) -> None:
        if prev_scale_connected != snapshot.scale_connected:
            if snapshot.scale_connected:
                mode = "эмуляция" if self._scale_test_mode_active() else "COM"
                self.logger.info(
                    "Весы на связи (%s: %s).", mode, snapshot.scale_port or "не указан"
                )
            else:
                self.logger.warning("Связь с весами потеряна.")

        if prev_furnace_connected != snapshot.furnace_connected:
            if snapshot.furnace_connected:
                mode = "эмуляция" if self._furnace_test_mode_active() else "COM"
                self.logger.info(
                    "Печь на связи (%s: %s).",
                    mode,
                    snapshot.furnace_port or "не указан",
                )
            else:
                self.logger.warning("Связь с печью потеряна.")

    def save_plot_image(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить график",
            defaultextension=".png",
            initialfile="datafusion_rt_plot.png",
            filetypes=[("PNG", "*.png")],
        )
        if not target:
            return
        destination = Path(target)
        if not destination.suffix:
            destination = destination.with_suffix(".png")
        try:
            self.plotter.save_image(destination)
            self._set_status(f"График сохранён: {destination.name}")
        except Exception as exc:
            self._set_status(f"Не удалось сохранить график: {exc}", logging.WARNING)

    def toggle_plot_zoom(self) -> None:
        active = self.plotter.toggle_zoom()
        self.plot_zoom_button.configure(text="🔍*" if active else "🔍")
        self.plot_pan_button.configure(text="↔ Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, active)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def toggle_plot_pan(self) -> None:
        active = self.plotter.toggle_pan()
        self.plot_pan_button.configure(text="↔ Сдвиг*" if active else "↔ Сдвиг")
        self.plot_zoom_button.configure(text="🔍")
        self._set_plot_button_selected(self.plot_pan_button, active)
        self._set_plot_button_selected(self.plot_zoom_button, False)

    def zoom_in_plot(self) -> None:
        self.plotter.zoom_in()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="↔ Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def zoom_out_plot(self) -> None:
        self.plotter.zoom_out()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="↔ Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def reset_plot_view(self) -> None:
        self.plotter.reset_view()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="↔ Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def autoscale_plot(self) -> None:
        self.plotter.autoscale()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="↔ Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def toggle_plot_pause(self) -> None:
        paused = self.plotter.toggle_display_pause()
        self.plot_pause_button.configure(
            style="Accent.TButton" if paused else "WindowIcon.TButton"
        )
        self.plot_live_button.state(["!disabled"] if paused else ["disabled"])
        self._set_status(
            "Live-движение графика остановлено, запись данных продолжается."
            if paused
            else "Пауза графика снята.",
            emit_log=False,
        )

    def resume_plot_live_view(self) -> None:
        self.plotter.resume_live_view()
        self.plot_pause_button.configure(style="WindowIcon.TButton")
        self.plot_live_button.state(["disabled"])
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="↔ Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)
        self._set_status("График возвращён к текущему моменту.", emit_log=False)

    def _status_text(self, snapshot: AcquisitionSnapshot) -> tuple[str, str]:
        if snapshot.test_mode:
            return "Тест", "Данные генерируются программой"
        scale_enabled = self.config_data.scale.enabled
        furnace_enabled = self.config_data.furnace.enabled
        if (
            scale_enabled
            and furnace_enabled
            and snapshot.scale_connected
            and snapshot.furnace_connected
        ):
            return "Готово", "Весы и печь на связи"
        scale_state = (
            "отключены"
            if not scale_enabled
            else "на связи"
            if snapshot.scale_connected
            else "нет связи"
        )
        furnace_state = (
            "отключена"
            if not furnace_enabled
            else "на связи"
            if snapshot.furnace_connected
            else "нет связи"
        )
        if (scale_enabled and snapshot.scale_connected) or (
            furnace_enabled and snapshot.furnace_connected
        ):
            return "Частично", f"Весы: {scale_state} | Печь: {furnace_state}"
        if not scale_enabled and not furnace_enabled:
            return "Ожидание", "Весы и печь отключены в настройках"
        return "Нет связи", f"Весы: {scale_state} | Печь: {furnace_state}"

    def _table_time_suffix_label(self) -> str:
        selection = (
            self.table_time_suffix_combo.get()
            if hasattr(self, "table_time_suffix_combo")
            else "Без зоны"
        )
        if selection == "местн.":
            return " местн."
        if selection == "UTC+смещ.":
            offset = datetime.now().astimezone().strftime("%z")
            if len(offset) == 5:
                offset = f"{offset[:3]}:{offset[3:]}"
            return f" UTC{offset}"
        return ""

    def _format_table_timestamp(self, raw_timestamp: str) -> str:
        dt = coerce_timestamp(raw_timestamp)
        if dt is None:
            cleaned = raw_timestamp.replace("T", " ")
            return cleaned
        selection = (
            self.table_time_format_combo.get()
            if hasattr(self, "table_time_format_combo")
            else "ЧЧ:ММ:СС.мс"
        )
        formats = {
            "ЧЧ:ММ:СС": "%H:%M:%S",
            "ЧЧ:ММ:СС.мс": "%H:%M:%S.%f",
            "Дата+время": "%Y-%m-%d %H:%M:%S",
            "Дата+время.мс": "%Y-%m-%d %H:%M:%S.%f",
        }
        value = dt.strftime(formats.get(selection, "%H:%M:%S.%f"))
        if "%f" in formats.get(selection, "%H:%M:%S.%f"):
            value = value[:-3]
        return value + self._table_time_suffix_label()

    def _refresh_table_timestamps(self) -> None:
        if not hasattr(self, "measurements_table"):
            return
        for item_id in self.measurements_table.get_children():
            raw_timestamp = self._table_timestamp_map.get(item_id)
            if not raw_timestamp:
                continue
            values = list(self.measurements_table.item(item_id, "values"))
            if not values:
                continue
            timestamp_index = self.table_column_order.index("timestamp")
            values[timestamp_index] = self._format_table_timestamp(raw_timestamp)
            self.measurements_table.item(item_id, values=values)

    def _format_card_timestamp(self, raw_timestamp: str) -> str:
        dt = coerce_timestamp(raw_timestamp)
        if dt is not None:
            return f"Текущее: {dt.strftime('%H:%M:%S')}"
        else:
            cleaned = raw_timestamp.replace("T", " ")
            if len(cleaned) >= 19:
                return f"Текущее: {cleaned[11:19]}"
            return f"Текущее: {cleaned}"

    def _format_elapsed_time(self, raw_timestamp: str) -> str:
        current = coerce_timestamp(raw_timestamp)
        if current is None:
            return "--:--"
        if self._session_started_at is None:
            self._session_started_at = current
        elapsed = max(0, int((current - self._session_started_at).total_seconds()))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _capture_measurement_baseline(self, record) -> None:
        current = coerce_timestamp(record.timestamp)
        if current is None:
            return
        if self._session_started_at is None:
            self._session_started_at = current
        if record.mass is None:
            return
        if self._baseline_mass is None:
            self._baseline_mass = float(record.mass)
            self._baseline_mass_timestamp = record.timestamp
            self._baseline_mass_samples = [float(record.mass)]
            self.mass_card.set_secondary(
                f"Начальная: {_format_value(self._baseline_mass, 3)} г"
            )

    def sync_measurement_baseline(self) -> None:
        if not self.measurement_records:
            self._set_status("Нет данных для синхронизации начальной точки.", logging.WARNING)
            return
        self.open_mass_sync_dialog()

    def open_mass_sync_dialog(self) -> None:
        if not self.measurement_records:
            self._set_status("Нет данных для синхронизации начальной точки.", logging.WARNING)
            return
        first_record = next((record for record in self.measurement_records if record.mass is not None), None)
        last_record = next((record for record in reversed(self.measurement_records) if record.mass is not None), None)
        if first_record is None or last_record is None:
            self._set_status("Нет доступной массы для синхронизации.", logging.WARNING)
            return
        window = tk.Toplevel(self)
        window.title("Синхронизация начальной массы")
        window.transient(self)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW", lambda: (self.plotter.cancel_interactive_pick(), window.destroy()))
        window.geometry(f"{int(620 * self.ui_scale)}x{int(420 * self.ui_scale)}")
        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Синхронизация начальной массы", style="CardTitle.TLabel").pack(anchor="w")
        default_record = first_record
        picked_value_var = tk.StringVar(value=_format_value(default_record.mass, 3))
        picked_timestamp_var = tk.StringVar(value=default_record.timestamp)
        picked_source_var = tk.StringVar(value="Первая точка")
        ttk.Label(
            outer,
            text="Укажите массу вручную или выберите точку на графике.",
            style="CardText.TLabel",
            wraplength=int(420 * self.ui_scale),
            justify="left",
        ).pack(anchor="w", pady=(self._pad_y(6), self._pad_y(10)))
        form = ttk.Frame(outer, style="Card.TFrame")
        form.pack(fill="x")
        ttk.Label(form, text="Масса", style="CardText.TLabel").grid(row=0, column=0, sticky="w", pady=(0, self._pad_y(8)))
        ttk.Entry(form, textvariable=picked_value_var).grid(row=0, column=1, sticky="ew", pady=(0, self._pad_y(8)))
        ttk.Label(form, text="Первая точка", style="CardText.TLabel").grid(row=1, column=0, sticky="w", pady=(0, self._pad_y(6)))
        ttk.Label(form, text=f"{_format_value(first_record.mass, 3)} г | {self._format_card_timestamp(first_record.timestamp).replace('Текущее: ', '')}", style="CardText.TLabel").grid(row=1, column=1, sticky="w", pady=(0, self._pad_y(6)))
        ttk.Label(form, text="Текущая точка", style="CardText.TLabel").grid(row=2, column=0, sticky="w", pady=(0, self._pad_y(6)))
        ttk.Label(form, text=f"{_format_value(last_record.mass, 3)} г | {self._format_card_timestamp(last_record.timestamp).replace('Текущее: ', '')}", style="CardText.TLabel").grid(row=2, column=1, sticky="w", pady=(0, self._pad_y(6)))
        ttk.Label(form, text="Источник", style="CardText.TLabel").grid(row=3, column=0, sticky="w", pady=(0, self._pad_y(6)))
        ttk.Label(form, textvariable=picked_source_var, style="CardText.TLabel").grid(row=3, column=1, sticky="w", pady=(0, self._pad_y(6)))
        form.grid_columnconfigure(1, weight=1)

        def use_record(record: MeasurementRecord, source_label: str) -> None:
            if record.mass is None:
                return
            picked_value_var.set(_format_value(record.mass, 3))
            picked_timestamp_var.set(record.timestamp)
            picked_source_var.set(source_label)

        def finish_graph_pick(payload: dict[str, object]) -> None:
            mass_value = payload.get("mass")
            timestamp = payload.get("timestamp")
            if mass_value is None or timestamp is None:
                return
            picked_value_var.set(_format_value(float(mass_value), 3))
            picked_timestamp_var.set(str(timestamp))
            picked_source_var.set("Точка с графика")
            try:
                window.lift()
                window.grab_set()
                window.focus_force()
            except Exception:
                pass

        def start_graph_pick() -> None:
            self.plotter.begin_point_pick(finish_graph_pick)
            picked_source_var.set("Выбор на графике...")
            try:
                window.grab_release()
                self.focus_force()
            except Exception:
                pass

        quick = ttk.Frame(outer, style="Card.TFrame")
        quick.pack(fill="x", pady=(self._pad_y(10), 0))
        for idx in range(3):
            quick.grid_columnconfigure(idx, weight=1)
        ttk.Button(quick, text="Взять первую", style="Soft.TButton", command=lambda: use_record(first_record, "Первая точка")).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(quick, text="Взять текущую", style="Soft.TButton", command=lambda: use_record(last_record, "Текущая точка")).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(quick, text="Режим курсора", style="Soft.TButton", command=start_graph_pick).grid(row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0))

        def apply_sync() -> None:
            try:
                baseline_value = float(str(picked_value_var.get()).replace(",", "."))
            except ValueError:
                messagebox.showwarning("Синхронизация массы", "Введите корректное число массы.", parent=window)
                return
            baseline_timestamp = picked_timestamp_var.get().strip() or first_record.timestamp
            self._apply_measurement_baseline(baseline_value, baseline_timestamp, emit_status=True)
            self.plotter.cancel_interactive_pick()
            window.destroy()

        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", side="bottom", pady=(self._pad_y(14), 0))
        for idx in range(2):
            buttons.grid_columnconfigure(idx, weight=1)
        ttk.Button(buttons, text="Применить", style="Accent.TButton", command=apply_sync).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(buttons, text="Закрыть", style="Soft.TButton", command=lambda: (self.plotter.cancel_interactive_pick(), window.destroy())).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

    def _apply_measurement_baseline(self, baseline_mass: float, baseline_timestamp: str, *, emit_status: bool) -> None:
        self._baseline_mass = float(baseline_mass)
        self._baseline_mass_timestamp = baseline_timestamp
        self._baseline_mass_samples.clear()
        self.mass_card.set_secondary(f"Начальная: {_format_value(self._baseline_mass, 3)} г")
        try:
            self._session_started_at = coerce_timestamp(baseline_timestamp)
            if self._session_started_at is None:
                raise ValueError
        except ValueError:
            self._session_started_at = None
        self.plotter.sync_marker_a_to_timestamp(baseline_timestamp)
        if emit_status:
            self._set_status("Начальная точка синхронизирована.", emit_log=False)

    def _reset_readouts(self) -> None:
        self.last_scale_connected = False
        self.last_furnace_connected = False
        self._last_scale_seen_at = 0.0
        self._last_furnace_seen_at = 0.0
        self._session_started_at = None
        self._baseline_mass = None
        self._baseline_mass_timestamp = None
        self._baseline_mass_samples.clear()
        self.measurement_records.clear()
        self.mass_card.set_value("--", unit="g", subtitle="Ожидание данных")
        self.mass_card.set_secondary("Начальная: --")
        self.temp_card.set_value("--", unit="°C", subtitle="Камера")
        self.thermocouple_card.set_value("--", unit="°C", subtitle="Термопара")
        self.status_card.set_value("Ожидание", subtitle="Нажмите «Старт»")
        self.time_card.set_value("--:--", subtitle="Текущее: --")
        self.diag_last_sample_var.set("Последний сэмпл: --")
        self.diag_last_time_var.set("Время: --")
        self._refresh_diagnostics()
        self._update_action_buttons()

    def _scale_actions_allowed(self) -> bool:
        return self.controller.running and (
            self.config_data.app.test_mode or self.last_scale_connected
        )

    def _update_action_buttons(self) -> None:
        can_start = not self.controller.running and (
            self.config_data.app.test_mode
            or (
                self.config_data.scale.enabled
                and bool(self.scale_port_var.get().strip())
            )
            or (
                self.config_data.furnace.enabled
                and bool(self.furnace_port_var.get().strip())
            )
        )
        has_selection = self._get_selected_tree_device() is not None
        self.start_button.state(["!disabled"] if can_start else ["disabled"])
        self.stop_button.state(
            ["!disabled"] if self.controller.running else ["disabled"]
        )
        self.pause_button.state(
            ["!disabled"] if self.controller.running else ["disabled"]
        )
        self.tare_button.state(
            ["!disabled"] if (self._scale_actions_allowed() and not self._scale_command_in_progress) else ["disabled"]
        )
        self.zero_button.state(
            ["!disabled"] if (self._scale_actions_allowed() and not self._scale_command_in_progress) else ["disabled"]
        )

    def save_runtime_log(self) -> None:
        log_dir = resolve_path(self.config_data.app.log_path).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить журнал",
            defaultextension=".txt",
            filetypes=[("Текстовый файл", "*.txt"), ("Все файлы", "*.*")],
            initialdir=str(log_dir),
            initialfile="datafusion_rt_log.txt",
        )
        if not target:
            return
        content = self.log_text.get("1.0", "end-1c").strip()
        if not content:
            messagebox.showinfo("Журнал", "Журнал пока пуст.", parent=self)
            return
        Path(target).write_text(content + "\n", encoding="utf-8")
        self._set_status(f"Журнал сохранён: {target}")

    def open_logs_folder(self) -> None:
        folder = resolve_path(self.config_data.app.log_path).parent
        folder.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["explorer", str(folder)])
            self._set_status(f"Открыта папка журналов: {folder}", emit_log=False)
        except Exception as exc:
            self._set_status(
                f"Не удалось открыть папку журналов: {exc}", logging.WARNING
            )

    def adjust_font_scale(self, delta: float) -> None:
        self._apply_font_scale_value(
            _clamp(float(self.config_data.app.font_scale) + delta, 0.9, 1.6)
        )

    def reset_font_scale(self) -> None:
        self._apply_font_scale_value(1.0)

    def _apply_font_scale_value(self, value: float) -> None:
        rounded = round(value, 2)
        self.config_data.app.font_scale = rounded
        self.ui_scale = self._compute_ui_scale()
        self._apply_tk_scaling()
        self._apply_theme()
        if (
            hasattr(self, "font_scale_value_label")
            and self.font_scale_value_label.winfo_exists()
        ):
            self.font_scale_value_label.configure(text=f"{int(round(rounded * 100))}%")
        if (
            hasattr(self, "settings_font_scale_value_label")
            and self.settings_font_scale_value_label.winfo_exists()
        ):
            self.settings_font_scale_value_label.configure(
                text=f"{int(round(rounded * 100))}%"
            )
        if bool(self.autosave_settings_var.get()):
            save_config(self.config_data, self.config_path)
        self._set_status(
            f"Размер шрифта: {int(round(rounded * 100))}%.", emit_log=False
        )

    def _log_settings_changes(
        self, before: dict[str, object], after: dict[str, object]
    ) -> None:
        changes: list[str] = []
        for section_name, section_values in after.items():
            previous_section = before.get(section_name, {})
            if not isinstance(section_values, dict) or not isinstance(
                previous_section, dict
            ):
                continue
            for key, value in section_values.items():
                old_value = previous_section.get(key)
                if old_value != value:
                    changes.append(f"{section_name}.{key}: {old_value} -> {value}")
        if changes:
            self.logger.info("Изменены настройки: %s", "; ".join(changes))

    def _set_status(
        self, message: str, level: int = logging.INFO, *, emit_log: bool = True
    ) -> None:
        self.status_var.set(message)
        if emit_log:
            self.logger.log(level, message)

    def _get_bool(self, key: str) -> bool:
        return bool(self.setting_vars[key].get())

    def _get_str(self, key: str) -> str:
        value = str(self.setting_vars[key].get()).strip()
        if not value:
            raise ValueError(f"Поле '{key}' не заполнено.")
        return value

    def _get_int(self, key: str) -> int:
        try:
            return int(float(self._get_str(key)))
        except ValueError as exc:
            raise ValueError(f"Поле '{key}' должно быть целым числом.") from exc

    def _get_float(self, key: str) -> float:
        try:
            return float(self._get_str(key).replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"Поле '{key}' должно быть числом.") from exc

    def _exit_fullscreen(self, _event=None) -> None:
        try:
            if bool(self.attributes("-fullscreen")):
                self.attributes("-fullscreen", False)
        except Exception:
            pass

    def _pad(self, x: int, y: int) -> tuple[int, int]:
        return self._pad_x(x), self._pad_y(y)

    def _pad_x(self, value: int) -> int:
        return max(2, int(value * self.ui_scale))

    def _pad_y(self, value: int) -> int:
        return max(2, int(value * self.ui_scale))

    def _pad_pair(self, value: int) -> tuple[int, int]:
        scaled = self._pad_x(value)
        return scaled, scaled

    def _on_close(self) -> None:
        try:
            if self._autosave_timer_id is not None:
                self.after_cancel(self._autosave_timer_id)
                self._autosave_timer_id = None
            if self.controller.running:
                self.controller.stop()
                self.autosave_session()
            self.controller.close()
            self.plotter.close()
        finally:
            logging.getLogger().removeHandler(self.log_handler)
            self.destroy()


def _format_value(value: float | None, digits: int) -> str:
    if value is None:
        return "--"
    return f"{value:.{digits}f}"


def _blend_color(primary: str, secondary: str, ratio: float) -> str:
    ratio = _clamp(ratio, 0.0, 1.0)

    def parse(color: str) -> tuple[int, int, int]:
        color = color.lstrip("#")
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)

    p_r, p_g, p_b = parse(primary)
    s_r, s_g, s_b = parse(secondary)
    r = int(p_r * ratio + s_r * (1.0 - ratio))
    g = int(p_g * ratio + s_g * (1.0 - ratio))
    b = int(p_b * ratio + s_b * (1.0 - ratio))
    return f"#{r:02X}{g:02X}{b:02X}"
