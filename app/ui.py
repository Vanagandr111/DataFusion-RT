from __future__ import annotations

import ctypes
import dataclasses
import json
import logging
import queue
import subprocess
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import pandas as pd

from app.config import load_config, resolve_path, save_config
from app.logger_setup import reconfigure_file_logging
from app.models import AcquisitionSnapshot, AppConfig, PortInfo
from app.services.acquisition import AcquisitionController
from app.services.device_probe import probe_furnace_port, probe_scale_port
from app.services.export_service import MeasurementExportService
from app.services.plotter import LivePlotter
from app.theme import ThemeManager, ThemePalette
from app.utils.serial_tools import (
    detect_preferred_ports,
    guess_port_kind,
    list_available_ports,
    port_display_label,
)


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
    "furnace.bytesize": 7,
    "furnace.parity": "E",
    "furnace.stopbits": 1,
    "furnace.timeout": 1.0,
    "furnace.slave_id": 1,
    "furnace.register_pv": 90,
    "furnace.register_sv": 91,
    "furnace.scale_factor": 0.1,
    "furnace.driver": "dk518",
    "furnace.access_mode": "read_only",
    "furnace.window_enabled": False,
    "furnace.window_period_ms": 1000,
    "furnace.window_open_ms": 120,
    "furnace.window_offset_ms": 0,
    "furnace.experimental_write_enabled": False,
    "furnace.input_type_code": 0,
    "furnace.input_type_name": "K",
    "furnace.high_limit": 1200.0,
    "furnace.high_alarm": 999.9,
    "furnace.low_alarm": 999.9,
    "furnace.pid_p": 10.0,
    "furnace.pid_t": 8.0,
    "furnace.ctrl_mode": 3,
    "furnace.output_high_limit": 100.0,
    "furnace.display_decimals": 2,
    "furnace.sensor_correction": 0.0,
    "furnace.opt_code": 8,
    "furnace.run_code": 27,
    "furnace.alarm_output_code": 3333,
    "furnace.m5_value": 420.0,
    "app.poll_interval_sec": 1.0,
    "app.max_points_on_plot": 500,
    "app.auto_detect_ports": True,
    "app.test_mode": False,
    "app.test_mode_scope": "all",
    "app.autosave_settings": False,
    "app.enable_file_logging": False,
    "app.start_maximized": False,
    "app.fullscreen": False,
    "app.font_scale": 1.05,
    "app.theme": "dark",
    "app.csv_path": "data/measurements.csv",
    "app.log_path": "logs/app.log",
}


