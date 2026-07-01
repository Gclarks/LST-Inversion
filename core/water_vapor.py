"""
MODIS 大气水汽含量自动获取模块。
通过 NASA CMR API 搜索与 Landsat 8 影像时空匹配的 MODIS 水汽产品，
下载、裁剪至研究区、质量过滤后取均值，输出水汽含量 w (g/cm²)。
"""

import datetime
import math
import os
import re
import tempfile
import time
from typing import Optional
from urllib.parse import urlencode

import ssl

import numpy as np
import requests
from requests.adapters import HTTPAdapter

# ── NASA Earthdata 认证 ──────────────────────────────────────

# Earthdata URS 地址
URS_MACHINE = 'urs.earthdata.nasa.gov'

# 产品列表（按优先级）
PRODUCTS = [
    'MOD05_L2',   # Terra 水汽产品，~10:30 AM，与 Landsat 8 时间最接近
    'MYD05_L2',   # Aqua 水汽产品，~1:30 PM，回退
]

# CMR 搜索 API
CMR_GRANULES_URL = 'https://cmr.earthdata.nasa.gov/search/granules.json'


def _netrc_path() -> Optional[str]:
    """获取系统 .netrc / _netrc 文件路径。"""
    home = os.path.expanduser('~')
    for name in ('_netrc', '.netrc'):
        p = os.path.join(home, name)
        if os.path.isfile(p):
            return p
    return None


def _check_netrc_has_earthdata() -> bool:
    """检查 .netrc 中是否包含 Earthdata 凭据。"""
    path = _netrc_path()
    if not path:
        return False
    try:
        with open(path, 'r') as f:
            content = f.read()
        return f'machine {URS_MACHINE}' in content
    except Exception:
        return False


def save_earthdata_credentials(username: str, password: str):
    """将 Earthdata 凭据追加到 .netrc。"""
    home = os.path.expanduser('~')
    # Windows 使用 _netrc，Unix 使用 .netrc
    name = '_netrc' if os.name == 'nt' else '.netrc'
    path = os.path.join(home, name)

    entry = (
        f'\nmachine {URS_MACHINE}'
        f'\n    login {username}'
        f'\n    password {password}\n'
    )

    # 检查是否已有 Earthdata 条目
    if os.path.isfile(path):
        with open(path, 'r') as f:
            content = f.read()
        if f'machine {URS_MACHINE}' in content:
            # 替换已有条目
            content = re.sub(
                rf'machine\s+{re.escape(URS_MACHINE)}\s*\n\s*login\s+\S+\s*\n\s*password\s+\S+',
                entry.strip(),
                content,
            )
            with open(path, 'w') as f:
                f.write(content)
            return

    # 追加
    with open(path, 'a') as f:
        f.write(entry)


# ── CMR 搜索 ─────────────────────────────────────────────────

def _search_granules(
    product: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    bbox: tuple[float, float, float, float],
    max_results: int = 10,
) -> list[dict]:
    """搜索 CMR 获取 granules 列表。

    Args:
        product:     产品短名，如 'MOD05_L2'。
        start_time:  搜索起始时间 (UTC)。
        end_time:    搜索结束时间 (UTC)。
        bbox:        (lon_min, lat_min, lon_max, lat_max)。
        max_results: 最大返回数。

    Returns:
        granules 列表，每个 granule 包含 title, time_start, time_end, download_url。
    """
    params = {
        'short_name': product,
        'temporal': (
            f'{start_time.strftime("%Y-%m-%dT%H:%M:%SZ")},'
            f'{end_time.strftime("%Y-%m-%dT%H:%M:%SZ")}'
        ),
        'bounding_box': f'{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}',
        'page_size': max_results,
        'sort_key': '-start_date',
    }
    url = CMR_GRANULES_URL + '?' + urlencode(params)

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    entries = data.get('feed', {}).get('entry', [])
    if not isinstance(entries, list):
        entries = [entries]  # 单条结果不是列表

    granules = []
    for entry in entries:
        title = entry.get('title', '')
        time_start = entry.get('time_start', '')
        time_end = entry.get('time_end', '')

        # 找 download URL
        download_url = None
        for link in entry.get('links', []):
            href = link.get('href', '')
            if href.endswith('.hdf') or href.endswith('.hdf.gz'):
                download_url = href
                break

        if download_url:
            granules.append({
                'title': title,
                'time_start': time_start,
                'time_end': time_end,
                'download_url': download_url,
            })

    return granules


