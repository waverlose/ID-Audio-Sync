# ID Audio Sync

> 局域网跨机 **系统声音同步**。一台电脑出声，另一台电脑同步听到。
> 纯托盘、无界面、开机自启、两台机器自动配对 —— 不需要填 IP，也不依赖任何第三方软件。

---

## 这是什么

用 KVM / 键鼠共享软件（Input Director、Synergy、微软无界鼠标等）把两台电脑拼成一套工作站时，**键盘鼠标过去了，声音过不去**。
这个工具补上最后一块：把 A 机的系统声音（音乐、视频、游戏、任何 App）实时推到 B 机播放，走局域网，延迟约 20~80 ms。

- **发送方**：这台机器出声（抓取系统输出）
- **接收方**：这台机器听声（播放远端声音）

---

## 特性

| 特性 | 说明 |
| --- | --- |
| 自动配对 | UDP 广播互相发现，**不用填 IP**；配对结果会记住，下次开机秒连 |
| 角色自动仲裁 | `auto` 模式下两台机器自己商量谁发谁收，不会出现"两边都在听"或"两边都在发" |
| 纯托盘 | 无主窗口，只有托盘图标；图标颜色直接反映状态 |
| 单文件安装 | 一个 exe 双击即装：复制到安装目录、加自启、放行防火墙、拉起托盘 |
| 抓取系统声音 | WASAPI loopback，**不需要虚拟声卡**，也不需要立体声混音 |
| 静音门限 | 没声音时只发心跳，空闲带宽从 ~1.5 Mbps 降到几十 kbps |
| 可配静音策略 | 接收方可随时静音/调音量；发送方可选"发送时静音本机扬声器" |
| 单实例 | 重复双击不会起第二个进程 |
| 自愈 | 角色线程意外退出会自动重启；网络断开自动重连 |

---

## 快速开始

1. 在**两台**电脑上都双击 `IDAudioSync.exe`（首次会弹 UAC，允许即可）。
2. 装好后托盘出现一个喇叭图标，**不需要做任何配置**。
3. 自动模式下：先启动的那台会当接收方，后启动的那台发现它之后自动当发送方。
4. 在发送方那台播放任何声音，接收方那台就应该能听到。

> 首次安装会请求管理员权限，用于写入 `Program Files`、注册开机自启、添加防火墙入站规则。

### 图标颜色含义

| 颜色 | 含义 |
| --- | --- |
| 🟢 绿色 | 接收方，正在播放远端声音 |
| 🟠 橙色 | 已就绪但还没连上（等待配对 / 正在搜索） |
| 🔵 蓝色 | 发送方，已连上接收方 |
| ⚪ 灰色 | 已关闭 |

---

## 托盘菜单

```
状态行（实时显示当前在做什么，不可点）
────────────────────────
角色            ├ 自动配对（推荐）
                ├ 发送方：我这台出声
                ├ 接收方：我这台听声
                └ 关闭
交换角色
────────────────────────
静音（接收方）
接收音量         ├ 100% / 70% / 40% / 15%
发送时静音本机扬声器
────────────────────────
重新搜索对端
播放测试音（检查扬声器）
诊断信息
登录时自动启动
────────────────────────
查看日志
编辑配置文件
────────────────────────
卸载并退出
退出
```

几个说明：

- **发送时静音本机扬声器**：默认关闭。开启后，发送方在本机就听不到声音了（声音"搬"到接收方），适合只想在接收方听歌的场景；想两台同时响就保持关闭。
- **接收音量 / 静音**：随时可调，不影响发送方。
- **诊断信息**：弹窗显示本机名、本机 IP、安装目录、端口、当前状态、输出设备、日志路径 —— 排查问题先看这里。

---

## 命令行（一般用不到）

```
IDAudioSync.exe                  安装（首次）并常驻托盘
IDAudioSync.exe --install        只安装，不启动
IDAudioSync.exe --uninstall      卸载（清自启 / 防火墙 / 文件）
IDAudioSync.exe --dry-run        只探测环境并打印，不安装不启动
IDAudioSync.exe --no-install     跳过安装，直接跑托盘（源码调试用）
IDAudioSync.exe receiver         直接以接收方运行（排错）
IDAudioSync.exe sender           直接以发送方运行（排错）
IDAudioSync.exe tone-test        播放 1.5 秒测试音
IDAudioSync.exe list-devices     列出本机音频设备
```

