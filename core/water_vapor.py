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


# Earthdata token endpoint: GET retrieves existing, POST creates new
TOKEN_URL = 'https://urs.earthdata.nasa.gov/api/users/token'


def _create_earthdata_session(username: str, password: str) -> requests.Session:
    """创建带 NASA Earthdata 认证的 requests Session。

    Session 会为所有到 urs.earthdata.nasa.gov 的请求自动发送 Basic Auth，
    从而正确处理 OAuth 重定向链。
    """
    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({'User-Agent': 'LST-Inversion/1.0'})
    # 预热：先访问 URS 建立 cookies/session
    try:
        session.get('https://urs.earthdata.nasa.gov/', timeout=15)
    except requests.RequestException:
        pass
    return session


def _get_earthdata_token(username: str, password: str) -> str:
    """获取 NASA Earthdata Bearer Token。

    尝试多种认证方式：Basic Auth header → form data。
    """
    headers = {'User-Agent': 'LST-Inversion/1.0'}

    # 方式 1: GET with Basic Auth
    try:
        resp = requests.get(TOKEN_URL, auth=(username, password),
                            headers=headers, timeout=30)
        if resp.status_code == 200:
            token = resp.json().get('access_token', '')
            if token:
                return token
    except requests.RequestException:
        pass

    # 方式 2: POST with Basic Auth
    try:
        resp = requests.post(TOKEN_URL, auth=(username, password),
                             headers=headers, timeout=30)
        if resp.status_code == 200:
            token = resp.json().get('access_token', '')
            if token:
                return token
    except requests.RequestException:
        pass

    # 方式 3: POST with form data (no Basic Auth header)
    try:
        resp = requests.post(
            TOKEN_URL,
            data={'username': username, 'password': password,
                  'client_id': 'PIR2OBoAXa-jbm8w9WyxPQ',
                  'grant_type': 'password'},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            j = resp.json()
            token = j.get('access_token') or j.get('token') or ''
            if token:
                return token
    except requests.RequestException:
        pass

    # 全部失败，给出明确提示
    raise RuntimeError(
        '无法获取 Earthdata 访问令牌。\n\n'
        '请确认：\n'
        '1. 已在 https://urs.earthdata.nasa.gov/ 注册并激活账号\n'
        '2. 在浏览器中能正常登录上述网址\n'
        '3. 在"算法设置 → MODIS 数据源"中填入的用户名密码与网页登录一致\n\n'
        '如以上均无误，可能是 NASA 服务器临时故障，请稍后重试。'
    )


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
    session: requests.Session,
) -> str:
    filename = os.path.basename(url.split('?')[0])
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest

    last_error = None
    for attempt in range(3):
        try:
            resp = session.get(url, stream=True, timeout=(30, 120))
            resp.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            size = os.path.getsize(dest)
            if size < 1000:
                raise requests.RequestException(f'文件异常小 ({size} 字节)')
            return dest
        except requests.RequestException as e:
            last_error = e
            if os.path.exists(dest):
                os.unlink(dest)
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise requests.RequestException(f'下载失败 (已重试 3 次): {last_error}')


# ── HDF 提取（多格式）────────────────────────────────────────

