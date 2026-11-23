"""端到端业务测试：Flask 测试客户端 + 临时 SQLite。"""

import io

import pytest

from app import create_app
from app.service import MAX_VOLUMES, CatalogService, ServiceError


@pytest.fixture()
def app(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def svc(app):
    return app.extensions["catalog_service"]


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, path, lines):
    data = "\n".join(lines).encode()
    return client.post(path, data={"file": (io.BytesIO(data), "f.txt")},
                       content_type="multipart/form-data", follow_redirects=True)


def setup_basic(client):
    upload(client, "/volumes/upload", ["V1", "V2", "V3", "V4"])
    client.post("/lists/register", data={"name": "甲馆", "kind": "accept"})
    client.post("/lists/register", data={"name": "乙馆", "kind": "accept"})
    client.post("/lists/register", data={"name": "暂缓丙", "kind": "hold"})


# ---------------------------------------------------------------- 页面冒烟

def test_pages_render_empty(client):
    for path in ["/", "/volumes", "/lists", "/rules", "/results", "/versions"]:
        rv = client.get(path)
        assert rv.status_code == 200


# ---------------------------------------------------------------- 完整业务流

def test_full_flow_and_verdicts(client, svc):
    setup_basic(client)
    # 规则：甲馆 ∪ 乙馆 − 暂缓丙
    rv = client.post("/rules/save", data={"rule": '{"差":[{"并":["甲馆","乙馆"]},"暂缓丙"]}'},
                     follow_redirects=True)
    assert rv.status_code == 200
    # 全部缺报时所有卷号待补
    with svc._conn() as conn:
        vid = svc.current_version_id(conn)
        assert svc.result_count(conn, vid, "pending") == 4
        page = svc.result_page(conn, vid, "pending", 100, 0)
        rel = {r["volume_no"]: r["relevant_names"] for r in page}
        # 三个缺报名单都可能翻转结果
        assert set(rel["V1"].split("、")) == {"甲馆", "乙馆", "暂缓丙"}

    # 提交甲馆：含 V1、V2；空行和 BOM 都应被接受
    upload(client, "/lists/1/upload", ["V1", "V2"])
    with svc._conn() as conn:
        vid = svc.current_version_id(conn)
        verdicts = {r["volume_no"]: r["verdict"]
                    for r in svc.result_page(conn, vid, None, 100, 0)}
        assert verdicts["V1"] == "pending"  # 乙馆补报不影响并集，只有暂缓丙能剔除它
        assert verdicts["V3"] == "pending"
        v1 = next(r for r in svc.result_page(conn, vid, "pending", 100, 0)
                  if r["volume_no"] == "V1")
        assert v1["relevant_names"] == "暂缓丙"
        v3 = next(r for r in svc.result_page(conn, vid, "pending", 100, 0)
                  if r["volume_no"] == "V3")
        assert set(v3["relevant_names"].split("、")) == {"乙馆", "暂缓丙"}

    # 提交暂缓丙为空名单（已提交、零卷号）：不再缺报，V1/V2 确定入选
    rv = client.post("/lists/3/upload",
                     data={"file": (io.BytesIO(b""), "empty.txt")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert rv.status_code == 200
    with svc._conn() as conn:
        vid = svc.current_version_id(conn)
        version = svc.get_version(conn, vid)
        assert version['n_missing_lists'] == 1  # 只剩乙馆缺报
        verdicts = {r["volume_no"]: r["verdict"]
                    for r in svc.result_page(conn, vid, None, 100, 0)}
        assert verdicts["V1"] == "in"
        assert verdicts["V2"] == "in"
        assert verdicts["V3"] == "pending"
        assert verdicts["V4"] == "pending"  # 乙馆缺报，V4 可能被其补入，不能判落选

    # 撤回甲馆：回到缺报，V1/V2 重新待补
    client.post("/lists/1/withdraw", follow_redirects=True)
    with svc._conn() as conn:
        vid = svc.current_version_id(conn)
        version = svc.get_version(conn, vid)
        assert version['n_missing_lists'] == 2
        assert svc.result_count(conn, vid, "in") == 0


def test_recompute_failure_keeps_previous_version(client, svc):
    setup_basic(client)
    client.post("/rules/save", data={"rule": '{"并":["甲馆","乙馆"]}'})
    upload(client, "/lists/1/upload", ["V1"])
    with svc._conn() as conn:
        good_version = svc.current_version_id(conn)

    # 上传含总册之外卷号的名单：整笔失败，版本不变
    rv = client.post("/lists/2/upload",
                     data={"file": (io.BytesIO(b"V9\n"), "bad.txt")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "操作失败" in rv.text
    with svc._conn() as conn:
        assert svc.current_version_id(conn) == good_version
        # 乙馆仍为缺报，没有残留条目
        row = svc.get_source(conn, 2)
        assert row["status"] == "missing"

    # 非法规则 JSON：保存失败，规则与版本都不变
    rv = client.post("/rules/save", data={"rule": "{not json"}, follow_redirects=True)
    assert "操作失败" in rv.text
    with svc._conn() as conn:
        assert svc.current_version_id(conn) == good_version

    # 引用未登记名单的规则同样失败
    rv = client.post("/rules/save", data={"rule": '{"并":["甲馆","幽灵馆"]}'},
                     follow_redirects=True)
    assert "操作失败" in rv.text
    with svc._conn() as conn:
        assert svc.current_version_id(conn) == good_version


def test_old_version_csv_remains_downloadable(client, svc):
    setup_basic(client)
    client.post("/rules/save", data={"rule": '{"并":["甲馆","乙馆"]}'})
    upload(client, "/lists/1/upload", ["V1", "V2"])
    with svc._conn() as conn:
        v1 = svc.current_version_id(conn)
    old_csv = svc.csv_for_version(v1, only_in=True)
    assert "V1" in old_csv and "V2" in old_csv

    # 补交乙馆后产生新版本
    upload(client, "/lists/2/upload", ["V3"])
    with svc._conn() as conn:
        v2 = svc.current_version_id(conn)
    assert v2 > v1
    # 旧版本下载内容不变，新版本含 V3
    assert svc.csv_for_version(v1, only_in=True) == old_csv
    assert "V3" in svc.csv_for_version(v2, only_in=True)
    assert "V3" not in svc.csv_for_version(v1, only_in=True)

    # HTTP 下载路由
    rv = client.get(f"/versions/{v1}/download?only_in=1")
    assert rv.status_code == 200
    assert rv.headers["Content-Disposition"].startswith("attachment")
    # v1 中乙馆仍缺报，全量 CSV 应含待补结论；v2 全部名单已提交，无待补
    assert "待补" in svc.csv_for_version(v1, only_in=False)
    assert "待补" not in svc.csv_for_version(v2, only_in=False)
    rv_all = client.get(f"/versions/{v1}/download")
    assert "待补" in rv_all.data.decode("utf-8")


def test_list_limits_and_duplicates(client, svc):
    upload(client, "/volumes/upload", [f"V{i}" for i in range(3)])
    for i in range(14):
        rv = client.post("/lists/register", data={"name": f"L{i}", "kind": "accept"},
                         follow_redirects=True)
        assert rv.status_code == 200
    rv = client.post("/lists/register", data={"name": "L14", "kind": "accept"},
                     follow_redirects=True)
    assert "最多 14 个" in rv.text

    # 文件内重复卷号
    rv = client.post("/lists/1/upload",
                     data={"file": (io.BytesIO(b"V1\nV1\n"), "dup.txt")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "重复卷号" in rv.text

    # 重名名单
    rv = client.post("/lists/register", data={"name": "L0", "kind": "accept"},
                     follow_redirects=True)
    assert "已存在" in rv.text


def test_definite_out_and_scan_kind(client, svc):
    """规则引用的名单全部已提交时，可产生确定落选；扫描类型与其他类型行为一致。"""
    upload(client, "/volumes/upload", ["V1", "V2", "V3"])
    client.post("/lists/register", data={"name": "扫描单", "kind": "scan"})
    client.post("/lists/register", data={"name": "验收单", "kind": "accept"})
    client.post("/rules/save", data={"rule": '{"交":["扫描单","验收单"]}'})
    upload(client, "/lists/1/upload", ["V1", "V2"])
    upload(client, "/lists/2/upload", ["V2", "V3"])
    conn = svc._conn()
    try:
        vid = svc.current_version_id(conn)
        verdicts = {r["volume_no"]: r["verdict"]
                    for r in svc.result_page(conn, vid, None, 100, 0)}
        assert verdicts == {"V1": "out", "V2": "in", "V3": "out"}
        # 确定结论不附带可翻转名单
        rows = {r["volume_no"]: r["relevant_json"]
                for r in svc.result_page(conn, vid, None, 100, 0)}
        assert rows["V1"] == "[]" and rows["V2"] == "[]"
    finally:
        conn.close()


def test_replace_volumes_cleans_dangling_entries(client, svc):
    upload(client, "/volumes/upload", ["V1", "V2"])
    client.post("/lists/register", data={"name": "甲馆", "kind": "scan"})
    client.post("/rules/save", data={"rule": '{"并":["甲馆"]}'})
    upload(client, "/lists/1/upload", ["V1", "V2"])
    # 总册替换为不含 V2 的新集合
    upload(client, "/volumes/upload", ["V1", "V3"])
    conn = svc._conn()
    try:
        entries = {r["volume_no"] for r in conn.execute(
            "SELECT volume_no FROM list_entry WHERE list_id = 1")}
        assert entries == {"V1"}
    finally:
        conn.close()


def test_volume_limit_enforced(tmp_path):
    svc = CatalogService(str(tmp_path / "big.db"))
    raw = ("\n".join(f"V{i}" for i in range(MAX_VOLUMES + 1))).encode()
    with pytest.raises(ServiceError, match="超过上限"):
        svc.replace_volumes(raw)
    # 失败后总册仍为空（事务回滚）
    with svc._conn() as conn:
        assert svc.volume_count(conn) == 0


def test_withdraw_missing_list_rejected(client):
    setup_basic(client)
    rv = client.post("/lists/1/withdraw", follow_redirects=True)
    assert "无需撤回" in rv.text


def test_upload_before_volumes_rejected(client):
    client.post("/lists/register", data={"name": "甲馆", "kind": "accept"})
    rv = client.post("/lists/1/upload",
                     data={"file": (io.BytesIO(b"V1\n"), "a.txt")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "请先上传总册卷号" in rv.text


def test_version_switch_is_atomic_on_recompute(client, svc):
    setup_basic(client)
    client.post("/rules/save", data={"rule": '{"并":["甲馆"]}'})
    upload(client, "/lists/1/upload", ["V1"])
    with svc._conn() as conn:
        first = svc.current_version_id(conn)

    # 替换名单（含非法卷号）失败：当前版本不动，条目也不动
    rv = client.post("/lists/1/upload",
                     data={"file": (io.BytesIO(b"NOPE\n"), "x.txt")},
                     content_type="multipart/form-data", follow_redirects=True)
    assert rv.status_code == 200
    with svc._conn() as conn:
        assert svc.current_version_id(conn) == first
        entries = conn.execute("SELECT volume_no FROM list_entry WHERE list_id=1").fetchall()
        assert [r["volume_no"] for r in entries] == ["V1"]


def test_list_reupload_replaces_and_recomputes(client, svc):
    setup_basic(client)
    client.post("/rules/save", data={"rule": '{"并":["甲馆","乙馆"]}'})
    upload(client, "/lists/1/upload", ["V1", "V2"])
    conn = svc._conn()
    try:
        v_before = svc.current_version_id(conn)
    finally:
        conn.close()
    # 用只含 V3 的新文件替换甲馆：V1/V2 条目被清除，产生新版本
    upload(client, "/lists/1/upload", ["V3"])
    conn = svc._conn()
    try:
        v_after = svc.current_version_id(conn)
        assert v_after > v_before
        entries = [r["volume_no"] for r in conn.execute(
            "SELECT volume_no FROM list_entry WHERE list_id=1 ORDER BY volume_no")]
        assert entries == ["V3"]
        verdicts = {r["volume_no"]: r["verdict"]
                    for r in svc.result_page(conn, v_after, None, 100, 0)}
        assert verdicts["V3"] == "in"
        assert verdicts["V1"] == "pending"  # 乙馆缺报，V1 仍可能补入
    finally:
        conn.close()


def test_50k_volumes_performance(tmp_path):
    """上限规模冒烟：5 万卷号 + 3 个缺报名单的重算耗时与结果完整性。"""
    svc = CatalogService(str(tmp_path / "perf.db"))
    svc.replace_volumes(("\n".join(f"V{i:06d}" for i in range(MAX_VOLUMES))).encode())
    for name in ["甲馆", "乙馆", "丙馆"]:
        svc.register_list(name, "accept")
    import time
    t0 = time.perf_counter()
    svc.save_rule('{"差":[{"并":["甲馆","乙馆"]},"丙馆"]}')
    elapsed = time.perf_counter() - t0
    conn = svc._conn()
    try:
        vid = svc.current_version_id(conn)
        v = svc.get_version(conn, vid)
        assert v['total'] == MAX_VOLUMES
        assert v['n_in'] == 0
        assert v['n_out'] == 0
        assert v['n_pending'] == MAX_VOLUMES
    finally:
        conn.close()
    assert elapsed < 30, f"重算过慢：{elapsed:.1f}s"
