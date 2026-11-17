"""表情包资源检查：插件加载完成后在后台运行，把问题以插件名义打印到 AstrBot 控制台。

只读：不下载、不修改任何文件。meme-generator 自带的 check_resources 会重新下载并覆盖
与官方 MD5 不同的素材，会冲掉用户自己改过的表情，所以这里只核对、只报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sys
from typing import Any, Iterable

MODULE_PREFIX = "meme_generator.memes."
NAME_LIMIT = 20


@dataclass(slots=True)
class HealthReport:
    engine: str = ""
    version: str = ""
    loaded: int = 0
    failed_dirs: list[str] = field(default_factory=list)  # 导入失败
    empty_dirs: list[str] = field(default_factory=list)  # 导入成功但没有注册表情（多为重名）
    missing: list[str] = field(default_factory=list)  # 已安装表情缺少的官方素材
    modified: list[str] = field(default_factory=list)  # 与官方 MD5 不同的素材
    official_checked: int = 0
    official_error: str = ""
    font: str | None = None  # 能显示中文的字体；None 表示没找到
    font_checked: bool = False
    keyword_conflicts: list[tuple[str, list[str]]] = field(default_factory=list)  # 关键词 -> 使用它的表情（第一个生效）
    unreachable: list[str] = field(default_factory=list)  # 关键词全被别的表情占用的表情
    command_clashes: list[str] = field(default_factory=list)  # 与本插件指令同名的关键词

    @property
    def problems(self) -> int:
        return sum(bool(x) for x in (
            self.failed_dirs, self.empty_dirs, self.missing, self.font_checked and not self.font,
            self.keyword_conflicts, self.command_clashes,
        ))


def keyword_conflicts(entries: Iterable[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """entries 是按加载顺序排列的 (表情名, 关键词)。关键词不区分大小写，同一个关键词只有最先加载的表情生效。"""
    owners: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for key, keywords in entries:
        for keyword in keywords:
            folded = keyword.casefold()
            shown.setdefault(folded, keyword)
            users = owners.setdefault(folded, [])
            if key not in users:
                users.append(key)
    return [(shown[k], users) for k, users in owners.items() if len(users) > 1]


def unreachable_memes(entries: Iterable[tuple[str, list[str]]]) -> list[str]:
    """所有关键词都被先加载的其他表情占用、没法用关键词触发的表情。"""
    winner: dict[str, str] = {}
    entries = list(entries)
    for key, keywords in entries:
        for keyword in keywords:
            winner.setdefault(keyword.casefold(), key)
    return [key for key, keywords in entries if keywords and all(winner[k.casefold()] != key for k in keywords)]


def command_clashes(keywords: Iterable[str], command_names: Iterable[str]) -> list[str]:
    commands = {name.casefold() for name in command_names}
    return sorted({k for k in keywords if k.casefold() in commands})


def meme_dirs(memes_dir: Path) -> list[Path]:
    """memes 目录下会被当成表情加载的条目（与 meme-generator 的加载方式一致）。"""
    return [
        p for p in sorted(memes_dir.iterdir())
        if not p.name.startswith(("_", ".")) and (p.is_dir() or p.suffix == ".py")
    ]


def module_name(path: Path) -> str:
    return MODULE_PREFIX + (path.stem if path.is_file() else path.name)


def owner_dir(meme: Any) -> str | None:
    """表情所在的 memes 子目录名；不是内置目录加载的返回 None。"""
    module = str(getattr(getattr(meme, "function", None), "__module__", "") or "")
    if not module.startswith(MODULE_PREFIX):
        return None
    return module[len(MODULE_PREFIX):].split(".", 1)[0]


def scan_dirs(memes_dir: Path, memes: Iterable[Any], modules: Any = None) -> tuple[list[str], list[str]]:
    """返回 (导入失败的目录, 导入成功却没有注册任何表情的目录)。"""
    modules = sys.modules if modules is None else modules
    owners = {owner for owner in map(owner_dir, memes) if owner}
    failed, empty = [], []
    for path in meme_dirs(memes_dir):
        name = path.stem if path.is_file() else path.name
        if module_name(path) not in modules:
            failed.append(name)
        elif name not in owners:
            empty.append(name)
    return failed, empty


def compare_official(memes_dir: Path, resource_list: list[dict]) -> tuple[list[str], list[str], int]:
    """按官方清单核对素材，只看已安装的表情（整个目录不存在的视为用户没装）。

    返回 (缺失, 与官方不同, 核对的文件数)。"""
    missing, modified, checked = [], [], 0
    for item in resource_list:
        rel = str(item.get("path") or "")
        expected = str(item.get("hash") or "")
        parts = Path(rel).parts
        if not rel or not parts or ".." in parts or Path(rel).is_absolute():
            continue
        if not (memes_dir / parts[0]).exists():
            continue
        checked += 1
        path = memes_dir / rel
        if not path.is_file():
            missing.append(rel)
        elif expected and hashlib.md5(path.read_bytes()).hexdigest() != expected:
            modified.append(rel)
    return missing, modified, checked


def find_cjk_font() -> tuple[bool, str | None]:
    """Python 版用 skia（pil-utils）排版文字。返回 (是否检查了, 能显示「中」的字体名)。"""
    try:
        import skia  # type: ignore
    except Exception:
        return False, None
    try:
        typeface = skia.FontMgr().matchFamilyStyleCharacter("", skia.FontStyle(), ["zh"], ord("中"))
    except Exception:
        return False, None
    return True, (typeface.getFamilyName() if typeface is not None else None)


def names(items: list[str], limit: int = NAME_LIMIT) -> str:
    shown = "、".join(items[:limit])
    return shown + (f" 等 {len(items)} 个" if len(items) > limit else "")


def top_dirs(paths: list[str]) -> list[str]:
    return sorted({Path(p).parts[0] for p in paths})


def log_report(report: HealthReport, log: Any) -> None:
    """严重问题用 error，控制台里一眼能看到；用户自己改过的素材只是提示。"""
    tag = "[多模态处理器] 表情包资源检查："
    if report.failed_dirs:
        log.error(f"{tag}{len(report.failed_dirs)} 个表情目录导入失败，这些表情不可用：{names(report.failed_dirs)}。原因见上方 meme-generator 的报错。")
    if report.empty_dirs:
        log.error(
            f"{tag}{len(report.empty_dirs)} 个表情目录没有注册任何表情（通常是与其他表情重名，"
            f"或在 meme-generator 的禁用列表里），这些表情不可用：{names(report.empty_dirs)}"
        )
    if report.missing:
        dirs = top_dirs(report.missing)
        log.error(
            f"{tag}缺少 {len(report.missing)} 个官方素材文件，涉及 {len(dirs)} 个表情：{names(dirs)}，使用时会失败。"
            f"缺少的文件例如：{names(report.missing, 5)}"
        )
    if report.modified:
        dirs = top_dirs(report.modified)
        log.info(f"{tag}{len(report.modified)} 个素材文件与官方版本不同（自己替换过的可以忽略，插件不会覆盖）：{names(dirs)}")
    if report.official_error:
        log.warning(f"{tag}无法获取官方素材清单，跳过素材核对：{report.official_error}")
    if report.keyword_conflicts:
        sample = "；".join(f"「{kw}」{users[0]} 生效，{'、'.join(users[1:])} 不会被触发" for kw, users in report.keyword_conflicts[:10])
        more = f"……共 {len(report.keyword_conflicts)} 个" if len(report.keyword_conflicts) > 10 else ""
        log.error(f"{tag}{len(report.keyword_conflicts)} 个关键词被多个表情同时使用，只有最先加载的生效：{sample}{more}")
    if report.unreachable:
        log.error(f"{tag}{len(report.unreachable)} 个表情的关键词全被其他表情占用，无法用关键词触发：{names(report.unreachable)}")
    if report.command_clashes:
        log.warning(f"{tag}这些关键词与本插件的指令同名，带唤醒前缀发送时会执行指令而不是生成表情：{names(report.command_clashes)}")
    if report.font_checked and not report.font:
        log.error(f"{tag}没有找到能显示中文的字体，表情里的中文会显示成方块。请安装 Noto Sans CJK 等中文字体后重启 AstrBot。")
    engine = "Python" if report.engine == "python" else "Rust"
    summary = f"{tag}已加载 {report.loaded} 个表情（{engine} 版 {report.version}）"
    if report.official_checked:
        summary += f"，核对官方素材 {report.official_checked} 个"
    if report.font:
        summary += f"，中文字体 {report.font}"
    if report.problems:
        log.warning(f"{summary}；发现 {report.problems} 类问题，见上方日志。")
    else:
        log.info(f"{summary}，没有发现问题。")
