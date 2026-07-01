"""
元数据查看弹窗。
以分组树形表格展示 MTL.txt 中的关键参数。
"""

import tkinter as tk
from tkinter import ttk

from core.metadata import LandsatMetadata


class MetadataDialog(tk.Toplevel):
    """模态弹窗，展示 Landsat 8 元数据。"""

    def __init__(self, parent: tk.Widget, metadata: LandsatMetadata):
        super().__init__(parent)
        self.title("影像元数据")
        self.geometry("600x520")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        # 树形表格：两列 (参数, 值)
        columns = ('param', 'value')
        self._tree = ttk.Treeview(self, columns=columns,
                                  show='tree headings',
                                  selectmode='none')
        self._tree.heading('param', text='参数')
        self._tree.heading('value', text='值')
        self._tree.column('param', width=260, minwidth=180)
        self._tree.column('value', width=320, minwidth=200)

        # 垂直滚动条
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                  command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')

        # 关闭按钮
        btn = ttk.Button(self, text='关闭', command=self.destroy)
        btn.grid(row=1, column=0, columnspan=2, pady=(10, 0))

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._populate(metadata)

    def _populate(self, meta: LandsatMetadata):
        """填充元数据到树形表格。"""
        sections = meta.to_display_dict()
        for section_name, items in sections.items():
            parent_id = self._tree.insert('', tk.END, text=section_name,
                                          open=True,
                                          tags=('section',))
            for key, value in items.items():
                self._tree.insert(parent_id, tk.END,
                                  values=(key, str(value)),
                                  tags=('item',))

        # 样式：分区标题加粗
        self._tree.tag_configure('section', font=('', 9, 'bold'))
        self._tree.tag_configure('item', font=('', 9))
