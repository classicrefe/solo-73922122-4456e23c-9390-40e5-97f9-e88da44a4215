# 专题目录入编判定台（Flask + SQLite）

部分分馆名册未到齐时，编目员需要先把**确定入编**的卷号交给专题目录。
本应用按“并、交、差”收录规则，结合已提交名单与缺报名单的所有可能归属，
对最多 5 万个总册卷号逐一给出 **确定入选 / 确定落选 / 待补** 结论，
并为待补卷号列出“仅翻转该名单归属就可能翻转结果”的缺报名单。

## 业务语义

- **总册卷号**：全部候选卷号，上限 50,000 个，每行一个（兼容单列 CSV、UTF-8/GB18030）。
- **名单来源**：最多 14 个，类型为扫描 / 验收 / 暂缓。登记后默认“缺报”。
- **逐份上传名单**：上传后状态变为“已提交”；上传空文件表示该名单**确定不含任何卷号**
  （与“缺报”语义不同）。撤回后回到“缺报”。
- **缺报**：卷号在该名单中可能在、也可能不在；同一名单在规则中出现多次时，
  同一卷号的归属始终一致。
- **收录规则**：JSON 表达，操作键支持 `并/union`、`交/intersect`、`差/diff`
  （差集恰好两个参数），叶子为名单名字符串，可任意嵌套。
- **判定**：枚举缺报名单的全部 2^K 种归属组合（K ≤ 14）——
  所有组合都入选则“确定入选”，都落选则“确定落选”，否则“待补”；
  待补卷号附带对其结果敏感（翻转即可能翻转结论）的缺报名单。
- **版本与原子切换**：每次名单补交/撤回、规则保存、总册替换都在**单个 SQLite 事务**内
  重算并生成不可变版本；成功才切换“当前版本”，失败整体回滚、网页归类与下载沿用旧版。
  历史版本永久保留，可随时下载。
- **CSV 下载**：任一历史版本均可下载“确定入编卷号 CSV”或“全部结论 CSV”（带 UTF-8 BOM）。

## 规则示例

```json
{
  "差": [
    { "并": ["甲馆验收", "乙馆扫描"] },
    { "交": ["丙馆暂缓", "甲馆验收"] }
  ]
}
```

含义：(甲馆验收 ∪ 乙馆扫描) − (丙馆暂缓 ∩ 甲馆验收)。

## 本地运行（无 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CATALOG_DB=/tmp/catalog.db python -m flask --app app.wsgi:app run --port 8000
# 或 gunicorn（容器同款入口）
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 4 app.wsgi:app
```

## Docker Compose 演示

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `catalog-data`（容器内 `/data/catalog.db`）。
单 worker + 多线程部署，进程内锁串行化所有写操作，保证重算事务原子性。

## 测试

```bash
pip install pytest
python -m pytest
```

- `tests/test_logic.py`：规则解析与三值逻辑判定，内置朴素枚举器对真值表实现做交叉验证
  （9 条规则 × 全部已报/缺报组合 × 并/交/差/重复引用）。
- `tests/test_app.py`：Flask 端到端流程、空名单 vs 缺报、撤回、失败回滚、
  旧版本 CSV 可下载、14 名单上限、5 万卷性能等。

## 目录结构

```
app/
  logic.py     规则解析 + 三值逻辑真值表求值（位掩码、归属签名缓存）
  service.py   SQLite 表结构、业务编排、事务与版本、CSV 生成
  __init__.py  Flask 路由与页面
  templates/   概览/总册/名单/规则/结果/版本 六个页面
  static/      样式
Dockerfile, docker-compose.yml, requirements.txt
```
