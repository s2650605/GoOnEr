#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests.py — 一键跑完本项目的回归测试。

为什么需要：这个工具由三方组成（两个监控脚本 + 一个 Pi 插件），
它们之间靠「命令行参数」和「文件通道指令」两份隐式契约耦合。
改动任一侧都可能让另一侧静默失效（GUI 传了脚本不认识的参数、
插件不认新指令……），所以必须有能一键跑完的检查。

覆盖范围：
  1. 语法检查（Python + 插件 JS）
  2. 续接逻辑单测（深链、模型继承、监控集合切换、dry-run 安全）
  3. GUI ↔ 监控脚本的参数对齐（含真跑一次 Pi 分支）
  4. 插件动作单测（compact / create，用 mock 的 pi API）
  5. 端到端场景（连续超限 → 压缩 → 新建会话 → 接管）
  6. 主循环接入（确认 main() 真的会走续接分支，而不只是函数能跑）
  7. 真实通道自检（需要 PI-Desktop 正在运行；不在则跳过）

用法：
  python tests/run_tests.py            # 全部
  python tests/run_tests.py --quick    # 跳过需要 Pi 在线的真实通道测试
"""
import argparse
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def run(label, cmd, required=True):
    """跑一条命令，返回是否通过。"""
    print()
    print("=" * 70)
    print(label)
    print("=" * 70)
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    # 只显示尾部，避免刷屏；失败时多显示一些
    lines = out.rstrip().splitlines()
    tail = lines[-25:] if proc.returncode == 0 else lines[-60:]
    for line in tail:
        print("  " + line)
    ok = proc.returncode == 0
    if not ok and required:
        print(f"  ✗ 失败（退出码 {proc.returncode}）")
    return ok


def main():
    ap = argparse.ArgumentParser(description="跑完本项目的回归测试")
    ap.add_argument("--quick", action="store_true",
                    help="跳过需要 PI-Desktop 在线的真实通道测试")
    args = ap.parse_args()

    results = []

    # 1. 语法
    py_files = ["pi_monitor.py", "codex_monitor_gui.py", "codex_monitor.py", "pi_archive.py",
                "codex_activity.py", "build.py", "install_pi_plugin.py", "update_pi_plugin.py",
                "check_pi_plugin.py"]
    results.append(("Python 语法检查",
                    run("1. Python 语法检查",
                        [sys.executable, "-m", "py_compile"] + py_files)))
    results.append(("插件 JS 语法检查",
                    run("2. 插件 main.js 语法检查",
                        ["node", "--check", "pi-session-keeper/main.js"])))

    # 2-4. 单测
    results.append(("续接逻辑单测",
                    run("3. 续接逻辑单测", [sys.executable, str(HERE / "test_context_continue.py")])))
    results.append(("GUI 参数对齐",
                    run("4. GUI ↔ 监控脚本参数对齐",
                        [sys.executable, str(HERE / "test_gui_args.py")])))
    results.append(("插件动作单测",
                    run("5. 插件 compact / create 动作单测",
                        ["node", str(HERE / "test_plugin.js")])))
    results.append(("GUI 控件联动",
                    run("5b. GUI 控件联动（真实实例化窗口）",
                        [sys.executable, str(HERE / "test_gui_widgets.py")])))
    results.append(("归档/删除屏蔽",
                    run("5d. 归档/删除会话屏蔽（读 leveldb + 列表 + 选会话）",
                        [sys.executable, str(HERE / "test_pi_archive.py")])))
    results.append(("单实例闸门",
                    run("5c. 单实例闸门（重复启动只留一个控制台）",
                        [sys.executable, str(HERE / "test_single_instance.py")])))
    results.append(("inProgress 残留识别",
                    run("5e. inProgress 残留识别（被打断的 turn 不再算进行中）",
                        [sys.executable, str(HERE / "test_codex_stale.py")])))
    # 5-6. 端到端
    results.append(("端到端场景",
                    run("6. 端到端场景（超限 → 压缩 → 新建会话 → 接管）",
                        [sys.executable, str(HERE / "test_e2e_scenario.py")])))
    results.append(("主循环接入",
                    run("7. 主循环接入验证",
                        [sys.executable, str(HERE / "test_mainloop.py")])))
    results.append(("关机功能",
                    run("7b. 全部完成后关机 + 跨来源协调",
                        [sys.executable, str(HERE / "test_shutdown.py")])))
    results.append(("关机端到端",
                    run("7c. 关机端到端实测（两个真实进程）",
                        [sys.executable, str(HERE / "test_e2e_shutdown.py")])))
    results.append(("关机 GUI 链路",
                    run("7d. 关机 GUI 链路实测（GUI 勾选 → 假关机指令）",
                        [sys.executable, str(HERE / "test_shutdown_gui_e2e.py")])))
    results.append(("Codex 400 续接",
                    run("7e. Codex 400 续接实测（真实调用 codex exec）",
                        [sys.executable, str(HERE / "test_400_e2e.py")])))
    results.append(("400 续接流水线",
                    run("7f. 400 自动续接流水线（监控范围/触发/交接/保护）",
                        [sys.executable, str(HERE / "test_400_pipeline.py")])))
    results.append(("报400直接新建会话",
                    run("7g. 报 400 直接新建会话（真实主循环 + 假插件通道）",
                        [sys.executable, str(HERE / "test_400_direct.py")])))

    # 7. 真实通道（可选）
    if args.quick:
        print()
        print("=" * 70)
        print("8. 真实通道自检（--quick，已跳过）")
        print("=" * 70)
    else:
        results.append(("真实通道",
                        run("8. 真实通道自检（需要 PI-Desktop 在线）",
                            [sys.executable, str(HERE / "test_real_channel.py")],
                            required=False)))

    # 汇总
    print()
    print("=" * 70)
    print("汇总")
    print("=" * 70)
    failed = [name for name, ok in results if not ok]
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print()
    if failed:
        print(f"有 {len(failed)} 项未通过：{'、'.join(failed)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
