import csv
import io
import json
import os
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, Response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

from database import (
    get_db, init_db, get_team_config, update_team_config,
    get_player_full, get_eval_averages, calculate_season_averages,
    seed_players, seed_default_user, verify_user, get_user_by_id, create_user,
    ZONE_NAMES,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "courtiq-dev-key")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access CourtIQ."
login_manager.login_message_category = "warning"

import sys
print(f"Starting CourtIQ... DATABASE_URL set: {bool(os.environ.get('DATABASE_URL'))}", flush=True)
print(f"PORT: {os.environ.get('PORT', 'not set')}", flush=True)

try:
    init_db()
    print("Tables created.", flush=True)
    seed_players()
    print("Players seeded.", flush=True)
    seed_default_user()
    print("Default user created.", flush=True)
    print("Database initialized successfully.", flush=True)
except Exception as e:
    import traceback
    print(f"DATABASE INIT ERROR: {e}", flush=True)
    traceback.print_exc()
    print("App will start without database.", flush=True)


class User(UserMixin):
    def __init__(self, user_dict):
        self.id = user_dict["id"]
        self.username = user_dict["username"]


@login_manager.user_loader
def load_user(user_id):
    data = get_user_by_id(int(user_id))
    if data:
        return User(data)
    return None

POSITIONS = ["PG", "SG", "SF", "PF", "C"]
STATUSES = ["active", "injured", "inactive"]
GOAL_CATEGORIES = ["shooting", "conditioning", "skills", "defense", "rebounding", "passing", "other"]


def height_to_display(inches):
    if not inches:
        return "—"
    return f"{inches // 12}'{inches % 12}\""


@app.context_processor
def inject_globals():
    return {
        "positions": POSITIONS,
        "statuses": STATUSES,
        "goal_categories": GOAL_CATEGORIES,
        "zone_names": ZONE_NAMES,
        "height_to_display": height_to_display,
        "team_config": get_team_config(),
    }


# ── Auth ──────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return "OK", 200


@app.before_request
def require_login():
    allowed = ("login", "static", "health")
    if request.endpoint and request.endpoint not in allowed and not current_user.is_authenticated:
        return redirect(url_for("login", next=request.url))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_data = verify_user(username, password)
        if user_data:
            login_user(User(user_data), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        user_data = verify_user(current_user.username, current_pw)
        if not user_data:
            flash("Current password is incorrect.", "danger")
        elif len(new_pw) < 4:
            flash("New password must be at least 4 characters.", "danger")
        elif new_pw != confirm_pw:
            flash("New passwords don't match.", "danger")
        else:
            from database import _hash_pw
            conn = get_db()
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (_hash_pw(new_pw), current_user.id))
            conn.commit()
            conn.close()
            flash("Password changed successfully.", "success")
            return redirect(url_for("team_settings"))

    return render_template("change_password.html")


# ── Dashboard ─────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    conn = get_db()
    config = get_team_config()
    season = config["season"]

    total = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM players WHERE status = 'active'").fetchone()[0]

    by_position = conn.execute(
        "SELECT position, COUNT(*) as cnt FROM players WHERE status = 'active' GROUP BY position ORDER BY position"
    ).fetchall()

    team_avgs = conn.execute("""
        SELECT ROUND(AVG(sa.ppg), 1) as ppg, ROUND(AVG(sa.rpg), 1) as rpg,
               ROUND(AVG(sa.apg), 1) as apg, ROUND(AVG(sa.fg_pct), 1) as fg_pct,
               ROUND(AVG(sa.three_pct), 1) as three_pct, ROUND(AVG(sa.ft_pct), 1) as ft_pct
        FROM season_averages sa
        JOIN players p ON p.id = sa.player_id
        WHERE p.status = 'active' AND sa.season = ?
    """, (season,)).fetchone()

    recent_evals = conn.execute("""
        SELECT ce.*, p.first_name, p.last_name
        FROM coach_evaluations ce
        JOIN players p ON p.id = ce.player_id
        ORDER BY ce.created_at DESC LIMIT 5
    """).fetchall()

    goal_counts = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM development_goals GROUP BY status"
    ).fetchall()

    roster = conn.execute("""
        SELECT p.*, sa.ppg, sa.rpg, sa.apg
        FROM players p
        LEFT JOIN season_averages sa ON sa.player_id = p.id AND sa.season = ?
        WHERE p.status = 'active'
        ORDER BY p.last_name, p.first_name
    """, (season,)).fetchall()

    conn.close()
    return render_template("index.html",
        total=total, active=active,
        by_position=[dict(r) for r in by_position],
        team_avgs=dict(team_avgs) if team_avgs else {},
        recent_evals=[dict(r) for r in recent_evals],
        goal_counts={r["status"]: r["cnt"] for r in goal_counts},
        roster=[dict(r) for r in roster],
    )


