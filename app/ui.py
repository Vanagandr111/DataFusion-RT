from __future__ import annotations

import ctypes
import dataclasses
import logging
import queue
import subprocess
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from app.config import load_config, resolve_path, save_config
from app.logger_setup import reconfigure_file_logging
from app.models import AcquisitionSnapshot, AppConfig, PortInfo
from app.services.acquisition import AcquisitionController
from app.services.device_probe import probe_furnace_port, probe_scale_port
from app.services.export_service import MeasurementExportService
from app.services.plotter import LivePlotter
from app.theme import ThemeManager, ThemePalette
from app.utils.serial_tools import guess_port_kind, list_available_ports, port_display_label


DEFAULTS: dict[str, object] = {
    "scale.enabled": True,
    "scale.baudrate": 9600,
    "scale.timeout": 1.0,
    "scale.mode": "continuous",
    "scale.request_command": "P\r\n",
    "scale.p1_polling_enabled": False,
    "scale.p1_poll_interval_sec": 0.1,
    "furnace.enabled": True,
    "furnace.baudrate": 9600,
    "furnace.bytesize": 8,
    "furnace.parity": "N",
    "furnace.stopbits": 1,
    "furnace.timeout": 1.0,
    "furnace.slave_id": 1,
    "furnace.register_pv": 0,
    "furnace.register_sv": 1,
    "furnace.scale_factor": 0.1,
    "app.poll_interval_sec": 1.0,
    "app.max_points_on_plot": 500,
    "app.test_mode": False,
    "app.autosave_settings": False,
    "app.enable_file_logging": False,
    "app.start_maximized": False,
    "app.fullscreen": False,
    "app.font_scale": 1.05,
    "app.theme": "dark",
    "app.csv_path": "data/measurements.csv",
    "app.log_path": "logs/app.log",
}


SETTINGS_SECTIONS: list[tuple[str, list[tuple[str, str, str, str, tuple[str, ...] | None]]]] = [
    (
        "Весы",
        [
            ("scale.enabled", "Использовать весы", "bool", "Включает опрос лабораторных весов.", None),
            ("scale.baudrate", "Скорость связи", "entry", "Для ускоренного режима обычно 9600 бод, если весы настроены так же.", None),
            ("scale.timeout", "Таймаут, сек", "entry", "Сколько ждать строку от весов перед повтором.", None),
            ("scale.mode", "Режим чтения", "combo", "Для P2 Con лучше continuous. auto оставляет резервный опрос, если поток пропадёт.", ("auto", "continuous", "poll")),
            ("scale.request_command", "Команда опроса", "entry", "Обычно P\\r\\n. Тара и ноль задаются отдельными кнопками.", None),
            ("scale.p1_polling_enabled", "Режим P1 Prt", "bool", "Принудительно читать весы по команде P в режиме P1 Prt.", None),
            ("scale.p1_poll_interval_sec", "Тайминг опроса P1, сек", "entry", "Интервал между командными опросами в P1 Prt. По умолчанию быстрый.", None),
        ],
    ),
    (
        "Печь",
        [
            ("furnace.enabled", "Использовать печь", "bool", "Включает чтение температуры по Modbus RTU.", None),
            ("furnace.baudrate", "Скорость связи", "entry", "Обычно 9600 бод для USB-RS485 адаптера.", None),
            ("furnace.bytesize", "Биты данных", "combo", "Обычно 8 бит данных.", ("7", "8")),
            ("furnace.parity", "Чётность", "combo", "Обычно N. Уточните в паспорте контроллера.", ("N", "E", "O")),
            ("furnace.stopbits", "Стоп-биты", "combo", "Обычно 1.", ("1", "1.5", "2")),
            ("furnace.timeout", "Таймаут, сек", "entry", "Сколько ждать ответ по Modbus RTU.", None),
            ("furnace.slave_id", "Адрес устройства", "entry", "Modbus slave ID контроллера.", None),
            ("furnace.register_pv", "Регистр текущей температуры", "entry", "PV: регистр фактической температуры.", None),
            ("furnace.register_sv", "Регистр заданной температуры", "entry", "SV: регистр уставки температуры.", None),
            ("furnace.scale_factor", "Масштаб температуры", "entry", "Например 0.1, если 253 означает 25.3 °C.", None),
        ],
    ),
    (
        "Приложение",
        [
            ("app.poll_interval_sec", "Интервал опроса, сек", "entry", "Как часто обновлять измерения и график.", None),
            ("app.max_points_on_plot", "Точек на графике", "entry", "Сколько последних точек держать на экране.", None),
            ("app.test_mode", "Тестовый режим", "bool", "Генерирует данные без реального оборудования.", None),
        ],
    ),
    (
        "Интерфейс и файлы",
        [
            ("app.theme", "Тема оформления", "combo", "Оформление окна программы.", ("dark", "light")),
            ("app.start_maximized", "Старт развернутым", "bool", "Рекомендуется включить для лабораторного ПК.", None),
            ("app.fullscreen", "Полный экран", "bool", "Если включено, окно откроется на весь экран.", None),
            ("app.csv_path", "Файл CSV", "entry", "Основной файл накопления измерений.", None),
            ("app.log_path", "Файл журнала", "entry", "Файл служебного журнала программы.", None),
            ("app.enable_file_logging", "Включить автологирование", "bool", "Записывать служебный журнал в файл на диске.", None),
        ],
    ),
]


TOOLTIP_DETAILS: dict[str, str] = {
    "scale.enabled": "Отключите этот параметр, если весы физически не подключены, чтобы программа не тратила время на лишние попытки чтения.",
    "scale.baudrate": "Если на весах уже включены P2 Con и For2, удобно использовать 9600 бод. Главное, чтобы скорость в программе совпадала с настройкой самих весов.",
    "scale.timeout": "Слишком маленький таймаут даст ложные ошибки, слишком большой замедлит опрос. Для начала обычно хватает 0.8-1.5 секунды.",
    "scale.mode": "Режим auto обычно самый удобный: программа сначала ждёт поток, а если поток не идёт, отправляет команду опроса сама.",
    "scale.request_command": "Меняйте это поле только если на реальном стенде выяснится, что весы требуют другую ASCII-команду. Обычно достаточно P\\r\\n.",
    "scale.p1_polling_enabled": "Включайте только если на самих весах выставлен режим P1 Prt. Тогда программа будет опрашивать их по команде, а не ждать непрерывный поток.",
    "scale.p1_poll_interval_sec": "Если хотите максимально частый опрос в P1 Prt, ставьте маленький интервал вроде 0.1-0.2 секунды. Слишком маленькое значение может только зря грузить COM-порт.",
    "furnace.enabled": "Если печь пока не подключена, можно временно снять галочку. Тогда программа продолжит работать только с весами.",
    "furnace.baudrate": "Если есть связь по USB-RS485, но нет ответа Modbus, проверьте baudrate одним из первых параметров вместе с parity и slave ID.",
    "furnace.bytesize": "У большинства контроллеров используется 8 бит данных. Меняйте только если это явно указано в документации контроллера.",
    "furnace.parity": "Для Modbus RTU часто критично совпадение parity. Если N не работает, проверьте в документации варианты E или O.",
    "furnace.stopbits": "Обычно используется 1 стоп-бит. Несовпадение этого параметра тоже может полностью ломать обмен по Modbus.",
    "furnace.timeout": "Если контроллер отвечает медленно, попробуйте увеличить таймаут до 1.5-2.0 секунды, чтобы исключить ложные таймауты.",
    "furnace.slave_id": "Это адрес Modbus-устройства. Если контроллер на шине не один, у каждого устройства должен быть свой адрес.",
    "furnace.register_pv": "PV — это фактическая температура. Если значение выглядит странно или пусто, проверьте адрес регистра в паспорте контроллера.",
    "furnace.register_sv": "SV — это заданная температура. Этот параметр читается только для отображения и не изменяет настройки печи.",
    "furnace.scale_factor": "Если регистр возвращает, например, 253 вместо 25.3 °C, задайте коэффициент 0.1. Если приходит 2530, может понадобиться 0.01.",
    "app.poll_interval_sec": "Для большинства задач 1 секунда удобно и достаточно. Уменьшайте интервал только если действительно нужна более частая запись.",
    "app.max_points_on_plot": "Чем больше точек, тем длиннее история на экране, но тем тяжелее перерисовка графика на слабом ноутбуке.",
    "app.test_mode": "Полезно для проверки интерфейса дома без оборудования: программа будет сама генерировать массу и температуру.",
    "app.autosave_settings": "Когда автосохранение включено, корректные изменения применяются и записываются в config.yaml сразу. Если поле введено не полностью, сохранение подождёт валидного значения.",
    "app.enable_file_logging": "Когда эта галочка выключена, программа пишет сообщения только в окно журнала и в консоль. Файл на диске не создаётся.",
    "app.theme": "Переключайте тему под освещение лаборатории. Светлая удобнее для печати скриншотов, тёмная часто комфортнее при длительной работе.",
    "app.start_maximized": "Рекомендуется оставить включённым для лабораторного ПК, чтобы все крупные элементы были сразу хорошо видны.",
    "app.fullscreen": "Используйте только если хотите режим без рамок окна. Для обычной работы чаще удобнее просто развёрнутое окно.",
    "app.csv_path": "Это основной файл накопления измерений. Убедитесь, что папка доступна на запись и не находится в защищённом системном каталоге.",
    "app.log_path": "Если программа ведёт себя нестабильно, этот файл помогает понять причину. Логи удобно прикладывать при разборе ошибок.",
    "app.font_scale": "Масштаб шрифта интерфейса. Увеличивайте, если на экране много мелкого текста. После резкого увеличения шрифта лучше перезапустить программу.",
}


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
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        SPI_GETWORKAREA = 48
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
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


