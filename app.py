import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, render_template, request, redirect, url_for, flash, session, abort
from werkzeug.security import generate_password_hash, check_password_hash

JST = timezone(timedelta(hours=9))
CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
TABLE_NAME = os.environ.get("TABLE_NAME", "posts")
CONTAINER_NAME = os.environ.get("BLOB_CONTAINER", "images")

SUBJECTS = ["国語", "数学", "英語", "理科", "社会", "情報", "その他"]
DEFAULT_NICKNAME = "名無しの高校生"

# content type is decided from the extension, never from what the browser sends
IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

NG_WORDS = ["死ね", "しね", "殺す", "ころす", "きもい", "キモい", "うざい", "ウザい", "バカ", "ばか"]
PERSONAL_INFO_REGEXES = [
    re.compile(r"(?<!\d)0\d{1,4}[-ー－(（]\d{1,4}[-ー－)）]\d{4}(?!\d)"),  # phone with separators
    re.compile(r"(?<!\d)0\d{9,10}(?!\d)"),  # phone without separators
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email
    re.compile(r"(line|ライン)\s*(id|ＩＤ|ID)", re.IGNORECASE),  # LINE ID
    re.compile(r"(住所|じゅうしょ)\s*[:：は]"),  # address
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB max per upload


# Storage

class AzureStore:
    # posts and replies in Table Storage, images in Blob Storage

    def __init__(self, connection_string):
        from azure.data.tables import TableServiceClient
        from azure.storage.blob import BlobServiceClient

        table_service = TableServiceClient.from_connection_string(connection_string)
        self.table = table_service.create_table_if_not_exists(TABLE_NAME)

        blob_service = BlobServiceClient.from_connection_string(connection_string)
        self.container = blob_service.get_container_client(CONTAINER_NAME)
        if not self.container.exists():
            self.container.create_container()  # private by default

    def query(self, partition):
        return list(self.table.query_entities("PartitionKey eq @pk", parameters={"pk": partition}))

    def get(self, partition, row):
        try:
            return dict(self.table.get_entity(partition, row))
        except Exception:
            return None

    def upsert(self, entity):
        self.table.upsert_entity(entity)

    def delete(self, partition, row):
        self.table.delete_entity(partition, row)

    def save_image(self, name, data, content_type):
        from azure.storage.blob import ContentSettings

        settings = ContentSettings(content_type=content_type)
        self.container.upload_blob(name, data, overwrite=True, content_settings=settings)

    def load_image(self, name):
        try:
            return self.container.download_blob(name).readall()
        except Exception:
            return None

    def delete_image(self, name):
        try:
            self.container.delete_blob(name)
        except Exception:
            pass


class MemoryStore:
    # used locally when there is no connection string (data is lost on restart)

    def __init__(self):
        self.rows = {}
        self.images = {}

    def query(self, partition):
        return [dict(entity) for (pk, _), entity in sorted(self.rows.items()) if pk == partition]

    def get(self, partition, row):
        entity = self.rows.get((partition, row))
        return dict(entity) if entity else None

    def upsert(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def delete(self, partition, row):
        self.rows.pop((partition, row), None)

    def save_image(self, name, data, content_type):
        self.images[name] = data

    def load_image(self, name):
        return self.images.get(name)

    def delete_image(self, name):
        self.images.pop(name, None)


store = AzureStore(CONNECTION_STRING) if CONNECTION_STRING else MemoryStore()


# Helpers

def now_text():
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M")


def make_row_key(newest_first):
    # Table Storage sorts by RowKey, so a reversed timestamp puts new posts first
    millis = int(time.time() * 1000)
    number = 10**13 - millis if newest_first else millis
    return f"{number:013d}-{uuid.uuid4().hex[:6]}"


def check_text(*texts):
    all_text = " ".join(texts)
    if any(word in all_text for word in NG_WORDS):
        return "傷つける言葉が含まれています。言い方を変えてみてください。"
    if any(regex.search(all_text) for regex in PERSONAL_INFO_REGEXES):
        return "電話番号・メールアドレス・LINE ID・住所などの個人情報は書けません。"
    return None


def form_value(field, max_length):
    return (request.form.get(field) or "").strip()[:max_length]


def get_post_or_404(post_id):
    post = store.get("post", post_id)
    if post is None:
        abort(404)
    return post


# Routes

@app.route("/")
def index():
    subject = request.args.get("subject", "")
    search = request.args.get("q", "").strip()
    unsolved = request.args.get("unsolved") == "1"
    sort = request.args.get("sort", "new")

    posts = store.query("post")
    if subject in SUBJECTS:
        posts = [post for post in posts if post.get("subject") == subject]
    if unsolved:
        posts = [post for post in posts if not post.get("solved")]
    if search:
        posts = [post for post in posts if search in post.get("title", "") or search in post.get("body", "")]

    posts.sort(key=lambda post: post["RowKey"])
    if sort == "metoo":
        posts.sort(key=lambda post: int(post.get("metoo", 0)), reverse=True)

    return render_template("index.html", posts=posts, subjects=SUBJECTS, subject=subject,
                           q=search, unsolved=unsolved, sort=sort)


@app.route("/post", methods=["POST"])
def create_post():
    nickname = form_value("nickname", 20) or DEFAULT_NICKNAME
    subject = request.form.get("subject")
    title = form_value("title", 50)
    body = form_value("body", 1000)
    understood = form_value("understood", 300)
    delete_key = form_value("delete_key", 20)

    if subject not in SUBJECTS or not title or not body:
        flash("教科・タイトル・本文は必須です。", "error")
        return redirect(url_for("index"))
    if len(delete_key) < 4:
        flash("削除キーは4文字以上にしてください。", "error")
        return redirect(url_for("index"))
    error = check_text(nickname, title, body, understood)
    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    image_name = ""
    uploaded_file = request.files.get("image")
    if uploaded_file and uploaded_file.filename:
        extension = uploaded_file.filename.rsplit(".", 1)[-1].lower() if "." in uploaded_file.filename else ""
        if extension not in IMAGE_TYPES:
            flash("画像は jpg / png / gif / webp だけです。", "error")
            return redirect(url_for("index"))
        image_name = f"posts/{uuid.uuid4().hex}.{extension}"
        store.save_image(image_name, uploaded_file.read(), IMAGE_TYPES[extension])

    store.upsert({
        "PartitionKey": "post",
        "RowKey": make_row_key(newest_first=True),
        "nickname": nickname,
        "subject": subject,
        "title": title,
        "body": body,
        "understood": understood,
        "image": image_name,
        "metoo": 0,
        "solved": False,
        "replies": 0,
        "delete_hash": generate_password_hash(delete_key),
        "created": now_text(),
    })
    flash("質問を投稿しました！", "ok")
    return redirect(url_for("index"))


@app.route("/post/<post_id>")
def detail(post_id):
    post = get_post_or_404(post_id)
    replies = sorted(store.query(f"reply-{post_id}"), key=lambda reply: reply["RowKey"])
    return render_template("detail.html", post=post, replies=replies)


@app.route("/post/<post_id>/reply", methods=["POST"])
def reply(post_id):
    post = get_post_or_404(post_id)
    nickname = form_value("nickname", 20) or DEFAULT_NICKNAME
    body = form_value("body", 1000)

    if not body:
        flash("回答を書いてください。", "error")
        return redirect(url_for("detail", post_id=post_id))
    error = check_text(nickname, body)
    if error:
        flash(error, "error")
        return redirect(url_for("detail", post_id=post_id))

    store.upsert({
        "PartitionKey": f"reply-{post_id}",
        "RowKey": make_row_key(newest_first=False),
        "nickname": nickname,
        "body": body,
        "created": now_text(),
    })
    post["replies"] = int(post.get("replies", 0)) + 1
    store.upsert(post)
    flash("回答しました。ありがとう！", "ok")
    return redirect(url_for("detail", post_id=post_id))


@app.route("/post/<post_id>/metoo", methods=["POST"])
def metoo(post_id):
    post = get_post_or_404(post_id)
    voted = session.get("metoo", [])
    # one vote per person (kept in the session cookie)
    if post_id not in voted:
        post["metoo"] = int(post.get("metoo", 0)) + 1
        store.upsert(post)
        session["metoo"] = (voted + [post_id])[-200:]
    return redirect(url_for("detail", post_id=post_id))


@app.route("/post/<post_id>/solve", methods=["POST"])
def solve(post_id):
    post = get_post_or_404(post_id)
    if not check_password_hash(post["delete_hash"], form_value("delete_key", 20)):
        flash("削除キーが違います。", "error")
    else:
        post["solved"] = True
        store.upsert(post)
        flash("解決済みにしました！", "ok")
    return redirect(url_for("detail", post_id=post_id))


@app.route("/post/<post_id>/delete", methods=["POST"])
def delete(post_id):
    post = get_post_or_404(post_id)
    if not check_password_hash(post["delete_hash"], form_value("delete_key", 20)):
        flash("削除キーが違います。", "error")
        return redirect(url_for("detail", post_id=post_id))

    for reply in store.query(f"reply-{post_id}"):
        store.delete(reply["PartitionKey"], reply["RowKey"])
    if post.get("image"):
        store.delete_image(post["image"])
    store.delete("post", post_id)
    flash("質問を削除しました。", "ok")
    return redirect(url_for("index"))


@app.route("/image/<path:name>")
def image(name):
    # images go through Flask so the blob container can stay private
    if ".." in name or not re.fullmatch(r"[\w\-./]+", name):
        abort(404)
    extension = name.rsplit(".", 1)[-1].lower()
    if extension not in IMAGE_TYPES:
        abort(404)
    data = store.load_image(name)
    if data is None:
        abort(404)
    headers = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
    return Response(data, mimetype=IMAGE_TYPES[extension], headers=headers)


@app.route("/about")
def about():
    return render_template("about.html")


@app.errorhandler(413)
def file_too_large(error):
    flash("画像が大きすぎます（5MBまで）。", "error")
    return redirect(url_for("index"))


@app.context_processor
def storage_info():
    return {"storage_mode": "Azure" if CONNECTION_STRING else "メモリ（ローカル）"}


if __name__ == "__main__":
    app.run(debug=True)
