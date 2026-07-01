"""
MODIS 大气水汽含量自动获取模块。
通过 NASA CMR API 搜索与 Landsat 8 影像时空匹配的 MODIS 水汽产品，
下载、裁剪至研究区、质量过滤后取均值，输出水汽含量 w (g/cm²)。

自动适配 HDF4 / HDF5 / GDAL 三种数据格式。
"""

import datetime
import gzip
import math
import os
import re
import ssl
import tempfile
import time
from typing import Optional
from urllib.parse import urlencode

import numpy as np
import requests
from requests.adapters import HTTPAdapter


# ── SSL 适配器 ────────────────────────────────────────────────

class _TLSAdapter(HTTPAdapter):
    """强制 TLS 1.2+，兼容 NASA Earthdata Cloud 服务器。"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ── NASA Earthdata 认证 ──────────────────────────────────────

URS_MACHINE = 'urs.earthdata.nasa.gov'

PRODUCTS = ['MOD05_L2', 'MYD05_L2']

CMR_GRANULES_URL = 'https://cmr.earthdata.nasa.gov/search/granules.json'


def _netrc_path() -> Optional[str]:
    home = os.path.expanduser('~')
    for name in ('_netrc', '.netrc'):
        p = os.path.join(home, name)
        if os.path.isfile(p):
            return p
    return None


def _check_netrc_has_earthdata() -> bool:
    path = _netrc_path()
    if not path:
        return False
    try:
        with open(path, 'r') as f:
            return f'machine {URS_MACHINE}' in f.read()
    except Exception:
        return False


def save_earthdata_credentials(username: str, password: str):
    home = os.path.expanduser('~')
    name = '_netrc' if os.name == 'nt' else '.netrc'
    path = os.path.join(home, name)
    entry = (
        f'\nmachine {URS_MACHINE}'
        f'\n    login {username}'
        f'\n    password {password}\n'
    )
    if os.path.isfile(path):
        with open(path, 'r') as f:
            content = f.read()
        if f'machine {URS_MACHINE}' in content:
            content = re.sub(
                rf'machine\s+{re.escape(URS_MACHINE)}\s*\n\s*login\s+\S+\s*\n\s*password\s+\S+',
                entry.strip(), content,
            )
            with open(path, 'w') as f:
                f.write(content)
            return
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
        entries = [entries]

    granules = []
    for entry in entries:
        download_url = None
        for link in entry.get('links', []):
            href = link.get('href', '')
            if href.endswith('.hdf') or href.endswith('.hdf.gz'):
                download_url = href
                break
        if download_url:
            granules.append({
                'title': entry.get('title', ''),
                'time_start': entry.get('time_start', ''),
                'time_end': entry.get('time_end', ''),
                'download_url': download_url,
            })
    return granules


def _pick_best_granule(
    granules: list[dict],
    target_time: datetime.datetime,
) -> Optional[dict]:
    best, best_delta = None, float('inf')
    for g in granules:
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

def _download_hdf(
    url: str, dest_dir: str,
    username: str = '', password: str = '',
) -> str:
    filename = os.path.basename(url.split('?')[0])
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest

    auth = (username, password) if username and password else None
    headers = {'User-Agent': 'LST-Inversion/1.0'}
    session = requests.Session()
    session.mount('https://', _TLSAdapter())

    last_error = None
    for attempt in range(3):
        try:
            resp = session.get(url, auth=auth, headers=headers,
                               stream=True, timeout=(30, 120))
            resp.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            size = os.path.getsize(dest)
            if size < 1000:
                raise requests.RequestException(f'文件异常小 ({size} 字节)')
            session.close()
            return dest
        except requests.RequestException as e:
            last_error = e
            if os.path.exists(dest):
                os.unlink(dest)
            if attempt < 2:
                time.sleep(2 ** attempt)
    session.close()
    raise requests.RequestException(f'下载失败 (已重试 3 次): {last_error}')


# ── HDF 提取（多格式）────────────────────────────────────────

def _extract_water_vapor(
    hdf_path: str,
    corners: dict[str, tuple[float, float]],
) -> Optional[float]:
    """从 MODIS HDF 提取研究区水汽均值，自动适配 HDF4/HDF5/GDAL。"""
    if hdf_path.endswith('.gz'):
        decompressed = hdf_path[:-3]
        with gzip.open(hdf_path, 'rb') as gz:
            with open(decompressed, 'wb') as out:
                out.write(gz.read())
        hdf_path = decompressed

    if not os.path.isfile(hdf_path):
        raise RuntimeError(f'HDF 文件不存在: {hdf_path}')
    file_size = os.path.getsize(hdf_path)
    if file_size < 1000:
        raise RuntimeError(f'HDF 文件损坏（大小仅 {file_size} 字节）')

    errors = []
    for reader in (_read_hdf4, _read_hdf5, _read_gdal):
        try:
            data = reader(hdf_path)
            if data is not None:
                wv, lat, lon, qa = data
                return _compute_mean_wv(wv, lat, lon, qa, corners)
            # reader 返回 None = 不能识别，记录原因
            errors.append(f'{reader.__name__}: 无法识别该格式')
        except RuntimeError as e:
            raise  # 结构性错误直接抛出
        except Exception as e:
            errors.append(f'{reader.__name__}: {e}')
            continue

    raise RuntimeError(
        f'无法读取 HDF 文件。\n'
        f'文件: {os.path.basename(hdf_path)}\n'
        f'大小: {file_size} 字节\n'
        + '\n'.join(errors)
    )


def _read_hdf4(hdf_path: str):
    """pyhdf — HDF4 格式。返回 (wv, lat, lon, qa) 或 None。"""
    try:
        from pyhdf.SD import SD, SDC
    except ImportError:
        return None
    try:
        hdf = SD(hdf_path, SDC.READ)
    except Exception:
        return None

    names = list(hdf.datasets().keys())
    if 'Water_Vapor_Near_Infrared' not in names:
        hdf.end()
        return None

    wv = hdf.select('Water_Vapor_Near_Infrared').get().astype(np.float32)

    lat = lon = None
    for n in ('Latitude', 'Latitude_1km'):
        if n in names: lat = hdf.select(n).get().astype(np.float32); break
    for n in ('Longitude', 'Longitude_1km'):
        if n in names: lon = hdf.select(n).get().astype(np.float32); break

    qa = None
    for n in ('Water_Vapor_Near_Infrared_Quality', 'Quality_Assurance_NIR'):
        if n in names: qa = hdf.select(n).get(); break

    hdf.end()
    if lat is None or lon is None:
        return None
    if np.nanmax(wv) > 100:
        wv *= 0.001
    return _align_resolution(wv, lat, lon, qa)


def _read_hdf5(hdf_path: str):
    """h5py — HDF5 格式（NASA Cloud）。返回 (wv, lat, lon, qa) 或 None。"""
    try:
        import h5py
    except ImportError:
        return None
    try:
        f = h5py.File(hdf_path, 'r')
    except Exception:
        return None

    wv_paths, lat_paths, lon_paths, qa_paths = [], [], [], []

    def _search(name, obj):
        if isinstance(obj, h5py.Dataset):
            bn = os.path.basename(name)
            if bn == 'Water_Vapor_Near_Infrared':
                wv_paths.append(name)
            elif bn in ('Latitude', 'Latitude_1km'):
                lat_paths.append(name)
            elif bn in ('Longitude', 'Longitude_1km'):
                lon_paths.append(name)
            elif 'Quality' in bn and 'NIR' in bn:
                qa_paths.append(name)

    f.visititems(_search)
    if not wv_paths or not lat_paths or not lon_paths:
        # 收集所有叶子数据集名帮助诊断
        all_leaves = []
        def _collect(name, obj):
            if isinstance(obj, h5py.Dataset):
                all_leaves.append(name)
        f.visititems(_collect)
        f.close()
        raise RuntimeError(
            f'HDF5 文件结构不匹配。'
            f'找到 {len(all_leaves)} 个数据集，但缺少所需字段。\n'
            f'预期: Water_Vapor_Near_Infrared / Latitude / Longitude\n'
            f'实际前20个: {all_leaves[:20]}'
        )

    wv = f[wv_paths[0]][()].astype(np.float32)
    lat = f[lat_paths[0]][()].astype(np.float32)
    lon = f[lon_paths[0]][()].astype(np.float32)
    qa = f[qa_paths[0]][()] if qa_paths else None
    f.close()

    if np.nanmax(wv) > 100:
        wv *= 0.001
    return _align_resolution(wv, lat, lon, qa)


def _read_gdal(hdf_path: str):
    """GDAL — 兜底方案。返回 (wv, lat, lon, qa) 或 None。"""
    try:
        from osgeo import gdal
    except ImportError:
        return None

    ds = gdal.Open(hdf_path)
    if ds is None:
        return None

    meta = ds.GetMetadata('SUBDATASETS')
    ds = None
    if not meta:
        return None

    subdatasets = {}
    for k, v in meta.items():
        m = re.match(r'SUBDATASET_\d+_(NAME|DESC)', k)
        if m:
            subdatasets.setdefault(m.group(1), {})[k] = v

    # 构建 NAME→path 映射
    name_to_path = {}
    for k, v in meta.items():
        m = re.match(r'SUBDATASET_(\d+)_NAME', k)
        if m:
            desc_key = k.replace('_NAME', '_DESC')
            if desc_key in meta:
                name_to_path[meta[desc_key]] = v

    def _find(*patterns):
        for pat in patterns:
            for desc, path in name_to_path.items():
                if re.search(pat, desc, re.IGNORECASE):
                    d = gdal.Open(path)
                    if d:
                        arr = d.ReadAsArray().astype(np.float32)
                        d = None
                        return arr
        return None

    wv = _find('Water_Vapor_Near_Infrared')
    lat = _find('Latitude')
    lon = _find('Longitude')
    qa = _find(r'Water_Vapor_Near_Infrared_Quality|Quality_Assurance_NIR')

    if wv is None or lat is None or lon is None:
        return None
    if np.nanmax(wv) > 100:
        wv *= 0.001
    return _align_resolution(wv, lat, lon, qa)


def _align_resolution(wv, lat, lon, qa):
    """分辨率对齐：若 wv 与 lat/lon 形状不同，降采样 wv。"""
    if lat.shape != wv.shape:
        h, w = lat.shape
        bh, bw = wv.shape[0] // h, wv.shape[1] // w
        if bh > 0 and bw > 0:
            wv = wv[:bh * h, :bw * w].reshape(h, bh, w, bw).mean(axis=(1, 3))
    return wv, lat, lon, qa


def _compute_mean_wv(wv, lat, lon, qa, corners) -> Optional[float]:
    """计算研究区水汽均值。"""
    lats = [corners[c][0] for c in corners if corners[c] is not None]
    lons = [corners[c][1] for c in corners if corners[c] is not None]
    if len(lats) < 4 or len(lons) < 4:
        return None

    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    in_bbox = ((lat >= lat_min) & (lat <= lat_max) &
               (lon >= lon_min) & (lon <= lon_max))
    data_valid = (wv > -9999) & (wv > 0) & np.isfinite(wv)

    if qa is not None and qa.shape == wv.shape:
        mask = in_bbox & data_valid & ((qa >= 0) & (qa <= 1))
    else:
        mask = in_bbox & data_valid

    count = np.sum(mask)
    if count < 10:
        return None
    return float(np.mean(wv[mask]))


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
    cache_dir: str = '',
) -> tuple[float, str]:
    """获取 Landsat 8 影像对应的大气水汽含量。"""
    if not username or not password:
        try:
            from netrc import netrc
            path = _netrc_path()
            if path:
                auth = netrc(path).authenticators(URS_MACHINE)
                if auth:
                    username, password = auth[0], auth[2]
        except Exception:
            pass

    if not username or not password:
        raise MODISWaterVaporError(
            '未配置 Earthdata 凭据。\n'
            '请先注册 https://urs.earthdata.nasa.gov/ 账号，\n'
            '然后在设置中填写用户名和密码。'
        )

    # 解析成像时间
    dt_str = acquisition_datetime.strip()
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1]
    dt_str = re.sub(r'(\.\d{6})\d+', r'\1', dt_str)
    target_time = None
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            target_time = datetime.datetime.strptime(dt_str, fmt)
            break
        except ValueError:
            continue
    if target_time is None:
        raise MODISWaterVaporError(f'无法解析成像时间: {acquisition_datetime}')

    window = datetime.timedelta(hours=time_window_hours)
    start_time = target_time - window
    end_time = target_time + window

    bbox = _corners_to_bbox(corners)
    if bbox is None:
        raise MODISWaterVaporError('无法从角坐标构建包围盒')

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        temp_dir = cache_dir
    else:
        temp_dir = tempfile.mkdtemp(prefix='modis_wv_')
    errors = []

    for product in PRODUCTS:
        try:
            granules = _search_granules(product, start_time, end_time, bbox)
            if not granules:
                errors.append(f'{product}: 无匹配数据')
                continue
            best = _pick_best_granule(granules, target_time)
            if best is None:
                errors.append(f'{product}: 无法确定最佳 granule')
                continue
            hdf_path = _download_hdf(
                best['download_url'], temp_dir,
                username=username, password=password,
            )
            wv = _extract_water_vapor(hdf_path, corners)
            if wv is not None:
                return wv, f'{product} ({os.path.basename(best["download_url"])})'
            else:
                errors.append(f'{product}: 研究区无有效水汽像元（云覆盖/超出条带）')
        except requests.RequestException as e:
            errors.append(f'{product}: 网络错误 — {e}')
        except RuntimeError as e:
            errors.append(f'{product}: {e}')
        except Exception as e:
            errors.append(f'{product}: {e}')

    if time_window_hours < 3.0:
        try:
            return fetch_water_vapor(
                acquisition_datetime, corners,
                username=username, password=password,
                time_window_hours=3.0, cache_dir=cache_dir,
            )
        except MODISWaterVaporError:
            pass

    if not cache_dir:
        for f in os.listdir(temp_dir):
            os.unlink(os.path.join(temp_dir, f))
        os.rmdir(temp_dir)
    raise MODISWaterVaporError(
        '未能获取有效水汽含量:\n' + '\n'.join(f'  • {e}' for e in errors)
    )


# ── 工具函数 ─────────────────────────────────────────────────

def _corners_to_bbox(
    corners: dict[str, tuple[float, float]],
) -> Optional[tuple[float, float, float, float]]:
    lats = [corners[c][0] for c in corners if corners[c] is not None]
    lons = [corners[c][1] for c in corners if corners[c] is not None]
    if not lats or not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))
