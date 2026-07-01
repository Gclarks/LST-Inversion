"""
物理常数、传感器参数、算法系数、波段信息等常量定义。
"""

# ── 物理常数 ─────────────────────────────────────────────────

# 第一辐射常数（W·μm⁴·m⁻²·sr⁻¹）
C1 = 1.19104e8

# 第二辐射常数（μm·K）
C2 = 1.43877e4

# ── Landsat 8 Band 10 传感器参数 ─────────────────────────────

# Band 10 有效波长（μm）
LAMBDA_TIR = 10.904

# ── 波段信息 ─────────────────────────────────────────────────

# (波段号, 波段名, 中心波长 μm, 用途)
BAND_INFO = {
    1:  ('Coastal',   0.44),
    2:  ('Blue',      0.48),
    3:  ('Green',     0.56),
    4:  ('Red',       0.655),
    5:  ('NIR',       0.865),
    6:  ('SWIR1',     1.61),
    7:  ('SWIR2',     2.20),
    8:  ('Pan',       0.59),
    9:  ('Cirrus',    1.37),
    10: ('TIR1',      10.9),
    11: ('TIR2',      12.0),
}

# 反演必需的波段
REQUIRED_BANDS = (4, 5, 10)

# NDVI 计算用波段
BAND_RED = 4
BAND_NIR = 5
BAND_TIR = 10

# ── MTL 元数据关键字段 ───────────────────────────────────────

# 用于从 MTL.txt 提取定标系数和角度信息的 key
# Collection 1 和 Collection 2 的 key 名称可能略有不同，均在此列出

MTL_RADIANCE_MULT_KEYS = [
    'RADIANCE_MULT_BAND_10',
    'RADIANCE_MULT_BAND_%d',
]
MTL_RADIANCE_ADD_KEYS = [
    'RADIANCE_ADD_BAND_10',
    'RADIANCE_ADD_BAND_%d',
]
MTL_REFLECTANCE_MULT_KEYS = [
    'REFLECTANCE_MULT_BAND_%d',
    'REFLECTANCE_MULT_BAND_{}',
]
MTL_REFLECTANCE_ADD_KEYS = [
    'REFLECTANCE_ADD_BAND_%d',
    'REFLECTANCE_ADD_BAND_{}',
]
MTL_SUN_ELEVATION_KEYS = ['SUN_ELEVATION']
MTL_ACQUISITION_DATE_KEYS = ['DATE_ACQUIRED']
MTL_ACQUISITION_TIME_KEYS = ['SCENE_CENTER_TIME']
MTL_EARTH_SUN_DISTANCE_KEYS = ['EARTH_SUN_DISTANCE']

# ── 辐射定标 ─────────────────────────────────────────────────

# TOA Reflectance 有效范围
TOA_REFLECTANCE_MIN = 0.0
TOA_REFLECTANCE_MAX = 1.0

# ── NDVI 与地表比辐射率 ──────────────────────────────────────

# NDVI 阈值（Sobrino et al.）
NDVI_SOIL = 0.2   # 裸土 NDVI
NDVI_VEG = 0.5    # 浓密植被 NDVI

# 地表比辐射率参数（Landsat 8 Band 10）
EMISSIVITY_VEGETATION = 0.986
EMISSIVITY_SOIL = 0.967

# 裸土 / 水体比辐射率（NDVI < 0.2）
# ε = EMISSIVITY_BARE_A - EMISSIVITY_BARE_B * ρ_red
# 式中 ρ_red 为 Band 4 的 TOA Reflectance
EMISSIVITY_BARE_A = 0.973
EMISSIVITY_BARE_B = 0.047

# 浓密植被比辐射率（NDVI > 0.5）
EMISSIVITY_DENSE_VEG = 0.99

# 腔体效应项 Δε 的地表几何因子（通常忽略，取 0）
EMISSIVITY_GEOMETRIC_FACTOR = 0.55

# ── 水汽含量 ─────────────────────────────────────────────────

# 缺省水汽含量（g/cm²），用于用户未输入时的估算（中纬度夏季大气）
DEFAULT_WATER_VAPOR = 2.0

# 水汽含量有效范围（g/cm²）
WV_MIN = 0.0
WV_MAX = 7.0

# ── 单通道算法系数 a₀~a₇ ────────────────────────────────────

# 系数按水汽含量 w（g/cm²）分段；来源为 MODTRAN + TIGR 大气廓线拟合
# 各分段区间约定为左闭右开：[low, high)

SC_COEFFICIENTS = {
    # w ∈ [0.0, 2.0)
    (0.0, 2.0): {
        'a0': -0.28009,
        'a1':  1.257429,
        'a2':  0.275109,
        'a3': -1.32876,
        'a4': -0.1696,
        'a5':  0.999069,
        'a6':  0.033453,
        'a7':  0.015232,
    },
    # w ∈ [2.0, 4.0)
    (2.0, 4.0): {
        'a0': -0.60336,
        'a1':  1.613485,
        'a2': -4.98989,
        'a3':  2.772703,
        'a4': -1.04271,
        'a5':  1.739598,
        'a6': -0.54978,
        'a7':  0.129006,
    },
    # w ∈ [4.0, 7.0)
    (4.0, 7.0): {
        'a0':  2.280539,
        'a1':  0.918191,
        'a2': -38.3363,
        'a3':  13.82581,
        'a4': -1.75455,
        'a5':  5.003919,
        'a6': -1.62832,
        'a7':  0.196687,
    },
}

# Full range 回退系数组（w 超出分段范围时使用）
SC_COEFFICIENTS_FULL = {
    'a0': -0.4107,
    'a1':  1.493577,
    'a2':  0.278271,
    'a3': -1.22502,
    'a4': -0.31067,
    'a5':  1.022016,
    'a6': -0.01969,
    'a7':  0.036001,
}

# ── 输出设置 ─────────────────────────────────────────────────

# 输出 GeoTIFF 数据类型（GDAL GDT_Float32）
OUTPUT_DATATYPE = 'Float32'

# NoData 值
OUTPUT_NODATA = -9999.0

# 默认输出文件名
OUTPUT_FILENAME = 'LST_result'

# 温度单位
KELVIN = 'K'
CELSIUS = 'C'