def _extract_water_vapor(
    hdf_path: str,
    corners: dict[str, tuple[float, float]],
) -> Optional[float]:
    """从 MODIS HDF 提取研究区水汽均值，自动适配 HDF4/HDF5/GDAL。"""
    if not os.path.isfile(hdf_path):
        raise RuntimeError(f'HDF 文件不存在: {hdf_path}')
    file_size = os.path.getsize(hdf_path)
    if file_size < 1000:
        raise RuntimeError(f'HDF 文件损坏（大小仅 {file_size} 字节）')

    # 魔数检测：优先用匹配的 reader
    with open(hdf_path, 'rb') as fh:
        magic = fh.read(4)
    is_hdf4 = (magic[:4] == b'\x0e\x03\x13\x01')
    is_hdf5 = (magic[:4] == b'\x89HDF')
    is_gzip = (magic[:2] == b'\x1f\x8b')

    if is_gzip:
        import gzip
        decompressed = hdf_path[:-3] if hdf_path.endswith('.gz') else hdf_path + '.dec'
        with gzip.open(hdf_path, 'rb') as gz:
            with open(decompressed, 'wb') as out:
                out.write(gz.read())
        hdf_path = decompressed
        with open(hdf_path, 'rb') as fh:
            magic = fh.read(4)
        is_hdf4 = (magic[:4] == b'\x0e\x03\x13\x01')
        is_hdf5 = (magic[:4] == b'\x89HDF')

    errors = []
    readers = []
    if is_hdf4:
        readers = [_read_hdf4] + [_r for _r in (_read_hdf5, _read_gdal) if _r != _read_hdf4]
    elif is_hdf5:
        readers = [_read_hdf5] + [_r for _r in (_read_hdf4, _read_gdal) if _r != _read_hdf5]
    else:
        readers = [_read_hdf4, _read_hdf5, _read_gdal]
        errors.append(f'未知魔数: {magic[:4].hex()}')

    for reader in readers:
        for attempt in range(2):  # HDF4 有时瞬态失败，重试一次
            try:
                data = reader(hdf_path)
                if data is not None:
                    wv, lat, lon, qa = data
                    return _compute_mean_wv(wv, lat, lon, qa, corners)
                break  # reader 返回 None = 格式不适用
            except Exception as e:
                if attempt == 0 and reader is _read_hdf4:
                    time.sleep(0.5)  # 稍等后重试
                    continue
                errors.append(f'{reader.__name__}: {e}')
                break
        else:
            continue

    raise RuntimeError(
        f'无法读取 HDF 文件。\n'
        f'文件: {os.path.basename(hdf_path)} 魔数: {magic[:4].hex()}\n'
        f'大小: {file_size} 字节\n'
        + '\n'.join(errors)
    )


def _read_hdf4(hdf_path: str):
    """pyhdf — HDF4 格式。返回 (wv, lat, lon, qa) 或 raise。"""
    try:
        from pyhdf.SD import SD, SDC
    except ImportError:
        raise RuntimeError('pyhdf 未安装')

    # 复制到纯 ASCII 临时路径，规避 Windows 下可能的路径/锁问题
    import shutil
    tmp = os.path.join(tempfile.gettempdir(), '_lst_modis_read.hdf')
    shutil.copy2(hdf_path, tmp)
    try:
        hdf = SD(tmp, SDC.READ)
    except Exception as e:
        os.unlink(tmp)
        raise RuntimeError(f'pyhdf 无法打开: {e}')
    # 注意：hdf 句柄保持打开，调用者用完后需 hdf.end()；tmp 文件在 .end() 后可删
    # 我们返回时会先读完数据再 end，见下方
    return _read_pyhdf_datasets(hdf, tmp)


def _read_pyhdf_datasets(hdf, tmp_path: str):
    """从 pyhdf SD 对象读取数据层，清理临时文件，返回 (wv, lat, lon, qa)。"""
    try:
        names = list(hdf.datasets().keys())
        if 'Water_Vapor_Near_Infrared' not in names:
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
    finally:
        hdf.end()
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    if lat is None or lon is None:
        return None
    if np.nanmax(wv) > 100:
        wv *= 0.001
    return _align_resolution(wv, lat, lon, qa)

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
    """h5py — HDF5 格式（NASA Cloud）。返回 (wv, lat, lon, qa) 或 raise。"""
    try:
        import h5py
    except ImportError:
        raise RuntimeError('h5py 未安装')
    try:
        f = h5py.File(hdf_path, 'r')
    except Exception as e:
        raise RuntimeError(f'h5py 无法打开: {e}')

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
    """GDAL — 兜底方案。返回 (wv, lat, lon, qa) 或 raise。"""
    try:
        from osgeo import gdal
        gdal.UseExceptions()
    except ImportError:
        raise RuntimeError('GDAL 未安装')

    try:
        ds = gdal.Open(hdf_path)
    except Exception as e:
        raise RuntimeError(f'GDAL 无法打开: {e}')
    if ds is None:
        raise RuntimeError('GDAL 无法识别该文件格式')

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


# ── 水汽栅格重采样 ──────────────────────────────────────────

