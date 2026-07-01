"""
后台处理线程。
将完整的反演流水线置于独立线程中运行，通过队列向 GUI 主线程报告进度，
确保界面在处理期间保持响应。

中间产品存入 cache/ 目录下以时间戳命名的子文件夹，处理完成后保留。
"""

import datetime
import os
import queue
import threading
import traceback

import numpy as np
from osgeo import gdal


# ── 进度消息协议 ──────────────────────────────────────────────

def make_msg(msg_type: str, **kwargs) -> dict:
    """构造进度消息。"""
    return {'type': msg_type, **kwargs}


# ── 流水线步骤（权重用于进度条）────────────────────────────────

PIPELINE_STEPS = [
    ('解析元数据',          5),
    ('辐射定标',           25),
    ('计算 NDVI',         10),
    ('估算地表比辐射率',   15),
    ('反演地表温度',       30),
    ('输出结果',           15),
]


class InversionWorker(threading.Thread):
    """地表温度反演后台工作线程。

    通过 progress_queue 向主线程报告进度。
    """

    def __init__(
        self,
        input_dir: str,
        output_dir: str,
        water_vapor: float,
        progress_queue: queue.Queue,
        cancel_event: threading.Event,
        output_filename: str = 'LST_result.tif',
        output_unit: str = 'K',
    ):
        super().__init__(daemon=True)
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.water_vapor = water_vapor
        self.queue = progress_queue
        self.cancel = cancel_event
        self.output_filename = output_filename
        self.output_unit = output_unit

        self.cache_dir: str = ''

    def _check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError("用户取消")

    def _report(self, msg_type: str, **kwargs):
        self.queue.put(make_msg(msg_type, **kwargs))

    # ── 主流程 ───────────────────────────────────────────────

    def run(self):
        try:
            self._pipeline()
        except InterruptedError:
            self._report('done', success=False, message='已取消')
        except Exception as e:
            tb = traceback.format_exc()
            self._report('done', success=False,
                         message=f'{e}\n\n{tb}')
        finally:
            pass

    def _pipeline(self):
        from utils.file_utils import (
            scan_landsat_directory, validate_input_directory,
            create_run_cache_dir, get_cache_path,
        )
        from utils.constants import OUTPUT_FILENAME, KELVIN, CELSIUS
        from core.metadata import LandsatMetadata
        from core.calibration import (
            dn_to_toa_reflectance, dn_to_toa_radiance,
        )
        from core.emissivity import calc_ndvi, calc_emissivity
        from core.lst_inversion import invert_lst, get_coefficient_info

        steps = PIPELINE_STEPS
        log_lines: list[str] = []

        def advance(step_idx: int, message: str):
            completed_weight = 0.0
            for i in range(step_idx + 1):
                completed_weight += steps[i][1]
            pct = min(int(completed_weight), 100)
            self._report('progress', percent=pct,
                         step=steps[step_idx][0], message=message)

        def log(msg: str):
            log_lines.append(f'[{datetime.datetime.now():%H:%M:%S}] {msg}')
            self._report('log', message=msg)

        # ── 创建缓存目录 ──
        self.cache_dir = create_run_cache_dir()
        log(f'缓存目录: {self.cache_dir}')

        # ── 1. 扫描与验证 ──
        advance(0, '正在扫描影像目录...')
        self._check_cancel()

        scan = validate_input_directory(self.input_dir)
        if not scan['valid']:
            raise ValueError('\n'.join(scan['errors']))

        band_paths = scan['bands']
        mtl_path = scan['mtl']
        log(f'识别到 {len(band_paths)} 个波段文件')

        # ── 2. 解析元数据 ──
        advance(0, '正在解析 MTL 元数据...')
        self._check_cancel()

        meta = LandsatMetadata(mtl_path)
        issues = meta.validate()
        if issues:
            raise ValueError('元数据不完整:\n' + '\n'.join(issues))

        log(f'成像时间: {meta.acquisition_datetime}')
        log(f'太阳高度角: {meta.sun_elevation:.4f}°')

        os.makedirs(self.output_dir, exist_ok=True)

        # ── 3. 辐射定标 ──
        advance(1, '正在进行辐射定标 (Band 4 → TOA Reflectance)...')
        self._check_cancel()

        toa_b4 = get_cache_path(self.cache_dir, 'toa_ref_b4')
        dn_to_toa_reflectance(
            band_paths[4], toa_b4,
            m_rho=meta.reflectance_mult(4),
            a_rho=meta.reflectance_add(4),
            sun_elevation=meta.sun_elevation,
        )
        log('Band 4 辐射定标完成')

        advance(1, '正在进行辐射定标 (Band 5 → TOA Reflectance)...')
        toa_b5 = get_cache_path(self.cache_dir, 'toa_ref_b5')
        dn_to_toa_reflectance(
            band_paths[5], toa_b5,
            m_rho=meta.reflectance_mult(5),
            a_rho=meta.reflectance_add(5),
            sun_elevation=meta.sun_elevation,
        )
        log('Band 5 辐射定标完成')

        advance(1, '正在进行辐射定标 (Band 10 → TOA Radiance)...')
        toa_b10 = get_cache_path(self.cache_dir, 'toa_radiance')
        dn_to_toa_radiance(
            band_paths[10], toa_b10,
            m_l=meta.radiance_mult(10),
            a_l=meta.radiance_add(10),
        )
        advance(1, '辐射定标全部完成')
        log('辐射定标全部完成')

        # ── 4. NDVI ──
        advance(2, '正在计算 NDVI...')
        self._check_cancel()

        ndvi_path = get_cache_path(self.cache_dir, 'ndvi')
        calc_ndvi(toa_b4, toa_b5, ndvi_path)
        advance(2, 'NDVI 计算完成')
        log('NDVI 计算完成')

        # ── 5. 比辐射率 ──
        advance(3, '正在估算地表比辐射率...')
        self._check_cancel()

        emis_path = get_cache_path(self.cache_dir, 'emissivity')
        calc_emissivity(ndvi_path, toa_b4, emis_path, use_cavity=False)
        advance(3, '地表比辐射率估算完成')
        log('地表比辐射率估算完成')

        # ── 6. LST 反演 ──
        advance(4, f'正在反演地表温度 (w={self.water_vapor} g/cm²)...')
        self._check_cancel()

        coeff_info = get_coefficient_info(self.water_vapor)
        log(f'水汽区间: {coeff_info["interval"]}')

        lst_out = os.path.join(self.output_dir, self.output_filename)
        unit_label = 'K' if self.output_unit == 'K' else '°C'
        invert_lst(
            toa_b10, emis_path,
            water_vapor=self.water_vapor,
            output_path=lst_out,
            output_unit=self.output_unit,
        )
        advance(4, f'地表温度反演完成 ({unit_label})')
        log(f'输出文件: {os.path.basename(lst_out)}')

        # ── 7. 输出 ──
        advance(5, '正在写入最终结果...')
        log(f'输出目录: {self.output_dir}')

        # ── 写入处理日志 ──
        log_path = get_cache_path(self.cache_dir, 'inversion_log')
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(log_lines))
        log(f'处理日志已保存至缓存')

        # 完成
        self._report('done', success=True,
                     message=f'处理完成！\n输出: {lst_out}\n缓存: {self.cache_dir}')