def _pick_best_granule(
    granules: list[dict],
    target_time: datetime.datetime,
) -> Optional[dict]:
    """从搜索结果中选取时间最接近目标的 granule。"""
    if not granules:
        return None

    best = None
    best_delta = float('inf')

    for g in granules:
        # 用 time_start 或 time_end 计算时间差
        for key in ('time_start', 'time_end'):
            ts = g.get(key, '')
            if not ts:
                continue
            try:
                t = datetime.datetime.strptime(
                    ts.replace('Z', ''),
                    '%Y-%m-%dT%H:%M:%S.%f' if '.' in ts else '%Y-%m-%dT%H:%M:%S',
                )
            except ValueError:
                continue
            delta = abs((t - target_time).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best = g

    return best


# ── 下载 ─────────────────────────────────────────────────────

# SSL adapter ensuring TLS 1.2+ for NASA Earthdata Cloud servers
class _TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _download_hdf(
    url: str,
    dest_dir: str,
    username: str = '',
    password: str = '',
) -> str:
    """下载 HDF 文件到目标目录，支持认证和重试。返回本地文件路径。"""
    filename = os.path.basename(url.split('?')[0])
    dest = os.path.join(dest_dir, filename)

    if os.path.exists(dest):
        return dest

    auth = (username, password) if username and password else None
    headers = {
        'User-Agent': 'LST-Inversion/1.0',
    }

    session = requests.Session()
    session.mount('https://', _TLSAdapter())

    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            resp = session.get(
                url,
                auth=auth,
                headers=headers,
                stream=True,
                timeout=(30, 120),
            )
            resp.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            session.close()
            return dest
        except requests.RequestException as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    session.close()
    raise requests.RequestException(
        f'下载失败 (已重试 {max_retries} 次): {last_error}'
    )


# ── HDF 提取 ─────────────────────────────────────────────────

def _extract_water_vapor(
    hdf_path: str,
    corners: dict[str, tuple[float, float]],
) -> Optional[float]:
    """从 MOD05_L2 HDF 文件中提取研究区水汽均值。

    使用 pyhdf 直接读取 HDF-EOS 格式，不依赖 GDAL HDF4 驱动。

    Args:
        hdf_path: MODIS HDF 文件路径。
        corners:   Landsat 8 四至角坐标 {'ul': (lat,lon), ...}。

    Returns:
        平均水汽含量 (g/cm²)，如果无法提取则返回 None。
    """
    try:
        from pyhdf.SD import SD, SDC
    except ImportError:
        raise RuntimeError(
            '缺少 pyhdf 库。请安装: conda install -c conda-forge pyhdf'
        )

    try:
        hdf = SD(hdf_path, SDC.READ)
    except Exception as e:
        raise RuntimeError(f'无法打开 HDF 文件: {e}')

    # 获取各 SDS (Scientific Data Set)
    sds_names = [s[0] for s in hdf.datasets().values()]
    for name in ['Water_Vapor_Near_Infrared', 'Latitude', 'Longitude']:
        if name not in sds_names:
            hdf.end()
            raise RuntimeError(
                f'HDF 中缺少数据层: {name}。'
                f'可用层: {sds_names}'
            )

    try:
        wv_sds = hdf.select('Water_Vapor_Near_Infrared')
        lat_sds = hdf.select('Latitude')
        lon_sds = hdf.select('Longitude')
        wv = wv_sds.get().astype(np.float32)
        lat = lat_sds.get().astype(np.float32)
        lon = lon_sds.get().astype(np.float32)

        # 尝试读取 QA
        qa = None
        if 'Water_Vapor_Near_Infrared_Quality' in sds_names:
            qa = hdf.select('Water_Vapor_Near_Infrared_Quality').get()
    finally:
        hdf.end()

    # 缩放因子: MOD05_L2 NIR Water Vapor 可能是 scaled integer 或 float
    # 如果值在数百以上则乘以 0.001（MOD05 典型 scale_factor）
    # pyhdf 读取的是原始值，需要手动缩放
    if np.nanmax(wv) > 100:
        wv *= 0.001

    # 获取研究区边界
    lats = [corners[c][0] for c in corners if corners[c] is not None]
    lons = [corners[c][1] for c in corners if corners[c] is not None]
    if len(lats) < 4 or len(lons) < 4:
        return None

    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    # 空间掩膜
    in_bbox = (
        (lat >= lat_min) & (lat <= lat_max) &
        (lon >= lon_min) & (lon <= lon_max)
    )

    # 数据质量掩膜
    wv_fill = -9999.0  # MODIS fill value
    data_valid = (wv > wv_fill) & (wv > 0) & np.isfinite(wv)

    # QA 掩膜（如果可用）
    if qa is not None:
        qa_valid = (qa >= 0) & (qa <= 1)
        mask = in_bbox & data_valid & qa_valid
    else:
        mask = in_bbox & data_valid

    count = np.sum(mask)
    if count < 10:
        # 有效像元太少（云覆盖 / 不在条带内）
        return None

    # 均值
    mean_wv = float(np.mean(wv[mask]))
    return mean_wv


# ── 公开接口 ─────────────────────────────────────────────────

class MODISWaterVaporError(Exception):
    """MODIS 水汽获取过程中的错误。"""
    pass


def fetch_water_vapor(
    acquisition_datetime: str,
    corners: dict[str, tuple[float, float]],
    username: str = '',
    password: str = '',
    time_window_hours: float = 1.0,
) -> tuple[float, str]:
    """获取 Landsat 8 影像对应的大气水汽含量。

    按优先级尝试不同 MODIS 产品，返回最匹配的水汽值。

    Args:
        acquisition_datetime: 成像时间字符串 "YYYY-MM-DD HH:MM:SS"。
        corners:              四至角坐标 {'ul': (lat,lon), ...}。
        username:             Earthdata 用户名。
        password:             Earthdata 密码。
        time_window_hours:    搜索时间窗口（小时），默认 ±1。

    Returns:
        (water_vapor_g_cm2, source_info)

    Raises:
        MODISWaterVaporError: 所有产品尝试失败时。
    """
    # 尝试从 .netrc 获取凭据
    if not username or not password:
        try:
            from netrc import netrc
            auth = netrc(_netrc_path()).authenticators(URS_MACHINE)
            if auth:
                username = auth[0]
                password = auth[2]
        except Exception:
            pass

    if not username or not password:
        raise MODISWaterVaporError(
            '未配置 Earthdata 凭据。\n'
            '请先注册 https://urs.earthdata.nasa.gov/ 账号，\n'
            '然后在设置中填写用户名和密码。'
        )

    # 解析成像时间（兼容多种格式 & 微秒截断）
    dt_str = acquisition_datetime.strip()
    # 去掉尾部 Z（UTC 标记）
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1]
    # 截断多余的小数秒位（Python %f 只支持 6 位微秒）
    dt_str = re.sub(r'(\.\d{6})\d+', r'\1', dt_str)
    # 尝试多种格式
    formats = [
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
    ]
    target_time = None
    for fmt in formats:
        try:
            target_time = datetime.datetime.strptime(dt_str, fmt)
            break
        except ValueError:
            continue
    if target_time is None:
        raise MODISWaterVaporError(
            f'无法解析成像时间: {acquisition_datetime}'
        )

    # 构建搜索窗口
    window = datetime.timedelta(hours=time_window_hours)
    start_time = target_time - window
    end_time = target_time + window

    # 构建包围盒
    bbox = _corners_to_bbox(corners)
    if bbox is None:
        raise MODISWaterVaporError('无法从角坐标构建包围盒')

    temp_dir = tempfile.mkdtemp(prefix='modis_wv_')
    errors = []

    for product in PRODUCTS:
        try:
            # 搜索
            granules = _search_granules(product, start_time, end_time, bbox)
            if not granules:
                errors.append(f'{product}: 无匹配数据')
                continue

            # 选最佳
            best = _pick_best_granule(granules, target_time)
            if best is None:
                errors.append(f'{product}: 无法确定最佳 granule')
                continue

            # 下载
            hdf_path = _download_hdf(
                best['download_url'], temp_dir,
                username=username, password=password,
            )

            # 提取
            wv = _extract_water_vapor(hdf_path, corners)
            if wv is not None:
                source = (
                    f'{product} ({os.path.basename(best["download_url"])})'
                )
                return wv, source
            else:
                errors.append(
                    f'{product}: 研究区无有效水汽像元（云覆盖/超出条带）'
                )

        except requests.RequestException as e:
            errors.append(f'{product}: 网络错误 — {e}')
        except Exception as e:
            errors.append(f'{product}: {e}')
        finally:
            # 清理下载的 HDF 文件
            _cleanup_temp_dir(temp_dir, keep_dir=True)

    # 回退：扩大时间窗口
    if time_window_hours < 3.0:
        _cleanup_temp_dir(temp_dir, keep_dir=False)
        try:
            return fetch_water_vapor(
                acquisition_datetime, corners,
                username=username, password=password,
                time_window_hours=3.0,
            )
        except MODISWaterVaporError:
            pass

    _cleanup_temp_dir(temp_dir, keep_dir=False)
    raise MODISWaterVaporError(
        '未能获取有效水汽含量:\n' + '\n'.join(f'  • {e}' for e in errors)
    )


# ── 工具函数 ─────────────────────────────────────────────────

def _corners_to_bbox(
    corners: dict[str, tuple[float, float]],
) -> Optional[tuple[float, float, float, float]]:
    """从四至角坐标构建 (lon_min, lat_min, lon_max, lat_max)。"""
    lats = [corners[c][0] for c in corners if corners[c] is not None]
    lons = [corners[c][1] for c in corners if corners[c] is not None]
    if not lats or not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def _cleanup_temp_dir(temp_dir: str, keep_dir: bool = False):
    """清理临时目录。"""
    try:
        for f in os.listdir(temp_dir):
            os.unlink(os.path.join(temp_dir, f))
        if not keep_dir:
            os.rmdir(temp_dir)
    except Exception:
        pass
