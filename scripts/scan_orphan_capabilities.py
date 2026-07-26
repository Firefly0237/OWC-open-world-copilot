# 零调用能力扫描：枚举 src 内“能力级”公开符号，判定其是否被产品路径可达地引用，
# 产出“零生产调用”清单。治的是本项目反复出现的“能力实现完整、却没接到任何产品
# 入口”的模式（治本闸，先做软门）。
#
# 产品路径（务实定义）：CLI(cli/) / REST(service/) / MCP(mcp_server/) / app 动作层
#   (app/actions/) 四个入口，以及从这四者经 import 传递可达的所有 src 模块，合称
#   “live surface（生产可达面）”。一个符号只要在 live surface 里被真正“引用”
#   （Call/Attribute/Name-load，不含纯 import 再导出），即算被生产调用。
#   仅被 tests/ 或仅被 live surface 之外的 src 引用 = 零生产调用。
#
# 设计取舍（详见文末 report 的“薄弱点”）：
#   - 引用采集刻意排除 import 语句本身：`from m import f` 只有在后面真的 `f(...)` 时才算用到，
#     这样 __init__.py 里的“纯再导出”不会把孤儿符号假装成已用。
#   - 函数名用 Name-load ∪ Attribute-attr 的并集匹配（over-approximate 引用 → 保守 → 少误报）。
#   - 方法/类噪声更大（同名方法 run/load/save 撞名），单独分档并加警示。
"""静态扫描：找出 src 内“实现了但没接到产品路径”的公开能力符号。"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# ── 常量：入口目录与候选排除 ────────────────────────────────────────────────
_SRC_ROOT_PARTS = ("src", "owcopilot")

# 产品路径入口（相对 owcopilot 包的目录前缀）。BFS 的种子，同时排除出“候选”之外
# （入口函数由框架 argparse / FastAPI 装饰器 / MCP 注册调用，天然不会被别处按名调用）。
_ENTRY_DIRS: tuple[tuple[str, ...], ...] = (
    ("cli",),
    ("service",),
    ("mcp_server",),
    ("app", "actions"),
)

# 不作为“能力候选”的模块（测试替身 / 明确的非产品脚本面）。fakes 是测试替身，其符号供 tests 用。
_CANDIDATE_SKIP_BASENAMES = {"fakes.py"}

_DUNDER_OK: frozenset[str] = frozenset()  # 方法档里不纳入任何 __dunder__


@dataclass(frozen=True)
class Capability:
    """一个能力级公开符号。"""

    module: str  # owcopilot.content.snapshot
    qualname: str  # merge_versions  /  ContentStore.save_scoped
    name: str  # 用于引用匹配的裸名：merge_versions / save_scoped / ContentStore
    kind: str  # "function" | "class" | "method"
    lineno: int
    in_dunder_all: bool  # 是否在“定义模块自身”的 __all__ 里（声明为库 API）


@dataclass
class ModuleInfo:
    dotted: str
    path: Path
    tree: ast.Module
    is_init: bool
    is_entry: bool  # 位于四个产品入口目录之一
    is_test_like: bool  # src 内一般没有；预留
    dunder_all: set[str] = field(default_factory=set)
    import_targets: set[str] = field(default_factory=set)  # 指向的 src 模块 dotted 名
    referenced_names: set[str] = field(default_factory=set)  # 本模块内引用到的裸名（不含 import）


# ── 模块发现与 dotted 名映射 ────────────────────────────────────────────────
def _find_src_root(repo_root: Path) -> Path:
    root = repo_root.joinpath(*_SRC_ROOT_PARTS)
    if not root.is_dir():
        raise SystemExit(f"找不到 src 包根：{root}")
    return root


def _dotted_name(src_root: Path, path: Path) -> str:
    """src/owcopilot/content/store.py -> owcopilot.content.store；__init__.py -> 其包名。"""
    rel = path.relative_to(src_root.parent)  # 相对 src/ → owcopilot/...
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _rel_parts_under_pkg(src_root: Path, path: Path) -> tuple[str, ...]:
    """返回相对 owcopilot 包目录的路径分量，用于判定入口目录。"""
    return path.relative_to(src_root).parts


def _is_entry(rel_parts: tuple[str, ...]) -> bool:
    return any(rel_parts[: len(prefix)] == prefix for prefix in _ENTRY_DIRS)


# ── AST 提取：__all__ / 导入目标 / 引用名 ──────────────────────────────────
def _extract_dunder_all(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == "__all__"]
        if not targets:
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.add(elt.value)
    return out


def _resolve_relative(pkg: str, level: int, module: str | None) -> str:
    """把 relative import 解析成绝对 dotted base。pkg=当前模块所在包。"""
    base = pkg
    for _ in range(level - 1):
        base = base.rsplit(".", 1)[0] if "." in base else base
    if module:
        base = f"{base}.{module}" if base else module
    return base


def _package_of(info_dotted: str, is_init: bool) -> str:
    """relative import 的锚点包：__init__ 是包自身，普通模块是其父包。"""
    if is_init:
        return info_dotted
    return info_dotted.rsplit(".", 1)[0] if "." in info_dotted else info_dotted


def _collect_import_targets(tree: ast.Module, pkg: str, known_modules: set[str]) -> set[str]:
    """收集本模块 import 指向的、属于 src 的模块 dotted 名（用于构建 import 可达图）。"""
    targets: set[str] = set()

    def _add_base_and_children(base: str, names: list[str]) -> None:
        if base in known_modules:
            targets.add(base)
        # `from base import x` 里 x 可能是子模块，也可能是符号；子模块要连边
        for n in names:
            child = f"{base}.{n}"
            if child in known_modules:
                targets.add(child)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name in known_modules:
                    targets.add(name)
                # import owcopilot.a.b —— 也把可解析到的最长已知前缀连上
                parts = name.split(".")
                for i in range(len(parts), 0, -1):
                    cand = ".".join(parts[:i])
                    if cand in known_modules:
                        targets.add(cand)
                        break
        elif isinstance(node, ast.ImportFrom):
            imported = [a.name for a in node.names]
            if node.level and node.level > 0:
                base = _resolve_relative(pkg, node.level, node.module)
            else:
                base = node.module or ""
            if base:
                _add_base_and_children(base, imported)
    return targets


class _RefCollector(ast.NodeVisitor):
    """采集“真正引用”的裸名：Name(Load) 与 Attribute.attr，刻意跳过 import 语句。"""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 —— 不下钻，import 不算引用
        return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        return

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self.names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        self.names.add(node.attr)
        self.generic_visit(node)


def _collect_referenced_names(tree: ast.Module) -> set[str]:
    c = _RefCollector()
    c.visit(tree)
    return c.names


# ── 能力枚举 ────────────────────────────────────────────────────────────────
def _public(name: str) -> bool:
    return not name.startswith("_")


def _enumerate_capabilities(info: ModuleInfo) -> list[Capability]:
    """模块级公开函数、公开类、公开类的公开方法。"""
    caps: list[Capability] = []
    for node in info.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            caps.append(
                Capability(
                    module=info.dotted,
                    qualname=node.name,
                    name=node.name,
                    kind="function",
                    lineno=node.lineno,
                    in_dunder_all=node.name in info.dunder_all,
                )
            )
        elif isinstance(node, ast.ClassDef) and _public(node.name):
            caps.append(
                Capability(
                    module=info.dotted,
                    qualname=node.name,
                    name=node.name,
                    kind="class",
                    lineno=node.lineno,
                    in_dunder_all=node.name in info.dunder_all,
                )
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(item.name):
                    if item.name.startswith("__") and item.name not in _DUNDER_OK:
                        continue
                    caps.append(
                        Capability(
                            module=info.dotted,
                            qualname=f"{node.name}.{item.name}",
                            name=item.name,
                            kind="method",
                            lineno=item.lineno,
                            # 方法名极少出现在 __all__；类在 __all__ 时，方法归到“类是 API”一并豁免
                            in_dunder_all=node.name in info.dunder_all,
                        )
                    )
    return caps


# ── 白名单 ──────────────────────────────────────────────────────────────────
@dataclass
class Allowlist:
    exact: dict[str, str]  # "owcopilot.mod::qualname" -> reason
    module_wild: dict[str, str]  # "owcopilot.mod" -> reason（整模块豁免）

    def match(self, cap: Capability) -> str | None:
        key = f"{cap.module}::{cap.qualname}"
        if key in self.exact:
            return self.exact[key]
        if cap.module in self.module_wild:
            return self.module_wild[cap.module]
        return None


def _load_allowlist(path: Path) -> Allowlist:
    exact: dict[str, str] = {}
    module_wild: dict[str, str] = {}
    if not path.is_file():
        return Allowlist(exact, module_wild)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry, _, reason = line.partition("#")
        entry = entry.strip()
        reason = reason.strip() or "(未注明理由)"
        if "::" not in entry:
            continue
        mod, _, qual = entry.partition("::")
        mod = mod.strip()
        qual = qual.strip()
        if qual == "*":
            module_wild[mod] = reason
        else:
            exact[f"{mod}::{qual}"] = reason
    return Allowlist(exact, module_wild)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _load_modules(src_root: Path) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    paths = [p for p in src_root.rglob("*.py") if "__pycache__" not in p.parts]
    # 先建全量 dotted 名集合，供 import 目标解析
    dotted_of: dict[Path, str] = {p: _dotted_name(src_root, p) for p in paths}
    known = set(dotted_of.values())
    for p in paths:
        dotted = dotted_of[p]
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as exc:  # 罕见：编码/语法异常，明说而非静默
            print(f"[warn] 跳过无法解析的文件 {p}: {exc}", file=sys.stderr)
            continue
        rel_parts = _rel_parts_under_pkg(src_root, p)
        is_init = p.name == "__init__.py"
        info = ModuleInfo(
            dotted=dotted,
            path=p,
            tree=tree,
            is_init=is_init,
            is_entry=_is_entry(rel_parts),
            is_test_like=False,
            dunder_all=_extract_dunder_all(tree),
        )
        pkg = _package_of(dotted, is_init)
        info.import_targets = _collect_import_targets(tree, pkg, known)
        info.referenced_names = _collect_referenced_names(tree)
        modules[dotted] = info
    return modules


def _live_surface(modules: dict[str, ModuleInfo]) -> set[str]:
    """从四个产品入口沿 import 边 BFS 到不动点，得到生产可达模块集合。"""
    seeds = [m.dotted for m in modules.values() if m.is_entry]
    seen: set[str] = set(seeds)
    q: deque[str] = deque(seeds)
    while q:
        cur = q.popleft()
        info = modules.get(cur)
        if info is None:
            continue
        for tgt in info.import_targets:
            if tgt not in seen and tgt in modules:
                seen.add(tgt)
                q.append(tgt)
    return seen


def _referenced_in(modules: dict[str, ModuleInfo], module_names: set[str]) -> set[str]:
    out: set[str] = set()
    for name in module_names:
        info = modules.get(name)
        if info is not None:
            out |= info.referenced_names
    return out


@dataclass
class ScanResult:
    orphans: list[tuple[Capability, str]]  # (cap, 次级信号)
    declared_api: list[Capability]  # 在定义模块 __all__ 里，informational
    whitelisted: list[tuple[Capability, str]]  # (cap, reason)
    counts: dict[str, int]
    live_module_count: int
    total_module_count: int


def scan(repo_root: Path, allowlist_path: Path) -> ScanResult:
    src_root = _find_src_root(repo_root)
    modules = _load_modules(src_root)
    allowlist = _load_allowlist(allowlist_path)

    live = _live_surface(modules)
    product_refs = _referenced_in(modules, live)
    all_src_names = set(modules)
    all_src_refs = _referenced_in(modules, all_src_names)

    orphans: list[tuple[Capability, str]] = []
    declared_api: list[Capability] = []
    whitelisted: list[tuple[Capability, str]] = []
    counts = {"function": 0, "class": 0, "method": 0}

    for info in modules.values():
        # 入口目录自身的符号是“根”，不作候选（由框架调用，不会被别处按名引用）
        if info.is_entry or info.path.name in _CANDIDATE_SKIP_BASENAMES:
            continue
        for cap in _enumerate_capabilities(info):
            if cap.name in product_refs:
                continue  # 被生产路径引用 → 非孤儿
            reason = allowlist.match(cap)
            if reason is not None:
                whitelisted.append((cap, reason))
                continue
            if cap.in_dunder_all:
                declared_api.append(cap)  # 声明为库 API，转 informational 档
                continue
            # 次级置信信号：是否被“任何 src（含非产品可达）”引用过
            if cap.name in all_src_refs:
                signal = "仅被非产品 src 引用（可能是整条子树都没接进产品）"
            else:
                signal = "全项目零引用（仅 tests 或无人引用）"
            orphans.append((cap, signal))
            counts[cap.kind] += 1

    orphans.sort(key=lambda t: (t[0].kind, t[0].module, t[0].qualname))
    declared_api.sort(key=lambda c: (c.module, c.qualname))
    whitelisted.sort(key=lambda t: (t[0].module, t[0].qualname))
    return ScanResult(
        orphans=orphans,
        declared_api=declared_api,
        whitelisted=whitelisted,
        counts=counts,
        live_module_count=len(live),
        total_module_count=len(modules),
    )


# ── 报告渲染 ────────────────────────────────────────────────────────────────
def _render_markdown(res: ScanResult) -> str:
    lines: list[str] = []
    a = lines.append
    a("# 零调用能力扫描报告")
    a("")
    a("> 由 `scripts/scan_orphan_capabilities.py` 生成。软门：仅报告，不阻断。")
    a("")
    a("## 概览")
    a("")
    a(f"- 生产可达模块 / 全部 src 模块：**{res.live_module_count} / {res.total_module_count}**")
    total = sum(res.counts.values())
    a(
        f"- 零生产调用能力（可行动项）：**{total}** "
        f"（函数 {res.counts['function']} / 类 {res.counts['class']} "
        f"/ 方法 {res.counts['method']}）"
    )
    a(f"- 声明为库 API（在定义模块 __all__）：{len(res.declared_api)}")
    a(f"- 白名单豁免：{len(res.whitelisted)}")
    a("")

    def _table(title: str, rows: list[tuple[Capability, str]]) -> None:
        a(f"### {title}（{len(rows)}）")
        a("")
        if not rows:
            a("_无_")
            a("")
            return
        a("| 模块 | 符号 | 类型 | 行 | 次级信号 |")
        a("| --- | --- | --- | --: | --- |")
        for cap, sig in rows:
            a(f"| `{cap.module}` | `{cap.qualname}` | {cap.kind} | {cap.lineno} | {sig} |")
        a("")

    a("## 可行动：零生产调用能力")
    a("")
    funcs = [(c, s) for c, s in res.orphans if c.kind == "function"]
    classes = [(c, s) for c, s in res.orphans if c.kind == "class"]
    methods = [(c, s) for c, s in res.orphans if c.kind == "method"]
    _table("档 A · 模块级公开函数（高置信）", funcs)
    _table("档 B · 公开类，生产面从未引用（中置信）", classes)
    _table("档 C · 公开方法（低置信：同名撞名风险，需人工核）", methods)

    a("## Informational：声明为库 API（__all__）")
    a("")
    if res.declared_api:
        a("| 模块 | 符号 | 类型 | 行 |")
        a("| --- | --- | --- | --: |")
        for cap in res.declared_api:
            a(f"| `{cap.module}` | `{cap.qualname}` | {cap.kind} | {cap.lineno} |")
    else:
        a("_无_")
    a("")

    a("## 白名单豁免")
    a("")
    if res.whitelisted:
        a("| 模块 | 符号 | 理由 |")
        a("| --- | --- | --- |")
        for cap, reason in res.whitelisted:
            a(f"| `{cap.module}` | `{cap.qualname}` | {reason} |")
    else:
        a("_无_")
    a("")

    a(_METHOD_AND_LIMITS)
    return "\n".join(lines)


# 报告尾部固定说明：范围、判定口径、已知漏检、白名单维护。让报告自解释、可交接。
_METHOD_AND_LIMITS = """## 方法、范围与薄弱点

