#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Сетевой сторож — сидит в трее и сообщает, кто заходит на ваш компьютер.

Следит за сетевыми подключениями к вашему ПК (общие папки, скрытые
шары C$/D$, принтеры) через PowerShell Get-SmbSession / Get-SmbOpenFile.

Возможности:
  * уведомление в трее при каждом новом подключении (кто и с какого компьютера);
  * окно: кто подключён сейчас, какие файлы открыты, журнал событий;
  * журнал в SQLite: все подключения, отключения, запуски и ошибки пишутся
    в базу SmbWatchdog.db (рядом со скриптом; если папка только для чтения —
    в %LOCALAPPDATA%\SmbWatchdog). Смотреть — вкладки «История» и
    «Кто заходил» в окне программы;
  * тёмная тема (цвета сайта шпаргалок);
  * интервал опроса, пауза, автозапуск с Windows (при входе в Windows
    программа стартует сразу свёрнутой в трей, окно не появляется);
  * защита от второго экземпляра.

Запуск: двойной клик по файлу (расширение .pyw — окно консоли не появляется).
Зависимости: Python 3.8+ и PyQt6:
    py -m pip install PyQt6

ВАЖНО: список сетевых сеансов Windows отдаёт только с правами администратора.
Без них программа честно об этом скажет — в меню трея есть
«Перезапустить от администратора».
"""

import html
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback

from PyQt6.QtCore import QDateTime, QProcess, QSettings, Qt, QTimer
from PyQt6.QtGui import (QAction, QBrush, QColor, QFont, QIcon,
                         QLinearGradient, QPainter, QPalette, QPen, QPixmap)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                             QComboBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMainWindow, QMenu, QMessageBox,
                             QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
                             QSystemTrayIcon, QTabWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

# === Константы ===============================================================

APP_TITLE = "Сетевой сторож"
APP_NAME_REG = "SmbWatchdog"      # ASCII-имя для реестра и планировщика задач
ORG_NAME = "Maxim"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOCK_NAME = "SmbWatchdog-singleton"
DB_FILE_NAME = "SmbWatchdog.db"      # журнал в SQLite, рядом со скриптом
TRAY_FLAG = "--tray"                 # запуститься сразу в трей (автозапуск)

IS_WINDOWS = (sys.platform == "win32")
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # чтобы не мелькала консоль

# Палитра — цвета сайта шпаргалок (#1a1a1a / #3399ff / #00ffcc)
C_BG, C_TEXT = "#1a1a1a", "#f5f5f5"
C_ACCENT, C_ACCENT2 = "#3399ff", "#66ccff"
C_MINT, C_GRAY = "#00ffcc", "#999999"
C_WARN, C_ERR = "#ffb86c", "#ff6b6b"

PS_EXE = "powershell.exe"
PS_SCRIPT = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
try {
    $s = @(Get-SmbSession | ForEach-Object {
        [pscustomobject]@{
            id     = [string]$_.SessionId
            user   = [string]$_.ClientUserName
            comp   = [string]$_.ClientComputerName
            exists = [int]$_.SecondsExists
            idle   = [int]$_.SecondsIdle
        }
    })
    $f = @(Get-SmbOpenFile | ForEach-Object {
        [pscustomobject]@{
            id   = [string]$_.SessionId
            user = [string]$_.ClientUserName
            comp = [string]$_.ClientComputerName
            path = [string]$_.Path
        }
    })
    @{ ok = $true; s = $s; f = $f } | ConvertTo-Json -Compress -Depth 4
} catch {
    @{ ok = $false; err = [string]$_.Exception.Message } | ConvertTo-Json -Compress
}
'''
PS_ARGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-Command", PS_SCRIPT]


# === Вспомогательные функции ==================================================

def human_secs(n):
    """Секунды -> «42 с» / «7 мин» / «2 ч 05 мин»."""
    n = int(n or 0)
    if n < 60:
        return f"{n} с"
    if n < 3600:
        return f"{n // 60} мин"
    h, rem = divmod(n, 3600)
    return f"{h} ч {rem // 60:02d} мин"


def is_admin():
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def pythonw_exe():
    """Путь к pythonw.exe (без консольного окна), если найдётся."""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def script_path():
    return os.path.abspath(sys.argv[0])


def autostart_mode():
    """None / 'task' (планировщик) / 'reg' (реестр)."""
    if not IS_WINDOWS:
        return None
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", APP_NAME_REG],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            return "task"
    except OSError:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME_REG)
            return "reg"
    except OSError:
        pass
    return None


def set_autostart(on):
    """Включить/выключить автозапуск. Возвращает (ok, сообщение)."""
    if not IS_WINDOWS:
        return False, "Автозапуск работает только в Windows"
    # Сначала убираем всё, что было
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", APP_NAME_REG, "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME_REG)
    except OSError:
        pass
    if not on:
        return True, "выключен"

    cmd = f'"{pythonw_exe()}" "{script_path()}" {TRAY_FLAG}'
    if is_admin():
        # Задача планировщика: запуск от имени администратора без запроса UAC
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", APP_NAME_REG, "/TR", cmd,
             "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            return True, "задача планировщика (с правами администратора)"
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return False, "не удалось создать задачу: " + err

    # Без прав администратора — обычный автозапуск из реестра
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, APP_NAME_REG, 0, winreg.REG_SZ, cmd)
    return True, ("запись в реестре (без прав администратора список сеансов "
                  "не будет виден — перезапустите программу от администратора)")


OLD_SCRIPT_NAMES = ("smbwatchdog.pyw",)  # прежние имена файла (для миграции)


def _autostart_cmd_text():
    """Команда автозапуска из реестра или планировщика ('' если не найдена)."""
    if not IS_WINDOWS:
        return ""
    mode = autostart_mode()
    if mode == "reg":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                cmd, _ = winreg.QueryValueEx(k, APP_NAME_REG)
                return str(cmd)
        except OSError:
            return ""
    if mode == "task":
        # Ищем строку с командой: путь к .pyw — ASCII, поэтому переживает
        # любые кодировки локализованного вывода schtasks.
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", APP_NAME_REG, "/V", "/FO", "LIST"],
                capture_output=True, creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                out = r.stdout.decode("utf-8", "replace")
                for line in out.splitlines():
                    if ".pyw" in line.lower():
                        return line
        except OSError:
            pass
    return ""


def _autostart_needs_update():
    """True, если команду автозапуска надо переписать под текущий файл.

    Два повода: команда ещё без флага --tray (старая версия программы)
    или ссылается на прежнее имя файла — SmbWatchdog.pyw до переименования
    в main.pyw. Обновление происходит само при очередном запуске программы.
    """
    if not IS_WINDOWS:
        return False
    if autostart_mode() is None:
        return False
    cmd = _autostart_cmd_text().lower()
    if not cmd:
        return False
    if any(name in cmd for name in OLD_SCRIPT_NAMES):
        return True
    return TRAY_FLAG not in cmd


# === Журнал в SQLite ==========================================================

def script_dir():
    """Каталог, в котором лежит сам скрипт."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.path.dirname(os.path.abspath(sys.argv[0]))


