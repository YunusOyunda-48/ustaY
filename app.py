import os
import sqlite3
import uuid
from pathlib import Path
from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "mods.db"
UPLOAD_DIR = BASE_DIR / "storage" / "mod_files"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            steam_base_path TEXT NOT NULL,
            stplugin_relative TEXT NOT NULL,
            depotcache_relative TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            target_type TEXT NOT NULL CHECK(target_type IN ('stplugin', 'depotcache')),
            target_subdir TEXT,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        );
        """
    )

    exists = conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
    if not exists:
        conn.execute(
            """
            INSERT INTO settings (id, steam_base_path, stplugin_relative, depotcache_relative)
            VALUES (1, ?, ?, ?)
            """,
            (
                r"C:\Program Files (x86)\Steam\config",
                "stplug-in",
                "depotcache",
            ),
        )

    conn.commit()
    conn.close()


init_db()


@app.route("/")
def index():
    conn = get_conn()
    games = conn.execute("SELECT id, name FROM games ORDER BY name").fetchall()
    selected_game = request.args.get("game_id", type=int)
    mods = []
    if selected_game:
        mods = conn.execute(
            """
            SELECT id, name, original_filename, target_type, COALESCE(target_subdir, '') AS target_subdir
            FROM mods
            WHERE game_id = ?
            ORDER BY name
            """,
            (selected_game,),
        ).fetchall()

    conn.close()
    return render_template("index.html", games=games, mods=mods, selected_game=selected_game)


@app.post("/install")
def install_mods():
    game_id = request.form.get("game_id", type=int)
    selected_mod_ids = request.form.getlist("mod_ids")

    if not game_id or not selected_mod_ids:
        flash("Lütfen bir oyun ve en az bir mod seçin.", "warning")
        return redirect(url_for("index", game_id=game_id))

    conn = get_conn()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    mods = conn.execute(
        f"SELECT * FROM mods WHERE game_id = ? AND id IN ({','.join(['?'] * len(selected_mod_ids))})",
        (game_id, *selected_mod_ids),
    ).fetchall()

    if not mods:
        conn.close()
        flash("Seçilen oyun için geçerli mod bulunamadı.", "danger")
        return redirect(url_for("index", game_id=game_id))

    steam_base = Path(settings["steam_base_path"])
    stplugin_root = steam_base / settings["stplugin_relative"]
    depotcache_root = steam_base / settings["depotcache_relative"]

    copied, errors = 0, []
    for mod in mods:
        src = UPLOAD_DIR / mod["filename"]
        base_target = stplugin_root if mod["target_type"] == "stplugin" else depotcache_root
        target_dir = base_target / (mod["target_subdir"] or "")
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / mod["original_filename"]

        try:
            if not src.exists():
                raise FileNotFoundError(f"Kaynak dosya bulunamadı: {src}")

            with src.open("rb") as fsrc, target_path.open("wb") as fdst:
                fdst.write(fsrc.read())
            copied += 1
        except Exception as exc:
            errors.append(f"{mod['name']}: {exc}")

    conn.close()

    if copied:
        flash(f"{copied} mod başarıyla kopyalandı.", "success")
    if errors:
        flash("Hatalar: " + " | ".join(errors), "danger")

    return redirect(url_for("index", game_id=game_id))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    admin_token = os.environ.get("ADMIN_TOKEN", "admin123")
    if request.args.get("token") != admin_token:
        return "Yetkisiz erişim. /admin?token=... kullanın.", 403

    conn = get_conn()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_game":
            name = (request.form.get("name") or "").strip()
            if name:
                try:
                    conn.execute("INSERT INTO games(name) VALUES (?)", (name,))
                    conn.commit()
                    flash("Oyun eklendi.", "success")
                except sqlite3.IntegrityError:
                    flash("Bu oyun zaten var.", "warning")

        elif action == "add_mod":
            game_id = request.form.get("game_id", type=int)
            mod_name = (request.form.get("mod_name") or "").strip()
            target_type = request.form.get("target_type")
            target_subdir = (request.form.get("target_subdir") or "").strip()
            file = request.files.get("file")

            if not (game_id and mod_name and file and target_type in {"stplugin", "depotcache"}):
                flash("Mod eklemek için tüm zorunlu alanları doldurun.", "danger")
            else:
                original_filename = secure_filename(file.filename)
                saved_name = f"{uuid.uuid4().hex}_{original_filename}"
                save_path = UPLOAD_DIR / saved_name
                file.save(save_path)

                conn.execute(
                    """
                    INSERT INTO mods(game_id, name, filename, original_filename, target_type, target_subdir)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (game_id, mod_name, saved_name, original_filename, target_type, target_subdir),
                )
                conn.commit()
                flash("Mod eklendi.", "success")

        elif action == "save_settings":
            steam_base = (request.form.get("steam_base_path") or "").strip()
            stplugin_rel = (request.form.get("stplugin_relative") or "").strip()
            depotcache_rel = (request.form.get("depotcache_relative") or "").strip()

            if steam_base and stplugin_rel and depotcache_rel:
                conn.execute(
                    """
                    UPDATE settings
                    SET steam_base_path = ?, stplugin_relative = ?, depotcache_relative = ?
                    WHERE id = 1
                    """,
                    (steam_base, stplugin_rel, depotcache_rel),
                )
                conn.commit()
                flash("Ayarlar kaydedildi.", "success")

        return redirect(url_for("admin", token=admin_token))

    games = conn.execute("SELECT id, name FROM games ORDER BY name").fetchall()
    mods = conn.execute(
        """
        SELECT m.id, m.name, g.name AS game_name, m.target_type, COALESCE(m.target_subdir, '') AS target_subdir
        FROM mods m
        JOIN games g ON g.id = m.game_id
        ORDER BY g.name, m.name
        """
    ).fetchall()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()

    return render_template("admin.html", games=games, mods=mods, settings=settings, token=admin_token)


@app.route("/files/<path:filename>")
def files(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
