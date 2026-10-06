#!/usr/bin/env python3
"""Transform gui.py: replace large method bodies with thin shell delegates."""
import re

src = "e:/workspace/python/vrchat-livetranslate/vlt/gui.py"
with open(src, encoding="utf-8") as f:
    text = f.read()

# We'll work line-by-line for precise section replacement
lines = text.split("\n")

def find_line(pattern, start=0):
    """Find first line matching pattern after start."""
    for i in range(start, len(lines)):
        if re.search(pattern, lines[i]):
            return i
    return -1

def replace_section(start_marker, end_marker, replacement):
    """Replace lines between start_marker (inclusive) and end_marker (exclusive)."""
    global lines
    s = find_line(start_marker)
    e = find_line(end_marker, s + 1)
    if s < 0 or e < 0:
        print(f"WARN: Could not find markers: {start_marker!r} / {end_marker!r}")
        return
    lines = lines[:s] + replacement.split("\n") + lines[e:]
    print(f"Replaced lines {s}-{e-1} ({e-s} lines → {len(replacement.split(chr(10)))} lines)")

# ================================================================
# 1. Replace tune page callback methods (keep build methods, replace the rest)
# ================================================================

# Replace _apply_desktop_slider through _save_overlay_cfg with thin delegates
# Keep: _build_tune_page, _build_tune_grid, _build_desktop_tune_page
# Replace: _apply_desktop_slider ... _save_overlay_cfg

replace_section(
    r"def _apply_desktop_slider",
    r"# =+ 设置对话框",
    """    @staticmethod
    def _apply_desktop_slider(key, var, lbl, fmt, gui):  # noqa: ANN001
        gui_desktop.apply_desktop_slider(key, var, lbl, fmt, gui._desktop_ctx)

    def _on_desktop_font(self, _v=""):
        gui_desktop.on_desktop_font(self._desktop_ctx)

    def _on_desktop_srcfont(self, _v=""):
        gui_desktop.on_desktop_srcfont(self._desktop_ctx)

    def _on_desktop_width(self, _v=""):
        gui_desktop.on_desktop_width(self._desktop_ctx)

    def _on_desktop_height(self, _v=""):
        gui_desktop.on_desktop_height(self._desktop_ctx)

    def _current_anchor(self):
        return gui_desktop.current_anchor(self._desktop_ctx)

    def _load_anchor_offset(self, anchor):
        ov = self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
        gui_desktop.load_anchor_offset(self._desktop_ctx, anchor, ov)

    def _make_tune_handler(self, key, var, lbl, unit):  # noqa: ANN001
        return gui_desktop.make_tune_handler(key, var, lbl, unit, self._desktop_ctx,
                                              overlay_fn=lambda: self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {})

    def _on_anchor_change(self):
        gui_desktop.on_anchor_change(self._desktop_ctx,
                                     overlay_fn=lambda: self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {})

    def _schedule_overlay_save(self):
        gui_desktop.schedule_overlay_save(self._desktop_ctx, root=self._root,
                                          overlay_fn=lambda: self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {})

    def _save_overlay_cfg(self):
        gui_desktop.save_overlay_cfg(self._desktop_ctx, cfg=self._cfg,
                                     overlay_fn=lambda: self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {})

    # ================================================================"""
)

# ================================================================
# 2. Replace chat/status methods with thin delegates
# ================================================================

# Replace _build_chat through _build_status (keep _check_api_key)
replace_section(
    r"def _build_chat\(self\)",
    r"def _check_api_key",
    """    def _build_chat(self):
        ctx = self._chat_ctx
        gui_chat.build_chat(self._root, ctx, self._on_mousewheel)
        self._canvas = ctx.canvas
        self._vsb = ctx.vsb

    def _build_input_row(self):
        ctx = self._chat_ctx
        gui_chat.build_input_row(self._root, ctx, self._cfg, self._attach_edit_menu)
        self._text_var = ctx.text_var
        self._text_entry = ctx.text_entry
        self._send_btn = ctx.send_btn

    def _set_text_input_enabled(self, on):
        gui_chat.set_text_input_enabled(self._chat_ctx, on)

    def _on_text_enter(self, _event=None):
        return gui_chat.on_text_enter(self._chat_ctx)

    def _send_typed(self):
        gui_chat.send_typed(self._chat_ctx, self._engines, self._engine_dirs, self._set_status)

    def _build_status(self):
        gui_chat.build_status(self._root, self._chat_ctx)
        self._status_dot = self._chat_ctx.status_dot
        self._status_label = self._chat_ctx.status_label
        self._stats_label = self._chat_ctx.stats_label

    # ================================================================ 事件处理

    def _check_api_key"""
)

