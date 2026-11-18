# SZU 网络守护（Windows / macOS 通用）

深大校园网断线自动重连工具。单一代码库支持 macOS 与 Windows：业务逻辑共享，
平台差异收敛在适配层，运行时按系统自动分派（架构详见 `CROSSPLATFORM_PLAN.md`）。

轻量、安静地守护你的校园网连接：驻留在系统托盘 / 菜单栏，检测公共互联网，
掉线后按需重新完成校园网认证（深澜 SRun / ePortal）。**Wi-Fi 有 IP、校内网页
可达或门户返回认证成功，都不会单独被当成互联网恢复。**

## 下载使用（推荐）

### 方式一：下载 ZIP

1. 点击本页面绿色的 **Code** → **Download ZIP**，解压到任意文件夹
   （或者 `git clone https://github.com/HuangLM03/SZUReconnectingNetWork.git`）
2. 安装 [Python 3.10+](https://www.python.org/downloads/)（保持默认勾选即可，
   会自动带上 tkinter 并加入 PATH）
3. 双击 **`启动.command`**（macOS）或 **`启动.bat`**（Windows）：
   脚本会自动安装依赖并在托盘 / 菜单栏启动守护
4. 首次启动会自动弹出设置窗口，填入校园网账号密码即可

首次运行的常见提示（只出现一次）：

- macOS 若提示「来自身份不明的开发者」：右键 `启动.command` → 「打开」→ 再点「打开」
- Windows 若弹出 SmartScreen「已保护你的电脑」：点「更多信息」→「仍要运行」
- 首次安装依赖需要联网；之后日常使用可勾选「开机自动启动」

> 注意：请只从本仓库下载。这个工具需要你的校园网密码，二道贩子的副本可能被改过。

### 方式二：命令行

```bash
python3 -m pip install -r requirements.txt

# 启动托盘 / 菜单栏守护（首次会自动弹出设置窗口）
python3 main.py

# 仅检测，不做认证：输出整体、物理直连和系统应用路径的逐探针诊断
python3 main.py --detect

# 完整执行一轮「检测 + 按需认证 + 公网复查」
python3 main.py --check
```

`--detect`、`--check` 和后台监控使用同一套传输初始化与互联网判定。
调试入口不会再绕过设置中的强制直连。`--detect` 不会提交账号或密码。

## 功能

- **双区域认证**：教学 / 办公区（深澜 SRun，`net.szu.edu.cn`）与宿舍区
  （ePortal），支持自动尝试；仅门户不可用时才尝试另一区域，明确拒绝不盲目重试
- **严格公网检测**：使用 Apple 固定成功页、百度首页和淘宝首页三个 HTTPS 探针；
  至少两个独立目标通过原主机 TLS、来源和响应内容校验才判定在线
- **双路径诊断**：分别检测绑定物理网卡的直连路径，以及应用实际使用的
  系统路由 / 代理 / TUN 路径，不再把某一个路径的可达性冒充设备整体恢复
- **强制直连**：macOS `IP_BOUND_IF` / Windows `IP_UNICAST_IF` 加源地址绑定；
  直连 DNS 只决定 TCP 目标，HTTPS 始终保留原域名的 SNI 和证书校验
- **Wi-Fi 链路自动重连**：接口没有 IPv4 时重新加入配置的 SSID；有 IP 不抢网络。
  重连或接口重置后，仍必须重新通过公网验证才可能变绿
- **托盘 / 菜单栏常驻**：🟢 最近验证在线 / 🟡 检测中或受限 / 🟠 恢复中 /
  🔴 离线 / ⚪️ 停止。菜单显示上次健康结论的时间与延迟
- **安全凭据**：macOS 钥匙串 / Windows DPAPI，配置文件不落盘明文密码
- **开机自启**：macOS LaunchAgent，Windows 注册表 Run 键
- **本地日志**：逐探针、逐路径、接口、源 IP、目标 IP 和认证客户端 IP 诊断；
  不记录密码、challenge 或加密认证数据；按天保存并清理 7 天前日志
- **设置即时生效**：保存后由新监控代次接管，不沿用旧线程的停止事件或状态

## 环境要求

- macOS 13+（在 macOS 15 上开发验证）或 Windows 10+
- Python 3.10+（含 tkinter；python.org 官方安装包 / Anaconda 均可）
- requests 2.32+、urllib3 2.2+；其余依赖由启动脚本或 requirements 自动安装

## 状态语义：什么才算“互联网恢复”

以下四件事彼此独立，不能互相替代：

1. **链路**：Wi-Fi 已关联、接口获得 IPv4
2. **认证**：校园门户明确接受本次认证请求
3. **物理直连公网**：绑定物理接口的至少两个 HTTPS 探针严格通过
4. **系统应用公网**：系统代理 / TUN 路径的至少两个 HTTPS 探针严格通过

启用强制直连时，只有第 3、4 项都为在线，整体才是 🟢。
关闭强制直连时，只按系统应用路径判断整体是否在线。

- **仅校内网 / 认证成功**：不会显示为互联网恢复
- **直连通、系统代理 / TUN 不通**：🟡 受限，提示检查代理/TUN，不重复认证或重置 Wi-Fi
- **系统路径通、物理直连不通**：🟡 受限，不宣称校园物理链路恢复
- **只有一个公网探针通过**：🟡 受限，证据不足以宣称完全恢复
- **全部收到拦截/错误响应**：🔴 离线
- **全部超时或无法取得可信证据**：🔴 状态未知，仍不显示绿色

等待期间颜色表示**上次结论**，不是实时保证；菜单会显示该结论的实际检测时间。
可点击「立即检测」主动刷新。恢复通知只在本轮或前一轮明确失败后、
新一轮严格验证整体在线时发送。

### 探针为什么不再使用旧实现

旧版使用明文微软 NCSI 和“百度页面回显随机 token”判断成功，直连时还把
HTTPS 主机改写成 IP 并关闭证书校验。这些逻辑可能把门户拦截、错误 DNS、
重定向或回显页面误认为公网成功，已完整删除。

新探针要求：原 HTTPS 主机、默认 CA 和证书主机名校验、禁止重定向、限定状态码、
内容类型与正文语义；公共连接拒绝私网、回环、fake-IP 等目标。
探针通过证明检测当时有严格验证的公共互联网通路，不承诺任意外网站点永远可用。

## 恢复节奏与僵局升级

- 初次检测失败才按需认证；校园会话失效不会跳过独立的互联网检测
- 认证已被接受且链路没有重建时，只重新验证互联网，不重复登录
- 链路实际重建后才创建新客户端并按需重新认证；恢复重试前先稳定等待 5 秒
- 普通失败最多 2 轮恢复重试（间隔 15 秒），下一轮最多 5 分钟后复查
- 账号被门户明确拒绝时退避至少 30 分钟，不重置 Wi-Fi，避免机械提交凭据
- 连续 6 轮「认证已接受但公共互联网仍阻断」且配置了受管 Wi-Fi 时，
  自动重置接口以重建 DHCP / 网关绑定；未配置 SSID、系统路径已在线或账号被拒时不重置
- **接口重置只是动作，不是结果**：必须观察到旧 IP 离开并重新获得 IP，
  随后立即重新认证/检测；公网仍失败就保持离线

## Wi-Fi 重连是如何工作的

只有互联网未通过验证、且设置了 Wi-Fi SSID 时，守护才会进入链路恢复：

1. 检查 Wi-Fi 接口是否有活跃 IPv4（macOS `ifconfig`；Windows GetAdaptersAddresses）
2. 已有 IP 时不重连、不切换你手动选择的热点；没有 IP 才尝试加入配置的网络
3. macOS 使用 `networksetup -setairportnetwork`，必要时电源循环；
   Windows 使用系统已保存配置文件的 `netsh wlan connect`
4. 等待重新获得 IPv4 后立即重新进行严格公网检测

SSID 需要密码而系统没有保存时，加入会失败；请先手动连接一次该网络。
SSID 读数只作辅助诊断，不再把“有 IP”写成“已确认加入指定 SSID”。

## 跨平台架构

```
入口 main.py / settings_window.py（tkinter 双平台）
  └─ service.py 共享服务（监控生命周期 / 设置子进程 / 通知 / 日志）
       ├─ macOS：menubar.py（rumps）
       └─ Windows：wintray.py（pystray）
业务核心：connectivity.py / network.py / srun.py / monitor.py / models.py
平台适配层（sys.platform 分派）：
  direct_network.py  网卡、严格 DNS、接口钉定、保留 TLS 主机名的直连连接池
  storage.py         凭据保护（钥匙串 ↔ DPAPI）
  startup.py         开机自启（LaunchAgent ↔ 注册表 Run 键）
  wifi.py            Wi-Fi 链路恢复（networksetup ↔ netsh wlan）
```

配置目录：macOS `~/Library/Application Support/SZUNetworkGuardian/`；
Windows `%LOCALAPPDATA%\SZUNetworkGuardian\`（可用 `SZU_GUARDIAN_DATA_DIR` 覆盖）。
配置里的 `password_scheme` 标记保护方案，跨系统拷贝配置时会要求重新输入密码。

## 凭据保护说明

- macOS：登录钥匙串（服务名 `com.szu.network-guardian`，账户名 `szu-guardian`）；
  密码以 base64(UTF-8) 存储，以兼容任意 Unicode 和前导 `-` 密码
- Windows：DPAPI（CryptProtectData）按当前用户加密，密文存于 `password_dpapi` 字段
- ePortal 协议地址本身为 HTTP，提交凭据仅限明确的校园门户地址；公共互联网
  探针与校园门户使用完全隔离的连接池，公共探针绝不复用门户的安全策略

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：SRun 加密向量与一致成功字段、ePortal 明确结果字段、双路径健康聚合、
HTTPS 证书/来源/正文校验、门户伪造与 token 回显反例、跨域/相对重定向、
公网私地址拒绝、DNS 问题与 CNAME 答案归属、首次连接的直连路由注入、
接口钉定、Wi-Fi 旧 IP 离开与新 IP 获取、恢复证据合并、僵局后立即复查、
停止/重启代次、异常死亡清状态和恢复通知。

真实只读验证可使用 `python3 main.py --detect`。不会用假凭据向真实门户冒烟，
以免触发账号锁定。

## 已知限制

- 认证需要身处校园网内；宿舍区 `172.30.255.42` 仅校园网可达
- 系统通知需在系统设置中允许 Python / 终端发送通知
- 尚未提供独立 .app / .exe 安装包
- **Windows 侧尚未实机验证**：GetAdaptersAddresses、注册表、netsh、pystray
  有单元测试覆盖，但首次在 Windows 使用请按顺序冒烟：安装依赖 → `--detect` →
  启动托盘并填写设置 → 勾选开机自启 → 断开 Wi-Fi 验证恢复
- 公共探针无法证明所有网站和所有应用永久可用；若显示 🟡，请按日志中的
  direct/system 路径诊断检查代理、TUN 或校园网，而不是只看认证是否成功

## 许可与致谢

SRun 协议实现源自 MIT 许可的
[Sleepstars/SZU-login](https://github.com/Sleepstars/SZU-login) 与
[vidar-team/srun-login](https://github.com/vidar-team/srun-login)；
项目结构参考 MIT 许可的 [ackness/szu-autoconnect](https://github.com/ackness/szu-autoconnect)
与上游 [Georgeupup/szu-network-guardian](https://github.com/Georgeupup/szu-network-guardian)。
详见 `THIRD_PARTY_NOTICES.md`。
