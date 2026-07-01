"""
Landsat 8 MTL.txt 元数据解析器。
解析 MTL 文件的 GROUP/END_GROUP 结构，提取辐射定标系数、太阳角度、
成像时间等关键参数，并提供格式化展示接口供 GUI 元数据弹窗使用。
"""

import os
import re
from typing import Any, Optional


# ── MTL 文件解析 ──────────────────────────────────────────────

# 行模式
_RE_GROUP = re.compile(r'^\s*GROUP\s*=\s*(.+)$', re.IGNORECASE)
_RE_END_GROUP = re.compile(r'^\s*END_GROUP\s*=\s*(.+)$', re.IGNORECASE)
_RE_KV = re.compile(r'^\s*([A-Za-z0-9_]+)\s*=\s*(.+)$')


def _parse_value(raw: str) -> Any:
    """解析 MTL 值：尝试 float/int，失败则返回去引号的字符串。"""
    val = raw.strip()
    # 去除可能存在的首尾引号
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1]
    try:
        if '.' in val or 'E' in val.upper():
            return float(val)
        return int(val)
    except ValueError:
        return val


def parse_mtl(mtl_path: str) -> dict[str, Any]:
    """解析 MTL.txt 文件，返回扁平化的键值字典。

    所有键值以扁平形式存储（如 'RADIANCE_MULT_BAND_10' → 1.234e-02），
    值类型自动推断（float / int / str）。
    """
    if not os.path.isfile(mtl_path):
        raise FileNotFoundError(f"MTL 文件不存在: {mtl_path}")

    data: dict[str, Any] = {}
    group_stack: list[str] = []

    with open(mtl_path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            # 跳过纯空白行
            stripped = line.strip()
            if not stripped:
                continue

            # GROUP 开始
            m = _RE_GROUP.match(line)
            if m:
                group_stack.append(m.group(1).strip())
                continue

            # GROUP 结束
            m = _RE_END_GROUP.match(line)
            if m:
                if group_stack:
                    expected = group_stack[-1]
                    actual = m.group(1).strip()
                    if actual != expected:
                        # 宽松处理：只弹出栈顶，不严格匹配
                        pass
                    group_stack.pop()
                continue

            # 键值对
            m = _RE_KV.match(line)
            if m:
                key = m.group(1).strip()
                value = _parse_value(m.group(2))
                # 扁平存储，同名键后者覆盖（实际 MTL 中不会重复）
                data[key] = value

    return data


# ── MTL 元数据对象 ────────────────────────────────────────────

class LandsatMetadata:
    """Landsat 8 元数据封装，提供类型安全的参数访问。"""

    def __init__(self, mtl_path: str):
        self._mtl_path = mtl_path
        self._raw = parse_mtl(mtl_path)

    # ── 通用查找 ──────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    def _band_key(self, pattern: str, band: int) -> Any:
        """尝试多种 key 格式查找波段参数。"""
        candidates = [pattern % band, pattern.format(band)]
        for key in candidates:
            if key in self._raw:
                return self._raw[key]
        return None

    # ── 辐射定标系数 ──────────────────────────────────────────

    def radiance_mult(self, band: int = 10) -> Optional[float]:
        """热红外波段辐射亮度缩放因子 M_L。"""
        v = self._band_key('RADIANCE_MULT_BAND_%d', band)
        return float(v) if v is not None else None

    def radiance_add(self, band: int = 10) -> Optional[float]:
        """热红外波段辐射亮度偏移量 A_L。"""
        v = self._band_key('RADIANCE_ADD_BAND_%d', band)
        return float(v) if v is not None else None

    def reflectance_mult(self, band: int) -> Optional[float]:
        """多光谱波段表观反射率缩放因子 M_ρ。"""
        v = self._band_key('REFLECTANCE_MULT_BAND_%d', band)
        return float(v) if v is not None else None

    def reflectance_add(self, band: int) -> Optional[float]:
        """多光谱波段表观反射率偏移量 A_ρ。"""
        v = self._band_key('REFLECTANCE_ADD_BAND_%d', band)
        return float(v) if v is not None else None

    # ── 太阳与成像几何 ────────────────────────────────────────

    @property
    def sun_elevation(self) -> Optional[float]:
        """太阳高度角（度）。"""
        v = self.get('SUN_ELEVATION')
        return float(v) if v is not None else None

    @property
    def sun_azimuth(self) -> Optional[float]:
        """太阳方位角（度）。"""
        v = self.get('SUN_AZIMUTH')
        return float(v) if v is not None else None

    @property
    def earth_sun_distance(self) -> Optional[float]:
        """日地距离（AU）。"""
        v = self.get('EARTH_SUN_DISTANCE')
        return float(v) if v is not None else None

    # ── 时间信息 ──────────────────────────────────────────────

    @property
    def date_acquired(self) -> Optional[str]:
        """成像日期，格式 YYYY-MM-DD。"""
        return self.get('DATE_ACQUIRED')

    @property
    def scene_center_time(self) -> Optional[str]:
        """成像中心时间，格式 HH:MM:SS。"""
        return self.get('SCENE_CENTER_TIME')

    @property
    def acquisition_datetime(self) -> Optional[str]:
        """成像日期 + 时间组合字符串。"""
        d = self.date_acquired
        t = self.scene_center_time
        if d and t:
            return f"{d} {t}"
        return d or t or None

    # ── 影像基础信息 ──────────────────────────────────────────

    @property
    def spacecraft_id(self) -> Optional[str]:
        return self.get('SPACECRAFT_ID') or self.get('SPACECRAFT_NAME')

    @property
    def sensor_id(self) -> Optional[str]:
        return self.get('SENSOR_ID')

    @property
    def landsat_product_id(self) -> Optional[str]:
        return (self.get('LANDSAT_PRODUCT_ID')
                or self.get('LANDSAT_SCENE_ID'))

    @property
    def wrs_path(self) -> Optional[int]:
        v = self.get('WRS_PATH')
        return int(v) if v is not None else None

    @property
    def wrs_row(self) -> Optional[int]:
        v = self.get('WRS_ROW')
        return int(v) if v is not None else None

    @property
    def cloud_cover(self) -> Optional[float]:
        """云量百分比（0~100）。"""
        v = self.get('CLOUD_COVER')
        return float(v) if v is not None else None

    @property
    def cloud_cover_land(self) -> Optional[float]:
        v = self.get('CLOUD_COVER_LAND')
        return float(v) if v is not None else None

    # ── 影像四至 ──────────────────────────────────────────────

    @property
    def corner_ul(self) -> Optional[tuple[float, float]]:
        """左上角 (lat, lon)。"""
        lat = self.get('CORNER_UL_LAT_PRODUCT')
        lon = self.get('CORNER_UL_LON_PRODUCT')
        if lat is not None and lon is not None:
            return (float(lat), float(lon))
        return None

    @property
    def corner_ur(self) -> Optional[tuple[float, float]]:
        lat = self.get('CORNER_UR_LAT_PRODUCT')
        lon = self.get('CORNER_UR_LON_PRODUCT')
        if lat is not None and lon is not None:
            return (float(lat), float(lon))
        return None

    @property
    def corner_ll(self) -> Optional[tuple[float, float]]:
        lat = self.get('CORNER_LL_LAT_PRODUCT')
        lon = self.get('CORNER_LL_LON_PRODUCT')
        if lat is not None and lon is not None:
            return (float(lat), float(lon))
        return None

    @property
    def corner_lr(self) -> Optional[tuple[float, float]]:
        lat = self.get('CORNER_LR_LAT_PRODUCT')
        lon = self.get('CORNER_LR_LON_PRODUCT')
        if lat is not None and lon is not None:
            return (float(lat), float(lon))
        return None

    # ── 投影信息 ──────────────────────────────────────────────

    @property
    def utm_zone(self) -> Optional[int]:
        v = self.get('UTM_ZONE')
        return int(v) if v is not None else None

    @property
    def datum(self) -> Optional[str]:
        return self.get('DATUM')

    @property
    def ellipsoid(self) -> Optional[str]:
        return self.get('ELLIPSOID')

    # ── GUI 展示 ──────────────────────────────────────────────

    def to_display_dict(self) -> dict[str, Any]:
        """返回按类别组织、适合元数据弹窗展示的字典。"""
        return {
            '影像标识': {
                '产品 ID':   self.landsat_product_id or '—',
                '卫星':      self.spacecraft_id or '—',
                '传感器':    self.sensor_id or '—',
                'Path/Row':  f"{self.wrs_path or '—'} / {self.wrs_row or '—'}",
                '成像日期':  self.date_acquired or '—',
                '成像时间':  self.scene_center_time or '—',
                '云量 (%)':  f"{self.cloud_cover:.2f}" if self.cloud_cover is not None else '—',
            },
            '太阳与几何': {
                '太阳高度角 (°)': f"{self.sun_elevation:.4f}" if self.sun_elevation is not None else '—',
                '太阳方位角 (°)': f"{self.sun_azimuth:.4f}" if self.sun_azimuth is not None else '—',
                '日地距离 (AU)':  f"{self.earth_sun_distance:.4f}" if self.earth_sun_distance is not None else '—',
                'UTM 投影带':     self.utm_zone or '—',
                '基准面':         self.datum or '—',
            },
            '辐射定标系数 (热红外 Band 10)': {
                'RADIANCE_MULT_BAND_10': f"{self.radiance_mult(10):.6e}" if self.radiance_mult(10) is not None else '—',
                'RADIANCE_ADD_BAND_10':  f"{self.radiance_add(10):.6f}" if self.radiance_add(10) is not None else '—',
            },
            '辐射定标系数 (Red Band 4)': {
                'REFLECTANCE_MULT_BAND_4': f"{self.reflectance_mult(4):.6e}" if self.reflectance_mult(4) is not None else '—',
                'REFLECTANCE_ADD_BAND_4':  f"{self.reflectance_add(4):.6f}" if self.reflectance_add(4) is not None else '—',
            },
            '辐射定标系数 (NIR Band 5)': {
                'REFLECTANCE_MULT_BAND_5': f"{self.reflectance_mult(5):.6e}" if self.reflectance_mult(5) is not None else '—',
                'REFLECTANCE_ADD_BAND_5':  f"{self.reflectance_add(5):.6f}" if self.reflectance_add(5) is not None else '—',
            },
            '影像覆盖范围': {
                '左上 (UL)': self._format_corner(self.corner_ul),
                '右上 (UR)': self._format_corner(self.corner_ur),
                '左下 (LL)': self._format_corner(self.corner_ll),
                '右下 (LR)': self._format_corner(self.corner_lr),
            },
        }

    @staticmethod
    def _format_corner(corner) -> str:
        if corner is None:
            return '—'
        return f"({corner[0]:.6f}, {corner[1]:.6f})"

    # ── 验证 ──────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """检查元数据完整性，返回缺失项列表。"""
        issues: list[str] = []
        for b in (4, 5):
            if self.reflectance_mult(b) is None:
                issues.append(f"缺少 REFLECTANCE_MULT_BAND_{b}")
            if self.reflectance_add(b) is None:
                issues.append(f"缺少 REFLECTANCE_ADD_BAND_{b}")
        for b in (10,):
            if self.radiance_mult(b) is None:
                issues.append(f"缺少 RADIANCE_MULT_BAND_{b}")
            if self.radiance_add(b) is None:
                issues.append(f"缺少 RADIANCE_ADD_BAND_{b}")
        if self.sun_elevation is None:
            issues.append("缺少 SUN_ELEVATION")
        return issues

    def is_valid(self) -> bool:
        return len(self.validate()) == 0
