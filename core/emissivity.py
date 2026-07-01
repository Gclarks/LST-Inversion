"""
NDVI 计算与地表比辐射率（LSE）估算。
基于 TOA Reflectance (Band 4 Red + Band 5 NIR) 计算 NDVI，
通过植被覆盖度（FVC）分段估算 Landsat 8 Band 10 的地表比辐射率 ε。
"""

import os

import numpy as np
from osgeo import gdal

from utils.constants import (
    NDVI_SOIL, NDVI_VEG,
    EMISSIVITY_VEGETATION, EMISSIVITY_SOIL,
    EMISSIVITY_BARE_A, EMISSIVITY_BARE_B,
    EMISSIVITY_DENSE_VEG,
    EMISSIVITY_GEOMETRIC_FACTOR,
)


# ── 块读取工具 ────────────────────────────────────────────────

def _read_blocks(ds: gdal.Dataset, band: int = 1):
    """生成器：逐块 yield (data, x, y, cols, rows)。"""
    rb = ds.GetRasterBand(band)
    bx, by = rb.GetBlockSize()
    if bx <= 0 or by <= 0:
        bx, by = 512, 512
    xsz, ysz = ds.RasterXSize, ds.RasterYSize
    for y in range(0, ysz, by):
        rows = min(by, ysz - y)
        for x in range(0, xsz, bx):
            cols = min(bx, xsz - x)
            yield rb.ReadAsArray(x, y, cols, rows), x, y, cols, rows


def _create_match(src_ds: gdal.Dataset, out_path: str,
                  dtype=gdal.GDT_Float32) -> gdal.Dataset:
    """创建与源数据集投影/尺寸一致的输出 GeoTIFF。"""
    driver = gdal.GetDriverByName('GTiff')
    ds = driver.Create(out_path, src_ds.RasterXSize, src_ds.RasterYSize,
                       1, dtype)
    ds.SetGeoTransform(src_ds.GetGeoTransform())
    ds.SetProjection(src_ds.GetProjection())
    return ds


# ── NDVI ─────────────────────────────────────────────────────

def calc_ndvi(
    red_path: str,
    nir_path: str,
    out_path: str,
) -> str:
    """计算 NDVI：(NIR − Red) / (NIR + Red)。

    Args:
        red_path: Band 4 TOA Reflectance GeoTIFF。
        nir_path: Band 5 TOA Reflectance GeoTIFF。
        out_path: 输出 NDVI GeoTIFF 路径。

    Returns:
        out_path。

    NDVI 理论范围 [−1, 1]，输出时 clip 到此范围。
    分母为 0 的像元（如水体）设为 0。
    """
    red_ds = gdal.Open(red_path, gdal.GA_ReadOnly)
    nir_ds = gdal.Open(nir_path, gdal.GA_ReadOnly)
    if red_ds is None or nir_ds is None:
        raise RuntimeError(f"无法打开 NDVI 输入文件: {red_path} / {nir_path}")

    out_ds = _create_match(red_ds, out_path, gdal.GDT_Float32)
    out_band = out_ds.GetRasterBand(1)

    # 逐块读取两个波段对应区域
    rb = red_ds.GetRasterBand(1)
    nb = nir_ds.GetRasterBand(1)
    bx, by = rb.GetBlockSize()
    if bx <= 0 or by <= 0:
        bx, by = 512, 512
    xsz, ysz = red_ds.RasterXSize, red_ds.RasterYSize

    for y in range(0, ysz, by):
        rows = min(by, ysz - y)
        for x in range(0, xsz, bx):
            cols = min(bx, xsz - x)
            red = rb.ReadAsArray(x, y, cols, rows).astype(np.float32)
            nir = nb.ReadAsArray(x, y, cols, rows).astype(np.float32)

            denom = red + nir
            ndvi = np.zeros_like(red)
            mask = denom != 0
            ndvi[mask] = (nir[mask] - red[mask]) / denom[mask]
            ndvi = np.clip(ndvi, -1.0, 1.0)

            out_band.WriteArray(ndvi, x, y)

    out_band.SetNoDataValue(np.nan)
    out_band.FlushCache()
    red_ds, nir_ds, out_ds = None, None, None
    return out_path


# ── 植被覆盖度 FVC ──────────────────────────────────────────

def calc_fvc(
    ndvi_path: str,
    out_path: str,
    ndvi_soil: float = NDVI_SOIL,
    ndvi_veg: float = NDVI_VEG,
) -> str:
    """由 NDVI 计算植被覆盖度（Fractional Vegetation Cover）。

    P_v = ((NDVI − NDVI_soil) / (NDVI_veg − NDVI_soil))²

    结果裁剪到 [0, 1]。
    """
    ndvi_ds = gdal.Open(ndvi_path, gdal.GA_ReadOnly)
    if ndvi_ds is None:
        raise RuntimeError(f"无法打开 NDVI 文件: {ndvi_path}")

    out_ds = _create_match(ndvi_ds, out_path, gdal.GDT_Float32)
    out_band = out_ds.GetRasterBand(1)
    denom = ndvi_veg - ndvi_soil

    for block, x, y, cols, rows in _read_blocks(ndvi_ds):
        # P_v 仅对混合像元有意义，边界外会被截断
        pv = ((block - ndvi_soil) / denom) ** 2
        pv = np.clip(pv, 0.0, 1.0)
        out_band.WriteArray(pv, x, y)

    out_band.SetNoDataValue(np.nan)
    out_band.FlushCache()
    ndvi_ds, out_ds = None, None
    return out_path


