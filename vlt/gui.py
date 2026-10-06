"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。
用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations
import argparse, queue, re, subprocess, threading, tkinter as tk, webbrowser
from pathlib import Path
from tkinter import ttk
from typing import Any
from . import __version__, crashlog, i18n, update_check
from .config import Direction, DEFAULT_CONFIG, load_config
from .config_io import _fmt_scalar, _yaml_set_in_text, _yaml_set_or_create, _write_config_text
from .i18n import t
from .paths import APP_DIR
# ── 重导出（保持 from vlt.gui import X 向后兼容）──
from . import ui_tk
from .ui_text import (SOURCE_LANGS, TARGET_LANGS, _is_unsupported_voice_err, _lang_key, _lang_label,
    _play_pcm_local as _play_pcm_local, _persist_provider, _source_name, _sponsor_qr_specs, _target_name, updater_env)
from .ui_theme import (COLOR_SRC_MINE, FONT_MAX, FONT_MIN, MAX_BUBBLES, PANEL, PANEL_H_MAX, PANEL_H_MIN,
    PANEL_W_MAX, PANEL_W_MIN, QIANWEN_SIGNUP_URL as QIANWEN_SIGNUP_URL, SETTINGS_MIN_H, SETTINGS_WIDTH,
    SPONSOR_QR_SIZE, SPONSOR_URL, SPONSORS, SRC_FONT_MIN, TAB_INSET_X, TEXT, TEXT_DIM, TEXT_MUTED)
from .ui_tk import _char_width_for, _combo_width, _int_fmt, apply_theme, combo_values, round_rect
from .ui_state import room_status_text, save_room_cfg
from .gui_selftest import run_self_test
from .glossary_text import _glossary_line_issues, _glossary_to_lines, _parse_glossary_lines
from .level_probe import LevelProbe
from . import gui_room, gui_voice, gui_audio, gui_update, gui_settings
from . import gui_desktop, gui_chat, gui_engine, gui_layout
from .gui_room import RoomCtx; from .gui_voice import VoiceCtx
from .gui_audio import AudioCtx; from .gui_desktop import DesktopCtx
from .gui_chat import ChatCtx; from .gui_engine import EngineCtx
ROOT = APP_DIR
FONT = ui_tk.FONT; FONT_SMALL = ui_tk.FONT_SMALL; FONT_META = ui_tk.FONT_META
FONT_UI = ui_tk.FONT_UI; FONT_STATUS = ui_tk.FONT_STATUS
FONT_BOLD_SM = ui_tk.FONT_BOLD_SM; FONT_BOLD_MD = ui_tk.FONT_BOLD_MD; FONT_BOLD_LG = ui_tk.FONT_BOLD_LG
def _yaml_write(p: Path, fn, *, err="保存"):
    if not p.exists(): return
    try: text = p.read_text(encoding="utf-8"); text = fn(text); _write_config_text(p, text)
    except Exception as exc: print(f"[gui] {err}失败：{exc}", flush=True)
# gui.py ↔ EngineCtx 双向同步的属性名
_GATE_HINT = "只有响度超过门限的声音才会被翻译；改完立刻生效（勾选「启用」后这里显示实时电平）"
_E2G = ["engines", "engine_dirs", "specs", "sinks", "pending_starts", "current",
    "auto_scroll", "closing", "overlay_out", "desktop_out", "desktop_dragging",
    "desktop_save_job", "desktop_alpha_touched", "start_job"]
_G2E = ["start_btn", "stop_btn", "direction_var", "chatbox_var", "overlay_var",
    "desktop_var", "vmic_var", "desktop_alpha_var", "desktop_alpha_lbl",
    "desktop_font_var", "desktop_srcfont_var", "desktop_w_var", "desktop_h_var", "desktop_drag_btn"]
