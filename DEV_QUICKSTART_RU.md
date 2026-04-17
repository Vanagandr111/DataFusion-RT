# Краткая документация для разработчика

## Что это за проект

`DataFusion RT` это Windows-приложение на Python для:

- чтения массы с весов
- чтения температуры печи
- live-отображения данных в GUI
- экспорта данных

Важно:

- GUI это только верхний слой
- приборная логика уже вынесена в отдельное локальное ядро
- дальше новые программы лучше строить не через `app/ui.py`, а через `app/devices/*`

## Какие библиотеки используются

Из `requirements.txt`:

- `pyserial`
  - работа с COM-портами
- `pymodbus`
  - активное Modbus RTU чтение
- `matplotlib`
  - график
- `pandas`
  - табличные данные и экспорт
- `openpyxl`
  - экспорт в `xlsx`
- `PyYAML`
  - чтение/запись `config.yaml`
- `pyinstaller`
  - сборка `exe`

Из стандартной библиотеки активно используются:

- `tkinter`
  - GUI
- `logging`
  - логирование
- `threading`
  - acquisition loop
- `queue`
  - доставка snapshot из acquisition в UI
- `dataclasses`
  - конфиг и runtime модели

## Как устроен проект

### Основные слои

- `app/main.py`
  - точка входа
- `app/ui.py`
  - главное окно GUI
- `app/ui_support/*`
  - вынесенные части GUI
- `app/services/plotter.py`
  - график и интерактивность
- `app/services/acquisition.py`
  - orchestration цикла измерений
- `app/devices/*`
  - приборное ядро

### Где сейчас правильный вход для приборов

Главный новый слой:

- `app/devices/facade.py`

Он нужен как локальный внешний вход в приборное ядро.

Через него можно:

- делать `probe`
- создавать readers
- снимать текущее состояние связи
- получать текущие reading status

## Структура приборного ядра

### `app/devices/readers/*`

Конкретные reader-классы:

- весы
- печь active modbus
- печь passive dk518

### `app/devices/probe/*`

Короткие проверки доступности устройств.

### `app/devices/transport/*`

Низкоуровневое открытие:

- serial
- modbus client

### `app/devices/runtime/*`

Вспомогательная runtime-логика:

- factory-функции
- test mode значения
- helpers для acquisition

### `app/devices/models/*`

Общие модели приборного слоя:

- runtime state
- probe/status модели

## Что важно не ломать

Без отдельного решения не менять:

- механику passive DK518
- механику active modbus
- команды весов
- poll intervals
- connected semantics

Если нужен новый рефактор:

- сначала править `app/devices/*`
- потом при необходимости обновлять `app/services/*`

## Что такое `app/services/*` для приборов

Сейчас это в основном совместимые фасады.

То есть:

- старый импорт может жить дальше
- но основную логику нужно считать расположенной в `app/devices/*`

## Как писать новую программу на этом коде

Правильный путь:

1. загрузить `AppConfig`
2. создать `DeviceFacade`
3. использовать `probe_*`
4. создать нужные readers
5. читать данные без GUI

Пример скелета:

```python
import logging

from app.config import load_config
from app.devices.facade import DeviceFacade

logger = logging.getLogger("my-device-app")
config = load_config("config/config.yaml")
devices = DeviceFacade(config, logger=logger)

scale_probe = devices.probe_scale()
furnace_probe = devices.probe_furnace()

scale_reader = devices.create_scale_reader()
furnace_reader = devices.create_furnace_reader()

scale_status = devices.sample_scale(scale_reader)
furnace_status = devices.sample_furnace(furnace_reader)
```

## Какой тип новых программ лучше делать

Сейчас лучший следующий шаг:

- отдельный CLI
- или маленькое отдельное desktop-приложение

Поверх:

- `app/devices/facade.py`

Не лучший путь на этом этапе:

- HTTP API
- отдельный daemon
- IPC-архитектура

Это можно делать позже, когда локальное ядро стабилизируется.

## Как запускать проект

Установка:

```bat
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
```

Запуск:

```bat
python app\main.py --config config\config.yaml
```

Тесты:

```bat
python -m unittest
```

## Куда смотреть при проблемах

### Если ломается GUI

Смотреть:

- `app/ui.py`
- `app/ui_support/*`
- `app/services/plotter.py`

### Если ломается чтение приборов

Смотреть:

- `app/devices/readers/*`
- `app/devices/probe/*`
- `app/devices/runtime/*`
- `app/devices/facade.py`

### Если странное поведение протокола

Смотреть:

- `instruments/*`

Это важная лабораторная зона с проверочными инструментами и эталонными сценариями.

## Практическое правило для разработки

Если задача про:

- COM
- Modbus
- весы
- печь
- probe
- test mode приборов

то почти наверняка начинать надо с:

- `app/devices/*`

Если задача про:

- окна
- кнопки
- таблицу
- легенду
- графические панели

то начинать надо с:

- `app/ui.py`
- `app/ui_support/*`
- `app/services/plotter.py`
