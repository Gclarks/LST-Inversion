"""
Landsat 8 目录扫描、波段文件识别、元数据文件发现、临时文件管理。
"""

import os
import re
import shutil
from typing import Optional


# ── 波段识别 ────────────────────────────────────────────────

# Landsat 8 波段文件名模式：Collection 1 & 2 均以 _B{n}.TIF 结尾
_BAND_PATTERN = re.compile(r'_B(\d{1,2})\.(?:TIF|tif)$')

# 本次反演必需的波段
REQUIRED_BANDS = (4, 5, 10)

# Landsat 8 OLI/TIRS 全部波段（仅供参考）
ALL_BANDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)


def scan_landsat_directory(input_dir: str) -> dict:
    """扫描 Landsat 8 影像目录，识别波段文件和 MTL 元数据文件。

    Args:
        input_dir: 影像目录路径。

    Returns:
        {
            'bands': {1: 'path/to/B1.tif', ..., 11: 'path/to/B11.tif'},
            'mtl': 'path/to/MTL.txt' | None,
            'missing_required': [4, 10, ...],
        }
    """
    if not os.path.isdir(input_dir):
        raise NotADirectoryError(f"目录不存在或无法访问: {input_dir}")

    bands: dict[int, str] = {}
    mtl_path: Optional[str] = None

    with os.scandir(input_dir) as entries:
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name

            # 尝试匹配波段文件
            m = _BAND_PATTERN.search(name)
            if m:
                band_num = int(m.group(1))
                if 1 <= band_num <= 11:
                    bands[band_num] = entry.path
                continue

            # 尝试匹配 MTL 文件（*_MTL.txt）
            if name.endswith('_MTL.txt') or name.endswith('_MTL.TXT'):
                if mtl_path is None:
                    mtl_path = entry.path

    missing_required = [b for b in REQUIRED_BANDS if b not in bands]

    return {
        'bands': dict(sorted(bands.items())),
        'mtl': mtl_path,
        'missing_required': missing_required,
    }


def validate_input_directory(input_dir: str) -> dict:
    """验证输入目录是否包含可用的 Landsat 8 数据。

    Returns:
        scan_landsat_directory 的返回值，额外增加 'valid' (bool) 和 'errors' (list[str])。
    """
    result = scan_landsat_directory(input_dir)
    errors: list[str] = []

    if result['missing_required']:
        errors.append(
            f"缺少必需波段 Band {result['missing_required']} "
            f"(路径: {input_dir})"
        )

    if result['mtl'] is None:
        errors.append(f"未找到 MTL 元数据文件 (*_MTL.txt) (路径: {input_dir})")

    if not result['bands']:
        errors.append(f"未识别到任何 Landsat 8 波段文件 (路径: {input_dir})")

    result['valid'] = len(errors) == 0
    result['errors'] = errors
    return result


# ── 临时文件管理 ────────────────────────────────────────────

TEMP_DIR_NAME = '_temp_'

# 中间产品文件名
TEMP_FILES = {
    'toa_radiance':    'toa_radiance.tif',        # Band 10 TOA Radiance
    'toa_ref_b4':      'toa_reflectance_b4.tif',  # Band 4 TOA Reflectance
    'toa_ref_b5':      'toa_reflectance_b5.tif',  # Band 5 TOA Reflectance
    'ndvi':            'ndvi.tif',
    'emissivity':      'emissivity.tif',
}


def create_temp_dir(base_path: str) -> str:
    """在 base_path 下创建临时目录，返回其完整路径。已存在则不报错。"""
    temp_dir = os.path.join(base_path, TEMP_DIR_NAME)
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def get_temp_path(temp_dir: str, key: str) -> str:
    """获取指定中间产品在临时目录中的完整路径。

    Args:
        temp_dir: 临时目录路径。
        key: TEMP_FILES 中的键名，如 'toa_radiance', 'ndvi' 等。
    """
    filename = TEMP_FILES.get(key)
    if filename is None:
        raise KeyError(f"未知的临时文件键名: {key}。可用: {list(TEMP_FILES)}")
    return os.path.join(temp_dir, filename)


def cleanup_temp_dir(temp_dir: str) -> bool:
    """删除临时目录及其全部内容。

    Returns:
        True 表示删除成功，False 表示目录不存在。
    """
    if not os.path.isdir(temp_dir):
        return False
    shutil.rmtree(temp_dir)
    return True
