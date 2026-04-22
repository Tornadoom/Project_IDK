# Project IDK Dashboard

一个无外部依赖的 Dashboard 原型，包含：

- 注册 / 登录
- 邀请码注册，邀请码固定为 `RQSB`
- 用户昵称、头像上传与裁剪
- 待办事项增删改查、按截止时间排序
- 购物车增删改查、图片详情上传与查看
- 操作日志本地保存
- SQLite 数据本地存储
- 每 12 小时自动备份数据库
- Markdown / Excel `.xlsx` 导出

## 启动

```powershell
python server.py
```

默认访问：

```text
http://127.0.0.1:8000
```

如需指定端口：

```powershell
python server.py --host 0.0.0.0 --port 8000
```

## 本地数据目录

```text
data/
  dashboard.db
  backups/
  logs/
  uploads/
```

## 部署备注

当前版本使用 Python 标准库，方便你快速本地测试。后续如果上传 GitHub 并使用 ECS + OpenClaw，可以继续保留 API 协议，替换为 Node/Go 后端或 React 前端。