# Replace _on_canvas_scroll through _on_canvas_configure
replace_section(
    r"def _on_canvas_scroll",
    r"def _on_direction_change",
    """    def _on_canvas_scroll(self, first, last):
        gui_chat.on_canvas_scroll(self._chat_ctx, first, last)
        self._auto_scroll = self._chat_ctx.auto_scroll

    def _on_mousewheel(self, event):
        gui_chat.on_mousewheel(self._chat_ctx, event)

    def _on_canvas_configure(self, event=None):
        gui_chat.on_canvas_configure(self._chat_ctx, self._root, event)
        self._canvas_w = self._chat_ctx.canvas_w

    def _on_direction_change"""
)

# ================================================================
# 3. Replace _push_lang_to_engines with thin delegate
# ================================================================
replace_section(
    r"def _push_lang_to_engines\(self\)",
    r"# =+ 引擎控制",
    """    def _push_lang_to_engines(self):
        self._engine_ctx.cfg = self._cfg
        self._engine_ctx.lang_pair = self._lang_pair
        self._engine_ctx.engines = self._engines
        self._engine_ctx.engine_dirs = self._engine_dirs
        gui_engine.push_lang_to_engines(self._engine_ctx)

    # ================================================================ 引擎控制"""
)

# ================================================================
# 4. Replace engine methods (_start through _on_close)
# ================================================================
replace_section(
    r"def _start\(self\)",
    r"# =+ 设备选择",
    """    def _start(self):
        self._sync_engine_ctx()
        gui_engine.start(self._engine_ctx)
        self._unsync_engine_ctx()

    def _start_engine(self, index):
        self._sync_engine_ctx()
        gui_engine.start_engine(self._engine_ctx, index)
        self._unsync_engine_ctx()

    def _on_engine_status(self, lvl, msg, who):
        gui_engine.on_engine_status(self._q, lvl, msg, who)

    def _start_overlay(self, *, force=False):
        self._sync_engine_ctx()
        result = gui_engine.start_overlay(self._engine_ctx, force=force)
        self._overlay_out = self._engine_ctx.overlay_out
        self._unsync_engine_ctx()
        return result

    def _on_overlay_toggle(self):
        if self._overlay_var.get():
            if self._start_overlay(force=True):
                self._set_status("info", t("手腕屏已开启（位置 / 字号等见「设置 → 手腕屏」）"))
            else:
                self._overlay_var.set(False)
                self._set_status("error", t("手腕屏没启动起来，已自动取消勾选（先把 SteamVR 打开，再勾一次即可）"))
        else:
            self._stop_overlay()
            self._sinks.discard("overlay")
        self._save_ui_state()

    def _stop_overlay(self):
        self._engine_ctx.overlay_out = self._overlay_out
        gui_engine._stop_overlay(self._engine_ctx)
        self._overlay_out = self._engine_ctx.overlay_out

    def _push_overlay(self, force=False):
        self._engine_ctx.overlay_out = self._overlay_out
        self._engine_ctx.bubbles = self._bubbles
        gui_engine.push_overlay(self._engine_ctx, force=force)

    def _desktop_cfg(self):
        return gui_engine.desktop_cfg(self._cfg)

    def _start_desktop(self, *, force=False):
        self._sync_engine_ctx()
        result = gui_engine.start_desktop(self._engine_ctx, force=force)
        self._desktop_out = self._engine_ctx.desktop_out
        self._unsync_engine_ctx()
        return result

    def _on_desktop_toggle(self):
        if self._desktop_var.get():
            if self._start_desktop(force=True):
                self._set_status("info", t("桌面字幕已开启（拖到想要的位置；尺寸/字号/透明度见「设置 → 桌面字幕」）"))
            else:
                self._desktop_var.set(False)
                self._set_status("error", t("桌面字幕没启动起来，已自动取消勾选"))
        else:
            self._stop_desktop()
            self._sinks.discard("desktop")
        self._save_ui_state()

    def _stop_desktop(self):
        self._sync_engine_ctx()
        gui_engine.stop_desktop(self._engine_ctx)
        self._desktop_out = self._engine_ctx.desktop_out
        self._desktop_dragging = False
        self._unsync_engine_ctx()

    def _push_desktop(self, force=False):
        self._engine_ctx.desktop_out = self._desktop_out
        self._engine_ctx.bubbles = self._bubbles
        gui_engine.push_desktop(self._engine_ctx, force=force)

    def _on_desktop_alpha(self, _v=""):
        gui_engine_desktop_alpha = gui_engine.desktop_cfg  # noqa
        # 透明度滑块：先改窗口（立刻见效），停手 300ms 再落盘
        self._desktop_alpha_touched = True
        a = float(self._desktop_alpha_var.get())
        lbl = getattr(self, "_desktop_alpha_lbl", None)
        if lbl is not None:
            lbl.configure(text=f"{a:.2f}")
        if self._desktop_out is not None:
            self._desktop_out.set_alpha(a)
        self._schedule_desktop_save()

    def _schedule_desktop_save(self):
        self._engine_ctx.desktop_out = self._desktop_out
        self._engine_ctx.desktop_save_job = self._desktop_save_job
        self._engine_ctx.root = self._root
        gui_engine.schedule_desktop_save(self._engine_ctx)
        self._desktop_save_job = self._engine_ctx.desktop_save_job

    def _save_desktop_cfg(self):
        self._sync_engine_ctx()
        gui_engine.save_desktop_cfg(self._engine_ctx)
        self._desktop_save_job = self._engine_ctx.desktop_save_job

    def _toggle_desktop_drag(self):
        self._sync_engine_ctx()
        gui_engine.toggle_desktop_drag(self._engine_ctx)
        self._desktop_out = self._engine_ctx.desktop_out
        self._desktop_dragging = self._engine_ctx.desktop_dragging

    def _stop(self):
        self._sync_engine_ctx()
        gui_engine.stop(self._engine_ctx)
        self._unsync_engine_ctx()

    def _wait_stop_done(self, engines):
        self._engine_ctx.q = self._q
        self._engine_ctx.stop_done_evt = self._stop_done_evt
        self._engine_ctx.root = self._root
        gui_engine.wait_stop_done(self._engine_ctx, engines)

    def _on_close(self):
        self._sync_engine_ctx()
        gui_engine.on_close(self._engine_ctx)

    def _sync_engine_ctx(self):
        ctx = self._engine_ctx
        ctx.engines = self._engines; ctx.engine_dirs = self._engine_dirs
        ctx.specs = self._specs; ctx.sinks = self._sinks
        ctx.pending_starts = self._pending_starts; ctx.current = self._current
        ctx.auto_scroll = self._auto_scroll; ctx.closing = self._closing
        ctx.overlay_out = self._overlay_out; ctx.desktop_out = self._desktop_out
        ctx.desktop_dragging = self._desktop_dragging
        ctx.start_btn = getattr(self, '_start_btn', None)
        ctx.stop_btn = getattr(self, '_stop_btn', None)
        ctx.direction_var = getattr(self, '_direction_var', None)
        ctx.chatbox_var = getattr(self, '_chatbox_var', None)
        ctx.overlay_var = getattr(self, '_overlay_var', None)
        ctx.desktop_var = getattr(self, '_desktop_var', None)
        ctx.vmic_var = getattr(self, '_vmic_var', None)
        ctx.lang_pair = self._lang_pair
        ctx.desktop_alpha_var = getattr(self, '_desktop_alpha_var', None)
        ctx.desktop_alpha_lbl = getattr(self, '_desktop_alpha_lbl', None)
        ctx.desktop_font_var = getattr(self, '_desktop_font_var', None)
        ctx.desktop_srcfont_var = getattr(self, '_desktop_srcfont_var', None)
        ctx.desktop_w_var = getattr(self, '_desktop_w_var', None)
        ctx.desktop_h_var = getattr(self, '_desktop_h_var', None)
        ctx.desktop_drag_btn = getattr(self, '_desktop_drag_btn', None)
        ctx.desktop_save_job = self._desktop_save_job
        ctx.desktop_alpha_touched = self._desktop_alpha_touched
        ctx.desktop_tuned = getattr(self, '_desktop_tuned', set())
        ctx.bubbles = self._bubbles; ctx.q = self._q; ctx.root = self._root
        ctx.cfg = self._cfg; ctx.start_job = self._start_job
        ctx.stop_done_evt = self._stop_done_evt
        ctx.set_status_fn = self._set_status
        ctx.on_engine_text_fn = self._on_engine_text
        ctx.set_text_input_enabled_fn = self._set_text_input_enabled
        ctx.refresh_api_key_fn = self._refresh_api_key_in_cfg
        ctx.start_room_fn = self._start_room; ctx.stop_room_fn = self._stop_room
        ctx.refresh_room_status_fn = self._refresh_room_status_label
        ctx.sync_gate_probe_fn = self._sync_gate_level_probe
        ctx.stop_gate_probe_fn = self._stop_gate_probe
        ctx.maybe_replace_on_exit_fn = self._maybe_replace_on_exit
        ctx.destroy_root_fn = lambda: self._root.destroy()
        ctx.save_ui_state_fn = self._save_ui_state

    def _unsync_engine_ctx(self):
        ctx = self._engine_ctx
        self._engines = ctx.engines; self._engine_dirs = ctx.engine_dirs
        self._specs = ctx.specs; self._sinks = ctx.sinks
        self._pending_starts = ctx.pending_starts; self._current = ctx.current
        self._auto_scroll = ctx.auto_scroll; self._closing = ctx.closing
        self._overlay_out = ctx.overlay_out; self._desktop_out = ctx.desktop_out
        self._desktop_dragging = ctx.desktop_dragging
        self._desktop_save_job = ctx.desktop_save_job
        self._desktop_alpha_touched = ctx.desktop_alpha_touched
        self._start_job = ctx.start_job

    # ================================================================ 设备选择（→ gui_audio）"""
)

