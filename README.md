# SZU 网络守护（Windows / macOS 通用）

深大校园网断线自动重连工具。最初为 macOS 移植版（参考
[Georgeupup/szu-network-guardian](https://github.com/Georgeupup/szu-network-guardian)），
现已成为单一代码库的跨平台项目：业务逻辑共享，平台差异收敛在适配层，
运行时按系统自动分派（架构详见 `CROSSPLATFORM_PLAN.md`）。

轻量、安静地守护你的校园网连接：驻留在系统托盘 / 菜单栏，定时检测外网连通性，
掉线后自动重新完成校园网认证（深澜 SRun / ePortal），全程无需人工干预。

## 下载使用（推荐）

**方式一：下载 ZIP（适合大多数同学）**

1. 点击本页面绿色的 **Code** 按钮 → **Download ZIP**，解压到任意文件夹
   （或者 `git clone https://github.com/HuangLM03/SZUReconnectingNetWork.git`）
2. 安装 [Python 3.10+](https://www.python.org/downloads/)（保持默认勾选即可，
   会自动带上 tkinter 并加入 PATH）
3. 双击 **`启动.command`**（macOS）或 **`启动.bat`**（Windows）：
   脚本会自动安装依赖并在托盘 / 菜单栏启动守护
4. 首次启动会自动弹出设置窗口，填入校园网账号密码即可

首次运行的常见提示（只出现一次）：

- macOS 若提示「来自身份不明的开发者」：右键 `启动.command` → 「打开」→ 再点「打开」
- Windows 若弹出 SmartScreen「已保护你的电脑」：点「更多信息」→「仍要运行」
- 首次安装依赖需要联网；之后日常使用可勾选「开机自动启动」，不再需要手动运行脚本

**方式二：命令行**

```bash
# 安装依赖（requirements.txt 已按平台环境标记分好）
python3 -m pip install -r requirements.txt

# 启动托盘 / 菜单栏守护（首次会自动弹出设置窗口）
python3 main.py

# 调试：仅检测外网连通性（不做认证）
python3 main.py --detect

# 调试：完整执行一轮「检测 + 重连」
python3 main.py --check
```

> 注意：请只从本仓库下载。这个工具需要你的校园网密码，二道贩子的副本可能被改过。

## 功能

- **双区域认证**：教学 / 办公区（深澜 SRun，`net.szu.edu.cn`）与宿舍区（ePortal），支持自动尝试
- **掉线监控**：可配置间隔（1–1440 分钟）检测外网连通性（微软探测页 + 百度随机 token 双探针）；
  某轮恢复失败后自动进入最多 5 分钟间隔的快速复查，恢复成功再回到常规间隔
- **Wi-Fi 链路自动重连**：断网时若 Wi-Fi 与 SZU_WLAN 的关联也断了（休眠唤醒、信号丢失、
  Wi-Fi 被误关），自动重新加入并等待拿到 IP，再继续门户认证；详见下文「Wi-Fi 重连」
- **强制直连**：检测与认证流量绑定物理网卡直连（源地址绑定 + 接口钉定
  [macOS `IP_BOUND_IF` / Windows `IP_UNICAST_IF`] + 直连 DNS），绕过 VPN / 代理
  TUN 的 fake-IP 与路由变化干扰（推荐开启）
- **托盘 / 菜单栏常驻**：状态一目了然（🟢 正常 / 🟡 检测中 / 🟠 重连中 / 🔴 异常 / ⚪️ 停止），
  重连成功发系统通知（macOS 为 rumps 菜单栏，Windows 为 pystray 托盘）
- **安全凭据**：密码按平台保护 —— macOS 钥匙串 / Windows DPAPI，配置文件不落盘明文
- **开机自启**：macOS 用 LaunchAgent，Windows 用注册表 Run 键，登录后自动开始守护
- **本地日志**：按天写入日志目录（macOS `~/Library/Application Support/SZUNetworkGuardian/logs/`，
  Windows `%LOCALAPPDATA%\SZUNetworkGuardian\logs\`），自动清理 7 天前日志
- **设置即时生效**：设置窗口保存后，监控线程下一轮自动读取新配置，无需重启

## 环境要求

- macOS 13+（在 macOS 15 上开发验证）或 Windows 10+
- Python 3.10+（含 tkinter；python.org 官方安装包 / Anaconda 均可，系统自带的无 tkinter）
- 其余依赖由 `启动` 脚本或 `pip install -r requirements.txt` 自动安装
  （`requirements.txt` 已按平台环境标记分好，在哪个系统装就装哪套）

## 日常使用

1. 启动后点击托盘 / 菜单栏图标 → 「设置…」填写账号、密码、所在区域、检测间隔；
   「Wi-Fi 重连」一栏填 `SZU_WLAN`（留空则不管理 Wi-Fi）
2. 勾选「开机自动启动」后，登录系统即自动开始守护，无需再手动运行启动脚本
3. 掉线时托盘变 🟠，重连成功变 🟢 并收到系统通知；图标始终反映**最近一次
   检测结论**，等待间隙不会把失败状态显示成绿色

## 恢复节奏与状态语义

- **图标 / 颜色 = 健康结论**：🟢 最近一次检测在线；🔴 最近一次检测失败；
  🟡 / 🟠 为检测中 / 重连中的瞬时状态。两次检测之间的等待不是独立状态，
  不会冲掉上一次结论的颜色
- **失败后快速复查**：某轮未恢复时，下一轮最多 5 分钟后就开始（而不是
  等完整间隔）；恢复成功后回到配置的常规间隔
- **链路重建后的稳定等待**：Wi-Fi 刚拿到 IP 时，DNS / 路由 / 门户通路
  可能还要几秒才就绪；恢复重试前先稳定等待 5 秒，且最多重试 2 轮
  （间隔 15 秒），避免链路尚在就绪过程中误报「恢复失败」

## Wi-Fi 重连是如何工作的

守护每轮先做外网检测 + 门户认证；只有确认断网、且设置了 Wi-Fi SSID 时，
才会进入 Wi-Fi 链路恢复：

1. 判断 Wi-Fi 接口上有没有活跃 IPv4（macOS 解析 `ifconfig`；Windows 用
   GetAdaptersAddresses 的 WLAN 接口 —— 都避开了不可靠 / 本地化的 SSID 读数）
2. 若链路已恢复（比如你手动切到了手机热点），不做任何动作、不抢你的网络
3. 确认链路断了：macOS 确保 Wi-Fi 电源开启 → `networksetup -setairportnetwork`
   重新加入 SSID（失败重启一次 Wi-Fi 再试）；Windows 用
   `netsh wlan connect`（使用系统已保存的网络配置文件，失败重试一次）；
   两侧都最多等待 20 秒拿到 IP
4. 链路恢复后**换新的网络客户端**再完整走一轮检测 + 认证
   （Wi-Fi 重连后本机 IP 可能变化，旧直连会话绑定的源地址会失效）

若 SSID 需要密码而系统没有保存，加入会失败并提示：请先手动连接一次该网络。

## 跨平台架构

```
入口 main.py / settings_window.py（tkinter 双平台）
  └─ service.py 共享服务（监控生命周期 / 设置子进程 / 通知 / 日志）
       ├─ macOS：menubar.py（rumps 菜单栏）
       └─ Windows：wintray.py（pystray 托盘）
业务核心（双平台共用）：srun.py / network.py / monitor.py / models.py
平台适配层（sys.platform 分派）：
  direct_network.py  网卡发现 + 接口钉定（ifconfig/scutil ↔ GetAdaptersAddresses）
  storage.py         凭据保护（钥匙串 ↔ DPAPI，password_scheme 自描述）
  startup.py         开机自启（LaunchAgent ↔ 注册表 Run 键）
  wifi.py            Wi-Fi 重连（networksetup ↔ netsh wlan）
```

配置目录：macOS `~/Library/Application Support/SZUNetworkGuardian/`；
Windows `%LOCALAPPDATA%\SZUNetworkGuardian\`（均可用环境变量
`SZU_GUARDIAN_DATA_DIR` 覆盖）。配置里的 `password_scheme` 标记保护方案，
跨系统拷贝配置时另一侧会优雅降级为要求重新输入密码。

## 凭据保护说明

- macOS：存登录钥匙串（服务名 `com.szu.network-guardian`，账户名 `szu-guardian`）；
  密码以 base64(UTF-8) 形式存储 —— `security` CLI 对非 ASCII / 以 `-` 开头的
  密码读取时会返回十六进制甚至报错，base64 化后可完整还原任意密码；
  可用「钥匙串访问」搜索服务名查看 / 删除
- Windows：DPAPI（CryptProtectData）按当前用户加密，密文存于配置文件的
  `password_dpapi` 字段（与上游原版格式兼容，可互相迁移）

## 测试

```bash
python3 -m pytest tests/ -q
```

60+ 用例覆盖：SRun 加密向量与协议交互、双区域认证与掉线重连状态机、
ifconfig/scutil 解析与直连路由偏好、接口钉定（macOS `IP_BOUND_IF` 与
Windows `IP_UNICAST_IF` 的 ntohl 字节序）、钥匙串编码回环与 DPAPI 方案分派、
LaunchAgent 读写与注册表命令构造（pythonw 优先）、监控线程的配置热加载与唤醒、
两个平台的 Wi-Fi 恢复命令序列、过程异常（Errno 49 等）触发的立即恢复重试、
跨系统配置的优雅降级。

## 已知限制

- `--check` / 认证需要身处校园网内（或在已连通的网络环境下）；宿舍区认证地址
  `172.30.255.42` 仅校园网可达
- 系统通知需在系统设置中允许 Python / 终端发送通知；未授权不影响守护本身
- 打包为独立 .app / .exe 未包含在本版内（可直接用自启项 + 脚本路径常驻）
- **Windows 侧代码尚未实机验证**（本项目在 macOS 上开发）：GetAdaptersAddresses
  解析、注册表自启、netsh wlan、pystray 托盘均移植自上游已验证实现或按官方
  文档编写，命令构造与分派逻辑有单元测试覆盖；首次在 Windows 上使用请按
  以下清单冒烟：`pip install -r requirements.txt` → `python main.py --detect` →
  启动托盘并填写设置 → 勾选开机自启 → 断开 Wi-Fi 验证自动重连

## 许可与致谢

SRun 协议实现源自 MIT 许可的
[Sleepstars/SZU-login](https://github.com/Sleepstars/SZU-login) 与
[vidar-team/srun-login](https://github.com/vidar-team/srun-login)；
项目结构参考 MIT 许可的
[ackness/szu-autoconnect](https://github.com/ackness/szu-autoconnect) 与上游
[Georgeupup/szu-network-guardian](https://github.com/Georgeupup/szu-network-guardian)。
详见 `THIRD_PARTY_NOTICES.md`。