# ── 地表比辐射率 ε ──────────────────────────────────────────

def calc_emissivity(
    ndvi_path: str,
    red_ref_path: str,
    out_path: str,
    use_cavity: bool = False,
) -> str:
    """由 NDVI 和 Red 反射率估算 Landsat 8 Band 10 地表比辐射率。

    分段策略（Sobrino et al.）：

    +-------------------+---------------------------------------------+
    | NDVI 范围          | 公式                                         |
    +-------------------+---------------------------------------------+
    | NDVI < 0.2         | ε = 0.973 − 0.047 × ρ_red                    |
    | 0.2 ≤ NDVI ≤ 0.5   | ε = ε_v·P_v + ε_s·(1−P_v) + Δε              |
    | NDVI > 0.5         | ε = 0.99                                     |
    +-------------------+---------------------------------------------+

    Δε 为腔体效应项（可选，默认忽略）：
        Δε = (1 − ε_s) × (1 − P_v) × F × ε_v,   F = 0.55

    Args:
        ndvi_path:     NDVI GeoTIFF 路径。
        red_ref_path:  Band 4 TOA Reflectance GeoTIFF 路径。
        out_path:      输出 emissivity GeoTIFF 路径。
        use_cavity:    是否启用腔体效应项 Δε。

    Returns:
        out_path。
    """
    ndvi_ds = gdal.Open(ndvi_path, gdal.GA_ReadOnly)
    red_ds = gdal.Open(red_ref_path, gdal.GA_ReadOnly)
    if ndvi_ds is None or red_ds is None:
        raise RuntimeError(f"无法打开文件: {ndvi_path} / {red_ref_path}")

    out_ds = _create_match(ndvi_ds, out_path, gdal.GDT_Float32)
    out_band = out_ds.GetRasterBand(1)

    eps_v = EMISSIVITY_VEGETATION
    eps_s = EMISSIVITY_SOIL
    denom_pv = NDVI_VEG - NDVI_SOIL

    nb = ndvi_ds.GetRasterBand(1)
    rb = red_ds.GetRasterBand(1)
    bx, by = nb.GetBlockSize()
    if bx <= 0 or by <= 0:
        bx, by = 512, 512
    xsz, ysz = ndvi_ds.RasterXSize, ndvi_ds.RasterYSize

    for y in range(0, ysz, by):
        rows = min(by, ysz - y)
        for x in range(0, xsz, bx):
            cols = min(bx, xsz - x)
            ndvi = nb.ReadAsArray(x, y, cols, rows).astype(np.float32)
            red = rb.ReadAsArray(x, y, cols, rows).astype(np.float32)

            eps = np.full_like(ndvi, EMISSIVITY_DENSE_VEG, dtype=np.float32)

            # 混合像元区域: 0.2 ≤ NDVI ≤ 0.5
            mix_mask = (ndvi >= NDVI_SOIL) & (ndvi <= NDVI_VEG)
            if mix_mask.any():
                pv = ((ndvi[mix_mask] - NDVI_SOIL) / denom_pv) ** 2
                pv = np.clip(pv, 0.0, 1.0)
                eps[mix_mask] = eps_v * pv + eps_s * (1.0 - pv)
                if use_cavity:
                    delta = (1.0 - eps_s) * (1.0 - pv) * EMISSIVITY_GEOMETRIC_FACTOR * eps_v
                    eps[mix_mask] += delta

            # 裸土 / 水体区域: NDVI < 0.2
            bare_mask = ndvi < NDVI_SOIL
            if bare_mask.any():
                r = np.clip(red[bare_mask], 0.0, 1.0)
                eps[bare_mask] = EMISSIVITY_BARE_A - EMISSIVITY_BARE_B * r

            out_band.WriteArray(eps, x, y)

    out_band.SetNoDataValue(np.nan)
    out_band.FlushCache()
    ndvi_ds, red_ds, out_ds = None, None, None
    return out_path


# ── 一站式流水线 ──────────────────────────────────────────────

def run_emissivity_pipeline(
    toa_b4_path: str,
    toa_b5_path: str,
    ndvi_out: str,
    emissivity_out: str,
    use_cavity: bool = False,
) -> tuple[str, str]:
    """TOA Reflectance → NDVI → Emissivity 一站式处理。

    Returns:
        (ndvi_out, emissivity_out)。
    """
    calc_ndvi(toa_b4_path, toa_b5_path, ndvi_out)
    calc_emissivity(ndvi_out, toa_b4_path, emissivity_out, use_cavity=use_cavity)
    return ndvi_out, emissivity_out
