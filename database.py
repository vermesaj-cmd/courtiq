import os
import re
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

def _hash_pw(password):
    return generate_password_hash(password, method="pbkdf2:sha256")

DB_PATH = os.path.join(os.path.dirname(__file__), "courtiq.db")
DATABASE_URL = os.environ.get("DATABASE_URL")

# ── Connection adapter ───────────────────────────────────────────
# PostgreSQL in production (Render), SQLite locally.
# The adapter translates ? params to %s so app.py queries work everywhere.

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    # Render gives postgres:// but psycopg2 needs postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def _translate_sql(sql):
    """Convert SQLite-style ? placeholders to PostgreSQL %s."""
    return sql.replace("?", "%s")


class PgCursorWrapper:
    """Wraps a psycopg2 cursor to accept sqlite3-style ? params."""
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        sql = _translate_sql(sql)
        # Convert SQLite-specific syntax
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.IGNORECASE)
        if "INSERT INTO" in sql.upper() and "ON CONFLICT" not in sql.upper() and "OR IGNORE" not in sql.upper():
            pass  # normal insert
        self._cursor.execute(sql, params or ())
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def lastrowid(self):
        # PostgreSQL doesn't have lastrowid natively — we use RETURNING
        row = self._cursor.fetchone()
        return row["id"] if row else None

    @property
    def description(self):
        return self._cursor.description


