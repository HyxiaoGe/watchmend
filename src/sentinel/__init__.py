from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("watchmend")
except PackageNotFoundError:  # 未安装(源码树直跑)→ 退化占位,不抛
    __version__ = "0.0.0+dev"
