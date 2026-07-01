"""
辐射定标模块。
将 Landsat 8 原始 DN 值转换为物理量：
  - 多光谱波段 (Band 4 Red, Band 5 NIR): DN → TOA Reflectance
  - 热红外波段 (Band 10 TIR):              DN → TOA Radiance
使用 GDAL 逐块读写，支持大影像，内存安全。
"""

import math
import os

import numpy as np
from osgeo import gdal, osr

from utils.constants import TOA_REFLECTANCE_MIN, TOA_REFLECTANCE_MAX


# ── 通用 GDAL 工具 ────────────────────────────────────────────

def _open_dataset(path: str) -> gdal.Dataset:
    """打开栅格文件，文件不存在时给出明确错误。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"波段文件不存在: {path}")
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL 无法打开文件: {path}")
    return ds


def _create_output(
    src_ds: gdal.Dataset,
    out_path: str,
    dtype=gdal.GDT_Float32,
    band_count: int = 1,
) -> gdal.Dataset:
    """创建与源数据集具有相同投影/地理变换/尺寸的输出 GeoTIFF。"""
    driver = gdal.GetDriverByName('GTiff')
    ds = driver.Create(
        out_path,
        src_ds.RasterXSize,
        src_ds.RasterYSize,
        band_count,
        dtype,
    )
    ds.SetGeoTransform(src_ds.GetGeoTransform())
    ds.SetProjection(src_ds.GetProjection())
    return ds


def _process_block(src_ds: gdal.Dataset, out_ds: gdal.Dataset,
                   transform_func: callable, band: int = 1):
    """逐块读取源波段，应用 transform_func 后写入输出数据集。

    Args:
        src_ds: 源 GDAL Dataset（单波段或取其第 band 波段）。
        out_ds: 输出 GDAL Dataset。
        transform_func: fn(block: np.ndarray) -> np.ndarray。
        band: 源数据集的波段号（1-based）。
    """
    src_band = src_ds.GetRasterBand(band)
    out_band = out_ds.GetRasterBand(1)

    # 使用 GDAL 推荐的块大小
    block_size = src_band.GetBlockSize()
    x_block, y_block = block_size[0], block_size[1]
    # 若块大小不合理（某些文件返回全图尺寸），回退到固定大小
    if x_block <= 0 or y_block <= 0:
        x_block, y_block = 512, 512

    x_size = src_ds.RasterXSize
    y_size = src_ds.RasterYSize

    for y in range(0, y_size, y_block):
        rows = min(y_block, y_size - y)
        for x in range(0, x_size, x_block):
            cols = min(x_block, x_size - x)
            data = src_band.ReadAsArray(x, y, cols, rows)
            result = transform_func(data)
            out_band.WriteArray(result, x, y)
        # 每写完一行释放写入缓存
        out_band.FlushCache()

    out_band.SetNoDataValue(np.nan)
    out_band.FlushCache()


# ── DN → TOA Reflectance ─────────────────────────────────────

def dn_to_toa_reflectance(
    input_path: str,
    output_path: str,
    m_rho: float,
    a_rho: float,
    sun_elevation: float,
) -> str:
    """多光谱波段辐射定标：DN → TOA Reflectance。

    Args:
        input_path:  原始波段 GeoTIFF 路径。
        output_path: 输出 TOA Reflectance GeoTIFF 路径。
        m_rho:       反射率缩放因子 M_ρ（来自 MTL）。
        a_rho:       反射率偏移量 A_ρ（来自 MTL）。
        sun_elevation: 太阳高度角（度）。

    Returns:
        output_path。
    """
    sin_se = math.sin(math.radians(sun_elevation))
    if sin_se <= 0:
        raise ValueError(f"无效的太阳高度角: {sun_elevation}°（sin = {sin_se}）")

    src_ds = _open_dataset(input_path)
    out_ds = _create_output(src_ds, output_path, gdal.GDT_Float32)

    def calibrate(block: np.ndarray) -> np.ndarray:
        # 先转为 float32 避免溢出
        dn = block.astype(np.float32)
        toa_ref = (m_rho * dn + a_rho) / sin_se
        # 裁剪到有效范围
        return np.clip(toa_ref, TOA_REFLECTANCE_MIN, TOA_REFLECTANCE_MAX)

    _process_block(src_ds, out_ds, calibrate)

    src_ds = None
    out_ds = None
    return output_path


# ── DN → TOA Radiance ────────────────────────────────────────

def dn_to_toa_radiance(
    input_path: str,
    output_path: str,
    m_l: float,
    a_l: float,
) -> str:
    """热红外波段辐射定标：DN → TOA Radiance。

    Args:
        input_path:  原始 Band 10 GeoTIFF 路径。
        output_path: 输出 TOA Radiance GeoTIFF 路径。
        m_l:         辐射亮度缩放因子 M_L。
        a_l:         辐射亮度偏移量 A_L。

    Returns:
        output_path。
    """
    src_ds = _open_dataset(input_path)
    out_ds = _create_output(src_ds, output_path, gdal.GDT_Float32)

    def calibrate(block: np.ndarray) -> np.ndarray:
        dn = block.astype(np.float32)
        # TOA Radiance, 单位: W·m⁻²·sr⁻¹·μm⁻¹
        return m_l * dn + a_l

    _process_block(src_ds, out_ds, calibrate)

    src_ds = None
    out_ds = None
    return output_path


# ── 快捷函数：从元数据一键定标 ─────────────────────────────────

def calibrate_reflectance_bands(
    band_paths: dict[int, str],
    metadata,
    temp_dir: str,
) -> dict[int, str]:
    """对 Band 4 和 Band 5 进行 TOA Reflectance 定标。

    Args:
        band_paths: {band_number: filepath}，如 {4: '...B4.tif', 5: '...B5.tif'}。
        metadata:   LandsatMetadata 实例。
        temp_dir:   临时输出目录。

    Returns:
        {band_number: output_path}。
    """
    from utils.constants import BAND_RED, BAND_NIR
    from utils.file_utils import get_temp_path

    results = {}
    for band in (BAND_RED, BAND_NIR):
        if band not in band_paths:
            raise FileNotFoundError(f"波段 {band} 文件未找到")

        m_rho = metadata.reflectance_mult(band)
        a_rho = metadata.reflectance_add(band)
        sun_el = metadata.sun_elevation

        if m_rho is None or a_rho is None:
            raise ValueError(f"缺少 Band {band} 反射率定标系数")
        if sun_el is None:
            raise ValueError("缺少太阳高度角 (SUN_ELEVATION)")

        key = 'toa_ref_b4' if band == BAND_RED else 'toa_ref_b5'
        out_path = get_temp_path(temp_dir, key)

        dn_to_toa_reflectance(
            band_paths[band], out_path,
            m_rho=m_rho, a_rho=a_rho, sun_elevation=sun_el,
        )
        results[band] = out_path

    return results


def calibrate_thermal_band(
    band_paths: dict[int, str],
    metadata,
    temp_dir: str,
) -> str:
    """对 Band 10 进行 TOA Radiance 定标。

    Returns:
        TOA Radiance GeoTIFF 路径。
    """
    from utils.constants import BAND_TIR
    from utils.file_utils import get_temp_path

    if BAND_TIR not in band_paths:
        raise FileNotFoundError(f"波段 {BAND_TIR} 文件未找到")

    m_l = metadata.radiance_mult(BAND_TIR)
    a_l = metadata.radiance_add(BAND_TIR)

    if m_l is None or a_l is None:
        raise ValueError(f"缺少 Band {BAND_TIR} 辐射定标系数")

    out_path = get_temp_path(temp_dir, 'toa_radiance')

    return dn_to_toa_radiance(
        band_paths[BAND_TIR], out_path,
        m_l=m_l, a_l=a_l,
    )