**判定口径**：一个符号被视为“已接进产品”，当且仅当它的裸名在“生产可达面”
（从 cli/service/mcp_server/app.actions 沿 import 传递可达的 src 模块集合）内，
以 `Call` / `Attribute` / `Name-load` 形式被引用到。纯 `import`/再导出不算引用
（否则 `__init__.py` 的 re-export 会把孤儿伪装成已用）。

**覆盖三档**：模块级公开函数（档 A，高置信）、公开类（档 B）、公开类的公开方法
（档 C，低置信）。入口目录（四个产品路径）自身的符号视为“根”，不作候选。

**已知会漏检 / 需人工复核的类别**：
- **跨语言调用**：UE 侧 Python (`integrations/unreal/.../*.py`)、UE C++、Unity C# 对
  Python 能力的调用，纯 Python AST 看不到 → 相关导出可能被误列为孤儿（如
  `integrations.unreal_remote` 一族）。
- **字符串反射 / 动态派发**：`getattr(mod, name)`、`registry.run("name")` 之类按字符串
  取用的 handler，静态不可见。本项目内置技能 handler 以 `tools.<fn>` 按引用注册，
  能被识别；若日后改成字符串派发，需登记进白名单。
- **条件 / 延迟 import**：函数体内的 `import`、`TYPE_CHECKING` 分支、插件式发现，
  import 图可能连不全，导致该模块被误判为不可达。