class ToolTip:
    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip_window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None) -> None:
        if self.tip_window or not self.text:
            return
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


class MetricCard(tk.Frame):
    def __init__(self, master, title: str, accent_role: str, *, value_size: int, unit_size: int = 22) -> None:
        super().__init__(master, bd=0)
        self.title_text = title
        self.accent_role = accent_role
        self.value_size = value_size
        self.unit_size = unit_size
        self.default_border = "#243140"

        self.top_bar = tk.Frame(self, height=4, bd=0)
        self.top_bar.pack(fill="x", side="top")

        self.body = tk.Frame(self, bd=0)
        self.body.pack(fill="both", expand=True)

        self.title_label = tk.Label(self.body, anchor="w")
        self.title_label.pack(fill="x")

        self.value_row = tk.Frame(self.body, bd=0)
        self.value_row.pack(fill="x", pady=(10, 6))
        self.value_label = tk.Label(self.value_row, text="--", anchor="w")
        self.value_label.pack(side="left")
        self.unit_label = tk.Label(self.value_row, text="", anchor="sw")
        self.unit_label.pack(side="left", padx=(8, 0), pady=(0, 5))

        self.subtitle_label = tk.Label(self.body, anchor="w", justify="left")
        self.subtitle_label.pack(fill="x")

    def apply_theme(self, palette: ThemePalette, scale: float) -> None:
        accent = getattr(palette, self.accent_role, palette.accent)
        self.default_border = palette.border
        self.configure(bg=palette.card_bg, highlightthickness=1, highlightbackground=palette.border)
        self.top_bar.configure(bg=accent)
        self.body.configure(bg=palette.card_bg, padx=max(16, int(18 * scale)), pady=max(14, int(16 * scale)))
        self.value_row.configure(bg=palette.card_bg)
        self.title_label.configure(bg=palette.card_bg, fg=palette.subtext, text=self.title_text, font=("Segoe UI Semibold", max(12, int(14 * scale))))
        self.value_label.configure(bg=palette.card_bg, fg=palette.text, font=("Bahnschrift SemiBold", max(24, int(self.value_size * scale))))
        self.unit_label.configure(bg=palette.card_bg, fg=palette.subtext, font=("Segoe UI Semibold", max(12, int(self.unit_size * scale))))
        self.subtitle_label.configure(bg=palette.card_bg, fg=palette.subtext, font=("Segoe UI", max(10, int(11 * scale))))

    def set_value(self, value: str, *, unit: str = "", subtitle: str = "") -> None:
        self.value_label.configure(text=value)
        self.unit_label.configure(text=unit)
        self.subtitle_label.configure(text=subtitle)

    def pulse(self, color: str) -> None:
        self.configure(highlightbackground=color)
        self.after(260, lambda: self.configure(highlightbackground=self.default_border))