class PgConnectionWrapper:
    """Wraps a psycopg2 connection to behave like sqlite3 connection."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        # For INSERT statements, add RETURNING id to get lastrowid
        needs_returning = False
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("INSERT") and "RETURNING" not in sql_upper:
            needs_returning = True
            sql = sql.rstrip().rstrip(";") + " RETURNING id"

        sql = _translate_sql(sql)
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.IGNORECASE)

        # Handle ON CONFLICT for INSERT OR IGNORE
        if needs_returning and "ON CONFLICT" not in sql.upper():
            # Check if this was an INSERT OR IGNORE (now just INSERT INTO)
            pass

        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cursor.execute(sql, params or ())
        except psycopg2.errors.UniqueViolation:
            self._conn.rollback()
            return PgCursorWrapper(cursor)
        return PgCursorWrapper(cursor)

    def cursor(self):
        return PgCursorWrapper(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def rollback(self):
        self._conn.rollback()


def get_db():
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        except Exception:
            conn = psycopg2.connect(DATABASE_URL, sslmode="disable")
        return PgConnectionWrapper(conn)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


# ── Schema ───────────────────────────────────────────────────────

SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS team_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        team_name TEXT NOT NULL DEFAULT 'My Team',
        season TEXT NOT NULL DEFAULT '2025-26',
        coach_name TEXT DEFAULT '',
        school_name TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "INSERT OR IGNORE INTO team_config (id) VALUES (1)",

    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        position TEXT NOT NULL,
        height_inches INTEGER NOT NULL,
        weight INTEGER,
        wingspan_inches INTEGER,
        jersey_number INTEGER,
        grad_year INTEGER NOT NULL,
        gpa REAL,
        status TEXT DEFAULT 'active' CHECK(status IN ('active','injured','inactive')),
        photo_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS season_averages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        season TEXT NOT NULL,
        games_played INTEGER DEFAULT 0,
        mpg REAL DEFAULT 0,
        ppg REAL DEFAULT 0,
        rpg REAL DEFAULT 0,
        apg REAL DEFAULT 0,
        spg REAL DEFAULT 0,
        bpg REAL DEFAULT 0,
        fg_pct REAL DEFAULT 0,
        three_pct REAL DEFAULT 0,
        ft_pct REAL DEFAULT 0,
        topg REAL DEFAULT 0,
        is_manual INTEGER DEFAULT 1,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS game_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        game_date TEXT NOT NULL,
        opponent TEXT NOT NULL,
        result TEXT CHECK(result IN ('W','L')),
        minutes INTEGER DEFAULT 0,
        points INTEGER DEFAULT 0,
        rebounds INTEGER DEFAULT 0,
        assists INTEGER DEFAULT 0,
        steals INTEGER DEFAULT 0,
        blocks INTEGER DEFAULT 0,
        fg_made INTEGER DEFAULT 0,
        fg_attempted INTEGER DEFAULT 0,
        three_made INTEGER DEFAULT 0,
        three_attempted INTEGER DEFAULT 0,
        ft_made INTEGER DEFAULT 0,
        ft_attempted INTEGER DEFAULT 0,
        turnovers INTEGER DEFAULT 0,
        fouls INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS coach_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        coach_name TEXT NOT NULL,
        eval_date TEXT DEFAULT (date('now')),
        speed INTEGER CHECK(speed BETWEEN 1 AND 10),
        vertical INTEGER CHECK(vertical BETWEEN 1 AND 10),
        agility INTEGER CHECK(agility BETWEEN 1 AND 10),
        strength INTEGER CHECK(strength BETWEEN 1 AND 10),
        endurance INTEGER CHECK(endurance BETWEEN 1 AND 10),
        ball_handling INTEGER CHECK(ball_handling BETWEEN 1 AND 10),
        passing INTEGER CHECK(passing BETWEEN 1 AND 10),
        shooting_form INTEGER CHECK(shooting_form BETWEEN 1 AND 10),
        post_moves INTEGER CHECK(post_moves BETWEEN 1 AND 10),
        off_ball_movement INTEGER CHECK(off_ball_movement BETWEEN 1 AND 10),
        transition_play INTEGER CHECK(transition_play BETWEEN 1 AND 10),
        basketball_iq INTEGER CHECK(basketball_iq BETWEEN 1 AND 10),
        motor INTEGER CHECK(motor BETWEEN 1 AND 10),
        coachability INTEGER CHECK(coachability BETWEEN 1 AND 10),
        leadership INTEGER CHECK(leadership BETWEEN 1 AND 10),
        clutch INTEGER CHECK(clutch BETWEEN 1 AND 10),
        defensive_instincts INTEGER CHECK(defensive_instincts BETWEEN 1 AND 10),
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS shot_charts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        season TEXT NOT NULL,
        zone_id INTEGER NOT NULL CHECK(zone_id BETWEEN 1 AND 11),
        zone_name TEXT NOT NULL,
        fg_made INTEGER DEFAULT 0,
        fg_attempted INTEGER DEFAULT 0,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
        UNIQUE(player_id, season, zone_id)
    )""",

    """CREATE TABLE IF NOT EXISTS conditioning_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        log_date TEXT NOT NULL,
        mile_time_sec REAL,
        shuttle_sec REAL,
        vertical_inches REAL,
        broad_jump_inches REAL,
        bench_reps INTEGER,
        body_weight REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS development_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        target_value REAL,
        current_value REAL DEFAULT 0,
        unit TEXT DEFAULT '',
        deadline TEXT,
        status TEXT DEFAULT 'in_progress' CHECK(status IN ('in_progress','achieved','behind')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS lineups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS lineup_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lineup_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        slot_position TEXT NOT NULL,
        slot_order INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (lineup_id) REFERENCES lineups(id) ON DELETE CASCADE,
        FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
        UNIQUE(lineup_id, player_id)
    )""",

    """CREATE TABLE IF NOT EXISTS team_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_date TEXT NOT NULL,
        opponent TEXT NOT NULL,
        location TEXT DEFAULT '',
        home_away TEXT DEFAULT 'home' CHECK(home_away IN ('home','away','neutral')),
        result TEXT CHECK(result IN ('W','L',NULL)),
        team_score INTEGER,
        opp_score INTEGER,
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS practice_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        practice_date TEXT,
        total_minutes INTEGER DEFAULT 90,
        notes TEXT DEFAULT '',
        is_template INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS practice_drills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        drill_name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        duration_minutes INTEGER DEFAULT 10,
        description TEXT DEFAULT '',
        drill_order INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (plan_id) REFERENCES practice_plans(id) ON DELETE CASCADE
    )""",
]

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS team_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        team_name TEXT NOT NULL DEFAULT 'My Team',
        season TEXT NOT NULL DEFAULT '2025-26',
        coach_name TEXT DEFAULT '',
        school_name TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "INSERT INTO team_config (id) VALUES (1) ON CONFLICT DO NOTHING",

    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS players (
        id SERIAL PRIMARY KEY,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        position TEXT NOT NULL,
        height_inches INTEGER NOT NULL,
        weight INTEGER,
        wingspan_inches INTEGER,
        jersey_number INTEGER,
        grad_year INTEGER NOT NULL,
        gpa REAL,
        status TEXT DEFAULT 'active',
        photo_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS season_averages (
        id SERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        season TEXT NOT NULL,
        games_played INTEGER DEFAULT 0,
        mpg REAL DEFAULT 0,
        ppg REAL DEFAULT 0,
        rpg REAL DEFAULT 0,
        apg REAL DEFAULT 0,
        spg REAL DEFAULT 0,
        bpg REAL DEFAULT 0,
        fg_pct REAL DEFAULT 0,
        three_pct REAL DEFAULT 0,
        ft_pct REAL DEFAULT 0,
        topg REAL DEFAULT 0,
        is_manual INTEGER DEFAULT 1
    )""",

    """CREATE TABLE IF NOT EXISTS game_logs (
        id SERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        game_date TEXT NOT NULL,
        opponent TEXT NOT NULL,
        result TEXT,
        minutes INTEGER DEFAULT 0,
        points INTEGER DEFAULT 0,
        rebounds INTEGER DEFAULT 0,
        assists INTEGER DEFAULT 0,
        steals INTEGER DEFAULT 0,
        blocks INTEGER DEFAULT 0,
        fg_made INTEGER DEFAULT 0,
        fg_attempted INTEGER DEFAULT 0,
        three_made INTEGER DEFAULT 0,
        three_attempted INTEGER DEFAULT 0,
        ft_made INTEGER DEFAULT 0,
        ft_attempted INTEGER DEFAULT 0,
        turnovers INTEGER DEFAULT 0,
        fouls INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS coach_evaluations (
        id SERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        coach_name TEXT NOT NULL,
        eval_date TEXT DEFAULT CURRENT_DATE::TEXT,
        speed INTEGER,
        vertical INTEGER,
        agility INTEGER,
        strength INTEGER,
        endurance INTEGER,
        ball_handling INTEGER,
        passing INTEGER,
        shooting_form INTEGER,
        post_moves INTEGER,
        off_ball_movement INTEGER,
        transition_play INTEGER,
        basketball_iq INTEGER,
        motor INTEGER,
        coachability INTEGER,
        leadership INTEGER,
        clutch INTEGER,
        defensive_instincts INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS shot_charts (
        id SERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        season TEXT NOT NULL,
        zone_id INTEGER NOT NULL,
        zone_name TEXT NOT NULL,
        fg_made INTEGER DEFAULT 0,
        fg_attempted INTEGER DEFAULT 0,
        UNIQUE(player_id, season, zone_id)
    )""",

    """CREATE TABLE IF NOT EXISTS conditioning_logs (
        id SERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        log_date TEXT NOT NULL,
        mile_time_sec REAL,
        shuttle_sec REAL,
        vertical_inches REAL,
        broad_jump_inches REAL,
        bench_reps INTEGER,
        body_weight REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS development_goals (
        id SERIAL PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        target_value REAL,
        current_value REAL DEFAULT 0,
        unit TEXT DEFAULT '',
        deadline TEXT,
        status TEXT DEFAULT 'in_progress',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS lineups (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS lineup_slots (
        id SERIAL PRIMARY KEY,
        lineup_id INTEGER NOT NULL REFERENCES lineups(id) ON DELETE CASCADE,
        player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        slot_position TEXT NOT NULL,
        slot_order INTEGER NOT NULL DEFAULT 1,
        UNIQUE(lineup_id, player_id)
    )""",

    """CREATE TABLE IF NOT EXISTS team_schedule (
        id SERIAL PRIMARY KEY,
        game_date TEXT NOT NULL,
        opponent TEXT NOT NULL,
        location TEXT DEFAULT '',
        home_away TEXT DEFAULT 'home',
        result TEXT,
        team_score INTEGER,
        opp_score INTEGER,
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS practice_plans (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        practice_date TEXT,
        total_minutes INTEGER DEFAULT 90,
        notes TEXT DEFAULT '',
        is_template INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",

    """CREATE TABLE IF NOT EXISTS practice_drills (
        id SERIAL PRIMARY KEY,
        plan_id INTEGER NOT NULL REFERENCES practice_plans(id) ON DELETE CASCADE,
        drill_name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'other',
        duration_minutes INTEGER DEFAULT 10,
        description TEXT DEFAULT '',
        drill_order INTEGER NOT NULL DEFAULT 1
    )""",
]


def init_db():
    conn = get_db()
    schema = PG_SCHEMA if DATABASE_URL else SQLITE_SCHEMA
    if DATABASE_URL:
        cur = conn._conn.cursor()
        for stmt in schema:
            cur.execute(stmt)
        conn._conn.commit()
    else:
        c = conn.cursor()
        for stmt in schema:
            c.execute(stmt)
        conn.commit()
    conn.close()


# ── Constants ────────────────────────────────────────────────────

ZONE_NAMES = {
    1: "Rim", 2: "Paint", 3: "Left Baseline Mid", 4: "Right Baseline Mid",
    5: "Left Elbow", 6: "Right Elbow", 7: "Left Corner 3", 8: "Right Corner 3",
    9: "Left Wing 3", 10: "Top of Key 3", 11: "Right Wing 3",
}


# ── Helpers ──────────────────────────────────────────────────────

def get_team_config():
    conn = get_db()
    row = conn.execute("SELECT * FROM team_config WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else {"team_name": "My Team", "season": "2025-26", "coach_name": "", "school_name": ""}


def update_team_config(team_name, season, coach_name, school_name):
    conn = get_db()
    conn.execute(
        "UPDATE team_config SET team_name=?, season=?, coach_name=?, school_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=1",
        (team_name, season, coach_name, school_name),
    )
    conn.commit()
    conn.close()


def get_eval_averages(player_id, conn=None):
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True

    evals = conn.execute(
        "SELECT * FROM coach_evaluations WHERE player_id = ? ORDER BY created_at DESC",
        (player_id,),
    ).fetchall()

    if close_conn:
        conn.close()

    eval_list = [dict(e) for e in evals]
    num_evals = len(eval_list)

    if num_evals == 0:
        return {"num_evaluations": 0, "evaluations": [], "averages": None, "evaluated": False}

    athleticism_fields = ["speed", "vertical", "agility", "strength", "endurance"]
    skills_fields = ["ball_handling", "passing", "shooting_form", "post_moves", "off_ball_movement", "transition_play"]
    intangibles_fields = ["basketball_iq", "motor", "coachability", "leadership", "clutch", "defensive_instincts"]
    all_fields = athleticism_fields + skills_fields + intangibles_fields

    averages = {}
    for field in all_fields:
        values = [e[field] for e in eval_list if e[field] is not None]
        averages[field] = round(sum(values) / len(values), 1) if values else None

    return {"num_evaluations": num_evals, "evaluations": eval_list, "averages": averages, "evaluated": True}


def calculate_season_averages(player_id, season, conn=None):
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True

    logs = conn.execute(
        "SELECT * FROM game_logs WHERE player_id = ?", (player_id,),
    ).fetchall()

    gp = len(logs)
    if gp == 0:
        existing = conn.execute(
            "SELECT id, is_manual FROM season_averages WHERE player_id = ? AND season = ?",
            (player_id, season),
        ).fetchone()
        if existing and not existing["is_manual"]:
            conn.execute(
                "UPDATE season_averages SET games_played=0, mpg=0, ppg=0, rpg=0, apg=0, spg=0, bpg=0, fg_pct=0, three_pct=0, ft_pct=0, topg=0, is_manual=0 WHERE id=?",
                (existing["id"],),
            )
            conn.commit()
        if close_conn:
            conn.close()
        return

    total_min = sum(r["minutes"] or 0 for r in logs)
    total_pts = sum(r["points"] or 0 for r in logs)
    total_reb = sum(r["rebounds"] or 0 for r in logs)
    total_ast = sum(r["assists"] or 0 for r in logs)
    total_stl = sum(r["steals"] or 0 for r in logs)
    total_blk = sum(r["blocks"] or 0 for r in logs)
    total_to = sum(r["turnovers"] or 0 for r in logs)
    total_fgm = sum(r["fg_made"] or 0 for r in logs)
    total_fga = sum(r["fg_attempted"] or 0 for r in logs)
    total_3m = sum(r["three_made"] or 0 for r in logs)
    total_3a = sum(r["three_attempted"] or 0 for r in logs)
    total_ftm = sum(r["ft_made"] or 0 for r in logs)
    total_fta = sum(r["ft_attempted"] or 0 for r in logs)

    avgs = {
        "games_played": gp,
        "mpg": round(total_min / gp, 1),
        "ppg": round(total_pts / gp, 1),
        "rpg": round(total_reb / gp, 1),
        "apg": round(total_ast / gp, 1),
        "spg": round(total_stl / gp, 1),
        "bpg": round(total_blk / gp, 1),
        "topg": round(total_to / gp, 1),
        "fg_pct": round(100 * total_fgm / total_fga, 1) if total_fga > 0 else 0,
        "three_pct": round(100 * total_3m / total_3a, 1) if total_3a > 0 else 0,
        "ft_pct": round(100 * total_ftm / total_fta, 1) if total_fta > 0 else 0,
    }

    existing = conn.execute(
        "SELECT id FROM season_averages WHERE player_id = ? AND season = ?",
        (player_id, season),
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE season_averages SET games_played=?, mpg=?, ppg=?, rpg=?, apg=?, spg=?, bpg=?,
            fg_pct=?, three_pct=?, ft_pct=?, topg=?, is_manual=0 WHERE id=?
        """, (
            avgs["games_played"], avgs["mpg"], avgs["ppg"], avgs["rpg"], avgs["apg"],
            avgs["spg"], avgs["bpg"], avgs["fg_pct"], avgs["three_pct"], avgs["ft_pct"],
            avgs["topg"], existing["id"],
        ))
    else:
        conn.execute("""
            INSERT INTO season_averages (player_id, season, games_played, mpg, ppg, rpg, apg, spg, bpg,
            fg_pct, three_pct, ft_pct, topg, is_manual)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            player_id, season, avgs["games_played"], avgs["mpg"], avgs["ppg"], avgs["rpg"],
            avgs["apg"], avgs["spg"], avgs["bpg"], avgs["fg_pct"], avgs["three_pct"],
            avgs["ft_pct"], avgs["topg"],
        ))

    conn.commit()
    if close_conn:
        conn.close()


def calc_advanced_metrics(game_logs):
    """Calculate PER, TS%, USG% from game logs."""
    if not game_logs:
        return {"per": None, "ts_pct": None, "usg_pct": None, "ortg": None}

    total_pts = sum(g["points"] or 0 for g in game_logs)
    total_reb = sum(g["rebounds"] or 0 for g in game_logs)
    total_ast = sum(g["assists"] or 0 for g in game_logs)
    total_stl = sum(g["steals"] or 0 for g in game_logs)
    total_blk = sum(g["blocks"] or 0 for g in game_logs)
    total_to = sum(g["turnovers"] or 0 for g in game_logs)
    total_fga = sum(g["fg_attempted"] or 0 for g in game_logs)
    total_fgm = sum(g["fg_made"] or 0 for g in game_logs)
    total_fta = sum(g["ft_attempted"] or 0 for g in game_logs)
    total_ftm = sum(g["ft_made"] or 0 for g in game_logs)
    total_3m = sum(g["three_made"] or 0 for g in game_logs)
    total_min = sum(g["minutes"] or 0 for g in game_logs)
    total_fouls = sum(g["fouls"] or 0 for g in game_logs)
    gp = len(game_logs)

    # True Shooting % = PTS / (2 * (FGA + 0.44 * FTA))
    ts_denom = 2 * (total_fga + 0.44 * total_fta)
    ts_pct = round(total_pts / ts_denom * 100, 1) if ts_denom > 0 else None

    # Simplified PER (John Hollinger's formula simplified for HS level)
    # PER ≈ (PTS + REB + AST + STL + BLK - (FGA-FGM) - (FTA-FTM) - TO) / GP
    if gp > 0 and total_min > 0:
        missed_fg = total_fga - total_fgm
        missed_ft = total_fta - total_ftm
        raw = total_pts + total_reb + total_ast + total_stl + total_blk - missed_fg - missed_ft - total_to
        per = round(raw / gp, 1)
    else:
        per = None

    # Usage Rate % — what share of team possessions a player uses per game
    # Simplified for HS: (FGA + 0.44*FTA + TO) per game / estimated ~65 team possessions
    if gp > 0:
        possessions_per_game = (total_fga + 0.44 * total_fta + total_to) / gp
        usg_pct = round(possessions_per_game / 65 * 100, 1)
    else:
        usg_pct = None

    # Offensive Rating (points produced per 40 minutes)
    ortg = round(total_pts / total_min * 40, 1) if total_min > 0 else None

    return {"per": per, "ts_pct": ts_pct, "usg_pct": usg_pct, "ortg": ortg}


def get_player_full(player_id):
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        return None

    season_avgs = conn.execute(
        "SELECT * FROM season_averages WHERE player_id = ? ORDER BY season DESC", (player_id,),
    ).fetchall()

    game_logs = conn.execute(
        "SELECT * FROM game_logs WHERE player_id = ? ORDER BY game_date DESC", (player_id,),
    ).fetchall()

    config = conn.execute("SELECT season FROM team_config WHERE id = 1").fetchone()
    current_season = config["season"] if config else "2025-26"

    shot_chart_rows = conn.execute(
        "SELECT * FROM shot_charts WHERE player_id = ? AND season = ? ORDER BY zone_id",
        (player_id, current_season),
    ).fetchall()
    # Pad missing zones so the profile always shows all 11
    shot_chart_map = {z["zone_id"]: dict(z) for z in shot_chart_rows}
    shot_chart = []
    for zid in range(1, 12):
        if zid in shot_chart_map:
            shot_chart.append(shot_chart_map[zid])
        else:
            shot_chart.append({"zone_id": zid, "zone_name": ZONE_NAMES.get(zid, ""), "fg_made": 0, "fg_attempted": 0})

    conditioning = conn.execute(
        "SELECT * FROM conditioning_logs WHERE player_id = ? ORDER BY log_date DESC",
        (player_id,),
    ).fetchall()

    goals = conn.execute(
        "SELECT * FROM development_goals WHERE player_id = ? ORDER BY created_at DESC",
        (player_id,),
    ).fetchall()

    eval_data = get_eval_averages(player_id, conn)

    conn.close()

    game_log_dicts = [dict(g) for g in game_logs]
    advanced = calc_advanced_metrics(game_log_dicts)

    return {
        "player": dict(player),
        "season_averages": [dict(s) for s in season_avgs],
        "game_logs": game_log_dicts,
        "advanced_metrics": advanced,
        "shot_chart": shot_chart,
        "conditioning": [dict(c) for c in conditioning],
        "goals": [dict(g) for g in goals],
        "eval_data": eval_data,
    }


def create_user(username, password):
    conn = get_db()
    pw_hash = _hash_pw(password)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, pw_hash),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()


def verify_user(username, password):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password):
        return dict(row)
    return None


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def seed_default_user():
    """Create default admin account if no users exist."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    if isinstance(count, dict):
        cnt = list(count.values())[0]
    else:
        cnt = count[0]
    conn.close()
    if cnt == 0:
        create_user("coach", "courtiq2025")


def seed_players():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM players").fetchone()
    # Handle both sqlite3.Row (index access) and dict (key access)
    if isinstance(count, dict):
        cnt = list(count.values())[0]
    else:
        cnt = count[0]
    if cnt > 0:
        conn.close()
        return

    config = conn.execute("SELECT season FROM team_config WHERE id = 1").fetchone()
    season = config["season"] if config else "2025-26"

    players = [
        ("Player", "One",      "PG", 73, 170, 74,  1, 2026, 3.4),
        ("Player", "Two",      "PG", 71, 165, 72,  3, 2027, 3.1),
        ("Player", "Three",    "SG", 75, 185, 77,  2, 2026, 3.6),
        ("Player", "Four",     "SG", 74, 180, 76, 12, 2026, 2.9),
        ("Player", "Five",     "SF", 78, 200, 80,  5, 2026, 3.3),
        ("Player", "Six",      "SF", 77, 195, 79, 15, 2027, 3.0),
        ("Player", "Seven",    "SF", 76, 190, 78, 21, 2026, 3.5),
        ("Player", "Eight",    "PF", 79, 210, 82,  4, 2026, 3.2),
        ("Player", "Nine",     "PF", 80, 215, 83, 32, 2027, 2.8),
        ("Player", "Ten",      "PF", 78, 205, 80, 24, 2026, 3.1),
        ("Player", "Eleven",   "C",  81, 225, 84, 50, 2026, 3.0),
        ("Player", "Twelve",   "C",  82, 230, 85, 33, 2027, 2.7),
        ("Player", "Thirteen", "SG", 74, 178, 75, 10, 2028, 3.7),
        ("Player", "Fourteen", "PG", 72, 168, 73, 11, 2028, 3.8),
        ("Player", "Fifteen",  "PF", 79, 208, 81, 44, 2026, 3.2),
    ]

    stats = [
        (20, 30.0, 14.2, 3.1, 6.5, 1.8, 0.2, 44.0, 35.0, 78.0, 2.5),
        (18, 18.0,  6.5, 2.0, 3.8, 1.2, 0.1, 40.0, 30.0, 72.0, 1.8),
        (20, 28.0, 16.8, 4.2, 2.5, 1.5, 0.3, 46.0, 38.0, 82.0, 1.9),
        (19, 16.0,  8.0, 2.8, 1.5, 0.8, 0.2, 42.0, 33.0, 70.0, 1.2),
        (20, 26.0, 12.5, 6.0, 2.0, 1.0, 0.8, 48.0, 32.0, 75.0, 1.5),
        (17, 14.0,  5.5, 3.5, 1.0, 0.7, 0.5, 43.0, 28.0, 68.0, 1.0),
        (20, 20.0,  9.0, 4.5, 1.8, 1.1, 0.4, 45.0, 34.0, 74.0, 1.3),
        (20, 28.0, 13.0, 8.0, 1.5, 0.8, 1.5, 50.0, 25.0, 70.0, 1.8),
        (16, 15.0,  6.0, 5.0, 0.8, 0.5, 1.0, 48.0, 20.0, 65.0, 1.5),
        (19, 18.0,  7.5, 5.5, 1.0, 0.6, 0.8, 47.0, 30.0, 72.0, 1.1),
        (20, 27.0, 11.0, 9.5, 1.2, 0.5, 2.5, 55.0, 15.0, 65.0, 2.0),
        (15, 13.0,  4.5, 6.0, 0.5, 0.3, 1.8, 52.0, 10.0, 58.0, 1.5),
        (12,  8.0,  3.0, 1.5, 1.0, 0.5, 0.1, 38.0, 30.0, 75.0, 0.8),
        (10,  6.0,  2.0, 1.0, 2.0, 0.8, 0.0, 36.0, 28.0, 80.0, 1.2),
        (18, 16.0,  7.0, 5.8, 0.8, 0.4, 1.0, 49.0, 22.0, 68.0, 1.4),
    ]

    for i, (first, last, pos, ht, wt, ws, jersey, grad, gpa) in enumerate(players):
        cur = conn.execute("""
            INSERT INTO players (first_name, last_name, position, height_inches, weight,
                                 wingspan_inches, jersey_number, grad_year, gpa, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """, (first, last, pos, ht, wt, ws, jersey, grad, gpa))

        # Get the player id
        if DATABASE_URL:
            pid = cur.lastrowid
        else:
            pid = cur.lastrowid

        gp, mpg, ppg, rpg, apg, spg, bpg, fg, three, ft, topg = stats[i]
        conn.execute("""
            INSERT INTO season_averages (player_id, season, games_played, mpg, ppg, rpg, apg, spg, bpg,
                                         fg_pct, three_pct, ft_pct, topg, is_manual)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (pid, season, gp, mpg, ppg, rpg, apg, spg, bpg, fg, three, ft, topg))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_players()
    print("Database initialized.")