可选参数：`--peer <ip>` 直接指定对端（跳过自动搜索）、`--port`、`--samplerate`、`--channels`、
`--block-ms`、`--no-gate`、`--gate-threshold`、`--token`（共享令牌）、`--seconds N`（跑 N 秒后退出）、`--log-file`。

---

## 下载

不想自己编译的话，直接到 [Releases](https://github.com/waverlose/ID-Audio-Sync/releases/latest) 下载 `IDAudioSync.exe`（单文件，约 31 MB）。

---

## 从源码构建

需要 Windows + Python 3.9 以上。

```bat
git clone https://github.com/waverlose/ID-Audio-Sync.git
cd ID-Audio-Sync
build\build.bat
```

产物在 `dist\IDAudioSync.exe`（单文件，约 31 MB）。

手动构建：

```bat
pip install -r requirements.txt
pyinstaller --clean --noconfirm build\idaudiosync.spec
```

直接跑源码调试：

```bat
python src\idaudiosync.py --no-install --log-file debug.log   :: 前台跑托盘，日志同时写文件
python tools\probe_devices.py                                 :: 列出音频设备并做抓取自检
```

---

## 目录结构

```
id-audio-sync/
├── src/
│   └── idaudiosync.py      # 全部逻辑：抓取/推流/配对/托盘/安装器
├── tools/
│   ├── probe_devices.py            # 列出扬声器/loopback/麦克风并试抓 0.3 秒
│   ├── play_music.py               # 合成一段音乐循环播放（测试用）
│   └── start_stream_and_music.py   # 后台起推流 + 放音乐（一键演示）
├── build/
│   ├── idaudiosync.spec    # PyInstaller 配置（单文件 / 无控制台）
│   └── build.bat           # 一键构建
├── docs/
│   └── PROTOCOL.md         # UDP 配对 + TCP 音频流协议说明
├── dist/                   # 构建产物（已 gitignore）
├── requirements.txt
├── LICENSE                 # MIT
└── README.md
```

---

## 运行数据放在哪

| 内容 | 默认位置 |
| --- | --- |
| 程序本体 | `C:\Program Files\ID Audio Sync\IDAudioSync.exe` |
| 配置 / 日志 | `%LOCALAPPDATA%\ID Audio Sync\`（`config.json`、`idaudiosync.log`） |

**想完全不落 C 盘**（绿色便携 / 装到 D 盘），先设两个环境变量再运行：

```bat
set IDAS_INSTALL_DIR=D:\Apps\ID Audio Sync
set IDAS_DATA_DIR=D:\Apps\ID Audio Sync\data
IDAudioSync.exe --install
```

---

## 常见问题

**Q：接收方听不到声音？**
1. 看接收方托盘图标颜色：橙色 = 还没连上，绿色 = 已连上在播。
2. 菜单 →「诊断信息」，确认两台的 IP 在同一网段、端口没被占用。
3. 确认发送方那台**确实在出声**（有些播放器走独占模式，loopback 抓不到）。
4. 检查两台电脑的防火墙是否放行了 TCP 31235 / UDP 31236。
5. 看日志：菜单 →「查看日志」，正常会看到"发现接收方 …，本机自动作为发送方"和"发送方已连接"。

**Q：两台都想当接收方怎么办？**
自动模式下会用主机名做确定性仲裁，名字小的那台自动改当发送方。也可以在托盘里手动指定角色。

**Q：有延迟吗？**
局域网内约 20~80 ms，听音乐、看视频完全够用；不适合对同步精度要求极高的场景（如专业音频对齐）。

**Q：能跨网段 / 跨公网吗？**
广播配对只在同一广播域有效。跨网段可在托盘手动指定 `--peer`，或改 `config.json` 里的 `peer` 字段；公网需要自己做端口转发，且**没有加密**，不建议直接暴露。

**Q：和 Input Director 有关系吗？**
没有。这是个完全独立的外部程序，Input Director 本身没有插件接口，所以做成伴侣程序而非插件。

---

## 已知限制

- 仅 Windows（依赖 WASAPI loopback）。
- 音频**不加密**，仅适合可信局域网。
- 单声道/多声道设备会统一按立体声 48 kHz 传输。
- 采样率固定 48 kHz，不自动跟随源设备。

---

## 许可

[MIT](LICENSE)