def db_path():
    """Путь к базе журнала SQLite.

    Приоритет: переменная окружения SMBWATCHDOG_DB -> папка скрипта (если
    туда можно писать) -> %LOCALAPPDATA%\\SmbWatchdog.
    """
    env = os.environ.get("SMBWATCHDOG_DB")
    if env:
        return env
    here = script_dir()
    try:
        probe = os.path.join(here, ".write-test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return os.path.join(here, DB_FILE_NAME)
    except OSError:
        pass
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "SmbWatchdog")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, DB_FILE_NAME)


class Store:
    """Журнал событий в SQLite. Любая ошибка базы не роняет программу."""

    KIND_LABELS = {   # подписи для колонки «Событие»
        "connect": "подключение",
        "disconnect": "отключение",
        "already": "уже подключён",
        "start": "запуск",
        "stop": "выход",
        "pause": "пауза",
        "resume": "продолжение",
        "error": "ошибка",
        "info": "примечание",
    }

    def __init__(self, path):
        self.path = path
        self.db = None
        try:
            self.db = sqlite3.connect(path)
            self.db.execute("""CREATE TABLE IF NOT EXISTS events(
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                kind    TEXT NOT NULL,
                who     TEXT,
                comp    TEXT,
                details TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS visitors(
                ident      TEXT PRIMARY KEY,
                who        TEXT NOT NULL,
                comp       TEXT NOT NULL,
                visits     INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT,
                last_seen  TEXT)""")
            self.db.commit()
        except sqlite3.Error:
            self.db = None

    def add(self, kind, details="", who=None, comp=None):
        """Дописать событие; подключения также обновляют сводку visitors."""
        if not self.db:
            return
        ts = QDateTime.currentDateTime().toString("yyyy-MM-dd hh:mm:ss")
        try:
            self.db.execute(
                "INSERT INTO events(ts, kind, who, comp, details) "
                "VALUES(?, ?, ?, ?, ?)", (ts, kind, who, comp, details))
            if who and kind in ("connect", "already"):
                ident = f"{who}|{comp}"
                self.db.execute(
                    "INSERT OR IGNORE INTO visitors(ident, who, comp, visits,"
                    " first_seen, last_seen) VALUES(?, ?, ?, 0, ?, ?)",
                    (ident, who, comp, ts, ts))
                if kind == "connect":
                    self.db.execute(
                        "UPDATE visitors SET visits = visits + 1,"
                        " last_seen = ? WHERE ident = ?", (ts, ident))
                else:
                    self.db.execute(
                        "UPDATE visitors SET last_seen = ? WHERE ident = ?",
                        (ts, ident))
            self.db.commit()
        except sqlite3.Error:
            pass

    def events(self, kinds=None, search=None, limit=1000):
        """Последние события (новые — сверху), с фильтром и поиском."""
        if not self.db:
            return []
        q = "SELECT ts, kind, who, comp, details FROM events"
        where, args = [], []
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args += list(kinds)
        if search:
            where.append("(who LIKE ? OR comp LIKE ? OR details LIKE ?)")
            like = f"%{search}%"
            args += [like, like, like]
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        try:
            return self.db.execute(q, args).fetchall()
        except sqlite3.Error:
            return []

    def visitors(self):
        """Сводка «кто заходил»: сколько визитов, первый и последний раз."""
        if not self.db:
            return []
        try:
            return self.db.execute(
                "SELECT who, comp, visits, first_seen, last_seen "
                "FROM visitors ORDER BY last_seen DESC").fetchall()
        except sqlite3.Error:
            return []

    def clear(self):
        """Стереть всю историю (с уплотнением файла базы)."""
        if not self.db:
            return False
        try:
            self.db.execute("DELETE FROM events")
            self.db.execute("DELETE FROM visitors")
            self.db.commit()
            self.db.execute("VACUUM")
            return True
        except sqlite3.Error:
            return False


