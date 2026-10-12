# 跨平台开发方案（Windows + macOS）

目标：将本项目改造为 Windows 与 macOS 通用的单一代码库，共享全部业务逻辑，
平台差异收敛到可替换的适配层，运行时按 `sys.platform` 自动分派。

## 1. 架构

```
┌──────────────────────────────────────────────────────┐
│ 入口层  main.py / settings_window.py（tkinter，双平台）│
├──────────────────────────────────────────────────────┤
│ 桌面层  service.py 共享服务（监控生命周期、设置子进程、 │
│         通知、日志）                                   │
│   ├─ menubar.py  macOS：rumps 菜单栏（瘦壳）           │
│   └─ wintray.py  Windows：pystray 托盘（瘦壳）        │
├──────────────────────────────────────────────────────┤
│ 业务核心（双平台共用，不再改动）                        │
│   srun.py / network.py / monitor.py / models.py      │
├──────────────────────────────────────────────────────┤
│ 平台适配层（运行时分派 sys.platform）                  │
│   direct_network.py  直连网卡发现 + 接口钉定           │
│   storage.py         凭据保护（Keychain / DPAPI）      │
│   startup.py         开机自启（LaunchAgent / 注册表）  │
│   wifi.py            Wi-Fi 重连（networksetup / netsh）│
│   local_log.py       日志目录打开（open / os.startfile）│
└──────────────────────────────────────────────────────┘
```

包名 `szu_guardian_mac` → `szu_guardian`（不再绑定平台）。

## 2. 平台差异矩阵与分派方式

| 能力 | macOS 实现（现状保留） | Windows 实现（新增） |
| --- | --- | --- |
| 网卡枚举 | `ifconfig -a` + `scutil --dns` 解析 | `GetAdaptersAddresses` ctypes（移植自上游原版，含 FriendlyName/IfIndex/IfType） |
| socket 钉定接口 | `IP_BOUND_IF`(=25) + `if_nametoindex` | `IP_UNICAST_IF`(=31)，值需 `socket.ntohl(if_index)`（网络字节序，Windows 惯用坑） |
| 凭据保护 | 钥匙串（`security` CLI，base64 存取） | DPAPI `CryptProtectData`（移植自上游原版） |
| 开机自启 | `~/Library/LaunchAgents/` plist + `launchctl` | HKCU `Software\...\Run` 注册表键（`pythonw.exe` 优先，避免控制台窗口） |
| 托盘 | rumps 菜单栏 | pystray + PIL 动态图标 |
| Wi-Fi 重连 | `networksetup -setairportnetwork`（可断电重启） | `netsh wlan connect name=<ssid>`（用已保存配置；电源切换需管理员，不做） |
| Wi-Fi 状态判断 | ifconfig 有无 IPv4（避开不可靠的 SSID 读数） | GetAdaptersAddresses 的 WLAN(IfType=71) 接口有无 IPv4（避开 netsh 本地化输出） |
| 打开日志目录 | `open` | `os.startfile` |
| 高分屏 | 无需处理 | `SetProcessDpiAwareness`（tkinter 需要） |

## 3. 模块改造清单

1. **service.py（新）**：从 menubar.py 抽出 `GuardianService` —— 监控启停/立即检测、
   设置子进程管理与「保存后生效」逻辑、事件队列（含本地日志写入与恢复通知）、
   自启开关、初始启动策略。前端（menubar/wintray）只做 UI 映射与轮询。
2. **storage.py**：`KeychainBackend`（现有）+ `DpapiBackend`（移植）。
   配置格式：写入 `password_scheme`（`keychain`/`dpapi`）与方案各自字段；
   读取兼容旧版 mac 配置（`password_keychain: true` 且无 scheme → 按钥匙串）。
   DPAPI blob 存 `password_dpapi`（base64，同上游原版格式，方便迁移）。
3. **direct_network.py**：`discover_adapter_configs()` 平台分派；
   `AdapterDnsConfig` 扩展 `if_index` / `is_wlan` / `interface`；
   `_bound_socket_options()` 与 DNS 查询的接口钉定按平台选择 socket 选项。
   mac 路径行为不变（现有 51 个测试继续覆盖）。
4. **wifi.py**：`recover_wifi` 内部按平台分派 `_recover_mac` / `_recover_win`。
   Windows：WLAN 接口名取自 GetAdaptersAddresses（IfType=71，免本地化），
   动作用 `netsh wlan connect`，失败重试一次后提示先在系统保存网络配置。
5. **startup.py**：`set_autostart` / `is_autostart_enabled` 分派；
   Windows 分支注册表实现（`winreg` 延迟导入，仅 win32）。
6. **wintray.py（新）**：pystray 托盘，菜单项文本用 callable 动态反映状态
   （🟢/🟡/🟠/🔴/⚪️），轮询线程 0.5s 刷新；通知用 `icon.notify`。
7. **menubar.py**：瘦身为 rumps 壳，逻辑全部委托 GuardianService。
8. **入口与设置窗口**：main.py 移除 darwin-only 守卫、Windows DPI 感知、
   桌面层分派；settings_window.py 字体映射
   （PingFang SC ↔ Microsoft YaHei UI）与凭据提示文案按平台。
9. **依赖**：`requirements.txt` 环境标记 ——
   `rumps; sys_platform == "darwin"`、`pystray`+`Pillow; sys_platform == "win32"`。

## 4. 测试策略

- 双平台纯逻辑（srun/monitor/models/network/storage 编解码）继续全平台跑。
- mac 适配层：现有 51 个用例保留（改名后全部通过）。
- 新增：DPAPI 后端（monkeypatch ctypes 层）、配置 scheme 分派与旧格式兼容、
  `IP_UNICAST_IF` 选项构造（显式平台参数）、Windows Wi-Fi 命令序列（注入假 runner）。
- Windows 独有真实系统调用（GetAdaptersAddresses 解析、注册表、pystray）：
  结构上可注入/mock 的尽量测；无法在 mac 上验证的部分在 README 标注
  「需在 Windows 实机冒烟」。

## 5. 风险与验证

- **Windows 侧无法在本机实测**：ctypes/注册表/netsh/pystray 代码移植自上游
  已验证实现或按官方文档编写，单元测试覆盖命令构造与分派逻辑，
  但首次在 Windows 运行时需按 README 的冒烟清单过一遍。
- `IP_UNICAST_IF` 字节序是历史性坑，测试固定断言 `ntohl` 结果。
- 包重命名后，正在运行的旧实例与其拉起的设置子进程需要重启才能引用新包名，
  收尾时统一重启菜单栏应用。

## 6. 实施顺序

方案文档 → 包重命名 → service 抽取与 menubar 瘦身（mac 回归）→
storage → direct_network → wifi → startup → wintray → 入口/设置/依赖 →
全量测试 + mac 冒烟 + 重启应用 + README/记忆更新。
