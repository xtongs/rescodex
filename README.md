# rescodex

macOS 本地看门狗:Codex 任务因额度耗尽(`usage_limit_exceeded`)中断后,等实时额度恢复,自动用 `codex exec resume` 续跑原任务。无界面、无通知、零依赖(仅 Python 标准库),CLI 和 Codex Desktop 会话都覆盖(两者共用 `~/.codex/sessions/`)。

灵感来自 [joejoeha/codex-quota-resume](https://github.com/joejoeha/codex-quota-resume),去掉了 GUI、托盘、后续任务弹窗和更新器,只保留核心循环。

## 原理

每 60 秒由 launchd 拉起一次 `watcher.py`:

1. **发现** — 扫描 `~/.codex/sessions/**/*.jsonl`:跟踪每个会话"打开的回合",`task_complete` 且 `error.codex_error_info == "usage_limit_exceeded"` 判定为额度中断;正常完成、取消(`turn_aborted`)或已被手动续开的回合都不算。按 `(mtime, size)` 缓存解析结果,未变化的文件不重读。
2. **确认额度** — 通过 `codex app-server --stdio` 调 `account/rateLimits/read` 查实时额度(`ordinaryUsageAllowed`);该实验接口不可用时回退用日志里 `rate_limits.*.resets_at` + 120 秒缓冲。
3. **续跑** — `codex exec resume --skip-git-repo-check --json <会话UUID> "<续跑提示>"`,工作目录用原会话的;并通过 `-c` 回放中断回合记录的 `model` 与 `approval_policy`(来自会话日志的 `turn_context`),使续跑回合保持原会话的模型与审批设置;沙箱沿用当前 `~/.codex/config.toml`。若桌面端占用会话(active writer)则回退 `codex queue`(由桌面端进程按其自身运行时执行)。

防重复与崩溃安全:

- 文件锁(`flock`)保证同一时刻只有一个监控实例。
- `state.json` 以 `会话|回合` 为 key **先落盘再派发**;派发失败回滚并退避 300 秒。
- 派发前用 `pgrep` 检查是否已有 codex 进程在处理该会话(手动接管不抢)。
- 派发后 10 分钟会话仍无动静且无存活进程 → 回滚重试;入队模式则放弃且不重复入队。
- 只处理**监控启用之后**发生的额度中断,不复活历史任务(首次运行时间记在 `monitoringSince`)。

## 安装 / 卸载

```bash
./install.sh     # 自检 → 写入并加载 LaunchAgent(每 60 秒)
./uninstall.sh   # 停止并移除,保留本地状态
```

日常查看:

```bash
python3 watcher.py --status    # 状态 + 当前候选
tail ~/Library/Application\ Support/codex-quota-resume/watcher.log
tail ~/Library/Application\ Support/codex-quota-resume/launchd.err.log  # 崩溃堆栈,正常为空
python3 watcher.py --dry-run   # 只报告将要做什么,不发送
python3 watcher.py --self-test # 内置夹具
python3 -m unittest test_watcher
```

追查问题看三处:`watcher.log` 记录每次决策(状态变化、派发/失败/恢复,长等待时每 10 分钟一条存活线);`state.json` 的 `lastCheckedAt` 是每分钟心跳、`sent` 是发送历史;`launchd.err.log` 只在进程异常退出时有内容。

## 边界

- 监控查询不消耗额度;真实续跑正常消耗 Codex 额度。
- 不读取/上传登录凭据,不兑换重置券,不购买额度,不改沙箱设置;`model` 与 `approval_policy` 按中断会话的 `turn_context` 记录原样回放,不引入新值。
- 续跑提示词固定为一条"继续完成原任务"的消息,不扩大原任务授权。
- `app-server` 属实验接口,Codex 升级可能改变行为;坏了只是退化到 resets_at 回退(晚几分钟)。
- 自动化测试全部基于临时目录夹具,不向真实任务发送消息。2026-09-18 已在一次真实"额度耗尽 → 重置"周期中完整验证:重置后 1 分钟内检测到额度恢复并自动续跑,回合运行 9 分钟后正常完成,全程无人工介入。
- 电脑需开机、已登录 Codex、网络可用;macOS 睡眠期间不检查,唤醒后下一轮补上。

## 文件

| 文件 | 作用 |
|---|---|
| `watcher.py` | 全部核心逻辑(单文件,标准库) |
| `test_watcher.py` | 状态机夹具测试 |
| `install.sh` / `uninstall.sh` | LaunchAgent 安装/卸载 |

MIT License.