def extract_wv_arrays(
    hdf_path: str,
    corners: dict[str, tuple[float, float]],
) -> dict:
    """从 MODIS HDF 提取水汽、经纬度、QA 的原始数组。

    Returns:
        {'wv': np.ndarray, 'lat': np.ndarray, 'lon': np.ndarray, 'qa': np.ndarray|None}
    """
    for reader in (_read_hdf4, _read_hdf5, _read_gdal):
        try:
            data = reader(hdf_path)
            if data is not None:
                return {'wv': data[0], 'lat': data[1],
                        'lon': data[2], 'qa': data[3]}
        except Exception:
            continue
    raise RuntimeError('无法从 HDF 中提取水汽数据')


def resample_wv_to_landsat(
    wv: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qa,
    landsat_reference_path: str,
    output_path: str,
) -> Optional[str]:
    """将 MODIS 水汽数据重采样到 Landsat 像元网格。

    使用 KD-tree 最近邻查询。QA 不合格的 MODIS 像元不参与插值。

    Args:
        wv, lat, lon, qa: 来自 extract_wv_arrays() 的数组。
        landsat_reference_path: Landsat Band 10 GeoTIFF，用于确定目标网格。
        output_path: 输出水汽栅格 GeoTIFF。

    Returns:
        output_path，如果研究区无有效像元则返回 None。
    """
    from scipy.spatial import cKDTree
    from osgeo import gdal, osr

    # QA 过滤
    if qa is not None and qa.shape == wv.shape:
        valid = (wv > -9999) & (wv > 0) & np.isfinite(wv) & (qa >= 0) & (qa <= 1)
    else:
        valid = (wv > -9999) & (wv > 0) & np.isfinite(wv)

    if np.sum(valid) < 10:
        return None

    # 构建 KD-tree
    coords = np.column_stack([lat[valid].ravel(), lon[valid].ravel()])
    values = wv[valid].ravel()
    tree = cKDTree(coords)

    # Landsat 参考网格
    ref_ds = gdal.Open(landsat_reference_path, gdal.GA_ReadOnly)
    geo = ref_ds.GetGeoTransform()
    cols, rows = ref_ds.RasterXSize, ref_ds.RasterYSize
    proj = ref_ds.GetProjection()
    ref_ds = None

    # 构建 Landsat 像元中心坐标网格
    xs = geo[0] + geo[1] * (np.arange(cols) + 0.5)
    ys = geo[3] + geo[5] * (np.arange(rows) + 0.5)
    xx, yy = np.meshgrid(xs, ys)

    # 投影坐标 → 经纬度（pyproj 向量化，极快）
    from pyproj import Transformer
    transformer = Transformer.from_crs(proj, 'EPSG:4326', always_xy=True)
    lons, lats = transformer.transform(xx, yy)

    # KD-tree 查询最近邻（限制最大距离 0.05° ≈ 5km）
    query = np.column_stack([lats.ravel(), lons.ravel()])
    dist, idx = tree.query(query, distance_upper_bound=0.05)
    too_far = np.isinf(dist)
    idx[too_far] = 0  # 占位
    wv_grid = values[idx].reshape(rows, cols)
    if too_far.any():
        wv_grid = wv_grid.ravel()
        wv_grid[too_far] = np.nan
        wv_grid = wv_grid.reshape(rows, cols)

    # NaN 像元填全域有效均值，避免 ENVI/QGIS 拉伸异常
    valid = np.isfinite(wv_grid)
    if valid.any():
        fill_val = np.mean(wv_grid[valid])
        wv_grid[~valid] = fill_val
    else:
        wv_grid[~valid] = 0.0
    # 写入 GeoTIFF
    driver = gdal.GetDriverByName('GTiff')
    out_ds = driver.Create(output_path, cols, rows, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(geo)
    out_ds.SetProjection(proj)
    out_ds.GetRasterBand(1).WriteArray(wv_grid)
    out_ds = None
    return output_path


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
            '然后在"算法设置 → MODIS 数据源"中填写用户名和密码。'
        )

    # 创建带认证的 session（自动处理 OAuth 重定向）
    session = _create_earthdata_session(username, password)

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
                best['download_url'], temp_dir, session=session,
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