class LabForgeApp(tk.Tk):
    def __init__(self, config: AppConfig, config_path: Path, logger: logging.Logger) -> None:
        _enable_windows_dpi_awareness()
        super().__init__()

        self.config_data = config
        self.config_path = config_path
        self.logger = logger
        self.controller = AcquisitionController(config, logger=logger.getChild("acquisition"))
        self.export_service = MeasurementExportService(logger=logger.getChild("export"))
        self.log_handler = UILogHandler()
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
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
        self.furnace_port_display_var = tk.StringVar(value=self.config_data.furnace.port)
        self.assignment_var = tk.StringVar(value="Выберите COM-порт слева.")
        self.port_status_var = tk.StringVar(value="Нажмите «Найти COM-порты».")
        self.device_check_var = tk.StringVar(value="Проверка устройств ещё не выполнялась.")
        self.diag_status_var = tk.StringVar(value="Ошибок нет.")
        self.diag_ports_var = tk.StringVar(value="")
        self.diag_last_sample_var = tk.StringVar(value="Последний сэмпл: --")
        self.diag_last_time_var = tk.StringVar(value="Время: --")
        self.autosave_settings_var = tk.BooleanVar(value=self.config_data.app.autosave_settings)
        self.settings_mode_var = tk.StringVar(value="Ручное сохранение")
        self.settings_mode_hint_var = tk.StringVar(value="Изменения записываются только после нажатия кнопки «Сохранить».")
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
        self._reset_readouts()
        self._update_side_panels()
        self._animate_indicators()
        self._poll_runtime_queues()

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
                self.logger.warning("Fullscreen mode is not supported on this system.", exc_info=True)

        if self.config_data.app.start_maximized:
            try:
                self.state("zoomed")
                return
            except Exception:
                self.logger.debug("Zoomed window state is not available.", exc_info=True)
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
            "app.poll_interval_sec": self.config_data.app.poll_interval_sec,
            "app.max_points_on_plot": self.config_data.app.max_points_on_plot,
            "app.test_mode": self.config_data.app.test_mode,
            "app.start_maximized": self.config_data.app.start_maximized,
            "app.fullscreen": self.config_data.app.fullscreen,
            "app.theme": self.config_data.app.theme,
            "app.csv_path": self.config_data.app.csv_path,
            "app.log_path": self.config_data.app.log_path,
            "app.enable_file_logging": self.config_data.app.enable_file_logging,
        }
        for key, value in values.items():
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

        self.toolbar = ttk.Frame(self, style="Header.TFrame", padding=self._pad(16, 12))
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
        self.help_button = tk.Menubutton(right_tools, text="Помощь", relief="flat", bd=0, direction="below")
        self.help_button.grid(row=0, column=2, padx=(0, self._pad_x(8)))
        help_menu = tk.Menu(self.help_button, tearoff=False)
        help_menu.add_command(label="Инструкция", command=self.show_help_dialog)
        help_menu.add_command(label="Об авторе", command=self.show_about_dialog)
        self.help_button.configure(menu=help_menu)
        self.log_menu_button = tk.Menubutton(right_tools, text="Лог", relief="flat", bd=0, direction="below")
        self.log_menu_button.grid(row=0, column=3, padx=(0, self._pad_x(8)))
        log_menu = tk.Menu(self.log_menu_button, tearoff=False)
        log_menu.add_command(label="Показать журнал", command=self.toggle_right_panel)
        log_menu.add_command(label="Сохранить журнал в TXT", command=self.save_runtime_log)
        log_menu.add_command(label="Открыть папку журналов", command=self.open_logs_folder)
        self.log_menu_button.configure(menu=log_menu)
        self.settings_button = ttk.Button(right_tools, text="Настройки", style="Soft.TButton", command=self.open_settings_window)
        self.settings_button.grid(row=0, column=4)

        self.header_status = tk.Label(self.toolbar, textvariable=self.status_var, anchor="w", justify="left")
        self.header_status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(self._pad_y(6), 0))

        self.body = ttk.Frame(self, style="App.TFrame", padding=self._pad(14, 14))
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(1, weight=1)

        self.left_panel = ttk.Frame(self.body, style="Card.TFrame", padding=self._pad(14, 14))
        self.left_panel.grid_rowconfigure(2, weight=1)
        self.left_panel.grid_columnconfigure(0, weight=1)
        self._build_left_panel()

        self.center_panel = ttk.Frame(self.body, style="App.TFrame")
        self.center_panel.grid(row=0, column=1, sticky="nsew")
        self.center_panel.grid_rowconfigure(0, weight=6, minsize=int(420 * self.ui_scale))
        self.center_panel.grid_rowconfigure(1, weight=2, minsize=int(170 * self.ui_scale))
        self.center_panel.grid_rowconfigure(2, weight=0)
        self.center_panel.grid_columnconfigure(0, weight=1)
        self._build_center_panel()

        self.right_panel = ttk.Frame(self.body, style="Card.TFrame", padding=self._pad(14, 14))
        self.right_panel.grid_rowconfigure(6, weight=1)
        self.right_panel.grid_columnconfigure(0, weight=1)
        self._build_right_panel()

    def _build_top_menus(self, parent) -> None:
        self.option_add("*Menu.Font", f"{{Segoe UI}} {max(11, int(12 * self.ui_scale))}")
        self.option_add("*TCombobox*Listbox.Font", f"{{Segoe UI}} {max(11, int(12 * self.ui_scale))}")
        self.file_menu_button = self._make_menu_button(parent, "Программа")
        file_menu = tk.Menu(self.file_menu_button, tearoff=False)
        file_menu.add_command(label="Экспорт CSV", command=lambda: self.export_measurements(default_ext=".csv"))
        file_menu.add_command(label="Экспорт Excel", command=lambda: self.export_measurements(default_ext=".xlsx"))
        file_menu.add_command(label="Изображение", command=self.save_plot_image)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self._on_close)
        self.file_menu_button.configure(menu=file_menu)

        self.theme_switch_button = ttk.Button(parent, text="", style="Soft.TButton", command=self.toggle_theme)
        self.theme_switch_button.pack(side="left", padx=(0, self._pad_x(8)))
        self.font_toolbar = ttk.Frame(parent, style="Header.TFrame")
        self.font_toolbar.pack(side="left", padx=(0, self._pad_x(8)))
        ttk.Label(self.font_toolbar, text="Размер шрифта", style="Subtitle.TLabel").grid(row=0, column=0, padx=(0, self._pad_x(6)))
        ttk.Button(self.font_toolbar, text="-", style="Soft.TButton", command=lambda: self.adjust_font_scale(-0.05), width=3).grid(row=0, column=1, padx=(0, self._pad_x(4)))
        self.font_scale_value_label = ttk.Label(self.font_toolbar, text="", style="Subtitle.TLabel", anchor="center")
        self.font_scale_value_label.grid(row=0, column=2, padx=(0, self._pad_x(4)))
        ttk.Button(self.font_toolbar, text="+", style="Soft.TButton", command=lambda: self.adjust_font_scale(0.05), width=3).grid(row=0, column=3, padx=(0, self._pad_x(4)))
        ttk.Button(self.font_toolbar, text="Сброс", style="Soft.TButton", command=self.reset_font_scale).grid(row=0, column=4)

    def _make_menu_button(self, parent, text: str) -> tk.Menubutton:
        button = tk.Menubutton(parent, text=text, relief="flat", bd=0, direction="below")
        button.pack(side="left", padx=(0, self._pad_x(8)))
        return button

    def _build_status_indicator(self, parent, title: str) -> dict[str, object]:
        frame = ttk.Frame(parent, style="Header.TFrame")
        canvas = tk.Canvas(frame, width=self._pad_x(18), height=self._pad_x(18), highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, padx=(0, self._pad_x(6)))
        outer = canvas.create_oval(1, 1, self._pad_x(17), self._pad_x(17), outline="", fill="#2B3644")
        inner = canvas.create_oval(4, 4, self._pad_x(14), self._pad_x(14), outline="", fill="#536273")
        label = tk.Label(frame, text=title, anchor="w")
        label.grid(row=0, column=1, sticky="w")
        return {"frame": frame, "canvas": canvas, "outer": outer, "inner": inner, "label": label}

    def _build_left_panel(self) -> None:
        ttk.Label(self.left_panel, text="Подключение устройств", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.left_panel, textvariable=self.port_status_var, style="CardText.TLabel").grid(row=1, column=0, sticky="w", pady=(self._pad_y(4), self._pad_y(10)))

        tree_frame = ttk.Frame(self.left_panel, style="Card.TFrame")
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self.port_tree = ttk.Treeview(tree_frame, columns=("port", "kind", "desc"), show="headings", height=10)
        self.port_tree.heading("port", text="Порт")
        self.port_tree.heading("kind", text="Тип")
        self.port_tree.heading("desc", text="Устройство")
        self.port_tree.column("port", width=int(90 * self.ui_scale), anchor="w")
        self.port_tree.column("kind", width=int(150 * self.ui_scale), anchor="w")
        self.port_tree.column("desc", width=int(250 * self.ui_scale), anchor="w")
        self.port_tree.grid(row=0, column=0, sticky="nsew")
        self.port_tree.bind("<<TreeviewSelect>>", self._on_port_selected)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.port_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.port_tree.configure(yscrollcommand=scrollbar.set)

        self.assignment_label = ttk.Label(self.left_panel, textvariable=self.assignment_var, style="CardText.TLabel", wraplength=int(420 * self.ui_scale))
        self.assignment_label.grid(row=3, column=0, sticky="w", pady=(self._pad_y(10), 0))

        ports_bar = ttk.Frame(self.left_panel, style="Card.TFrame")
        ports_bar.grid(row=4, column=0, sticky="ew", pady=(self._pad_y(12), 0))
        for idx in range(2):
            ports_bar.grid_columnconfigure(idx, weight=1)
        ttk.Button(ports_bar, text="Назначить как Весы", style="Soft.TButton", command=self.assign_selected_to_scale).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(ports_bar, text="Назначить как Печь", style="Soft.TButton", command=self.assign_selected_to_furnace).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

        probe_bar = ttk.Frame(self.left_panel, style="Card.TFrame")
        probe_bar.grid(row=5, column=0, sticky="ew", pady=(self._pad_y(10), 0))
        for idx in range(3):
            probe_bar.grid_columnconfigure(idx, weight=1)
        ttk.Button(probe_bar, text="Найти", style="Soft.TButton", command=self.refresh_ports).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(probe_bar, text="Проверить весы", style="Soft.TButton", command=self.probe_scale_device).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(probe_bar, text="Проверить печь", style="Soft.TButton", command=self.probe_furnace_device).grid(row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0))

    def _build_center_panel(self) -> None:
        plot_card = ttk.Frame(self.center_panel, style="Card.TFrame", padding=self._pad(12, 12))
        plot_card.grid(row=0, column=0, sticky="nsew")
        plot_card.grid_rowconfigure(1, weight=1)
        plot_card.grid_columnconfigure(0, weight=1)
        plot_card.grid_columnconfigure(1, weight=0, minsize=int(104 * self.ui_scale))
        plot_card.grid_columnconfigure(2, weight=0, minsize=int(124 * self.ui_scale))

        plot_header = ttk.Frame(plot_card, style="Card.TFrame")
        plot_header.grid(row=0, column=0, columnspan=3, sticky="ew")
        plot_header.grid_columnconfigure(0, weight=1)
        ttk.Label(plot_header, text="График измерений", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")

        self.plotter = LivePlotter(
            plot_card,
            max_points=int(self.config_data.app.max_points_on_plot),
            plot_theme=self.theme_manager.palette.plot,
            scale=self.ui_scale,
            logger=self.logger.getChild("plotter"),
        )
        self.plotter.get_widget().grid(row=1, column=0, sticky="nsew", pady=(self._pad_y(10), 0))
        tools_panel, tools_body = self._create_plot_button_panel(
            plot_card,
            title="Инструменты",
            column=1,
            width=int(110 * self.ui_scale),
            padx=(self._pad_x(10), self._pad_x(6)),
        )
        self.plot_zoom_button = self._make_plot_tool_button(tools_body, "🔍", self.toggle_plot_zoom, width=10)
        self.plot_zoom_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_pan_button = self._make_plot_tool_button(tools_body, "Сдвиг", self.toggle_plot_pan, width=10)
        self.plot_pan_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_plus_button = self._make_plot_tool_button(tools_body, "+", self.zoom_in_plot, width=10)
        self.plot_plus_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_minus_button = self._make_plot_tool_button(tools_body, "-", self.zoom_out_plot, width=10)
        self.plot_minus_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_reset_button = self._make_plot_tool_button(tools_body, "Сброс", self.reset_plot_view, width=10)
        self.plot_reset_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_auto_button = self._make_plot_tool_button(tools_body, "Авто", self.autoscale_plot, width=10)
        self.plot_auto_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_points_button = self._make_plot_tool_button(tools_body, "Точки", self.set_plot_points, width=10)
        self.plot_points_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_lines_button = self._make_plot_tool_button(tools_body, "Линии", self.set_plot_lines, width=10)
        self.plot_lines_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_smooth_button = self._make_plot_tool_button(tools_body, "Сглаж.", self.set_plot_smooth, width=10)
        self.plot_smooth_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_save_button = self._make_plot_tool_button(tools_body, "PNG", self.save_plot_image, width=10)
        self.plot_save_button.pack(fill="x")

        views_panel, views_body = self._create_plot_button_panel(
            plot_card,
            title="Виды",
            column=2,
            width=int(124 * self.ui_scale),
            padx=(self._pad_x(6), 0),
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
        self._update_plot_mode_buttons()
        self._update_plot_render_buttons()

        cards = ttk.Frame(self.center_panel, style="App.TFrame")
        cards.grid(row=1, column=0, sticky="nsew", pady=(self._pad_y(14), 0))
        for idx in range(4):
            cards.grid_columnconfigure(idx, weight=1)
        self.mass_card = MetricCard(cards, "Масса", "accent", value_size=48)
        self.mass_card.grid(row=0, column=0, sticky="nsew", padx=(0, self._pad_x(8)))
        self.temp_card = MetricCard(cards, "Температура", "heat", value_size=48)
        self.temp_card.grid(row=0, column=1, sticky="nsew", padx=self._pad_pair(4))
        self.status_card = MetricCard(cards, "Статус", "success", value_size=30, unit_size=1)
        self.status_card.grid(row=0, column=2, sticky="nsew", padx=self._pad_pair(4))
        self.time_card = MetricCard(cards, "Время", "border", value_size=20, unit_size=1)
        self.time_card.grid(row=0, column=3, sticky="nsew", padx=(self._pad_x(8), 0))

        actions = ttk.Frame(self.center_panel, style="App.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(self._pad_y(14), 0))
        for idx in range(6):
            actions.grid_columnconfigure(idx, weight=1)
        self.start_button = ttk.Button(actions, text="Старт", style="Accent.TButton", command=self.start_acquisition)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        self.stop_button = ttk.Button(actions, text="Стоп", style="Warm.TButton", command=self.stop_acquisition)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        self.reset_button = ttk.Button(actions, text="Сброс", style="Soft.TButton", command=self.clear_graph)
        self.reset_button.grid(row=0, column=2, sticky="ew", padx=self._pad_pair(3))
        self.export_button = ttk.Button(actions, text="Экспорт", style="Soft.TButton", command=self.export_with_default_format)
        self.export_button.grid(row=0, column=3, sticky="ew", padx=self._pad_pair(3))
        self.tare_button = ttk.Button(actions, text="Тара", style="Soft.TButton", command=self.tare_scale)
        self.tare_button.grid(row=0, column=4, sticky="ew", padx=self._pad_pair(3))
        self.zero_button = ttk.Button(actions, text="Ноль", style="Soft.TButton", command=self.zero_scale)
        self.zero_button.grid(row=0, column=5, sticky="ew", padx=(self._pad_x(6), 0))

    def _build_right_panel(self) -> None:
        self.right_panel.grid_propagate(False)
        ttk.Label(self.right_panel, text="Лог и диагностика", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.right_panel, textvariable=self.diag_ports_var, style="CardText.TLabel", wraplength=int(360 * self.ui_scale)).grid(row=1, column=0, sticky="w", pady=(self._pad_y(6), 0))
        ttk.Label(self.right_panel, textvariable=self.diag_last_sample_var, style="CardText.TLabel", wraplength=int(360 * self.ui_scale)).grid(row=2, column=0, sticky="w", pady=(self._pad_y(6), 0))
        ttk.Label(self.right_panel, textvariable=self.diag_last_time_var, style="CardText.TLabel").grid(row=3, column=0, sticky="w", pady=(self._pad_y(6), 0))
        ttk.Label(self.right_panel, textvariable=self.diag_status_var, style="CardText.TLabel", wraplength=int(360 * self.ui_scale)).grid(row=4, column=0, sticky="w", pady=(self._pad_y(6), self._pad_y(10)))

        log_actions = ttk.Frame(self.right_panel, style="Card.TFrame")
        log_actions.grid(row=5, column=0, sticky="ew", pady=(0, self._pad_y(8)))
        log_actions.grid_columnconfigure(0, weight=1)
        log_actions.grid_columnconfigure(1, weight=1)
        ttk.Button(log_actions, text="Сохранить TXT", style="Soft.TButton", command=self.save_runtime_log).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(log_actions, text="Папка журналов", style="Soft.TButton", command=self.open_logs_folder).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

        self.log_text = ScrolledText(self.right_panel, wrap="word", relief="flat", height=18)
        self.log_text.grid(row=6, column=0, sticky="nsew")
        self.log_text.insert("end", "Журнал готов.\n")
        self.log_text.configure(state="disabled")

    def open_settings_window(self) -> None:
        if hasattr(self, "_settings_window") and self._settings_window.winfo_exists():
            self._settings_window.focus_set()
            return

        self._restore_settings_from_disk()

        self._settings_window = tk.Toplevel(self)
        self._settings_window.title("Настройки")
        self._settings_window.transient(self)
        self._settings_window.grab_set()
        self._settings_window.protocol("WM_DELETE_WINDOW", self._close_settings_window)
        area = _windows_work_area()
        if area is not None:
            x, y, width, height = area
            min_width = min(max(int(960 * self.ui_scale), 900), max(width - 24, 640))
            min_height = min(max(int(680 * self.ui_scale), 620), max(height - 24, 480))
            win_width = max(min_width, width - 16)
            win_height = max(min_height, height - 16)
            self._settings_window.minsize(min_width, min_height)
            self._settings_window.maxsize(width, height)
            self._settings_window.geometry(f"{win_width}x{win_height}+{x + 8}+{y + 8}")
        else:
            self._settings_window.minsize(int(960 * self.ui_scale), int(680 * self.ui_scale))
            try:
                self._settings_window.state("zoomed")
            except Exception:
                width = int(self.winfo_screenwidth() * 0.92)
                height = int(self.winfo_screenheight() * 0.9)
                self._settings_window.geometry(f"{width}x{height}+30+30")

        outer = ttk.Frame(self._settings_window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        canvas.configure(bg=self.theme_manager.palette.app_bg)
        self._settings_canvas = canvas
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)

        inner = ttk.Frame(canvas, style="App.TFrame")
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        self._bind_mousewheel_to_canvas(canvas)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_columnconfigure(1, weight=1)

        hero = ttk.Frame(inner, style="Card.TFrame", padding=self._pad(12, 8))
        hero.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, self._pad_y(10)))
        hero.grid_columnconfigure(0, weight=1)
        hero.grid_columnconfigure(1, weight=0)
        self.settings_title_label = ttk.Label(hero, text="⚙ Параметры подключения и оформления", style="CardTitle.TLabel")
        self.settings_title_label.configure(font=("Segoe UI Semibold", max(15, int(17 * self.ui_scale))))
        self.settings_title_label.grid(row=0, column=0, sticky="w")
        self.settings_intro_label = ttk.Label(
            hero,
            text="Здесь можно выбрать порты, проверить устройства, настроить связь и внешний вид программы.",
            style="CardText.TLabel",
            wraplength=int(900 * self.ui_scale),
        )
        self.settings_intro_label.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
        self.settings_intro_label.grid(row=1, column=0, sticky="w", pady=(0, 0))
        hero_controls = ttk.Frame(hero, style="Card.TFrame")
        hero_controls.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(self._pad_x(10), 0))
        for idx in range(3):
            hero_controls.grid_columnconfigure(idx, weight=1)
        self.settings_reset_button = ttk.Button(hero_controls, text="Сброс по умолчанию", style="Soft.TButton", command=self.reset_default_settings, width=18)
        self.settings_reset_button.grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        self.settings_save_button = ttk.Button(hero_controls, text="Сохранить", style="Accent.TButton", command=self.save_settings, width=18)
        self.settings_save_button.grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        self.autosave_checkbox = ttk.Checkbutton(
            hero_controls,
            text="Автосохранение",
            variable=self.autosave_settings_var,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        self.autosave_checkbox.grid(row=1, column=1, sticky="w", pady=(self._pad_y(8), 0), padx=(0, self._pad_x(10)))
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
        self.settings_exit_button = ttk.Button(hero_controls, text="Выход", style="Soft.TButton", command=self._close_settings_window, width=18)
        self.settings_exit_button.grid(row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0))
        self.settings_mode_badge.grid(row=1, column=2, sticky="ew", pady=(self._pad_y(8), 0))
        self.settings_mode_hint = ttk.Label(
            hero_controls,
            textvariable=self.settings_mode_hint_var,
            style="CardText.TLabel",
            wraplength=int(620 * self.ui_scale),
            justify="left",
        )
        self.settings_mode_hint.configure(font=("Segoe UI", max(10, int(11 * self.ui_scale))))
        self.settings_mode_hint.grid(row=3, column=1, columnspan=2, sticky="w", pady=(self._pad_y(4), 0))
        devices_frame = ttk.LabelFrame(inner, text="Устройства", style="Section.TLabelframe", padding=self._pad(12, 10))
        devices_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, self._pad_y(10)))
        devices_frame.grid_columnconfigure(1, weight=1)
        devices_frame.grid_columnconfigure(2, weight=1)

        ttk.Label(devices_frame, text="Порт весов", style="CardText.TLabel").grid(row=0, column=0, sticky="w", padx=(0, self._pad_x(12)), pady=(self._pad_y(6), self._pad_y(6)))
        self.settings_scale_combo = ttk.Combobox(devices_frame, textvariable=self.scale_port_display_var, width=96)
        self.settings_scale_combo.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
        self.settings_scale_combo.grid(row=0, column=1, sticky="ew", pady=(self._pad_y(6), self._pad_y(6)))
        ttk.Label(
            devices_frame,
            text="Формат списка: COM-порт - имя интерфейса в системе - тип устройства. Можно ввести вручную.",
            style="CardText.TLabel",
            wraplength=int(330 * self.ui_scale),
        ).grid(row=0, column=2, sticky="w", padx=(self._pad_x(12), 0))

        ttk.Label(devices_frame, text="Порт печи", style="CardText.TLabel").grid(row=1, column=0, sticky="w", padx=(0, self._pad_x(12)), pady=(self._pad_y(6), self._pad_y(6)))
        self.settings_furnace_combo = ttk.Combobox(devices_frame, textvariable=self.furnace_port_display_var, width=96)
        self.settings_furnace_combo.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
        self.settings_furnace_combo.grid(row=1, column=1, sticky="ew", pady=(self._pad_y(6), self._pad_y(6)))
        ttk.Label(
            devices_frame,
            text="Для печи обычно выбирается USB-RS485 адаптер. Проверку лучше делать после уточнения slave ID и регистров.",
            style="CardText.TLabel",
            wraplength=int(330 * self.ui_scale),
        ).grid(row=1, column=2, sticky="w", padx=(self._pad_x(12), 0))

        device_buttons = ttk.Frame(devices_frame, style="Card.TFrame")
        device_buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(8), 0))
        for idx in range(4):
            device_buttons.grid_columnconfigure(idx, weight=1)
        ttk.Button(device_buttons, text="Обновить порты", style="Soft.TButton", command=self.refresh_ports).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(device_buttons, text="Проверить весы", style="Soft.TButton", command=self.probe_scale_device).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(device_buttons, text="Проверить печь", style="Soft.TButton", command=self.probe_furnace_device).grid(row=0, column=2, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(device_buttons, text="Применить порты", style="Accent.TButton", command=self.save_settings).grid(row=0, column=3, sticky="ew", padx=(self._pad_x(6), 0))
        ttk.Label(devices_frame, textvariable=self.device_check_var, style="CardText.TLabel", wraplength=int(640 * self.ui_scale)).grid(
            row=3,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(self._pad_y(10), 0),
        )

        self._sync_port_display_vars()
        values = [self._settings_port_label(port) for port in self.available_ports]
        self.settings_scale_combo.configure(values=values)
        self.settings_furnace_combo.configure(values=values)

        section_positions = {
            "Весы": (2, 0),
            "Печь": (2, 1),
            "Приложение": (3, 0),
            "Интерфейс и файлы": (3, 1),
        }

        for section_title, fields in SETTINGS_SECTIONS:
            row, column = section_positions.get(section_title, (3, 1))
            frame = ttk.LabelFrame(inner, text=section_title, style="Section.TLabelframe", padding=self._pad(12, 10))
            frame.grid(row=row, column=column, sticky="nsew", pady=(0, self._pad_y(10)), padx=(0, self._pad_x(8)) if column == 0 else (self._pad_x(8), 0))
            frame.grid_columnconfigure(1, weight=1)
            frame.grid_columnconfigure(2, weight=1)
            self._build_settings_section(frame, fields)

        self._apply_theme_to_toplevel(self._settings_window)
        self._update_settings_control_states()
        self._settings_window.update_idletasks()
        self._settings_window.focus_set()

    def _build_settings_section(self, parent, fields) -> None:
        parent.grid_columnconfigure(0, weight=0, minsize=int(210 * self.ui_scale))
        parent.grid_columnconfigure(1, weight=1, minsize=int(210 * self.ui_scale))
        parent.grid_columnconfigure(2, weight=1, minsize=int(260 * self.ui_scale))
        for row, (key, label, kind, tooltip, choices) in enumerate(fields):
            tooltip_text = self._build_setting_tooltip(key, tooltip)
            label_widget = ttk.Label(parent, text=label, style="CardText.TLabel")
            label_widget.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
            label_widget.grid(row=row, column=0, sticky="w", padx=(0, self._pad_x(10)), pady=(self._pad_y(4), self._pad_y(4)))
            ToolTip(label_widget, tooltip_text)

            var = self.setting_vars[key]
            if kind == "bool":
                widget = ttk.Checkbutton(parent, variable=var, style="Card.TCheckbutton", takefocus=False)
                widget.configure(text=" ")
            elif kind == "combo":
                widget = ttk.Combobox(parent, textvariable=var, state="readonly", values=list(choices or ()))
            else:
                widget = ttk.Entry(parent, textvariable=var)
            if kind in {"combo", "entry"}:
                widget.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
            if kind == "bool":
                widget.configure(style="Card.TCheckbutton")
            widget.grid(row=row, column=1, sticky="ew", pady=(self._pad_y(4), self._pad_y(4)))
            hint_label = ttk.Label(parent, text=f"{tooltip} По умолчанию: {DEFAULTS[key]}", style="CardAltText.TLabel", wraplength=int(420 * self.ui_scale))
            hint_label.configure(font=("Segoe UI", max(11, int(12 * self.ui_scale))))
            hint_label.grid(
                row=row,
                column=2,
                sticky="w",
                padx=(self._pad_x(10), 0),
                pady=(self._pad_y(4), self._pad_y(4)),
            )
            ToolTip(widget, tooltip_text)

    def refresh_ports(self) -> None:
        self.available_ports = list_available_ports()
        self.port_map = {port.device.upper(): port for port in self.available_ports}
        self.port_display_map = {port.device.upper(): self._settings_port_label(port) for port in self.available_ports}

        for item_id in self.port_tree.get_children():
            self.port_tree.delete(item_id)

        for port in self.available_ports:
            self.port_tree.insert("", "end", iid=port.device, values=(port.device, guess_port_kind(port), port.description))

        if self.available_ports:
            self.port_status_var.set(f"Найдено COM-портов: {len(self.available_ports)}")
            self._set_status("Список COM-портов обновлён.")
        else:
            self.port_status_var.set("COM-порты не найдены.")
            self._set_status("COM-порты не обнаружены.", logging.WARNING)

        if hasattr(self, "settings_scale_combo") and self.settings_scale_combo.winfo_exists():
            values = [self._settings_port_label(port) for port in self.available_ports]
            self.settings_scale_combo.configure(values=values)
            self.settings_furnace_combo.configure(values=values)
            self._sync_port_display_vars()

        self._refresh_diagnostics()
        self._update_action_buttons()

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
            test_mode=self.config_data.app.test_mode,
            logger=self.logger.getChild("probe.scale"),
        )
        self.device_check_var.set(f"Весы: {message}")
        self.diag_status_var.set(message)
        self._set_status(message, logging.INFO if ok else logging.WARNING)
        messagebox.showinfo("Проверка весов", message, parent=self) if ok else messagebox.showwarning("Проверка весов", message, parent=self)

    def probe_furnace_device(self) -> None:
        if not self._commit_settings_to_config(show_errors=True):
            return
        self.config_data.furnace.port = self.furnace_port_var.get().strip()
        ok, message = probe_furnace_port(
            self.config_data.furnace,
            test_mode=self.config_data.app.test_mode,
            logger=self.logger.getChild("probe.furnace"),
        )
        self.device_check_var.set(f"Печь: {message}")
        self.diag_status_var.set(message)
        self._set_status(message, logging.INFO if ok else logging.WARNING)
        messagebox.showinfo("Проверка печи", message, parent=self) if ok else messagebox.showwarning("Проверка печи", message, parent=self)

    def start_acquisition(self) -> None:
        if not self._commit_settings_to_config(show_errors=True):
            return
        if self.controller.running:
            self._set_status("Опрос уже запущен.")
            return

        missing: list[str] = []
        if not self.config_data.app.test_mode:
            if self.config_data.scale.enabled and not self.scale_port_var.get().strip():
                missing.append("весы")
            if self.config_data.furnace.enabled and not self.furnace_port_var.get().strip():
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
            scale_enabled=self.config_data.scale.enabled,
            furnace_enabled=self.config_data.furnace.enabled,
        )
        save_config(self.config_data, self.config_path)
        self.plotter.set_max_points(self.config_data.app.max_points_on_plot)
        self.plotter.clear()
        self.controller.start()
        self._set_status("Измерение запущено.")
        self._update_action_buttons()

    def stop_acquisition(self) -> None:
        if not self.controller.running:
            self._set_status("Опрос уже остановлен.")
            return
        self.controller.stop()
        self._set_status("Измерение остановлено.")
        self._update_action_buttons()

    def clear_graph(self) -> None:
        self.plotter.clear()
        self._reset_readouts()
        self._set_status("График очищен.")

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

        success, message = self.export_service.export(resolve_path(self.config_data.app.csv_path), destination)
        self._set_status(message, logging.INFO if success else logging.WARNING)
        self.diag_status_var.set(message)
        if success:
            messagebox.showinfo("Экспорт данных", message, parent=self)
        else:
            messagebox.showwarning("Экспорт данных", message, parent=self)

    def tare_scale(self) -> None:
        if not self._scale_actions_allowed():
            self._set_status("Тара недоступна: нет связи с весами.", logging.WARNING)
            return
        result = self.controller.tare_scale()
        self._set_status("Команда тары отправлена." if result else "Не удалось отправить команду тары.", logging.INFO if result else logging.WARNING)

    def zero_scale(self) -> None:
        if not self._scale_actions_allowed():
            self._set_status("Команда нуля недоступна: нет связи с весами.", logging.WARNING)
            return
        result = self.controller.zero_scale()
        self._set_status("Команда нуля отправлена." if result else "Не удалось отправить команду нуля.", logging.INFO if result else logging.WARNING)

    def toggle_left_panel(self) -> None:
        self.open_settings_window()

    def toggle_right_panel(self) -> None:
        self.right_panel_visible = not self.right_panel_visible
        self.view_mode_var.set("advanced" if self.left_panel_visible or self.right_panel_visible else self.view_mode_var.get())
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
        help_window = tk.Toplevel(self)
        help_window.title("Инструкция")
        help_window.transient(self)
        help_window.grab_set()
        help_window.geometry(f"{int(760 * self.ui_scale)}x{int(620 * self.ui_scale)}")

        outer = ttk.Frame(help_window, style="Card.TFrame", padding=self._pad(18, 18))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Как пользоваться программой", style="CardTitle.TLabel").pack(anchor="w")

        text = ScrolledText(outer, wrap="word", relief="flat")
        text.pack(fill="both", expand=True, pady=(self._pad_y(12), 0))
        text.insert(
            "end",
            "ПОДКЛЮЧЕНИЕ\n"
            "1. Подключите весы и/или печь к компьютеру.\n"
            "2. Откройте окно «Настройки».\n"
            "3. В блоке «Устройства» выберите COM-порты и нажмите проверку.\n\n"
            "ЗАПУСК ИЗМЕРЕНИЯ\n"
            "1. Нажмите «Старт».\n"
            "2. График начнет обновляться автоматически.\n"
            "3. Крупные карточки под графиком покажут текущую массу, температуру и статус.\n\n"
            "РАБОТА С ГРАФИКОМ\n"
            "• Справа от графика находятся две панели: сначала «Инструменты», затем «Виды».\n"
            "• Кнопка «PNG» сохраняет только график как изображение.\n"
            "• Экспорт CSV и Excel в меню «Файл» сохраняет таблицу измерений, а не картинку.\n\n"
            "ЕСЛИ НЕТ COM-ПОРТА\n"
            "• Проверьте кабель и питание устройства.\n"
            "• Установите драйвер USB-Serial или USB-RS485 адаптера.\n"
            "• Убедитесь, что COM-порт не занят другой программой.\n\n"
            "ЕСЛИ УСТРОЙСТВО НЕ ОТВЕЧАЕТ\n"
            "• Проверьте скорость связи и таймаут.\n"
            "• Для печи проверьте slave ID, регистры и линии A/B.\n"
            "• Для весов проверьте режим передачи данных и команду опроса.\n\n"
            "АНАЛИЗ ГРАФИКА\n"
            "• «🔍» — выделение области для детального просмотра.\n"
            "• «Сдвиг» — перемещение уже увеличенного графика.\n"
            "• «+» и «-» — быстрое приближение и отдаление.\n"
            "• «Авто» — вернуть удобный автоматический масштаб.\n"
            "• «Сброс» — полностью восстановить исходный вид.\n",
        )
        text.configure(state="disabled")
        self._apply_theme_to_toplevel(help_window, text_widget=text)

    def show_about_dialog(self) -> None:
        about_window = tk.Toplevel(self)
        about_window.title("Об авторе")
        about_window.transient(self)
        about_window.grab_set()
        about_window.geometry(f"{int(560 * self.ui_scale)}x{int(340 * self.ui_scale)}")

        outer = ttk.Frame(about_window, style="Card.TFrame", padding=self._pad(18, 18))
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)

        ttk.Label(outer, text="DataFusion RT", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text="Разработчик:\nДенчик Артур Станиславович\n\nНа базе:\nФИЦ УУХ СО РАН",
            style="CardText.TLabel",
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(self._pad_y(8), self._pad_y(14)))

        links = ttk.Frame(outer, style="Card.TFrame")
        links.grid(row=2, column=0, sticky="ew")
        links.grid_columnconfigure(0, weight=1)
        links.grid_columnconfigure(1, weight=1)
        ttk.Button(
            links,
            text="ВКонтакте",
            style="Soft.TButton",
            command=lambda: webbrowser.open("https://vk.com/id391500377"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            links,
            text="Исходный код",
            style="Soft.TButton",
            command=lambda: webbrowser.open("https://github.com/Vanagandr111/DataFusion-RT"),
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

        ttk.Button(outer, text="Закрыть", style="Accent.TButton", command=about_window.destroy).grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(self._pad_y(18), 0),
        )
        self._apply_theme_to_toplevel(about_window)

    def _apply_theme(self) -> None:
        palette = self.theme_manager.palette
        style = self.theme_manager.apply_ttk_styles(self, scale=self.ui_scale)
        style.configure("Section.TLabelframe", background=palette.card_bg, bordercolor=palette.border)
        style.configure("Section.TLabelframe.Label", background=palette.card_bg, foreground=palette.text)
        self.configure(bg=palette.app_bg)
        self.header_status.configure(bg=palette.header_bg, fg=palette.subtext, font=("Segoe UI", max(11, int(12 * self.ui_scale))))
        self._style_menu_button(self.file_menu_button, palette)
        self._style_menu_button(self.help_button, palette)
        self._style_menu_button(self.log_menu_button, palette)
        self.theme_switch_button.configure(text="Тема: светлая" if palette.name == "light" else "Тема: тёмная")
        self.log_text.configure(background=palette.input_bg, foreground=palette.text, insertbackground=palette.text, font=("Consolas", max(10, int(11 * self.ui_scale))))
        if hasattr(self, "settings_mode_badge") and self.settings_mode_badge.winfo_exists():
            self._update_settings_control_states()
        if hasattr(self, "font_scale_value_label") and self.font_scale_value_label.winfo_exists():
            self.font_scale_value_label.configure(text=f"{int(round(self.config_data.app.font_scale * 100))}%")
        for card in (self.mass_card, self.temp_card, self.status_card, self.time_card):
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

    def _apply_theme_to_toplevel(self, window: tk.Toplevel, *, text_widget: ScrolledText | None = None) -> None:
        palette = self.theme_manager.palette
        window.configure(bg=palette.app_bg)
        if hasattr(self, "_settings_canvas"):
            try:
                self._settings_canvas.configure(bg=palette.app_bg)
            except Exception:
                pass
        if text_widget is not None:
            text_widget.configure(background=palette.input_bg, foreground=palette.text, insertbackground=palette.text, font=("Segoe UI", max(12, int(13 * self.ui_scale))))

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
            step = 0.035
            new_top = min(max_top, max(0.0, top + (step * delta)))
            canvas.yview_moveto(new_top)
        except tk.TclError:
            return

    def _style_indicator(self, indicator: dict[str, object], palette: ThemePalette) -> None:
        frame = indicator["frame"]
        canvas = indicator["canvas"]
        label = indicator["label"]
        if isinstance(frame, ttk.Frame):
            frame.configure(style="Header.TFrame")
        canvas.configure(bg=palette.header_bg)
        label.configure(bg=palette.header_bg, fg=palette.text, font=("Segoe UI Semibold", max(10, int(11 * self.ui_scale))))

    def _animate_indicators(self) -> None:
        palette = self.theme_manager.palette
        self._indicator_phase = (getattr(self, "_indicator_phase", 0) + 1) % 24
        intensity = 0.45 + abs(12 - self._indicator_phase) / 18.0

        def update(indicator: dict[str, object], *, connected: bool, enabled: bool, base_color: str) -> None:
            canvas = indicator["canvas"]
            outer = indicator["outer"]
            inner = indicator["inner"]
            if connected:
                outer_fill = _blend_color(base_color, palette.card_bg, 0.45 + (intensity * 0.15))
                inner_fill = base_color
            elif enabled:
                outer_fill = _blend_color(palette.error, palette.card_bg, 0.42 + (intensity * 0.18))
                inner_fill = palette.error
            else:
                outer_fill = _blend_color(palette.border, palette.header_bg, 0.4)
                inner_fill = palette.disabled
            canvas.itemconfig(outer, fill=outer_fill)
            canvas.itemconfig(inner, fill=inner_fill)

        update(
            self.scale_indicator,
            connected=self.last_scale_connected or self.config_data.app.test_mode,
            enabled=self.config_data.scale.enabled,
            base_color=palette.accent,
        )
        update(
            self.furnace_indicator,
            connected=self.last_furnace_connected or self.config_data.app.test_mode,
            enabled=self.config_data.furnace.enabled,
            base_color=palette.heat,
        )
        self.after(140, self._animate_indicators)

    def _update_side_panels(self) -> None:
        self.body.grid_columnconfigure(0, weight=0, minsize=0)
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_columnconfigure(2, weight=0, minsize=0)

        if self.left_panel_visible:
            self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, self._pad_x(12)))
            self.body.grid_columnconfigure(0, minsize=int(420 * self.ui_scale))
        else:
            self.left_panel.grid_remove()

        self.center_panel.grid(row=0, column=1, sticky="nsew")

        if self.right_panel_visible:
            self.right_panel.grid(row=0, column=2, sticky="nsew", padx=(self._pad_x(12), 0))
            self.body.grid_columnconfigure(2, minsize=int(390 * self.ui_scale))
        else:
            self.right_panel.grid_remove()
        self._set_log_button_text()

    def _set_log_button_text(self) -> None:
        if hasattr(self, "log_menu_button"):
            self.log_menu_button.configure(text="Лог ▾" if self.right_panel_visible else "Лог")

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
        self.last_scale_connected = snapshot.scale_connected
        self.last_furnace_connected = snapshot.furnace_connected
        self.plotter.update(snapshot.record)
        self.mass_card.set_value(_format_value(snapshot.record.mass, 3), unit="g", subtitle="Текущее значение")
        self.temp_card.set_value(_format_value(snapshot.record.furnace_pv, 1), unit="°C", subtitle="Текущее значение")
        status_main, status_sub = self._status_text(snapshot)
        self.status_card.set_value(status_main, subtitle=status_sub)
        self.time_card.set_value(snapshot.record.timestamp.replace("T", " "), subtitle="Последняя запись")

        if snapshot.scale_connected:
            self.mass_card.pulse(self.theme_manager.palette.success)
        if snapshot.furnace_connected:
            self.temp_card.pulse(self.theme_manager.palette.success)

        self.diag_last_sample_var.set(
            f"Последний сэмпл: масса={_format_value(snapshot.record.mass, 3)} g, температура={_format_value(snapshot.record.furnace_pv, 1)} °C"
        )
        self.diag_last_time_var.set(f"Время: {snapshot.record.timestamp.replace('T', ' ')}")
        self.diag_status_var.set(status_sub)
        self._set_status("Измерение выполняется.", emit_log=False)
        self._update_action_buttons()

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
            self.assignment_var.set(f"{port_display_label(port)} | {guess_port_kind(port)} | {port.hwid}")
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
        self.scale_port_display_var.set(self._display_for_port(self.scale_port_var.get().strip()))
        self.furnace_port_display_var.set(self._display_for_port(self.furnace_port_var.get().strip()))

    def _display_for_port(self, port_name: str) -> str:
        if not port_name:
            return ""
        return self.port_display_map.get(port_name.upper(), port_name)

    def _build_setting_tooltip(self, key: str, short_text: str) -> str:
        extra = TOOLTIP_DETAILS.get(key, "")
        default_text = f"По умолчанию: {DEFAULTS[key]}"
        if extra:
            return f"{short_text}\n\nПодсказка:\n{extra}\n\n{default_text}"
        return f"{short_text}\n\n{default_text}"

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
        if "app.start_maximized" in self.setting_vars and "app.fullscreen" in self.setting_vars:
            start_maximized = bool(self.setting_vars["app.start_maximized"].get())
            fullscreen_var = self.setting_vars["app.fullscreen"]
            if not start_maximized and bool(fullscreen_var.get()):
                self._suspend_settings_autosave = True
                try:
                    fullscreen_var.set(False)
                finally:
                    self._suspend_settings_autosave = False
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
        reconfigure_file_logging(resolve_path(self.config_data.app.log_path), enable_file_logging=self.config_data.app.enable_file_logging)
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
        if hasattr(self, "settings_save_button") and self.settings_save_button.winfo_exists():
            self.settings_save_button.state(["disabled"] if autosave_enabled else ["!disabled"])
        if hasattr(self, "settings_mode_badge") and self.settings_mode_badge.winfo_exists():
            palette = self.theme_manager.palette
            if autosave_enabled:
                self.settings_mode_var.set("💾 Автосохранение включено")
                self.settings_mode_hint_var.set("Корректные изменения применяются и записываются в config.yaml сразу после редактирования.")
                badge_bg = palette.accent
                badge_fg = "#081016" if palette.name == "dark" else "#FFFFFF"
                badge_border = _blend_color(palette.accent, palette.border, 0.6)
            else:
                self.settings_mode_var.set("✍ Ручное сохранение")
                self.settings_mode_hint_var.set("Изменения записываются только после нажатия кнопки «Сохранить».")
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

    def _pulse_autosave_badge(self) -> None:
        if not hasattr(self, "settings_mode_badge") or not self.settings_mode_badge.winfo_exists():
            return
        if not bool(self.autosave_settings_var.get()):
            return
        base_bg = str(self.settings_mode_badge.cget("bg"))
        accent = "#8AF3E5" if self.theme_manager.palette.name == "dark" else "#11A799"
        self.settings_mode_badge.configure(bg=accent)
        self.after(180, lambda: self._restore_badge_color(base_bg))

    def _restore_badge_color(self, color: str) -> None:
        if hasattr(self, "settings_mode_badge") and self.settings_mode_badge.winfo_exists():
            self.settings_mode_badge.configure(bg=color)

    def _settings_port_label(self, port: PortInfo) -> str:
        return f"{port.device} - {port.description} - {guess_port_kind(port)}"

    def _extract_port_name(self, raw_value: str) -> str:
        if not raw_value:
            return ""
        if " - " in raw_value:
            return raw_value.split(" - ", 1)[0].strip()
        return raw_value.strip()

    def _commit_settings_to_config(self, *, show_errors: bool) -> bool:
        try:
            self.scale_port_var.set(self._extract_port_name(self.scale_port_display_var.get().strip()))
            self.furnace_port_var.set(self._extract_port_name(self.furnace_port_display_var.get().strip()))
            self.config_data.scale.port = self.scale_port_var.get().strip()
            self.config_data.furnace.port = self.furnace_port_var.get().strip()
            self.config_data.scale.enabled = self._get_bool("scale.enabled")
            self.config_data.scale.baudrate = self._get_int("scale.baudrate")
            self.config_data.scale.timeout = self._get_float("scale.timeout")
            self.config_data.scale.mode = self._get_str("scale.mode").lower()
            self.config_data.scale.request_command = self._get_str("scale.request_command")
            self.config_data.scale.p1_polling_enabled = self._get_bool("scale.p1_polling_enabled")
            self.config_data.scale.p1_poll_interval_sec = self._get_float("scale.p1_poll_interval_sec")
            self.config_data.furnace.enabled = self._get_bool("furnace.enabled")
            self.config_data.furnace.baudrate = self._get_int("furnace.baudrate")
            self.config_data.furnace.bytesize = self._get_int("furnace.bytesize")
            self.config_data.furnace.parity = self._get_str("furnace.parity").upper()[:1] or "N"
            self.config_data.furnace.stopbits = self._get_float("furnace.stopbits")
            self.config_data.furnace.timeout = self._get_float("furnace.timeout")
            self.config_data.furnace.slave_id = self._get_int("furnace.slave_id")
            self.config_data.furnace.register_pv = self._get_int("furnace.register_pv")
            self.config_data.furnace.register_sv = self._get_int("furnace.register_sv")
            self.config_data.furnace.scale_factor = self._get_float("furnace.scale_factor")
            self.config_data.app.poll_interval_sec = self._get_float("app.poll_interval_sec")
            self.config_data.app.max_points_on_plot = self._get_int("app.max_points_on_plot")
            self.config_data.app.test_mode = self._get_bool("app.test_mode")
            self.config_data.app.autosave_settings = bool(self.autosave_settings_var.get())
            self.config_data.app.start_maximized = self._get_bool("app.start_maximized")
            self.config_data.app.fullscreen = self._get_bool("app.fullscreen") if self.config_data.app.start_maximized else False
            self.config_data.app.theme = self._get_str("app.theme").lower()
            self.config_data.app.csv_path = self._get_str("app.csv_path")
            self.config_data.app.log_path = self._get_str("app.log_path")
            self.config_data.app.enable_file_logging = self._get_bool("app.enable_file_logging")
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
        reconfigure_file_logging(resolve_path(self.config_data.app.log_path), enable_file_logging=self.config_data.app.enable_file_logging)
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
                    var.set(str(value))
            self.autosave_settings_var.set(bool(DEFAULTS["app.autosave_settings"]))
        finally:
            self._suspend_settings_autosave = False
        if bool(self.autosave_settings_var.get()):
            self._autosave_settings_silent()
        self._set_status("Значения по умолчанию восстановлены.")
        self._update_settings_control_states()

    def _make_plot_tool_button(self, parent, text: str, command, *, width: int | None = None) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, style="Soft.TButton", width=width)
        return button

    def _create_plot_button_panel(self, parent, *, title: str, column: int, width: int, padx: tuple[int, int]) -> tuple[ttk.Frame, ttk.Frame]:
        panel = ttk.Frame(parent, style="CardAlt.TFrame", padding=self._pad(8, 8))
        panel.grid(row=1, column=column, sticky="ns", pady=(self._pad_y(10), 0), padx=padx)
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        ttk.Label(panel, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, self._pad_y(6)))

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
        canvas.bind("<Configure>", lambda e, c=canvas, w=window_id: (c.itemconfigure(w, width=e.width), update_scrollbar()))
        return panel, body

    def _set_plot_button_selected(self, button: ttk.Button, selected: bool) -> None:
        button.configure(style="SelectedSoft.TButton" if selected else "Soft.TButton")

    def _update_plot_mode_buttons(self) -> None:
        if not hasattr(self, "plot_mode_buttons"):
            return
        current = self.plotter.view_mode
        for mode_key, button in self.plot_mode_buttons.items():
            self._set_plot_button_selected(button, mode_key == current)

    def _update_plot_render_buttons(self) -> None:
        if not hasattr(self, "plot_lines_button"):
            return
        current = self.plotter.render_mode
        self._set_plot_button_selected(self.plot_lines_button, current == LivePlotter.RENDER_LINE)
        self._set_plot_button_selected(self.plot_points_button, current == LivePlotter.RENDER_POINTS)
        self._set_plot_button_selected(self.plot_smooth_button, current == LivePlotter.RENDER_SMOOTH)

    def set_plot_mode_combined(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_COMBINED)
        self._update_plot_mode_buttons()
        self._set_status("Открыт общий график: масса и температура вместе.", emit_log=False)

    def set_plot_mode_split(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_SPLIT)
        self._update_plot_mode_buttons()
        self._set_status("Открыты два синхронизированных графика: масса и температура.", emit_log=False)

    def set_plot_mode_mass(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_MASS)
        self._update_plot_mode_buttons()
        self._set_status("Открыт отдельный график массы.", emit_log=False)

    def set_plot_mode_temp(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_TEMP)
        self._update_plot_mode_buttons()
        self._set_status("Открыт отдельный график температуры.", emit_log=False)

    def set_plot_mode_delta(self) -> None:
        self.plotter.set_view_mode(LivePlotter.VIEW_DELTA)
        self._update_plot_mode_buttons()
        self._set_status("Открыт режим изменения параметров: Δмассы и Δтемпературы.", emit_log=False)

    def set_plot_lines(self) -> None:
        self.plotter.set_render_mode(LivePlotter.RENDER_LINE)
        self._update_plot_render_buttons()
        self._set_status("График показывает линии.", emit_log=False)

    def set_plot_points(self) -> None:
        self.plotter.set_render_mode(LivePlotter.RENDER_POINTS)
        self._update_plot_render_buttons()
        self._set_status("График показывает отдельные точки измерений.", emit_log=False)

    def set_plot_smooth(self) -> None:
        self.plotter.set_render_mode(LivePlotter.RENDER_SMOOTH)
        self._update_plot_render_buttons()
        self._set_status("График показывает сглаженные кривые.", emit_log=False)

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
        self.plot_pan_button.configure(text="Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, active)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def toggle_plot_pan(self) -> None:
        active = self.plotter.toggle_pan()
        self.plot_pan_button.configure(text="Сдвиг: вкл" if active else "Сдвиг")
        self.plot_zoom_button.configure(text="🔍")
        self._set_plot_button_selected(self.plot_pan_button, active)
        self._set_plot_button_selected(self.plot_zoom_button, False)

    def zoom_in_plot(self) -> None:
        self.plotter.zoom_in()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def zoom_out_plot(self) -> None:
        self.plotter.zoom_out()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def reset_plot_view(self) -> None:
        self.plotter.reset_view()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def autoscale_plot(self) -> None:
        self.plotter.autoscale()
        self.plot_zoom_button.configure(text="🔍")
        self.plot_pan_button.configure(text="Сдвиг")
        self._set_plot_button_selected(self.plot_zoom_button, False)
        self._set_plot_button_selected(self.plot_pan_button, False)

    def _status_text(self, snapshot: AcquisitionSnapshot) -> tuple[str, str]:
        if snapshot.test_mode:
            return "Тест", "Данные генерируются программой"
        if snapshot.scale_connected and snapshot.furnace_connected:
            return "Готово", "Весы и печь на связи"
        if snapshot.scale_connected or snapshot.furnace_connected:
            return "Частично", f"Весы: {'на связи' if snapshot.scale_connected else 'нет связи'} | Печь: {'на связи' if snapshot.furnace_connected else 'нет связи'}"
        return "Нет связи", "Проверьте подключение устройств"

    def _reset_readouts(self) -> None:
        self.last_scale_connected = False
        self.last_furnace_connected = False
        self.mass_card.set_value("--", unit="g", subtitle="Ожидание данных")
        self.temp_card.set_value("--", unit="°C", subtitle="Ожидание данных")
        self.status_card.set_value("Ожидание", subtitle="Нажмите «Старт»")
        self.time_card.set_value("--", subtitle="Последняя запись")
        self.diag_last_sample_var.set("Последний сэмпл: --")
        self.diag_last_time_var.set("Время: --")
        self._refresh_diagnostics()
        self._update_action_buttons()

    def _scale_actions_allowed(self) -> bool:
        return self.controller.running and (self.config_data.app.test_mode or self.last_scale_connected)

    def _update_action_buttons(self) -> None:
        can_start = not self.controller.running and (
            self.config_data.app.test_mode
            or (self.config_data.scale.enabled and bool(self.scale_port_var.get().strip()))
            or (self.config_data.furnace.enabled and bool(self.furnace_port_var.get().strip()))
        )
        has_selection = self._get_selected_tree_device() is not None
        self.start_button.state(["!disabled"] if can_start else ["disabled"])
        self.stop_button.state(["!disabled"] if self.controller.running else ["disabled"])
        self.tare_button.state(["!disabled"] if self._scale_actions_allowed() else ["disabled"])
        self.zero_button.state(["!disabled"] if self._scale_actions_allowed() else ["disabled"])

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
            self._set_status(f"Не удалось открыть папку журналов: {exc}", logging.WARNING)

    def adjust_font_scale(self, delta: float) -> None:
        self._apply_font_scale_value(_clamp(float(self.config_data.app.font_scale) + delta, 0.9, 1.6))

    def reset_font_scale(self) -> None:
        self._apply_font_scale_value(1.0)

    def _apply_font_scale_value(self, value: float) -> None:
        rounded = round(value, 2)
        self.config_data.app.font_scale = rounded
        self.ui_scale = self._compute_ui_scale()
        self._apply_tk_scaling()
        self._apply_theme()
        if hasattr(self, "font_scale_value_label") and self.font_scale_value_label.winfo_exists():
            self.font_scale_value_label.configure(text=f"{int(round(rounded * 100))}%")
        if bool(self.autosave_settings_var.get()):
            save_config(self.config_data, self.config_path)
        self._set_status(f"Размер шрифта: {int(round(rounded * 100))}%.", emit_log=False)

    def _log_settings_changes(self, before: dict[str, object], after: dict[str, object]) -> None:
        changes: list[str] = []
        for section_name, section_values in after.items():
            previous_section = before.get(section_name, {})
            if not isinstance(section_values, dict) or not isinstance(previous_section, dict):
                continue
            for key, value in section_values.items():
                old_value = previous_section.get(key)
                if old_value != value:
                    changes.append(f"{section_name}.{key}: {old_value} -> {value}")
        if changes:
            self.logger.info("Изменены настройки: %s", "; ".join(changes))

    def _set_status(self, message: str, level: int = logging.INFO, *, emit_log: bool = True) -> None:
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