- **同名方法撞名**：档 C 用属性名匹配，`run`/`load`/`save`/`get` 等公共名会被任意
  同名调用点“借用”成已引用 → 真正的孤儿方法可能漏检（假阴性，方向保守）。
- **未传的参数（非本扫描目标）**：某能力函数存在，但其某个可选参数在所有调用点都没被
  传（例：`exporters.unreal_datatable.export_unreal_datatables` 的 `row_struct_map`
  四入口零传参）——这是“参数级死角”，符号本身已被调用，本扫描按设计不覆盖，需独立的
  “形参未传”分析。
- **缺失的能力（非本扫描目标）**：需求存在但符号根本没实现（例：assist 扫描的“逐一
  dismiss”从未落地）——扫描只能发现“已实现却没接线”，无法发现“压根没实现”。

**白名单维护**：见 `scripts/orphan_scan_allowlist.txt`。仅登记“确有生产用途、只是静态
匹配抓不到”的符号（反射派发、跨语言入口、明确对外接口），每条附理由；模块自身 `__all__`
里的符号由脚本自动归入上面的“声明为库 API”档，无需手工登记。
"""


def _render_summary(res: ScanResult) -> str:
    total = sum(res.counts.values())
    parts = [
        f"零调用能力扫描：可行动 {total} 项",
        f"(函数 {res.counts['function']}/类 {res.counts['class']}/方法 {res.counts['method']})",
        f"｜__all__ API {len(res.declared_api)}｜白名单 {len(res.whitelisted)}",
        f"｜可达模块 {res.live_module_count}/{res.total_module_count}",
    ]
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="零调用能力（orphan capability）静态扫描")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="仓库根目录（默认：本脚本上一级）",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path(__file__).resolve().parent / "orphan_scan_allowlist.txt",
        help="白名单文件路径",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="报告输出路径（Markdown）；默认 docs/零调用能力扫描报告.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="硬门模式：存在可行动孤儿则退出码 1（默认关闭，软门只报告）",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    out_path: Path = args.out or (repo_root / "docs" / "零调用能力扫描报告.md")

    res = scan(repo_root, args.allowlist.resolve())
    report = _render_markdown(res)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print(_render_summary(res))
    print(f"报告已写入：{out_path}")

    if args.check and sum(res.counts.values()) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
