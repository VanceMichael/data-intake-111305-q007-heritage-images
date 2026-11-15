"""来源采集、原始响应及本地协议演练。"""
from pathlib import Path

__version__ = "0.3.0"


def fixture_path(name):
    """定位随包分发的合成资料文件，源码目录与安装后的 wheel 均可使用。"""
    if not name or Path(name).name != name:
        raise ValueError("fixture name must be a plain file name")
    packaged = Path(__file__).parent / "fixtures" / name
    if packaged.is_file():
        return packaged
    in_tree = Path(__file__).parents[1] / "fixtures" / name
    if in_tree.is_file():
        return in_tree
    raise FileNotFoundError("fixture not found: " + name)
