# 房间文本中继 · 服务端（Cloudflare Worker + Durable Object）

把每个人的 ASR 源文广播给同一房间里的其他人。**只转文字**：不翻译、不合成语音、
不缓冲、不排序、不落库。一个房间码 = 一个 Durable Object 实例。

> 本目录是**骨架**（批次 1）。客户端在 `vlt/room/`，协议定义见
> `.hermes/plans/2026-09-28_112035-room-relay.md` §4。

---

## 本地跑起来

```bash
cd server
npm install                 # 只装 wrangler（devDependency）
npx wrangler dev --local    # → http://127.0.0.1:8787
```

探活（不是 WebSocket 的请求会拿到一个只读 JSON 状态页）：

```bash
curl http://127.0.0.1:8787/
```

WebSocket 端点：

```
ws://127.0.0.1:8787/ws?room=<8位房间码>[&k=<令牌>]
```

- `room` 必填，8 位 Crockford Base32（不含易混的 `I/L/O/U`；填错了会自动把
  `I/L→1`、`O→0` 归一，两端同一套映射，所以手抄错了也能进同一个房）。
  房间码就是 DO 的路由键，所以**必须在查询串里**给 ——
  等 `hello` 帧到了再认房间就晚了，那时候连接已经建好了。
- 客户端连上后第一帧发 `hello`，服务端回 `welcome`，然后开始即收即转。

拿真客户端打本地 DO（批次 1 用这个验过，见下「已验证」）—— `server_url` **不用**手写
`?room=`，`RoomClient._connect_url()` 会把归一化后的房间码补进查询串（配了 `token`
就一并补 `k=`；用户已经手写了 `room=`/`k=` 就不覆盖）：

```python
RoomConfig(server_url="ws://127.0.0.1:8787/ws", room_code="TEST1234", ...)
```

## 令牌（可选）

不配就是**开放房间**：任何拿到房间码的人都能进。要开门禁，在 `server/.dev.vars`
（本地）或 Worker Secrets（线上）里配 `ROOM_TOKEN_HASH`：

```bash
# 本地：server/.dev.vars（已被 .gitignore 排除，别提交）
ROOM_TOKEN_HASH=<令牌的 sha256 十六进制小写>
```

```bash
# 算哈希（PowerShell）
$t = 'my-secret-token'
$sha = [System.Security.Cryptography.SHA256]::Create()
([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($t))) -replace '-','').ToLower()
```

服务端**只存哈希**，明文令牌既不落盘也不进日志。鉴权在 Worker 层做完：
令牌不对直接 401/403，**不进 DO** —— 否则每一发攻击流量都在替你付 DO 请求费。

客户端这边对齐同一口径：令牌走查询串会被日志原样打出来，所以 `RoomClient` 打连接日志前
先过 `_masked_url()`，把 `k`/`tok`/`token` 的值抹成 `***`（日志会跟着 crashlog 落到用户硬盘上）。

## 部署（批次 3 做，这里只留步骤）

```bash
cd server
npx wrangler deploy                        # 首次会建 DO migration v1
npx wrangler secret put ROOM_TOKEN_HASH    # 可选：开门禁
```

**必须绑自定义域名，不要用 `workers.dev`** —— `*.workers.dev` 在大陆基本不可达，
而这个项目的用户就在大陆。做法是给 Worker 加 zone route（`wrangler.toml` 里已留注释）：

```toml
[[routes]]
pattern = "vlt-room.kcm-nixi.cn/*"
zone_name = "kcm-nixi.cn"
```

然后把客户端的 `room.server_url` 填成 `wss://vlt-room.kcm-nixi.cn/ws`（房间码客户端自己补进查询串）。

---

## 三条不能破的纪律

改这个目录之前先读完，这三条都是**计费/可用性**级别的坑，不是代码风格问题。

### 1. 必须用 `state.acceptWebSocket(ws)`，不能用 `ws.accept()`

`ws.accept()` 会让 DO 在整条连接存续期间保持唤醒，**按连接时长计费**；
Hibernation（`state.acceptWebSocket`）把连接托管给 CF 边缘，没消息时 DO 不占 CPU。
一个房间 8 个人挂着不说话，两种写法的账单差一个数量级。

代价：**休眠后内存全清空**。所以每连接状态（成员 id、昵称、限速窗口）必须
`serializeAttachment()` 挂到连接上，醒来时用 `getWebSockets()` + `deserializeAttachment()`
捞回来。`src/room.js` 里的 `attach()/attached()` 就是这层 —— 本机 workerd 只把这两个
方法挂在 WebSocket 上、`state` 上没有，新版文档写的是 `state.serializeAttachment(ws, x)`，
所以两处都试，取到哪个用哪个。

### 2. 不能用 `setTimeout` / `setInterval`

