# Landsat 8 单通道地表温度反演系统

基于 Landsat 8 OLI/TIRS 影像的单通道算法（Single-Channel Algorithm）地表温度反演工具，提供完整图形化操作界面。

## 功能

- **元数据解析** — 自动识别 MTL.txt，提取辐射定标系数、太阳高度角、成像时间、四至坐标等参数
- **辐射定标** — Band 4/5: DN → TOA Reflectance；Band 10: DN → TOA Radiance
- **NDVI 计算** — 基于 Red (B4) 和 NIR (B5) TOA Reflectance 逐像元计算
- **地表比辐射率** — NDVI 三段式估算（裸土/混合像元/浓密植被）
- **单通道算法** — MODTRAN+TIGR 大气廓线拟合系数，3 个水汽区间，Planck 反函数求温
- **MODIS 水汽自动获取** — CMR API 搜索 → 下载 → 裁剪 → QA 过滤，自动填入水汽值
- **输出 GeoTIFF** — 保留原始投影和地理参考，可选 K / °C

## 安装

### 从发布包（普通用户）

1. 下载 `LST-Inversion-v1.x.x.zip`
2. 解压，双击 `install.bat`（首次约 2-5 分钟）
3. 双击 `run.bat` 启动

### 从源码（开发者）

```bash
conda create -n lst_env python=3.13
conda activate lst_env
conda install -c conda-forge gdal numpy scipy requests pyhdf h5py
python main.py
```

## 使用

1. 点击「浏览」选择 Landsat 8 影像目录（需含 `*_B4.TIF`, `*_B5.TIF`, `*_B10.TIF`, `*_MTL.txt`）
2. 选择输出目录和文件名
3. 输入水汽含量或点击「自动获取」（需 NASA Earthdata 账号）
4. 点击「开始反演」

## 项目结构

```
core/           # 算法模块
  metadata.py     MTL 元数据解析
  calibration.py  辐射定标
  emissivity.py   NDVI + 地表比辐射率
  lst_inversion.py 单通道算法反演
  water_vapor.py  MODIS 水汽自动获取
gui/            # GUI 模块
  main_window.py 主窗口
  metadata_dialog.py 元数据弹窗
  settings_dialog.py 算法设置面板
  cache_dialog.py 缓存管理
  worker.py     后台处理线程
utils/          # 工具模块
  file_utils.py  文件扫描 + 缓存管理
  constants.py   物理常数 + 算法系数
```

## 分支

| 分支 | 说明 |
|------|------|
| `master` | 完整版，含 MODIS 水汽自动获取 |
| `v1.0-base` | 基础版，手动输入水汽 |

## 依赖

- Python 3.13
- GDAL 3.12
- NumPy / SciPy
- pyhdf / h5py（MODIS HDF 读取）
- requests（MODIS 下载）
- Tkinter（GUI，Python 内置）
