"""可移植性回归：程序里不得出现"这台电脑"的特征信息。

设计原则（重要）
----------------
这份检查**自己不含任何本机信息**。它不维护一份"违禁词清单"——那种清单本身
就等于把机器特征抄进了仓库。它改为在**运行时从当前环境推导**出本机特征：

  * 你的用户目录、用户名（USERPROFILE / LOCALAPPDATA / APPDATA / TEMP …）；
  * 仓库自身所在路径及其有辨识度的目录名；
  * 真实扫描得到的 Steam 根目录、每个库路径与库目录名；
  * 库里**每一款**游戏的名字、安装目录名与 appid；
  * FastCopy 的实际安装目录。

然后断言源码与文档里不出现这些字符串。这样做有三个好处：
  1. 换台机器（换个人）跑同样有效，不需要改一行；
  2. 覆盖面更大——游戏名、库路径是扫描出来的，不是我手写的几个；
  3. 谁也无法从这份文件里反推出这台机器长什么样。

另有两条**纯结构**规则，同样不需要知道任何特征：
  * 程序源码里不得出现 ``X:\\`` 形式的盘符字面量（路径只能由配置/环境推导）；
  * 程序源码里不得出现 ``\\Users\\<名字>``（用户目录只能通过环境变量拿）。

运行： python tests/test_portability.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 日志重定向到测试沙箱（真实扫描会写日志，不能污染正式审计日志）
os.environ.setdefault("STEAMBOOST_LOG_DIR", str(Path(__file__).resolve().parent / "_portability_sandbox" / "logs"))

SKIP_DIRS = {"__pycache__", "dist", "build", "build_dbg", "build_debug", ".git", "node_modules"}
SKIP_FILES = {Path(__file__).resolve()}
SCAN_SUFFIXES = (".py", ".md", ".spec", ".txt")

#: 太通用的词不当特征用（否则会把正常代码判成违规）
STOPWORDS = {
    "steam", "steamapps", "common", "game", "games", "library", "libraries",
    "program", "programs", "programdata", "users", "windows", "system32",
    "documents", "desktop", "downloads", "projects", "project", "source", "src",
    "repos", "repo", "code", "dev", "work", "workspace", "temp", "tmp", "data",
    "test", "tests", "cache", "local", "roaming", "appdata", "tools", "public",
    # 本程序集成的第三方工具名：按设计就会出现在源码里，不算机器特征
    "fastcopy", "robocopy",
}
#: 程序自己的名字必然到处出现——直接从配置里取来加进"通用词"，免得手抄拼错
sys.path.insert(0, str(ROOT))
try:
    from config import APP_NAME  # noqa: E402

    STOPWORDS.add(APP_NAME.lower())
except Exception as exc:  # noqa: BLE001 - 取不到就少一条豁免，不影响正确性
    print(f"[INFO] 未能读取程序名，跳过该豁免（{exc}）")
#: 短于这个长度的词不当作特征（太容易误命中）
MIN_TOKEN_LEN = 6
#: 短于这个长度的 appid 不检查（容易和普通数字撞上）
MIN_APPID_LEN = 5
#: 盘符字面量：即 ``<字母>:\\`` 或 ``<字母>:/`` 这种把盘符写死的形式
DRIVE_LITERAL = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]):[\\/]")
#: 写死的用户目录：``\Users\<字母>``
USERS_LITERAL = re.compile(r"\\Users\\[A-Za-z]", re.IGNORECASE)


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def normalize(text: str) -> str:
    """把源码里的转义反斜杠还原，好让"按路径原文"写的特征词也能被搜到。"""
    return text.replace("\\\\", "\\")


def is_specific(token: str) -> bool:
    """这个词是否有足够辨识度，可以当作本机特征。"""
    text = (token or "").strip().strip("\\/")
    if len(text) < MIN_TOKEN_LEN:
        return False
    return text.lower() not in STOPWORDS


def derived_traits() -> list[tuple[str, str]]:
    """从当前环境推导本机特征：[(来源说明, 特征串), …]。

    全部是**运行时算出来的**，本文件里没有任何一个字是写死的机器信息。
    """
    traits: list[tuple[str, str]] = []

    def add(note: str, text: str) -> None:
        value = (text or "").strip()
        if len(value) < 4:
            return
        if value.lower() in STOPWORDS:
            return
        if all(value.lower() != existing.lower() for _n, existing in traits):
            traits.append((note, value))

    def add_path(note: str, path: str | os.PathLike[str] | None) -> None:
        """整条路径一定算特征；其中有辨识度的目录名也算（盘符、通用词不算）。"""
        if not path:
            return
        add(note, str(path))
        for part in Path(str(path)).parts:
            if is_specific(part):
                add(f"{note}（目录名）", part)

    # ---------------- 用户与仓库位置 ----------------
    add_path("仓库自身路径", ROOT)
    profile = os.environ.get("USERPROFILE")
    if profile:
        add_path("你的用户目录", profile)
        user = Path(profile).name
        if user:
            # 用户名本身太短、太容易撞词，只检查它作为路径片段出现的情况
            add("你的用户名", "\\Users\\" + user)
    for variable in ("LOCALAPPDATA", "APPDATA", "ProgramData", "TEMP", "TMP"):
        add_path(f"%{variable}%", os.environ.get(variable))

    # ---------------- Steam 与游戏（真实扫描得来） ----------------
    sys.path.insert(0, str(ROOT))
    try:
        from config import Config  # noqa: PLC0415
        from steam_scanner import find_steam_root, scan  # noqa: PLC0415

        steam_root = find_steam_root()
        if steam_root:
            add_path("你的 Steam 安装目录", steam_root)
            add_path("Steam 安装目录的上级", Path(steam_root).parent)

        report = scan(Config())
        for library in report.libraries:
            add_path("你的 Steam 库路径", library.path)
        for game in report.games:
            if is_specific(game.name):
                add("你库里的游戏名", game.name)
            if is_specific(game.installdir):
                add("你库里的安装目录名", game.installdir)
            if len(game.appid) >= MIN_APPID_LEN:
                add("你库里的 appid", game.appid)
    except Exception as exc:  # noqa: BLE001 - 推导失败不该让整个检查崩掉
        print(f"[INFO] 未能从 Steam 推导特征（{exc}）")

    # ---------------- FastCopy ----------------
    try:
        from copy_engine import find_fastcopy  # noqa: PLC0415

        executable = find_fastcopy()
        if executable:
            add_path("你的 FastCopy 安装目录", Path(executable).parent)
    except Exception as exc:  # noqa: BLE001
        print(f"[INFO] 未能推导 FastCopy 位置（{exc}）")

    return traits


def source_files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if path.resolve() in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return sorted(found)


def program_files() -> list[Path]:
    """真正打包进 exe 的程序源码（不含测试与工具脚本）。"""
    files = [path for path in ROOT.glob("*.py")]
    files.extend((ROOT / "ui").glob("*.py"))
    return sorted(path for path in files if path.is_file())


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  → {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    files = source_files()
    print(f"扫描 {len(files)} 个文件（{', '.join(SCAN_SUFFIXES)}）")

    texts: dict[Path, str] = {}
    for path in files:
        try:
            texts[path] = normalize(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    check("确实扫到了源码文件", len(texts) > 10, f"{len(texts)} 个")

    # ---------------- 1) 从环境推导出的本机特征 ----------------
    traits = derived_traits()
    print(f"从本机环境推导出 {len(traits)} 条特征（用户名、用户目录、Steam 根目录、库路径、"
          f"每款游戏的名字/安装目录/appid、FastCopy 目录…）")
    check("特征推导成功（否则本检查会变成空转）", len(traits) >= 5, f"只推导出 {len(traits)} 条")

    for note, token in traits:
        hits: list[str] = []
        for path, text in texts.items():
            if token.lower() not in text.lower():
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if token.lower() in line.lower():
                    hits.append(f"{rel(path)}:{number}")
                    break
        check(f"没有出现「{note}」", not hits, f"{token} → {', '.join(hits[:5])}")

    # ---------------- 2) 结构规则：程序源码里不许有盘符字面量 ----------------
    program = program_files()
    check("扫到了程序源码", len(program) >= 8, f"{len(program)} 个")
    drive_hits: list[str] = []
    users_hits: list[str] = []
    for path in program:
        text = texts.get(path) or normalize(path.read_text(encoding="utf-8", errors="replace"))
        for number, line in enumerate(text.splitlines(), start=1):
            if DRIVE_LITERAL.search(line):
                drive_hits.append(f"{rel(path)}:{number} {line.strip()[:60]}")
            if USERS_LITERAL.search(line):
                users_hits.append(f"{rel(path)}:{number} {line.strip()[:60]}")
    check("程序源码里没有写死的盘符路径", not drive_hits, " | ".join(drive_hits[:5]))
    check("程序源码里没有写死的用户目录", not users_hits, " | ".join(users_hits[:5]))

    # ---------------- 3) 路径都由配置/环境推导 ----------------
    from copy_engine import fastcopy_candidate_dirs, find_fastcopy, registry_fastcopy_dirs  # noqa: PLC0415

    dirs = fastcopy_candidate_dirs()
    check("FastCopy 候选目录全部是绝对路径", all(path.is_absolute() for path in dirs), str(dirs))
    check("FastCopy 候选目录数量合理", 3 <= len(dirs) <= 32, str(len(dirs)))
    check("注册表探测可用（返回列表）", isinstance(registry_fastcopy_dirs(), list))
    check(
        "显式路径无效时退回自动检测（不抛异常）",
        find_fastcopy(r"Q:\nowhere\fcp.exe") == find_fastcopy(),
    )
    program_files_dir = os.environ.get("ProgramFiles")
    check(
        "FastCopy 候选目录确实由系统环境变量推导",
        bool(program_files_dir)
        and any(str(path).lower().startswith(program_files_dir.lower()) for path in dirs),
        f"{program_files_dir} ∉ {[str(p) for p in dirs]}",
    )

    # ---------------- 4) 设置页确实提供了 FastCopy 路径入口 ----------------
    settings_src = (ROOT / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    for needle, label in (
        ("fastcopy_edit", "FastCopy 路径输入框"),
        ("_autodetect_fastcopy", "自动检测按钮"),
        ("_browse_fastcopy", "浏览按钮"),
        ("_refresh_fastcopy_hint", "引擎状态提示"),
    ):
        check(f"设置页有{label}", needle in settings_src)

    # ---------------- 5) 工作区里不许留构建残留 ----------------
    # PyInstaller 的 build 目录里有一堆 xref HTML，逐行写着构建机的绝对模块路径
    # （用户名、解释器安装位置都在里面），所以打包完必须清掉，也不能进版本库。
    build_dir = ROOT / "build"
    leftovers: list[str] = []
    if build_dir.is_dir():
        leftovers = [rel(path) for path in list(build_dir.rglob("*"))[:3]]
    check(
        "工作区里没有 PyInstaller 的 build 目录（里面有构建机绝对路径）",
        not leftovers,
        f"请删除 {build_dir}；打包时可用 --workpath 指到项目外",
    )

    # ---------------- 6) 图片也属于"会泄漏机器特征"的文件 ----------------
    # docs/ 里的截图是用 tools/make_demo_shot.py 渲染的**虚构数据**（X: / Y: 两个盘、
    # 12 款不存在的游戏）。真实扫描出来的截图会带上本机盘符、库路径与用户目录，
    # 而且照片里的文字没法像源码那样被上面的检查搜出来——所以只认这三张。
    # 测试沙箱里的截图（tests/_*sandbox、tests/_gui_shot.png）默认就不生成，也不该进库。
    expected_shots = {
        "screenshot-main.png",
        "screenshot-settings.png",
        "screenshot-writeback-confirm.png",
    }
    docs_dir = ROOT / "docs"
    shots = {path.name for path in docs_dir.glob("*.png")} if docs_dir.is_dir() else set()
    unexpected = sorted(shots - expected_shots)
    check(
        "docs/ 里只有用虚构数据渲染的展示图",
        not unexpected,
        f"多出来的图片：{unexpected}；请用 python tools\\make_demo_shot.py 重新生成，"
        "不要提交真实扫描出来的截图",
    )
    check("展示图确实存在（README 里引用了它们）", expected_shots <= shots, f"缺少 {sorted(expected_shots - shots)}")
    stray = [rel(path) for path in (ROOT / "tests").glob("*.png")]
    check("tests/ 下没有遗留截图", not stray, f"{stray}（截图默认不落盘，需要时用 STEAMBOOST_GUI_SHOTS=1）")

    # 收尾：测试自己的日志沙箱不留痕迹
    from logger import close_loggers  # noqa: PLC0415

    close_loggers()
    sandbox = Path(os.environ["STEAMBOOST_LOG_DIR"]).parent
    shutil.rmtree(sandbox, ignore_errors=True)
    check("测试日志沙箱已清理", not sandbox.exists())

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