_DELEGATE_MAP = {
    "_build_settings_dialog": gui_settings.build_settings_dialog,
    "_sync_settings_pages": gui_settings.sync_settings_pages,
    "_on_settings_wheel": gui_settings.on_settings_wheel,
    "_size_settings_window": gui_settings.size_settings_window,
    "_build_settings_general": gui_settings.build_settings_general,
    "_on_save_osc_port": gui_settings.on_save_osc_port,
    "_build_provider_section": gui_settings.build_provider_section,
    "_on_provider_change": gui_settings.on_provider_change,
    "_on_save_provider": gui_settings.on_save_provider,
    "_build_settings_audio": gui_settings.build_settings_audio,
    "_build_settings_wrist": gui_settings.build_settings_wrist,
    "_build_settings_desktop": gui_settings.build_settings_desktop,
    "_build_settings_glossary": gui_settings.build_settings_glossary,
    "_build_settings_room": gui_settings.build_settings_room,
    "_build_settings_about": gui_settings.build_settings_about,
    "_on_export_logs": gui_settings.on_export_logs,
    "_on_open_log_folder": gui_settings.on_open_log_folder,
    "_select_settings_page": gui_settings.select_settings_page,
    "_open_settings": gui_settings.open_settings,
    "_close_settings": gui_settings.close_settings,
    "_on_ui_lang_change": gui_settings.on_ui_lang_change,
    "_save_ui_language": gui_settings.save_ui_language,
    "_refresh_log_info": gui_settings.refresh_log_info,
    "_open_kofi": gui_update.open_kofi,
    "_open_sponsor": gui_update.open_sponsor,
    "_build_sponsor_dialog": gui_update.build_sponsor_dialog,
    "_close_sponsor": gui_update.close_sponsor,
    "_schedule_update_check": gui_update.schedule_update_check,
    "_run_update_check": gui_update.run_update_check,
    "_on_update_check_result": gui_update.on_update_check_result,
    "_show_update_dialog": gui_update.show_update_dialog,
    "_close_update_dialog": gui_update.close_update_dialog,
    "_open_release_page": gui_update.open_release_page,
    "_on_update_ignore": gui_update.on_update_ignore,
    "_on_update_later": gui_update.on_update_later,
    "_on_update_now": gui_update.on_update_now,
    "_show_download_window": gui_update.show_download_window,
    "_close_download_window": gui_update.close_download_window,
    "_open_download_page": gui_update.open_download_page,
    "_on_download_window_close": gui_update.on_download_window_close,
    "_start_download": gui_update.start_download,
    "_on_download_progress": gui_update.on_download_progress,
    "_on_download_done": gui_update.on_download_done,
    "_enter_download_done_state": gui_update.enter_download_done_state,
    "_on_download_error": gui_update.on_download_error,
    "_on_reload_clicked": gui_update.on_reload_clicked,
    "_on_postpone_clicked": gui_update.on_postpone_clicked,
    "_mark_update_pending": gui_update.mark_update_pending,
    "_relaunch_appimage": gui_update.relaunch_appimage,
    "_maybe_replace_on_exit": gui_update.maybe_replace_on_exit,
    "_offer_manual_download": gui_update.offer_manual_download,
    "_check_pending_update_at_startup": gui_update.check_pending_update_at_startup,
    "_schedule_version_changed_hint": gui_update.schedule_version_changed_hint,
    "_show_version_changed_hint": gui_update.show_version_changed_hint,
    "_close_updated_hint": gui_update.close_updated_hint,
    "_refresh_key_status": gui_update.refresh_key_status,
    "_open_qianwen_signup": gui_update.open_qianwen_signup,
    "_refresh_api_key_in_cfg": gui_update.refresh_api_key_in_cfg,
    "_on_save_key": gui_update.on_save_key,
    "_on_clear_key": gui_update.on_clear_key,
    "_build_ui": gui_layout.build_ui,
    "_fit_window_width": gui_layout.fit_window_width,
    "_set_window_icon": gui_layout.set_window_icon,
    "_apply_dark_titlebar": gui_layout._apply_dark_titlebar,
    "_divider": gui_layout._divider, "_vsep": gui_layout._vsep,
    "_attach_edit_menu": gui_layout._attach_edit_menu,
    "_build_controls": gui_layout.build_controls,
    "_build_output_row": gui_layout.build_output_row,
    "_indicator_kw": gui_layout._indicator_kw,
}
class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口。"""
    def __init__(self, headless: bool = False) -> None:
        self._headless = headless; self._q: queue.Queue = queue.Queue()
        self._engines: list = []; self._engine_dirs: list = []; self._specs: list = []; self._sinks: set = set(); self._pending_starts = 0
        self._overlay_out: Any = None; self._desktop_out: Any = None; self._desktop_dragging = False
        self._desktop_save_job: str | None = None; self._desktop_alpha_touched = False; self._start_job: str | None = None
        self._current: dict = {}; self._auto_scroll = True; self._stop_done_evt = threading.Event(); self._stop_done_evt.set()
        self._voice_ctx = VoiceCtx(); self._bubbles: list = []; self._stats: dict = {}; self._canvas_w = 600
        self._relayout_job: str | None = None; self._last_status_level = ""
        self._sponsor_win: tk.Toplevel | None = None; self._sponsor_imgs: list = []; self._sponsor_qr_labels: list = []
        self._update_check_done = False; self._update_check_running = False; self._update_snoozed = False
        self._update_win: tk.Toplevel | None = None; self._update_check_job: str | None = None
        self._update_checker = update_check.check_for_updates; self._update_downloader = update_check.download_and_verify
        self._dl_win: tk.Toplevel | None = None; self._dl_bar: ttk.Progressbar | None = None
        self._dl_text: ttk.Label | None = None; self._dl_note: ttk.Label | None = None; self._dl_btn_frame: ttk.Frame | None = None
        self._dl_info: update_check.ReleaseInfo | None = None; self._dl_new_exe: Path | None = None
        self._dl_cancel: threading.Event | None = None; self._dl_downloading = False
        self._dl_throttle_s = 0.1; self._dl_last_push = 0.0; self._dl_reload_btn: ttk.Button | None = None
        self._dl_postpone_btn: ttk.Button | None = None; self._dl_link: tk.Label | None = None
        self._update_pending_exit = False; self._update_pending_info: update_check.ReleaseInfo | None = None
        self._reload_started = False; self._closing = False
        self._updated_hint_win: tk.Toplevel | None = None; self._updated_hint_job: str | None = None
        self._settings_win: tk.Toplevel | None = None; self._settings_nb: ttk.Notebook | None = None
        self._settings_pages: list = []; self._settings_size = (SETTINGS_WIDTH, SETTINGS_MIN_H); self._settings_ctx = None
        self._mic_names: list = []; self._loopback_names: list = []; self._audio_out_names: list = []; self._device_scan_pending = False
        from .engine import LEVEL_FLOOR_DB
        self._gate_level_canvas: tk.Canvas | None = None; self._gate_level_lbl: ttk.Label | None = None
        self._gate_level_hold = LEVEL_FLOOR_DB; self._gate_level_tick = 0; self._gate_probe: Any = None; self._gate_save_job: str | None = None
        self._gate_hold_ms = 500.0; self._gate_preroll_ms = 250; self._audio_ctx = AudioCtx(); self._proxy = None
        self._names_holder = {"mic": self._mic_names, "loop": self._loopback_names, "out": self._audio_out_names}
        self._scan_holder = {"pending": False, "names": self._names_holder}
        self._gate_holder = {"probe": self._gate_probe, "save_job": self._gate_save_job,
            "level_hold": self._gate_level_hold, "hold_ms": self._gate_hold_ms,
            "preroll_ms": self._gate_preroll_ms, "engines": []}
        self._desktop_ctx = DesktopCtx(); self._chat_ctx = ChatCtx(); self._engine_ctx = EngineCtx()
        self._cfg = load_config(require_key=False)
        _sl = (self._cfg.ui or {}).get("lang"); i18n.set_language(_sl if _sl else i18n.detect_system_language())
        mine = self._cfg.directions.get("mine")
        self._lang_pair = {"source": mine.source_lang if mine else "zh",
                           "target": (mine.target_lang if mine else "en") or "en"}
        from .room.client import RoomClient; from .room.model import RoomConfig; from .room.publisher import SourcePublisher
        self._room: RoomClient | None = None
        self._room_cfg = RoomConfig.from_dict(self._cfg.room); self._publisher = SourcePublisher()
        self._room_status_next = 0.0; self._room_ctx = RoomCtx()
        gui_engine.bind_gui_callbacks(self._engine_ctx, self)
        if not headless: self._build_ui()
    def __getattr__(self, name):
        """薄壳委托：把 ~60 个简单方法代理到子模块。"""
        fn = _DELEGATE_MAP.get(name)
        if fn is not None:
            def _d(*a, _fn=fn, _s=self, **kw): return _fn(_s, *a, **kw)
            return _d
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
    # ── gui_voice 显式委托（函数签名需要 cfg/ctx 而非 gui）──
    def _effective_speech_voice(self) -> str: return gui_voice.effective_speech_voice(self._cfg)
    def _on_speech_voice_change(self) -> None: gui_voice.on_speech_voice_change(self._voice_ctx, self._cfg, self._engines, set_status=self._set_status)
    def _on_tts_voice_change(self) -> None: gui_voice.on_tts_voice_change(self._voice_ctx, self._cfg, set_status=self._set_status)
    def _set_voice_config(self, voice: str) -> None: gui_voice.set_voice_config(self._cfg, voice)
    def _set_tts_voice_config(self, voice: str) -> None: gui_voice.set_tts_voice_config(self._cfg, voice)
    def _glossary_scope(self) -> str: return gui_voice.glossary_scope(self._voice_ctx)
    def _glossary_scope_label(self, scope: str) -> str: return gui_voice.glossary_scope_label(self._voice_ctx, scope)
    def _read_glossary_from_disk(self, scope: str): return gui_voice.read_glossary_from_disk(self._cfg, scope)
    def _read_glossary_from_memory(self, scope: str): return gui_voice.read_glossary_from_memory(self._cfg, scope)
    def _glossary_hint_text(self, scope: str) -> str: return gui_voice.glossary_hint_text(self._voice_ctx, scope)
    def _on_glossary_scope_change(self) -> None: gui_voice.on_glossary_scope_change(self._voice_ctx, self._cfg)
    def _refresh_glossary_box(self) -> None: gui_voice.refresh_glossary_box(self._voice_ctx, self._cfg)
    def _resolve_api_key_safe(self) -> str: return gui_voice.resolve_api_key_safe(self._cfg)
    def _on_voice_preview_done(self, kind, voice, err) -> None: gui_voice.on_voice_preview_done(self._voice_ctx, kind, voice, err, set_status=self._set_status)
    def _refresh_voice_mode_btn(self) -> None:
        self._voice_ctx.proxy = self._proxy; gui_voice.refresh_voice_mode_btn(self._voice_ctx, self._engines)
    # ── 需要特殊处理的设置/更新/音色方法 ──
    def _settings_page(self, nb, title): return gui_settings.settings_page(self._settings_ctx, self, title)
    def _sync_page_scrollbar(self, canvas, inner, sb) -> None: gui_settings.sync_page_scrollbar(canvas, inner, sb)
    def _chatbox_port(self) -> int: return gui_settings.chatbox_port(self)
    def _selected_provider(self) -> str: return gui_settings.selected_provider(self)
    def _provider(self) -> str: return gui_settings.get_provider(self)
    def _provider_label(self) -> str: return gui_settings.provider_label(self)
    def _current_key_slot(self) -> str: return gui_settings.get_current_key_slot(self)
    def _signup_url(self) -> str: return gui_settings.signup_url(self)
    def _log_dir(self) -> Path: return gui_settings.log_dir(self)
    _SETTINGS_PAGE_TAB = gui_settings._SETTINGS_PAGE_TAB
    def _build_sponsor_list(self, flow) -> None: gui_settings.build_sponsor_list(flow, self)
    def _load_qr(self, parent, path): return gui_update._load_qr(self, parent, path)
    def _launch_updater_bat(self, bat_text) -> Path: return gui_update.launch_updater_bat(self, bat_text)
    def _on_save_glossary(self) -> None:
        gui_voice.on_save_glossary(self._voice_ctx, self._cfg, set_glossary_status=self._set_glossary_status, push_to_engines=self._push_glossary_to_engines, config_path=DEFAULT_CONFIG)
    def _save_glossary_config(self, path, mapping) -> bool: return gui_voice.save_glossary_config(path, mapping, config_path=DEFAULT_CONFIG)
    def _push_glossary_to_engines(self, scope, mapping) -> None: gui_voice.push_glossary_to_engines(self._engines, self._engine_dirs, self._voice_ctx, scope, mapping)
    def _set_glossary_status(self, text, *, warn=False) -> None: gui_voice.set_glossary_status(self._voice_ctx, text, warn=warn)
    @staticmethod
    def _glossary_scope_path(scope) -> list: return gui_voice.glossary_scope_path(scope)
    def _write_leaf(self, path, value, err_label, *, create=False) -> None:
        _yaml_write(DEFAULT_CONFIG, lambda t, _s=(_yaml_set_or_create if create else _yaml_set_in_text): _s(t, path, value), err=err_label)
    @property
    def _preview_busy(self) -> bool: return self._voice_ctx.preview_busy
    @_preview_busy.setter
    def _preview_busy(self, value) -> None: self._voice_ctx.preview_busy = value
    def _preview_voice(self, kind) -> None:
        gui_voice.preview_voice(self._voice_ctx, self._cfg, kind, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local)
    def _preview_worker(self, kind, voice, api_key, **kw) -> None: gui_voice.preview_worker(self._q, kind, voice, api_key, play_fn=_play_pcm_local, **kw)
    def _on_preview_speech_voice(self) -> None:
        gui_voice.on_preview_speech_voice(self._voice_ctx, self._cfg, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local)
    def _on_preview_tts_voice(self) -> None:
        gui_voice.on_preview_tts_voice(self._voice_ctx, self._cfg, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local)
    def _apply_theme(self) -> None: apply_theme(self._root)
    # ── 房间 ──
    def _build_room_row(self) -> None:
        row = gui_room.build_room_row(self._root, self._room_ctx, self._room_cfg)
        self._room_row = row; self._room_var = self._room_ctx.room_var; self._room_btn = self._room_ctx.room_btn
        self._room_status = self._room_ctx.room_status; self._room_btn.configure(command=self._on_room_button); self._refresh_room_btn()
    def _room_status_text(self) -> str: return room_status_text(self._room)
    def _refresh_room_status_label(self) -> None: gui_room.refresh_status_label(self._room_ctx, self._room)
    def _refresh_room_btn(self) -> None: gui_room.refresh_btn(self._room_ctx, self._room)
    def _on_room_button(self) -> None:
        if self._room is not None: self._room_var.set(False); self._on_room_toggle(); return
        self._sync_room_cfg_from_fields()
        if not self._room_cfg.room_code: self._set_status("warn", t("先在「设置 → 房间」里填房间码")); self._open_settings(page="room"); return
        self._room_var.set(True); self._on_room_toggle()
    def _on_room_toggle(self) -> None:
        self._sync_room_cfg_from_fields(); self._save_room_cfg()
        if self._room_var.get(): self._start_room()
        else: self._stop_room()
        self._refresh_room_status_label()
    def _on_room_field_change(self) -> None:
        was_on = self._room is not None; self._sync_room_cfg_from_fields(); self._save_room_cfg()
        if was_on and self._room_var.get(): self._stop_room(); self._start_room()
        self._refresh_room_status_label()
    def _on_room_generate(self) -> None: gui_room.on_room_generate(self, field_change_fn=self._on_room_field_change)
    def _sync_room_cfg_from_fields(self) -> None: self._room_cfg = gui_room.sync_room_cfg_from_fields(self, self._room_cfg)
    def _save_room_cfg(self) -> None: self._room_cfg = save_room_cfg(DEFAULT_CONFIG, self._room_cfg)
    def _start_room(self) -> None:
        if self._room is not None: return
        self._sync_room_cfg_from_fields()
        self._room = gui_room.start_room(self._room_cfg, self._publisher, on_message_fn=self._on_room_message, on_status_fn=self._on_room_status, set_status=self._set_status)
    def _stop_room(self) -> None: room = self._room; self._room = None; gui_room.stop_room(room, self._publisher)
    def _on_room_message(self, msg) -> None: gui_room.on_room_message(msg, self._room_cfg, self._q)
    def _on_room_status(self, text: str) -> None: gui_room.on_room_status(text, self._q)
    def _on_engine_text(self, who, source_id, src_text, tgt_text, is_final) -> None:
        self._q.put(("text", who, src_text, tgt_text, is_final)); self._publish_to_room(source_id, src_text, is_final)
    def _publish_to_room(self, source_id, src_text, is_final) -> None:
        gui_room.publish_to_room(self._room, self._room_cfg, self._publisher, source_id, src_text, is_final)
    # ── 手腕屏微调 ──
    def _ov_fn(self): return self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
    def _build_tune_page(self, body) -> None:
        gui_desktop.build_tune_page(body, self._desktop_ctx, self._ov_fn(), overlay_fn=self._ov_fn, settings_width=SETTINGS_WIDTH)
        for a in ("anchor_combo", "tracker_var", "tune_panel_w", "anchor_label_to_key", "tune_values",
                  "tune_vars", "tune_lbls", "tune_units", "tune_grid", "ov_save_job"):
            setattr(self, f"_{a}", getattr(self._desktop_ctx, a))
    def _build_tune_grid(self, grid, specs, label_w, cols):
        gui_desktop.build_tune_grid(grid, specs, label_w, cols, self._desktop_ctx, overlay_fn=self._ov_fn)
    def _build_desktop_tune_page(self, body) -> None:
        gui_desktop.build_desktop_tune_page(body, self._desktop_ctx, self._desktop_cfg())
        for a in ("desktop_font_var", "desktop_font_lbl", "desktop_srcfont_var", "desktop_srcfont_lbl",
                  "desktop_w_var", "desktop_w_lbl", "desktop_h_var", "desktop_h_lbl",
                  "desktop_alpha_var", "desktop_alpha_lbl", "desktop_drag_btn", "desktop_tuned"):
            setattr(self, f"_{a}", getattr(self._desktop_ctx, a))
    @staticmethod
    def _apply_desktop_slider(key, var, lbl, fmt, gui): gui_desktop.apply_desktop_slider(key, var, lbl, fmt, gui._desktop_ctx)
    def _on_desktop_font(self, _v=""): gui_desktop.on_desktop_font(self._desktop_ctx)
    def _on_desktop_srcfont(self, _v=""): gui_desktop.on_desktop_srcfont(self._desktop_ctx)
    def _on_desktop_width(self, _v=""): gui_desktop.on_desktop_width(self._desktop_ctx)
    def _on_desktop_height(self, _v=""): gui_desktop.on_desktop_height(self._desktop_ctx)
    def _current_anchor(self): return gui_desktop.current_anchor(self._desktop_ctx)
    def _load_anchor_offset(self, anchor): gui_desktop.load_anchor_offset(self._desktop_ctx, anchor, self._ov_fn())
    def _make_tune_handler(self, key, var, lbl, unit):
        return gui_desktop.make_tune_handler(key, var, lbl, unit, self._desktop_ctx, overlay_fn=self._ov_fn)
    def _on_anchor_change(self): gui_desktop.on_anchor_change(self._desktop_ctx, overlay_fn=self._ov_fn)
    def _schedule_overlay_save(self): gui_desktop.schedule_overlay_save(self._desktop_ctx, root=self._root, overlay_fn=self._ov_fn)
    def _save_overlay_cfg(self): gui_desktop.save_overlay_cfg(self._desktop_ctx, cfg=self._cfg, overlay_fn=self._ov_fn)
    # ── 聊天 / 输入 / 状态 ──
    def _build_chat(self):
        ctx = self._chat_ctx; gui_chat.build_chat(self._root, ctx, self._on_mousewheel)
        self._canvas = ctx.canvas; self._vsb = ctx.vsb
    def _build_input_row(self):
        ctx = self._chat_ctx; gui_chat.build_input_row(self._root, ctx, self._cfg, self._attach_edit_menu)
        self._text_var = ctx.text_var; self._text_entry = ctx.text_entry; self._send_btn = ctx.send_btn
    def _set_text_input_enabled(self, on): gui_chat.set_text_input_enabled(self._chat_ctx, on)
    def _on_text_enter(self, _event=None): return gui_chat.on_text_enter(self._chat_ctx, self._engines, self._engine_dirs, self._set_status)
    def _send_typed(self): gui_chat.send_typed(self._chat_ctx, self._engines, self._engine_dirs, self._set_status)
    def _build_status(self):
        gui_chat.build_status(self._root, self._chat_ctx)
        self._status_dot = self._chat_ctx.status_dot; self._status_label = self._chat_ctx.status_label; self._stats_label = self._chat_ctx.stats_label
    def _check_api_key(self) -> None:
        self._refresh_key_status()
        try:
            from .config import load_api_key; load_api_key(slot=self._current_key_slot())
        except SystemExit as e: self._set_status("error", str(e))
    def _on_canvas_scroll(self, first, last): gui_chat.on_canvas_scroll(self._chat_ctx, first, last); self._auto_scroll = self._chat_ctx.auto_scroll
    def _on_mousewheel(self, event): gui_chat.on_mousewheel(self._chat_ctx, event)
    def _on_canvas_configure(self, event=None): gui_chat.on_canvas_configure(self._chat_ctx, self._root, event); self._canvas_w = self._chat_ctx.canvas_w
    # ── 事件处理 ──
    def _on_direction_change(self) -> None: self._update_direction_langs(); self._save_ui_state()
    def _update_direction_langs(self) -> None:
        d = self._direction_var.get(); a, b = self._lang_pair["source"], self._lang_pair["target"]
        if d == "theirs": src_code, tgt_code = b, a or "zh"; src_langs = tgt_langs = TARGET_LANGS
        else: src_code, tgt_code = a, b; src_langs = SOURCE_LANGS; tgt_langs = TARGET_LANGS
        self._source_combo.configure(values=[_lang_label(k) for k in src_langs])
        self._target_combo.configure(values=[_lang_label(k) for k in tgt_langs])
        self._source_combo.set(_lang_label(_source_name(src_code))); self._target_combo.set(_lang_label(_target_name(tgt_code)))
    def _on_lang_change(self, _event=None) -> None:
        d = self._direction_var.get()
        src_shown = _lang_key(self._source_combo.get(), SOURCE_LANGS if d != "theirs" else TARGET_LANGS)
        tgt_shown = _lang_key(self._target_combo.get(), TARGET_LANGS)
        if d == "theirs": self._lang_pair["target"] = TARGET_LANGS.get(src_shown, self._lang_pair["target"]); self._lang_pair["source"] = TARGET_LANGS.get(tgt_shown)
        else: self._lang_pair["source"] = SOURCE_LANGS.get(src_shown); self._lang_pair["target"] = TARGET_LANGS.get(tgt_shown, self._lang_pair["target"])
        if self._lang_pair["source"] is None:
            self._set_status("info", t("已切换为{target} → 中文", target=_lang_label(_target_name(self._lang_pair["target"]))))
        self._save_lang_config(); self._push_lang_to_engines(); self._publisher.reset(); self._update_direction_langs()
    def _save_lang_config(self) -> None:
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        pairs = {"mine": {"source_lang": a, "target_lang": b}, "theirs": {"source_lang": b, "target_lang": a or "zh"}}
        def _fn(text):
            for name, langs in pairs.items():
                for key, val in langs.items(): text = _yaml_set_in_text(text, ["directions", name, key], _fmt_scalar(val))
            return text
        _yaml_write(DEFAULT_CONFIG, _fn, err="保存配置")
    def _save_ui_state(self) -> None:
        def _fn(text):
            if not re.search(r"^ui:", text, re.M): text = text.rstrip("\n") + "\n\n# 界面上次的选择\nui:\n"
            for kp, val in [("direction", self._direction_var.get()), ("chatbox", _fmt_scalar(bool(self._chatbox_var.get()))),
                            ("overlay", _fmt_scalar(bool(self._overlay_var.get()))), ("desktop_overlay", _fmt_scalar(bool(self._desktop_var.get())))]:
                text = _yaml_set_in_text(text, ["ui", kp], val)
            return text
        _yaml_write(DEFAULT_CONFIG, _fn, err="保存界面选择")
    def _save_audio_flag(self) -> None:
        want = bool(self._vmic_var.get())
        _yaml_write(DEFAULT_CONFIG, lambda t: _yaml_set_in_text(t, ["output", "audio", "enabled"], _fmt_scalar(want)), err="保存译音开关")
    def _push_lang_to_engines(self):
        ctx = self._engine_ctx; ctx.cfg = self._cfg; ctx.lang_pair = self._lang_pair; ctx.engines = self._engines; ctx.engine_dirs = self._engine_dirs
        gui_engine.push_lang_to_engines(ctx)
    # ── 引擎控制 ──
    def _start(self):
        self._sync_engine_ctx()
        need_key = gui_engine.start(self._engine_ctx)
        self._unsync_engine_ctx()
        if need_key:                       # 无 key 被拦下 → 自动弹设置窗引导填 key
            self._open_settings()
    def _start_engine(self, index): self._sync_engine_ctx(); gui_engine.start_engine(self._engine_ctx, index); self._unsync_engine_ctx()
    def _on_engine_status(self, lvl, msg, who): gui_engine.on_engine_status(self._q, lvl, msg, who)
    def _start_overlay(self, *, force=False):
        self._sync_engine_ctx(); r = gui_engine.start_overlay(self._engine_ctx, force=force); self._overlay_out = self._engine_ctx.overlay_out; self._unsync_engine_ctx(); return r
    def _on_overlay_toggle(self):
        if self._overlay_var.get():
            if self._start_overlay(force=True): self._set_status("info", t("手腕屏已开启（位置 / 字号等见「设置 → 手腕屏」）"))
            else: self._overlay_var.set(False); self._set_status("error", t("手腕屏没启动起来，已自动取消勾选（先把 SteamVR 打开，再勾一次即可）"))
        else: self._stop_overlay(); self._sinks.discard("overlay")
        self._save_ui_state()
    def _stop_overlay(self): self._engine_ctx.overlay_out = self._overlay_out; gui_engine._stop_overlay(self._engine_ctx); self._overlay_out = self._engine_ctx.overlay_out
    def _push_overlay(self, force=False): self._engine_ctx.overlay_out = self._overlay_out; self._engine_ctx.bubbles = self._bubbles; gui_engine.push_overlay(self._engine_ctx, force=force)
    def _desktop_cfg(self): return gui_engine.desktop_cfg(self._cfg)
    def _start_desktop(self, *, force=False):
        self._sync_engine_ctx(); r = gui_engine.start_desktop(self._engine_ctx, force=force); self._desktop_out = self._engine_ctx.desktop_out; self._unsync_engine_ctx(); return r
    def _on_desktop_toggle(self):
        if self._desktop_var.get():
            if self._start_desktop(force=True): self._set_status("info", t("桌面字幕已开启（拖到想要的位置；尺寸/字号/透明度见「设置 → 桌面字幕」）"))
            else: self._desktop_var.set(False); self._set_status("error", t("桌面字幕没启动起来，已自动取消勾选"))
        else: self._stop_desktop(); self._sinks.discard("desktop")
        self._save_ui_state()
    def _stop_desktop(self):
        self._sync_engine_ctx(); gui_engine.stop_desktop(self._engine_ctx); self._desktop_out = self._engine_ctx.desktop_out; self._desktop_dragging = False; self._unsync_engine_ctx()
    def _push_desktop(self, force=False): self._engine_ctx.desktop_out = self._desktop_out; self._engine_ctx.bubbles = self._bubbles; gui_engine.push_desktop(self._engine_ctx, force=force)
    def _on_desktop_alpha(self, _v=""):
        self._desktop_alpha_touched = True; a = float(self._desktop_alpha_var.get())
        lbl = getattr(self, "_desktop_alpha_lbl", None)
        if lbl is not None: lbl.configure(text=f"{a:.2f}")
        if self._desktop_out is not None: self._desktop_out.set_alpha(a); self._schedule_desktop_save()
    def _schedule_desktop_save(self):
        self._engine_ctx.desktop_out = self._desktop_out; self._engine_ctx.desktop_save_job = self._desktop_save_job; self._engine_ctx.root = self._root
        gui_engine.schedule_desktop_save(self._engine_ctx); self._desktop_save_job = self._engine_ctx.desktop_save_job
    def _save_desktop_cfg(self):
        self._sync_engine_ctx(); gui_engine.save_desktop_cfg(self._engine_ctx, config_path=DEFAULT_CONFIG); self._desktop_save_job = self._engine_ctx.desktop_save_job
    def _toggle_desktop_drag(self):
        self._sync_engine_ctx(); gui_engine.toggle_desktop_drag(self._engine_ctx); self._desktop_out = self._engine_ctx.desktop_out; self._desktop_dragging = self._engine_ctx.desktop_dragging
    def _stop(self): self._sync_engine_ctx(); gui_engine.stop(self._engine_ctx); self._unsync_engine_ctx()
    def _wait_stop_done(self, engines):
        self._engine_ctx.q = self._q; self._engine_ctx.stop_done_evt = self._stop_done_evt; self._engine_ctx.root = self._root; gui_engine.wait_stop_done(self._engine_ctx, engines)
    def _on_close(self): self._sync_engine_ctx(); gui_engine.on_close(self._engine_ctx)
    def _sync_engine_ctx(self):
        c = self._engine_ctx
        for n in _E2G: setattr(c, n, getattr(self, f"_{n}"))
        for n in _G2E: setattr(c, n, getattr(self, f"_{n}", None))
        c.lang_pair = self._lang_pair; c.bubbles = self._bubbles; c.q = self._q; c.root = self._root; c.cfg = self._cfg
        c.desktop_tuned = getattr(self, '_desktop_tuned', set())
    def _unsync_engine_ctx(self):
        c = self._engine_ctx
        for n in _E2G: setattr(self, f"_{n}", getattr(c, n))
    # ── 设备选择（→ gui_audio）──
    def _sync_audio_ctx(self) -> None: gui_audio.sync_from_gui(self._audio_ctx, self)
    def _start_device_scan(self) -> None:
        self._sync_audio_ctx(); gui_audio.start_device_scan(self._audio_ctx, self._root, self._headless, self._scan_holder); self._device_scan_pending = self._scan_holder["pending"]
    def _on_refresh_devices(self) -> None:
        self._sync_audio_ctx(); gui_audio.on_refresh_devices(self._audio_ctx, self._root, self._engines, self._headless, self._scan_holder)
    def _on_proxy_toggle(self) -> None:
        self._sync_audio_ctx(); self._audio_ctx.proxy_enabled = self._proxy_enabled_var.get()
        _yaml_write(DEFAULT_CONFIG, lambda t: _yaml_set_in_text(t, ["output", "audio", "proxy", "enabled"], _fmt_scalar(self._audio_ctx.proxy_enabled)), err="保存代理开关")
        if hasattr(self, '_restart_proxy'): self._restart_proxy()
        self._sync_proxy_controls_state()
    def _on_proxy_buffer_change(self, _event=None) -> None:
        if self._passthrough_spin is None: return
        try:
            pt = int(self._passthrough_var.get()); tr = int(self._translated_buf_var.get())
            pt_clamped = max(60, min(500, pt)); tr_clamped = max(50, min(2000, tr))
            if pt != pt_clamped: self._passthrough_var.set(pt_clamped)
            if tr != tr_clamped: self._translated_buf_var.set(tr_clamped)
            pt, tr = pt_clamped, tr_clamped
        except (ValueError, TypeError): return
        self._sync_audio_ctx()
        def _fn(t):
            t = _yaml_set_in_text(t, ["output", "audio", "proxy", "passthrough_buffer_ms"], _fmt_scalar(pt))
            return _yaml_set_in_text(t, ["output", "audio", "buffer_ms"], _fmt_scalar(tr))
        _yaml_write(DEFAULT_CONFIG, _fn, err="保存代理缓冲")
        if hasattr(self, '_restart_proxy'): self._restart_proxy()
    def _sync_proxy_controls_state(self) -> None:
        if self._proxy_check is None: return
        on = bool(self._proxy_enabled_var.get())
        if self._passthrough_spin is not None:
            try: self._passthrough_spin.configure(state=tk.NORMAL if on else tk.DISABLED)
            except Exception: pass
        if self._translated_spin is not None:
            try: self._translated_spin.configure(state=tk.NORMAL)
            except Exception: pass
        if hasattr(self, '_proxy_hint') and self._proxy_hint is not None:
            self._proxy_hint.configure(text="" if on else t("已关闭：回到旧行为（译音输出随翻译启停，主界面切换开关置灰）"))
    def _on_device_scan_result(self, mics, loops, outs) -> None:
        self._sync_audio_ctx(); gui_audio.on_device_scan_result(self._audio_ctx, self._cfg, mics, loops, outs, self._names_holder, self._scan_holder)
        self._mic_names = self._names_holder["mic"]; self._loopback_names = self._names_holder["loop"]; self._audio_out_names = self._names_holder["out"]
        self._device_scan_pending = self._scan_holder.get("pending", False)
    def _on_device_change(self, _event=None) -> None: self._sync_audio_ctx(); gui_audio.on_device_change(self._audio_ctx, self._cfg, self._names_holder)
    def _save_device_config(self, mic_name, loop_name, out_name) -> None: self._sync_audio_ctx(); gui_audio.save_device_config(self._audio_ctx, self._cfg, mic_name, loop_name, out_name)
    def _on_gate_change(self, _v=None) -> None:
        self._sync_audio_ctx(); gui_audio.on_gate_change(self._audio_ctx, self._cfg, self._root, self._engines, self._gate_holder); self._gate_probe = self._gate_holder["probe"]; self._gate_save_job = self._gate_holder["save_job"]
    def _apply_gate_live(self) -> None: self._sync_audio_ctx(); gui_audio.apply_gate_live(self._audio_ctx, self._engines)
    def _save_gate_cfg(self) -> None: self._sync_audio_ctx(); gui_audio.save_gate_cfg(self._audio_ctx, self._cfg, self._gate_holder); self._gate_save_job = self._gate_holder["save_job"]
    def _gate_probe_wanted(self) -> bool: self._sync_audio_ctx(); return gui_audio.gate_probe_wanted(self._audio_ctx, self._engines)
    def _sync_gate_level_probe(self) -> None: self._sync_audio_ctx(); gui_audio.sync_gate_level_probe(self._audio_ctx, self._cfg, self._gate_holder); self._gate_probe = self._gate_holder["probe"]
    def _stop_gate_probe(self) -> None: self._sync_audio_ctx(); gui_audio.stop_gate_probe(self._audio_ctx, self._gate_holder); self._gate_probe = self._gate_holder["probe"]
    def _gate_level_db(self): self._sync_audio_ctx(); return gui_audio.gate_level_db(self._audio_ctx, self._engines, self._gate_holder)
    def _refresh_gate_level(self) -> None:
        self._sync_audio_ctx(); gui_audio.refresh_gate_level(self._audio_ctx, self._engines, self._gate_holder); self._gate_level_hold = self._gate_holder.get("level_hold", 0)
    # ── 轮询 / 气泡 / 状态 ──
    def _poll(self):
        gui_chat.setup_poll_ctx(self._chat_ctx, self)
        _ov = [self._overlay_out]; _dov = [self._desktop_out]
        gui_chat.poll(self._chat_ctx, self._root, self._set_status, self._add_text, self._refresh_status, self._stats, [self._pending_starts], _ov, _dov)
        self._pending_starts = [self._pending_starts][0]; self._overlay_out = _ov[0]; self._desktop_out = _dov[0]
        c = self._chat_ctx
        self._room_status_next = c.room_status_next; self._gate_level_tick = c.gate_level_tick
        if c.engines_ref is not self._engines: self._engines = c.engines_ref; self._engine_dirs = c.engine_dirs_ref
    def _add_text(self, source, text, is_final, who="mine", label=""):
        gui_chat.add_text(self._chat_ctx, source, text, is_final, who, label)
        self._bubbles = self._chat_ctx.bubbles; self._current = self._chat_ctx.current; self._auto_scroll = self._chat_ctx.auto_scroll
    def _draw_bubble(self, b): return gui_chat.draw_bubble(self._chat_ctx, b)
    def _redraw_current(self, b): gui_chat.redraw_current(self._chat_ctx, b)
    def _trim(self): gui_chat.trim(self._chat_ctx)
    def _update_scrollregion(self): gui_chat.update_scrollregion(self._chat_ctx)
    def _redraw_all(self): gui_chat.redraw_all(self._chat_ctx)
    def _set_status(self, level, msg): gui_chat.set_status(self._chat_ctx, level, msg); self._last_status_level = self._chat_ctx.last_status_level
    def _refresh_status(self): gui_chat.refresh_status(self._chat_ctx, self._engines, getattr(self, '_direction_var', None), self._stats)
    def run_self_test(self) -> int: return run_self_test()
    def run_self_test_dual(self) -> int:
        from .gui_selftest import run_self_test_dual as _rst2; return _rst2(self)
def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 - 图形界面")
    ap.add_argument("--self-test", action="store_true", help="自动化验收（单方向）")
    ap.add_argument("--self-test-dual", action="store_true", help="双向同时验收")
    args = ap.parse_args()
    from .paths import ensure_app_dir, migrate_legacy_files
    migrate_legacy_files(); ensure_app_dir()
    crashlog.install(ROOT / "logs", "gui")
    crashlog.log_startup_info(f"gui {'--self-test-dual' if args.self_test_dual else args.self_test and '--self-test' or ''}")
    if args.self_test_dual: gui = TranslationGUI(headless=True); return gui.run_self_test_dual()
    if args.self_test: gui = TranslationGUI(headless=True); return gui.run_self_test()
    gui = TranslationGUI()
    crashlog.install_tk(gui._root)
    try: gui._root.mainloop()
    finally: crashlog.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