有待处理的定时器 = DO 不许休眠，等于白写了第 1 条。空房回收改用
`storage.setAlarm()`：最后一个人走了排一次闹钟，`alarm()` 里发现还是空的就
`storage.deleteAll()`。

### 3. 不能给还没握手的连接发房间帧

`fetch()` 里 `acceptWebSocket` 之后连接就进了 `this.sockets`，但那时它**还没发
`hello`**、还没被分配成员 id。如果这时候别人进房触发了 `members` 广播，这条连接的
第一帧就是 `members` 而不是 `welcome` —— 客户端按协议判错、直接断线重连。

两人同时进房时这个竞态**在真机 `wrangler dev --local` 上实测复现过**。
所以所有扇出/广播/人数判断都必须走 `members()`（只返回已握手的连接）。

---

## 其它口径

| 项 | 值 | 说明 |
|---|---|---|
| 成员上限 | 8 | 再多手腕屏也显示不下；满了回 `err{code:"room_full"}` 并关连接 |
| 每连接限速 | 20 帧/秒 | 固定 1 秒窗口，超出的帧**丢掉** + 回一条非致命 `err{code:"rate"}`，不断线 |
| 帧大小上限 | 8192 字节 | 与客户端 `protocol.MAX_FRAME_BYTES` 对齐 |
| ack | **只对 `final`** | partial 丢了无所谓，下一拍就是更全的快照 |
| 回声 | 不回发给说话人自己 | 客户端因此不需要做回声消除 |
| 落点 | `locationHint: "apac"` | 只是 best-effort，CF 明确不保证 |
| 存储类 | `new_sqlite_classes` | 本 DO 只用 `setAlarm/deleteAll`，不碰 `storage.sql` |

客户端拿到 `err` 的行为：`code` 属于 `{auth, room_full, bad_room, banned}` 就**不再重连**
（重连只会一直被拒，白烧请求），其余（比如 `rate`、`bad_frame`）留痕但保持连接。

`err` 帧里**没有** `fatal` 字段 —— 致命与否只由 `code` 决定。服务端对这四个致命码发完 `err`
就顺手 `close(1008)`。口径写在两处：`src/room.js` 的 `FATAL_CODES` 和客户端
`vlt/room/protocol.py` 的 `FATAL_ERR_CODES`，**改一边必须改另一边**，否则会出现
「服务端认为致命、客户端还在退避重连」这种一直撞墙的循环。

## 已验证 / 未验证

都是对**当前这份代码**在 `npx wrangler dev --local --port 8822`（wrangler 4.142.0 /
node v22.23.1，本机 workerd）上实测的，探针脚本用完就删（不入库）。

- ✅ **起得来**：`/` 返回只读 JSON 状态页；`/ws` 非升级请求回 426；房间码非法回 400。
- ✅ **正常收发**：两个真 `RoomClient` 同时进房 —— 成员表 2 人、3 个 partial + 1 个 final
  共 4 帧按 `seq=[1,2,3,4]` 有序到达、文本与发送端一致、说话人归属（成员 id + 昵称）正确、
  `final` 被 ack（`pending_replay` 归零）、发送端无回声、零重连零错误留痕、`stop()` 后线程退出。
- ✅ **`bad_room`（致命）**：URL 上房间码与 `hello` 里的不一致 → 回 `err{code:"bad_room"}`
  并 `close(1008)`。
- ✅ **`bad_frame`（非致命）**：发一段坏 JSON → 回 `err{code:"bad_frame"}`，**连接保留**，
  紧接着补一个合法 `hello` 仍能拿到 `welcome`。
- ✅ **`room_full`（致命）**：塞满 8 人后第 9 条连接 → 回 `err{code:"room_full"}` + `close(1008)`；
  真 `RoomClient` 拿到它之后 `last_error` 留下可读原因、连接态转 `error`、**零重连**、线程退出。

- ❌ **未验证**：真部署到 CF 边缘（`wrangler deploy`）、跨洋延迟、DO 休眠后
  `getWebSockets()` + attachment 恢复成员表的真实行为（本地 workerd 不模拟休眠）、
  `alarm()` 空房回收（本地跑不到 60s 宽限期就结束了）。这些都是批次 3 的事。
- ❌ **未验证**：令牌鉴权。`ROOM_TOKEN_HASH` 一次都没真配过，401/403 那两条分支
  只是读代码读出来的；哈希比对用了不短路的 `sameHash()`，但也只在本地静态检查过。
- ❌ **未验证**：20 帧/秒限速在**真 DO** 上的行为。限速逻辑在 `tests/test_room_client.py`
  的进程内假中继上验过（发 40 帧丢 35 帧、留痕不断线），真 DO 上没跑过 ——
  本地的 DO 计时精度和线上不一样，这一条要等批次 3 部署后再看。
