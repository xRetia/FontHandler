# FontHandler 2.0 (PyQt6)

用 PyQt6 重写 `GaspHack_v2_MOD.bat` + `FontReplace.ps1`：生成 GaspHack 补丁字体、
替换系统字体、并把 TrustedInstaller 权限恢复原样。补上了原脚本缺失的备份、
重启队列校验和 ACL 还原。

```
pip install -r requirements.txt
python run.py
```

首次运行会提示需要管理员权限——点状态栏的 **「以管理员身份重启」**（或
`Ctrl+Shift+A`），在 UAC 弹窗里选「是」即可。

---

## 目录

- [它做了什么](#它做了什么)
- [安装与运行](#安装与运行)
- [权限模型](#权限模型)
- [五个页面](#五个页面)
- [沙盒模式](#沙盒模式)
- [相对原脚本修掉的问题](#相对原脚本修掉的问题)
- [字节级等价](#字节级等价)
- [数据结构与文件位置](#数据结构与文件位置)
- [项目结构](#项目结构)
- [测试](#测试)
- [故障排查](#故障排查)
- [已知限制](#已知限制)

---

## 它做了什么

| 能力 | 说明 |
|---|---|
| GaspHack | 从系统字体生成打好补丁的副本，产物逐字节对齐原工具链 |
| TTC 拆合 | `AllUniteTTC` 等价实现，可拆可合 |
| 热替换 | `MoveFileExW(REPLACE_EXISTING)`，被占用时自动排入重启队列 |
| 自动备份 | 替换前打包原件 + SHA256 + 原 ACL，可整包还原 |
| 权限还原 | 还原到 TrustedInstaller，或重置为纯继承 |
| 重启队列 | 写入后回读校验，不假装成功 |

## 安装与运行

**要求** Windows 10/11 + Python 3.10 或更高 + PyQt6。

```
pip install -r requirements.txt
python run.py
```

`run.py` 会调用 `FreeConsole()` 摘掉控制台窗口，直接双击也不会在界面后面留一个
黑框。为此所有子进程（`icacls`、`net`）都必须带 `CREATE_NO_WINDOW`——没有它，
父进程没有控制台时 Windows 会给每个子进程新开一个并闪一下。这条约束写在
`acl.run_hidden()` 里，新加的子进程调用都要走它。

## 权限模型

把字体所有者改成 TrustedInstaller 需要三样东西同时成立，缺一不可：

1. **提升的令牌** — 用 `TokenIsElevated` 判断
2. **开启的特权** — 提权后的令牌默认带着这些权限但处于禁用状态，要用
   `AdjustTokenPrivileges` 逐个打开
3. **能写 DACL** — 需要 `SeRestorePrivilege` / `SeTakeOwnershipPrivilege`

已请求的特权（`elevation.REQUIRED_PRIVILEGES`）：

| 特权 | 用途 |
|---|---|
| `SeRestorePrivilege` | 把快照里的安全描述符写回去 |
| `SeTakeOwnershipPrivilege` | 成为当前不属于自己的文件的所有者 |
| `SeBackupPrivilege` | 读取 SACL 的前提 |
| `SeSecurityPrivilege` | 读取审计 SACL |

点状态栏 **「权限」** 按钮可以看到 Windows 实际授予了哪些。

### 两个容易踩的坑

**`IsUserAnAdmin()` 不能用来判断是否提权。** 它只回答「用户是否属于
Administrators 组」，而 UAC 交给管理员账号的**筛选令牌**下这个返回值依然是 TRUE。
用它的后果是界面显示「已提权」，但一个特权都开不出来——看起来像 Windows 拒绝了
权限，实际是根本没提权。判断提权要看 `TokenIsElevated`。

**`AdjustTokenPrivileges` 即使没能分配权限也返回 TRUE。** 真实结果在
`GetLastError()` 里（`ERROR_SUCCESS` 为成功，`1300`
`ERROR_NOT_ALL_ASSIGNED` 表示被筛选令牌拒绝）。只看返回值的话，「权限已开启」
永远是假的。

**`CloseHandle` 在 kernel32，不在 advapi32。** 在 advapi32 上调用它抛
`AttributeError`；如果这行写在 `finally:` 里，异常会**顶掉已经算好的返回值**，
再被宽泛的 `except AttributeError` 吞成 `False`。表现就是：提权成功的进程也报
「非管理员」、特权一个都开不出来——看起来像 Windows 拒绝，实际是自己的清理代码
吃掉了结果。`acl._close_handle()` 专门兜住这种情况：关句柄失败绝不能改变调用方
已经得到的结论。

## 五个页面

| 页面 | 作用 |
|---|---|
| GaspHack | 从系统字体生成打好补丁的副本；内置校验 |
| 替换字体 | 扫描 → 自动备份 → 热替换 → 占用时排入重启队列 |
| 备份恢复 | zip 备份包（原件 + SHA256 + 原 ACL）；可整包还原 |
| TrustedInstaller | 扫描/预览权限所有者，重置 DACL 为纯继承，清理残留 `.new` |
| 重启队列 | 查看 / 清理 `PendingFileRenameOperations` |

长任务跑在后台线程，带协作式取消；底部是可折叠日志面板（可按级别过滤，
最多 5000 行）。重复启动时后一个进程直接退出，不会开第二个窗口
（`ui/single_instance.py` 持有本地 socket）。

> 提权重启会先释放单实例锁再拉起新进程，否则新进程会以为已有窗口而静默退出。

## 沙盒模式

把「目标目录」设成 `C:\Windows\Fonts` 以外的任何路径即进入沙盒：所有替换、
备份、重启队列写入都只作用于该目录，界面挂紫色徽标，字体缓存清理与注册表
写入自动跳过。

`selftest.py` 的全部测试都在 `%TEMP%\FontHandlerSandbox_*` 里跑，从不写真实
系统目录。

## 相对原脚本修掉的问题

| # | 原脚本的问题 | 现在 |
|---|---|---|
| 1 | 残留冗余显式 ACE（`TrustedInstaller:(F)` 与目录其它字体不一致） | 暂存文件继承目录 DACL，替换后 `icacls /reset` + 还原所有者 |
| 2 | 无备份 | 替换前自动打包，可还原 |
| 3 | 不记录原 ACL | 每次替换前 `snapshot()` 完整安全描述符 |
| 4 | 排入重启队列不校验 | `MoveFileExW(DELAY_UNTIL_REBOOT)` 后回读 `PendingFileRenameOperations`，写不进就报错 |
| 5 | 源目录硬编码 | 可配置 |
| 6 | 残留 `.new` 无清理入口 | TrustedInstaller 页有按钮 |

实现层面修掉的几个具体缺陷：

- **`get_sddl()` 恒返回空** — 原来调 `icacls <path> /save` 但没给输出文件，
  icacls 固定返回 87。现在走 `GetNamedSecurityInfoW` +
  `ConvertSecurityDescriptorToStringSecurityDescriptorW`。
- **`set_owner()` 顺手改 DACL** — 原来用 `icacls /setowner`，它会把继承的
  `(A;ID;FA;;;OW)` 改写成 `(A;IOID;FA;;;OW)`，导致快照/还原无法还原成原样。
  现在只发 `OWNER_SECURITY_INFORMATION`，且所有者已是目标值时直接跳过写入。
- **`get_last_error()` 报假错误** — `ctypes.windll` 不保存 last-error，
  `MoveFileExW` 失败时会读到无关的陈旧值。表现为 `WinError 58`
  （`ERROR_BAD_NET_RESP`「指定的服务器无法运行请求的操作」），把排查引向网络。
  现在统一 `WinDLL(..., use_last_error=True)`。
- **子进程控制台窗口** — 见[安装与运行](#安装与运行)。
- **中文错误信息乱码** — `icacls`/`net` 按 OEM 代码页输出（中文系统是 936/GBK），
  按 UTF-8 解码会变成乱码。现在用 `acl.console_encoding()` 取 `GetOEMCP()`。

## 字节级等价

对照原工具链 `workingDir\output` 的产物逐字节比对：

| 范围 | 结果 |
|---|---|
| CJK 白名单 24 个字体（11 TTC + 13 TTF） | **24/24 逐字节相同** |
| 系统全部单字体 TTF（139 个） | 131/139 相同 |

余下 8 个的差异来自 `ttx.exe` 自己会反编译再重编译某些表（`wingding.ttf` 的
`cmap` 被它从 768 字节压成 632 字节）。本实现保留原始字节，不做这种无意义的
重写，因此在这类字体上与参考不同——这是更保守的行为，且不影响任何白名单字体。

## 数据结构与文件位置

所有用户数据在 `%LOCALAPPDATA%\FontHandler`：

| 路径 | 内容 |
|---|---|
| `userdata\` | 设置文件 |
| `gasp\` | 生成的 GaspHack 字体（默认源目录） |
| `backups\` | zip 备份包 |
| `logs\` | 应用日志 |
| `crash.log` | 启动期未捕获异常的堆栈 |

写系统：`C:\Windows\Fonts`、`HKLM\...\Session Manager\PendingFileRenameOperations`。

## 项目结构

```
pyqt/
├─ run.py            启动入口（摘控制台 + 崩溃日志兜底）
├─ selftest.py       无 GUI 回归测试
├─ fonthandler/
│  ├─ sfnt.py        表级 sfnt/TTC 读写（规范表序 + 每 face 校验和）
│  ├─ gasp.py        GaspHack 补丁 + TTC 拆合（AllUniteTTC 等价）
│  ├─ config.py      CJK 白名单 / 排除表 / 设置持久化
│  ├─ acl.py         SDDL 快照与还原 / TrustedInstaller 所有者
│  ├─ registry.py    HKLM（可注入）+ 待重启重命名队列
│  ├─ session.py     PendingFileRenameOperations 读写
│  ├─ replace.py     热替换 / 重启队列引擎（可注入 FileOps）
│  ├─ backup.py      zip 备份包 + 恢复
│  ├─ pipeline.py    高层流程，不依赖 Qt
│  ├─ fontcache.py   字体缓存清理
│  ├─ livefont.py    WM_FONTCHANGE 广播
│  ├─ systeminfo.py  只读环境探测
│  ├─ elevation.py   管理员检测 / 特权开启 / UAC 自提权
│  └─ ui/
│     ├─ main_window.py     主窗口 + AppContext
│     ├─ page_gasp.py       GaspHack 页
│     ├─ page_replace.py    替换字体页
│     ├─ page_backup.py     备份恢复页
│     ├─ page_acl.py        TrustedInstaller 页
│     ├─ page_pending.py    重启队列页
│     ├─ common.py          Worker 线程 + BasePage 基类
│     ├─ single_instance.py 单实例锁
│     ├─ log_panel.py       可折叠日志面板
│     └─ style.py           QSS 主题
└─ tests/
   ├─ sandbox.py     临时目录 + 内存注册表/ACL
   └─ test_core.py   83 项断言
```

`sfnt.py` / `gasp.py` 不 import 任何第三方库，可单独拿去用。

## 测试

```
python selftest.py            # 全部
python selftest.py sfnt gasp  # 指定分组
python selftest.py -v         # 详细输出
```

测试不需要 GUI，也从不写系统目录。部分用例会真的调 Windows API（临时目录上的
真实安全描述符读写、特权启用），在非 Windows 或非管理员环境下自动跳过。

`byte` 分组需要原项目的 `ttx.exe` 与 `workingDir\output` 作为 ground truth；
缺省会明确报错而不是静默跳过。

## 故障排查

**启动时黑框一闪一闪 / 反复弹窗** — 之前所有 `icacls`/`net` 调用都会闪一个
控制台窗口。现在统一走 `acl.run_hidden()`。若自己加了子进程调用，别直接用
`subprocess.run`。

**`WinError 58`（指定的服务器无法运行请求的操作）** — 58 是
`ERROR_BAD_NET_RESP`，跟网络无关，通常是 last-error 被污染的陈旧值。检查新代码
有没有用 `ctypes.windll` 而非 `WinDLL(..., use_last_error=True)`。

**`WinError 5`（拒绝访问）设不了 TrustedInstaller** — 没提权，或令牌被筛选。
点状态栏「权限」看哪些特权没拿到；如果是四项全「未启用」，那是根本没提权，
点「以管理员身份重启」。

**`WinError 1300`** — `ERROR_NOT_ALL_ASSIGNED`，同一个精简令牌问题。

**提权后新窗口没出现** — 单实例锁没释放。`elevate_and_exit()` 前必须先
`guard.release()`。

**启动崩溃但什么都没显示** — 控制台已被摘掉，堆栈写在
`%LOCALAPPDATA%\FontHandler\crash.log`。

**中文错误信息是乱码** — 子进程输出要按 OEM 代码页解码，用
`acl.console_encoding()` 而不是 `encoding="utf-8"`。

## 已知限制

- 只在 Windows 上有意义；其它平台自动降级为沙盒。
- 热替换失败（字体被占用）只能排入重启队列，需要重启才生效。
- 清字体缓存需要停 `FontCache` 服务，非管理员会静默跳过。
- 快照/还原覆盖 DACL 与所有者，不含 SACL（读 SACL 要 `SeSecurityPrivilege`，
  且请求它会让整个调用在普通账户上失败）。
- 替换进行中关闭程序不会回滚已完成的替换——备份包里都有原件。