# Фильтры вкладки «История» — по порядку пунктов выпадающего списка
HISTORY_FILTERS = [
    None,                          # все события
    ("connect", "already"),        # только подключения
    ("disconnect",),               # только отключения
    ("start", "stop"),             # запуски и выходы
    ("error",),                    # ошибки
]

# === Оформление — по дизайн-системе сайта шпаргалок ===========================
# Фон #1a1a1a, текст #f5f5f5, синий акцент #3399ff, светло-синий #66ccff,
# мятный #00ffcc, шапки таблиц #333 с цианом #00ffff, линии #444.

APP_QSS = """
/* Окно и общий текст */
QMainWindow, QDialog { background-color: #1a1a1a; }
QWidget { color: #f5f5f5; font-family: \"Segoe UI\"; font-size: 10pt; }

/* Заголовки секций — как h2 сайта: синяя полоса слева */
QLabel#section {
    color: #66ccff;
    font-size: 10.5pt;
    font-weight: 600;
    padding: 1px 0 1px 10px;
    border-left: 4px solid #3399ff;
}

/* Таблицы */
QTableWidget {
    background-color: #202020;
    alternate-background-color: #262626;
    border: 1px solid #444;
    border-radius: 6px;
    gridline-color: #3a3a3a;
    selection-background-color: #2d5a8e;
    selection-color: #ffffff;
    outline: none;
}
QTableWidget::item { padding: 3px 8px; }
QTableWidget::item:hover { background-color: #24344a; }
QTableCornerButton::section { background-color: #333; border: none; }
QHeaderView::section {
    background-color: #333;
    color: #00ffff;
    border: none;
    border-right: 1px solid #444;
    border-bottom: 2px solid #3399ff;
    padding: 5px 8px;
    font-weight: 600;
}

/* Поля ввода — как блоки кода на сайте */
QPlainTextEdit, QLineEdit, QSpinBox, QComboBox {
    background-color: #202020;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 4px 6px;
    color: #f5f5f5;
    selection-background-color: #2d5a8e;
    selection-color: #ffffff;
}
QPlainTextEdit {
    font-family: Consolas, \"Courier New\", monospace;
    font-size: 9pt;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #3399ff; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #222222;
    border: 1px solid #444;
    selection-background-color: #3399ff;
    selection-color: #ffffff;
}
QSpinBox::up-button, QSpinBox::down-button {
    background-color: #2f2f2f;
    border: none;
    width: 18px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background-color: #3399ff; }

/* Чекбоксы */
QCheckBox { spacing: 8px; }
QCheckBox::indicator, QMenu::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid #555;
    border-radius: 3px;
    background-color: #202020;
}
QCheckBox::indicator:checked, QMenu::indicator:non-exclusive:checked {
    background-color: #3399ff;
    border-color: #66ccff;
}

/* Кнопки — при наведении синеют, как ссылки сайта */
QPushButton {
    background-color: #2f2f2f;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 6px 14px;
}
QPushButton:hover { background-color: #3399ff; border-color: #66ccff; color: #ffffff; }
QPushButton:pressed { background-color: #2470c0; }
QPushButton:disabled { color: #7a7a7a; }

/* Статусная строка и разделители панелей */
QStatusBar { background-color: #202020; border-top: 1px solid #444; color: #999999; }
QStatusBar::item { border: none; }
QSplitter::handle { background-color: #333333; }
QSplitter::handle:vertical { height: 3px; }
QSplitter::handle:hover { background-color: #3399ff; }

/* Вкладки журнала/истории */
QTabWidget::pane { border: none; }
QTabBar::tab {
    background-color: #242424;
    color: #999999;
    padding: 6px 16px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}
QTabBar::tab:selected { color: #3399ff; border-bottom: 2px solid #3399ff; }
QTabBar::tab:hover:!selected { color: #cccccc; }

/* Меню трея */
QMenu { background-color: #222222; border: 1px solid #444; padding: 4px; }
QMenu::item { padding: 6px 24px; border-radius: 4px; }
QMenu::item:selected { background-color: #3399ff; color: #ffffff; }
QMenu::separator { height: 1px; background-color: #444; margin: 4px 8px; }

/* Подсказки */
QToolTip { background-color: #333; color: #f5f5f5; border: 1px solid #555; padding: 4px; }

/* Полосы прокрутки */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #3d3d3d; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3399ff; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #3d3d3d; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #3399ff; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


def make_palette():
    """Тёмная палитра в стиле Fusion."""
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(C_BG))
    p.setColor(QPalette.ColorRole.WindowText, QColor(C_TEXT))
    p.setColor(QPalette.ColorRole.Base, QColor("#222222"))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor("#282828"))
    p.setColor(QPalette.ColorRole.Text, QColor(C_TEXT))
    p.setColor(QPalette.ColorRole.Button, QColor("#2f2f2f"))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(C_TEXT))
    p.setColor(QPalette.ColorRole.Highlight, QColor(C_ACCENT))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor("#333333"))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(C_TEXT))
    p.setColor(QPalette.ColorRole.Link, QColor(C_ACCENT2))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(C_GRAY))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,
               QColor("#7a7a7a"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
               QColor("#7a7a7a"))
    return p


def make_icon(count):
    """Значок в трей: число подключённых (0 — мятная точка почти вверху)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    grad = QLinearGradient(0, 0, 0, 64)
    grad.setColorAt(0.0, QColor("#26364d"))
    grad.setColorAt(1.0, QColor("#151515"))
    p.setBrush(QBrush(grad))
    p.setPen(QPen(QColor(C_ACCENT), 4))
    p.drawRoundedRect(5, 5, 54, 54, 15, 15)
    if count > 0:
        p.setPen(QColor(C_WARN))
        f = QFont("Segoe UI")
        f.setPointSize(34 if count < 10 else 26)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter,
                   "99" if count > 99 else str(count))
    else:
        # «Всё спокойно»: мятная точка почти у верхнего края. Рисуем кругом,
        # а не символом «•» — тот сидит у базовой линии шрифта и из-за
        # центрирования строки оказывается у самого низа значка.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C_MINT))
        p.drawEllipse(32 - 8, 12, 16, 16)    # центр (32, 20)
    p.end()
    return QIcon(pm)