# ── Player List ───────────────────────────────────────────────────
@app.route("/players")
def players_list():
    query = request.args.get("q", "")
    position = request.args.get("position", "")
    status = request.args.get("status", "")
    sort_by = request.args.get("sort", "name")

    config = get_team_config()
    conn = get_db()

    sql = """
        SELECT p.*, sa.ppg, sa.rpg, sa.apg
        FROM players p
        LEFT JOIN season_averages sa ON sa.player_id = p.id AND sa.season = ?
        WHERE 1=1
    """
    params = [config["season"]]

    if query:
        sql += " AND (p.first_name LIKE ? OR p.last_name LIKE ?)"
        q = f"%{query}%"
        params.extend([q, q])
    if position:
        sql += " AND p.position = ?"
        params.append(position)
    if status:
        sql += " AND p.status = ?"
        params.append(status)

    sort_map = {
        "name": "p.last_name, p.first_name",
        "position": "p.position, p.last_name",
        "year": "p.grad_year, p.last_name",
        "number": "p.jersey_number, p.last_name",
    }
    sql += f" ORDER BY {sort_map.get(sort_by, 'p.last_name, p.first_name')}"

    players = conn.execute(sql, params).fetchall()
    conn.close()

    return render_template("players.html",
        players=[dict(p) for p in players],
        query=query, position=position, status=status, sort_by=sort_by,
    )


