"""テスマエ！ - テスト前の「わからない」を助け合う掲示板 (Flask)

環境変数:
  AZURE_STORAGE_CONNECTION_STRING  ストレージアカウントの接続文字列（未設定ならメモリ保存で動く）
  SECRET_KEY                       Flaskのセッション用キー
  TABLE_NAME (任意, 既定 posts) / BLOB_CONTAINER (任意, 既定 images)
"""
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

JST = timezone(timedelta(hours=9))
CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
TABLE_NAME = os.environ.get("TABLE_NAME", "posts")
CONTAINER = os.environ.get("BLOB_CONTAINER", "images")

SUBJECTS = ["国語", "数学", "英語", "理科", "社会", "情報", "その他"]
# 拡張子 → Content-Type（ブラウザが送る値は信用しない：SVGなどによるXSS対策）
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "gif": "image/gif", "webp": "image/webp"}
ALLOWED_EXT = set(IMAGE_TYPES)
NG_WORDS = ["死ね", "しね", "殺す", "ころす", "きもい", "キモい", "うざい", "ウザい", "バカ", "ばか"]
PERSONAL_PATTERNS = [
    re.compile(r"(?<!\d)0\d{1,4}[-ー－(（]\d{1,4}[-ー－)）]\d{4}(?!\d)"),  # 電話番号（区切りあり）
    re.compile(r"(?<!\d)0\d{9,10}(?!\d)"),                        # 電話番号（10〜11桁）
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                      # メールアドレス
    re.compile(r"(line|ライン)\s*(id|ＩＤ|ID)", re.IGNORECASE),    # LINE ID
    re.compile(r"(住所|じゅうしょ)\s*[:：は]"),                    # 住所
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 画像は5MBまで
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")


# ---------------------------------------------------------------- storage
class AzureStore:
    """Table Storage + Blob Storage に保存する"""

    def __init__(self, conn):
        from azure.data.tables import TableServiceClient
        from azure.storage.blob import BlobServiceClient

        self.table = TableServiceClient.from_connection_string(conn).create_table_if_not_exists(TABLE_NAME)
        self.container = BlobServiceClient.from_connection_string(conn).get_container_client(CONTAINER)
        try:
            self.container.create_container()  # プライベートで作成
        except Exception:
            pass  # 既にある

    def query(self, pk):
        return list(self.table.query_entities("PartitionKey eq @pk", parameters={"pk": pk}))

    def get(self, pk, rk):
        try:
            return dict(self.table.get_entity(pk, rk))
        except Exception:
            return None

    def upsert(self, entity):
        self.table.upsert_entity(entity)

    def delete(self, pk, rk):
        self.table.delete_entity(pk, rk)

    def save_image(self, name, data, content_type):
        from azure.storage.blob import ContentSettings

        self.container.upload_blob(name, data, overwrite=True,
                                   content_settings=ContentSettings(content_type=content_type))

    def load_image(self, name):
        try:
            blob = self.container.get_blob_client(name)
            props = blob.get_blob_properties()
            return blob.download_blob().readall(), props.content_settings.content_type
        except Exception:
            return None, None

    def delete_image(self, name):
        try:
            self.container.delete_blob(name)
        except Exception:
            pass


class MemoryStore:
    """接続文字列がないとき（ローカル確認用）"""

    def __init__(self):
        self.rows, self.images = {}, {}

    def query(self, pk):
        return [dict(v) for (p, _), v in sorted(self.rows.items()) if p == pk]

    def get(self, pk, rk):
        v = self.rows.get((pk, rk))
        return dict(v) if v else None

    def upsert(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def delete(self, pk, rk):
        self.rows.pop((pk, rk), None)

    def save_image(self, name, data, content_type):
        self.images[name] = (data, content_type)

    def load_image(self, name):
        return self.images.get(name, (None, None))

    def delete_image(self, name):
        self.images.pop(name, None)


store = AzureStore(CONN) if CONN else MemoryStore()


# ---------------------------------------------------------------- helpers
def now_str():
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M")


def new_key(newest_first):
    ms = int(time.time() * 1000)
    head = 10**13 - ms if newest_first else ms
    return f"{head:013d}-{uuid.uuid4().hex[:6]}"


def check_text(*texts):
    """問題があればエラーメッセージを返す"""
    joined = " ".join(texts)
    if any(w in joined for w in NG_WORDS):
        return "傷つける言葉が含まれています。言い方を変えてみてください。"
    if any(p.search(joined) for p in PERSONAL_PATTERNS):
        return "電話番号・メールアドレス・LINE ID・住所などの個人情報は書けません。"
    return None


def clean(field, limit):
    return (request.form.get(field) or "").strip()[:limit]


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    subject = request.args.get("subject", "")
    q = request.args.get("q", "").strip()
    unsolved = request.args.get("unsolved") == "1"
    sort = request.args.get("sort", "new")

    posts = store.query("post")
    if subject in SUBJECTS:
        posts = [p for p in posts if p.get("subject") == subject]
    if unsolved:
        posts = [p for p in posts if not p.get("solved")]
    if q:
        posts = [p for p in posts if q in p.get("title", "") or q in p.get("body", "")]
    posts.sort(key=lambda p: p["RowKey"])  # 新しい順
    if sort == "metoo":
        posts.sort(key=lambda p: -int(p.get("metoo", 0)))
    return render_template("index.html", posts=posts, subjects=SUBJECTS, subject=subject,
                           q=q, unsolved=unsolved, sort=sort)


@app.post("/post")
def create_post():
    nickname = clean("nickname", 20) or "名無しの高校生"
    subject = request.form.get("subject")
    title, body = clean("title", 50), clean("body", 1000)
    understood = clean("understood", 300)
    key = clean("delete_key", 20)

    if subject not in SUBJECTS or not title or not body:
        flash("教科・タイトル・本文は必須です。", "error")
        return redirect(url_for("index"))
    if len(key) < 4:
        flash("削除キーは4文字以上にしてください。", "error")
        return redirect(url_for("index"))
    err = check_text(nickname, title, body, understood)
    if err:
        flash(err, "error")
        return redirect(url_for("index"))

    image_name = ""
    f = request.files.get("image")
    if f and f.filename:
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXT:
            flash("画像は jpg / png / gif / webp だけです。", "error")
            return redirect(url_for("index"))
        image_name = f"posts/{uuid.uuid4().hex}.{ext}"
        store.save_image(image_name, f.read(), IMAGE_TYPES[ext])

    store.upsert({
        "PartitionKey": "post", "RowKey": new_key(newest_first=True),
        "nickname": nickname, "subject": subject, "title": title, "body": body,
        "understood": understood, "image": image_name, "metoo": 0, "solved": False,
        "replies": 0, "delete_hash": generate_password_hash(key), "created": now_str(),
    })
    flash("質問を投稿しました！", "ok")
    return redirect(url_for("index"))


def get_post_or_404(pid):
    post = store.get("post", pid)
    if not post:
        abort(404)
    return post


@app.route("/post/<pid>")
def detail(pid):
    post = get_post_or_404(pid)
    replies = sorted(store.query(f"reply-{pid}"), key=lambda r: r["RowKey"])
    return render_template("detail.html", post=post, replies=replies)


@app.post("/post/<pid>/reply")
def reply(pid):
    post = get_post_or_404(pid)
    nickname = clean("nickname", 20) or "名無しの高校生"
    body = clean("body", 1000)
    if not body:
        flash("回答を書いてください。", "error")
        return redirect(url_for("detail", pid=pid))
    err = check_text(nickname, body)
    if err:
        flash(err, "error")
        return redirect(url_for("detail", pid=pid))
    store.upsert({"PartitionKey": f"reply-{pid}", "RowKey": new_key(newest_first=False),
                  "nickname": nickname, "body": body, "created": now_str()})
    post["replies"] = int(post.get("replies", 0)) + 1
    store.upsert(post)
    flash("回答しました。ありがとう！", "ok")
    return redirect(url_for("detail", pid=pid))


@app.post("/post/<pid>/metoo")
def metoo(pid):
    post = get_post_or_404(pid)
    voted = session.get("metoo", [])
    if pid not in voted:  # 同じ人が何回も押せないようにする
        post["metoo"] = int(post.get("metoo", 0)) + 1
        store.upsert(post)
        session["metoo"] = (voted + [pid])[-200:]
    return redirect(url_for("detail", pid=pid))


@app.post("/post/<pid>/solve")
def solve(pid):
    post = get_post_or_404(pid)
    if not check_password_hash(post["delete_hash"], clean("delete_key", 20)):
        flash("削除キーが違います。", "error")
    else:
        post["solved"] = True
        store.upsert(post)
        flash("解決済みにしました！", "ok")
    return redirect(url_for("detail", pid=pid))


@app.post("/post/<pid>/delete")
def delete(pid):
    post = get_post_or_404(pid)
    if not check_password_hash(post["delete_hash"], clean("delete_key", 20)):
        flash("削除キーが違います。", "error")
        return redirect(url_for("detail", pid=pid))
    for r in store.query(f"reply-{pid}"):
        store.delete(r["PartitionKey"], r["RowKey"])
    if post.get("image"):
        store.delete_image(post["image"])
    store.delete("post", pid)
    flash("質問を削除しました。", "ok")
    return redirect(url_for("index"))


@app.route("/image/<path:name>")
def image(name):
    """Blob Storage の画像を表示する（コンテナは非公開のまま）"""
    if ".." in name or not re.fullmatch(r"[\w\-./]+", name):
        abort(404)
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in IMAGE_TYPES:
        abort(404)
    data, _ = store.load_image(name)
    if data is None:
        abort(404)
    return Response(data, mimetype=IMAGE_TYPES[ext],
                    headers={"Cache-Control": "public, max-age=86400",
                             "X-Content-Type-Options": "nosniff"})


@app.route("/about")
def about():
    return render_template("about.html")


@app.errorhandler(413)
def too_large(_):
    flash("画像が大きすぎます（5MBまで）。", "error")
    return redirect(url_for("index"))


@app.context_processor
def inject():
    return {"storage_mode": "Azure" if CONN else "メモリ（ローカル）"}


if __name__ == "__main__":
    app.run(debug=True)