# === Главное окно =============================================================

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.active = {}            # (user, comp) -> {exists, idle, id}
        self.files_by_ident = {}    # (user, comp) -> [пути]
        self._busy = False
        self._busy_since = 0.0
        self._first = True
        self._admin_warned = False
        self._hidden_once = False
        self._paused = False
        self._icon_count = -1

        self.settings = QSettings()

        # Журнал в SQLite: события и сводка «кто заходил»
        self.store = Store(db_path())

        self.setWindowTitle(f"{APP_TITLE} — кто заходит на компьютер")
        self.resize(920, 620)
        self.setWindowIcon(make_icon(0))

        self.build_ui()

        # Процесс опроса (PowerShell) — асинхронный, интерфейс не замирает
        self.proc = QProcess(self)
        self.proc.finished.connect(self.on_finished)
        self.proc.errorOccurred.connect(self.on_proc_error)

        # Таймер опроса
        interval = int(self.settings.value("interval", 3))
        interval = max(2, min(60, interval))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.setInterval(interval * 1000)
        self.timer.start()

        # Значок в трее
        self.tray = None
        self.act_pause = None
        self.act_notify = None
        self.act_autostart = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.build_tray()

        # Существующий автозапуск обновляем под текущий файл: путь main.pyw
        # (прежде файл назывался SmbWatchdog.pyw) и флаг --tray, чтобы при
        # входе в Windows стартовать сразу в свёрнутом виде. Задачу
        # планировщика можно переписать только с правами администратора —
        # без них попробуем ещё раз в одном из следующих запусков.
        if IS_WINDOWS and TRAY_FLAG not in sys.argv \
                and _autostart_needs_update():
            if autostart_mode() != "task" or is_admin():
                ok, _msg = set_autostart(True)
                if ok:
                    self.log("Автозапуск обновлён: новый путь (main.pyw) "
                             "и старт сразу в трей", C_MINT, kind="info")
                else:
                    self.log("Не удалось обновить команду автозапуска",
                             C_WARN, kind="error")

        # Записываем в журнал стартовое состояние
        if self.store.db is not None:
            self.log(f"Журнал SQLite: {self.store.path}", C_GRAY)
        else:
            self.log("База SQLite не открылась — журнал живёт только в окне",
                     C_WARN, kind="error")
        self.log(f"Запущен. Пользователь: {os.environ.get('USERNAME', '?')}"
                 f" · права администратора: {'есть' if is_admin() else 'НЕТ'}",
                 kind="start")

        QApplication.instance().aboutToQuit.connect(self.on_app_quit)

        QTimer.singleShot(300, self.poll)   # первая проверка — сразу

    # --- интерфейс ------------------------------------------------------------

    def build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.sess_table = self._table(
            ["Пользователь", "Компьютер", "Подключён с", "Бездействие",
             "Открыто файлов"])
        hh = self.sess_table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self._grouped("Подключённые сейчас", self.sess_table))

        self.files_table = self._table(
            ["Пользователь", "Компьютер", "Открытый файл"])
        self.files_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self._grouped("Открытые файлы", self.files_table))

        # Журнал (что происходит сейчас) + история из SQLite — во вкладках
        self.tabs = QTabWidget()
        self.tabs.addTab(self._journal_page(), "Журнал")
        self.tabs.addTab(self._history_page(), "История")
        self.tabs.addTab(self._visitors_page(), "Кто заходил")
        self.tabs.currentChanged.connect(self.on_tab_changed)
        splitter.addWidget(self._grouped("Журнал и история", self.tabs))

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 3)
        root.addWidget(splitter, 1)

        # Нижняя панель настроек
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Интервал опроса:"))
        self.spin = QSpinBox()
        self.spin.setRange(2, 60)
        self.spin.setSuffix(" с")
        self.spin.setValue(max(2, min(60, int(self.settings.value("interval", 3)))))
        self.spin.valueChanged.connect(self.set_interval)
        bar.addWidget(self.spin)

        self.chk_notify = QCheckBox("Уведомлять о новых подключениях")
        self.chk_notify.setChecked(
            self.settings.value("notify", True) in (True, "true", "True", 1, "1"))
        self.chk_notify.toggled.connect(
            lambda v: self.settings.setValue("notify", v))
        bar.addWidget(self.chk_notify)

        bar.addStretch(1)
        btn = QPushButton("Проверить сейчас")
        btn.clicked.connect(self.poll)
        bar.addWidget(btn)
        root.addLayout(bar)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Запуск…")

    @staticmethod
    def _table(cols):
        t = QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(24)
        return t

    @staticmethod
    def _grouped(title, widget):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        lab = QLabel(title)
        lab.setObjectName("section")
        v.addWidget(lab)
        v.addWidget(widget, 1)
        return box

    # --- вкладки журнала и истории -------------------------------------------

    def _journal_page(self):
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(600)
        return self.log_view

    def _history_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)

        bar = QHBoxLayout()
        self.hist_filter = QComboBox()
        self.hist_filter.addItems(
            ["все события", "только подключения", "только отключения",
             "запуски и выходы", "ошибки"])
        self.hist_filter.currentIndexChanged.connect(self.refresh_history)
        bar.addWidget(self.hist_filter)

        self.hist_search = QLineEdit()
        self.hist_search.setPlaceholderText(
            "поиск: пользователь, компьютер, текст…")
        self.hist_search.setClearButtonEnabled(True)
        self.hist_search.textChanged.connect(self.refresh_history)
        bar.addWidget(self.hist_search, 1)

        btn_open = QPushButton("Открыть базу")
        btn_open.setToolTip("Показать файл базы в Проводнике")
        btn_open.clicked.connect(self.open_db_folder)
        bar.addWidget(btn_open)

        btn_clear = QPushButton("Очистить историю")
        btn_clear.clicked.connect(self.clear_history)
        bar.addWidget(btn_clear)
        v.addLayout(bar)

        self.hist_table = self._table(
            ["Время", "Событие", "Пользователь", "Компьютер", "Подробности"])
        hh = self.hist_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.hist_table, 1)

        self.hist_caption = QLabel("")
        self.hist_caption.setStyleSheet("color: #999999; font-size: 9pt;")
        v.addWidget(self.hist_caption)
        return page

    def _visitors_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)
        self.vis_table = self._table(
            ["Пользователь", "Компьютер", "Визитов",
             "Первый раз замечен", "Последний раз"])
        hh = self.vis_table.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.vis_table, 1)
        note = QLabel(
            "Считаются подключения, замеченные при работающей программе. "
            "Полная история — на вкладке «История».")
        note.setStyleSheet("color: #999999; font-size: 9pt;")
        note.setWordWrap(True)
        v.addWidget(note)
        return page

    def on_tab_changed(self, idx):
        if idx == 1:
            self.refresh_history()
        elif idx == 2:
            self.refresh_visitors()

    def refresh_history(self, *_):
        idx = max(0, self.hist_filter.currentIndex())
        kinds = HISTORY_FILTERS[idx] if idx < len(HISTORY_FILTERS) else None
        search = self.hist_search.text().strip() or None
        rows = self.store.events(kinds=kinds, search=search)
        labels = Store.KIND_LABELS
        self.hist_table.setRowCount(len(rows))
        for r, (ts, kind, who, comp, details) in enumerate(rows):
            color = None
            if kind in ("connect", "already"):
                color = QColor(C_MINT)
            elif kind == "error":
                color = QColor(C_ERR)
            elif kind == "disconnect":
                color = QColor(C_GRAY)
            vals = [ts, labels.get(kind, kind), who or "—", comp or "—",
                    details or ""]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c in (0, 1):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if color is not None:
                    it.setForeground(color)
                self.hist_table.setItem(r, c, it)
        if self.store.db is None:
            self.hist_caption.setText("База SQLite недоступна")
        else:
            self.hist_caption.setText(
                f"База: {self.store.path} · показано записей: {len(rows)}")

    def refresh_visitors(self):
        rows = self.store.visitors()
        self.vis_table.setRowCount(len(rows))
        for r, (who, comp, visits, first, last) in enumerate(rows):
            vals = [who, comp, str(visits), first or "—", last or "—"]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 2:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.vis_table.setItem(r, c, it)

    def open_db_folder(self):
        """Показать файл базы в Проводнике."""
        if self.store.db is None:
            QMessageBox.warning(self, APP_TITLE,
                                "База SQLite не открыта — файл не создан.")
            return
        try:
            if IS_WINDOWS:
                subprocess.run(["explorer", "/select,", self.store.path],
                               creationflags=CREATE_NO_WINDOW, check=False)
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", self.store.path], check=False)
            else:
                subprocess.run(["xdg-open", os.path.dirname(self.store.path)],
                               check=False)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE,
                                f"Не удалось открыть папку базы: {e}")

    def clear_history(self):
        if self.store.db is None:
            QMessageBox.information(self, APP_TITLE,
                                    "База журнала недоступна.")
            return
        btn = QMessageBox.question(
            self, APP_TITLE,
            "Удалить всю историю подключений из базы?\n"
            f"{self.store.path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if btn != QMessageBox.StandardButton.Yes:
            return
        self.store.clear()
        self.refresh_history()
        self.refresh_visitors()
        self.log("История в базе очищена", C_WARN, kind="info")

    def build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(0), self)
        self.tray.setToolTip(f"{APP_TITLE}: запускаюсь…")

        m = QMenu(self)

        act_show = QAction("Открыть окно", self)
        act_show.triggered.connect(self.show_raise)
        m.addAction(act_show)

        m.addSeparator()

        self.act_pause = QAction("Пауза опроса", self)
        self.act_pause.setCheckable(True)
        self.act_pause.toggled.connect(self.set_paused)
        m.addAction(self.act_pause)

        self.act_notify = QAction("Уведомлять о подключениях", self)
        self.act_notify.setCheckable(True)
        self.act_notify.setChecked(self.chk_notify.isChecked())
        self.act_notify.toggled.connect(self.chk_notify.setChecked)
        self.chk_notify.toggled.connect(self.act_notify.setChecked)
        m.addAction(self.act_notify)

        self.act_autostart = QAction("Автозапуск с Windows", self)
        self.act_autostart.setCheckable(True)
        self.act_autostart.setChecked(autostart_mode() is not None)
        self.act_autostart.toggled.connect(self.on_autostart_toggle)
        m.addAction(self.act_autostart)

        m.addSeparator()

        act_now = QAction("Проверить сейчас", self)
        act_now.triggered.connect(self.poll)
        m.addAction(act_now)

        if not is_admin():
            act_admin = QAction("Перезапустить от администратора", self)
            act_admin.triggered.connect(self.relaunch_admin)
            m.addAction(act_admin)

        m.addSeparator()

        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(QApplication.instance().quit)
        m.addAction(act_quit)

        self.tray.setContextMenu(m)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.messageClicked.connect(self.show_raise)
        self.tray.show()

    # --- опрос ------------------------------------------------------------------

    def poll(self):
        if self._busy:
            # Зависший опрос (более 60 с) прибиваем
            if time.monotonic() - self._busy_since > 60:
                self.proc.kill()
            return
        self._busy = True
        self._busy_since = time.monotonic()
        self.proc.start(PS_EXE, PS_ARGS)

    def on_proc_error(self, err):
        self._busy = False
        if err == QProcess.ProcessError.FailedToStart:
            self.log("Не удалось запустить powershell.exe", C_ERR,
                     kind="error")
            self.statusBar().showMessage("Ошибка: PowerShell не найден")

    def on_finished(self, *_):
        self._busy = False
        raw = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace").strip()
        if not raw:
            self.log("PowerShell вернул пустой ответ", C_ERR, kind="error")
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            self.log("Не удалось разобрать ответ PowerShell", C_ERR,
                     kind="error")
            return

        if not obj.get("ok"):
            err = str(obj.get("err") or "неизвестная ошибка")
            low = err.lower()
            if ("отказано" in low or "denied" in low or "access" in low) \
                    and not is_admin():
                self.log(f"Нет прав на просмотр сеансов: {err}", C_ERR,
                         kind="error")
                if not self._admin_warned:
                    self._admin_warned = True
                    self.log("Перезапустите программу от имени администратора "
                             "(меню в трее)", C_WARN)
                    QMessageBox.warning(
                        self, APP_TITLE,
                        "Для просмотра сетевых подключений нужны права "
                        "администратора.\n\nВыберите в меню трея «Перезапустить "
                        "от администратора».")
                self.statusBar().showMessage(
                    "Нет прав администратора — список сеансов недоступен")
            else:
                self.log(f"Ошибка PowerShell: {err}", C_ERR, kind="error")
                self.statusBar().showMessage(f"Ошибка: {err[:120]}")
            return

        sessions = obj.get("s") or []
        files = obj.get("f") or []
        if isinstance(sessions, dict):
            sessions = [sessions]
        if isinstance(files, dict):
            files = [files]
        self.apply_snapshot(sessions, files)

    def apply_snapshot(self, sessions, files):
        current = {}
        for s in sessions:
            user = (s.get("user") or "").strip() or "—"
            comp = (s.get("comp") or "").replace("\\", "").strip() or "—"
            current[(user, comp)] = {
                "exists": int(s.get("exists") or 0),
                "idle": int(s.get("idle") or 0),
                "id": str(s.get("id") or ""),
            }

        sess_by_id = {v["id"]: k for k, v in current.items()}
        files_by_ident = {}
        for f in files:
            ident = sess_by_id.get(str(f.get("id") or ""))
            if ident is None:
                u = (f.get("user") or "").strip() or "—"
                c = (f.get("comp") or "").replace("\\", "").strip() or "—"
                ident = (u, c)
            files_by_ident.setdefault(ident, []).append(f.get("path") or "?")

        new = [k for k in current if k not in self.active]
        gone = [k for k in self.active if k not in current]

        if self._first:
            self._first = False
            if current:
                who = "; ".join(f"{u} ({c})" for u, c in current)
                self.log(f"При запуске уже подключены ({len(current)}): {who}",
                         C_MINT)
                for u, c in current:
                    self.store.add("already", "подключён на момент запуска",
                                   u, c)
                lines = [f"{u} — компьютер {c}" for u, c in current]
                self.notify(f"Уже подключены ({len(current)})",
                            "\n".join(lines[:10]))
            else:
                self.log("Старт: подключений нет")
        else:
            if new:
                lines = [f"{u} — компьютер {c}" for u, c in new]
                self.log("ПОДКЛЮЧИЛИСЬ: " + "; ".join(lines), C_MINT)
                for u, c in new:
                    n = len(files_by_ident.get((u, c), []))
                    self.store.add("connect",
                                   f"открыто файлов: {n}" if n else "",
                                   u, c)
                self.notify("К вам подключились" if len(new) > 1
                            else "К вам подключился", "\n".join(lines[:10]))
            for u, c in gone:
                self.log(f"Отключился: {u} ({c})", C_GRAY)
                self.store.add("disconnect", "", u, c)

        self.active = current
        self.files_by_ident = files_by_ident
        self.refresh_tables()
        self.refresh_tray()
        self.statusBar().showMessage(
            f"Последняя проверка: "
            f"{QDateTime.currentDateTime().toString('HH:mm:ss')}"
            f" · сеансов: {len(current)} · открытых файлов: {len(files)}")

    # --- отображение -------------------------------------------------------------

    def refresh_tables(self):
        idents = sorted(self.active)
        self.sess_table.setRowCount(len(idents))
        for row, ident in enumerate(idents):
            user, comp = ident
            d = self.active[ident]
            paths = self.files_by_ident.get(ident, [])
            since = QDateTime.currentDateTime().addSecs(-max(0, d["exists"]))
            fmt = "dd.MM hh:mm" if d["exists"] >= 86400 else "hh:mm"
            vals = [user, comp, since.toString(fmt),
                    human_secs(d["idle"]), str(len(paths))]
            for col, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if col >= 2:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 4 and paths:
                    it.setToolTip("\n".join(paths[:20]))
                self.sess_table.setItem(row, col, it)

        rows = []
        for ident in idents:
            for path in self.files_by_ident.get(ident, []):
                rows.append((ident[0], ident[1], path))
        self.files_table.setRowCount(len(rows))
        for r, (u, c, path) in enumerate(rows):
            self.files_table.setItem(r, 0, QTableWidgetItem(u))
            self.files_table.setItem(r, 1, QTableWidgetItem(c))
            self.files_table.setItem(r, 2, QTableWidgetItem(path))

    def refresh_tray(self):
        if self.tray is None:
            return
        n = len(self.active)
        if n != self._icon_count:
            self._icon_count = n
            self.tray.setIcon(make_icon(n))
        self.tray.setToolTip(f"{APP_TITLE}: подключений — {n}")

    def notify(self, title, text):
        if self.tray is None or not self.chk_notify.isChecked():
            return
        if QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, text,
                                  QSystemTrayIcon.MessageIcon.Information, 5000)

    def log(self, text, color=None, kind=None, who=None, comp=None):
        ts = QDateTime.currentDateTime().toString("HH:mm:ss")
        safe = html.escape(text)
        if color:
            self.log_view.appendHtml(
                f'<span style="color:{color}">[{ts}] {safe}</span>')
        else:
            self.log_view.appendHtml(f"[{ts}] {safe}")
        if kind:
            self.store.add(kind, text, who, comp)

    # --- действия ------------------------------------------------------------------

    def set_interval(self, secs):
        secs = max(2, min(60, int(secs)))
        self.timer.setInterval(secs * 1000)
        if not self._paused:
            self.timer.start()
        self.settings.setValue("interval", secs)

    def set_paused(self, on):
        self._paused = bool(on)
        if self._paused:
            self.timer.stop()
            self.log("Опрос поставлен на паузу", C_GRAY, kind="pause")
        else:
            self.timer.start()
            self.log("Опрос возобновлён", C_GRAY, kind="resume")

    def on_autostart_toggle(self, on):
        ok, msg = set_autostart(bool(on))
        self.log(f"Автозапуск: {msg}",
                 C_MINT if ok else C_ERR, kind="info")
        if not ok and self.act_autostart is not None:
            self.act_autostart.blockSignals(True)
            self.act_autostart.setChecked(not on)
            self.act_autostart.blockSignals(False)

    def relaunch_admin(self):
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", pythonw_exe(), f'"{script_path()}"', None, 1)
            if rc > 32:
                QApplication.instance().quit()
            else:
                QMessageBox.warning(self, APP_TITLE,
                                    "Перезапуск не удался (UAC отклонён?)")
        except Exception as e:
            QMessageBox.warning(self, APP_TITLE, f"Перезапуск не удался: {e}")

    def show_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_raise()

    def start_in_tray(self):
        """Запуск без показа окна — сразу в трей (автозапуск Windows)."""
        self._hidden_once = True   # подсказку «программа в трее» уже не показываем
        if self.tray is not None and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(
                APP_TITLE,
                "Сетевой сторож запущен и следит за подключениями.",
                QSystemTrayIcon.MessageIcon.Information, 3000)

    def on_app_quit(self):
        """Записать в базу событие выхода (вызывается при завершении)."""
        self.store.add("stop",
                       f"сеансов на момент выхода: {len(self.active)}")

    def closeEvent(self, event):
        # Крестик сворачивает в трей (если трей есть), а не завершает программу
        if self.tray is not None:
            event.ignore()
            self.hide()
            if not self._hidden_once:
                self._hidden_once = True
                if QSystemTrayIcon.supportsMessages():
                    self.tray.showMessage(
                        APP_TITLE,
                        "Программа осталась в трее и продолжает следить "
                        "за подключениями.",
                        QSystemTrayIcon.MessageIcon.Information, 4000)
        else:
            event.accept()


