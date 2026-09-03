"""settings.json 编码容错：GBK/带 BOM/二进制垃圾都不许让任务硬失败。

背景（1060 实机）：文件被外部工具（中文系统记事本等）重存成 ANSI/GBK 后，
worker 的 settings.load() 在引擎加载路径裸抛 UnicodeDecodeError，任务以
「引擎加载失败 UnicodeDecodeError: 'utf-8' codec can't decode byte ...」
硬失败。读取侧必须自救：UTF-8(含 BOM) 优先，退 GBK 解码，都失败回默认。
save() 始终按 UTF-8 写，GBK 文件在下次保存设置时自然愈合回单一口径。
"""
import json

from sv.server import settings as sv_settings
from sv.server.settings import DEFAULTS, load


def _write_gbk(path, **overrides):
    data = dict(DEFAULTS) | overrides
    path.write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("gbk"))


def test_gbk_file_loads_with_values(monkeypatch, tmp_path):
    p = tmp_path / "settings.json"
    _write_gbk(p, output_dir=r"D:\图片输出\漫画")
    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", p)
    assert load()["output_dir"] == r"D:\图片输出\漫画"


def test_utf8_bom_loads(monkeypatch, tmp_path):
    # 记事本「UTF-8（带 BOM）」另存：BOM 不许让 json 解析失败静默回默认
    p = tmp_path / "settings.json"
    data = dict(DEFAULTS) | {"output_dir": r"D:\图片"}
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", p)
    assert load()["output_dir"] == r"D:\图片"


def test_binary_garbage_falls_back_to_defaults(monkeypatch, tmp_path):
    p = tmp_path / "settings.json"
    p.write_bytes(b"\xff\xfe\x00\x01 not json at all \x81\x40")
    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", p)
    assert load() == dict(DEFAULTS)


def test_save_heals_gbk_file_to_utf8(monkeypatch, tmp_path):
    # GBK 文件读出值后，下一次 save() 整体按 UTF-8 原子回写（自然愈合）
    p = tmp_path / "settings.json"
    _write_gbk(p, output_dir=r"D:\图片输出")
    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", p)
    assert load()["output_dir"] == r"D:\图片输出"
    saved = sv_settings.save({"output_dir": r"D:\图片输出\漫画"})
    assert saved["output_dir"] == r"D:\图片输出\漫画"
    assert p.read_bytes().decode("utf-8")  # 已是合法 UTF-8，不再需要 GBK 兜底
    assert load()["output_dir"] == r"D:\图片输出\漫画"