# ================================================================
# 5. Replace _poll with thin delegate
# ================================================================
replace_section(
    r"def _poll\(self\)",
    r"# =+ 聊天气泡",
    """    def _poll(self):
        ctx = self._chat_ctx
        ctx.q = self._q; ctx.engines_ref = self._engines; ctx.engine_dirs_ref = self._engine_dirs
        ctx.room_status_next = self._room_status_next
        ctx.gate_level_tick = self._gate_level_tick
        ctx.push_overlay_fn = self._push_overlay; ctx.push_desktop_fn = self._push_desktop
        ctx.on_device_scan_result_fn = self._on_device_scan_result
        ctx.on_update_check_result_fn = self._on_update_check_result
        ctx.on_download_progress_fn = self._on_download_progress
        ctx.on_download_done_fn = self._on_download_done
        ctx.on_download_error_fn = self._on_download_error
        ctx.on_voice_preview_done_fn = self._on_voice_preview_done
        ctx.refresh_room_status_fn = self._refresh_room_status_label
        ctx.sync_gate_level_fn = self._sync_gate_level_probe
        ctx.refresh_gate_level_fn = self._refresh_gate_level
        ctx.start_btn_fn = lambda s: self._start_btn.configure(state=s)
        ctx.stop_btn_fn = lambda s: self._stop_btn.configure(state=s)
        _ov_holder = [self._overlay_out]; _dov_holder = [self._desktop_out]
        gui_chat.poll(ctx, self._root, self._set_status, self._add_text,
                      self._refresh_status, self._stats,
                      [self._pending_starts], _ov_holder, _dov_holder)
        self._pending_starts = [self._pending_starts][0]
        self._overlay_out = _ov_holder[0]; self._desktop_out = _dov_holder[0]
        self._room_status_next = ctx.room_status_next
        self._gate_level_tick = ctx.gate_level_tick
        if ctx.engines_ref is not self._engines:
            self._engines = ctx.engines_ref; self._engine_dirs = ctx.engine_dirs_ref

    # ================================================================ 聊天气泡"""
)