# === Точка входа ==============================================================

def _excepthook(etype, val, tb):
    """Ошибки не должны пропадать молча (.pyw не имеет консоли)."""
    try:
        QMessageBox.critical(
            None, f"{APP_TITLE} — ошибка",
            "".join(traceback.format_exception(etype, val, tb))[-1500:])
    except Exception:
        pass


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME_REG)
    app.setStyle("Fusion")
    app.setPalette(make_palette())
    app.setStyleSheet(APP_QSS)
    app.setQuitOnLastWindowClosed(False)
    sys.excepthook = _excepthook

    # Защита от второго экземпляра
    probe = QLocalSocket()
    probe.connectToServer(LOCK_NAME)
    if probe.waitForConnected(200):
        QMessageBox.warning(None, APP_TITLE,
                            "Сетевой сторож уже запущен — он в трее.")
        return 1
    QLocalServer.removeServer(LOCK_NAME)
    server = QLocalServer()          # noqa: F841 — держим ссылку живой
    server.listen(LOCK_NAME)

    win = MainWindow()
    if TRAY_FLAG in sys.argv and win.tray is not None:
        win.start_in_tray()      # автозапуск: окно не показываем, сидим в трее
    else:
        win.show()

    if "--smoke" in sys.argv:       # скрытый режим самопроверки
        QTimer.singleShot(2500, app.quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
