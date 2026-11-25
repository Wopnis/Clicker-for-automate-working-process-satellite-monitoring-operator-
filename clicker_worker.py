# clicker_worker.py
import time
import random
import re
import os
import threading
import importlib
from datetime import datetime
from pathlib import Path

import pytz
import requests
from PyQt6.QtCore import QThread, pyqtSignal


def _load_playsound():
    for modname in ("playsound3", "playsound"):
        try:
            mod = importlib.import_module(modname)
            fn = getattr(mod, "playsound", None)
            if callable(fn):
                return fn
        except Exception:
            continue
    return None


playsound = _load_playsound()

BASE_DIR = Path(__file__).resolve().parent
ALERT_SOUND_PATH = BASE_DIR / "alert.mp3"


class ClickerWorker(QThread):
    log_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str)
    alarm_signal = pyqtSignal(str)
    stats_signal = pyqtSignal(str, int)
    patterns_stats_signal = pyqtSignal(dict)
    finished_signal = pyqtSignal()

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.paused = False
        self.stop_flag = False
        self._last_alert_time = 0

        self.mock = bool(config.get("mock", False))
        self.auth_only = bool(config.get("auth_only", False))

        self.alarm_patterns = self._compile(self.config.get("alarm_patterns", []))
        self.no_auto_close_patterns = self._compile(self.config.get("no_auto_close_patterns", []))
        self.false_alarm_patterns = self._compile(self.config.get("false_alarm_patterns", []))
        self.false_alarm_raw = [
            p.strip().lower() for p in self.config.get("false_alarm_patterns", []) if p.strip()
        ]

        # ---- общие счётчики ----
        self._processed = 0
        self._false = 0
        self._critical = 0
        self._outside = 0
        self._repeats = 0
        self._start_ts = None

        # тревоги, для которых уже засчитан total (не удваиваем)
        self._counted_total_keys = set()

        # тревоги, для которых уже засчитан closed/false/critical (не удваиваем)
        self._counted_action_keys = set()

        # тревоги, которые уже видели (для подсчёта повторов)
        self._seen_keys = set()

        # {pattern: {"total":N, "closed":N, "false":N, "critical":N,
        #            "outside":N, "repeats":N}}
        self._pattern_stats = {}

    # ---------- helpers ----------
    @staticmethod
    def _compile(items):
        compiled = []
        for s in items:
            try:
                compiled.append(re.compile(s, re.IGNORECASE))
            except re.error:
                pass
        return compiled

    def _log(self, level, msg):
        self.log_signal.emit(level, msg)

    def _emit_stats(self):
        self.stats_signal.emit("processed", self._processed)
        self.stats_signal.emit("false", self._false)
        self.stats_signal.emit("critical", self._critical)
        self.stats_signal.emit("outside", self._outside)
        self.stats_signal.emit("repeats", self._repeats)
        self.patterns_stats_signal.emit(
            {k: dict(v) for k, v in self._pattern_stats.items()}
        )

    def _find_matching_pattern(self, alarm_text: str):
        for p in self.alarm_patterns:
            if p.search(alarm_text):
                return p.pattern
        return None

    def _bump_pattern(self, pattern: str, kind: str, delta: int = 1):
        if not pattern:
            return
        entry = self._pattern_stats.setdefault(
            pattern,
            {"total": 0, "closed": 0, "false": 0,
             "critical": 0, "outside": 0, "repeats": 0}
        )
        entry[kind] = entry.get(kind, 0) + delta

    def get_current_time(self):
        return datetime.now(pytz.timezone("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S")

    # ---------- control ----------
    def pause(self):
        self.paused = True
        self._log("INFO", "[*] Скрипт поставлен на паузу.")

    def resume(self):
        self.paused = False
        self._log("INFO", "[*] Скрипт продолжил работу.")

    def stop(self):
        self.stop_flag = True
        self._log("INFO", "[*] Получена команда остановки.")

    # ---------- sound / telegram ----------
    def _play_sound(self):
        if playsound is None or not ALERT_SOUND_PATH.exists():
            return
        now = time.time()
        if now - self._last_alert_time > 5:
            try:
                playsound(str(ALERT_SOUND_PATH))
                self._last_alert_time = now
            except Exception as e:
                self._log("WARNING", f"[!] Не удалось воспроизвести звук: {e}")

    def _send_telegram(self, message: str):
        token = self.config.get("bot_token")
        chat_id = self.config.get("chat_id")
        if not token or not chat_id:
            self._log("WARNING", "[Telegram] Бот или chat_id не заданы.")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        try:
            r = requests.post(url, json=payload, timeout=10)
            res = r.json()
            if res.get("ok"):
                self._log("INFO", "[Telegram] Сообщение отправлено.")
            else:
                self._log("ERROR", f"[Telegram] Ошибка: {res.get('description')}")
        except Exception as e:
            self._log("ERROR", f"[Telegram] Исключение: {e}")

    def _is_false_alarm(self, text: str) -> bool:
        if not text:
            return False
        for p in self.false_alarm_patterns:
            try:
                if p.search(text):
                    return True
            except Exception:
                pass
        norm = re.sub(r"\s+", " ", text).strip().lower()
        for s in self.false_alarm_raw:
            if s and s in norm:
                return True
        return False

    # ==================== MOCK ====================
    def _run_mock(self):
        self._log("INFO", "[MOCK] Тестовый режим запущен. Браузер не используется.")
        self.status_signal.emit("MOCK: мониторинг")

        all_active = (
            list(self.config.get("alarm_patterns", []))
            + list(self.config.get("no_auto_close_patterns", []))
            + list(self.config.get("false_alarm_patterns", []))
        )
        if not all_active:
            all_active = ["тестовая тревога"]

        last_pattern = None
        while not self.stop_flag:
            if self.paused:
                time.sleep(1)
                continue

            if last_pattern and random.random() < 0.20:
                pattern = last_pattern
                contract = f"TEST-{random.randint(1000, 9999)}"
            else:
                pattern = random.choice(all_active)
                contract = f"TEST-{random.randint(1000, 9999)}"
                last_pattern = pattern

            is_no_close = any(p.search(pattern) for p in self.no_auto_close_patterns)
            is_false = any(p.search(pattern) for p in self.false_alarm_patterns)

            current_time = self.get_current_time()
            msg = f"[!] {current_time} Тревога: {pattern}, Договор: {contract}"
            self._log("INFO", msg)
            self.alarm_signal.emit(pattern)

            key = f"{pattern}|{contract}"
            is_repeat = key in self._seen_keys

            # --- total: засчитываем только если это новый key ---
            if not is_repeat:
                self._seen_keys.add(key)
                if key not in self._counted_total_keys:
                    self._counted_total_keys.add(key)
                    self._bump_pattern(pattern, "total")
            else:
                self._repeats += 1
                self._bump_pattern(pattern, "repeats")
                self._log("INFO", f"[MOCK] ПОВТОР тревоги: {pattern} (Договор: {contract})")

            if is_no_close:
                # --- critical: засчитываем один раз ---
                if key not in self._counted_action_keys:
                    self._counted_action_keys.add(key)
                    self._critical += 1
                    self._bump_pattern(pattern, "critical")
                    self._log(
                        "INFO",
                        f"[MOCK] КРИТИЧЕСКАЯ тревога (NO_AUTO_CLOSE) — "
                        f"уведомляю, НЕ закрываю: {pattern}"
                    )
                    threading.Thread(target=self._play_sound, daemon=True).start()
                    threading.Thread(
                        target=self._send_telegram,
                        args=(f"⚠️ [MOCK] КРИТИЧЕСКАЯ ТРЕВОГА\n{msg}",),
                        daemon=True,
                    ).start()
                else:
                    self._log("INFO", f"[MOCK] Повтор критической — только звук/уведомление.")
                    threading.Thread(target=self._play_sound, daemon=True).start()
            else:
                # ALARM — ВСЕГДА пытаемся закрыть, вне зависимости от повтора
                self._log("INFO", f"[MOCK] Нажимаю 'Взять в работу' для {pattern}")
                time.sleep(random.uniform(0.5, 1.2))

                # закрытие засчитываем только один раз для уникального key
                if key not in self._counted_action_keys:
                    self._counted_action_keys.add(key)
                    if is_false:
                        self._false += 1
                        self._bump_pattern(pattern, "false")
                        self._log("INFO", f"[MOCK] Закрываю как ЛОЖНУЮ тревогу: {pattern}")
                    else:
                        self._log("INFO", f"[MOCK] Закрываю обычным способом: {pattern}")
                    self._processed += 1
                    self._bump_pattern(pattern, "closed")
                else:
                    self._log("INFO", f"[MOCK] Повтор ALARM — закрыт, в счётчиках уже учтён.")

            self._emit_stats()
            time.sleep(random.uniform(3, 6))

        self._log("INFO", "[MOCK] Тестовый режим остановлен.")

    # ==================== BROWSER ACTIONS ====================
    def _login(self, playwright):
        self.status_signal.emit("Авторизация...")
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context()
        context.clear_cookies()
        page = context.new_page()
        page.goto("https://online.myavo.ru/#!/login")
        self._log("INFO", f"[DEBUG] Открыта страница: {page.url}")
        page.wait_for_selector("#username", timeout=10000)
        page.wait_for_selector("#password", timeout=10000)
        page.locator("#username").fill(self.config.get("username") or "")
        page.locator("#password").fill(self.config.get("password") or "")
        page.locator(".login_submit").click()
        page.wait_for_timeout(3000)
        try:
            page.get_by_role("button", name="Тревоги").click()
        except Exception:
            self._log("WARNING", "[!] Не удалось сразу нажать 'Тревоги'.")
        self._log("INFO", "[+] Авторизация выполнена.")
        return page, context, browser

    def _just_clicker(self, row):
        try:
            button = row.get_by_role("button", name=" Взять в работу")
            if button.is_visible(timeout=2000):
                button.click()
                self._log("INFO", "[+] Кнопка 'Взять в работу' нажата")
                time.sleep(random.uniform(0.8, 1.6))
                return True
            return False
        except Exception as e:
            self._log("ERROR", f"[!] Не удалось нажать 'Взять в работу': {e}")
            return False

    def _close_modal(self, page):
        try:
            if page.get_by_text("В работе", exact=True).is_visible(timeout=1000):
                page.get_by_text("В работе", exact=True).click()
                page.get_by_text("Закрыть тревогу").click()
                self._log("INFO", "[+] Модальное окно закрыто.")
                time.sleep(random.uniform(0.7, 1.4))
                return True
        except Exception as e:
            self._log("ERROR", f"[!] Ошибка закрытия: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    def _close_false_alarm(self, page):
        try:
            if page.get_by_text("Ложная тревога", exact=True).is_visible(timeout=1000):
                page.get_by_text("Ложная тревога", exact=True).click()
                page.get_by_text("Закрыть тревогу").click()
                self._log("INFO", "[+] Ложная тревога закрыта.")
                time.sleep(random.uniform(0.7, 1.4))
                return True
        except Exception as e:
            self._log("ERROR", f"[!] Ошибка закрытия ложной тревоги: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    # ==================== AUTH ONLY ====================
    def _run_auth_only(self, page):
        self._log("INFO", "[AUTH-ONLY] Режим 'Только авторизация' активирован.")
        self._log("INFO", "[AUTH-ONLY] Скрипт НЕ будет нажимать никакие кнопки.")
        self.status_signal.emit("AUTH-ONLY: сканирование")

        scan_deadline = time.time() + 30
        found_alarms = 0

        while time.time() < scan_deadline and not self.stop_flag:
            if self.paused:
                time.sleep(1)
                continue

            try:
                rows = page.locator("tbody tr").all()
            except Exception as e:
                self._log("WARNING", f"[AUTH-ONLY] Не удалось получить строки: {e}")
                time.sleep(2)
                continue

            current_found = 0
            for i, row in enumerate(rows):
                try:
                    cells = row.locator("td").all()
                    if len(cells) < 6:
                        continue
                    try:
                        alarm_text = cells[5].inner_text(timeout=1000).strip()
                    except Exception:
                        continue

                    matched = any(p.search(alarm_text) for p in self.alarm_patterns)
                    if not matched:
                        continue

                    try:
                        contract_text = cells[3].inner_text(timeout=1000).strip()
                    except Exception:
                        contract_text = ""

                    is_no_auto_close = any(
                        p.search(alarm_text) for p in self.no_auto_close_patterns
                    )
                    is_false = self._is_false_alarm(alarm_text)

                    tag = "КРИТИЧНАЯ (не закрывать)" if is_no_auto_close else \
                          "ЛОЖНАЯ" if is_false else "ОБЫЧНАЯ"

                    self._log(
                        "INFO",
                        f"[AUTH-ONLY] {alarm_text} "
                        f"(Договор: {contract_text or '—'}) → {tag}"
                    )
                    self.alarm_signal.emit(alarm_text)
                    current_found += 1
                except Exception as e:
                    self._log("WARNING", f"[AUTH-ONLY] [{i}] {e}")

            found_alarms = max(found_alarms, current_found)
            time.sleep(2)

        self._log(
            "INFO",
            f"[AUTH-ONLY] Сканирование завершено. "
            f"Обнаружено тревог за проход: {found_alarms}. Клики не производились."
        )

    # ==================== MAIN ====================
    def run(self):
        self._start_ts = time.time()
        self._processed = 0
        self._false = 0
        self._critical = 0
        self._outside = 0
        self._repeats = 0
        self._pattern_stats = {}
        self._seen_keys = set()
        self._counted_total_keys = set()
        self._counted_action_keys = set()
        self._emit_stats()

        if self.mock:
            try:
                self._run_mock()
            except Exception as e:
                self._log("ERROR", f"[MOCK] Исключение: {e}")
            finally:
                self.status_signal.emit("Остановлено")
                self.finished_signal.emit()
            return

        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            self._log("ERROR", f"[FATAL] Не удалось импортировать playwright: {e}")
            self.status_signal.emit("Остановлено")
            self.finished_signal.emit()
            return

        try:
            with sync_playwright() as p:
                page, context, browser = self._login(p)

                if self.auth_only:
                    self._run_auth_only(page)
                else:
                    self.status_signal.emit("Мониторинг...")
                    self._monitor(page)

                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self._log("ERROR", f"[FATAL] {e}")
        finally:
            self.status_signal.emit("Остановлено")
            self.finished_signal.emit()

    def _monitor(self, page):
        """Мониторинг таблицы тревог. Все ALARM-тревоги закрываются ВСЕГДА,
        даже если ранее уже были учтены. Счётчики инкрементируются один раз на уникальный key."""
        self._log("INFO", "Мониторинг запущен.")
        while not self.stop_flag:
            if self.paused:
                time.sleep(1)
                continue
            try:
                rows = page.locator("tbody tr").all()
            except Exception as e:
                self._log("WARNING", f"Не удалось получить строки: {e}")
                time.sleep(2)
                continue

            for i, row in enumerate(rows):
                if self.stop_flag:
                    break
                try:
                    cells = row.locator("td").all()
                    if len(cells) < 6:
                        continue
                    try:
                        alarm_text = cells[5].inner_text(timeout=1000).strip()
                    except Exception:
                        continue

                    matched = any(p.search(alarm_text) for p in self.alarm_patterns)
                    if not matched:
                        continue

                    is_no_auto_close = any(p.search(alarm_text) for p in self.no_auto_close_patterns)
                    matched_pattern = self._find_matching_pattern(alarm_text)

                    try:
                        contract_text = cells[3].inner_text(timeout=1000).strip()
                    except Exception:
                        contract_text = ""

                    contract_number = contract_text if contract_text else None
                    msg = f"[!] {self.get_current_time()} Тревога: {alarm_text}"
                    if contract_number:
                        msg += f", Договор: {contract_number}"
                    self._log("INFO", msg)
                    self.alarm_signal.emit(alarm_text)

                    key = f"{alarm_text}|{contract_number}"
                    is_repeat = key in self._seen_keys

                    # --- total: только один раз на уникальный key ---
                    if not is_repeat:
                        self._seen_keys.add(key)
                        if key not in self._counted_total_keys:
                            self._counted_total_keys.add(key)
                            self._bump_pattern(matched_pattern, "total")
                    else:
                        self._repeats += 1
                        self._bump_pattern(matched_pattern, "repeats")
                        self._log(
                            "INFO",
                            f"[ACTION] ПОВТОР тревоги (учтён в повторах): {alarm_text}"
                        )

                    if is_no_auto_close:
                        # Критические — НИКОГДА не закрываем.
                        # Уведомление / звук — только при первом появлении уникального key.
                        if key not in self._counted_action_keys:
                            self._counted_action_keys.add(key)
                            self._critical += 1
                            self._bump_pattern(matched_pattern, "critical")
                            self._emit_stats()
                            self._log(
                                "INFO",
                                f"[ACTION] КРИТИЧЕСКАЯ тревога (NO_AUTO_CLOSE) — "
                                f"уведомляю, НЕ закрываю: {alarm_text}"
                            )
                            threading.Thread(target=self._play_sound, daemon=True).start()
                            threading.Thread(
                                target=self._send_telegram,
                                args=(f"⚠️ КРИТИЧЕСКАЯ ТРЕВОГА\n{msg}",),
                                daemon=True,
                            ).start()
                        # если повтор — тихо, ничего не делаем
                        continue

                    # ALARM — ЗАКРЫВАЕМ ВСЕГДА, независимо от повтора
                    clicked = self._just_clicker(row)
                    if clicked:
                        # счётчики инкрементируем один раз на уникальный key
                        if key not in self._counted_action_keys:
                            self._counted_action_keys.add(key)
                            if self._is_false_alarm(alarm_text):
                                self._close_false_alarm(page)
                                self._false += 1
                                self._bump_pattern(matched_pattern, "false")
                            else:
                                self._close_modal(page)
                            self._processed += 1
                            self._bump_pattern(matched_pattern, "closed")
                        else:
                            # повтор — просто закрываем модалку, счётчики не трогаем
                            if self._is_false_alarm(alarm_text):
                                self._close_false_alarm(page)
                            else:
                                self._close_modal(page)
                            self._log(
                                "INFO",
                                f"[ACTION] Повтор ALARM — закрыт без изменения счётчиков: {alarm_text}"
                            )
                        self._emit_stats()
                    else:
                        # клик не удалось сделать — кнопка исчезла / оператор успел
                        if key not in self._counted_action_keys:
                            self._counted_action_keys.add(key)
                            self._outside += 1
                            self._bump_pattern(matched_pattern, "outside")
                            self._log(
                                "INFO",
                                f"[ACTION] Тревога закрыта вне кликера "
                                f"(оператор успел / кнопка недоступна): {alarm_text}"
                            )
                            self._emit_stats()
                except Exception as e:
                    self._log("WARNING", f"[{i}] Ошибка обработки строки: {e}")

            time.sleep(2)