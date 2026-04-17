# Совместимость Windows-сборок

## Коротко

Один и тот же EXE для:

- Windows 11
- Windows 7
- Windows XP

на текущем стекe сделать нельзя.

Причина:

- сборка сейчас делается на Python 3.14
- такой runtime не совместим с Windows 7
- Windows XP не поддерживается современными Python, PyInstaller, pandas, matplotlib

## Что поддерживается реально

### Современная сборка

- целевые системы: Windows 10 / 11
- сборка: `scripts\build_exe.bat modern`
- среда: текущая `.venv`

### Legacy-сборка для Windows 7

- целевая система: Windows 7
- установка toolchain: `scripts\install_win7.bat`
- сборка: `scripts\build_win7.bat`
- прямой x86 build script: `scripts\build_win7_x86.bat`
- обязательная среда: отдельный Python `3.8.x`
- текущий контур использует `Python 3.8.10 x86`
- результат:
  - папка: `dist-win7-x86/DataFusion-RT`
  - zip: `dist-win7-x86/DataFusion-RT-win7-x86.zip`

Рекомендуемая схема:

1. создать отдельную среду под Win7
2. поставить туда совместимые версии зависимостей
3. собирать Win7-бинарь отдельно

В проекте это уже автоматизировано:

1. `scripts\install_win7.bat`
2. `scripts\build_win7.bat`

Важно:

- запускать только папку `dist-win7-x86/DataFusion-RT` целиком
- не запускать `exe` прямо из zip
- не переносить один `exe` без соседних DLL и `base_library.zip`
- не запускать ничего из `build-win7-x86`, это временная папка сборки
- legacy пакет дополнительно кладёт рядом UCRT DLL:
  - `ucrtbase.dll`
  - `api-ms-win-crt-*.dll`
- в build добавлен `hidden-import secrets`, чтобы stdlib-модуль не терялся в legacy сборке
- для legacy `Win7` сборки из PyInstaller исключён `multiprocessing`, потому что приложению он не нужен, а его runtime hook тянет `_socket` и может падать на старом Win7 ещё до запуска GUI
- для legacy `Win7` сборки также исключены `pkg_resources` / `setuptools`, потому что их runtime hook тоже может тянуть `email -> socket -> _socket` и падать на старом Win7 ещё до старта интерфейса

## Что с Windows XP

Windows XP сейчас не поддерживается.

Для XP нужна отдельная legacy-ветка:

- старый Python
- старый PyInstaller
- старые версии `pandas`, `matplotlib`, `PyYAML`
- отдельное тестирование на XP

То есть XP нельзя закрыть простым переключением флага в текущем build-скрипте.

## Практическое правило

- `modern` build: для Win10/11
- `win7` build: отдельная сборка из Python 3.8.x x86
- `xp`: только отдельный legacy-проект или legacy-ветка
