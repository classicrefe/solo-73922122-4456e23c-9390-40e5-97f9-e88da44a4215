"""SQLite 数据访问与业务编排。

表结构
------
meta          单行键值（规则文档、当前版本号等）
volume        总册卷号主数据
list_source   名单来源（最多 14 个），含状态 missing/reported/withdrawn
list_entry    已提交名单中的卷号条目
rule_doc      规则 JSON 文本（单行）
result_version 每次成功重算生成一个不可变版本
result_item   版本下每个卷号的结论（in/out/pending）及相关缺报名单

版本语义：重算在单个事务内完成，成功才写入新版本并切换“当前版本”；
失败则整体回滚，网页与下载继续沿用上一版。旧版本永久保留、可随时下载。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .logic import MAX_LISTS, RuleError, RuleEvaluator, parse_rule, referenced_names

MAX_VOLUMES = 50_000
MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 单次上传 8 MiB 上限
VERDICTS = ("in", "out", "pending")
VERDICT_LABELS = {"in": "确定入选", "out": "确定落选", "pending": "待补"}
KINDS = ("scan", "accept", "hold")
KIND_LABELS = {"scan": "扫描", "accept": "验收", "hold": "暂缓"}


class ServiceError(ValueError):
    """输入或业务校验失败（回滚整个请求）。"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS volume (
    id      INTEGER PRIMARY KEY,
    volume_no TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS list_source (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL DEFAULT 'accept',
    status    TEXT NOT NULL DEFAULT 'missing',          -- missing/reported/withdrawn
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_entry (
    list_id   INTEGER NOT NULL REFERENCES list_source(id),
    volume_no TEXT NOT NULL,
    PRIMARY KEY (list_id, volume_no)
);
CREATE TABLE IF NOT EXISTS rule_doc (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    doc TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_version (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    total       INTEGER NOT NULL,
    n_in        INTEGER NOT NULL,
    n_out       INTEGER NOT NULL,
    n_pending   INTEGER NOT NULL,
    n_missing_lists INTEGER NOT NULL,
    rule_snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_item (
    version_id    INTEGER NOT NULL REFERENCES result_version(id),
    volume_no     TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    relevant_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (version_id, volume_no)
);
CREATE INDEX IF NOT EXISTS idx_result_item_verdict
    ON result_item(version_id, verdict);
CREATE INDEX IF NOT EXISTS idx_list_entry_list ON list_entry(list_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = get_db(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 解析上传文件

def parse_volume_text(raw: bytes) -> list[str]:
    """解析上传的卷号文本：每行一个；也兼容单列 CSV。

    返回去空白后的有序去重列表；文件可为空（表示空名单）。
    """
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ServiceError(f"上传文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MiB 上限")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ServiceError("文件编码无法识别，请使用 UTF-8 文本") from exc

    out: list[str] = []
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        token = row[0].strip()
        if not token:
            continue
        if token in seen:
            raise ServiceError(f"文件内存在重复卷号：{token}")
        seen.add(token)
        out.append(token)
    return out


# ---------------------------------------------------------------- 服务

class CatalogService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        init_db(db_path)
        # 写操作串行化（配合单进程 gunicorn，保证重算原子性）
        self._lock = threading.RLock()

    def _conn(self) -> sqlite3.Connection:
        return get_db(self.db_path)

    # ---- 基础查询 -------------------------------------------------------

    def volume_count(self, conn: sqlite3.Connection) -> int:
        return conn.execute("SELECT COUNT(*) FROM volume").fetchone()[0]

    def list_volumes(self, conn: sqlite3.Connection, limit: int, offset: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT volume_no FROM volume ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()

    def list_sources(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM list_entry e WHERE e.list_id = s.id) AS n_entries "
            "FROM list_source s ORDER BY s.id"
        ).fetchall()

    def get_source(self, conn: sqlite3.Connection, list_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM list_source WHERE id = ?", (list_id,)).fetchone()

    def get_rule_text(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT doc FROM rule_doc WHERE id = 1").fetchone()
        return row["doc"] if row else None

    def current_version_id(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute("SELECT value FROM meta WHERE key = 'current_version'").fetchone()
        return int(row["value"]) if row else None

    def get_version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM result_version WHERE id = ?", (version_id,)
        ).fetchone()

    def list_versions(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM result_version ORDER BY id DESC").fetchall()

    # ---- 总册卷号 -------------------------------------------------------

    def replace_volumes(self, raw: bytes) -> dict:
        volumes = parse_volume_text(raw)
        if len(volumes) > MAX_VOLUMES:
            raise ServiceError(f"总册卷号数量 {len(volumes)} 超过上限 {MAX_VOLUMES}")
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM volume")
                conn.executemany(
                    "INSERT INTO volume(volume_no) VALUES (?)", [(v,) for v in volumes]
                )
                # 清理名单条目中已不在总册内的悬空卷号，随后同事务重算
                conn.execute(
                    "DELETE FROM list_entry WHERE volume_no NOT IN "
                    "(SELECT volume_no FROM volume)"
                )
                summary = self._recompute(conn, reason="总册卷号更新")
                conn.commit()
                return {"count": len(volumes), **summary}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---- 名单来源登记 ---------------------------------------------------

    def register_list(self, name: str, kind: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ServiceError("名单名称不能为空")
        if kind not in KINDS:
            raise ServiceError("名单类型必须是扫描、验收或暂缓")
        with self._lock:
            conn = self._conn()
            try:
                if conn.execute(
                    "SELECT 1 FROM list_source WHERE name = ?", (name,)
                ).fetchone():
                    raise ServiceError(f"名单名已存在：{name}")
                if conn.execute("SELECT COUNT(*) FROM list_source").fetchone()[0] >= MAX_LISTS:
                    raise ServiceError(f"名单来源最多 {MAX_LISTS} 个")
                ts = now_iso()
                conn.execute(
                    "INSERT INTO list_source(name, kind, status, created_at, updated_at) "
                    "VALUES (?, ?, 'missing', ?, ?)",
                    (name, kind, ts, ts),
                )
                conn.commit()
                return {"name": name, "kind": kind}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def upload_list(self, list_id: int, raw: bytes) -> dict:
        entries = parse_volume_text(raw)  # 空文件 -> 空名单（已提交，零卷号）
        with self._lock:
            conn = self._conn()
            try:
                src = self.get_source(conn, list_id)
                if src is None:
                    raise ServiceError("名单不存在")
                if self.volume_count(conn) == 0:
                    raise ServiceError("请先上传总册卷号")
                known = {r["volume_no"] for r in conn.execute("SELECT volume_no FROM volume")}
                unknown = sorted(set(entries) - known)
                if unknown:
                    raise ServiceError(
                        "存在总册之外的卷号（前 10 个）：" + "、".join(unknown[:10])
                    )
                conn.execute("DELETE FROM list_entry WHERE list_id = ?", (list_id,))
                conn.executemany(
                    "INSERT INTO list_entry(list_id, volume_no) VALUES (?, ?)",
                    [(list_id, v) for v in entries],
                )
                conn.execute(
                    "UPDATE list_source SET status = 'reported', updated_at = ? WHERE id = ?",
                    (now_iso(), list_id),
                )
                summary = self._recompute(conn, reason=f"名单《{src['name']}》提交")
                conn.commit()
                return {"entries": len(entries), **summary}
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def withdraw_list(self, list_id: int) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                src = self.get_source(conn, list_id)
                if src is None:
                    raise ServiceError("名单不存在")
                if src["status"] == "missing":
                    raise ServiceError("该名单尚未提交，无需撤回")
                conn.execute("DELETE FROM list_entry WHERE list_id = ?", (list_id,))
                conn.execute(
                    "UPDATE list_source SET status = 'withdrawn', updated_at = ? WHERE id = ?",
                    (now_iso(), list_id),
                )
                summary = self._recompute(conn, reason=f"名单《{src['name']}》撤回")
                conn.commit()
                return summary
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---- 规则 -----------------------------------------------------------

    def save_rule(self, text: str) -> dict:
        text = text or ""
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ServiceError(f"规则不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
        with self._lock:
            conn = self._conn()
            try:
                names = [r["name"] for r in self.list_sources(conn)]
                parse_rule(doc, names)  # 校验，失败抛 RuleError -> ServiceError
                conn.execute(
                    "INSERT INTO rule_doc(id, doc, updated_at) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at",
                    (text.strip(), now_iso()),
                )
                summary = self._recompute(conn, reason="收录规则更新")
                conn.commit()
                return summary
            except RuleError as exc:
                conn.rollback()
                raise ServiceError(str(exc)) from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---- 重算（核心）----------------------------------------------------

    def _recompute(self, conn: sqlite3.Connection, reason: str) -> dict:
        """在调用方事务/锁内执行重算。任何失败抛出，由调用方回滚。

        成功：写入新 result_version/result_item，切换 current_version。
        无规则或无总册：不产生版本，返回空摘要（页面提示先补齐前置数据）。
        """
        rule_text = self.get_rule_text(conn)
        volumes = [r["volume_no"] for r in conn.execute(
            "SELECT volume_no FROM volume ORDER BY id")]
        if rule_text is None or not volumes:
            return {"version_id": None, "total": len(volumes),
                    "n_in": 0, "n_out": 0, "n_pending": 0, "created": False}

        doc = json.loads(rule_text)
        sources = self.list_sources(conn)
        names = [r["name"] for r in sources]
        tree = parse_rule(doc, names)
        used = referenced_names(tree)

        # 缺报 = 未提交（missing）或已撤回（withdrawn）；空名单是 reported，不算缺报
        missing_rows = [r for r in sources
                        if r["status"] in ("missing", "withdrawn") and r["name"] in used]
        missing_names = [r["name"] for r in missing_rows]
        id_by_name = {r["name"]: r["id"] for r in sources}
        evaluator = RuleEvaluator(tree, missing_names)

        # 预取各已提交名单的卷号集合，并把名单名映射为求值器的归属位
        reported_bits = evaluator.reported_bits
        sig_by_vol: dict[str, int] = {}
        for r in sources:
            if r["status"] != "reported" or r["name"] not in used:
                continue
            bit = reported_bits.get(r["name"])
            if bit is None:
                continue
            rows = conn.execute(
                "SELECT volume_no FROM list_entry WHERE list_id = ?", (r["id"],)
            ).fetchall()
            for x in rows:
                vol = x["volume_no"]
                sig_by_vol[vol] = sig_by_vol.get(vol, 0) | bit

        # 逐卷判定；同归属签名的结论由求值器缓存复用
        items: list[tuple[str, str, str]] = []
        counts = {"in": 0, "out": 0, "pending": 0}
        for vol in volumes:
            verdict, rel_idx = evaluator.classify_signature(sig_by_vol.get(vol, 0))
            rel_ids = [id_by_name[missing_names[i]] for i in rel_idx]
            items.append((vol, verdict, json.dumps(rel_ids, ensure_ascii=False)))
            counts[verdict] += 1

        cur = conn.execute(
            "INSERT INTO result_version(created_at, reason, total, n_in, n_out, n_pending, "
            "n_missing_lists, rule_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), reason, len(volumes), counts["in"], counts["out"],
             counts["pending"], len(missing_names), rule_text),
        )
        version_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO result_item(version_id, volume_no, verdict, relevant_json) "
            "VALUES (?, ?, ?, ?)",
            [(version_id, vol, verdict, rel) for vol, verdict, rel in items],
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('current_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version_id),),
        )
        return {"version_id": version_id, "total": len(volumes),
                "n_in": counts["in"], "n_out": counts["out"],
                "n_pending": counts["pending"], "created": True}

    # ---- 结果查询与下载 -------------------------------------------------

    def result_page(self, conn: sqlite3.Connection, version_id: int,
                    verdict: str | None, limit: int, offset: int) -> list[sqlite3.Row]:
        sql = ("SELECT i.volume_no, i.verdict, i.relevant_json, "
               "       (SELECT group_concat(s.name, '、') FROM list_source s "
               "        WHERE s.id IN (SELECT value FROM json_each(i.relevant_json))) "
               "       AS relevant_names "
               "FROM result_item i WHERE i.version_id = ?")
        params: list = [version_id]
        if verdict in VERDICTS:
            sql += " AND i.verdict = ?"
            params.append(verdict)
        sql += " ORDER BY i.rowid LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return conn.execute(sql, params).fetchall()

    def result_count(self, conn: sqlite3.Connection, version_id: int,
                     verdict: str | None = None) -> int:
        if verdict in VERDICTS:
            return conn.execute(
                "SELECT COUNT(*) FROM result_item WHERE version_id = ? AND verdict = ?",
                (version_id, verdict),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM result_item WHERE version_id = ?", (version_id,)
        ).fetchone()[0]

    def csv_for_version(self, version_id: int, only_in: bool) -> str:
        """生成指定版本的 CSV。only_in=True 仅含确定入编卷号（交付专题目录用）。"""
        conn = self._conn()
        try:
            version = self.get_version(conn, version_id)
            if version is None:
                raise ServiceError("版本不存在")
            buf = io.StringIO()
            buf.write("﻿")  # UTF-8 BOM，便于 Excel 直接打开
            writer = csv.writer(buf)
            if only_in:
                writer.writerow(["卷号"])
                rows = conn.execute(
                    "SELECT volume_no FROM result_item WHERE version_id = ? AND verdict = 'in' "
                    "ORDER BY rowid", (version_id,)).fetchall()
                for r in rows:
                    writer.writerow([r["volume_no"]])
            else:
                writer.writerow(["卷号", "结论", "可翻转结论的缺报名单"])
                rows = conn.execute(
                    "SELECT volume_no, verdict, relevant_json FROM result_item "
                    "WHERE version_id = ? ORDER BY rowid", (version_id,)).fetchall()
                id_to_name = {r["id"]: r["name"]
                              for r in conn.execute("SELECT id, name FROM list_source")}
                for r in rows:
                    rel = [id_to_name.get(i, str(i))
                           for i in json.loads(r["relevant_json"])]
                    writer.writerow([r["volume_no"],
                                     VERDICT_LABELS[r["verdict"]],
                                     "、".join(rel)])
            return buf.getvalue()
        finally:
            conn.close()

    # ---- 仪表盘 ---------------------------------------------------------

    def dashboard(self, conn: sqlite3.Connection) -> dict:
        vid = self.current_version_id(conn)
        version = self.get_version(conn, vid) if vid else None
        return {
            "volume_count": self.volume_count(conn),
            "list_count": conn.execute("SELECT COUNT(*) FROM list_source").fetchone()[0],
            "reported_count": conn.execute(
                "SELECT COUNT(*) FROM list_source WHERE status = 'reported'").fetchone()[0],
            "rule_text": self.get_rule_text(conn),
            "current_version": version,
            "max_volumes": MAX_VOLUMES,
            "max_lists": MAX_LISTS,
        }
