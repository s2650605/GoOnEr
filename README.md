# GoOnEr · Codex / PI-Desktop 会话监控工具

挂机跑 AI 会话的看护程序：会话异常中止就按退避节奏补发「继续」，上下文爆了自动压缩或
新建会话交接，活干完了按规则关机 —— 同时盯 **Codex** 和 **PI-Desktop** 两个来源。

> **用户手册在 [使用说明.txt](使用说明.txt)**：界面怎么用、每个参数什么意思、各种场景怎么配，
> 都在那里。本 README 只讲「这个仓库是什么、怎么跑起来、怎么改、怎么发版」。

## 解决什么问题

夜里挂长任务，最怕三件事：

1. 会话中途 `aborted` / `error`，任务停在那儿没人管；
2. 上下文超限（`CONTEXT_TOO_LARGE`），再发「继续」也没用；
3. 活干完了，机器还开一整夜。

这个工具就是盯这三件事，并且保证一条底线：**活没干完绝不关机**。

## 功能一览

| 能力 | 说明 |
|---|---|
| 中止续接 | 会话最后一个 turn 属于中止状态时，按退避节奏（默认 1,1,3,5,5 分钟 ±3 秒，成功一轮后清零）发送「继续」；每会话每小时上限 20 次；发送前二次确认（重读数据库仍中止才发） |
| 上下文超限续接 | 连续 N 次命中 `CONTEXT_TOO_LARGE` 后：`compact` 先压缩上下文、压不动再新建会话；`new` 直接新建。新会话的首条消息：Pi 侧由插件带上**原会话深链 + 未完成的 todo + 最后一条用户消息**，Codex 侧只带深链 |
| 真报 400 直开 | 读 Pi 日志里的**真实 HTTP 状态码**（与当前回合时间对齐）判定，不等连续次数、不先压缩，直接新建会话 |
| 关机规则 | ① 错误码（Codex 默认 405/503、Pi 可配）连续 N 次；② 全部无活动满 N 分钟；③ 全部正常完成 + 观察 N 秒；④ 近期活跃会话**全部报 503**（服务商整体挂了） |
| 跨来源协调 | 两个进程各自只看得到自己那部分会话，通过 `~/.session-monitor/shutdown-coord.json` 只登记、不覆盖，要求「所有监控来源都完成」才关机 |
| 归档 / 删除屏蔽 | 已归档、已删除的会话不进会话列表，也不会被自动发「继续」 |
| 无人值守门禁 | 权限模式不是「全自动」的会话半夜会卡在确认上 → 启动时检查并**拒绝启动**（`--allow-unsafe` 可绕过） |
| 安全试跑 | `--dry-run` 只检测不发送、不关机；`--once` 只跑一轮就退出（自检用） |

## 两个来源的差别

| | Codex | PI-Desktop |
|---|---|---|
| 数据来源 | `~/.codex/state_5.sqlite` + `thread_history_1.sqlite` | `~/.pi-desktop/pi.sqlite` |
| 中止口径 | turn 状态 `failed` / `interrupted` | turn 状态 `aborted` / `error` |
| 怎么发「继续」 | `codex queue --thread <id> --message 继续` | 往 `~/.pi-desktop/session-keeper/commands/` 写指令文件，由插件走官方 `agent/prompt` 投递 |
| 续接链 | 沿 `codex://threads/<父id>` 引用自动跟随中继链 | 不适用（新建会话由插件创建） |
| 归档判定 | 读 `threads.archived` | 读 PI-Desktop 的 Local Storage（**不在数据库里**，见 `pi_archive.py`） |
| 新建会话（400/超限） | `codex exec -C <项目目录> "请继续这个会话的任务：codex://threads/<id>"` | 插件 `create` 动作 |

## 仓库结构

```
pi_monitor.py            Pi 侧监控：选会话、检测中止、发「继续」、关机规则、跨来源协调
codex_monitor.py         Codex 侧监控：同上（多了中继链跟随）
codex_monitor_gui.py     控制台 GUI：勾选会话、拼参数、看日志、插件心跳
pi_archive.py            读 PI-Desktop 的「已归档会话」表（leveldb + snappy，无第三方依赖）
shutdown_coord.py        「全部完成后关机」的跨来源协调（纯文件，无端口、无守护进程）
pi-session-keeper/       PI-Desktop 插件源码（动作：prompt / abort / compact / create / list / ping）
build.py                 一键打包：编译三个 exe + 归位源码 + 打分发包 zip
tests/                   回归测试（run_tests.py 一键跑完）
使用说明.txt             完整用户手册（分发包里那份由 build.py 复制生成）
Codex会话监控工具/        分发包目录：Mac/（源码）、Windows/（exe）、Pi插件/（插件源码副本）
```

