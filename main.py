#!/usr/bin/env python
"""
Landsat 8 单通道算法地表温度反演系统。
启动入口
"""

import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.main_window import MainWindow

def main():
    app = MainWindow()
    app.mainloop()


if __name__ == '__main__':
    main()
