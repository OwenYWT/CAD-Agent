# 部署 / 测试者发布指南

> 目标:把「没实例」变成「能发给测试者的链接」。本仓库当前**只改了代码**,部署需在一台**有 Docker 的机器**上执行。

## TL;DR — 给前 5-10 个私有测试者(推荐)

在一台装了 Docker 的机器上:

```bash
bash setup.sh          # 建沙箱镜像 + 起后端:8000 + 前端:5173
# 然后开一条隧道,把私有链接发给测试者:
cloudflared tunnel --url http://localhost:5173
```

`setup.sh` 会:检查 Docker → 从 `.env.example` 生成 `backend/.env`(需你填 `DASHSCOPE_API_KEY`)→ build `cad-agent-sandbox:latest`(首次约 10-20 分钟,conda 解算)→ 起后端 + 前端 → 冒烟测 `/health`。

**安全前提**:此模式 `API_KEYS=[]`(无鉴权)。**只在链接私有、用完即拆时可接受**。详见下方「公开前必做」。

## 启动自检 / 就绪探针

后端启动会自检三件事并打印结果:LLM key、Docker 守护进程、沙箱镜像。
- `GET /health` → liveness(进程活着)
- `GET /ready` → readiness;缺任一前提返回 **503 + 具体缺什么**(部署时先 curl 这个,别等测试者撞错)

## 信号采集(测试者价值的关键)

- 每个结果卡有 👍/👎 + 「打出来了吗?」chip → `POST /api/feedback`,写入 `history.db` 的 `feedback` 表。
- 这是系统**无法推断**的地面真相(success=代码跑通 ≠ 真打印成功)。
- 看数据:`sqlite3 backend/data/history.db "SELECT printed, count(*) FROM feedback GROUP BY printed;"`

## 公开前必做(链接从私有转公开的硬闸)

当前沙箱的软件层过滤可被绕过,**唯一真边界是 Docker**。链接公开 / 鉴权复用前,必须:

1. **容器加固已在代码里就位**(`executor.py`:`read_only`、`cap_drop=ALL`、`pids_limit`、ulimits、tmpfs `/tmp`)——需在有 Docker 的机器上跑一次确认生成仍正常(见下「验证加固」)。
2. **开鉴权**:`backend/.env` 设 `API_KEYS=["<长随机串>"]`,前端 build 时设 `VITE_API_TOKEN` 一致。
3. **限定 CORS**:`CORS_ORIGINS=["https://你的域名"]`。
4. 强烈建议:换 gVisor (`runtime=runsc`) 或 Kata 作为容器运行时(进一步隔离)。

### 验证加固(需 Docker host)

加了 `read_only=True` 后,务必跑一次真实生成,确认 CadQuery/OCP 不因只读根文件系统失败:

```bash
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一个 60x40x30mm 的收纳盒,壁厚 1.5mm"}'
# 期望:success=true 且 validation.printable=true
```
若失败且报权限/只读错误,检查库是否往 `/tmp` 之外写(Dockerfile 已把 HOME/缓存指向 `/tmp`)。

## 升级到 compose(当超出隧道,需并发 / 长期在线时)

`docker-compose.yml` + `backend/Dockerfile` + `frontend/Dockerfile` 已提供。在 Docker host 上:

```bash
cp backend/.env.example backend/.env   # 填 key / CORS / API_KEYS
docker compose build                   # 含 sandbox 镜像
docker compose up -d
```

> ⚠️ 后端容器通过挂载 `docker.sock` 来启动沙箱容器(sibling 模式)。这把宿主 Docker 暴露给后端进程,仅在受信主机使用;生产应换成 DinD 或远程受限 Docker API。

## 本机限制说明

本次开发机**未装 Docker**,因此:
- ✅ 全部后端逻辑(可打印性门、feedback API、自检、CORS)已用 `pytest` + `TestClient` + `trimesh` **本地跑通**。
- ✅ 前端 `tsc` + `vite build` 通过。
- ⏳ 沙箱加固、镜像 build、端到端生成 **需在有 Docker 的机器上验证**(命令见上)。