# ================================================================
# 6. Replace bubble methods with thin delegates
# ================================================================
replace_section(
    r"def _add_text\(self",
    r"# =+ 状态栏",
    """    def _add_text(self, source, text, is_final, who="mine", label=""):
        gui_chat.add_text(self._chat_ctx, source, text, is_final, who, label)
        self._bubbles = self._chat_ctx.bubbles; self._current = self._chat_ctx.current
        self._auto_scroll = self._chat_ctx.auto_scroll

    def _draw_bubble(self, b):
        return gui_chat.draw_bubble(self._chat_ctx, b)

    def _redraw_current(self, b):
        gui_chat.redraw_current(self._chat_ctx, b)

    def _trim(self):
        gui_chat.trim(self._chat_ctx)

    def _update_scrollregion(self):
        gui_chat.update_scrollregion(self._chat_ctx)

    def _redraw_all(self):
        gui_chat.redraw_all(self._chat_ctx)

    # ================================================================ 状态栏"""
)

# ================================================================
# 7. Replace status methods with thin delegates
# ================================================================
replace_section(
    r"def _set_status\(self",
    r"# =+ 自检",
    """    def _set_status(self, level, msg):
        gui_chat.set_status(self._chat_ctx, level, msg)
        self._last_status_level = self._chat_ctx.last_status_level

    def _refresh_status(self):
        gui_chat.refresh_status(self._chat_ctx, self._engines,
                                getattr(self, '_direction_var', None), self._stats)

    # ================================================================ 自检"""
)

