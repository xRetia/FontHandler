# FontHandler 2.0 (PyQt6)

让 Windows 字体看着更舒畅。FontHandler 基于 GaspHack 技术，让 Windows 原生渲染出类似 MacType 的清晰效果，并且具有不错的兼容性。

FontHandler 用 PyQt6 编写，提供图形界面来完成整套操作：

- 生成 GaspHack 补丁字体
- 替换系统字体
- 把字体的所有者和权限还原为 TrustedInstaller 状态

```bash
pip install -r requirements.txt
python run.py
```

首次运行需要管理员权限。请点状态栏上的 **「以管理员身份重启」**（或 `Ctrl+Shift+A`），在 UAC 对话框中点「是」即可。

## 功能

| 功能 | 说明 |
|---|---|
| GaspHack 补丁生成 | 从系统字体生成带 GaspHack 补丁的副本。 |
| 系统字体替换 | 直接替换系统字体；文件被占用时放入重启队列，不强制关闭进程。 |
| 自动备份 | 替换前备份字体文件与其安全描述符，可完整还原。 |
| 权限还原 | 把字体的所有者设为 TrustedInstaller，并把 DACL 重置为纯继承。 |
| 重启后自检 | 替换后下次登录会自动清理字体缓存并检查权限，发现问题可一键从备份还原。 |

## 安装与运行

免安装版：在 [Releases](https://github.com/xRetia/FontHandler/releases) 下载 `FontHandler.exe`，直接运行即可。随包附带 `SHA256SUMS.txt` 用于校验完整性。

源码运行需要 **Windows 7 SP1 或更高版本**（推荐 Windows 10 1709+ / Windows 11），以及 Python 3.10 或更高版本。在低于 Windows 10 1709 的系统上运行时，程序会弹出风险警告，确认后仍可使用。

```bash
pip install -r requirements.txt
python run.py
```

## 替换后的流程

替换完成后重启电脑。重启队列中的字体会在启动时生效，登录后 FontHandler 会自动运行一次，清理字体缓存并校验字体权限。

如果登录界面出现方框、错位或无法进入桌面，说明字体文件的所有者不是 TrustedInstaller。这时正常登录后打开 FontHandler，在弹出的提示里点「从备份还原」即可恢复。

## 数据位置

程序数据保存在 `%LOCALAPPDATA%\FontHandler`：

| 目录 | 内容 |
|---|---|
| `gasp/` | 生成好的补丁字体 |
| `backups/` | 替换前的备份 |
| `userdata/` | 配置 |
| `crash.log` | 崩溃记录 |

## 测试

```bash
python selftest.py
```