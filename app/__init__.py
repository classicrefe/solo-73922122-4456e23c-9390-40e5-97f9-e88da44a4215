"""Flask 应用工厂与路由。"""

from __future__ import annotations

import io
import os
from pathlib import Path

from flask import (Flask, flash, redirect, render_template, request,
                   send_file, url_for)

from .logic import MAX_LISTS
from .service import (KIND_LABELS, KINDS, MAX_VOLUMES, VERDICT_LABELS,
                      VERDICTS, CatalogService, ServiceError)

PAGE_SIZE = 100


def create_app(db_path: str | None = None) -> Flask:
    db_path = db_path or os.environ.get("CATALOG_DB", "/data/catalog.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "demo-secret-key")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    service = CatalogService(db_path)
    app.extensions["catalog_service"] = service

    def svc() -> CatalogService:
        return app.extensions["catalog_service"]

    def handle_error(exc: Exception):
        flash(f"操作失败，已沿用原有数据与版本：{exc}", "error")
        return redirect(request.referrer or url_for("index"))

    app.register_error_handler(ServiceError, handle_error)

    # ---------------- 首页概览 ----------------
    @app.route("/")
    def index():
        conn = svc()._conn()
        try:
            data = svc().dashboard(conn)
            data["versions"] = svc().list_versions(conn)[:10]
            return render_template("index.html", **data)
        finally:
            conn.close()

    # ---------------- 总册卷号 ----------------
    @app.route("/volumes", methods=["GET"])
    def volumes():
        page = max(1, request.args.get("page", 1, type=int))
        conn = svc()._conn()
        try:
            total = svc().volume_count(conn)
            rows = svc().list_volumes(conn, PAGE_SIZE, (page - 1) * PAGE_SIZE)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            return render_template("volumes.html", rows=rows, total=total,
                                   page=page, pages=pages, max_volumes=MAX_VOLUMES)
        finally:
            conn.close()

    @app.route("/volumes/upload", methods=["POST"])
    def volumes_upload():
        f = request.files.get("file")
        if f is None:
            raise ServiceError("请选择文件")
        summary = svc().replace_volumes(f.read())
        if summary.get("created"):
            flash(f"总册已更新为 {summary['total']} 个卷号，重算出版本 #{summary['version_id']}", "ok")
        else:
            flash(f"总册已保存 {summary['total']} 个卷号；登记规则后才会生成结论版本", "ok")
        return redirect(url_for("volumes"))

    # ---------------- 名单来源 ----------------
    @app.route("/lists")
    def lists():
        conn = svc()._conn()
        try:
            rows = svc().list_sources(conn)
            return render_template("lists.html", rows=rows, kinds=KINDS,
                                   kind_labels=KIND_LABELS, max_lists=MAX_LISTS,
                                   status_labels={"missing": "缺报", "reported": "已提交",
                                                  "withdrawn": "已撤回"})
        finally:
            conn.close()

    @app.route("/lists/register", methods=["POST"])
    def lists_register():
        svc().register_list(request.form.get("name", ""), request.form.get("kind", "accept"))
        flash("名单来源已登记", "ok")
        return redirect(url_for("lists"))

    @app.route("/lists/<int:list_id>/upload", methods=["POST"])
    def lists_upload(list_id):
        f = request.files.get("file")
        if f is None:
            raise ServiceError("请选择文件（空文件表示该名单没有卷号）")
        summary = svc().upload_list(list_id, f.read())
        if summary.get("created"):
            flash(f"名单已提交（{summary['entries']} 个卷号），重算出版本 "
                  f"#{summary['version_id']}：入选 {summary['n_in']} / 落选 "
                  f"{summary['n_out']} / 待补 {summary['n_pending']}", "ok")
        else:
            flash(f"名单已保存（{summary['entries']} 个卷号）；规则或总册缺失，暂未生成版本", "ok")
        return redirect(url_for("lists"))

    @app.route("/lists/<int:list_id>/withdraw", methods=["POST"])
    def lists_withdraw(list_id):
        summary = svc().withdraw_list(list_id)
        if summary.get("created"):
            flash(f"名单已撤回并按缺报处理，重算出版本 #{summary['version_id']}：入选 "
                  f"{summary['n_in']} / 落选 {summary['n_out']} / 待补 {summary['n_pending']}", "ok")
        else:
            flash("名单已撤回；规则或总册缺失，暂未生成版本", "ok")
        return redirect(url_for("lists"))

    # ---------------- 规则 ----------------
    @app.route("/rules", methods=["GET"])
    def rules():
        conn = svc()._conn()
        try:
            rule_text = svc().get_rule_text(conn)
            names = [r["name"] for r in svc().list_sources(conn)]
            example = '{\n  "差": [\n    {"并": ["验收A", "验收B"]},\n    {"交": ["暂缓C", "验收A"]}\n  ]\n}'
            return render_template("rules.html", rule_text=rule_text or "",
                                   names=names, example=example)
        finally:
            conn.close()

    @app.route("/rules/save", methods=["POST"])
    def rules_save():
        summary = svc().save_rule(request.form.get("rule", ""))
        if summary.get("created"):
            flash(f"规则已保存，重算出版本 #{summary['version_id']}：入选 {summary['n_in']} / "
                  f"落选 {summary['n_out']} / 待补 {summary['n_pending']}", "ok")
        elif summary.get("equivalent"):
            flash("新写法与现行规则在所有名单归属组合下结论一致：已保存新写法供下次发版使用，"
                  "本次跳过逐卷重算，现行版本不变", "ok")
        else:
            flash("规则已保存；总册卷号缺失，暂未生成版本", "ok")
        return redirect(url_for("rules"))

    # ---------------- 结果 ----------------
    @app.route("/results")
    def results():
        verdict = request.args.get("verdict") or None
        page = max(1, request.args.get("page", 1, type=int))
        conn = svc()._conn()
        try:
            vid = svc().current_version_id(conn)
            version = svc().get_version(conn, vid) if vid else None
            if version is None:
                return render_template("results.html", version=None, rows=[],
                                       total=0, page=1, pages=1, verdict=verdict,
                                       verdicts=VERDICTS, verdict_labels=VERDICT_LABELS)
            total = svc().result_count(conn, vid, verdict)
            rows = svc().result_page(conn, vid, verdict, PAGE_SIZE, (page - 1) * PAGE_SIZE)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            return render_template("results.html", version=version, rows=rows,
                                   total=total, page=page, pages=pages, verdict=verdict,
                                   verdicts=VERDICTS, verdict_labels=VERDICT_LABELS)
        finally:
            conn.close()

    # ---------------- 版本与下载 ----------------
    @app.route("/versions")
    def versions():
        conn = svc()._conn()
        try:
            rows = svc().list_versions(conn)
            current = svc().current_version_id(conn)
            return render_template("versions.html", rows=rows, current=current,
                                   verdict_labels=VERDICT_LABELS)
        finally:
            conn.close()

    @app.route("/versions/<int:version_id>/download")
    def download(version_id):
        only_in = request.args.get("only_in") == "1"
        content = svc().csv_for_version(version_id, only_in=only_in)
        suffix = "确定入编卷号" if only_in else "全部结论"
        return send_file(
            io.BytesIO(content.encode("utf-8")),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=f"catalog_v{version_id}_{suffix}.csv",
        )

    @app.errorhandler(413)
    def too_large(_e):
        flash("上传内容超过 16 MiB 上限", "error")
        return redirect(request.referrer or url_for("index"))

    return app
