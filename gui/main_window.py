"""
主窗口界面。
提供输入/输出路径选择、输出文件名、温度单位、水汽含量、
元数据查看、算法设置和反演控制。
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from core.metadata import LandsatMetadata
from gui.metadata_dialog import MetadataDialog
from gui.cache_dialog import CacheDialog
from gui.settings_dialog import SettingsDialog, load_settings_from_file
from gui.worker import InversionWorker
from utils.file_utils import validate_input_directory
from utils.constants import (
    DEFAULT_WATER_VAPOR, WV_MIN, WV_MAX,
    OUTPUT_FILENAME, KELVIN, CELSIUS,
)


class MainWindow(tk.Tk):
    """Landsat 8 单通道地表温度反演主窗口。"""

    def __init__(self):
        super().__init__()
        self.title("Landsat 8 单通道地表温度反演")
        self.geometry("640x560")
        self.minsize(540, 460)
        self.resizable(True, True)

        # 启动时从文件恢复保存过的设置
        load_settings_from_file()

        # 运行时状态
        self._worker: InversionWorker | None = None
        self._queue: queue.Queue = queue.Queue()
        self._cancel_event: threading.Event = threading.Event()
        self._processing = False
        self._metadata: LandsatMetadata | None = None

        self._build_ui()

    # ── UI 构建 ──────────────────────────────────────────────

    def _build_ui(self):
        """构建全部控件。"""
        main = ttk.Frame(self, padding=(12, 12, 12, 12))
        main.grid(row=0, column=0, sticky='nsew')
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        row = 0

        # ── 输入目录 ──
        ttk.Label(main, text='输入目录:').grid(
            row=row, column=0, sticky='w', pady=(0, 2)); row += 1
        dir_frame1 = ttk.Frame(main)
        dir_frame1.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 6)); row += 1
        self._input_var = tk.StringVar()
        self._input_entry = ttk.Entry(dir_frame1, textvariable=self._input_var)
        self._input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._browse_input_btn = ttk.Button(dir_frame1, text='浏览', width=6,
                                            command=self._browse_input)
        self._browse_input_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ── 输出目录 ──
        ttk.Label(main, text='输出目录:').grid(
            row=row, column=0, sticky='w', pady=(0, 2)); row += 1
        dir_frame2 = ttk.Frame(main)
        dir_frame2.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 6)); row += 1
        self._output_var = tk.StringVar()
        self._output_entry = ttk.Entry(dir_frame2, textvariable=self._output_var)
        self._output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._browse_output_btn = ttk.Button(dir_frame2, text='浏览', width=6,
                                             command=self._browse_output)
        self._browse_output_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ── 输出文件名 ──
        ttk.Label(main, text='输出文件名:').grid(
            row=row, column=0, sticky='w', pady=(0, 2)); row += 1
        name_frame = ttk.Frame(main)
        name_frame.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 6)); row += 1
        self._filename_var = tk.StringVar(value=OUTPUT_FILENAME)
        self._filename_entry = ttk.Entry(name_frame, textvariable=self._filename_var)
        self._filename_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(name_frame, text='.tif', font=('', 9)).pack(side=tk.LEFT, padx=(4, 0))

        # ── 温度单位 ──
        unit_frame = ttk.Frame(main)
        unit_frame.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 6)); row += 1
        ttk.Label(unit_frame, text='温度单位:').pack(side=tk.LEFT)
        self._unit_var = tk.StringVar(value=KELVIN)
        self._unit_rb_k = ttk.Radiobutton(unit_frame, text='开尔文 (K)',
                                          variable=self._unit_var, value=KELVIN)
        self._unit_rb_k.pack(side=tk.LEFT, padx=(8, 4))
        self._unit_rb_c = ttk.Radiobutton(unit_frame, text='摄氏度 (°C)',
                                          variable=self._unit_var, value=CELSIUS)
        self._unit_rb_c.pack(side=tk.LEFT, padx=(4, 0))

        # ── 水汽含量 ──
        wv_frame = ttk.Frame(main)
        wv_frame.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 6)); row += 1
        ttk.Label(wv_frame, text='水汽含量:').pack(side=tk.LEFT)
        self._wv_var = tk.StringVar(value=str(DEFAULT_WATER_VAPOR))
        vcmd = (self.register(self._validate_water_vapor), '%P')
        self._wv_entry = ttk.Entry(
            wv_frame, textvariable=self._wv_var, width=8,
            validate='focusout', validatecommand=vcmd,
        )
        self._wv_entry.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(wv_frame, text='g/cm²').pack(side=tk.LEFT)
        self._auto_wv_btn = ttk.Button(
            wv_frame, text='自动获取', width=8,
            command=self._auto_water_vapor, state=tk.DISABLED,
        )
        self._auto_wv_btn.pack(side=tk.LEFT, padx=(12, 0))

        # ── 控制按钮 ──
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=3,
                       sticky='ew', pady=(6, 8)); row += 1
        self._meta_btn = ttk.Button(
            btn_frame, text='查看元数据', width=14,
            command=self._show_metadata, state=tk.DISABLED,
        )
        self._meta_btn.pack(side=tk.LEFT)
        self._settings_btn = ttk.Button(btn_frame, text='算法设置', width=12,
                                        command=self._show_settings)
        self._settings_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._cache_btn = ttk.Button(btn_frame, text='管理缓存', width=12,
                                     command=self._show_cache)
        self._cache_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._start_btn = ttk.Button(
            btn_frame, text='开始反演', width=14,
            command=self._start_inversion,
        )
        self._start_btn.pack(side=tk.RIGHT)

        # ── 状态栏 ──
        status_frame = ttk.Frame(main)
        status_frame.grid(row=row, column=0, columnspan=3,
                          sticky='ew', pady=(0, 4)); row += 1
        self._status_var = tk.StringVar(value='就绪')
        ttk.Label(status_frame, textvariable=self._status_var,
                  font=('', 9)).pack(side=tk.LEFT)

        # ── 进度条 ──
        self._progress = ttk.Progressbar(
            main, mode='determinate', maximum=100,
        )
        self._progress.grid(row=row, column=0, columnspan=3,
                            sticky='ew', pady=(0, 8)); row += 1

        # ── 日志区 ──
        log_frame = ttk.LabelFrame(main, text='处理日志', padding=(4, 4))
        log_frame.grid(row=row, column=0, columnspan=3,
                       sticky='nsew', pady=(0, 0)); row += 1
        self._log_text = tk.Text(
            log_frame, height=10, width=70,
            state=tk.DISABLED, wrap=tk.WORD,
            font=('Consolas', 9),
        )
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                                   command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        self._log_text.grid(row=0, column=0, sticky='nsew')
        log_scroll.grid(row=0, column=1, sticky='ns')
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        # 布局权重
        main.grid_rowconfigure(row - 1, weight=1)
        main.grid_columnconfigure(0, weight=1)

    # ── 浏览 ────────────────────────────────────────────────

    def _browse_input(self):
        path = filedialog.askdirectory(title='选择 Landsat 8 影像目录')
        if not path:
            return
        self._input_var.set(path)
        try:
            scan = validate_input_directory(path)
            if scan['mtl'] is not None:
                self._metadata = LandsatMetadata(scan['mtl'])
                self._meta_btn.configure(state=tk.NORMAL)
            else:
                self._metadata = None
                self._meta_btn.configure(state=tk.DISABLED)
        except Exception:
            self._metadata = None
            self._meta_btn.configure(state=tk.DISABLED)

    def _browse_output(self):
        path = filedialog.askdirectory(title='选择输出目录')
        if path:
            self._output_var.set(path)

    # ── 弹窗 ────────────────────────────────────────────────

    def _show_metadata(self):
        if self._metadata is None:
            messagebox.showwarning('提示', '请先选择包含 MTL.txt 的影像目录。')
            return
        MetadataDialog(self, self._metadata)

    def _show_settings(self):
        SettingsDialog(self)

    def _show_cache(self):
        CacheDialog(self)

    # ── 水汽验证 ────────────────────────────────────────────

    def _validate_water_vapor(self, value: str) -> bool:
        if not value:
            return True
        try:
            v = float(value)
            if v < WV_MIN or v > WV_MAX:
                self._log(f'提示: 水汽含量 {v} 超出典型范围 [{WV_MIN}, {WV_MAX}]')
        except ValueError:
            self._log(f'警告: 水汽含量 "{value}" 不是有效数字')
            return False
        return True

    def _auto_water_vapor(self):
        messagebox.showinfo('提示', '自动获取水汽功能将在后续版本实现。\n请手动输入水汽含量。')

    # ── 反演启动 ────────────────────────────────────────────

    def _start_inversion(self):
        """启动反演流水线。"""
        input_dir = self._input_var.get().strip()
        output_dir = self._output_var.get().strip()

        if not input_dir or not os.path.isdir(input_dir):
            messagebox.showerror('错误', '请选择有效的输入目录。')
            return
        if not output_dir:
            output_dir = os.path.join(input_dir, 'LST_Output')
            self._output_var.set(output_dir)

        # 输出文件名
        filename = self._filename_var.get().strip()
        if not filename:
            filename = OUTPUT_FILENAME
        if not filename.lower().endswith('.tif'):
            filename += '.tif'

        # 温度单位
        output_unit = self._unit_var.get()

        # 水汽
        wv_str = self._wv_var.get().strip()
        if not wv_str:
            wv_str = str(DEFAULT_WATER_VAPOR)
            self._wv_var.set(wv_str)
        try:
            water_vapor = float(wv_str)
        except ValueError:
            messagebox.showerror('错误', '水汽含量必须是有效数字。')
            return
        if water_vapor <= 0:
            messagebox.showerror('错误', '水汽含量必须大于 0。')
            return

        # 验证输入目录
        scan = validate_input_directory(input_dir)
        if not scan['valid']:
            messagebox.showerror(
                '数据错误',
                '输入目录验证失败:\n' + '\n'.join(scan['errors']),
            )
            return

        # 处理状态
        self._set_processing_state(True)
        self._clear_log()
        self._log(f'输入目录: {input_dir}')
        self._log(f'输出目录: {output_dir}')
        self._log(f'输出文件: {filename}')
        self._log(f'温度单位: {output_unit}')
        self._log(f'水汽含量: {water_vapor} g/cm²')
        self._log('─' * 50)

        # 启动后台线程
        self._cancel_event.clear()
        self._queue = queue.Queue()
        self._worker = InversionWorker(
            input_dir=input_dir,
            output_dir=output_dir,
            output_filename=filename,
            water_vapor=water_vapor,
            output_unit=output_unit,
            progress_queue=self._queue,
            cancel_event=self._cancel_event,
        )
        self._worker.start()
        self._poll_queue()

    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        if self._processing:
            self.after(150, self._poll_queue)

    def _handle_message(self, msg: dict):
        msg_type = msg.get('type', '')
        if msg_type == 'progress':
            pct = msg.get('percent', 0)
            step = msg.get('step', '')
            detail = msg.get('message', '')
            self._progress['value'] = pct
            self._status_var.set(f'{step} — {detail} ({pct}%)')
            if detail:
                self._log(f'  [{step}] {detail}')
        elif msg_type == 'log':
            self._log(msg.get('message', ''))
        elif msg_type == 'done':
            success = msg.get('success', False)
            message = msg.get('message', '')
            if success:
                # 先闪一下 100% 再弹窗
                self._progress['value'] = 100
                self._status_var.set('处理完成')
                self._log('─' * 50)
                self._log(message)
                self.update_idletasks()
                messagebox.showinfo('完成', message)
            else:
                self._progress['value'] = 0
                self._status_var.set('处理失败')
                self._log(message)
                if '用户取消' not in message:
                    messagebox.showerror('错误', message)
            # 无论成功失败，弹窗关闭后统一回滚到初始状态
            self._reset_ui()

    # ── 状态管理 ────────────────────────────────────────────

    def _set_processing_state(self, processing: bool):
        self._processing = processing
        state = tk.DISABLED if processing else tk.NORMAL

        self._input_entry.configure(state=state)
        self._output_entry.configure(state=state)
        self._filename_entry.configure(state=state)
        self._unit_rb_k.configure(state=state)
        self._unit_rb_c.configure(state=state)
        self._wv_entry.configure(state=state)
        self._meta_btn.configure(state=state)
        self._settings_btn.configure(state=state)
        self._cache_btn.configure(state=state)
        self._browse_input_btn.configure(state=state)
        self._browse_output_btn.configure(state=state)

        self._start_btn.configure(
            text='取消' if processing else '开始反演',
            command=self._cancel_inversion if processing else self._start_inversion,
        )

    def _reset_ui(self):
        """反演完成后回滚 UI 到初始态。"""
        self._processing = False
        self._progress['value'] = 0
        self._status_var.set('就绪')
        self._start_btn.configure(
            text='开始反演',
            command=self._start_inversion,
        )
        state = tk.NORMAL
        self._input_entry.configure(state=state)
        self._output_entry.configure(state=state)
        self._filename_entry.configure(state=state)
        self._unit_rb_k.configure(state=state)
        self._unit_rb_c.configure(state=state)
        self._wv_entry.configure(state=state)
        self._meta_btn.configure(state=state)
        self._settings_btn.configure(state=state)
        self._cache_btn.configure(state=state)
        self._browse_input_btn.configure(state=state)
        self._browse_output_btn.configure(state=state)
        # 如输入目录仍有效，保持元数据按钮可用
        if self._metadata is not None:
            self._meta_btn.configure(state=tk.NORMAL)

    def _cancel_inversion(self):
        self._cancel_event.set()
        self._status_var.set('正在取消...')
        self._log('正在取消...')

    # ── 日志 ────────────────────────────────────────────────

    def _log(self, text: str):
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert(tk.END, text + '\n')
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete('1.0', tk.END)
        self._log_text.configure(state=tk.DISABLED)