# ================================================================
# 8. Also replace _build_tune_page and _build_desktop_tune_page
#    to populate DesktopCtx (needed for the thin delegates to work)
# ================================================================

# For _build_tune_page: after building, sync to self
# We need to add sync code at the end of _build_tune_page
# Find the end of _build_tune_page (just before _build_tune_grid)
replace_section(
    r"def _build_tune_page\(self, body: ttk\.Frame\)",
    r"def _build_tune_grid\(self",
    """    def _build_tune_page(self, body: ttk.Frame) -> None:
        ov = self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
        gui_desktop.build_tune_page(body, self._desktop_ctx, ov,
                                    overlay_fn=lambda: self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {})
        # Sync widget references back to self for backward compatibility
        ctx = self._desktop_ctx
        self._anchor_combo = ctx.anchor_combo; self._tracker_var = ctx.tracker_var
        self._tune_panel_w = ctx.tune_panel_w
        self._anchor_label_to_key = ctx.anchor_label_to_key
        self._tune_values = ctx.tune_values; self._tune_vars = ctx.tune_vars
        self._tune_lbls = ctx.tune_lbls; self._tune_units = ctx.tune_units
        self._tune_grid = ctx.tune_grid; self._ov_save_job = ctx.ov_save_job

    def _build_tune_grid(self"""
)

# Replace _build_tune_grid body
replace_section(
    r"def _build_tune_grid\(self, grid",
    r"def _build_desktop_tune_page",
    """    def _build_tune_grid(self, grid, specs, label_w, cols):
        gui_desktop.build_tune_grid(grid, specs, label_w, cols, self._desktop_ctx)

    def _build_desktop_tune_page"""
)

# Replace _build_desktop_tune_page body
replace_section(
    r"def _build_desktop_tune_page\(self, body",
    r"@staticmethod\n    def _apply_desktop_slider",
    """    def _build_desktop_tune_page(self, body: ttk.Frame) -> None:
        gui_desktop.build_desktop_tune_page(body, self._desktop_ctx, self._desktop_cfg())
        # Sync widget references back to self
        ctx = self._desktop_ctx
        self._desktop_font_var = ctx.desktop_font_var; self._desktop_font_lbl = ctx.desktop_font_lbl
        self._desktop_srcfont_var = ctx.desktop_srcfont_var; self._desktop_srcfont_lbl = ctx.desktop_srcfont_lbl
        self._desktop_w_var = ctx.desktop_w_var; self._desktop_w_lbl = ctx.desktop_w_lbl
        self._desktop_h_var = ctx.desktop_h_var; self._desktop_h_lbl = ctx.desktop_h_lbl
        self._desktop_alpha_var = ctx.desktop_alpha_var; self._desktop_alpha_lbl = ctx.desktop_alpha_lbl
        self._desktop_drag_btn = ctx.desktop_drag_btn; self._desktop_tuned = ctx.desktop_tuned

    @staticmethod
    def _apply_desktop_slider"""
)

# Write the result
result = "\n".join(lines)
with open(src, "w", encoding="utf-8") as f:
    f.write(result)

line_count = len(lines)
print(f"\nDone! gui.py is now {line_count} lines")
