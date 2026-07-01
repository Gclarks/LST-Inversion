"""
缓存管理弹窗。
列出所有缓存文件夹（时间戳命名），支持多选删除。
"""

import tkinter as tk
from tkinter import ttk, messagebox

from utils.file_utils import list_cache_folders, delete_cache_folder


class CacheDialog(tk.Toplevel):
    """模态弹窗，管理缓存文件夹。"""

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("管理缓存文件")
        self.geometry("580x380")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        # 表格：多选 Treeview
        columns = ('name', 'files', 'size')
        self._tree = ttk.Treeview(self, columns=columns,
                                  show='headings', selectmode='extended')
        self._tree.heading('name', text='文件夹')
        self._tree.heading('files', text='文件数')
        self._tree.heading('size', text='大小 (MB)')
        self._tree.column('name', width=280, minwidth=150)
        self._tree.column('files', width=80, anchor='center')
        self._tree.column('size', width=100, anchor='e')

        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                  command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')

        # 按钮区
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=1, column=0, columnspan=2, pady=(10, 0),
                       sticky='ew')
        ttk.Button(btn_frame, text='删除选中', width=12,
                   command=self._delete_selected).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text='刷新', width=8,
                   command=self._refresh).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btn_frame, text='关闭', width=8,
                   command=self.destroy).pack(side=tk.RIGHT)

        # 信息标签
        self._info_var = tk.StringVar()
        ttk.Label(self, textvariable=self._info_var, font=('', 8)).grid(
            row=2, column=0, columnspan=2, sticky='w', pady=(6, 0))

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

    def _refresh(self):
        """刷新文件夹列表。"""
        self._tree.delete(*self._tree.get_children())
        folders = list_cache_folders()
        total_size = 0
        for f in folders:
            self._tree.insert('', tk.END, iid=f['name'],
                              values=(f['name'], f['file_count'],
                                      f'{f["size_mb"]:.1f}'))
            total_size += f['size_mb']
        self._info_var.set(
            f'共 {len(folders)} 个缓存 • 总占用 {total_size:.1f} MB'
        )

    def _delete_selected(self):
        """删除选中的文件夹。"""
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo('提示', '请先选择要删除的缓存文件夹。')
            return
        if not messagebox.askyesno(
            '确认删除',
            f'确定要删除选中的 {len(selection)} 个缓存文件夹吗？\n此操作不可撤销。',
            parent=self,
        ):
            return
        for name in selection:
            delete_cache_folder(name)
        self._refresh()
