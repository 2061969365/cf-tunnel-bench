# cf-tunnel-bench

测「外部客户端经 Cloudflare Tunnel 从本机下载」的端到端吞吐。只用 Python 3 标准库，
设备侧（Windows）和 GitHub Actions（Linux）跑的是**同一份客户端代码**。

答案不是「隧道有多快」，而是「这条完整链路能给外部客户端多少 Mbps」：

```
客户端(GitHub runner / VPS) → Cloudflare 边缘(colo) → 你的上行 → cloudflared → 本机 origin
```

结果恒等于最慢的一段。多数情况下瓶颈是**家宽/办公网上行**，其次是 `cloudflared` 的
单连接性能，最后才是 origin 服务器。所以单看一个数字没有意义，必须同时测对照项。

## 指标定义

| 代号 | 谁 → 谁 | 命令 | 作用 |
|---|---|---|---|
| **A** | 本机 → Cloudflare 边缘（上传） | `--edge-up` | 隧道上行段的硬天花板 |
| **B** | runner → Cloudflare 边缘（下载） | `--edge-down` | 排除「runner 自己跑不动」 |
| **C** | runner → 隧道 → 本机（下载） | `--url` | **主指标** |
| **D** | 本机 → 127.0.0.1 origin | `--url http://127.0.0.1:PORT/...` | 排除 origin 成为瓶颈 |

判读：

- `C ≈ A`：瓶颈是上行，隧道没问题；想更快只能升带宽。
- `C < A`，且 `B`、`D` 都还有余量：瓶颈在隧道（协议 / 路径 / 单连接 BDP）。
- 单流低、8 并发明显更高：典型单 TCP 连接受 BDP 限制，不是带宽不够。
- `D < 3 × A`：本次数据无效，先修 origin（换 Caddy 或加大 chunk）。

## 快速开始（设备侧）

```powershell
# 1. 起 origin + 一个独立的 quick tunnel（不碰现有 Moonlight 隧道）
.\start-bench.ps1
# 输出类似：
#   public : https://xxxx-yyyy.trycloudflare.com
#   bench  : https://xxxx-yyyy.trycloudflare.com/<token>/stream

# 2. 本机先自测 origin 天花板（D）
python bench.py --url "http://127.0.0.1:18080/<token>/stream" --seconds 10 --streams 1,4 --reps 1

# 3. 本机走隧道量一轮（含 A/C，C 在这里是回环，只用于验证链路通）
python bench.py --edge-up --upload-bytes 10000000 --reps 2
python bench.py --url "https://xxxx-yyyy.trycloudflare.com/<token>/stream" --seconds 10 --streams 1,4 --reps 1

# 4. 收工
.\stop-bench.ps1
```

`start-bench.ps1` 只会启动并记录自己的进程（`.bench\*.pid`），`stop-bench.ps1` 只杀
名字匹配 `python` / `cloudflared` 且 PID 对得上的进程，不会动生产隧道。

## 快速开始（GitHub Actions）

```bash
gh repo create cf-tunnel-bench --public --source . --push
gh secret set BENCH_URL --body "https://<host>/<token>/stream"
gh workflow run bench.yml
gh run watch
```

跑完看 run 的 Summary：包含 runner 的 ASN/colo、控制项 B、主指标 C 的表格
（1/4/8 并发 × 3 轮，p50/max/min/CV），完整样本在 artifact 里。

参数（`workflow_dispatch`）：`url` 覆盖 secret、`seconds`（默认 20）、
`streams`（默认 `1,4,8`）、`reps`（默认 3）。单次约 3–5 分钟。

## 两种接入方式

1. **Quick Tunnel**（默认，`.\start-bench.ps1`）
   `cloudflared tunnel --url http://127.0.0.1:PORT`，得到 `*.trycloudflare.com`。
   没有固定域名、每次重启都变，适合手动 dispatch。
2. **专用命名隧道**（可选，便于定时）
   `.\start-bench.ps1 -Mode Named -Hostname bench.fastfaster.kdns.fr`
   需要该 zone 的 DNS 在你 Cloudflare 账号下；会用 `~/.cloudflared/cert.pem`
   建独立隧道 `cf-tunnel-bench` 并写 `.bench\named-config.yml`。
   固定 URL 后把 `BENCH_URL` secret 换成它，再打开 `.github/workflows/bench.yml`
   里的 `schedule`。

> 不要给现有的 `moonlight.fastfaster.kdns.fr` 那条隧道加 ingress（要重启它），
> 也不要拿同一个 tunnel ID 再跑第二个 connector：ingress 是 per-connector 的，
> hostname 会随机落到错的 connector 上返回 404。

## 协议 A/B

`start-bench.ps1 -Protocol http2` 与 `-Protocol quic` 各跑一轮同一组参数即可。
不要预设 QUIC 更快：2026.10 有实测 `cloudflared` 走 QUIC 的下载只有 HTTP/2 的
50–60%（[社区帖](https://community.cloudflare.com/t/cloudflared-over-quic-downloads-around-50-60-of-http-2-speed/968761)）。
生产配置当前是 `protocol: http2`，很可能就是因为它在这类网络下更快。

## 已核实的实测基线（2026-11-25，本机，直接连接）

| 项 | 结果 | 备注 |
|---|---|---|
| D origin 回环 | 27 Gbps 单流 / 43 Gbps 4 流 | Python 版 origin 不是瓶颈 |
| A 本机 → CF 边缘上传 | **6.1 Mbps**（LAX） | 25 MB POST |
| 本机 → CF 边缘下载 | **3.2 Mbps** 单流（LAX） | 4/8 并发未跑完，受限于链路 |
| `__down` 上限 | `bytes=25000000` 可用，`1e8` 起返回 **403** | 2026-11 实测，故控制项用 25 MB |

也就是说：在这台机器当前的国际出口上，隧道下载的天花板大约就是 6 Mbps 量级，
C 跑出来不会更高。换网络/换路径后再对比。

## 已知限制

- GitHub hosted runner 全在境外 Azure，**测的是境外用户体验**。要国内视角，把
  `bench.py` 原样丢到国内 VPS 跑 `--url`，参数保持一致即可。
- 标准 runner 不能选区域，只能多跑几次覆盖不同落点、不同 colo（报告里记了 cf-ray）。
- 单次传输约 1.5–2 GB 上行（50 Mbps × 20 s × 9 样本 + 对照）。免费 Tunnel 不适合
  持续大文件分发，别当 CDN 压；一天 ≤ 4 次足够。
- 若机器设置了 `HTTP(S)_PROXY`：`bench.py` 用 `http.client`，**不走代理**（会在
  stderr 提示），curl 则走代理。两者数字不可混着比。
- Windows 上 origin 用 Python，实测回环 27 Gbps+；若换到慢机器或大并发，
  先看 D。
- `cf-cache-status: HIT` 的样本判无效（说明测的是边缘缓存而不是你的设备）；
  origin 已设 `no-store`，请求也带 nonce。

## 文件

| 文件 | 说明 |
|---|---|
| `bench.py` | 客户端：多流、定时长、稳态剔除（默认丢掉前 3 s）、JSON + Markdown |
| `bench_origin.py` | origin：1 MiB 随机块无限流（chunked），token 路径，只默认绑回环 |
| `start-bench.ps1` / `stop-bench.ps1` | 设备侧起停（quick / named） |
| `.github/workflows/bench.yml` | 外部测量：控制项 B + 主指标 C + Summary + artifact |
| `out/`、`.bench/` | 运行产物，已 gitignore |