## 快速开始

### Windows（推荐，双击即用）

1. 解压分发包 `Codex会话监控工具.zip`；
2. 确保要监控的应用已打开并登录（Codex 桌面端 / PI-Desktop）；
3. 进 `Windows` 文件夹，双击 `codex_monitor_gui.exe`；
4. 首次建议勾上「模拟运行」试跑，确认日志里能看到你的会话，再取消勾选正式启动。

### 源码运行

```bash
python codex_monitor_gui.py            # 控制台 GUI（需要 tkinter）
python pi_monitor.py --help            # Pi 侧监控的全部参数
python codex_monitor.py --help         # Codex 侧监控的全部参数
```

开发与 CI 用的 Python 是 **3.14**（更早的版本没有验证过）。

### macOS

进 `Mac/` 目录，双击 `启动.command`（需要 `python3` + `tkinter`）。该文件在仓库里已置
可执行位，`*.command` 也固定用 LF 换行（见 `.gitattributes`）。

### 监控 Pi 会话必做：装插件

PI-Desktop → 左下角「扩展」→ 右上角「更多操作 ⋯」→「安装插件包」→ 选
`local.session-keeper-1.9.2.piplug`，权限页里 `ui.panel` / `background.service` /
`desktop.control` 三项都要允许。

## 打包与发布

```bash
python build.py              # 编译三个 exe + 归位源码 + 打 Codex会话监控工具.zip
python build.py --no-exe     # 跳过编译（非 Windows 上只能这样）
python tests/test_package.py # 检查产物：exe 能起来、zip 结构齐
```

打包参数（`onedir`、内容目录 `_pi` / `_codex` / `_gui`、GUI 用 `--windowed`）都固化在
`build.py` 里，不再靠「上次是怎么打的」这种口头知识。

**CI**：`.github/workflows/build.yml` 在 `windows-latest` 上跑一次 `build.py`，**同时产出
两个平台的内容** —— Mac 包只是「根目录源码 + 启动.command」的拷贝，不需要编译，所以不必开
macOS runner（省 10 倍计费）。产物上传为 artifact；打 `v*` tag 时用 `gh` 发 Release。

## 测试

```bash
python tests/run_tests.py            # 全部（含端到端：会真的起监控进程、调 codex exec）
python tests/run_tests.py --quick    # 跳过需要 PI-Desktop 在线的真实通道测试
```

部分端到端用例会真的启动监控进程、写指令文件、调用 `codex exec`（消耗 token），跑之前看清
`tests/` 里各文件顶部的说明。归档屏蔽相关的用例在 `tests/test_pi_archive.py`。

## 开发约定

- **源码只认根目录一份**：`Codex会话监控工具/` 里的 `.py` 与 `使用说明.txt` 都是 `build.py`
  从根目录复制过去的产物，不要手工改那边。
- 不进版本库（见 `.gitignore`）：`dist/`、`Codex会话监控工具/Windows/`（exe + 内容目录）、
  `Codex会话监控工具.zip`、`*.log`、`tests/_tmp/`、旧版 onefile exe 备份。
- `*.command` / `*.sh` 固定 LF、`*.bat` 固定 CRLF（`.gitattributes`）—— Mac 启动脚本被检出成
  CRLF 就跑不起来。

## 已知限制

- `local.session-keeper-1.9.2.piplug` 只能在本机用 PI-Desktop 的 PluginPack 打，**CI 里做不出来**；
  插件源码改了要手动重新打包并替换分发包里的那份。
- 「已归档」读的是 PI-Desktop 的内部存储（Electron Local Storage 的 leveldb）。它哪天换格式，
  `pi_archive.py` 会读不到 —— 此时**退回「不屏蔽」并在界面与日志里明说**，不会猜成「全都已归档」。
- 分发包 zip 不含旧版 onefile exe 备份（`Codex会话监控工具/_旧版onefile备份/`），那只是本机回退用的。
