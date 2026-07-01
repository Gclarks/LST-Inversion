"""
单通道算法地表温度（LST）反演。
基于 TOA Radiance、地表比辐射率 ε 和水汽含量 w，
使用分段系数的单通道算法计算地表温度。

公式(1): B(Ts) = f(Lsen, ε, w; a₀~a₇)
公式(2): Ts = Planck⁻¹(B(Ts))
"""

import math
import os

import numpy as np
from osgeo import gdal

from utils.constants import (
    C1, C2, LAMBDA_TIR,
    SC_COEFFICIENTS, SC_COEFFICIENTS_FULL,
    OUTPUT_NODATA, KELVIN, CELSIUS,
)


# ── 系数选择 ──────────────────────────────────────────────────

def _select_coefficients(w: float) -> dict[str, float]:
    """根据水汽含量 w 选择对应的 a₀~a₇ 系数组。

    区间约定：左闭右开 [low, high)。
    超出所有分段范围时回退到 Full range 系数组。
    """
    for (low, high), coeffs in SC_COEFFICIENTS.items():
        if low <= w < high:
            return coeffs
    return SC_COEFFICIENTS_FULL


# ── 公式(1): 计算地表黑体辐亮度 B(Ts) ─────────────────────────

def calc_bt(
    l_sen: np.ndarray,
    emissivity: np.ndarray,
    w: float,
    coeffs: dict[str, float],
) -> np.ndarray:
    """计算大气校正后的地表黑体辐亮度 B(Ts)。

    公式:
        B(Ts) = a₀ + a₁·w
              + (a₂ + a₃·w + a₄·w²) × (1 / ε)
              + (a₅ + a₆·w + a₇·w²) × (L_sen / ε)

    Args:
        l_sen:      TOA Radiance 矩阵 (Band 10)，单位 W·m⁻²·sr⁻¹·μm⁻¹。
        emissivity: 地表比辐射率矩阵，范围 ~[0.9, 1.0]。
        w:          大气水汽含量 g/cm²。
        coeffs:     算法系数 {'a0'...'a7'}。

    Returns:
        B(Ts) 矩阵，单位与 L_sen 一致。
    """
    a = coeffs
    w2 = w * w

    term0 = a['a0'] + a['a1'] * w
    term1 = a['a2'] + a['a3'] * w + a['a4'] * w2
    term2 = a['a5'] + a['a6'] * w + a['a7'] * w2

    # 避免除零；ε 在正常范围内不会接近 0，但做防御
    inv_eps = np.divide(1.0, emissivity, where=emissivity > 0,
                        out=np.full_like(emissivity, 0.0))

    bt = term0 + term1 * inv_eps + term2 * inv_eps * l_sen
    return bt.astype(np.float32)


# ── 公式(2): Planck 反函数 ────────────────────────────────────

def planck_inverse(bt: np.ndarray) -> np.ndarray:
    """Planck 反函数：由黑体辐亮度求亮温。

        Ts = (C₂ / λ) / ln( C₁ / (λ⁵ · B(Ts)) + 1 )

    对非法值 (B ≤ 0) 做安全处理，返回 NaN。
    """
    lam5 = LAMBDA_TIR ** 5
    # C1 / λ⁵ → 标量
    c1_lam5 = C1 / lam5
    c2_lam = C2 / LAMBDA_TIR

    # 只对有效像元计算
    valid = bt > 0
    ts = np.full(bt.shape, np.nan, dtype=np.float32)

    if valid.any():
        arg = c1_lam5 / bt[valid] + 1.0
        # ln(arg) 对 arg ≤ 0 无定义；arg 在 bt>0 时始终 > 1
        ts[valid] = c2_lam / np.log(arg)

    return ts


# ── 完整反演流程 ──────────────────────────────────────────────

def invert_lst(
    radiance_path: str,
    emissivity_path: str,
    water_vapor: float | str,
    output_path: str,
    output_unit: str = KELVIN,
) -> str:
    """由 TOA Radiance + 比辐射率 + 水汽含量 反演地表温度。

    Args:
        radiance_path:  Band 10 TOA Radiance GeoTIFF。
        emissivity_path: 地表比辐射率 ε GeoTIFF。
        water_vapor:    水汽含量 w (g/cm²)，标量或 GeoTIFF 路径（逐像元）。
        output_path:    输出 LST GeoTIFF 路径。
        output_unit:    温度单位：'K' / 'C'。
    """
    # 判断水汽输入类型
    wv_is_raster = isinstance(water_vapor, str)
    wv_ds = None

    if wv_is_raster:
        wv_ds = gdal.Open(water_vapor, gdal.GA_ReadOnly)
        if wv_ds is None:
            raise RuntimeError(f"无法打开水汽栅格: {water_vapor}")
        # 用全域均值选系数
        wv_mean = float(np.nanmean(wv_ds.ReadAsArray()))
        coeffs = _select_coefficients(wv_mean)
    else:
        coeffs = _select_coefficients(water_vapor)

    # 打开输入
    rad_ds = gdal.Open(radiance_path, gdal.GA_ReadOnly)
    emis_ds = gdal.Open(emissivity_path, gdal.GA_ReadOnly)
    if rad_ds is None or emis_ds is None:
        raise RuntimeError(f"无法打开: {radiance_path} / {emissivity_path}")

    # 创建输出
    driver = gdal.GetDriverByName('GTiff')
    out_ds = driver.Create(
        output_path,
        rad_ds.RasterXSize, rad_ds.RasterYSize, 1,
        gdal.GDT_Float32,
    )
    out_ds.SetGeoTransform(rad_ds.GetGeoTransform())
    out_ds.SetProjection(rad_ds.GetProjection())
    out_band = out_ds.GetRasterBand(1)

    # 逐块处理
    rb = rad_ds.GetRasterBand(1)
    eb = emis_ds.GetRasterBand(1)
    bx, by = rb.GetBlockSize()
    if bx <= 0 or by <= 0:
        bx, by = 512, 512
    xsz, ysz = rad_ds.RasterXSize, rad_ds.RasterYSize

    for y in range(0, ysz, by):
        rows = min(by, ysz - y)
        for x in range(0, xsz, bx):
            cols = min(bx, xsz - x)

            l_sen = rb.ReadAsArray(x, y, cols, rows).astype(np.float32)
            emis = eb.ReadAsArray(x, y, cols, rows).astype(np.float32)

            # 水汽：标量或逐像元
            if wv_ds is not None:
                wv_block = wv_ds.ReadAsArray(x, y, cols, rows).astype(np.float32)
            else:
                wv_block = water_vapor

            # 公式(1)
            bt = calc_bt(l_sen, emis, wv_block, coeffs)
            # 公式(2)
            ts = planck_inverse(bt)

            if output_unit == CELSIUS:
                ts -= 273.15

            out_band.WriteArray(ts, x, y)

    out_band.SetNoDataValue(OUTPUT_NODATA)
    out_band.FlushCache()
    rad_ds, emis_ds, wv_ds, out_ds = None, None, None, None
    return output_path


# ── 诊断输出 ──────────────────────────────────────────────────

def get_coefficient_info(water_vapor: float) -> dict:
    """返回指定水汽值对应的系数信息，供 GUI 状态栏展示。"""
    coeffs = _select_coefficients(water_vapor)

    # 确定所属区间
    interval = 'full_range'
    for (low, high), _ in SC_COEFFICIENTS.items():
        if low <= water_vapor < high:
            interval = f'w ∈ [{low}, {high})'
            break
    else:
        interval = 'full_range (fallback)'

    return {
        'water_vapor': water_vapor,
        'interval': interval,
        'coefficients': coeffs,
    }
