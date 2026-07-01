"""
设置界面。
以标签页展示全部算法常数，允许用户自定义并保存。
默认值来源于 utils.constants。
"""

import copy
import json
import os
import tkinter as tk
from tkinter import ttk, messagebox

from utils import constants as defaults

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             '.lst_settings.json')


# ── 帮助函数 ──────────────────────────────────────────────────

def _labeled_entry(parent, row: int, label: str, value,
                   width: int = 14) -> tk.StringVar:
    """在父容器 grid 的指定行放置 Label + Entry，返回绑定的 StringVar。"""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w',
                                       padx=(0, 8), pady=2)
    var = tk.StringVar(value=str(value))
    ttk.Entry(parent, textvariable=var, width=width).grid(
        row=row, column=1, sticky='ew', pady=2)
    return var


# ── 设置对话框 ───────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):
    """模态设置窗口，标签页组织全部可调参数。"""

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("算法设置")
        self.geometry("680x520")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._vars: dict[str, tk.StringVar] = {}
        self._build_ui()
        self._load_defaults()

    # ── UI 骨架 ──────────────────────────────────────────────

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        # 四个标签页
        self._tab_physics = ttk.Frame(notebook)
        self._tab_emissivity = ttk.Frame(notebook)
        self._tab_coeffs = ttk.Frame(notebook)
        self._tab_output = ttk.Frame(notebook)
        self._tab_modis = ttk.Frame(notebook)

        notebook.add(self._tab_physics, text='物理常数')
        notebook.add(self._tab_emissivity, text='NDVI & 比辐射率')
        notebook.add(self._tab_coeffs, text='算法系数 a₀~a₇')
        notebook.add(self._tab_output, text='输出设置')
        notebook.add(self._tab_modis, text='MODIS 数据源')

        self._populate_physics()
        self._populate_emissivity()
        self._populate_coefficients()
        self._populate_output()
        self._populate_modis()

        # 底部按钮
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8, pady=10)
        ttk.Button(btn_frame, text='恢复默认值',
                   command=self._load_defaults).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text='保存', width=10,
                   command=self._save).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_frame, text='取消', width=10,
                   command=self.destroy).pack(side=tk.RIGHT)

    # ── 标签页: 物理常数 ─────────────────────────────────────

    def _populate_physics(self):
        f = ttk.Frame(self._tab_physics, padding=(12, 12, 12, 12))
        f.pack(fill=tk.X)
        f.grid_columnconfigure(1, weight=1)

        self._vars['C1'] = _labeled_entry(f, 0, 'C1 (W·μm⁴·m⁻²·sr⁻¹)', f'{defaults.C1:.8e}', 18)
        self._vars['C2'] = _labeled_entry(f, 1, 'C2 (μm·K)', f'{defaults.C2:.6f}', 18)
        self._vars['LAMBDA_TIR'] = _labeled_entry(f, 2, 'λ (μm) — Band 10 有效波长',
                                                  f'{defaults.LAMBDA_TIR:.6f}', 18)
        self._vars['DEFAULT_WATER_VAPOR'] = _labeled_entry(
            f, 3, '默认水汽含量 w (g/cm²)', str(defaults.DEFAULT_WATER_VAPOR))

        ttk.Label(f, text='\n这些常数影响 Planck 反函数计算和缺省水汽值。',
                  font=('', 8)).grid(row=5, column=0, columnspan=2, sticky='w')

    # ── 标签页: NDVI & 比辐射率 ──────────────────────────────

    def _populate_emissivity(self):
        f = ttk.Frame(self._tab_emissivity, padding=(12, 12, 12, 12))
        f.pack(fill=tk.X)
        f.grid_columnconfigure(1, weight=1)

        ttk.Label(f, text='NDVI 阈值', font=('', 9, 'bold')).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 4))

        self._vars['NDVI_SOIL'] = _labeled_entry(f, 1, 'NDVI_soil (裸土阈值)',
                                                 str(defaults.NDVI_SOIL))
        self._vars['NDVI_VEG'] = _labeled_entry(f, 2, 'NDVI_veg (植被阈值)',
                                                str(defaults.NDVI_VEG))

        ttk.Label(f, text='\n地表比辐射率参数 (Band 10)', font=('', 9, 'bold')).grid(
            row=3, column=0, columnspan=2, sticky='w', pady=(8, 4))

        self._vars['EMISSIVITY_VEGETATION'] = _labeled_entry(
            f, 4, 'ε_v (植被比辐射率)', str(defaults.EMISSIVITY_VEGETATION))
        self._vars['EMISSIVITY_SOIL'] = _labeled_entry(
            f, 5, 'ε_s (土壤比辐射率)', str(defaults.EMISSIVITY_SOIL))
        self._vars['EMISSIVITY_BARE_A'] = _labeled_entry(
            f, 6, '裸土 ε 截距 A', str(defaults.EMISSIVITY_BARE_A))
        self._vars['EMISSIVITY_BARE_B'] = _labeled_entry(
            f, 7, '裸土 ε 斜率 B', str(defaults.EMISSIVITY_BARE_B))
        self._vars['EMISSIVITY_DENSE_VEG'] = _labeled_entry(
            f, 8, '浓密植被 ε', str(defaults.EMISSIVITY_DENSE_VEG))

        ttk.Label(f, text='\n裸土/水体: ε = A − B × ρ_red\n'
                  '混合像元: ε = ε_v·Pv + ε_s·(1−Pv)\n'
                  '浓密植被: ε = 固定值',
                  font=('', 8)).grid(row=10, column=0, columnspan=2, sticky='w')

    # ── 标签页: 算法系数 ─────────────────────────────────────

    def _populate_coefficients(self):
        # 使用 Canvas + Scrollbar 应对内容较多
        canvas = tk.Canvas(self._tab_coeffs, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self._tab_coeffs, orient=tk.VERTICAL,
                                  command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind('<Configure>',
                          lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=scroll_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        f = ttk.Frame(scroll_frame, padding=(12, 12, 12, 12))
        f.pack(fill=tk.X)

        coeff_labels = ['a0', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7']
        ranges = [
            ('w ∈ [0.0, 2.0)', defaults.SC_COEFFICIENTS[(0.0, 2.0)]),
            ('w ∈ [2.0, 4.0)', defaults.SC_COEFFICIENTS[(2.0, 4.0)]),
            ('w ∈ [4.0, 7.0)', defaults.SC_COEFFICIENTS[(4.0, 7.0)]),
            ('Full range',      defaults.SC_COEFFICIENTS_FULL),
        ]

        # 表头
        ttk.Label(f, text='', width=18).grid(row=0, column=0)
        for ci, (rlabel, _) in enumerate(ranges):
            ttk.Label(f, text=rlabel, font=('', 8, 'bold')).grid(
                row=0, column=ci+1, padx=6)

        for ri, key in enumerate(coeff_labels):
            ttk.Label(f, text=key, font=('', 9, 'bold')).grid(
                row=ri+1, column=0, sticky='w', pady=1)
            for ci, (rlabel, coeffs) in enumerate(ranges):
                var = tk.StringVar(value=str(coeffs[key]))
                ttk.Entry(f, textvariable=var, width=12).grid(
                    row=ri+1, column=ci+1, padx=4, pady=1)
                self._vars[f'coeff_{rlabel}_{key}'] = var

        ttk.Label(f, text='\n系数来源: MODTRAN + TIGR 大气廓线拟合。'
                  '\n修改后请确保已理解各分段区间的含义。',
                  font=('', 8)).grid(row=len(coeff_labels)+1, column=0,
                                     columnspan=5, sticky='w')

    # ── 标签页: 输出设置 ─────────────────────────────────────

    def _populate_output(self):
        f = ttk.Frame(self._tab_output, padding=(12, 12, 12, 12))
        f.pack(fill=tk.X)
        f.grid_columnconfigure(1, weight=1)

        self._vars['OUTPUT_UNIT'] = _labeled_entry(
            f, 0, '默认温度单位 (K 或 C)', str(defaults.KELVIN), 6)
        self._vars['OUTPUT_FILENAME'] = _labeled_entry(
            f, 1, '默认输出文件名', defaults.OUTPUT_FILENAME, 30)
        self._vars['OUTPUT_NODATA'] = _labeled_entry(
            f, 2, 'NoData 值', str(defaults.OUTPUT_NODATA))

        ttk.Label(f, text='\n这些作为默认值，每次处理前可在主界面覆盖。',
                  font=('', 8)).grid(row=4, column=0, columnspan=2, sticky='w')

    # ── 标签页: MODIS 数据源 ─────────────────────────────────

    def _populate_modis(self):
        f = ttk.Frame(self._tab_modis, padding=(12, 12, 12, 12))
        f.pack(fill=tk.X)
        f.grid_columnconfigure(1, weight=1)

        ttk.Label(f, text='NASA Earthdata 凭据',
                  font=('', 9, 'bold')).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))

        ttk.Label(f, text='用户名:').grid(row=1, column=0, sticky='w', pady=2)
        self._modis_user = tk.StringVar()
        ttk.Entry(f, textvariable=self._modis_user, width=30).grid(
            row=1, column=1, sticky='ew', pady=2)

        ttk.Label(f, text='密码:').grid(row=2, column=0, sticky='w', pady=2)
        self._modis_pass = tk.StringVar()
        ttk.Entry(f, textvariable=self._modis_pass, width=30,
                  show='*').grid(row=2, column=1, sticky='ew', pady=2)

        # 尝试预填已有凭据
        try:
            from core.water_vapor import _netrc_path
            from netrc import netrc
            path = _netrc_path()
            if path:
                auth = netrc(path).authenticators('urs.earthdata.nasa.gov')
                if auth:
                    self._modis_user.set(auth[0])
                    self._modis_pass.set(auth[2])
        except Exception:
            pass

        ttk.Label(f, text=(
            '\n需要 NASA Earthdata 账号才能自动获取 MODIS 水汽数据。\n'
            '免费注册: https://urs.earthdata.nasa.gov/\n'
            '凭据保存在 ~/.netrc 文件中。'
        ), font=('', 8)).grid(row=3, column=0, columnspan=2,
                               sticky='w', pady=(12, 0))

    # ── 动作 ─────────────────────────────────────────────────

    def _load_defaults(self):
        """将所有控件重置为模块默认值。"""
        # 物理常数
        self._vars['C1'].set(f'{defaults.C1:.8e}')
        self._vars['C2'].set(f'{defaults.C2:.6f}')
        self._vars['LAMBDA_TIR'].set(f'{defaults.LAMBDA_TIR:.6f}')
        self._vars['DEFAULT_WATER_VAPOR'].set(str(defaults.DEFAULT_WATER_VAPOR))

        # NDVI & 比辐射率
        self._vars['NDVI_SOIL'].set(str(defaults.NDVI_SOIL))
        self._vars['NDVI_VEG'].set(str(defaults.NDVI_VEG))
        self._vars['EMISSIVITY_VEGETATION'].set(str(defaults.EMISSIVITY_VEGETATION))
        self._vars['EMISSIVITY_SOIL'].set(str(defaults.EMISSIVITY_SOIL))
        self._vars['EMISSIVITY_BARE_A'].set(str(defaults.EMISSIVITY_BARE_A))
        self._vars['EMISSIVITY_BARE_B'].set(str(defaults.EMISSIVITY_BARE_B))
        self._vars['EMISSIVITY_DENSE_VEG'].set(str(defaults.EMISSIVITY_DENSE_VEG))

        # 系数
        ranges = [
            ('w ∈ [0.0, 2.0)', defaults.SC_COEFFICIENTS[(0.0, 2.0)]),
            ('w ∈ [2.0, 4.0)', defaults.SC_COEFFICIENTS[(2.0, 4.0)]),
            ('w ∈ [4.0, 7.0)', defaults.SC_COEFFICIENTS[(4.0, 7.0)]),
            ('Full range',      defaults.SC_COEFFICIENTS_FULL),
        ]
        for rlabel, coeffs in ranges:
            for key, val in coeffs.items():
                self._vars[f'coeff_{rlabel}_{key}'].set(str(val))

        # 输出
        self._vars['OUTPUT_UNIT'].set('K')
        self._vars['OUTPUT_FILENAME'].set(defaults.OUTPUT_FILENAME)
        self._vars['OUTPUT_NODATA'].set(str(defaults.OUTPUT_NODATA))

    def _save(self):
        """验证并写回常量模块 + 持久化 JSON。"""
        try:
            settings = self._collect()
        except ValueError as e:
            messagebox.showerror('输入错误', str(e), parent=self)
            return

        # 写回 constants 模块
        self._apply_to_constants(settings)

        # 保存 Earthdata 凭据
        modis_user = self._modis_user.get().strip()
        modis_pass = self._modis_pass.get().strip()
        if modis_user and modis_pass:
            try:
                from core.water_vapor import save_earthdata_credentials
                save_earthdata_credentials(modis_user, modis_pass)
            except Exception as e:
                messagebox.showwarning('凭据保存失败', str(e))

        # 持久化
        self._write_json(settings)

        messagebox.showinfo('已保存', '设置已保存。\n'
                            '这些值将在下次反演时生效，\n并作为新会话的默认值。')
        self.destroy()

    def _collect(self) -> dict:
        """从控件收集并校验所有设置值，返回字典。"""
        def _f(var_name: str) -> float:
            try:
                return float(self._vars[var_name].get())
            except ValueError:
                raise ValueError(f'"{var_name}" 必须是有效数字。')

        def _s(var_name: str) -> str:
            return self._vars[var_name].get().strip()

        settings = {}

        # 物理常数
        settings['C1'] = _f('C1')
        settings['C2'] = _f('C2')
        settings['LAMBDA_TIR'] = _f('LAMBDA_TIR')
        settings['DEFAULT_WATER_VAPOR'] = _f('DEFAULT_WATER_VAPOR')

        # NDVI & 比辐射率
        settings['NDVI_SOIL'] = _f('NDVI_SOIL')
        settings['NDVI_VEG'] = _f('NDVI_VEG')
        settings['EMISSIVITY_VEGETATION'] = _f('EMISSIVITY_VEGETATION')
        settings['EMISSIVITY_SOIL'] = _f('EMISSIVITY_SOIL')
        settings['EMISSIVITY_BARE_A'] = _f('EMISSIVITY_BARE_A')
        settings['EMISSIVITY_BARE_B'] = _f('EMISSIVITY_BARE_B')
        settings['EMISSIVITY_DENSE_VEG'] = _f('EMISSIVITY_DENSE_VEG')

        # 算法系数
        ranges = [
            ('w ∈ [0.0, 2.0)', '(0.0, 2.0)'),
            ('w ∈ [2.0, 4.0)', '(2.0, 4.0)'),
            ('w ∈ [4.0, 7.0)', '(4.0, 7.0)'),
            ('Full range',     'full'),
        ]
        coeff_keys = ['a0', 'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7']
        for rlabel, _rkey in ranges:
            coeff_dict = {}
            for key in coeff_keys:
                coeff_dict[key] = _f(f'coeff_{rlabel}_{key}')
            settings[f'coeff_{rlabel}'] = coeff_dict

        # 输出
        unit = _s('OUTPUT_UNIT').upper()
        if unit not in ('K', 'C'):
            raise ValueError('温度单位必须是 K 或 C')
        settings['OUTPUT_UNIT'] = unit
        settings['OUTPUT_FILENAME'] = _s('OUTPUT_FILENAME') or defaults.OUTPUT_FILENAME
        settings['OUTPUT_NODATA'] = _f('OUTPUT_NODATA')

        return settings

    def _apply_to_constants(self, settings: dict):
        """将设置写回 utils.constants 模块命名空间，运行时生效。"""
        for key in ['C1', 'C2', 'LAMBDA_TIR', 'DEFAULT_WATER_VAPOR',
                     'NDVI_SOIL', 'NDVI_VEG',
                     'EMISSIVITY_VEGETATION', 'EMISSIVITY_SOIL',
                     'EMISSIVITY_BARE_A', 'EMISSIVITY_BARE_B',
                     'EMISSIVITY_DENSE_VEG', 'OUTPUT_NODATA',
                     'OUTPUT_FILENAME']:
            if key in settings:
                setattr(defaults, key, settings[key])

        # 单位映射
        defaults.KELVIN = settings.get('OUTPUT_UNIT', 'K')

        # 系数表
        range_map = {
            'w ∈ [0.0, 2.0)': (0.0, 2.0),
            'w ∈ [2.0, 4.0)': (2.0, 4.0),
            'w ∈ [4.0, 7.0)': (4.0, 7.0),
        }
        for rlabel, key_tuple in range_map.items():
            coeff_key = f'coeff_{rlabel}'
            if coeff_key in settings:
                defaults.SC_COEFFICIENTS[key_tuple] = settings[coeff_key]

        full_key = 'coeff_Full range'
        if full_key in settings:
            defaults.SC_COEFFICIENTS_FULL.clear()
            defaults.SC_COEFFICIENTS_FULL.update(settings[full_key])

    def _write_json(self, settings: dict):
        """持久化到 .lst_settings.json。"""
        # 将 tuple key 转为字符串以便 JSON 序列化
        serializable = {}
        for k, v in settings.items():
            if isinstance(v, dict):
                serializable[k] = v
            else:
                serializable[k] = v
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, indent=2, ensure_ascii=False)


def load_settings_from_file():
    """启动时从 JSON 文件恢复设置到 constants 模块。"""
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for key, val in data.items():
            if key.startswith('coeff_'):
                # 系数组
                rlabel = key.replace('coeff_', '')
                range_map = {
                    'w ∈ [0.0, 2.0)': (0.0, 2.0),
                    'w ∈ [2.0, 4.0)': (2.0, 4.0),
                    'w ∈ [4.0, 7.0)': (4.0, 7.0),
                }
                if rlabel in range_map:
                    defaults.SC_COEFFICIENTS[range_map[rlabel]] = val
                elif rlabel == 'Full range':
                    defaults.SC_COEFFICIENTS_FULL.clear()
                    defaults.SC_COEFFICIENTS_FULL.update(val)
            elif key == 'OUTPUT_UNIT':
                defaults.KELVIN = val  # 用于默认单位判断
            elif hasattr(defaults, key):
                setattr(defaults, key, val)
    except Exception:
        pass  # 文件损坏则忽略，使用模块默认值