# ── Add Player ────────────────────────────────────────────────────
@app.route("/players/add", methods=["GET", "POST"])
def add_player():
    if request.method == "POST":
        conn = get_db()
        f = request.form
        config = get_team_config()

        cur = conn.execute("""
            INSERT INTO players (first_name, last_name, position, height_inches, weight,
                                 wingspan_inches, jersey_number, grad_year, gpa, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f["first_name"], f["last_name"], f["position"],
            int(f["height_inches"]),
            int(f["weight"]) if f.get("weight") else None,
            int(f["wingspan_inches"]) if f.get("wingspan_inches") else None,
            int(f["jersey_number"]) if f.get("jersey_number") else None,
            int(f["grad_year"]),
            float(f["gpa"]) if f.get("gpa") else None,
            f.get("status", "active"),
        ))
        player_id = cur.lastrowid

        conn.execute("""
            INSERT INTO season_averages (player_id, season, games_played, mpg, ppg, rpg, apg, spg, bpg,
                                         fg_pct, three_pct, ft_pct, topg, is_manual)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            player_id, config["season"],
            int(f["games_played"]) if f.get("games_played") else 0,
            float(f["mpg"]) if f.get("mpg") else 0,
            float(f["ppg"]) if f.get("ppg") else 0,
            float(f["rpg"]) if f.get("rpg") else 0,
            float(f["apg"]) if f.get("apg") else 0,
            float(f["spg"]) if f.get("spg") else 0,
            float(f["bpg"]) if f.get("bpg") else 0,
            float(f["fg_pct"]) if f.get("fg_pct") else 0,
            float(f["three_pct"]) if f.get("three_pct") else 0,
            float(f["ft_pct"]) if f.get("ft_pct") else 0,
            float(f["topg"]) if f.get("topg") else 0,
        ))

        conn.commit()
        conn.close()
        flash(f"Added {f['first_name']} {f['last_name']} to the roster.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    return render_template("add_player.html")


# ── Player Detail ─────────────────────────────────────────────────
@app.route("/players/<int:player_id>")
def player_detail(player_id):
    data = get_player_full(player_id)
    if not data:
        return "Player not found", 404
    return render_template("player_detail.html", data=data)


# ── Edit Player ───────────────────────────────────────────────────
@app.route("/players/<int:player_id>/edit", methods=["GET", "POST"])
def edit_player(player_id):
    if request.method == "POST":
        conn = get_db()
        f = request.form
        config = get_team_config()

        conn.execute("""
            UPDATE players SET first_name=?, last_name=?, position=?, height_inches=?,
            weight=?, wingspan_inches=?, jersey_number=?, grad_year=?, gpa=?, status=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (
            f["first_name"], f["last_name"], f["position"],
            int(f["height_inches"]),
            int(f["weight"]) if f.get("weight") else None,
            int(f["wingspan_inches"]) if f.get("wingspan_inches") else None,
            int(f["jersey_number"]) if f.get("jersey_number") else None,
            int(f["grad_year"]),
            float(f["gpa"]) if f.get("gpa") else None,
            f.get("status", "active"),
            player_id,
        ))

        existing = conn.execute(
            "SELECT id, is_manual FROM season_averages WHERE player_id = ? AND season = ? ORDER BY id DESC LIMIT 1",
            (player_id, config["season"]),
        ).fetchone()

        if existing and existing["is_manual"]:
            conn.execute("""
                UPDATE season_averages SET games_played=?, mpg=?, ppg=?, rpg=?, apg=?, spg=?, bpg=?,
                fg_pct=?, three_pct=?, ft_pct=?, topg=? WHERE id=?
            """, (
                int(f["games_played"]) if f.get("games_played") else 0,
                float(f["mpg"]) if f.get("mpg") else 0,
                float(f["ppg"]) if f.get("ppg") else 0,
                float(f["rpg"]) if f.get("rpg") else 0,
                float(f["apg"]) if f.get("apg") else 0,
                float(f["spg"]) if f.get("spg") else 0,
                float(f["bpg"]) if f.get("bpg") else 0,
                float(f["fg_pct"]) if f.get("fg_pct") else 0,
                float(f["three_pct"]) if f.get("three_pct") else 0,
                float(f["ft_pct"]) if f.get("ft_pct") else 0,
                float(f["topg"]) if f.get("topg") else 0,
                existing["id"],
            ))

        conn.commit()
        conn.close()
        flash("Player updated.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    data = get_player_full(player_id)
    if not data:
        return "Player not found", 404
    return render_template("edit_player.html", data=data)


# ── Delete Player ─────────────────────────────────────────────────
@app.route("/players/<int:player_id>/delete", methods=["POST"])
def delete_player(player_id):
    conn = get_db()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    flash("Player removed from roster.", "info")
    return redirect(url_for("players_list"))


# ── Coach Evaluation ──────────────────────────────────────────────
@app.route("/players/<int:player_id>/evaluate", methods=["GET", "POST"])
def evaluate_player(player_id):
    if request.method == "POST":
        conn = get_db()
        f = request.form

        coach_name = f.get("coach_name", "").strip()
        if not coach_name:
            flash("Coach name is required.", "danger")
            return redirect(url_for("evaluate_player", player_id=player_id))

        from datetime import date as _date
        today = _date.today().isoformat()
        conn.execute("""
            INSERT INTO coach_evaluations (
                player_id, coach_name, eval_date,
                speed, vertical, agility, strength, endurance,
                ball_handling, passing, shooting_form, post_moves, off_ball_movement, transition_play,
                basketball_iq, motor, coachability, leadership, clutch, defensive_instincts,
                notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, coach_name, today,
            int(f.get("speed", 5)), int(f.get("vertical", 5)),
            int(f.get("agility", 5)), int(f.get("strength", 5)),
            int(f.get("endurance", 5)),
            int(f.get("ball_handling", 5)), int(f.get("passing", 5)),
            int(f.get("shooting_form", 5)), int(f.get("post_moves", 5)),
            int(f.get("off_ball_movement", 5)), int(f.get("transition_play", 5)),
            int(f.get("basketball_iq", 5)), int(f.get("motor", 5)),
            int(f.get("coachability", 5)), int(f.get("leadership", 5)),
            int(f.get("clutch", 5)), int(f.get("defensive_instincts", 5)),
            f.get("notes", "").strip() or None,
        ))
        conn.commit()
        conn.close()
        flash(f"Evaluation by {coach_name} submitted.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    data = get_player_full(player_id)
    if not data:
        return "Player not found", 404
    return render_template("evaluate.html", data=data)


@app.route("/evaluations/<int:eval_id>/delete", methods=["POST"])
def delete_evaluation(eval_id):
    conn = get_db()
    ev = conn.execute("SELECT player_id FROM coach_evaluations WHERE id = ?", (eval_id,)).fetchone()
    if ev:
        conn.execute("DELETE FROM coach_evaluations WHERE id = ?", (eval_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("player_detail", player_id=ev["player_id"]))
    conn.close()
    return "Not found", 404


# ── Game Logs ─────────────────────────────────────────────────────
@app.route("/players/<int:player_id>/game-log", methods=["GET", "POST"])
def add_game_log(player_id):
    if request.method == "POST":
        conn = get_db()
        f = request.form
        config = get_team_config()

        conn.execute("""
            INSERT INTO game_logs (player_id, game_date, opponent, result, minutes, points,
                                   rebounds, assists, steals, blocks, fg_made, fg_attempted,
                                   three_made, three_attempted, ft_made, ft_attempted, turnovers, fouls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, f["game_date"], f["opponent"], f.get("result"),
            int(f.get("minutes", 0) or 0), int(f.get("points", 0) or 0),
            int(f.get("rebounds", 0) or 0), int(f.get("assists", 0) or 0),
            int(f.get("steals", 0) or 0), int(f.get("blocks", 0) or 0),
            int(f.get("fg_made", 0) or 0), int(f.get("fg_attempted", 0) or 0),
            int(f.get("three_made", 0) or 0), int(f.get("three_attempted", 0) or 0),
            int(f.get("ft_made", 0) or 0), int(f.get("ft_attempted", 0) or 0),
            int(f.get("turnovers", 0) or 0), int(f.get("fouls", 0) or 0),
        ))
        conn.commit()
        calculate_season_averages(player_id, config["season"], conn)
        conn.close()
        flash("Game log added.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    conn.close()
    if not player:
        return "Player not found", 404
    return render_template("game_log.html", player=dict(player), log=None)


@app.route("/players/<int:player_id>/game-log/<int:log_id>/edit", methods=["GET", "POST"])
def edit_game_log(player_id, log_id):
    if request.method == "POST":
        conn = get_db()
        f = request.form
        config = get_team_config()

        conn.execute("""
            UPDATE game_logs SET game_date=?, opponent=?, result=?, minutes=?, points=?,
            rebounds=?, assists=?, steals=?, blocks=?, fg_made=?, fg_attempted=?,
            three_made=?, three_attempted=?, ft_made=?, ft_attempted=?, turnovers=?, fouls=?
            WHERE id=?
        """, (
            f["game_date"], f["opponent"], f.get("result"),
            int(f.get("minutes", 0) or 0), int(f.get("points", 0) or 0),
            int(f.get("rebounds", 0) or 0), int(f.get("assists", 0) or 0),
            int(f.get("steals", 0) or 0), int(f.get("blocks", 0) or 0),
            int(f.get("fg_made", 0) or 0), int(f.get("fg_attempted", 0) or 0),
            int(f.get("three_made", 0) or 0), int(f.get("three_attempted", 0) or 0),
            int(f.get("ft_made", 0) or 0), int(f.get("ft_attempted", 0) or 0),
            int(f.get("turnovers", 0) or 0), int(f.get("fouls", 0) or 0),
            log_id,
        ))
        conn.commit()
        calculate_season_averages(player_id, config["season"], conn)
        conn.close()
        flash("Game log updated.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    log = conn.execute("SELECT * FROM game_logs WHERE id = ?", (log_id,)).fetchone()
    conn.close()
    if not player or not log:
        return "Not found", 404
    return render_template("game_log.html", player=dict(player), log=dict(log))


@app.route("/game-logs/<int:log_id>/delete", methods=["POST"])
def delete_game_log(log_id):
    conn = get_db()
    log = conn.execute("SELECT player_id FROM game_logs WHERE id = ?", (log_id,)).fetchone()
    if log:
        pid = log["player_id"]
        conn.execute("DELETE FROM game_logs WHERE id = ?", (log_id,))
        conn.commit()
        config = get_team_config()
        calculate_season_averages(pid, config["season"], conn)
        conn.close()
        return redirect(url_for("player_detail", player_id=pid))
    conn.close()
    return "Not found", 404


# ── Shot Chart ────────────────────────────────────────────────────
@app.route("/players/<int:player_id>/shot-chart")
def shot_chart_page(player_id):
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        return "Player not found", 404

    config = get_team_config()
    season = config["season"]

    zones = conn.execute(
        "SELECT * FROM shot_charts WHERE player_id = ? AND season = ? ORDER BY zone_id",
        (player_id, season),
    ).fetchall()
    zones = [dict(z) for z in zones]

    existing_ids = {z["zone_id"] for z in zones}
    for zid, zname in ZONE_NAMES.items():
        if zid not in existing_ids:
            zones.append({"zone_id": zid, "zone_name": zname, "fg_made": 0, "fg_attempted": 0})
    zones.sort(key=lambda z: z["zone_id"])

    conn.close()
    return render_template("shot_chart.html", player=dict(player), zones=zones, season=season)


@app.route("/players/<int:player_id>/shot-chart/update", methods=["POST"])
def update_shot_chart(player_id):
    conn = get_db()
    f = request.form
    config = get_team_config()
    season = config["season"]
    zone_id = int(f["zone_id"])
    fg_made = int(f.get("fg_made", 0) or 0)
    fg_attempted = int(f.get("fg_attempted", 0) or 0)
    zone_name = ZONE_NAMES.get(zone_id, "Unknown")

    conn.execute("""
        INSERT INTO shot_charts (player_id, season, zone_id, zone_name, fg_made, fg_attempted)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id, season, zone_id)
        DO UPDATE SET fg_made=?, fg_attempted=?
    """, (player_id, season, zone_id, zone_name, fg_made, fg_attempted, fg_made, fg_attempted))

    conn.commit()
    conn.close()
    return redirect(url_for("shot_chart_page", player_id=player_id))


# ── Conditioning ──────────────────────────────────────────────────
@app.route("/players/<int:player_id>/conditioning", methods=["GET", "POST"])
def add_conditioning(player_id):
    if request.method == "POST":
        conn = get_db()
        f = request.form
        conn.execute("""
            INSERT INTO conditioning_logs (player_id, log_date, mile_time_sec, shuttle_sec,
                                           vertical_inches, broad_jump_inches, bench_reps, body_weight)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, f["log_date"],
            float(f["mile_time_sec"]) if f.get("mile_time_sec") else None,
            float(f["shuttle_sec"]) if f.get("shuttle_sec") else None,
            float(f["vertical_inches"]) if f.get("vertical_inches") else None,
            float(f["broad_jump_inches"]) if f.get("broad_jump_inches") else None,
            int(f["bench_reps"]) if f.get("bench_reps") else None,
            float(f["body_weight"]) if f.get("body_weight") else None,
        ))
        conn.commit()
        conn.close()
        flash("Conditioning benchmark logged.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    conn.close()
    if not player:
        return "Player not found", 404
    return render_template("conditioning.html", player=dict(player))


@app.route("/conditioning/<int:log_id>/delete", methods=["POST"])
def delete_conditioning(log_id):
    conn = get_db()
    log = conn.execute("SELECT player_id FROM conditioning_logs WHERE id = ?", (log_id,)).fetchone()
    if log:
        conn.execute("DELETE FROM conditioning_logs WHERE id = ?", (log_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("player_detail", player_id=log["player_id"]))
    conn.close()
    return "Not found", 404


# ── Development Goals ─────────────────────────────────────────────
@app.route("/players/<int:player_id>/goals", methods=["GET", "POST"])
def manage_goals(player_id):
    if request.method == "POST":
        conn = get_db()
        f = request.form
        conn.execute("""
            INSERT INTO development_goals (player_id, category, description, target_value, current_value, unit, deadline)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            player_id, f["category"], f["description"],
            float(f["target_value"]) if f.get("target_value") else None,
            float(f["current_value"]) if f.get("current_value") else 0,
            f.get("unit", ""),
            f.get("deadline") or None,
        ))
        conn.commit()
        conn.close()
        flash("Goal added.", "success")
        return redirect(url_for("manage_goals", player_id=player_id))

    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    goals = conn.execute(
        "SELECT * FROM development_goals WHERE player_id = ? ORDER BY created_at DESC", (player_id,),
    ).fetchall()
    conn.close()
    if not player:
        return "Player not found", 404
    return render_template("goals.html", player=dict(player), goals=[dict(g) for g in goals])


@app.route("/goals/<int:goal_id>/update", methods=["POST"])
def update_goal(goal_id):
    conn = get_db()
    f = request.form
    goal = conn.execute("SELECT player_id FROM development_goals WHERE id = ?", (goal_id,)).fetchone()
    if goal:
        conn.execute(
            "UPDATE development_goals SET current_value=?, status=? WHERE id=?",
            (float(f.get("current_value", 0)), f.get("status", "in_progress"), goal_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("manage_goals", player_id=goal["player_id"]))
    conn.close()
    return "Not found", 404


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
def delete_goal(goal_id):
    conn = get_db()
    goal = conn.execute("SELECT player_id FROM development_goals WHERE id = ?", (goal_id,)).fetchone()
    if goal:
        conn.execute("DELETE FROM development_goals WHERE id = ?", (goal_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("manage_goals", player_id=goal["player_id"]))
    conn.close()
    return "Not found", 404


# ── Depth Chart ───────────────────────────────────────────────────
@app.route("/depth-chart")
def depth_chart_page():
    conn = get_db()
    players = conn.execute(
        "SELECT id, first_name, last_name, position, jersey_number, height_inches FROM players WHERE status = 'active' ORDER BY last_name"
    ).fetchall()
    lineups = conn.execute("SELECT * FROM lineups ORDER BY created_at DESC").fetchall()

    lineup_id = request.args.get("lineup_id")
    slots = []
    active_lineup = None
    if lineup_id:
        active_lineup = conn.execute("SELECT * FROM lineups WHERE id = ?", (lineup_id,)).fetchone()
        slots = conn.execute("SELECT * FROM lineup_slots WHERE lineup_id = ?", (lineup_id,)).fetchall()

    conn.close()
    return render_template("depth_chart.html",
        players=[dict(p) for p in players],
        lineups=[dict(l) for l in lineups],
        slots=[dict(s) for s in slots],
        active_lineup=dict(active_lineup) if active_lineup else None,
    )


@app.route("/depth-chart/save", methods=["POST"])
def save_lineup():
    conn = get_db()
    f = request.form
    lineup_name = f.get("lineup_name", "").strip() or "Untitled Lineup"
    lineup_desc = f.get("lineup_description", "").strip()
    lineup_id = f.get("lineup_id")

    if lineup_id:
        conn.execute("UPDATE lineups SET name=?, description=? WHERE id=?", (lineup_name, lineup_desc, int(lineup_id)))
        conn.execute("DELETE FROM lineup_slots WHERE lineup_id = ?", (int(lineup_id),))
    else:
        cur = conn.execute("INSERT INTO lineups (name, description) VALUES (?, ?)", (lineup_name, lineup_desc))
        lineup_id = cur.lastrowid

    slot_data = f.get("slot_data", "")
    if slot_data:
        try:
            entries = json.loads(slot_data)
            for entry in entries:
                conn.execute(
                    "INSERT INTO lineup_slots (lineup_id, player_id, slot_position, slot_order) VALUES (?, ?, ?, ?)",
                    (int(lineup_id), int(entry["player_id"]), entry["position"], int(entry["order"])),
                )
        except (json.JSONDecodeError, KeyError):
            pass

    conn.commit()
    conn.close()
    flash(f"Lineup '{lineup_name}' saved.", "success")
    return redirect(url_for("depth_chart_page", lineup_id=lineup_id))


@app.route("/lineups/<int:lineup_id>/delete", methods=["POST"])
def delete_lineup(lineup_id):
    conn = get_db()
    conn.execute("DELETE FROM lineups WHERE id = ?", (lineup_id,))
    conn.commit()
    conn.close()
    flash("Lineup deleted.", "info")
    return redirect(url_for("depth_chart_page"))


@app.route("/api/lineup-stats", methods=["POST"])
def api_lineup_stats():
    data = request.get_json()
    player_ids = data.get("player_ids", [])
    if not player_ids:
        return jsonify({"error": "No players"}), 400

    config = get_team_config()
    conn = get_db()

    totals = {"ppg": 0, "rpg": 0, "apg": 0, "fg_pcts": [], "three_pcts": [], "ft_pcts": [], "heights": [], "count": 0}

    for pid in player_ids:
        sa = conn.execute(
            "SELECT * FROM season_averages WHERE player_id = ? AND season = ? LIMIT 1",
            (int(pid), config["season"]),
        ).fetchone()
        p = conn.execute("SELECT height_inches FROM players WHERE id = ?", (int(pid),)).fetchone()

        if sa:
            totals["ppg"] += sa["ppg"] or 0
            totals["rpg"] += sa["rpg"] or 0
            totals["apg"] += sa["apg"] or 0
            if sa["fg_pct"]:
                totals["fg_pcts"].append(sa["fg_pct"])
            if sa["three_pct"]:
                totals["three_pcts"].append(sa["three_pct"])
            if sa["ft_pct"]:
                totals["ft_pcts"].append(sa["ft_pct"])
        if p:
            totals["heights"].append(p["height_inches"])
        totals["count"] += 1

    n = totals["count"] or 1
    result = {
        "total_ppg": round(totals["ppg"], 1),
        "total_rpg": round(totals["rpg"], 1),
        "total_apg": round(totals["apg"], 1),
        "avg_fg_pct": round(sum(totals["fg_pcts"]) / len(totals["fg_pcts"]), 1) if totals["fg_pcts"] else 0,
        "avg_three_pct": round(sum(totals["three_pcts"]) / len(totals["three_pcts"]), 1) if totals["three_pcts"] else 0,
        "avg_ft_pct": round(sum(totals["ft_pcts"]) / len(totals["ft_pcts"]), 1) if totals["ft_pcts"] else 0,
        "avg_height": height_to_display(round(sum(totals["heights"]) / len(totals["heights"]))) if totals["heights"] else "—",
        "player_count": totals["count"],
    }

    conn.close()
    return jsonify(result)


@app.route("/api/lineup-shotchart", methods=["POST"])
def api_lineup_shotchart():
    data = request.get_json()
    player_ids = data.get("player_ids", [])
    if not player_ids:
        return jsonify({"zones": []})

    config = get_team_config()
    conn = get_db()

    # Aggregate shot chart data across all players
    zones = []
    for zone_id in range(1, 12):
        total_made = 0
        total_att = 0
        for pid in player_ids:
            row = conn.execute(
                "SELECT fg_made, fg_attempted FROM shot_charts WHERE player_id = ? AND season = ? AND zone_id = ?",
                (int(pid), config["season"], zone_id),
            ).fetchone()
            if row:
                total_made += row["fg_made"] or 0
                total_att += row["fg_attempted"] or 0
        zones.append({
            "zone_id": zone_id,
            "zone_name": ZONE_NAMES.get(zone_id, ""),
            "fg_made": total_made,
            "fg_attempted": total_att,
            "fg_pct": round(total_made / total_att * 100, 1) if total_att > 0 else None,
        })

    conn.close()
    return jsonify({"zones": zones})


@app.route("/api/lineup/<int:lineup_id>")
def api_lineup_detail(lineup_id):
    conn = get_db()
    slots = conn.execute("SELECT * FROM lineup_slots WHERE lineup_id = ?", (lineup_id,)).fetchall()
    conn.close()
    return jsonify([dict(s) for s in slots])


# ── Compare ───────────────────────────────────────────────────────
@app.route("/compare")
def compare():
    ids = request.args.getlist("ids")
    players = []
    for pid in ids:
        data = get_player_full(int(pid))
        if data:
            players.append(data)

    conn = get_db()
    all_players = conn.execute(
        "SELECT id, first_name, last_name, position FROM players ORDER BY last_name"
    ).fetchall()
    conn.close()

    return render_template("compare.html",
        players=players,
        all_players=[dict(p) for p in all_players],
        selected_ids=ids,
    )


# ── CSV Import ────────────────────────────────────────────────────
def parse_height(val):
    if not val:
        return None
    val = val.strip().replace('"', '').replace('“', '').replace('”', '')
    for sep in ["'", "-", "."]:
        if sep in val:
            parts = val.split(sep)
            try:
                feet = int(parts[0].strip())
                inches = int(parts[1].strip()) if parts[1].strip() else 0
                return feet * 12 + inches
            except (ValueError, IndexError):
                continue
    try:
        n = int(val)
        return n if n > 12 else n * 12
    except ValueError:
        return None


CSV_FIELD_MAP = {
    "first_name": ["first_name", "first", "firstname", "fname"],
    "last_name": ["last_name", "last", "lastname", "lname", "surname"],
    "position": ["position", "pos"],
    "height": ["height", "ht", "height_inches"],
    "weight": ["weight", "wt", "lbs"],
    "jersey_number": ["jersey_number", "jersey", "number", "no", "#"],
    "grad_year": ["grad_year", "class", "year", "class_year", "graduation_year", "grad"],
    "gpa": ["gpa", "grade_point"],
    "games_played": ["games_played", "gp", "games", "g"],
    "minutes_pg": ["minutes_pg", "mpg", "minutes", "min"],
    "ppg": ["ppg", "pts", "points"],
    "rpg": ["rpg", "reb", "rebounds"],
    "apg": ["apg", "ast", "assists"],
    "spg": ["spg", "stl", "steals"],
    "bpg": ["bpg", "blk", "blocks"],
    "fg_pct": ["fg_pct", "fg%", "fg", "fgpct"],
    "three_pct": ["three_pct", "3pt%", "3pt", "3p%", "three", "threepct", "3fg%"],
    "ft_pct": ["ft_pct", "ft%", "ft", "ftpct"],
    "topg": ["topg", "to", "turnovers", "tov"],
}


def map_csv_headers(headers):
    mapping = {}
    for h in headers:
        clean = h.strip().lower().replace(" ", "_").replace("-", "_")
        for field, aliases in CSV_FIELD_MAP.items():
            if clean in aliases:
                mapping[h] = field
                break
    return mapping


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "POST":
        imported = 0
        skipped = 0
        errors = []
        config = get_team_config()

        csv_text = None
        if "csv_file" in request.files and request.files["csv_file"].filename:
            csv_text = request.files["csv_file"].stream.read().decode("utf-8-sig")
        elif request.form.get("csv_text", "").strip():
            csv_text = request.form["csv_text"]

        if not csv_text:
            flash("No CSV data provided.", "danger")
            return redirect(url_for("import_csv"))

        reader = csv.DictReader(io.StringIO(csv_text))
        header_map = map_csv_headers(reader.fieldnames or [])
        conn = get_db()

        for i, row in enumerate(reader, start=2):
            try:
                data = {}
                for orig_key, our_key in header_map.items():
                    data[our_key] = row.get(orig_key, "").strip()

                first_name = data.get("first_name", "")
                last_name = data.get("last_name", "")
                position = data.get("position", "").upper()

                if not first_name or not last_name:
                    skipped += 1
                    errors.append(f"Row {i}: Missing name")
                    continue

                if position not in POSITIONS:
                    pos_map = {"POINT GUARD": "PG", "SHOOTING GUARD": "SG", "SMALL FORWARD": "SF",
                               "POWER FORWARD": "PF", "CENTER": "C", "GUARD": "SG", "FORWARD": "SF",
                               "G": "SG", "F": "SF", "WING": "SF", "BIG": "PF"}
                    position = pos_map.get(position, "SG")

                height = parse_height(data.get("height", "")) or 72
                weight = None
                if data.get("weight"):
                    try:
                        weight = int(float(data["weight"]))
                    except ValueError:
                        pass

                jersey = None
                if data.get("jersey_number"):
                    try:
                        jersey = int(float(data["jersey_number"]))
                    except ValueError:
                        pass

                grad_year = 2026
                if data.get("grad_year"):
                    try:
                        grad_year = int(float(data["grad_year"]))
                    except ValueError:
                        pass

                gpa = None
                if data.get("gpa"):
                    try:
                        gpa = float(data["gpa"])
                    except ValueError:
                        pass

                cur = conn.execute("""
                    INSERT INTO players (first_name, last_name, position, height_inches, weight,
                        jersey_number, grad_year, gpa, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """, (first_name, last_name, position, height, weight, jersey, grad_year, gpa))
                pid = cur.lastrowid

                def to_float(key):
                    v = data.get(key, "")
                    if not v:
                        return 0
                    try:
                        return float(v.replace("%", ""))
                    except ValueError:
                        return 0

                def to_int(key):
                    v = data.get(key, "")
                    if not v:
                        return 0
                    try:
                        return int(float(v))
                    except ValueError:
                        return 0

                conn.execute("""
                    INSERT INTO season_averages (player_id, season, games_played, mpg, ppg, rpg, apg, spg, bpg,
                        fg_pct, three_pct, ft_pct, topg, is_manual)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (
                    pid, config["season"], to_int("games_played"), to_float("minutes_pg"),
                    to_float("ppg"), to_float("rpg"), to_float("apg"),
                    to_float("spg"), to_float("bpg"),
                    to_float("fg_pct"), to_float("three_pct"), to_float("ft_pct"), to_float("topg"),
                ))
                imported += 1

            except Exception as e:
                skipped += 1
                errors.append(f"Row {i}: {str(e)}")

        conn.commit()
        conn.close()

        if errors:
            flash(f"Imported {imported} players. Skipped {skipped} rows.", "warning")
        else:
            flash(f"Successfully imported {imported} players!", "success")
        return render_template("import.html", imported=imported, skipped=skipped, errors=errors[:20])

    return render_template("import.html", imported=None, skipped=None, errors=None)


@app.route("/export")
def export_csv():
    config = get_team_config()
    conn = get_db()
    players = conn.execute("""
        SELECT p.*, sa.season, sa.games_played, sa.mpg, sa.ppg, sa.rpg, sa.apg, sa.spg, sa.bpg,
               sa.fg_pct, sa.three_pct, sa.ft_pct, sa.topg
        FROM players p
        LEFT JOIN season_averages sa ON sa.player_id = p.id AND sa.season = ?
        ORDER BY p.last_name, p.first_name
    """, (config["season"],)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "first_name", "last_name", "position", "jersey", "height", "weight",
        "grad_year", "gpa", "status", "season", "gp", "mpg", "ppg", "rpg", "apg",
        "spg", "bpg", "fg%", "3pt%", "ft%", "topg",
    ])

    for p in players:
        d = dict(p)
        h = d["height_inches"]
        ht_str = f"{h // 12}'{h % 12}" if h else ""
        writer.writerow([
            d["first_name"], d["last_name"], d["position"], d.get("jersey_number") or "",
            ht_str, d["weight"] or "", d["grad_year"], d["gpa"] or "", d["status"],
            d.get("season") or "", d.get("games_played") or "", d.get("mpg") or "",
            d.get("ppg") or "", d.get("rpg") or "", d.get("apg") or "",
            d.get("spg") or "", d.get("bpg") or "",
            d.get("fg_pct") or "", d.get("three_pct") or "", d.get("ft_pct") or "",
            d.get("topg") or "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=courtiq_export.csv"},
    )


# ── Clear Database ────────────────────────────────────────────────
@app.route("/clear-db", methods=["POST"])
def clear_db():
    conn = get_db()
    for table in ["lineup_slots", "lineups", "development_goals", "conditioning_logs",
                   "shot_charts", "coach_evaluations", "game_logs", "season_averages", "players"]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    flash("Database cleared. All players removed.", "info")
    return redirect(url_for("index"))


# ── Team Settings ─────────────────────────────────────────────────
@app.route("/settings", methods=["GET", "POST"])
def team_settings():
    if request.method == "POST":
        f = request.form
        update_team_config(
            f.get("team_name", "My Team"),
            f.get("season", "2025-26"),
            f.get("coach_name", ""),
            f.get("school_name", ""),
        )
        flash("Team settings updated.", "success")
        return redirect(url_for("index"))
    return render_template("settings.html", config=get_team_config())


if __name__ == "__main__":
    init_db()
    app.run(debug=False, port=5001)
