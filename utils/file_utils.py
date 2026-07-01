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


# ── 缓存文件管理 ────────────────────────────────────────────

# 中间产品文件名
TEMP_FILES = {
    'toa_radiance':    'toa_radiance.tif',        # Band 10 TOA Radiance
    'toa_ref_b4':      'toa_reflectance_b4.tif',  # Band 4 TOA Reflectance
    'toa_ref_b5':      'toa_reflectance_b5.tif',  # Band 5 TOA Reflectance
    'ndvi':            'ndvi.tif',
    'emissivity':      'emissivity.tif',
    'modis_hdf':       'modis_wv.hdf',            # MODIS 水汽 HDF
    'inversion_log':   'inversion_log.txt',        # 处理日志
}


def _get_project_root() -> str:
    """获取项目根目录路径。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_cache_dir() -> str:
    """获取缓存根目录路径 (项目根/cache/)。"""
    return os.path.join(_get_project_root(), 'cache')


def create_run_cache_dir() -> str:
    """在 cache/ 下创建时间戳子文件夹并返回路径。"""
    import datetime
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(get_cache_dir(), timestamp)
    os.makedirs(path, exist_ok=True)
    return path


def get_cache_path(cache_dir: str, key: str) -> str:
    """获取指定中间产品在缓存目录中的完整路径。

    Args:
        cache_dir: 缓存目录路径（如 create_run_cache_dir 的返回值）。
        key: TEMP_FILES 中的键名。
    """
    filename = TEMP_FILES.get(key)
    if filename is None:
        raise KeyError(f"未知的缓存文件键名: {key}。可用: {list(TEMP_FILES)}")
    return os.path.join(cache_dir, filename)


def list_cache_folders() -> list[dict]:
    """列出所有缓存子文件夹及其信息。

    Returns:
        [{'name': '20260701_143052', 'path': '...', 'size_mb': 12.3, 'file_count': 6}, ...]
    """
    cache_dir = get_cache_dir()
    if not os.path.isdir(cache_dir):
        return []
    result = []
    for name in sorted(os.listdir(cache_dir), reverse=True):
        path = os.path.join(cache_dir, name)
        if not os.path.isdir(path):
            continue
        total_size = 0
        file_count = 0
        for dirpath, _, filenames in os.walk(path):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    total_size += os.path.getsize(fp)
                except OSError:
                    pass
                file_count += 1
        result.append({
            'name': name,
            'path': path,
            'size_mb': round(total_size / (1024 * 1024), 2),
            'file_count': file_count,
        })
    return result


def delete_cache_folder(name: str) -> bool:
    """删除指定名称的缓存子文件夹。

    Returns:
        True 表示删除成功，False 表示不存在。
    """
    path = os.path.join(get_cache_dir(), name)
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True