SETTINGS_SECTIONS: list[
    tuple[str, list[tuple[str, str, str, str, tuple[str, ...] | None]]]
] = [
    (
        "Весы",
        [
            (
                "scale.enabled",
                "Использовать весы",
                "bool",
                "Включает опрос лабораторных весов.",
                None,
            ),
            (
                "scale.baudrate",
                "Скорость связи",
                "entry",
                "Для ускоренного режима обычно 9600 бод, если весы настроены так же.",
                None,
            ),
            (
                "scale.timeout",
                "Таймаут, сек",
                "entry",
                "Сколько ждать строку от весов перед повтором.",
                None,
            ),
            (
                "scale.mode",
                "Режим чтения",
                "combo",
                "Для P2 Con лучше continuous. auto оставляет резервный опрос, если поток пропадёт.",
                ("auto", "continuous", "poll"),
            ),
            (
                "scale.request_command",
                "Команда опроса",
                "entry",
                "Обычно P\\r\\n. Тара и ноль задаются отдельными кнопками.",
                None,
            ),
            (
                "scale.p1_polling_enabled",
                "Режим P1 Prt",
                "bool",
                "Принудительно читать весы по команде P в режиме P1 Prt.",
                None,
            ),
            (
                "scale.p1_poll_interval_sec",
                "Тайминг опроса P1, сек",
                "entry",
                "Интервал между командными опросами в P1 Prt. По умолчанию быстрый.",
                None,
            ),
        ],
    ),
    (
        "Печь",
        [
            (
                "furnace.enabled",
                "Использовать печь",
                "bool",
                "Включает чтение температуры по Modbus RTU.",
                None,
            ),
            (
                "furnace.driver",
                "Драйвер печи",
                "combo",
                "Выберите профиль dk518 или ручной Modbus-режим для другого контроллера.",
                ("dk518", "modbus"),
            ),
            (
                "furnace.baudrate",
                "Скорость связи",
                "entry",
                "Обычно 9600 бод для USB-RS485 адаптера.",
                None,
            ),
            (
                "furnace.bytesize",
                "Биты данных",
                "combo",
                "Для подтверждённого профиля DK518 используется 7 бит данных.",
                ("7", "8"),
            ),
            (
                "furnace.parity",
                "Чётность",
                "combo",
                "Для подтверждённого профиля DK518 используется E.",
                ("N", "E", "O"),
            ),
            ("furnace.stopbits", "Стоп-биты", "combo", "Обычно 1.", ("1", "1.5", "2")),
            (
                "furnace.timeout",
                "Таймаут, сек",
                "entry",
                "Сколько ждать ответ по Modbus RTU.",
                None,
            ),
            (
                "furnace.slave_id",
                "Адрес устройства",
                "entry",
                "Modbus slave ID контроллера.",
                None,
            ),
            (
                "furnace.register_pv",
                "Регистр температуры камеры",
                "entry",
                "Подтверждённый адрес для температуры внутри камеры: 90 / 0x005A.",
                None,
            ),
            (
                "furnace.register_sv",
                "Регистр температуры термопары",
                "entry",
                "Подтверждённый адрес для термопары: 91 / 0x005B.",
                None,
            ),
            (
                "furnace.scale_factor",
                "Масштаб температуры",
                "entry",
                "Например 0.1, если 253 означает 25.3 °C.",
                None,
            ),
            (
                "furnace.input_type_code",
                "Код входа датчика",
                "entry",
                "С панели MCGS сейчас считан код 0.",
                None,
            ),
            (
                "furnace.input_type_name",
                "Тип датчика",
                "combo",
                "С панели MCGS сейчас считан тип K.",
                (
                    "K",
                    "S",
                    "R",
                    "T",
                    "E",
                    "J",
                    "B",
                    "N",
                    "WRe3-25",
                    "WRe5-26",
                    "Cu50",
                    "Pt100",
                ),
            ),
            (
                "furnace.high_limit",
                "Верхний предел",
                "entry",
                "С панели MCGS: HIAL = 1200.",
                None,
            ),
            (
                "furnace.high_alarm",
                "Верхняя тревога",
                "entry",
                "С панели MCGS: DHAL = 999.9.",
                None,
            ),
            (
                "furnace.low_alarm",
                "Нижняя тревога",
                "entry",
                "С панели MCGS: DLAL = 999.9.",
                None,
            ),
            ("furnace.pid_p", "PID: P", "entry", "С панели MCGS: P = 10.", None),
            ("furnace.pid_t", "PID: T", "entry", "С панели MCGS: T = 8.", None),
            (
                "furnace.ctrl_mode",
                "Режим CTRL",
                "entry",
                "С панели MCGS: CTRL = 3.",
                None,
            ),
            (
                "furnace.output_high_limit",
                "Предел выхода OPH",
                "entry",
                "С панели MCGS: OPH = 100.",
                None,
            ),
            (
                "furnace.display_decimals",
                "Разрядность DL",
                "entry",
                "С панели MCGS: DL = 2.",
                None,
            ),
            (
                "furnace.sensor_correction",
                "Коррекция SC",
                "entry",
                "С панели MCGS: SC = 0.",
                None,
            ),
            ("furnace.opt_code", "Код OPT", "entry", "С панели MCGS: OPT = 8.", None),
            ("furnace.run_code", "Код RUN", "entry", "С панели MCGS: RUN = 27.", None),
            (
                "furnace.alarm_output_code",
                "Код ALOP",
                "entry",
                "С панели MCGS: ALOP = 3333.",
                None,
            ),
            (
                "furnace.m5_value",
                "Параметр M5",
                "entry",
                "С панели MCGS: M5 = 420.",
                None,
            ),
        ],
    ),
    (
        "Приложение",
        [
            (
                "app.poll_interval_sec",
                "Интервал опроса, сек",
                "entry",
                "Как часто обновлять измерения и график.",
                None,
            ),
            (
                "app.max_points_on_plot",
                "Точек на графике",
                "entry",
                "Сколько последних точек держать на экране.",
                None,
            ),
            (
                "app.auto_detect_ports",
                "Автопоиск COM-портов",
                "bool",
                "При запуске программа пытается найти и назначить печь и весы по именам COM-устройств.",
                None,
            ),
            (
                "app.test_mode",
                "Тестовый режим",
                "bool",
                "Генерирует данные без реального оборудования.",
                None,
            ),
            (
                "app.test_mode_scope",
                "Что эмулировать",
                "combo",
                "Можно эмулировать только весы, только печь или оба устройства сразу.",
                ("all", "scale", "furnace"),
            ),
        ],
    ),
    (
        "Интерфейс и файлы",
        [
            (
                "app.theme",
                "Тема оформления",
                "combo",
                "Оформление окна программы.",
                ("dark", "light"),
            ),
            (
                "app.start_maximized",
                "Старт развернутым",
                "bool",
                "Рекомендуется включить для лабораторного ПК.",
                None,
            ),
            (
                "app.fullscreen",
                "Полный экран",
                "bool",
                "Если включено, окно откроется на весь экран.",
                None,
            ),
            (
                "app.csv_path",
                "Файл CSV",
                "entry",
                "Основной файл накопления измерений.",
                None,
            ),
            (
                "app.log_path",
                "Файл журнала",
                "entry",
                "Файл служебного журнала программы.",
                None,
            ),
            (
                "app.enable_file_logging",
                "Включить автологирование",
                "bool",
                "Записывать служебный журнал в файл на диске.",
                None,
            ),
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
    "furnace.driver": "dk518 — готовый профиль для вашей печи: 9600 7E1, slave 1, чтение только. modbus — ручной профиль для другого контроллера и своих регистров.",
    "furnace.baudrate": "Если есть связь по USB-RS485, но нет ответа Modbus, проверьте baudrate одним из первых параметров вместе с parity и slave ID.",
    "furnace.bytesize": "У большинства контроллеров используется 8 бит данных. Меняйте только если это явно указано в документации контроллера.",
    "furnace.parity": "Для Modbus RTU часто критично совпадение parity. Если N не работает, проверьте в документации варианты E или O.",
    "furnace.stopbits": "Обычно используется 1 стоп-бит. Несовпадение этого параметра тоже может полностью ломать обмен по Modbus.",
    "furnace.timeout": "Если контроллер отвечает медленно, попробуйте увеличить таймаут до 1.5-2.0 секунды, чтобы исключить ложные таймауты.",
    "furnace.slave_id": "Это адрес Modbus-устройства. Если контроллер на шине не один, у каждого устройства должен быть свой адрес.",
    "furnace.register_pv": "Для текущего профиля DK518 здесь подтверждён адрес 90 (0x005A): температура внутри камеры.",
    "furnace.register_sv": "Для текущего профиля DK518 здесь подтверждён адрес 91 (0x005B): температура термопары.",
    "furnace.scale_factor": "Если регистр возвращает, например, 253 вместо 25.3 °C, задайте коэффициент 0.1. Если приходит 2530, может понадобиться 0.01.",
    "furnace.input_type_code": "На панели MCGS видно INP = 0, а по таблице типов это K. Это справочный параметр: для Modbus-обмена он сам по себе не задаёт адреса регистров.",
    "furnace.input_type_name": "Сейчас с панели видно, что датчик настроен как термопара K. Если на реальном контроллере тип сменят, обновите это поле для наглядности в проекте.",
    "furnace.high_limit": "Сейчас на панели видно HIAL = 1200. Полезно как ориентир верхнего диапазона, но на чтение Modbus напрямую не влияет.",
    "furnace.high_alarm": "Сейчас на панели видно DHAL = 999.9. Это справочный порог аварии.",
    "furnace.low_alarm": "Сейчас на панели видно DLAL = 999.9. Это справочный порог аварии.",
    "furnace.pid_p": "Сейчас на панели видно P = 10. Поле справочное, чтобы держать под рукой текущую настройку контура.",
    "furnace.pid_t": "Сейчас на панели видно T = 8. Поле справочное, без записи в контроллер.",
    "furnace.ctrl_mode": "Сейчас на панели видно CTRL = 3. Пока трактуем как паспортную настройку контроллера.",
    "furnace.output_high_limit": "Сейчас на панели видно OPH = 100. Это верхний предел выхода контроллера.",
    "furnace.display_decimals": "Сейчас на панели видно DL = 2. Может помочь при подборе scale_factor и понимании формата отображения.",
    "furnace.sensor_correction": "Сейчас на панели видно SC = 0. Это коррекция датчика/смещение.",
    "furnace.opt_code": "Сейчас на панели видно OPT = 8. Поле оставлено как справочный код конфигурации.",
    "furnace.run_code": "Сейчас на панели видно RUN = 27. Поле оставлено как справочный код конфигурации.",
    "furnace.alarm_output_code": "Сейчас на панели видно ALOP = 3333. Поле оставлено как справочный код конфигурации.",
    "furnace.m5_value": "Сейчас на панели видно M5 = 420. Пока используем как паспортное значение, без попыток трактовать жёстко.",
    "app.auto_detect_ports": "Если галочка включена, программа при старте обновляет список COM-портов и пытается сама назначить печь и весы по описанию USB-адаптеров.",
    "app.poll_interval_sec": "Для большинства задач 1 секунда удобно и достаточно. Уменьшайте интервал только если действительно нужна более частая запись.",
    "app.max_points_on_plot": "Чем больше точек, тем длиннее история на экране, но тем тяжелее перерисовка графика на слабом ноутбуке.",
    "app.test_mode": "Полезно для проверки интерфейса дома без оборудования: программа будет сама генерировать массу и температуру.",
    "app.autosave_settings": "Когда автосохранение включено, корректные изменения применяются и записываются в config.yaml сразу. Если поле введено не полностью, сохранение подождёт валидного значения.",
    "app.enable_file_logging": "Когда эта галочка выключена, программа пишет сообщения только в окно журнала и в консоль. Файл на диске не создаётся.",
    "app.theme": "Переключайте тему под освещение лаборатории. Светлая удобнее для печати скриншотов, тёмная часто комфортнее при длительной работе.",
    "app.start_maximized": "Рекомендуется оставить включённым для лабораторного ПК, чтобы все крупные элементы были сразу хорошо видны.",
    "app.fullscreen": "Используйте только если хотите режим без рамок окна. Для обычной работы чаще удобнее просто развёрнутое окно.",
    "app.test_mode_scope": "Можно включить эмуляцию только весов, только печи или сразу обоих устройств. Это удобно для проверки интерфейса и логики без полного стенда.",
    "app.csv_path": "Это основной файл накопления измерений. Убедитесь, что папка доступна на запись и не находится в защищённом системном каталоге.",
    "app.log_path": "Если программа ведёт себя нестабильно, этот файл помогает понять причину. Логи удобно прикладывать при разборе ошибок.",
    "app.font_scale": "Масштаб шрифта интерфейса. Увеличивайте, если на экране много мелкого текста. После резкого увеличения шрифта лучше перезапустить программу.",
}


TABLE_COLUMN_SPECS: tuple[tuple[str, str, int, str], ...] = (
    ("timestamp", "Время", 270, "w"),
    ("mass", "Масса, г", 130, "center"),
    ("pv", "Камера PV, °C", 150, "center"),
    ("sv", "Термопара SV, °C", 170, "center"),
)

TEST_MODE_SCOPE_LABELS: dict[str, str] = {
    "all": "Весы и печь",
    "scale": "Только весы",
    "furnace": "Только печь",
}
TEST_MODE_SCOPE_VALUES: tuple[str, ...] = tuple(TEST_MODE_SCOPE_LABELS.values())


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

    def apply_theme(self, palette: ThemePalette, scale: float) -> None:
        accent = getattr(palette, self.accent_role, palette.accent)
        self.default_border = palette.border
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

    def set_value(self, value: str, *, unit: str = "", subtitle: str = "") -> None:
        self.value_label.configure(text=value)
        self.unit_label.configure(text=unit)
        self.subtitle_label.configure(text=subtitle)

    def pulse(self, color: str) -> None:
        self.configure(highlightbackground=color)
        self.after(260, lambda: self.configure(highlightbackground=self.default_border))


class LabForgeApp(tk.Tk):
    def __init__(
        self, config: AppConfig, config_path: Path, logger: logging.Logger
    ) -> None:
        _enable_windows_dpi_awareness()
        super().__init__()

        self.config_data = config
        self.config_path = config_path
        self.logger = logger
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
        self.session_autosave_dir = resolve_path("sessions/autosave")
        self.session_autosave_dir.mkdir(parents=True, exist_ok=True)
        self._autosave_timer_id = None

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
        self.file_menu_button = self._make_menu_button(parent, "Сохранить/экспорт")
        file_menu = tk.Menu(self.file_menu_button, tearoff=False)
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
        ttk.Label(
            self.left_panel, text="Подключение устройств", style="CardTitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self.left_panel, textvariable=self.port_status_var, style="CardText.TLabel"
        ).grid(row=1, column=0, sticky="w", pady=(self._pad_y(4), self._pad_y(10)))

        tree_frame = ttk.Frame(self.left_panel, style="Card.TFrame")
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self.port_tree = ttk.Treeview(
            tree_frame, columns=("port", "kind", "desc"), show="headings", height=10
        )
        self.port_tree.heading("port", text="Порт")
        self.port_tree.heading("kind", text="Тип")
        self.port_tree.heading("desc", text="Устройство")
        self.port_tree.column("port", width=int(90 * self.ui_scale), anchor="w")
        self.port_tree.column("kind", width=int(150 * self.ui_scale), anchor="w")
        self.port_tree.column("desc", width=int(250 * self.ui_scale), anchor="w")
        self.port_tree.grid(row=0, column=0, sticky="nsew")
        self.port_tree.bind("<<TreeviewSelect>>", self._on_port_selected)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.port_tree.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.port_tree.configure(yscrollcommand=scrollbar.set)

        self.assignment_label = ttk.Label(
            self.left_panel,
            textvariable=self.assignment_var,
            style="CardText.TLabel",
            wraplength=int(420 * self.ui_scale),
        )
        self.assignment_label.grid(
            row=3, column=0, sticky="w", pady=(self._pad_y(10), 0)
        )

        ports_bar = ttk.Frame(self.left_panel, style="Card.TFrame")
        ports_bar.grid(row=4, column=0, sticky="ew", pady=(self._pad_y(12), 0))
        for idx in range(2):
            ports_bar.grid_columnconfigure(idx, weight=1)
        ttk.Button(
            ports_bar,
            text="Назначить как Весы",
            style="Soft.TButton",
            command=self.assign_selected_to_scale,
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            ports_bar,
            text="Назначить как Печь",
            style="Soft.TButton",
            command=self.assign_selected_to_furnace,
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

        probe_bar = ttk.Frame(self.left_panel, style="Card.TFrame")
        probe_bar.grid(row=5, column=0, sticky="ew", pady=(self._pad_y(10), 0))
        for idx in range(3):
            probe_bar.grid_columnconfigure(idx, weight=1)
        ttk.Button(
            probe_bar, text="Найти", style="Soft.TButton", command=self.refresh_ports
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            probe_bar,
            text="Проверить весы",
            style="Soft.TButton",
            command=self.probe_scale_device,
        ).grid(row=0, column=1, sticky="ew", padx=self._pad_pair(3))
        ttk.Button(
            probe_bar,
            text="Проверить печь",
            style="Soft.TButton",
            command=self.probe_furnace_device,
        ).grid(row=0, column=2, sticky="ew", padx=(self._pad_x(6), 0))

    def _build_center_panel(self) -> None:
        content_tabs = ttk.Notebook(self.center_panel, style="Compact.TNotebook")
        content_tabs.grid(row=0, column=0, sticky="nsew")
        self.center_content_tabs = content_tabs

        graph_tab = ttk.Frame(content_tabs, style="App.TFrame")
        table_tab = ttk.Frame(content_tabs, style="App.TFrame")
        graph_tab.grid_rowconfigure(0, weight=1)
        graph_tab.grid_columnconfigure(0, weight=1)
        table_tab.grid_rowconfigure(0, weight=1)
        table_tab.grid_columnconfigure(0, weight=1)
        content_tabs.add(graph_tab, text="График")
        content_tabs.add(table_tab, text="Таблица")

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
        self.legend_panel = ttk.LabelFrame(
            plot_card,
            text="Легенда",
            style="Section.TLabelframe",
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
        self.plot_minus_button = self._make_plot_tool_button(
            zoom_row, "-", self.zoom_out_plot, width=4
        )
        self.plot_minus_button.grid(
            row=0, column=1, sticky="ew", padx=(self._pad_x(3), 0)
        )
        self.plot_reset_button = self._make_plot_tool_button(
            tools_body, "Сброс", self.reset_plot_view, width=10
        )
        self.plot_reset_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_auto_button = self._make_plot_tool_button(
            tools_body, "Авто", self.autoscale_plot, width=10
        )
        self.plot_auto_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_scale_button = self._make_plot_tool_button(
            tools_body, "Масштаб", self.open_plot_scale_dialog, width=10
        )
        self.plot_scale_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_points_button = self._make_plot_tool_button(
            tools_body, "Точки", self.set_plot_points, width=10
        )
        self.plot_points_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_lines_button = self._make_plot_tool_button(
            tools_body, "Линии", self.set_plot_lines, width=10
        )
        self.plot_lines_button.pack(fill="x", pady=(0, self._pad_y(5)))
        views_panel, views_body = self._create_plot_button_panel(
            plot_card,
            title="Виды",
            column=3,
            width=int(126 * self.ui_scale),
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
        self.calc_normalize_button = self._make_plot_tool_button(
            calculations_body, "Нормал.", self.toggle_calc_normalization, width=12
        )
        self.calc_normalize_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.calc_markers_button = self._make_plot_tool_button(
            calculations_body, "Маркеры", self.toggle_calc_markers, width=12
        )
        self.calc_markers_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.calc_heating_profile_button = self._make_plot_tool_button(
            calculations_body, "Профиль нагрева", self.toggle_calc_heating_profile, width=12
        )
        self.calc_heating_profile_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.plot_smooth_button = self._make_plot_tool_button(
            calculations_body, "Сглаж.", self.set_plot_smooth, width=12
        )
        self.plot_smooth_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.calc_stage_button = self._make_plot_tool_button(
            calculations_body, "Стадии", self.toggle_calc_stage_analysis, width=12
        )
        self.calc_stage_button.pack(fill="x", pady=(0, self._pad_y(5)))
        self.calc_summary_button = self._make_plot_tool_button(
            calculations_body, "Сводка", self.show_calc_summary, width=12
        )
        self.calc_summary_button.pack(fill="x")
        self._update_plot_mode_buttons()
        self._update_plot_render_buttons()
        self._update_calc_buttons()
        self._refresh_plot_legend()
        self._update_plot_side_panels_state()

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
        table_time_controls = ttk.Frame(table_controls, style="CardAlt.TFrame")
        table_time_controls.grid(row=0, column=2, sticky="e", padx=(self._pad_x(10), 0))
        self.table_time_format_combo = ttk.Combobox(
            table_time_controls,
            state="readonly",
            width=14,
            textvariable=self.table_time_format_var,
            values=("ЧЧ:ММ:СС", "ЧЧ:ММ:СС.мс", "Дата+время", "Дата+время.мс"),
        )
        self.table_time_format_combo.grid(
            row=0, column=0, sticky="w", padx=(0, self._pad_x(6))
        )
        self.table_time_suffix_combo = ttk.Combobox(
            table_time_controls,
            state="readonly",
            width=12,
            textvariable=self.table_time_suffix_var,
            values=("Без зоны", "местн.", "UTC+смещ."),
        )
        self.table_time_suffix_combo.grid(row=0, column=1, sticky="w")
        self.table_time_format_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_table_timestamps()
        )
        self.table_time_suffix_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_table_timestamps()
        )
        self.table_time_format_combo.set("ЧЧ:ММ:СС.мс")
        self.table_time_suffix_combo.set("Без зоны")
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
        self._apply_table_column_visibility()

        cards = ttk.Frame(self.center_panel, style="App.TFrame")
        cards.grid(row=1, column=0, sticky="nsew", pady=(self._pad_y(8), 0))
        for idx in range(5):
            cards.grid_columnconfigure(idx, weight=1, uniform="metric")
        self.mass_card = MetricCard(cards, "Масса", "accent", value_size=40)
        self.mass_card.grid(row=0, column=0, sticky="nsew", padx=(0, self._pad_x(8)))
        self.temp_card = MetricCard(cards, "Камера PV", "heat", value_size=40)
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

        actions = ttk.Frame(self.center_panel, style="App.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(self._pad_y(8), 0))
        for idx in range(5):
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
        self.reset_button = ttk.Button(
            actions, text="Сброс", style="Soft.TButton", command=self.clear_graph
        )
        self.reset_button.grid(
            row=0, column=2, sticky="ew", padx=(self._pad_x(3), self._pad_x(8))
        )
        self.tare_button = ttk.Button(
            actions, text="Тара", style="Soft.TButton", command=self.tare_scale
        )
        self.tare_button.grid(
            row=0, column=3, sticky="ew", padx=(self._pad_x(2), self._pad_x(3))
        )
        self.zero_button = ttk.Button(
            actions, text="Ноль", style="Soft.TButton", command=self.zero_scale
        )
        self.zero_button.grid(row=0, column=4, sticky="ew", padx=(self._pad_x(6), 0))

    def _build_right_panel(self) -> None:
        self.right_panel.grid_propagate(False)
        ttk.Label(
            self.right_panel, text="Лог и диагностика", style="CardTitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self.right_panel,
            textvariable=self.diag_ports_var,
            style="CardText.TLabel",
            wraplength=int(360 * self.ui_scale),
        ).grid(row=1, column=0, sticky="w", pady=(self._pad_y(6), 0))
        ttk.Label(
            self.right_panel,
            textvariable=self.diag_last_sample_var,
            style="CardText.TLabel",
            wraplength=int(360 * self.ui_scale),
        ).grid(row=2, column=0, sticky="w", pady=(self._pad_y(6), 0))
        ttk.Label(
            self.right_panel,
            textvariable=self.diag_last_time_var,
            style="CardText.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(self._pad_y(6), 0))
        ttk.Label(
            self.right_panel,
            textvariable=self.diag_status_var,
            style="CardText.TLabel",
            wraplength=int(360 * self.ui_scale),
        ).grid(row=4, column=0, sticky="w", pady=(self._pad_y(6), self._pad_y(10)))

        log_actions = ttk.Frame(self.right_panel, style="Card.TFrame")
        log_actions.grid(row=5, column=0, sticky="ew", pady=(0, self._pad_y(8)))
        log_actions.grid_columnconfigure(0, weight=1)
        log_actions.grid_columnconfigure(1, weight=1)
        ttk.Button(
            log_actions,
            text="Сохранить TXT",
            style="Soft.TButton",
            command=self.save_runtime_log,
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            log_actions,
            text="Папка журналов",
            style="Soft.TButton",
            command=self.open_logs_folder,
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

        self.log_text = ScrolledText(
            self.right_panel, wrap="word", relief="flat", height=18
        )
        self.log_text.grid(row=6, column=0, sticky="nsew")
        self.log_text.insert("end", "Журнал готов.\n")
        self.log_text.configure(state="disabled")

    def _refresh_plot_legend(self) -> None:
        if not hasattr(self, "legend_panel"):
            return
        for child in self.legend_panel.winfo_children():
            child.destroy()

        items = self.plotter.legend_items()
        if not items:
            ttk.Label(
                self.legend_panel,
                text="Нет доступных кривых для текущего режима.",
                style="CardText.TLabel",
            ).grid(row=0, column=0, sticky="w")
            spacer_column = 1
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
                button.grid(row=0, column=index, sticky="w", padx=(0, self._pad_x(12)))
                self.legend_checkbuttons[key] = button
            spacer_column = len(items)
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
        ttk.Button(
            legend_actions,
            text="⚙",
            style="WindowIcon.TButton",
            command=self.open_plot_style_editor,
            width=3,
        ).grid(row=0, column=2, sticky="e")
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
            "Для печи обычно выбирается USB-RS485 адаптер. Для вашего профиля DK518 по умолчанию используются регистры 0x005A и 0x005B.",
            wraplength=int(640 * self.ui_scale),
        ).grid(row=1, column=2, sticky="nsew", padx=(self._pad_x(12), 0))

        ttk.Checkbutton(
            devices_frame,
            text="Автопоиск COM-портов при запуске",
            variable=self.auto_detect_ports_var,
            style="Card.TCheckbutton",
            takefocus=False,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(self._pad_y(6), 0))

        device_buttons = ttk.Frame(devices_frame, style="Card.TFrame")
        device_buttons.grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(8), 0)
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
            row=4,
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
        ).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(10), 0))
        ttk.Button(
            devices_frame,
            text="Открыть страницу драйвера Adam",
            style="SettingsSoft.TButton",
            command=lambda: webbrowser.open(
                "https://adamequipment.co.uk/support/software-downloads.html"
            ),
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(self._pad_y(8), 0))
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
        ).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(self._pad_y(10), 0))

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

        if assigned:
            self.device_check_var.set(f"Автопоиск назначил: {', '.join(assigned)}.")
        if missing:
            message = f"Автопоиск не обнаружил COM-порты для: {', '.join(missing)}."
            self.port_status_var.set(message)
            self._set_status(message, logging.WARNING)
            if on_startup:
                messagebox.showwarning("DataFusion RT", message, parent=self)
        elif assigned:
            self.port_status_var.set(f"Автопоиск назначил: {', '.join(assigned)}.")
            self._set_status(self.port_status_var.get(), emit_log=not on_startup)

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
        saved = self.autosave_session()
        if saved is not None:
            self._set_status(f"Сессия автосохранена: {saved.name}", logging.INFO)

    def clear_graph(self) -> None:
        self.plotter.clear()
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
        visible_columns = [
            key
            for key in self.table_column_order
            if bool(self.table_column_vars[key].get())
        ]
        rows: list[dict[str, object]] = []
        for row_index, item_id in enumerate(
            self.measurements_table.get_children(), start=1
        ):
            values = self.measurements_table.item(item_id, "values")
            row_map = dict(zip(self.table_column_order, values))
            normalized: dict[str, object] = {"№": row_index}
            for key in visible_columns:
                header = next(
                    (
                        label
                        for column_key, label, _width, _anchor in TABLE_COLUMN_SPECS
                        if column_key == key
                    ),
                    key,
                )
                normalized[header] = row_map.get(key, "")
            rows.append(normalized)
        return pd.DataFrame(rows)

    def _build_session_data(self) -> dict:
        from app.services.plotter import LivePlotter

        plotter = self.plotter
        data = {
            "metadata": {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "records_count": len(self.measurement_records),
            },
            "records": [record.as_dict() for record in self.measurement_records],
            "plot_state": {
                "view_mode": plotter.view_mode,
                "render_mode": plotter.render_mode,
                "normalization_enabled": plotter.normalization_enabled,
                "markers_enabled": plotter.markers_enabled,
                "heating_profile_enabled": plotter.heating_profile_enabled,
                "cursor_probe_enabled": plotter.cursor_probe_enabled,
                "stage_analysis_enabled": plotter.stage_analysis_enabled,
                "series_visibility": plotter._series_visibility,
            },
            "config": {
                "scale_port": self.scale_port_var.get(),
                "furnace_port": self.furnace_port_var.get(),
                "scale_enabled": self.config_data.scale.enabled,
                "furnace_enabled": self.config_data.furnace.enabled,
            },
        }
        return data

    def save_session(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить сессию",
            defaultextension=".json",
            initialfile=f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            filetypes=[("JSON", "*.json")],
        )
        if not target:
            return
        destination = Path(target)
        if not destination.suffix:
            destination = destination.with_suffix(".json")

        session_data = self._build_session_data()
        try:
            with open(destination, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2, ensure_ascii=False)
            self._set_status(f"Сессия сохранена: {destination.name}", logging.INFO)
            messagebox.showinfo(
                "Сохранение сессии",
                f"Сессия сохранена в {destination.name}",
                parent=self,
            )
        except Exception as e:
            self.logger.exception("Ошибка сохранения сессии")
            self._set_status(f"Ошибка сохранения сессии: {e}", logging.ERROR)
            messagebox.showerror("Сохранение сессии", f"Ошибка: {e}", parent=self)

    def autosave_session(self) -> Path | None:
        if not self.measurement_records:
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = self.session_autosave_dir / f"autosave_{timestamp}.json"
        session_data = self._build_session_data()
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2, ensure_ascii=False)
            self._cleanup_autosaves(max_count=5)
            self.logger.info(f"Сессия автосохранена: {filename.name}")
            return filename
        except Exception as e:
            self.logger.exception("Ошибка автосохранения сессии")
            return None

    def _cleanup_autosaves(self, max_count: int = 5) -> None:
        try:
            files = list(self.session_autosave_dir.glob("autosave_*.json"))
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for file in files[max_count:]:
                file.unlink()
                self.logger.debug(f"Удалён старый автосейв: {file.name}")
        except Exception as e:
            self.logger.exception("Ошибка очистки автосейвов")

    def _autosave_timer(self) -> None:
        # Отменить предыдущий таймер, если есть
        if self._autosave_timer_id is not None:
            self.after_cancel(self._autosave_timer_id)

        if self.controller.running and self.measurement_records:
            saved = self.autosave_session()
            if saved is not None:
                self.logger.debug(f"Периодическое автосохранение: {saved.name}")

        # Планируем следующий вызов через 1 минуту (60000 мс)
        self._autosave_timer_id = self.after(60000, self._autosave_timer)

    def load_session(self, filepath: Path) -> bool:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                session_data = json.load(f)

            # Остановить текущее измерение
            if self.controller.running:
                self.controller.stop()

            # Очистить текущие данные
            self.measurement_records.clear()
            self.plotter.clear()
            if hasattr(self, "measurements_table"):
                for item_id in self.measurements_table.get_children():
                    self._table_timestamp_map.pop(item_id, None)
                    self.measurements_table.delete(item_id)

            # Загрузить записи
            records_data = session_data.get("records", [])
            for rec_dict in records_data:
                # Преобразовать dict в MeasurementRecord
                record = MeasurementRecord(
                    timestamp=rec_dict.get("timestamp"),
                    mass=rec_dict.get("mass"),
                    furnace_pv=rec_dict.get("furnace_pv"),
                    furnace_sv=rec_dict.get("furnace_sv"),
                    mass_timestamp=rec_dict.get("mass_timestamp"),
                    furnace_pv_timestamp=rec_dict.get("furnace_pv_timestamp"),
                    furnace_sv_timestamp=rec_dict.get("furnace_sv_timestamp"),
                )
                self.measurement_records.append(record)
                # Добавить в таблицу
                self._append_measurement_row(record)
                # Обновить график
                self.plotter.update(record)

            # Восстановить состояние графика
            plot_state = session_data.get("plot_state", {})
            if plot_state:
                view_mode = plot_state.get("view_mode")
                if view_mode and hasattr(self.plotter, "set_view_mode"):
                    self.plotter.set_view_mode(view_mode)
                render_mode = plot_state.get("render_mode")
                if render_mode and hasattr(self.plotter, "set_render_mode"):
                    self.plotter.set_render_mode(render_mode)
                normalization = plot_state.get("normalization_enabled")
                if (
                    normalization
                    and self.plotter.normalization_enabled != normalization
                ):
                    self.plotter.toggle_normalization()
                markers = plot_state.get("markers_enabled")
                if markers and self.plotter.markers_enabled != markers:
                    self.plotter.toggle_markers()
                heating_profile_enabled = plot_state.get("heating_profile_enabled")
                if (
                    heating_profile_enabled
                    and self.plotter.heating_profile_enabled != heating_profile_enabled
                ):
                    self.plotter.toggle_heating_profile()
                cursor_probe = plot_state.get("cursor_probe_enabled")
                if cursor_probe and self.plotter.cursor_probe_enabled != cursor_probe:
                    self.plotter.toggle_cursor_probe()
                stage_analysis = plot_state.get("stage_analysis_enabled")
                if (
                    stage_analysis
                    and self.plotter.stage_analysis_enabled != stage_analysis
                ):
                    self.plotter.toggle_stage_analysis()
                series_visibility = plot_state.get("series_visibility", {})
                for series_key, visible in series_visibility.items():
                    self.plotter.set_series_visible(series_key, visible)

            self._set_status(
                f"Сессия загружена: {filepath.name} ({len(records_data)} записей)",
                logging.INFO,
            )
            messagebox.showinfo(
                "Загрузка сессии",
                f"Сессия загружена: {len(records_data)} записей",
                parent=self,
            )
            return True
        except Exception as e:
            self.logger.exception(f"Ошибка загрузки сессии: {filepath}")
            self._set_status(f"Ошибка загрузки сессии: {e}", logging.ERROR)
            messagebox.showerror("Загрузка сессии", f"Ошибка: {e}", parent=self)
            return False

    def update_restore_session_menu(self) -> None:
        if not hasattr(self, "restore_session_menu"):
            return
        menu = self.restore_session_menu
        menu.delete(0, "end")
        try:
            files = list(self.session_autosave_dir.glob("autosave_*.json"))
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                menu.add_command(label="(пусто)", state="disabled")
                return
            for file in files[:5]:
                # Преобразовать имя файла в читаемую дату
                raw = file.stem.replace("autosave_", "")
                try:
                    dt = datetime.strptime(raw, "%Y%m%d_%H%M%S")
                    label = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    label = raw
                menu.add_command(
                    label=label, command=lambda f=file: self.load_session(f)
                )
        except Exception as e:
            self.logger.exception("Ошибка обновления меню восстановления сессии")
            menu.add_command(label="(ошибка)", state="disabled")
        finally:
            menu.add_separator()
            menu.add_command(
                label="Импорт из файла...", command=self.import_data_from_file
            )

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
                df = pd.read_excel(path)
            else:
                self._set_status(
                    f"Неподдерживаемый формат файла: {path.suffix}", logging.ERROR
                )
                return

            # Преобразовать DataFrame в записи
            records = []
            for _, row in df.iterrows():
                # Попробовать определить колонки
                timestamp = None
                mass = None
                furnace_pv = None
                furnace_sv = None

                # Искать колонки по возможным именам
                for col in df.columns:
                    col_lower = str(col).lower()
                    if (
                        "timestamp" in col_lower
                        or "время" in col_lower
                        or "дата" in col_lower
                    ):
                        timestamp = row[col]
                    elif (
                        "mass" in col_lower
                        or "масса" in col_lower
                        or "вес" in col_lower
                    ):
                        mass = row[col]
                    elif (
                        "furnace_pv" in col_lower
                        or "pv" in col_lower
                        or "камера" in col_lower
                    ):
                        furnace_pv = row[col]
                    elif (
                        "furnace_sv" in col_lower
                        or "sv" in col_lower
                        or "термопара" in col_lower
                    ):
                        furnace_sv = row[col]

                # Если не нашли timestamp, попробовать использовать индекс или первую колонку
                if timestamp is None and len(df.columns) > 0:
                    timestamp = row[df.columns[0]]

                # Преобразовать timestamp в строку, если это datetime
                if isinstance(timestamp, pd.Timestamp):
                    timestamp = timestamp.isoformat()
                elif timestamp is not None:
                    timestamp = str(timestamp)

                record = MeasurementRecord(
                    timestamp=timestamp or "",
                    mass=float(mass)
                    if mass is not None and not pd.isna(mass)
                    else None,
                    furnace_pv=float(furnace_pv)
                    if furnace_pv is not None and not pd.isna(furnace_pv)
                    else None,
                    furnace_sv=float(furnace_sv)
                    if furnace_sv is not None and not pd.isna(furnace_sv)
                    else None,
                )
                records.append(record)

            if not records:
                self._set_status("Не удалось извлечь данные из файла", logging.WARNING)
                messagebox.showwarning(
                    "Импорт данных", "Не удалось извлечь данные из файла", parent=self
                )
                return

            # Очистить текущие данные и загрузить новые
            if self.controller.running:
                self.controller.stop()

            self.measurement_records.clear()
            self.plotter.clear()
            if hasattr(self, "measurements_table"):
                for item_id in self.measurements_table.get_children():
                    self._table_timestamp_map.pop(item_id, None)
                    self.measurements_table.delete(item_id)

            for record in records:
                self.measurement_records.append(record)
                self._append_measurement_row(record)
                self.plotter.update(record)

            self._set_status(
                f"Импортировано {len(records)} записей из {path.name}", logging.INFO
            )
            messagebox.showinfo(
                "Импорт данных",
                f"Успешно импортировано {len(records)} записей",
                parent=self,
            )

        except Exception as e:
            self.logger.exception(f"Ошибка импорта данных из файла: {path}")
            self._set_status(f"Ошибка импорта: {e}", logging.ERROR)
            messagebox.showerror("Импорт данных", f"Ошибка: {e}", parent=self)

    def tare_scale(self) -> None:
        if not self._scale_actions_allowed():
            self._set_status("Тара недоступна: нет связи с весами.", logging.WARNING)
            return
        result = self.controller.tare_scale()
        self._set_status(
            "Команда тары отправлена."
            if result
            else "Не удалось отправить команду тары.",
            logging.INFO if result else logging.WARNING,
        )

    def zero_scale(self) -> None:
        if not self._scale_actions_allowed():
            self._set_status(
                "Команда нуля недоступна: нет связи с весами.", logging.WARNING
            )
            return
        result = self.controller.zero_scale()
        self._set_status(
            "Команда нуля отправлена."
            if result
            else "Не удалось отправить команду нуля.",
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
        help_window = tk.Toplevel(self)
        help_window.title("Инструкция")
        help_window.transient(self)
        help_window.grab_set()
        help_window.geometry(f"{int(760 * self.ui_scale)}x{int(620 * self.ui_scale)}")

        outer = ttk.Frame(help_window, style="Card.TFrame", padding=self._pad(18, 18))
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="Как пользоваться программой", style="CardTitle.TLabel"
        ).pack(anchor="w")

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
            "• Справа от графика находятся три панели: «Инструменты», «Виды» и «Анализ».\n"
            "• Пункт «Изображение» в верхнем меню сохраняет график как PNG.\n"
            "• Экспорт CSV и Excel в меню «Сохранить/экспорт» сохраняет таблицу измерений, а не картинку.\n\n"
            "ИНСТРУМЕНТЫ\n"
            "• «Курсор» — показывает координаты точки под мышью и пунктир до осей.\n"
            "• «Сброс меток» — удаляет все закреплённые метки курсора.\n"
            "• «🔍» — включает режим увеличения выбранной области.\n"
            "• «Сдвиг» — перемещает уже увеличенный участок графика.\n"
            "• «+» и «-» — быстро меняют масштаб.\n"
            "• «Сброс» — возвращает исходный вид графика.\n"
            "• «Авто» — подбирает автоматический масштаб по текущим данным.\n"
            "• «Точки» — показывает отдельные измерения точками.\n"
            "• «Линии» — обычный режим непрерывной кривой.\n"
            "• «Сглаж.» — сглаживает отображение кривой.\n\n"
            "АНАЛИЗ\n"
            "• «DTG» — показывает скорость изменения массы.\n"
            "• «Нормал.» — переводит массу в относительный масштаб.\n"
            "• «Маркеры» — считает Δm, Δm% и ΔT между точками A/B.\n"
            "• «Профиль нагрева» — строит эталонную линию нагрева по температурным данным.\n"
            "• Чтобы использовать профиль нагрева: откройте «Анализ», включите кнопку, дождитесь появления чёрной линии и затем сохраняйте PNG, если она нужна в файле.\n"
            "• «Стадии» — помогает выделить этапы процесса.\n"
            "• «Сводка» — открывает текстовый результат расчётов.\n\n"
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

    def show_tools_dialog(self) -> None:
        tools_window = tk.Toplevel(self)
        tools_window.title("Инструменты")
        tools_window.transient(self)
        tools_window.grab_set()
        tools_window.geometry(f"{int(760 * self.ui_scale)}x{int(620 * self.ui_scale)}")

        outer = ttk.Frame(tools_window, style="Card.TFrame", padding=self._pad(18, 18))
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="Инструменты и анализ графика", style="CardTitle.TLabel"
        ).pack(anchor="w")

        text = ScrolledText(outer, wrap="word", relief="flat")
        text.pack(fill="both", expand=True, pady=(self._pad_y(12), 0))
        text.insert(
            "end",
            "🔹 Курсор\n\n"
            "Показывает координаты под мышью\n"
            "Выводит X/Y у точки, рисует пунктир до осей и позволяет ставить якорные метки кликом.\n\n"
            "🔹 Сброс меток\n\n"
            "Очищает координатные метки\n"
            "Удаляет все ранее поставленные якоря курсора.\n\n"
            "🔹 Лупа / Сдвиг / Масштаб\n\n"
            "Управление видом графика\n"
            "Лупа выделяет область, сдвиг двигает увеличенный участок, кнопки + и - меняют масштаб, авто подбирает масштаб автоматически.\n\n"
            "🔹 Точки / Линии / Сглаживание\n\n"
            "Режимы отрисовки кривой\n"
            "Позволяют переключаться между точками, обычной линией и сглаженным отображением.\n\n"
            "🔹 DTG\n\n"
            "Скорость изменения массы (dM/dT)\n"
            "Показывает, где процесс идёт быстрее всего. Помогает найти стадии разложения.\n\n"
            "🔹 Нормализация\n\n"
            "Приведение массы к относительному виду (%)\n"
            "Позволяет сравнивать разные эксперименты независимо от начальной массы.\n\n"
            "🔹 Маркеры\n\n"
            "Измерение между двумя точками графика\n"
            "Показывает разницу массы, температуры и времени между точками A и B. Маркеры можно перетаскивать мышью по графику массы.\n\n"
            "🔹 Профиль нагрева\n\n"
            "Автоматическая эталонная линия нагрева\n"
            "Строит устойчивую к шуму чёрную линию по температурным данным, определяет участок разгона и выход на плато. Включите режим в панели «Анализ», дождитесь появления линии и сохраняйте PNG только в нужном состоянии экрана.\n\n"
            "🔹 Анализ стадий\n\n"
            "Автоматическое определение этапов процесса\n"
            "Находит начало и конец разложения и разбивает кривую на стадии.\n",
        )
        text.configure(state="disabled")
        self._apply_theme_to_toplevel(tools_window, text_widget=text)

    def show_about_dialog(self) -> None:
        about_window = tk.Toplevel(self)
        about_window.title("Об авторе")
        about_window.transient(self)
        about_window.grab_set()
        about_window.geometry(f"{int(560 * self.ui_scale)}x{int(340 * self.ui_scale)}")

        outer = ttk.Frame(about_window, style="Card.TFrame", padding=self._pad(18, 18))
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)

        ttk.Label(outer, text="DataFusion RT", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
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
            command=lambda: webbrowser.open(
                "https://github.com/Vanagandr111/DataFusion-RT"
            ),
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

        ttk.Button(
            outer, text="Закрыть", style="Accent.TButton", command=about_window.destroy
        ).grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(self._pad_y(18), 0),
        )
        self._apply_theme_to_toplevel(about_window)

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
        self.plotter.update(snapshot.record)
        self.mass_card.set_value(
            _format_value(snapshot.record.mass, 3),
            unit="g",
            subtitle="Текущее значение",
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
            self._format_card_timestamp(snapshot.record.timestamp),
            subtitle="Последняя запись",
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

    def _append_measurement_row(self, record) -> None:
        if not hasattr(self, "measurements_table"):
            return
        item_id = self.measurements_table.insert(
            "",
            "end",
            values=(
                self._format_table_timestamp(record.timestamp),
                _format_value(record.mass, 3),
                _format_value(record.furnace_pv, 1),
                _format_value(record.furnace_sv, 1),
            ),
        )
        self._table_timestamp_map[item_id] = record.timestamp
        max_rows = max(50, int(self.config_data.app.max_points_on_plot))
        children = self.measurements_table.get_children()
        excess = len(children) - max_rows
        if excess > 0:
            for item_id in children[:excess]:
                self._table_timestamp_map.pop(item_id, None)
                self.measurements_table.delete(item_id)
        children = self.measurements_table.get_children()
        if children:
            self.measurements_table.see(children[-1])

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
        if (
            hasattr(self, "settings_save_button")
            and self.settings_save_button.winfo_exists()
        ):
            self.settings_save_button.state(
                ["disabled"] if autosave_enabled else ["!disabled"]
            )
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
            self.config_data.furnace.driver = self._get_str("furnace.driver").lower()
            self.config_data.furnace.access_mode = "read_only"
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
                self.config_data.furnace.scale_factor = 0.1
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

        title_label = ttk.Label(panel, text=title, style="CardTitle.TLabel")
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

    def _update_plot_render_buttons(self) -> None:
        if not hasattr(self, "plot_lines_button"):
            return
        current = self.plotter.render_mode
        self._set_plot_button_selected(
            self.plot_lines_button, current == LivePlotter.RENDER_LINE
        )
        self._set_plot_button_selected(
            self.plot_points_button, current == LivePlotter.RENDER_POINTS
        )
        self._set_plot_button_selected(
            self.plot_smooth_button, current == LivePlotter.RENDER_SMOOTH
        )

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

    def set_plot_lines(self) -> None:
        self.plotter.set_render_mode(LivePlotter.RENDER_LINE)
        self._update_plot_render_buttons()
        self._update_calc_buttons()
        self._set_status("Включён обычный режим линий.", emit_log=False)

    def set_plot_points(self) -> None:
        self.plotter.set_render_mode(LivePlotter.RENDER_POINTS)
        self._update_plot_render_buttons()
        self._update_calc_buttons()
        self._set_status(
            "Включён режим точек для просмотра отдельных измерений.", emit_log=False
        )

    def set_plot_smooth(self) -> None:
        self.plotter.set_render_mode(LivePlotter.RENDER_SMOOTH)
        self._update_plot_render_buttons()
        self._update_calc_buttons()
        self._set_status("Включён сглаженный режим отображения.", emit_log=False)

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
                "Профиль нагрева включён: эталонная линия строится по температурным данным и обновляется автоматически.",
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
        summary = self.plotter.calculation_summary()
        message = (
            f"Δm: {summary['delta_mass']}\n"
            f"Δm%: {summary['delta_mass_percent']}\n"
            f"ΔT: {summary['delta_temperature']}\n"
            f"Δt: {summary['delta_time']}\n"
            f"Макс. DTG: {summary['max_dtg']}\n"
            f"Стадии: {summary['stage_range']}"
        )
        self.diag_status_var.set(message.replace("\n", " | "))
        window = tk.Toplevel(self)
        window.title("Сводка анализа")
        window.transient(self)
        window.grab_set()
        window.geometry(f"{int(760 * self.ui_scale)}x{int(620 * self.ui_scale)}")
        outer = ttk.Frame(window, style="Card.TFrame", padding=self._pad(16, 16))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Сводка анализа", style="CardTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text="Итоги по текущим данным графика и активным расчётам.",
            style="CardText.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(self._pad_y(4), self._pad_y(10)))

        metrics = ttk.Frame(outer, style="Card.TFrame")
        metrics.pack(fill="both", expand=True)
        for idx in range(2):
            metrics.grid_columnconfigure(idx, weight=1)

        card_specs = [
            (
                "Δm",
                summary["delta_mass"],
                "Потеря или прирост массы между точками A/B.",
            ),
            (
                "Δm%",
                summary["delta_mass_percent"],
                "То же изменение массы, но в процентах от базовой точки.",
            ),
            (
                "ΔT",
                summary["delta_temperature"],
                "Разница температур между выбранными точками или участком.",
            ),
            ("Δt", summary["delta_time"], "Временной интервал между точками A и B."),
            (
                "Макс. DTG",
                summary["max_dtg"],
                "Пиковая скорость изменения массы на текущем наборе данных.",
            ),
            (
                "Стадии",
                summary["stage_range"],
                "Найденный диапазон стадий процесса по текущему анализу.",
            ),
        ]

        for idx, (title, value, hint) in enumerate(card_specs):
            card = ttk.Frame(metrics, style="CardAlt.TFrame", padding=self._pad(12, 10))
            row = idx // 2
            column = idx % 2
            span = 2 if idx == len(card_specs) - 1 else 1
            padx = (0, self._pad_x(6)) if column == 0 else (self._pad_x(6), 0)
            if span == 2:
                padx = (0, 0)
            card.grid(
                row=row,
                column=column,
                columnspan=span,
                sticky="nsew",
                padx=padx,
                pady=(0, self._pad_y(10)),
            )
            ttk.Label(card, text=title, style="Subtitle.TLabel").pack(anchor="w")
            value_label = ttk.Label(
                card,
                text=str(value),
                style="CardTitle.TLabel",
                justify="left",
                wraplength=int(300 * self.ui_scale)
                if span == 1
                else int(640 * self.ui_scale),
            )
            value_label.configure(
                font=("Segoe UI Semibold", max(16, int(19 * self.ui_scale)))
            )
            value_label.pack(anchor="w", pady=(self._pad_y(4), self._pad_y(6)))
            hint_label = ttk.Label(
                card,
                text=hint,
                style="CardText.TLabel",
                justify="left",
                wraplength=int(300 * self.ui_scale)
                if span == 1
                else int(640 * self.ui_scale),
            )
            hint_label.pack(anchor="w")

        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", pady=(self._pad_y(6), 0))
        buttons.grid_columnconfigure(0, weight=1)
        buttons.grid_columnconfigure(1, weight=1)
        ttk.Button(
            buttons,
            text="Сохранить TXT",
            style="Soft.TButton",
            command=lambda: self._save_calc_summary_to_txt(message, parent=window),
        ).grid(row=0, column=0, sticky="ew", padx=(0, self._pad_x(6)))
        ttk.Button(
            buttons, text="Закрыть", style="Soft.TButton", command=window.destroy
        ).grid(row=0, column=1, sticky="ew", padx=(self._pad_x(6), 0))

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
            "Отрисовка графика поставлена на паузу."
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
        try:
            dt = datetime.fromisoformat(raw_timestamp)
        except ValueError:
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
            values[0] = self._format_table_timestamp(raw_timestamp)
            self.measurements_table.item(item_id, values=values)

    def _format_card_timestamp(self, raw_timestamp: str) -> str:
        try:
            dt = datetime.fromisoformat(raw_timestamp)
            return dt.strftime("%H:%M:%S")
        except ValueError:
            cleaned = raw_timestamp.replace("T", " ")
            if len(cleaned) >= 19:
                return cleaned[11:19]
            return cleaned

    def _reset_readouts(self) -> None:
        self.last_scale_connected = False
        self.last_furnace_connected = False
        self._last_scale_seen_at = 0.0
        self._last_furnace_seen_at = 0.0
        self.measurement_records.clear()
        self.mass_card.set_value("--", unit="g", subtitle="Ожидание данных")
        self.temp_card.set_value("--", unit="°C", subtitle="Камера")
        self.thermocouple_card.set_value("--", unit="°C", subtitle="Термопара")
        self.status_card.set_value("Ожидание", subtitle="Нажмите «Старт»")
        self.time_card.set_value("--", subtitle="Последняя запись")
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
        self.tare_button.state(
            ["!disabled"] if self._scale_actions_allowed() else ["disabled"]
        )
        self.zero_button.state(
            ["!disabled"] if self._scale_actions_allowed() else ["disabled"]
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
