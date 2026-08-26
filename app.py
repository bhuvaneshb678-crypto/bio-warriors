from flask import Flask, request, redirect, url_for, session, render_template_string, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, date, timedelta
import sqlite3
import random
import os

# ============================================================
# BIO WARRIORS - SINGLE FILE FLASK APP
# ============================================================
# Features:
# - Register / Login / Logout
# - Daily biology quiz with timer
# - Randomized questions and options
# - Score, percentage, points and streak
# - Daily mini challenge
# - Leaderboard
# - Personal history and statistics
# - Profile editing and password change
# - Hall of Fame / monthly champions
# - Admin dashboard
# - Admin user management
# - Admin question/content management
# - Admin mini-challenge management
# - Admin analytics
# - No external HTML templates required
#
# Run:
#   pip install flask werkzeug
#   python app.py
# Then open:
#   http://127.0.0.1:5000
# ============================================================

app = Flask(__name__)
app.secret_key = os.environ.get("BIO_WARRIORS_SECRET", "bio-warriors-change-this-secret")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bio_warriors_new.db")

QUIZ_MINUTES = 25
QUESTIONS_PER_DAY = 30
MINI_CHALLENGES_PER_DAY = 5
DAILY_QUIZ_POINTS = 100
MINI_POINTS = 10


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        active INTEGER NOT NULL DEFAULT 1,
        points INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0,
        best_streak INTEGER NOT NULL DEFAULT 0,
        last_quiz_date TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct_option TEXT NOT NULL,
        explanation TEXT DEFAULT '',
        category TEXT DEFAULT 'Biology',
        difficulty TEXT DEFAULT 'Medium',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS quiz_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        quiz_date TEXT NOT NULL,
        question_ids TEXT NOT NULL,
        started_at TEXT NOT NULL,
        submitted_at TEXT,
        score INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        points INTEGER DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        selected_option TEXT,
        is_correct INTEGER DEFAULT 0,
        FOREIGN KEY(session_id) REFERENCES quiz_sessions(id) ON DELETE CASCADE,
        FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS mini_challenges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        challenge_date TEXT NOT NULL,
        challenge_slot INTEGER NOT NULL DEFAULT 1,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct_option TEXT NOT NULL,
        explanation TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS mini_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        challenge_id INTEGER NOT NULL,
        selected_option TEXT,
        is_correct INTEGER NOT NULL DEFAULT 0,
        attempted_at TEXT NOT NULL,
        UNIQUE(user_id, challenge_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(challenge_id) REFERENCES mini_challenges(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_type TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """)

    # Migrate older databases from one mini challenge/day to five slots/day.
    mini_columns = [r[1] for r in conn.execute("PRAGMA table_info(mini_challenges)").fetchall()]
    if "challenge_slot" not in mini_columns:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("""
            CREATE TABLE mini_challenges_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                challenge_date TEXT NOT NULL,
                challenge_slot INTEGER NOT NULL DEFAULT 1,
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL,
                explanation TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            INSERT INTO mini_challenges_new
            (id,challenge_date,challenge_slot,question,option_a,option_b,option_c,option_d,correct_option,explanation,active)
            SELECT id,challenge_date,1,question,option_a,option_b,option_c,option_d,correct_option,explanation,active
            FROM mini_challenges
        """)
        conn.execute("DROP TABLE mini_challenges")
        conn.execute("ALTER TABLE mini_challenges_new RENAME TO mini_challenges")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_mini_date_slot ON mini_challenges(challenge_date,challenge_slot)")
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_mini_date_slot ON mini_challenges(challenge_date,challenge_slot)")

    # Create default admin only if it does not exist.
    admin = conn.execute(
        "SELECT id FROM users WHERE user_id=?",
        ("admin",)
    ).fetchone()

    if not admin:
        conn.execute("""
            INSERT INTO users
            (user_id,name,email,password_hash,role,created_at)
            VALUES (?,?,?,?,?,?)
        """, (
            "admin",
            "Bio Warriors Admin",
            "admin@biowarriors.local",
            generate_password_hash("admin123"),
            "admin",
            datetime.now().isoformat(timespec="seconds")
        ))

    # Seed questions if empty.
    qcount = conn.execute("SELECT COUNT(*) AS c FROM questions").fetchone()["c"]
    if qcount == 0:
        seed_questions(conn)
    ensure_question_bank(conn)

    # Seed mini challenges if empty.
    mcount = conn.execute("SELECT COUNT(*) AS c FROM mini_challenges").fetchone()["c"]
    if mcount == 0:
        seed_mini_challenges(conn)
    ensure_mini_challenges(conn)

    # Seed dashboard content.
    ccount = conn.execute("SELECT COUNT(*) AS c FROM content").fetchone()["c"]
    if ccount == 0:
        seed_content(conn)

    conn.commit()
    conn.close()


def seed_questions(conn):
    questions = [
        ("Which organelle contains chlorophyll in plant cells?",
         "Nucleus", "Chloroplast", "Ribosome", "Golgi apparatus", "B",
         "Chloroplasts contain chlorophyll and carry out photosynthesis.",
         "Cell Biology", "Easy"),

        ("Which molecule is directly used as cellular energy?",
         "DNA", "ATP", "RNA", "Glucose", "B",
         "ATP is the cell's immediate energy currency.",
         "Biochemistry", "Easy"),

        ("Which blood component is mainly involved in clotting?",
         "Red blood cells", "White blood cells", "Platelets", "Plasma", "C",
         "Platelets help form blood clots at damaged blood vessels.",
         "Human Biology", "Easy"),

        ("What is the basic unit of life?",
         "Tissue", "Organ", "Cell", "Atom", "C",
         "The cell is the basic structural and functional unit of life.",
         "Cell Biology", "Easy"),

        ("Which enzyme digests proteins in the stomach?",
         "Amylase", "Pepsin", "Lipase", "Maltase", "B",
         "Pepsin begins protein digestion in the acidic stomach.",
         "Biochemistry", "Medium"),

        ("What is the genetic material in most living organisms?",
         "ATP", "DNA", "Lipid", "Starch", "B",
         "DNA stores hereditary genetic information in most organisms.",
         "Genetics", "Easy"),

        ("Which phase of mitosis has chromosomes aligned at the equator?",
         "Prophase", "Metaphase", "Anaphase", "Telophase", "B",
         "During metaphase, chromosomes align at the cell equator.",
         "Cell Biology", "Medium"),

        ("What is the normal chromosome number in a human somatic cell?",
         "23", "44", "46", "48", "C",
         "Human somatic cells normally contain 46 chromosomes.",
         "Genetics", "Easy"),

        ("Which hormone lowers blood glucose concentration?",
         "Glucagon", "Insulin", "Adrenaline", "Thyroxine", "B",
         "Insulin promotes glucose uptake and storage, lowering blood glucose.",
         "Physiology", "Easy"),

        ("Where does most aerobic cellular respiration occur?",
         "Nucleus", "Mitochondria", "Lysosome", "Ribosome", "B",
         "Most aerobic respiration and ATP production occur in mitochondria.",
         "Cell Biology", "Easy"),

        ("Which base pairs with adenine in DNA?",
         "Uracil", "Cytosine", "Guanine", "Thymine", "D",
         "Adenine pairs with thymine in DNA.",
         "Genetics", "Easy"),

        ("What is the functional unit of the kidney?",
         "Neuron", "Nephron", "Alveolus", "Sarcomere", "B",
         "The nephron performs filtration and other processes in the kidney.",
         "Human Biology", "Medium"),

        ("Which vitamin is synthesized in skin after sunlight exposure?",
         "Vitamin A", "Vitamin B12", "Vitamin C", "Vitamin D", "D",
         "UVB exposure helps the skin synthesize vitamin D.",
         "Nutrition", "Easy"),

        ("Which process produces gametes?",
         "Mitosis", "Meiosis", "Binary fission", "Budding", "B",
         "Meiosis produces haploid gametes from diploid precursor cells.",
         "Genetics", "Medium"),

        ("What is the pH of a neutral solution at room temperature?",
         "2", "5", "7", "10", "C",
         "A neutral aqueous solution has a pH close to 7 at room temperature.",
         "Biochemistry", "Easy"),

        ("Which molecule carries amino acids to the ribosome?",
         "mRNA", "tRNA", "rRNA", "DNA", "B",
         "Transfer RNA carries amino acids to the ribosome during translation.",
         "Molecular Biology", "Medium"),

        ("Which structure controls movement of substances into and out of a cell?",
         "Cell wall", "Cell membrane", "Nucleolus", "Centrosome", "B",
         "The plasma membrane selectively regulates transport.",
         "Cell Biology", "Easy"),

        ("Which blood cells are primarily responsible for immune defense?",
         "Red blood cells", "Platelets", "White blood cells", "Erythrocytes", "C",
         "White blood cells are key cells of the immune system.",
         "Human Biology", "Easy"),

        ("What is the end product of glycolysis?",
         "Pyruvate", "Urea", "Lactose", "Cholesterol", "A",
         "One glucose molecule is converted into two pyruvate molecules.",
         "Biochemistry", "Medium"),

        ("Which part of the brain coordinates balance and movement?",
         "Cerebellum", "Medulla", "Hypothalamus", "Pituitary", "A",
         "The cerebellum is important for coordination, balance and motor control.",
         "Neurobiology", "Medium"),
    ]

    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO questions
        (question,option_a,option_b,option_c,option_d,correct_option,
         explanation,category,difficulty,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, [q + (now,) for q in questions])


def ensure_question_bank(conn):
    extras = [
        ("Which molecule stores hereditary information in cells?", "DNA", "ATP", "Glucose", "Cholesterol", "A", "DNA stores hereditary genetic information.", "Molecular Biology", "Easy"),
        ("Which organ pumps blood through the human circulatory system?", "Liver", "Heart", "Kidney", "Lung", "B", "The heart pumps blood through the circulation.", "Human Biology", "Easy"),
        ("Which cell structure is the main site of protein synthesis?", "Ribosome", "Lysosome", "Centrosome", "Vacuole", "A", "Ribosomes synthesize proteins.", "Cell Biology", "Easy"),
        ("Which blood protein carries oxygen?", "Insulin", "Hemoglobin", "Collagen", "Keratin", "B", "Hemoglobin binds and transports oxygen in red blood cells.", "Human Biology", "Medium"),
        ("Which process converts glucose to pyruvate?", "Glycolysis", "Translation", "Replication", "Transcription", "A", "Glycolysis breaks glucose into pyruvate.", "Biochemistry", "Medium"),
        ("Which organ is primarily responsible for detoxification and many metabolic processes?", "Heart", "Liver", "Spleen", "Thyroid", "B", "The liver performs many metabolic and detoxification functions.", "Human Biology", "Medium"),
        ("Which RNA carries the genetic message from DNA to the ribosome?", "tRNA", "mRNA", "rRNA", "miRNA", "B", "mRNA carries coding information to ribosomes.", "Molecular Biology", "Easy"),
        ("What is the movement of water across a selectively permeable membrane called?", "Diffusion", "Osmosis", "Active transport", "Endocytosis", "B", "Osmosis is the movement of water across a selectively permeable membrane.", "Cell Biology", "Medium"),
        ("Which organelle is known for intracellular digestion?", "Lysosome", "Ribosome", "Nucleolus", "Chloroplast", "A", "Lysosomes contain enzymes for intracellular digestion.", "Cell Biology", "Medium"),
        ("Which hormone is mainly associated with the fight-or-flight response?", "Insulin", "Adrenaline", "Melatonin", "Calcitonin", "B", "Adrenaline is a key hormone in the acute fight-or-flight response.", "Physiology", "Easy"),
    ]
    existing = {r[0] for r in conn.execute("SELECT question FROM questions").fetchall()}
    now = datetime.now().isoformat(timespec="seconds")
    rows = [q + (now,) for q in extras if q[0] not in existing]
    if rows:
        conn.executemany("""
            INSERT INTO questions
            (question,option_a,option_b,option_c,option_d,correct_option,
             explanation,category,difficulty,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, rows)


def seed_mini_challenges(conn):
    today = date.today()
    mini = [
        ("Which organelle contains chlorophyll in plant cells?", "Nucleus", "Chloroplast", "Ribosome", "Golgi apparatus", "B", "Chloroplasts contain chlorophyll."),
        ("Which molecule is directly used as cellular energy?", "DNA", "ATP", "RNA", "Glucose", "B", "ATP is the immediate energy currency of cells."),
        ("Which blood component is mainly involved in clotting?", "Red blood cells", "White blood cells", "Platelets", "Plasma", "C", "Platelets participate in clot formation."),
        ("Which hormone lowers blood glucose?", "Glucagon", "Insulin", "Adrenaline", "Thyroxine", "B", "Insulin lowers blood glucose by promoting uptake and storage."),
        ("Which organ filters blood and forms urine?", "Lung", "Kidney", "Stomach", "Pancreas", "B", "The kidneys filter blood and form urine."),
        ("Which structure contains the cell's chromosomes in a typical eukaryotic cell?", "Nucleus", "Ribosome", "Lysosome", "Golgi apparatus", "A", "The nucleus contains chromosomes in typical eukaryotic cells."),
        ("Which process uses oxygen to release energy from nutrients?", "Aerobic respiration", "Fermentation", "Transcription", "Replication", "A", "Aerobic respiration uses oxygen in energy production."),
        ("Which vitamin is important for normal blood clotting?", "Vitamin K", "Vitamin C", "Vitamin B1", "Vitamin B12", "A", "Vitamin K is important for synthesis of several clotting factors."),
        ("Which part of a neuron usually receives incoming signals?", "Axon", "Dendrites", "Myelin sheath", "Synaptic vesicle", "B", "Dendrites commonly receive incoming signals."),
        ("Which biomolecule class includes enzymes?", "Proteins", "Nucleic acids", "Minerals", "Water", "A", "Most enzymes are proteins."),
    ]
    for offset in range(30):
        d = today + timedelta(days=offset)
        for slot in range(1, MINI_CHALLENGES_PER_DAY + 1):
            q = mini[(offset * MINI_CHALLENGES_PER_DAY + slot - 1) % len(mini)]
            conn.execute("""
                INSERT OR IGNORE INTO mini_challenges
                (challenge_date,challenge_slot,question,option_a,option_b,option_c,option_d,
                 correct_option,explanation,active)
                VALUES (?,?,?,?,?,?,?,?,?,1)
            """, (d.isoformat(), slot, *q))


def ensure_mini_challenges(conn):
    # Fill missing slots for the next 30 days without overwriting admin content.
    today = date.today()
    mini = [
        ("Which organelle contains chlorophyll in plant cells?", "Nucleus", "Chloroplast", "Ribosome", "Golgi apparatus", "B", "Chloroplasts contain chlorophyll."),
        ("Which molecule is directly used as cellular energy?", "DNA", "ATP", "RNA", "Glucose", "B", "ATP is the immediate energy currency of cells."),
        ("Which blood component is mainly involved in clotting?", "Red blood cells", "White blood cells", "Platelets", "Plasma", "C", "Platelets participate in clot formation."),
        ("Which hormone lowers blood glucose?", "Glucagon", "Insulin", "Adrenaline", "Thyroxine", "B", "Insulin lowers blood glucose by promoting uptake and storage."),
        ("Which organ filters blood and forms urine?", "Lung", "Kidney", "Stomach", "Pancreas", "B", "The kidneys filter blood and form urine."),
        ("Which structure contains the cell's chromosomes in a typical eukaryotic cell?", "Nucleus", "Ribosome", "Lysosome", "Golgi apparatus", "A", "The nucleus contains chromosomes in typical eukaryotic cells."),
        ("Which process uses oxygen to release energy from nutrients?", "Aerobic respiration", "Fermentation", "Transcription", "Replication", "A", "Aerobic respiration uses oxygen in energy production."),
        ("Which vitamin is important for normal blood clotting?", "Vitamin K", "Vitamin C", "Vitamin B1", "Vitamin B12", "A", "Vitamin K is important for synthesis of several clotting factors."),
        ("Which part of a neuron usually receives incoming signals?", "Axon", "Dendrites", "Myelin sheath", "Synaptic vesicle", "B", "Dendrites commonly receive incoming signals."),
        ("Which biomolecule class includes enzymes?", "Proteins", "Nucleic acids", "Minerals", "Water", "A", "Most enzymes are proteins."),
    ]
    for offset in range(30):
        d = today + timedelta(days=offset)
        for slot in range(1, MINI_CHALLENGES_PER_DAY + 1):
            exists = conn.execute("SELECT 1 FROM mini_challenges WHERE challenge_date=? AND challenge_slot=?", (d.isoformat(), slot)).fetchone()
            if exists:
                continue
            q = mini[(offset * MINI_CHALLENGES_PER_DAY + slot - 1) % len(mini)]
            conn.execute("""INSERT INTO mini_challenges
                (challenge_date,challenge_slot,question,option_a,option_b,option_c,option_d,correct_option,explanation,active)
                VALUES (?,?,?,?,?,?,?,?,?,1)""", (d.isoformat(), slot, *q))


def seed_content(conn):
    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        ("tip", "Remember Enzymes", "For enzyme questions, identify the substrate and reaction first."),
        ("tip", "Genetics Tip", "Write the genotype carefully before working out a genetic cross."),
        ("tip", "Cell Biology Tip", "Connect each organelle with its main function."),
        ("tip", "Exam Tip", "Read every option before choosing your final answer."),
        ("news", "Welcome to Bio Warriors", "A new daily challenge is available every day."),
        ("image", "Biology Image of the Day", "Use the biology dashboard to explore daily learning content.")
    ]
    conn.executemany("""
        INSERT INTO content(content_type,title,body,created_at)
        VALUES (?,?,?,?)
    """, [x + (now,) for x in rows])


# ============================================================
# AUTH HELPERS
# ============================================================

def current_user():
    uid = session.get("user_pk")
    if not uid:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or not user["active"]:
            session.clear()
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            flash("Administrator access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "today": date.today().isoformat()
    }


# ============================================================
# TEMPLATE / UI
# ============================================================

PAGE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | Bio Warriors</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f4f7fb;color:#172033}
nav{background:#102a43;color:white;padding:14px 5%;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
nav .brand{font-size:21px;font-weight:800;margin-right:auto}
nav a{color:white;text-decoration:none;padding:8px 10px;border-radius:8px}
nav a:hover{background:#1d466b}
.container{max-width:1100px;margin:28px auto;padding:0 18px}
.hero{background:linear-gradient(135deg,#0f766e,#2563eb);color:white;border-radius:18px;padding:28px;margin-bottom:22px}
.card{background:white;border-radius:16px;padding:20px;margin:15px 0;box-shadow:0 5px 20px rgba(0,0,0,.07)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:15px}
.stat{font-size:28px;font-weight:800}
.muted{color:#64748b}
.btn{display:inline-block;border:0;border-radius:9px;padding:10px 15px;background:#2563eb;color:white;text-decoration:none;cursor:pointer}
.btn.secondary{background:#475569}
.btn.success{background:#059669}
.btn.danger{background:#dc2626}
.btn.warning{background:#d97706}
input,select,textarea{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:8px;margin:6px 0 12px}
label{font-weight:700}
table{width:100%;border-collapse:collapse;background:white}
th,td{padding:11px;border-bottom:1px solid #e2e8f0;text-align:left}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#e0f2fe;color:#075985;font-size:12px}
.flash{padding:12px;border-radius:9px;margin-bottom:10px;background:#dcfce7;color:#166534}
.flash.error{background:#fee2e2;color:#991b1b}
.option{display:block;background:#f8fafc;border:1px solid #cbd5e1;border-radius:10px;padding:13px;margin:10px 0;cursor:pointer}
.option:hover{background:#eef6ff}
.option.locked{cursor:not-allowed;opacity:.72}
.option.selected{border:2px solid #059669;background:#ecfdf5}
.timer{font-size:24px;font-weight:800;color:#dc2626;position:sticky;top:10px}
.progress{height:10px;background:#e2e8f0;border-radius:10px;overflow:hidden}
.progress>div{height:100%;background:#2563eb}
footer{text-align:center;padding:30px;color:#64748b}
small{color:#64748b}
</style>
</head>
<body>
<nav>
<div class="brand">🧬 Bio Warriors</div>
<a href="{{ url_for('dashboard') }}">Dashboard</a>
{% if current_user %}
<a href="{{ url_for('quiz') }}">Daily Quiz</a>
<a href="{{ url_for('mini_challenge') }}">Mini Challenge</a>
<a href="{{ url_for('leaderboard') }}">Leaderboard</a>
<a href="{{ url_for('history') }}">History</a>
<a href="{{ url_for('profile') }}">Profile</a>
<a href="{{ url_for('hall_of_fame') }}">Hall of Fame</a>
{% if current_user["role"] == "admin" %}
<a href="{{ url_for('admin_dashboard') }}">Admin</a>
{% endif %}
<a href="{{ url_for('logout') }}">Logout</a>
{% else %}
<a href="{{ url_for('login') }}">Login</a>
<a href="{{ url_for('register') }}">Register</a>
{% endif %}
</nav>

<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% for category,msg in messages %}
<div class="flash {{ 'error' if category == 'error' else '' }}">{{ msg }}</div>
{% endfor %}
{% endwith %}

{{ body|safe }}
</div>
<footer>Bio Warriors • Learn Biology • Practice Daily • Grow Your Score</footer>
</body>
</html>
"""


def page(title, body, **context):
    return render_template_string(PAGE, title=title, body=render_template_string(body, **context))


# ============================================================
# AUTH ROUTES
# ============================================================

AUTH_FORM = r"""
<div class="card" style="max-width:520px;margin:auto">
<h1>{{ heading }}</h1>
<p class="muted">{{ subtitle }}</p>
<form method="post">
{% if register %}
<label>Name</label>
<input name="name" required>
<label>User ID</label>
<input name="user_id" required>
<label>Email</label>
<input type="email" name="email" required>
{% endif %}
<label>{% if register %}Password{% else %}User ID or Email{% endif %}</label>
{% if register %}
<input type="password" name="password" minlength="6" required>
{% else %}
<input name="identity" required>
<label>Password</label>
<input type="password" name="password" required>
{% endif %}
<button class="btn" type="submit">{{ button }}</button>
</form>
</div>
"""


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        user_id = request.form.get("user_id", "").strip().lower()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not user_id or not email or len(password) < 6:
            flash("Complete all fields. Password must contain at least 6 characters.", "error")
        else:
            conn = get_db()
            try:
                conn.execute("""
                    INSERT INTO users
                    (user_id,name,email,password_hash,created_at)
                    VALUES (?,?,?,?,?)
                """, (
                    user_id, name, email,
                    generate_password_hash(password),
                    datetime.now().isoformat(timespec="seconds")
                ))
                conn.commit()
                flash("Registration successful. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("User ID or email already exists.", "error")
            finally:
                conn.close()

    return page(
        "Register",
        AUTH_FORM,
        heading="Create Bio Warriors Account",
        subtitle="Start your daily biology journey.",
        register=True,
        button="Create Account"
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identity = request.form.get("identity", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute("""
            SELECT * FROM users
            WHERE lower(user_id)=? OR lower(email)=?
        """, (identity, identity)).fetchone()
        conn.close()

        if user and user["active"] and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_pk"] = user["id"]
            flash("Welcome back, " + user["name"] + "!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid login details or inactive account.", "error")

    conn = get_db()
    live_leaders = conn.execute("""
        SELECT name,user_id,points,streak FROM users
        WHERE role='user' AND active=1
        ORDER BY points DESC, streak DESC, name ASC
        LIMIT 10
    """).fetchall()
    conn.close()

    login_body = AUTH_FORM + r"""
<div class="card">
<h2>🏆 Live Leaderboard</h2>
<p class="muted">Top Bio Warriors right now</p>
<table>
<tr><th>#</th><th>Warrior</th><th>Points</th><th>🔥 Streak</th></tr>
{% for u in live_leaders %}
<tr><td>#{{ loop.index }}</td><td>{{ u["name"] }}</td><td><b>{{ u["points"] }}</b></td><td>{{ u["streak"] }}</td></tr>
{% endfor %}
</table>
</div>
"""
    return page(
        "Login", login_body,
        heading="Welcome Back",
        subtitle="Log in to continue your Bio Warriors journey.",
        register=False,
        button="Login",
        live_leaders=live_leaders
    )


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
@login_required
def dashboard():
    user = current_user()
    conn = get_db()

    stats = conn.execute("""
        SELECT
        COUNT(*) AS quizzes,
        COALESCE(SUM(correct),0) AS correct,
        COALESCE(SUM(total),0) AS total,
        COALESCE(SUM(points),0) AS quiz_points
        FROM quiz_sessions
        WHERE user_id=? AND submitted_at IS NOT NULL
    """, (user["id"],)).fetchone()

    mini_count = conn.execute("""
        SELECT COUNT(*) AS c FROM mini_attempts WHERE user_id=?
    """, (user["id"],)).fetchone()["c"]

    tips = conn.execute("""
        SELECT * FROM content WHERE active=1
        ORDER BY id DESC LIMIT 4
    """).fetchall()

    conn.close()

    accuracy = round((stats["correct"] / stats["total"]) * 100, 1) if stats["total"] else 0

    body = r"""
<div class="hero">
<h1>Welcome, {{ user["name"] }} 👋</h1>
<p>Ready for today's biology challenge?</p>
<a class="btn success" href="{{ url_for('quiz') }}">Start Daily Quiz</a>
<a class="btn" href="{{ url_for('mini_challenge') }}">Brain Booster</a>
</div>

<div class="grid">
<div class="card"><div class="muted">Total Points</div><div class="stat">{{ user["points"] }}</div></div>
<div class="card"><div class="muted">Quiz Attempts</div><div class="stat">{{ stats["quizzes"] }}</div></div>
<div class="card"><div class="muted">Accuracy</div><div class="stat">{{ accuracy }}%</div></div>
<div class="card"><div class="muted">Current Streak</div><div class="stat">🔥 {{ user["streak"] }}</div></div>
<div class="card"><div class="muted">Mini Challenges</div><div class="stat">{{ mini_count }}</div></div>
</div>

<div class="card">
<h2>Today's Learning Tips</h2>
{% for t in tips %}
<p><b>{{ t["title"] }}</b> — {{ t["body"] }}</p>
{% endfor %}
</div>

<div class="card">
<h2>Quick Links</h2>
<a class="btn secondary" href="{{ url_for('leaderboard') }}">🏆 Leaderboard</a>
<a class="btn secondary" href="{{ url_for('history') }}">📚 My History</a>
<a class="btn secondary" href="{{ url_for('profile') }}">👤 Profile</a>
<a class="btn secondary" href="{{ url_for('hall_of_fame') }}">⭐ Hall of Fame</a>
</div>
"""
    return page("Dashboard", body, user=user, stats=stats, mini_count=mini_count,
                tips=tips, accuracy=accuracy)


# ============================================================
# QUIZ
# ============================================================

def get_or_create_daily_session(user_id):
    today = date.today().isoformat()
    conn = get_db()

    existing = conn.execute("""
        SELECT * FROM quiz_sessions
        WHERE user_id=? AND quiz_date=?
        ORDER BY id DESC LIMIT 1
    """, (user_id, today)).fetchone()

    if existing:
        conn.close()
        return existing

    questions = conn.execute("""
        SELECT id FROM questions WHERE active=1
        ORDER BY RANDOM() LIMIT ?
    """, (QUESTIONS_PER_DAY,)).fetchall()

    if not questions:
        conn.close()
        return None

    ids = ",".join(str(q["id"]) for q in questions)
    cur = conn.execute("""
        INSERT INTO quiz_sessions
        (user_id,quiz_date,question_ids,started_at,total)
        VALUES (?,?,?,?,?)
    """, (
        user_id, today, ids,
        datetime.now().isoformat(timespec="seconds"),
        len(questions)
    ))
    conn.commit()

    session_id = cur.lastrowid
    row = conn.execute(
        "SELECT * FROM quiz_sessions WHERE id=?", (session_id,)
    ).fetchone()
    conn.close()
    return row


@app.route("/quiz")
@login_required
def quiz():
    user = current_user()
    qsession = get_or_create_daily_session(user["id"])

    if not qsession:
        flash("No active questions are available.", "error")
        return redirect(url_for("dashboard"))

    if qsession["submitted_at"]:
        return redirect(url_for("quiz_result", session_id=qsession["id"]))

    conn = get_db()
    ids = [int(x) for x in qsession["question_ids"].split(",") if x]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM questions WHERE id IN ({placeholders})",
        ids
    ).fetchall()
    conn.close()

    # Keep the stored question order.
    by_id = {r["id"]: r for r in rows}
    questions = [by_id[i] for i in ids if i in by_id]

    body = r"""
<div class="card">
<div style="display:flex;justify-content:space-between;gap:20px;align-items:center">
<div>
<h1>🧠 Today's Biology Quiz</h1>
<p>{{ questions|length }} questions • {{ minutes }} minutes</p>
</div>
<div class="timer" id="timer">{{ minutes }}:00</div>
</div>
<div class="progress"><div style="width:{{ 100 / questions|length }}%"></div></div>
</div>

<form method="post" action="{{ url_for('submit_quiz') }}" id="quizForm">
{% for q in questions %}
<div class="card">
<h2>Question {{ loop.index }} of {{ questions|length }}</h2>
<p><b>{{ q["question"] }}</b></p>
{% for letter,text in [('A',q["option_a"]),('B',q["option_b"]),('C',q["option_c"]),('D',q["option_d"])] %}
<label class="option">
<input type="radio" name="q_{{ q["id"] }}" value="{{ letter }}">
<b>{{ letter }}.</b> {{ text }}
</label>
{% endfor %}
</div>
{% endfor %}
<button class="btn success" type="submit">Submit Quiz</button>
</form>

<script>
document.querySelectorAll('#quizForm input[type="radio"]').forEach(radio => {
  radio.addEventListener('change', function() {
    const group = document.querySelectorAll('input[name="' + this.name + '"]');
    group.forEach(r => { if (r !== this) r.disabled = true; });
    group.forEach(r => r.closest('.option').classList.add('locked'));
    this.closest('.option').classList.add('selected');
  }, {once:true});
});

let seconds={{ minutes }}*60;
const timer=document.getElementById("timer");
const form=document.getElementById("quizForm");
const tick=setInterval(()=>{
    seconds--;
    if(seconds<=0){
        clearInterval(tick);
        form.submit();
        return;
    }
    let m=Math.floor(seconds/60);
    let s=seconds%60;
    timer.textContent=m+":"+(s<10?"0":"")+s;
},1000);
</script>
"""
    return page("Daily Quiz", body, questions=questions, minutes=QUIZ_MINUTES)


@app.route("/submit-quiz", methods=["POST"])
@login_required
def submit_quiz():
    user = current_user()
    qsession = get_or_create_daily_session(user["id"])

    if not qsession:
        return redirect(url_for("dashboard"))

    if qsession["submitted_at"]:
        return redirect(url_for("quiz_result", session_id=qsession["id"]))

    conn = get_db()
    ids = [int(x) for x in qsession["question_ids"].split(",") if x]

    correct = 0
    for qid in ids:
        q = conn.execute(
            "SELECT * FROM questions WHERE id=?", (qid,)
        ).fetchone()
        selected = request.form.get("q_" + str(qid), "")
        is_correct = int(bool(q and selected == q["correct_option"]))
        correct += is_correct

        conn.execute("""
            INSERT INTO answers(session_id,question_id,selected_option,is_correct)
            VALUES (?,?,?,?)
        """, (qsession["id"], qid, selected or None, is_correct))

    total = len(ids)
    score = round((correct / total) * 100) if total else 0
    points = correct * (DAILY_QUIZ_POINTS // max(total, 1))

    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        UPDATE quiz_sessions
        SET submitted_at=?,score=?,correct=?,total=?,points=?
        WHERE id=?
    """, (now, score, correct, total, points, qsession["id"]))

    # Update streak once per completed daily quiz.
    old = conn.execute(
        "SELECT last_quiz_date,streak,best_streak,points FROM users WHERE id=?",
        (user["id"],)
    ).fetchone()

    today = date.today()
    previous = None
    if old["last_quiz_date"]:
        try:
            previous = date.fromisoformat(old["last_quiz_date"])
        except ValueError:
            previous = None

    if previous == today:
        new_streak = old["streak"]
    elif previous == today - timedelta(days=1):
        new_streak = old["streak"] + 1
    else:
        new_streak = 1

    best = max(old["best_streak"], new_streak)

    conn.execute("""
        UPDATE users
        SET points=points+?,streak=?,best_streak=?,last_quiz_date=?
        WHERE id=?
    """, (points, new_streak, best, today.isoformat(), user["id"]))

    conn.commit()
    conn.close()

    return redirect(url_for("quiz_result", session_id=qsession["id"]))


@app.route("/quiz/result/<int:session_id>")
@login_required
def quiz_result(session_id):
    user = current_user()
    conn = get_db()

    qsession = conn.execute("""
        SELECT * FROM quiz_sessions WHERE id=? AND user_id=?
    """, (session_id, user["id"])).fetchone()

    if not qsession:
        conn.close()
        flash("Quiz result not found.", "error")
        return redirect(url_for("dashboard"))

    answers = conn.execute("""
        SELECT a.*,q.question,q.correct_option,q.explanation
        FROM answers a
        JOIN questions q ON q.id=a.question_id
        WHERE a.session_id=?
        ORDER BY a.id
    """, (session_id,)).fetchall()

    conn.close()

    body = r"""
<div class="hero">
<h1>🎉 Quiz Complete!</h1>
<p>Score: <b>{{ qs["score"] }}%</b></p>
<p>Correct: {{ qs["correct"] }} / {{ qs["total"] }} • Points earned: +{{ qs["points"] }}</p>
<a class="btn success" href="{{ url_for('dashboard') }}">Back to Dashboard</a>
</div>

<div class="card">
<h2>Answer Review</h2>
{% for a in answers %}
<div style="padding:14px 0;border-bottom:1px solid #e2e8f0">
<p><b>{{ loop.index }}. {{ a["question"] }}</b></p>
<p>Your answer:
{% if a["is_correct"] %}
<span class="badge">Correct</span>
{% else %}
<span class="badge" style="background:#fee2e2;color:#991b1b">Incorrect</span>
{% endif %}
</p>
<p><small>{{ a["explanation"] }}</small></p>
</div>
{% endfor %}
</div>
"""
    return page("Quiz Result", body, qs=qsession, answers=answers)


# ============================================================
# MINI CHALLENGE
# ============================================================

@app.route("/mini-challenge", methods=["GET", "POST"])
@login_required
def mini_challenge():
    user = current_user()
    today = date.today().isoformat()
    conn = get_db()

    challenges = conn.execute("""
        SELECT * FROM mini_challenges
        WHERE challenge_date=? AND active=1
        ORDER BY challenge_slot
        LIMIT ?
    """, (today, MINI_CHALLENGES_PER_DAY)).fetchall()

    if not challenges:
        conn.close()
        flash("No mini challenges are configured for today.", "error")
        return redirect(url_for("dashboard"))

    attempts = {r["challenge_id"]: r for r in conn.execute("""
        SELECT * FROM mini_attempts WHERE user_id=? AND challenge_id IN (%s)
    """ % ",".join("?" * len(challenges)), (user["id"], *[c["id"] for c in challenges])).fetchall()}

    if request.method == "POST":
        for challenge in challenges:
            if challenge["id"] in attempts:
                continue
            selected = request.form.get("answer_" + str(challenge["id"]), "").strip().upper()
            if selected not in "ABCD":
                continue
            is_correct = int(selected == challenge["correct_option"])
            try:
                conn.execute("""
                    INSERT INTO mini_attempts
                    (user_id,challenge_id,selected_option,is_correct,attempted_at)
                    VALUES (?,?,?,?,?)
                """, (user["id"], challenge["id"], selected, is_correct, datetime.now().isoformat(timespec="seconds")))
                if is_correct:
                    conn.execute("UPDATE users SET points=points+? WHERE id=?", (MINI_POINTS, user["id"]))
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        attempts = {r["challenge_id"]: r for r in conn.execute("""
            SELECT * FROM mini_attempts WHERE user_id=? AND challenge_id IN (%s)
        """ % ",".join("?" * len(challenges)), (user["id"], *[c["id"] for c in challenges])).fetchall()}

    conn.close()

    body = r"""
<div class="card">
<h1>🎯 Brain Booster — 5 Mini Challenges</h1>
<p class="muted">{{ today }} • Select an answer once — it locks immediately.</p>
</div>
{% for challenge in challenges %}
<div class="card mini-card {% if challenge['id'] in attempts %}answered{% endif %}">
<h2>Challenge {{ loop.index }} of {{ challenges|length }}</h2>
<h3>{{ challenge["question"] }}</h3>
{% if challenge['id'] not in attempts %}
<form method="post" class="mini-form" data-challenge="{{ challenge['id'] }}">
{% for letter,text in [('A',challenge["option_a"]),('B',challenge["option_b"]),('C',challenge["option_c"]),('D',challenge["option_d"])] %}
<label class="option">
<input type="radio" name="answer_{{ challenge['id'] }}" value="{{ letter }}" required>
<b>{{ letter }}.</b> {{ text }}
</label>
{% endfor %}
<button class="btn success" type="submit">Submit Challenge</button>
</form>
{% else %}
{% set attempt = attempts[challenge['id']] %}
{% if attempt["is_correct"] %}
<div class="flash">✅ Correct! +{{ points }} points</div>
{% else %}
<div class="flash error">❌ Not correct. Correct answer: {{ challenge["correct_option"] }}</div>
{% endif %}
<p><b>Explanation:</b> {{ challenge["explanation"] }}</p>
{% endif %}
</div>
{% endfor %}

<script>
document.querySelectorAll('.mini-form').forEach(form => {
  form.addEventListener('change', () => {
    const chosen = form.querySelector('input[type=radio]:checked');
    if (!chosen) return;
    form.querySelectorAll('input[type=radio]').forEach(r => { if (r !== chosen) r.disabled = true; });
    form.querySelectorAll('.option').forEach(label => label.classList.add('locked'));
    chosen.closest('.option').classList.add('selected');
  }, {once:true});
});
</script>
"""
    return page("Mini Challenge", body, challenges=challenges, attempts=attempts, points=MINI_POINTS, today=today)


# ============================================================
# LEADERBOARD / HISTORY
# ============================================================

@app.route("/leaderboard")
@login_required
def leaderboard():
    conn = get_db()
    leaders = conn.execute("""
        SELECT name,user_id,points,streak,best_streak
        FROM users
        WHERE role='user' AND active=1
        ORDER BY points DESC, best_streak DESC, name ASC
        LIMIT 50
    """).fetchall()
    conn.close()

    body = r"""
<div class="card">
<h1>🏆 Leaderboard</h1>
<table>
<tr><th>Rank</th><th>Warrior</th><th>Points</th><th>Streak</th><th>Best Streak</th></tr>
{% for u in leaders %}
<tr>
<td><b>#{{ loop.index }}</b></td>
<td>{{ u["name"] }} <small>@{{ u["user_id"] }}</small></td>
<td><b>{{ u["points"] }}</b></td>
<td>🔥 {{ u["streak"] }}</td>
<td>{{ u["best_streak"] }}</td>
</tr>
{% endfor %}
</table>
</div>
"""
    return page("Leaderboard", body, leaders=leaders)


@app.route("/history")
@login_required
def history():
    user = current_user()
    conn = get_db()

    rows = conn.execute("""
        SELECT * FROM quiz_sessions
        WHERE user_id=? AND submitted_at IS NOT NULL
        ORDER BY quiz_date DESC,id DESC
    """, (user["id"],)).fetchall()

    conn.close()

    body = r"""
<div class="card">
<h1>📚 Quiz History</h1>
{% if rows %}
<table>
<tr><th>Date</th><th>Score</th><th>Correct</th><th>Points</th><th>Review</th></tr>
{% for r in rows %}
<tr>
<td>{{ r["quiz_date"] }}</td>
<td>{{ r["score"] }}%</td>
<td>{{ r["correct"] }}/{{ r["total"] }}</td>
<td>+{{ r["points"] }}</td>
<td><a class="btn secondary" href="{{ url_for('quiz_result',session_id=r['id']) }}">View</a></td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No completed quizzes yet. Start today's quiz!</p>
<a class="btn" href="{{ url_for('quiz') }}">Start Quiz</a>
{% endif %}
</div>
"""
    return page("History", body, rows=rows)


# ============================================================
# PROFILE
# ============================================================

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not name or not email:
            flash("Name and email cannot be empty.", "error")
        else:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE users SET name=?,email=? WHERE id=?",
                    (name, email, user["id"])
                )
                conn.commit()
                flash("Profile updated.", "success")
            except sqlite3.IntegrityError:
                flash("That email is already in use.", "error")
            finally:
                conn.close()

    user = current_user()

    conn = get_db()
    metrics = conn.execute("""
        SELECT
        COUNT(*) quizzes,
        COALESCE(SUM(correct),0) correct,
        COALESCE(SUM(total),0) total,
        COALESCE(SUM(points),0) quiz_points
        FROM quiz_sessions
        WHERE user_id=? AND submitted_at IS NOT NULL
    """, (user["id"],)).fetchone()
    conn.close()

    accuracy = round(metrics["correct"] / metrics["total"] * 100, 1) if metrics["total"] else 0

    body = r"""
<div class="card">
<h1>👤 My Profile</h1>
<form method="post">
<label>Name</label>
<input name="name" value="{{ user['name'] }}" required>
<label>Email</label>
<input type="email" name="email" value="{{ user['email'] }}" required>
<button class="btn" type="submit">Save Profile</button>
</form>
</div>

<div class="grid">
<div class="card"><div class="muted">Points</div><div class="stat">{{ user["points"] }}</div></div>
<div class="card"><div class="muted">Accuracy</div><div class="stat">{{ accuracy }}%</div></div>
<div class="card"><div class="muted">Current Streak</div><div class="stat">🔥 {{ user["streak"] }}</div></div>
<div class="card"><div class="muted">Best Streak</div><div class="stat">{{ user["best_streak"] }}</div></div>
</div>

<div class="card">
<h2>Account</h2>
<p><b>User ID:</b> {{ user["user_id"] }}</p>
<p><b>Member since:</b> {{ user["created_at"] }}</p>
<a class="btn warning" href="{{ url_for('change_password') }}">Change Password</a>
</div>
"""
    return page("Profile", body, user=user, metrics=metrics, accuracy=accuracy)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()

    if request.method == "POST":
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not check_password_hash(user["password_hash"], old):
            flash("Current password is incorrect.", "error")
        elif len(new) < 6:
            flash("New password must contain at least 6 characters.", "error")
        elif new != confirm:
            flash("New passwords do not match.", "error")
        else:
            conn = get_db()
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(new), user["id"])
            )
            conn.commit()
            conn.close()
            flash("Password changed successfully.", "success")
            return redirect(url_for("profile"))

    body = r"""
<div class="card" style="max-width:550px;margin:auto">
<h1>🔐 Change Password</h1>
<form method="post">
<label>Current Password</label>
<input type="password" name="old_password" required>
<label>New Password</label>
<input type="password" name="new_password" minlength="6" required>
<label>Confirm New Password</label>
<input type="password" name="confirm_password" minlength="6" required>
<button class="btn success" type="submit">Change Password</button>
</form>
</div>
"""
    return page("Change Password", body)


# ============================================================
# HALL OF FAME
# ============================================================

@app.route("/hall-of-fame")
@login_required
def hall_of_fame():
    conn = get_db()
    months = []

    cur = date.today().replace(day=1)

    for _ in range(12):
        start = cur.replace(day=1)
        if start.month == 12:
            nxt = start.replace(year=start.year + 1, month=1)
        else:
            nxt = start.replace(month=start.month + 1)

        row = conn.execute("""
            SELECT u.name,u.user_id,COALESCE(SUM(q.points),0) AS points
            FROM quiz_sessions q
            JOIN users u ON u.id=q.user_id
            WHERE q.submitted_at IS NOT NULL
              AND q.quiz_date>=? AND q.quiz_date<?
            GROUP BY u.id
            ORDER BY points DESC
            LIMIT 1
        """, (start.isoformat(), nxt.isoformat())).fetchone()

        if row:
            months.append({
                "month": start.strftime("%B %Y"),
                "name": row["name"],
                "user_id": row["user_id"],
                "points": row["points"]
            })

        cur = start - timedelta(days=1)
        cur = cur.replace(day=1)

    conn.close()

    body = r"""
<div class="card">
<h1>⭐ Hall of Fame</h1>
<p class="muted">Monthly Bio Warriors champions.</p>
{% if months %}
<table>
<tr><th>Month</th><th>Champion</th><th>Points</th></tr>
{% for m in months %}
<tr><td>{{ m["month"] }}</td><td>🏆 {{ m["name"] }} <small>@{{ m["user_id"] }}</small></td><td>{{ m["points"] }}</td></tr>
{% endfor %}
</table>
{% else %}
<p>No monthly champion data yet. Complete quizzes to appear here.</p>
{% endif %}
</div>
"""
    return page("Hall of Fame", body, months=months)


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()

    users = conn.execute("SELECT COUNT(*) c FROM users WHERE role='user'").fetchone()["c"]
    active_users = conn.execute("SELECT COUNT(*) c FROM users WHERE role='user' AND active=1").fetchone()["c"]
    questions = conn.execute("SELECT COUNT(*) c FROM questions WHERE active=1").fetchone()["c"]
    quizzes = conn.execute("SELECT COUNT(*) c FROM quiz_sessions WHERE submitted_at IS NOT NULL").fetchone()["c"]
    attempts = conn.execute("SELECT COUNT(*) c FROM mini_attempts").fetchone()["c"]
    points = conn.execute("SELECT COALESCE(SUM(points),0) p FROM users WHERE role='user'").fetchone()["p"]

    top = conn.execute("""
        SELECT name,user_id,points FROM users
        WHERE role='user'
        ORDER BY points DESC LIMIT 10
    """).fetchall()

    conn.close()

    body = r"""
<div class="hero">
<h1>🛠 Admin Dashboard</h1>
<p>Manage Bio Warriors from one place.</p>
</div>

<div class="grid">
<div class="card"><div class="muted">Users</div><div class="stat">{{ users }}</div></div>
<div class="card"><div class="muted">Active Users</div><div class="stat">{{ active_users }}</div></div>
<div class="card"><div class="muted">Questions</div><div class="stat">{{ questions }}</div></div>
<div class="card"><div class="muted">Completed Quizzes</div><div class="stat">{{ quizzes }}</div></div>
<div class="card"><div class="muted">Mini Attempts</div><div class="stat">{{ attempts }}</div></div>
<div class="card"><div class="muted">Total User Points</div><div class="stat">{{ points }}</div></div>
</div>

<div class="card">
<h2>Admin Tools</h2>
<a class="btn" href="{{ url_for('admin_users') }}">Users</a>
<a class="btn" href="{{ url_for('admin_questions') }}">Questions</a>
<a class="btn" href="{{ url_for('admin_mini') }}">Mini Challenges</a>
<a class="btn" href="{{ url_for('admin_content') }}">Content</a>
<a class="btn" href="{{ url_for('admin_analytics') }}">Analytics</a>
</div>

<div class="card">
<h2>Top Warriors</h2>
<table>
<tr><th>Rank</th><th>Name</th><th>Points</th></tr>
{% for u in top %}
<tr><td>#{{ loop.index }}</td><td>{{ u["name"] }}</td><td>{{ u["points"] }}</td></tr>
{% endfor %}
</table>
</div>
"""
    return page("Admin", body, users=users, active_users=active_users,
                questions=questions, quizzes=quizzes, attempts=attempts,
                points=points, top=top)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    conn = get_db()

    if request.method == "POST":
        user_id = request.form.get("user_id", "").strip().lower()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not user_id or not name or not email or len(password) < 6:
            flash("Complete all fields. Password must contain at least 6 characters.", "error")
        else:
            try:
                conn.execute("""
                    INSERT INTO users
                    (user_id,name,email,password_hash,created_at)
                    VALUES (?,?,?,?,?)
                """, (
                    user_id,name,email,generate_password_hash(password),
                    datetime.now().isoformat(timespec="seconds")
                ))
                conn.commit()
                flash("User created.", "success")
            except sqlite3.IntegrityError:
                flash("User ID or email already exists.", "error")

    users = conn.execute("""
        SELECT id,user_id,name,email,role,active,points,streak,created_at
        FROM users ORDER BY id DESC
    """).fetchall()
    conn.close()

    body = r"""
<div class="card">
<h1>👥 User Management</h1>
<form method="post">
<div class="grid">
<div><label>User ID</label><input name="user_id" required></div>
<div><label>Name</label><input name="name" required></div>
<div><label>Email</label><input name="email" required></div>
<div><label>Temporary Password</label><input type="password" name="password" minlength="6" required></div>
</div>
<button class="btn" type="submit">Create User</button>
</form>
</div>

<div class="card">
<table>
<tr><th>User</th><th>Email</th><th>Role</th><th>Active</th><th>Points</th><th>Action</th></tr>
{% for u in users %}
<tr>
<td>{{ u["name"] }}<br><small>@{{ u["user_id"] }}</small></td>
<td>{{ u["email"] }}</td>
<td>{{ u["role"] }}</td>
<td>{{ "Yes" if u["active"] else "No" }}</td>
<td>{{ u["points"] }}</td>
<td>
{% if u["role"] != "admin" %}
<a class="btn {{ 'danger' if u['active'] else 'success' }}"
href="{{ url_for('toggle_user',user_id=u['id']) }}">
{{ "Disable" if u["active"] else "Enable" }}
</a>
{% endif %}
</td>
</tr>
{% endfor %}
</table>
</div>
"""
    return page("Admin Users", body, users=users)


@app.route("/admin/users/<int:user_id>/toggle")
@admin_required
def toggle_user(user_id):
    conn = get_db()
    conn.execute("""
        UPDATE users SET active=CASE WHEN active=1 THEN 0 ELSE 1 END
        WHERE id=? AND role!='admin'
    """, (user_id,))
    conn.commit()
    conn.close()
    flash("User status updated.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/questions", methods=["GET", "POST"])
@admin_required
def admin_questions():
    conn = get_db()

    if request.method == "POST":
        added = 0
        for i in range(1, 11):
            prefix = f"q{i}_"
            values = [
                request.form.get(prefix + "question", "").strip(),
                request.form.get(prefix + "option_a", "").strip(),
                request.form.get(prefix + "option_b", "").strip(),
                request.form.get(prefix + "option_c", "").strip(),
                request.form.get(prefix + "option_d", "").strip(),
                request.form.get(prefix + "correct_option", "").strip().upper(),
                request.form.get(prefix + "explanation", "").strip(),
                request.form.get(prefix + "category", "Biology").strip(),
                request.form.get(prefix + "difficulty", "Medium").strip()
            ]
            # Empty rows are allowed so admins can add fewer than 10 in one batch.
            if not any(values[:6]):
                continue
            if not all(values[:6]) or values[5] not in "ABCD":
                flash(f"Question {i} is incomplete. Please fill all required fields.", "error")
                continue
            conn.execute("""
                INSERT INTO questions
                (question,option_a,option_b,option_c,option_d,correct_option,
                 explanation,category,difficulty,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (*values, datetime.now().isoformat(timespec="seconds")))
            added += 1
        conn.commit()
        if added:
            flash(f"{added} question(s) added successfully.", "success")

    questions = conn.execute("SELECT * FROM questions ORDER BY id DESC").fetchall()
    conn.close()

    body = r"""
<div class="card">
<h1>📝 Question Bank — Add 10 at a Time</h1>
<p class="muted">Fill up to 10 questions and save them together. Empty rows are skipped.</p>
<form method="post">
{% for i in range(1,11) %}
<div class="card" style="border:1px solid #e2e8f0;box-shadow:none">
<h2>Question {{ i }}</h2>
<label>Question</label><textarea name="q{{ i }}_question" {% if i == 1 %}required{% endif %}></textarea>
<div class="grid">
<div><label>A</label><input name="q{{ i }}_option_a"></div>
<div><label>B</label><input name="q{{ i }}_option_b"></div>
<div><label>C</label><input name="q{{ i }}_option_c"></div>
<div><label>D</label><input name="q{{ i }}_option_d"></div>
</div>
<div class="grid">
<div><label>Correct Option</label><select name="q{{ i }}_correct_option"><option>A</option><option>B</option><option>C</option><option>D</option></select></div>
<div><label>Category</label><input name="q{{ i }}_category" value="Biology"></div>
<div><label>Difficulty</label><select name="q{{ i }}_difficulty"><option>Easy</option><option>Medium</option><option>Hard</option></select></div>
</div>
<label>Explanation</label><textarea name="q{{ i }}_explanation"></textarea>
</div>
{% endfor %}
<button class="btn success" type="submit">➕ Add 10 Questions</button>
</form>
</div>

<div class="card">
<h2>Question Bank</h2>
<table>
<tr><th>ID</th><th>Question</th><th>Category</th><th>Difficulty</th><th>Active</th></tr>
{% for q in questions %}
<tr><td>{{ q["id"] }}</td><td>{{ q["question"] }}</td><td>{{ q["category"] }}</td><td>{{ q["difficulty"] }}</td><td>{{ "Yes" if q["active"] else "No" }}</td></tr>
{% endfor %}
</table>
</div>
"""
    return page("Admin Questions", body, questions=questions)


@app.route("/admin/mini", methods=["GET", "POST"])
@admin_required
def admin_mini():
    conn = get_db()

    if request.method == "POST":
        values = [
            request.form.get("challenge_date", "").strip(),
            int(request.form.get("challenge_slot", "1") or 1),
            request.form.get("question", "").strip(),
            request.form.get("option_a", "").strip(),
            request.form.get("option_b", "").strip(),
            request.form.get("option_c", "").strip(),
            request.form.get("option_d", "").strip(),
            request.form.get("correct_option", "").strip().upper(),
            request.form.get("explanation", "").strip()
        ]
        if not values[0] or not 1 <= values[1] <= MINI_CHALLENGES_PER_DAY or not all(values[2:7]) or values[7] not in "ABCD":
            flash("Complete all mini challenge fields and choose a slot from 1 to 5.", "error")
        else:
            try:
                conn.execute("""
                    INSERT INTO mini_challenges
                    (challenge_date,challenge_slot,question,option_a,option_b,option_c,option_d,
                     correct_option,explanation,active)
                    VALUES (?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(challenge_date,challenge_slot) DO UPDATE SET
                      question=excluded.question, option_a=excluded.option_a, option_b=excluded.option_b,
                      option_c=excluded.option_c, option_d=excluded.option_d, correct_option=excluded.correct_option,
                      explanation=excluded.explanation, active=1
                """, values)
                conn.commit()
                flash("Mini challenge saved.", "success")
            except sqlite3.Error as e:
                flash("Could not save mini challenge: " + str(e), "error")

    challenges = conn.execute("SELECT * FROM mini_challenges ORDER BY challenge_date DESC, challenge_slot LIMIT 100").fetchall()
    conn.close()

    body = r"""
<div class="card">
<h1>🎯 Mini Challenge Manager — 5 Per Day</h1>
<form method="post">
<div class="grid">
<div><label>Date</label><input type="date" name="challenge_date" value="{{ today }}" required></div>
<div><label>Challenge Slot</label><select name="challenge_slot">{% for n in range(1,6) %}<option value="{{ n }}">{{ n }}</option>{% endfor %}</select></div>
<div><label>Correct Option</label><select name="correct_option"><option>A</option><option>B</option><option>C</option><option>D</option></select></div>
</div>
<label>Question</label><textarea name="question" required></textarea>
<div class="grid">
<div><label>A</label><input name="option_a" required></div><div><label>B</label><input name="option_b" required></div><div><label>C</label><input name="option_c" required></div><div><label>D</label><input name="option_d" required></div>
</div>
<label>Explanation</label><textarea name="explanation"></textarea>
<button class="btn success">Save Challenge</button>
</form>
</div>
<div class="card"><table><tr><th>Date</th><th>Slot</th><th>Question</th><th>Answer</th></tr>
{% for c in challenges %}<tr><td>{{ c["challenge_date"] }}</td><td>{{ c["challenge_slot"] }}</td><td>{{ c["question"] }}</td><td>{{ c["correct_option"] }}</td></tr>{% endfor %}
</table></div>
"""
    return page("Admin Mini Challenges", body, challenges=challenges)


@app.route("/admin/content", methods=["GET", "POST"])
@admin_required
def admin_content():
    conn = get_db()

    if request.method == "POST":
        content_type = request.form.get("content_type", "tip").strip()
        title = request.form.get("title", "").strip()
        body_text = request.form.get("body", "").strip()

        if not title or not body_text:
            flash("Title and content are required.", "error")
        else:
            conn.execute("""
                INSERT INTO content(content_type,title,body,created_at)
                VALUES (?,?,?,?)
            """, (
                content_type,title,body_text,
                datetime.now().isoformat(timespec="seconds")
            ))
            conn.commit()
            flash("Content added.", "success")

    rows = conn.execute(
        "SELECT * FROM content ORDER BY id DESC"
    ).fetchall()
    conn.close()

    body = r"""
<div class="card">
<h1>📢 Content Manager</h1>
<form method="post">
<label>Type</label>
<select name="content_type">
<option value="tip">Tip</option>
<option value="news">News</option>
<option value="image">Image</option>
</select>
<label>Title</label><input name="title" required>
<label>Body</label><textarea name="body" required></textarea>
<button class="btn success">Add Content</button>
</form>
</div>

<div class="card">
<table>
<tr><th>Type</th><th>Title</th><th>Content</th><th>Active</th></tr>
{% for r in rows %}
<tr><td>{{ r["content_type"] }}</td><td>{{ r["title"] }}</td><td>{{ r["body"] }}</td><td>{{ r["active"] }}</td></tr>
{% endfor %}
</table>
</div>
"""
    return page("Admin Content", body, rows=rows)


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    conn = get_db()

    total_users = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE role='user'"
    ).fetchone()["c"]

    total_quizzes = conn.execute(
        "SELECT COUNT(*) c FROM quiz_sessions WHERE submitted_at IS NOT NULL"
    ).fetchone()["c"]

    avg_score = conn.execute(
        "SELECT COALESCE(AVG(score),0) a FROM quiz_sessions WHERE submitted_at IS NOT NULL"
    ).fetchone()["a"]

    mini_correct = conn.execute(
        "SELECT COALESCE(SUM(is_correct),0) c FROM mini_attempts"
    ).fetchone()["c"]

    mini_total = conn.execute(
        "SELECT COUNT(*) c FROM mini_attempts"
    ).fetchone()["c"]

    category_rows = conn.execute("""
        SELECT q.category,
               COUNT(a.id) attempts,
               COALESCE(SUM(a.is_correct),0) correct
        FROM answers a
        JOIN questions q ON q.id=a.question_id
        GROUP BY q.category
        ORDER BY attempts DESC
    """).fetchall()

    conn.close()

    mini_accuracy = round(mini_correct / mini_total * 100, 1) if mini_total else 0

    body = r"""
<div class="card">
<h1>📊 Analytics</h1>
</div>

<div class="grid">
<div class="card"><div class="muted">Registered Users</div><div class="stat">{{ total_users }}</div></div>
<div class="card"><div class="muted">Completed Quizzes</div><div class="stat">{{ total_quizzes }}</div></div>
<div class="card"><div class="muted">Average Quiz Score</div><div class="stat">{{ "%.1f"|format(avg_score) }}%</div></div>
<div class="card"><div class="muted">Mini Accuracy</div><div class="stat">{{ mini_accuracy }}%</div></div>
</div>

<div class="card">
<h2>Category Performance</h2>
<table>
<tr><th>Category</th><th>Attempts</th><th>Correct</th><th>Accuracy</th></tr>
{% for r in category_rows %}
<tr>
<td>{{ r["category"] }}</td>
<td>{{ r["attempts"] }}</td>
<td>{{ r["correct"] }}</td>
<td>{{ "%.1f"|format(r["correct"]/r["attempts"]*100) }}%</td>
</tr>
{% endfor %}
</table>
</div>
"""
    return page("Admin Analytics", body, total_users=total_users,
                total_quizzes=total_quizzes, avg_score=avg_score,
                mini_accuracy=mini_accuracy, category_rows=category_rows)


# ============================================================
# HEALTH / API
# ============================================================

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "app": "Bio Warriors",
        "date": date.today().isoformat()
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    conn = get_db()
    rows = conn.execute("""
        SELECT name,user_id,points,streak,best_streak
        FROM users WHERE role='user' AND active=1
        ORDER BY points DESC LIMIT 20
    """).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("BIO WARRIORS IS STARTING")
    print("Admin login: admin / admin123")
    print("Open: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
