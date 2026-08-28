from flask import Flask, request, redirect, url_for, session, render_template_string, flash, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, date, timedelta
import random
import os
import psycopg2

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

# Reuse PostgreSQL connections instead of opening a new TCP connection
# for every small query/request. This is especially important on Render.
_DB_POOL = None

def get_db():
    """Return a PostgreSQL connection from a small thread-safe connection pool."""
    global _DB_POOL

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Create a Render PostgreSQL database "
            "and connect its Internal Database URL to this web service."
        )

    if _DB_POOL is None:
        from psycopg2.pool import ThreadedConnectionPool
        _DB_POOL = ThreadedConnectionPool(2, 20, dsn=database_url)

    return PostgresConnection(_DB_POOL)


class PostgresConnection:
    """Small compatibility wrapper around a pooled PostgreSQL connection."""
    def __init__(self, pool):
        from psycopg2.extras import RealDictCursor
        self._pool = pool
        self._conn = pool.getconn()
        self._cursor_factory = RealDictCursor
        self._closed = False

    def _sql(self, sql):
        # Existing app queries use SQLite's ? placeholders.
        return sql.replace("?", "%s")

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        cur.execute(self._sql(sql), params or ())
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        cur.executemany(self._sql(sql), seq_of_params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor(cursor_factory=self._cursor_factory)
        cur.execute(sql)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._closed:
            return
        try:
            # Roll back any unfinished transaction before reuse.
            self._conn.rollback()
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
        finally:
            self._closed = True
            return
        self._pool.putconn(self._conn)
        self._closed = True


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
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
        id SERIAL PRIMARY KEY,
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
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        quiz_date TEXT NOT NULL,
        question_ids TEXT NOT NULL,
        started_at TEXT NOT NULL,
        submitted_at TEXT,
        score INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        total INTEGER DEFAULT 0,
        points INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS answers (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES quiz_sessions(id) ON DELETE CASCADE,
        question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        selected_option TEXT,
        is_correct INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS mini_challenges (
        id SERIAL PRIMARY KEY,
        challenge_date TEXT NOT NULL,
        challenge_slot INTEGER NOT NULL DEFAULT 1,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        option_c TEXT NOT NULL,
        option_d TEXT NOT NULL,
        correct_option TEXT NOT NULL,
        explanation TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(challenge_date, challenge_slot)
    );

    CREATE TABLE IF NOT EXISTS mini_attempts (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        challenge_id INTEGER NOT NULL REFERENCES mini_challenges(id) ON DELETE CASCADE,
        selected_option TEXT,
        is_correct INTEGER NOT NULL DEFAULT 0,
        attempted_at TEXT NOT NULL,
        UNIQUE(user_id, challenge_id)
    );

    CREATE TABLE IF NOT EXISTS content (
        id SERIAL PRIMARY KEY,
        content_type TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    -- Indexes keep the 500+ question bank and growing history fast.
    CREATE INDEX IF NOT EXISTS idx_questions_active ON questions(active);
    CREATE INDEX IF NOT EXISTS idx_questions_category ON questions(category);
    CREATE INDEX IF NOT EXISTS idx_quiz_sessions_user_date ON quiz_sessions(user_id, quiz_date);
    CREATE INDEX IF NOT EXISTS idx_answers_session ON answers(session_id);
    CREATE INDEX IF NOT EXISTS idx_answers_question ON answers(question_id);
    CREATE INDEX IF NOT EXISTS idx_mini_attempts_user ON mini_attempts(user_id);
    CREATE INDEX IF NOT EXISTS idx_users_leaderboard ON users(role, active, points DESC, best_streak DESC);
    CREATE INDEX IF NOT EXISTS idx_content_active ON content(active, id DESC);
    """)

    # Create the default admin only when it does not already exist.
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

    qcount = conn.execute("SELECT COUNT(*) AS c FROM questions").fetchone()["c"]
    if qcount == 0:
        seed_questions(conn)
    ensure_question_bank(conn)

    mcount = conn.execute("SELECT COUNT(*) AS c FROM mini_challenges").fetchone()["c"]
    if mcount == 0:
        seed_mini_challenges(conn)
    ensure_mini_challenges(conn)

    ccount = conn.execute("SELECT COUNT(*) AS c FROM content").fetchone()["c"]
    if ccount == 0:
        seed_content(conn)

    conn.commit()
    conn.close()


def seed_questions(conn):
    """Seed 500 syllabus-aligned MCQs: 125 from each of the four subjects."""
    questions = [
        ('What is the primary hereditary material in most cellular organisms?', 'DNA', 'ATP', 'Lipids', 'Polysaccharides', 'A', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Genetic material?', 'ATP', 'Lipids', 'Polysaccharides', 'DNA', 'D', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Genetic material?', 'Lipids', 'Polysaccharides', 'DNA', 'ATP', 'C', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Genetic material should identify which statement as correct?', 'Polysaccharides', 'DNA', 'ATP', 'Lipids', 'B', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Genetic material?', 'DNA', 'ATP', 'Lipids', 'Polysaccharides', 'A', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What holds complementary DNA bases together between the two strands?', 'Disulfide bonds', 'Hydrogen bonds', 'Peptide bonds', 'Ester bonds', 'B', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA structure?', 'Hydrogen bonds', 'Peptide bonds', 'Ester bonds', 'Disulfide bonds', 'A', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA structure?', 'Peptide bonds', 'Ester bonds', 'Disulfide bonds', 'Hydrogen bonds', 'D', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA structure should identify which statement as correct?', 'Ester bonds', 'Disulfide bonds', 'Hydrogen bonds', 'Peptide bonds', 'C', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA structure?', 'Disulfide bonds', 'Hydrogen bonds', 'Peptide bonds', 'Ester bonds', 'B', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which base is characteristic of RNA instead of thymine?', 'Deoxyribose', 'Guanine only', 'Uracil', 'Thymine', 'C', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with RNA structure?', 'Guanine only', 'Uracil', 'Thymine', 'Deoxyribose', 'B', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of RNA structure?', 'Uracil', 'Thymine', 'Deoxyribose', 'Guanine only', 'A', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying RNA structure should identify which statement as correct?', 'Thymine', 'Deoxyribose', 'Guanine only', 'Uracil', 'D', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of RNA structure?', 'Deoxyribose', 'Guanine only', 'Uracil', 'Thymine', 'C', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Where is the main chromosome located in a typical prokaryotic cell?', 'Nucleolus', 'Golgi apparatus', 'Mitochondrial matrix', 'Nucleoid region', 'D', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Prokaryotic genome organization?', 'Golgi apparatus', 'Mitochondrial matrix', 'Nucleoid region', 'Nucleolus', 'C', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Prokaryotic genome organization?', 'Mitochondrial matrix', 'Nucleoid region', 'Nucleolus', 'Golgi apparatus', 'B', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Prokaryotic genome organization should identify which statement as correct?', 'Nucleoid region', 'Nucleolus', 'Golgi apparatus', 'Mitochondrial matrix', 'A', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Prokaryotic genome organization?', 'Nucleolus', 'Golgi apparatus', 'Mitochondrial matrix', 'Nucleoid region', 'D', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What proteins help package eukaryotic DNA into chromatin?', 'Histones', 'Actin only', 'Collagen', 'Insulin', 'A', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Eukaryotic DNA organization?', 'Actin only', 'Collagen', 'Insulin', 'Histones', 'D', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Eukaryotic DNA organization?', 'Collagen', 'Insulin', 'Histones', 'Actin only', 'C', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Eukaryotic DNA organization should identify which statement as correct?', 'Insulin', 'Histones', 'Actin only', 'Collagen', 'B', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Eukaryotic DNA organization?', 'Histones', 'Actin only', 'Collagen', 'Insulin', 'A', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What phenomenon did Griffith demonstrate?', 'RNA splicing', 'Bacterial transformation', 'DNA sequencing', 'Protein translation', 'B', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Griffith experiment?', 'Bacterial transformation', 'DNA sequencing', 'Protein translation', 'RNA splicing', 'A', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Griffith experiment?', 'DNA sequencing', 'Protein translation', 'RNA splicing', 'Bacterial transformation', 'D', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Griffith experiment should identify which statement as correct?', 'Protein translation', 'RNA splicing', 'Bacterial transformation', 'DNA sequencing', 'C', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Griffith experiment?', 'RNA splicing', 'Bacterial transformation', 'DNA sequencing', 'Protein translation', 'B', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What did Avery, MacLeod and McCarty identify as the transforming principle?', 'RNA', 'Lipid', 'DNA', 'Protein', 'C', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Avery MacLeod McCarty?', 'Lipid', 'DNA', 'Protein', 'RNA', 'B', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Avery MacLeod McCarty?', 'DNA', 'Protein', 'RNA', 'Lipid', 'A', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Avery MacLeod McCarty should identify which statement as correct?', 'Protein', 'RNA', 'Lipid', 'DNA', 'D', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Avery MacLeod McCarty?', 'RNA', 'Lipid', 'DNA', 'Protein', 'C', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What did the Hershey-Chase experiment support?', 'Proteins are always genetic material', 'Lipids replicate DNA', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'D', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Hershey Chase experiment?', 'Lipids replicate DNA', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'Proteins are always genetic material', 'C', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Hershey Chase experiment?', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'Proteins are always genetic material', 'Lipids replicate DNA', 'B', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Hershey Chase experiment should identify which statement as correct?', 'DNA is the genetic material of the phage', 'Proteins are always genetic material', 'Lipids replicate DNA', 'RNA is always double stranded', 'A', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Hershey Chase experiment?', 'Proteins are always genetic material', 'Lipids replicate DNA', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'D', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What does semiconservative replication mean?', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'Both strands are always parental', 'Only RNA is copied', 'A', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Semiconservative replication?', 'Both strands are newly synthesized', 'Both strands are always parental', 'Only RNA is copied', 'Each daughter DNA has one parental and one new strand', 'D', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Semiconservative replication?', 'Both strands are always parental', 'Only RNA is copied', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'C', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Semiconservative replication should identify which statement as correct?', 'Only RNA is copied', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'Both strands are always parental', 'B', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Semiconservative replication?', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'Both strands are always parental', 'Only RNA is copied', 'A', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a central function of DNA polymerase?', 'Lipid synthesis', 'DNA synthesis', 'Protein degradation', 'RNA splicing', 'B', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA polymerase?', 'DNA synthesis', 'Protein degradation', 'RNA splicing', 'Lipid synthesis', 'A', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA polymerase?', 'Protein degradation', 'RNA splicing', 'Lipid synthesis', 'DNA synthesis', 'D', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA polymerase should identify which statement as correct?', 'RNA splicing', 'Lipid synthesis', 'DNA synthesis', 'Protein degradation', 'C', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA polymerase?', 'Lipid synthesis', 'DNA synthesis', 'Protein degradation', 'RNA splicing', 'B', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is produced directly by transcription?', 'Lipids', 'Amino acids', 'RNA', 'DNA from protein', 'C', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Transcription?', 'Amino acids', 'RNA', 'DNA from protein', 'Lipids', 'B', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Transcription?', 'RNA', 'DNA from protein', 'Lipids', 'Amino acids', 'A', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Transcription should identify which statement as correct?', 'DNA from protein', 'Lipids', 'Amino acids', 'RNA', 'D', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Transcription?', 'Lipids', 'Amino acids', 'RNA', 'DNA from protein', 'C', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is the product of translation?', 'DNA', 'mRNA only', 'A phospholipid', 'A polypeptide', 'D', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Translation?', 'mRNA only', 'A phospholipid', 'A polypeptide', 'DNA', 'C', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Translation?', 'A phospholipid', 'A polypeptide', 'DNA', 'mRNA only', 'B', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Translation should identify which statement as correct?', 'A polypeptide', 'DNA', 'mRNA only', 'A phospholipid', 'A', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Translation?', 'DNA', 'mRNA only', 'A phospholipid', 'A polypeptide', 'D', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a codon?', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'A DNA enzyme', 'A lipid group', 'A', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Codon?', 'A protein domain', 'A DNA enzyme', 'A lipid group', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'D', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Codon?', 'A DNA enzyme', 'A lipid group', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'C', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Codon should identify which statement as correct?', 'A lipid group', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'A DNA enzyme', 'B', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Codon?', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'A DNA enzyme', 'A lipid group', 'A', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a spontaneous mutation?', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'A protein modification', 'B', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Spontaneous mutation?', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'A protein modification', 'A normal transcription event', 'A', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Spontaneous mutation?', 'A mutation caused only by radiation', 'A protein modification', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'D', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Spontaneous mutation should identify which statement as correct?', 'A protein modification', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'C', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Spontaneous mutation?', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'A protein modification', 'B', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What is the purpose of DNA repair mechanisms?', 'To synthesize lipids', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'C', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA repair?', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'To synthesize lipids', 'B', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA repair?', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'To synthesize lipids', 'To destroy all genes', 'A', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA repair should identify which statement as correct?', 'To translate mRNA', 'To synthesize lipids', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'D', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA repair?', 'To synthesize lipids', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'C', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is DNA methylation?', 'A change in amino-acid sequence', 'A type of protein translation', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'D', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with DNA methylation?', 'A type of protein translation', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'A change in amino-acid sequence', 'C', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of DNA methylation?', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'A change in amino-acid sequence', 'A type of protein translation', 'B', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying DNA methylation should identify which statement as correct?', 'An epigenetic chemical modification of DNA', 'A change in amino-acid sequence', 'A type of protein translation', 'A chromosome duplication only', 'A', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of DNA methylation?', 'A change in amino-acid sequence', 'A type of protein translation', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'D', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('How can histone modification affect gene expression?', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'By replacing mRNA with protein', 'By digesting chromosomes', 'A', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Histone modification?', 'By changing every DNA base', 'By replacing mRNA with protein', 'By digesting chromosomes', 'By altering chromatin properties and DNA accessibility', 'D', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Histone modification?', 'By replacing mRNA with protein', 'By digesting chromosomes', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'C', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Histone modification should identify which statement as correct?', 'By digesting chromosomes', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'By replacing mRNA with protein', 'B', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Histone modification?', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'By replacing mRNA with protein', 'By digesting chromosomes', 'A', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What is the lac operon associated with?', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'Protein folding only', 'B', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Lac operon?', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'Protein folding only', 'Lipid synthesis in humans', 'A', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Lac operon?', 'DNA replication in mitochondria', 'Protein folding only', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'D', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Lac operon should identify which statement as correct?', 'Protein folding only', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'C', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Lac operon?', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'Protein folding only', 'B', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What does the trp operon regulate?', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'C', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Trp operon?', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'Mitochondrial ATP synthesis', 'B', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Trp operon?', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'A', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Trp operon should identify which statement as correct?', 'Genes for lactose digestion in humans', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'D', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Trp operon?', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'C', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What is a key property of restriction enzymes?', 'They synthesize proteins', 'They translate mRNA', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'D', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Restriction enzymes?', 'They translate mRNA', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'They synthesize proteins', 'C', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Restriction enzymes?', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'They synthesize proteins', 'They translate mRNA', 'B', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Restriction enzymes should identify which statement as correct?', 'They recognize specific DNA sequences and cleave DNA', 'They synthesize proteins', 'They translate mRNA', 'They join amino acids', 'A', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Restriction enzymes?', 'They synthesize proteins', 'They translate mRNA', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'D', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Why are Type II restriction enzymes useful in cloning?', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'They synthesize RNA', 'They replicate plasmids by themselves', 'A', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Type II restriction enzymes?', 'They randomly destroy all DNA', 'They synthesize RNA', 'They replicate plasmids by themselves', 'They can cut DNA at predictable recognition sites', 'D', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Type II restriction enzymes?', 'They synthesize RNA', 'They replicate plasmids by themselves', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'C', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Type II restriction enzymes should identify which statement as correct?', 'They replicate plasmids by themselves', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'They synthesize RNA', 'B', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Type II restriction enzymes?', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'They synthesize RNA', 'They replicate plasmids by themselves', 'A', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What does DNA ligase do in cloning?', 'Unwinds proteins', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'B', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA ligase?', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'Unwinds proteins', 'A', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA ligase?', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'Unwinds proteins', 'Joins DNA fragments', 'D', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA ligase should identify which statement as correct?', 'Synthesizes RNA', 'Unwinds proteins', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'C', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA ligase?', 'Unwinds proteins', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'B', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a common role of a plasmid vector?', 'Separating DNA by size', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'C', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Plasmid vectors?', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'Separating DNA by size', 'B', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Plasmid vectors?', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'Separating DNA by size', 'Translating RNA', 'A', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Plasmid vectors should identify which statement as correct?', 'Digesting proteins', 'Separating DNA by size', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'D', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Plasmid vectors?', 'Separating DNA by size', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'C', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What directs Cas9 to its target sequence?', 'A ribosomal protein', 'A lipid', 'An antibody', 'A guide RNA', 'D', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with CRISPR-Cas9?', 'A lipid', 'An antibody', 'A guide RNA', 'A ribosomal protein', 'C', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of CRISPR-Cas9?', 'An antibody', 'A guide RNA', 'A ribosomal protein', 'A lipid', 'B', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying CRISPR-Cas9 should identify which statement as correct?', 'A guide RNA', 'A ribosomal protein', 'A lipid', 'An antibody', 'A', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of CRISPR-Cas9?', 'A ribosomal protein', 'A lipid', 'An antibody', 'A guide RNA', 'D', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is central to Sanger sequencing?', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'Protein antibodies', 'Lipid dyes', 'A', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Sanger sequencing?', 'Restriction enzymes only', 'Protein antibodies', 'Lipid dyes', 'Chain-terminating dideoxynucleotides', 'D', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Sanger sequencing?', 'Protein antibodies', 'Lipid dyes', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'C', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Sanger sequencing should identify which statement as correct?', 'Lipid dyes', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'Protein antibodies', 'B', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Sanger sequencing?', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'Protein antibodies', 'Lipid dyes', 'A', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is the primary catalytic role of an enzyme?', 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'B', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Enzymes?', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'Be permanently consumed in the reaction', 'A', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Enzymes?', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'D', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Enzymes should identify which statement as correct?', "Increase the reaction's ΔG", 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', 'C', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Enzymes?', 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'B', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('Which model describes a relatively rigid active site that complements the substrate?', 'Operon model', 'Endosymbiotic model', 'Lock-and-key model', 'Fluid mosaic model', 'C', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Lock-and-key model?', 'Endosymbiotic model', 'Lock-and-key model', 'Fluid mosaic model', 'Operon model', 'B', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Lock-and-key model?', 'Lock-and-key model', 'Fluid mosaic model', 'Operon model', 'Endosymbiotic model', 'A', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Lock-and-key model should identify which statement as correct?', 'Fluid mosaic model', 'Operon model', 'Endosymbiotic model', 'Lock-and-key model', 'D', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Lock-and-key model?', 'Operon model', 'Endosymbiotic model', 'Lock-and-key model', 'Fluid mosaic model', 'C', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('What happens in the induced-fit model when substrate binds?', 'The enzyme is degraded', 'The substrate becomes DNA', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'D', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Induced-fit model?', 'The substrate becomes DNA', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'The enzyme is degraded', 'C', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Induced-fit model?', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'The enzyme is degraded', 'The substrate becomes DNA', 'B', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Induced-fit model should identify which statement as correct?', 'The enzyme changes conformation to improve catalytic alignment', 'The enzyme is degraded', 'The substrate becomes DNA', 'The active site disappears', 'A', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Induced-fit model?', 'The enzyme is degraded', 'The substrate becomes DNA', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'D', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('What is a ribozyme?', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Ribozymes?', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A catalytically active RNA molecule', 'D', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Ribozymes?', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'C', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Ribozymes should identify which statement as correct?', 'A protein-only receptor', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'B', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Ribozymes?', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('What is the basis of EC enzyme classification?', 'Subcellular color', 'The type of reaction catalyzed', "The organism's habitat only", 'Protein length only', 'B', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with EC classification?', 'The type of reaction catalyzed', "The organism's habitat only", 'Protein length only', 'Subcellular color', 'A', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of EC classification?', "The organism's habitat only", 'Protein length only', 'Subcellular color', 'The type of reaction catalyzed', 'D', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying EC classification should identify which statement as correct?', 'Protein length only', 'Subcellular color', 'The type of reaction catalyzed', "The organism's habitat only", 'C', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of EC classification?', 'Subcellular color', 'The type of reaction catalyzed', "The organism's habitat only", 'Protein length only', 'B', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('What is a cofactor?', 'A ribosomal subunit', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'A substrate gene', 'C', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Cofactors?', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'A substrate gene', 'A ribosomal subunit', 'B', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Cofactors?', 'A non-protein component required by some enzymes', 'A substrate gene', 'A ribosomal subunit', 'A type of nucleic acid', 'A', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Cofactors should identify which statement as correct?', 'A substrate gene', 'A ribosomal subunit', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'D', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Cofactors?', 'A ribosomal subunit', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'A substrate gene', 'C', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which description best fits a coenzyme?', 'A protein that forms ribosomes', 'A membrane lipid', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'D', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Coenzymes?', 'A membrane lipid', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'A protein that forms ribosomes', 'C', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Coenzymes?', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'A protein that forms ribosomes', 'A membrane lipid', 'B', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Coenzymes should identify which statement as correct?', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'A protein that forms ribosomes', 'A membrane lipid', 'A DNA promoter', 'A', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Coenzymes?', 'A protein that forms ribosomes', 'A membrane lipid', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'D', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('What are isoenzymes?', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'A', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Isoenzymes?', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'Different enzyme forms catalyzing the same reaction', 'D', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Isoenzymes?', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'C', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Isoenzymes should identify which statement as correct?', 'Different substrates for one enzyme', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'B', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Isoenzymes?', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'A', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('Which inhibitor competes directly with substrate for the active site?', 'Coenzyme', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'Allosteric activator', 'B', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Competitive inhibition?', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'Allosteric activator', 'Coenzyme', 'A', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Competitive inhibition?', 'Uncompetitive inhibitor', 'Allosteric activator', 'Coenzyme', 'Competitive inhibitor', 'D', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Competitive inhibition should identify which statement as correct?', 'Allosteric activator', 'Coenzyme', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'C', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Competitive inhibition?', 'Coenzyme', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'Allosteric activator', 'B', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('In ideal pure noncompetitive inhibition, what is primarily reduced?', "The enzyme's amino-acid sequence", 'The reaction temperature', 'Vmax', 'Substrate concentration at every time', 'C', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Noncompetitive inhibition?', 'The reaction temperature', 'Vmax', 'Substrate concentration at every time', "The enzyme's amino-acid sequence", 'B', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Noncompetitive inhibition?', 'Vmax', 'Substrate concentration at every time', "The enzyme's amino-acid sequence", 'The reaction temperature', 'A', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Noncompetitive inhibition should identify which statement as correct?', 'Substrate concentration at every time', "The enzyme's amino-acid sequence", 'The reaction temperature', 'Vmax', 'D', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Noncompetitive inhibition?', "The enzyme's amino-acid sequence", 'The reaction temperature', 'Vmax', 'Substrate concentration at every time', 'C', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('What does the Michaelis-Menten equation relate?', 'DNA length to GC content', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'D', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Michaelis-Menten?', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'DNA length to GC content', 'C', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Michaelis-Menten?', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'DNA length to GC content', 'Protein mass to pH only', 'B', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Michaelis-Menten should identify which statement as correct?', 'Initial reaction velocity to substrate concentration', 'DNA length to GC content', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'A', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Michaelis-Menten?', 'DNA length to GC content', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'D', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('What does Km represent in the basic Michaelis-Menten model?', 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'The final product concentration', "The enzyme's molecular weight", 'A', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Km?', 'The maximum enzyme concentration', 'The final product concentration', "The enzyme's molecular weight", 'The substrate concentration at half Vmax', 'D', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Km?', 'The final product concentration', "The enzyme's molecular weight", 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'C', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Km should identify which statement as correct?', "The enzyme's molecular weight", 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'The final product concentration', 'B', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Km?', 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'The final product concentration', "The enzyme's molecular weight", 'A', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('When is Vmax approached?', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'When the enzyme is denatured', 'B', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Vmax?', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'When the enzyme is denatured', 'When pH is always 7', 'A', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Vmax?', 'When no substrate is present', 'When the enzyme is denatured', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'D', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Vmax should identify which statement as correct?', 'When the enzyme is denatured', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'C', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Vmax?', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'When the enzyme is denatured', 'B', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('Which axes define a Lineweaver-Burk plot?', 'pH versus temperature', 'DNA length versus GC%', '1/v versus 1/[S]', 'v versus [S]^2', 'C', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Lineweaver-Burk plot?', 'DNA length versus GC%', '1/v versus 1/[S]', 'v versus [S]^2', 'pH versus temperature', 'B', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Lineweaver-Burk plot?', '1/v versus 1/[S]', 'v versus [S]^2', 'pH versus temperature', 'DNA length versus GC%', 'A', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Lineweaver-Burk plot should identify which statement as correct?', 'v versus [S]^2', 'pH versus temperature', 'DNA length versus GC%', '1/v versus 1/[S]', 'D', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Lineweaver-Burk plot?', 'pH versus temperature', 'DNA length versus GC%', '1/v versus 1/[S]', 'v versus [S]^2', 'C', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('Why can enzyme activity fall at high temperature?', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'D', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Enzyme temperature?', 'ATP becomes DNA', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'Substrate concentration becomes infinite', 'C', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Enzyme temperature?', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'B', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Enzyme temperature should identify which statement as correct?', 'Protein structure can be disrupted, reducing catalytic activity', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'All enzymes become ribozymes', 'A', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Enzyme temperature?', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'D', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('Why does pH affect enzyme activity?', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'It always increases Vmax', 'It converts proteins into lipids', 'A', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Enzyme pH?', 'It changes the genetic code', 'It always increases Vmax', 'It converts proteins into lipids', 'It can alter ionization states, structure and catalytic residues', 'D', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Enzyme pH?', 'It always increases Vmax', 'It converts proteins into lipids', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'C', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Enzyme pH should identify which statement as correct?', 'It converts proteins into lipids', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'It always increases Vmax', 'B', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Enzyme pH?', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'It always increases Vmax', 'It converts proteins into lipids', 'A', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('Where does an allosteric regulator typically bind?', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'Only to the ribosome', 'B', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Allosteric regulation?', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'Only to the ribosome', 'Inside the substrate molecule', 'A', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Allosteric regulation?', 'Only to DNA bases', 'Only to the ribosome', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'D', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Allosteric regulation should identify which statement as correct?', 'Only to the ribosome', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'C', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Allosteric regulation?', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'Only to the ribosome', 'B', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('What is the usual target of feedback inhibition?', 'DNA replication only', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'C', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Feedback inhibition?', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'DNA replication only', 'B', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Feedback inhibition?', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'DNA replication only', 'Ribosomal RNA only', 'A', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Feedback inhibition should identify which statement as correct?', 'The cell membrane phospholipids only', 'DNA replication only', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'D', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Feedback inhibition?', 'DNA replication only', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'C', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('What is an advantage of enzyme immobilization?', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'D', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Immobilized enzymes?', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'The enzyme becomes DNA', 'C', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Immobilized enzymes?', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'B', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Immobilized enzymes should identify which statement as correct?', 'The enzyme can often be recovered and reused', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'A', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Immobilized enzymes?', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'D', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('What is the main carbon product of glycolysis from glucose?', 'Pyruvate', 'Urea', 'Cholesterol', 'Glycogen', 'A', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Glycolysis?', 'Urea', 'Cholesterol', 'Glycogen', 'Pyruvate', 'D', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Glycolysis?', 'Cholesterol', 'Glycogen', 'Pyruvate', 'Urea', 'C', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Glycolysis should identify which statement as correct?', 'Glycogen', 'Pyruvate', 'Urea', 'Cholesterol', 'B', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Glycolysis?', 'Pyruvate', 'Urea', 'Cholesterol', 'Glycogen', 'A', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('What enters the citric acid cycle as the key two-carbon acetyl unit?', 'Lactose', 'Acetyl-CoA', 'Glucose-6-phosphate', 'Urea', 'B', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Citric acid cycle?', 'Acetyl-CoA', 'Glucose-6-phosphate', 'Urea', 'Lactose', 'A', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Citric acid cycle?', 'Glucose-6-phosphate', 'Urea', 'Lactose', 'Acetyl-CoA', 'D', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Citric acid cycle should identify which statement as correct?', 'Urea', 'Lactose', 'Acetyl-CoA', 'Glucose-6-phosphate', 'C', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Citric acid cycle?', 'Lactose', 'Acetyl-CoA', 'Glucose-6-phosphate', 'Urea', 'B', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('Which reduced forms carry high-energy electrons?', 'ATP and ADP', 'DNA and RNA', 'NADH and FADH2', 'NAD+ and FAD', 'C', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with NAD and FAD?', 'DNA and RNA', 'NADH and FADH2', 'NAD+ and FAD', 'ATP and ADP', 'B', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of NAD and FAD?', 'NADH and FADH2', 'NAD+ and FAD', 'ATP and ADP', 'DNA and RNA', 'A', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying NAD and FAD should identify which statement as correct?', 'NAD+ and FAD', 'ATP and ADP', 'DNA and RNA', 'NADH and FADH2', 'D', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of NAD and FAD?', 'ATP and ADP', 'DNA and RNA', 'NADH and FADH2', 'NAD+ and FAD', 'C', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('What type of group is commonly carried by coenzyme A?', 'Phosphate groups only', 'DNA bases', 'Amino acids only', 'Acyl groups', 'D', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with CoA?', 'DNA bases', 'Amino acids only', 'Acyl groups', 'Phosphate groups only', 'C', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of CoA?', 'Amino acids only', 'Acyl groups', 'Phosphate groups only', 'DNA bases', 'B', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying CoA should identify which statement as correct?', 'Acyl groups', 'Phosphate groups only', 'DNA bases', 'Amino acids only', 'A', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of CoA?', 'Phosphate groups only', 'DNA bases', 'Amino acids only', 'Acyl groups', 'D', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('Which pair consists only of purines?', 'Adenine and guanine', 'Cytosine and thymine', 'Thymine and uracil', 'Cytosine and uracil', 'A', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Purine and pyrimidine metabolism?', 'Cytosine and thymine', 'Thymine and uracil', 'Cytosine and uracil', 'Adenine and guanine', 'D', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Purine and pyrimidine metabolism?', 'Thymine and uracil', 'Cytosine and uracil', 'Adenine and guanine', 'Cytosine and thymine', 'C', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Purine and pyrimidine metabolism should identify which statement as correct?', 'Cytosine and uracil', 'Adenine and guanine', 'Cytosine and thymine', 'Thymine and uracil', 'B', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Purine and pyrimidine metabolism?', 'Adenine and guanine', 'Cytosine and thymine', 'Thymine and uracil', 'Cytosine and uracil', 'A', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('What type of resource is BRENDA?', 'A DNA cloning vector', 'An enzyme information database', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'B', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with BRENDA?', 'An enzyme information database', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'A DNA cloning vector', 'A', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of BRENDA?', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'A DNA cloning vector', 'An enzyme information database', 'D', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying BRENDA should identify which statement as correct?', 'A protein electrophoresis gel', 'A DNA cloning vector', 'An enzyme information database', 'A nucleotide sequencing instrument', 'C', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of BRENDA?', 'A DNA cloning vector', 'An enzyme information database', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'B', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('What is AlphaFold primarily associated with?', 'Protein staining', 'PCR amplification', 'AI-based protein structure prediction', 'DNA restriction digestion', 'C', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with AlphaFold?', 'PCR amplification', 'AI-based protein structure prediction', 'DNA restriction digestion', 'Protein staining', 'B', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of AlphaFold?', 'AI-based protein structure prediction', 'DNA restriction digestion', 'Protein staining', 'PCR amplification', 'A', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('A student studying AlphaFold should identify which statement as correct?', 'DNA restriction digestion', 'Protein staining', 'PCR amplification', 'AI-based protein structure prediction', 'D', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of AlphaFold?', 'Protein staining', 'PCR amplification', 'AI-based protein structure prediction', 'DNA restriction digestion', 'C', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('How can AI support drug discovery?', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'D', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with AI-driven drug discovery?', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'By replacing all laboratory experiments automatically', 'C', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of AI-driven drug discovery?', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'B', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('A student studying AI-driven drug discovery should identify which statement as correct?', 'By learning patterns to prioritize targets or candidate molecules', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'A', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of AI-driven drug discovery?', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'D', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('What is a common goal of biological data mining?', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'A', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Biological data mining?', 'Destroying database records', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'Finding patterns and generating testable hypotheses', 'D', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Biological data mining?', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'C', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('A student studying Biological data mining should identify which statement as correct?', 'Measuring blood pressure directly', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'Changing amino-acid sequences', 'B', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Biological data mining?', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'A', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('What can a protein interaction network help reveal?', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'Only microscope magnification', 'B', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Protein interaction networks?', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'Only microscope magnification', 'Only protein molecular weight', 'A', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of Protein interaction networks?', 'Only DNA base composition', 'Only microscope magnification', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'D', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('A student studying Protein interaction networks should identify which statement as correct?', 'Only microscope magnification', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'C', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of Protein interaction networks?', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'Only microscope magnification', 'B', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('What is an SVM commonly used for in bioinformatics?', 'Protein digestion', 'Cell culture', 'Classification or prediction from labeled features', 'DNA extraction', 'C', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Support Vector Machines?', 'Cell culture', 'Classification or prediction from labeled features', 'DNA extraction', 'Protein digestion', 'B', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Support Vector Machines?', 'Classification or prediction from labeled features', 'DNA extraction', 'Protein digestion', 'Cell culture', 'A', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('A student studying Support Vector Machines should identify which statement as correct?', 'DNA extraction', 'Protein digestion', 'Cell culture', 'Classification or prediction from labeled features', 'D', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Support Vector Machines?', 'Protein digestion', 'Cell culture', 'Classification or prediction from labeled features', 'DNA extraction', 'C', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('What is a Random Forest?', 'A sequence alignment algorithm only', 'A protein purification method', 'A DNA repair pathway', 'An ensemble of decision trees', 'D', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Random Forests?', 'A protein purification method', 'A DNA repair pathway', 'An ensemble of decision trees', 'A sequence alignment algorithm only', 'C', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Random Forests?', 'A DNA repair pathway', 'An ensemble of decision trees', 'A sequence alignment algorithm only', 'A protein purification method', 'B', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('A student studying Random Forests should identify which statement as correct?', 'An ensemble of decision trees', 'A sequence alignment algorithm only', 'A protein purification method', 'A DNA repair pathway', 'A', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Random Forests?', 'A sequence alignment algorithm only', 'A protein purification method', 'A DNA repair pathway', 'An ensemble of decision trees', 'D', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('What is the purpose of k-means clustering?', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'A', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with K-means clustering?', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'Grouping similar observations into clusters', 'D', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of K-means clustering?', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'C', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('A student studying K-means clustering should identify which statement as correct?', 'Annotating ribosomes experimentally', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'B', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of K-means clustering?', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'A', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('What is BLAST mainly used for?', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'Measuring enzyme pH', 'B', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with BLAST?', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'Measuring enzyme pH', 'Separating proteins by charge', 'A', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of BLAST?', 'Predicting blood pressure', 'Measuring enzyme pH', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'D', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('A student studying BLAST should identify which statement as correct?', 'Measuring enzyme pH', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'C', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of BLAST?', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'Measuring enzyme pH', 'B', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('What does ProtTrans provide?', 'A microscope image archive', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'C', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with ProtTrans?', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'A microscope image archive', 'B', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of ProtTrans?', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'A microscope image archive', 'A lipid metabolism pathway', 'A', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('A student studying ProtTrans should identify which statement as correct?', 'A restriction enzyme catalogue only', 'A microscope image archive', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'D', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of ProtTrans?', 'A microscope image archive', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'C', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('What distinguishes DeepBLAST from traditional BLAST-style searching?', 'It uses only wet-lab PCR', 'It is a protein gel', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'D', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with DeepBLAST?', 'It is a protein gel', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'It uses only wet-lab PCR', 'C', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of DeepBLAST?', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'It uses only wet-lab PCR', 'It is a protein gel', 'B', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('A student studying DeepBLAST should identify which statement as correct?', 'It uses deep learning to model protein sequence relationships', 'It uses only wet-lab PCR', 'It is a protein gel', 'It is a genome sequencer', 'A', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of DeepBLAST?', 'It uses only wet-lab PCR', 'It is a protein gel', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'D', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('What type of sequences are primarily stored in GenBank?', 'Nucleotide sequences', 'Only protein structures', 'Only microarray images', 'Only enzyme kinetic curves', 'A', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with GenBank?', 'Only protein structures', 'Only microarray images', 'Only enzyme kinetic curves', 'Nucleotide sequences', 'D', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of GenBank?', 'Only microarray images', 'Only enzyme kinetic curves', 'Nucleotide sequences', 'Only protein structures', 'C', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('A student studying GenBank should identify which statement as correct?', 'Only enzyme kinetic curves', 'Nucleotide sequences', 'Only protein structures', 'Only microarray images', 'B', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of GenBank?', 'Nucleotide sequences', 'Only protein structures', 'Only microarray images', 'Only enzyme kinetic curves', 'A', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('EMBL is primarily associated with which kind of data?', 'Mass spectra only', 'Nucleotide sequence data', 'Protein gel images', 'Clinical prescriptions', 'B', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with EMBL database?', 'Nucleotide sequence data', 'Protein gel images', 'Clinical prescriptions', 'Mass spectra only', 'A', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of EMBL database?', 'Protein gel images', 'Clinical prescriptions', 'Mass spectra only', 'Nucleotide sequence data', 'D', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('A student studying EMBL database should identify which statement as correct?', 'Clinical prescriptions', 'Mass spectra only', 'Nucleotide sequence data', 'Protein gel images', 'C', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of EMBL database?', 'Mass spectra only', 'Nucleotide sequence data', 'Protein gel images', 'Clinical prescriptions', 'B', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('What does DDBJ mainly store?', 'Enzyme reaction temperatures only', 'Microscope videos only', 'Nucleotide sequence records', 'Protein crystal images only', 'C', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DDBJ?', 'Microscope videos only', 'Nucleotide sequence records', 'Protein crystal images only', 'Enzyme reaction temperatures only', 'B', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of DDBJ?', 'Nucleotide sequence records', 'Protein crystal images only', 'Enzyme reaction temperatures only', 'Microscope videos only', 'A', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('A student studying DDBJ should identify which statement as correct?', 'Protein crystal images only', 'Enzyme reaction temperatures only', 'Microscope videos only', 'Nucleotide sequence records', 'D', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of DDBJ?', 'Enzyme reaction temperatures only', 'Microscope videos only', 'Nucleotide sequence records', 'Protein crystal images only', 'C', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('What is a key feature of Swiss-Prot?', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'D', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Swiss-Prot?', 'It is a clustering algorithm', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'It stores only raw DNA reads', 'C', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of Swiss-Prot?', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'B', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('A student studying Swiss-Prot should identify which statement as correct?', 'Manual curation and reviewed protein records', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'It is a sequencing machine', 'A', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of Swiss-Prot?', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'D', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('How does TrEMBL differ from Swiss-Prot?', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'A', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with TrEMBL?', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'TrEMBL contains computationally annotated records awaiting manual review', 'D', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of TrEMBL?', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'C', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('A student studying TrEMBL should identify which statement as correct?', 'They are both sequencing instruments', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'B', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of TrEMBL?', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'A', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('How are derived databases generally created?', 'By deleting annotations', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'By replacing all primary records', 'B', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Derived databases?', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'By replacing all primary records', 'By deleting annotations', 'A', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of Derived databases?', 'By sequencing samples without computers', 'By replacing all primary records', 'By deleting annotations', 'By processing and organizing data from primary databases', 'D', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('A student studying Derived databases should identify which statement as correct?', 'By replacing all primary records', 'By deleting annotations', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'C', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of Derived databases?', 'By deleting annotations', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'By replacing all primary records', 'B', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('What does PROSITE help identify?', 'Only lipid droplets', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'C', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with PROSITE?', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'Only lipid droplets', 'B', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of PROSITE?', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'Only lipid droplets', 'Only cell organelles', 'A', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('A student studying PROSITE should identify which statement as correct?', 'Only DNA sequencing errors', 'Only lipid droplets', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'D', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of PROSITE?', 'Only lipid droplets', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'C', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('What is a major focus of Pfam?', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'Protein families and conserved domains', 'D', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Pfam?', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'Protein families and conserved domains', 'Genome sequencing hardware', 'C', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Pfam?', 'DNA cloning vectors', 'Protein families and conserved domains', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'B', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('A student studying Pfam should identify which statement as correct?', 'Protein families and conserved domains', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'A', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Pfam?', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'Protein families and conserved domains', 'D', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('Which organization provides GenBank and BLAST resources?', 'NCBI', 'PDB only', 'WHO only', 'EMBL-EBI only', 'A', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with NCBI?', 'PDB only', 'WHO only', 'EMBL-EBI only', 'NCBI', 'D', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of NCBI?', 'WHO only', 'EMBL-EBI only', 'NCBI', 'PDB only', 'C', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('A student studying NCBI should identify which statement as correct?', 'EMBL-EBI only', 'NCBI', 'PDB only', 'WHO only', 'B', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of NCBI?', 'NCBI', 'PDB only', 'WHO only', 'EMBL-EBI only', 'A', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('What is sequence retrieval?', 'Measuring pH', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'Separating proteins by size', 'B', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Sequence retrieval?', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'Separating proteins by size', 'Measuring pH', 'A', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Sequence retrieval?', 'Digesting DNA with enzymes', 'Separating proteins by size', 'Measuring pH', 'Searching a database and obtaining sequence records', 'D', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('A student studying Sequence retrieval should identify which statement as correct?', 'Separating proteins by size', 'Measuring pH', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'C', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Sequence retrieval?', 'Measuring pH', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'Separating proteins by size', 'B', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('Which feature identifies a standard FASTA record?', 'A gel image', 'A binary executable', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'C', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with FASTA format?', 'A binary executable', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'A gel image', 'B', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of FASTA format?', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'A gel image', 'A binary executable', 'A', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('A student studying FASTA format should identify which statement as correct?', 'A four-column spreadsheet only', 'A gel image', 'A binary executable', 'A header line beginning with > followed by the sequence', 'D', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of FASTA format?', 'A gel image', 'A binary executable', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'C', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('What is KEGG especially useful for?', 'Protein staining', 'DNA extraction', 'Microscope calibration', 'Pathway and systems-level biological information', 'D', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with KEGG?', 'DNA extraction', 'Microscope calibration', 'Pathway and systems-level biological information', 'Protein staining', 'C', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of KEGG?', 'Microscope calibration', 'Pathway and systems-level biological information', 'Protein staining', 'DNA extraction', 'B', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('A student studying KEGG should identify which statement as correct?', 'Pathway and systems-level biological information', 'Protein staining', 'DNA extraction', 'Microscope calibration', 'A', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of KEGG?', 'Protein staining', 'DNA extraction', 'Microscope calibration', 'Pathway and systems-level biological information', 'D', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('What does the CATH acronym represent?', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'A', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with CATH?', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'Class, Architecture, Topology, Homologous superfamily', 'D', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of CATH?', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'C', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('A student studying CATH should identify which statement as correct?', 'Chromosome, ATP, Transfer, Histone', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'B', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of CATH?', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'A', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('What is SCOP used for?', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'Measuring enzyme Km', 'B', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with SCOP?', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'Measuring enzyme Km', 'Storing clinical images only', 'A', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of SCOP?', 'Running PCR', 'Measuring enzyme Km', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'D', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('A student studying SCOP should identify which statement as correct?', 'Measuring enzyme Km', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'C', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of SCOP?', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'Measuring enzyme Km', 'B', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('What is the PDB primarily used for?', 'Running machine-learning models', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'C', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with PDB?', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'Running machine-learning models', 'B', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of PDB?', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'Running machine-learning models', 'Storing patient passwords', 'A', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('A student studying PDB should identify which statement as correct?', 'Storing only DNA primers', 'Running machine-learning models', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'D', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of PDB?', 'Running machine-learning models', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'C', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('What is the proteome?', 'The complete DNA sequence only', 'All cellular lipids only', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'D', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Proteome?', 'All cellular lipids only', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'The complete DNA sequence only', 'C', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Proteome?', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'The complete DNA sequence only', 'All cellular lipids only', 'B', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('A student studying Proteome should identify which statement as correct?', 'The complete set of proteins expressed under a defined condition', 'The complete DNA sequence only', 'All cellular lipids only', 'All metabolites only', 'A', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Proteome?', 'The complete DNA sequence only', 'All cellular lipids only', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'D', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('What defines primary protein structure?', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'The DNA promoter', 'The lipid bilayer', 'A', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Primary protein structure?', 'The arrangement of multiple subunits only', 'The DNA promoter', 'The lipid bilayer', 'The linear amino-acid sequence', 'D', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Primary protein structure?', 'The DNA promoter', 'The lipid bilayer', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'C', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('A student studying Primary protein structure should identify which statement as correct?', 'The lipid bilayer', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'The DNA promoter', 'B', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Primary protein structure?', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'The DNA promoter', 'The lipid bilayer', 'A', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('Which are major protein secondary structures?', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'Micelles and liposomes', 'B', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Secondary structure?', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'Micelles and liposomes', 'Chromosomes and centromeres', 'A', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Secondary structure?', 'DNA double helices and plasmids', 'Micelles and liposomes', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'D', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('A student studying Secondary structure should identify which statement as correct?', 'Micelles and liposomes', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'C', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Secondary structure?', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'Micelles and liposomes', 'B', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('What does tertiary structure describe?', 'Only interactions between chromosomes', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'C', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Tertiary structure?', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'Only interactions between chromosomes', 'B', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Tertiary structure?', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'Only interactions between chromosomes', 'Only RNA splicing', 'A', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('A student studying Tertiary structure should identify which statement as correct?', 'Only the amino-acid sequence', 'Only interactions between chromosomes', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'D', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Tertiary structure?', 'Only interactions between chromosomes', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'C', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('What does quaternary structure involve?', 'Only peptide-bond formation', 'Only DNA methylation', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'D', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Quaternary structure?', 'Only DNA methylation', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'Only peptide-bond formation', 'C', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Quaternary structure?', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'Only peptide-bond formation', 'Only DNA methylation', 'B', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('A student studying Quaternary structure should identify which statement as correct?', 'Association of multiple polypeptide subunits', 'Only peptide-bond formation', 'Only DNA methylation', 'Only membrane transport', 'A', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Quaternary structure?', 'Only peptide-bond formation', 'Only DNA methylation', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'D', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('When do post-translational modifications occur?', 'After translation of the polypeptide', 'Before DNA replication only', 'Only before transcription', 'Only during nucleotide synthesis', 'A', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Post-translational modification?', 'Before DNA replication only', 'Only before transcription', 'Only during nucleotide synthesis', 'After translation of the polypeptide', 'D', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Post-translational modification?', 'Only before transcription', 'Only during nucleotide synthesis', 'After translation of the polypeptide', 'Before DNA replication only', 'C', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('A student studying Post-translational modification should identify which statement as correct?', 'Only during nucleotide synthesis', 'After translation of the polypeptide', 'Before DNA replication only', 'Only before transcription', 'B', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Post-translational modification?', 'After translation of the polypeptide', 'Before DNA replication only', 'Only before transcription', 'Only during nucleotide synthesis', 'A', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('What is a systems-biology perspective?', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'Studying only microscopy optics', 'B', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Systems biology?', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'Studying only microscopy optics', 'Studying only DNA extraction', 'A', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Systems biology?', 'Studying one amino acid only', 'Studying only microscopy optics', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'D', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('A student studying Systems biology should identify which statement as correct?', 'Studying only microscopy optics', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'C', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Systems biology?', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'Studying only microscopy optics', 'B', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('Why integrate proteomics with genomics?', 'To sequence only lipids', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'C', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Proteomics-genomics integration?', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'To sequence only lipids', 'B', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Proteomics-genomics integration?', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'To sequence only lipids', 'To replace all databases', 'A', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('A student studying Proteomics-genomics integration should identify which statement as correct?', 'To eliminate protein measurements', 'To sequence only lipids', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'D', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Proteomics-genomics integration?', 'To sequence only lipids', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'C', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('What are the two dimensions in 2-DE?', 'DNA length followed by GC content', 'pH followed by temperature', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'D', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Two-dimensional gel electrophoresis?', 'pH followed by temperature', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'DNA length followed by GC content', 'C', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Two-dimensional gel electrophoresis?', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'DNA length followed by GC content', 'pH followed by temperature', 'B', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('A student studying Two-dimensional gel electrophoresis should identify which statement as correct?', 'Isoelectric point followed by molecular mass', 'DNA length followed by GC content', 'pH followed by temperature', 'RNA length followed by charge only', 'A', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Two-dimensional gel electrophoresis?', 'DNA length followed by GC content', 'pH followed by temperature', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'D', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('What property is used in isoelectric focusing?', 'Isoelectric point', 'DNA sequence', 'Protein gene count', 'Cell diameter', 'A', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Isoelectric focusing?', 'DNA sequence', 'Protein gene count', 'Cell diameter', 'Isoelectric point', 'D', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Isoelectric focusing?', 'Protein gene count', 'Cell diameter', 'Isoelectric point', 'DNA sequence', 'C', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('A student studying Isoelectric focusing should identify which statement as correct?', 'Cell diameter', 'Isoelectric point', 'DNA sequence', 'Protein gene count', 'B', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Isoelectric focusing?', 'Isoelectric point', 'DNA sequence', 'Protein gene count', 'Cell diameter', 'A', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('What is the major basis of SDS-PAGE separation?', 'Protein fluorescence only', 'Molecular mass', 'Isoelectric point only', 'DNA sequence', 'B', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with SDS-PAGE?', 'Molecular mass', 'Isoelectric point only', 'DNA sequence', 'Protein fluorescence only', 'A', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of SDS-PAGE?', 'Isoelectric point only', 'DNA sequence', 'Protein fluorescence only', 'Molecular mass', 'D', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('A student studying SDS-PAGE should identify which statement as correct?', 'DNA sequence', 'Protein fluorescence only', 'Molecular mass', 'Isoelectric point only', 'C', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of SDS-PAGE?', 'Protein fluorescence only', 'Molecular mass', 'Isoelectric point only', 'DNA sequence', 'B', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('Why is protein solubilization important in 2-DE?', 'To synthesize lipids', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'C', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Protein sample solubilization?', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'To synthesize lipids', 'B', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Protein sample solubilization?', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'To synthesize lipids', 'To sequence chromosomes', 'A', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('A student studying Protein sample solubilization should identify which statement as correct?', 'To replicate DNA', 'To synthesize lipids', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'D', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Protein sample solubilization?', 'To synthesize lipids', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'C', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('What can a reducing agent do during protein sample preparation?', 'Add DNA bases', 'Digest RNA into nucleotides', 'Create peptide bonds', 'Break disulfide bonds', 'D', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Reduction in sample preparation?', 'Digest RNA into nucleotides', 'Create peptide bonds', 'Break disulfide bonds', 'Add DNA bases', 'C', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Reduction in sample preparation?', 'Create peptide bonds', 'Break disulfide bonds', 'Add DNA bases', 'Digest RNA into nucleotides', 'B', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('A student studying Reduction in sample preparation should identify which statement as correct?', 'Break disulfide bonds', 'Add DNA bases', 'Digest RNA into nucleotides', 'Create peptide bonds', 'A', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Reduction in sample preparation?', 'Add DNA bases', 'Digest RNA into nucleotides', 'Create peptide bonds', 'Break disulfide bonds', 'D', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('Which factor improves reproducibility of 2-DE?', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'Avoiding normalization', 'A', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with 2-DE reproducibility?', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'Avoiding normalization', 'Consistent experimental and image-analysis conditions', 'D', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of 2-DE reproducibility?', 'Using different sample amounts randomly', 'Avoiding normalization', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'C', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('A student studying 2-DE reproducibility should identify which statement as correct?', 'Avoiding normalization', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'B', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of 2-DE reproducibility?', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'Avoiding normalization', 'A', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('What is a key feature of shotgun proteomics?', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'Protein analysis without peptides', 'B', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Shotgun proteomics?', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'Protein analysis without peptides', 'Only microscopy', 'A', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Shotgun proteomics?', 'Separation only by DNA length', 'Protein analysis without peptides', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'D', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('A student studying Shotgun proteomics should identify which statement as correct?', 'Protein analysis without peptides', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'C', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Shotgun proteomics?', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'Protein analysis without peptides', 'B', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('How can MS identify a protein?', 'By observing chromosome shape', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'C', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Mass spectrometry protein identification?', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'By observing chromosome shape', 'B', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Mass spectrometry protein identification?', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'By observing chromosome shape', 'By staining RNA', 'A', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('A student studying Mass spectrometry protein identification should identify which statement as correct?', 'By measuring only cell size', 'By observing chromosome shape', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'D', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Mass spectrometry protein identification?', 'By observing chromosome shape', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'C', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('What is de novo peptide sequencing?', 'Copying DNA by PCR', 'Separating proteins by pI only', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'D', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with De novo sequencing?', 'Separating proteins by pI only', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'Copying DNA by PCR', 'C', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of De novo sequencing?', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'Copying DNA by PCR', 'Separating proteins by pI only', 'B', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('A student studying De novo sequencing should identify which statement as correct?', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'Copying DNA by PCR', 'Separating proteins by pI only', 'Predicting cell division', 'A', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of De novo sequencing?', 'Copying DNA by PCR', 'Separating proteins by pI only', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'D', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('What does tandem MS add to mass spectrometry?', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'DNA replication', 'RNA splicing', 'A', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Tandem mass spectrometry?', 'A second gel electrophoresis dimension', 'DNA replication', 'RNA splicing', 'Fragmentation that provides sequence-informative product ions', 'D', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Tandem mass spectrometry?', 'DNA replication', 'RNA splicing', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'C', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('A student studying Tandem mass spectrometry should identify which statement as correct?', 'RNA splicing', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'DNA replication', 'B', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Tandem mass spectrometry?', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'DNA replication', 'RNA splicing', 'A', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('What is a microarray designed to do?', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'B', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Microarray technology?', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'Culture bacteria automatically', 'A', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Microarray technology?', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'D', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('A student studying Microarray technology should identify which statement as correct?', 'Purify one enzyme', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'C', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Microarray technology?', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'B', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('Which is important in microarray experiment design?', 'Ignoring controls', 'Using only one sample for every study', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'C', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Microarray experiment design?', 'Using only one sample for every study', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'Ignoring controls', 'B', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Microarray experiment design?', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'Ignoring controls', 'Using only one sample for every study', 'A', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('A student studying Microarray experiment design should identify which statement as correct?', 'Changing probe sequences during the experiment', 'Ignoring controls', 'Using only one sample for every study', 'Controls, replicates and normalization', 'D', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Microarray experiment design?', 'Ignoring controls', 'Using only one sample for every study', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'C', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('How can NGS complement proteomics?', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'D', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Next-generation sequencing and proteomics?', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'By directly measuring enzyme Km', 'C', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Next-generation sequencing and proteomics?', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'B', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('A student studying Next-generation sequencing and proteomics should identify which statement as correct?', 'By providing genomic or transcript information for protein interpretation', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'A', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Next-generation sequencing and proteomics?', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'D', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('How can proteomics support drug development?', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'By measuring only DNA length', 'By producing antibiotics directly', 'A', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Proteomics in drug development?', 'By replacing all clinical trials', 'By measuring only DNA length', 'By producing antibiotics directly', 'By identifying biomarkers and potential therapeutic targets', 'D', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Proteomics in drug development?', 'By measuring only DNA length', 'By producing antibiotics directly', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'C', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('A student studying Proteomics in drug development should identify which statement as correct?', 'By producing antibiotics directly', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'By measuring only DNA length', 'B', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Proteomics in drug development?', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'By measuring only DNA length', 'By producing antibiotics directly', 'A', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('What is a use of phage display in proteomics-related applications?', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'Sequencing whole genomes only', 'B', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Phage antibodies?', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'Sequencing whole genomes only', 'Measuring lipid oxidation', 'A', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Phage antibodies?', 'Separating proteins by size', 'Sequencing whole genomes only', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'D', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('A student studying Phage antibodies should identify which statement as correct?', 'Sequencing whole genomes only', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'C', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Phage antibodies?', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'Sequencing whole genomes only', 'B', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('What is a valid AI application in proteomics?', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'C', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with AI in proteomics?', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'Changing amino-acid chemistry', 'B', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of AI in proteomics?', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'A', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('A student studying AI in proteomics should identify which statement as correct?', 'Replacing every protein with a neural network', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'D', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of AI in proteomics?', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'C', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('What can plant proteomics investigate?', 'Only human chromosomes', 'Only bacterial plasmids', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'D', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Plant proteomics?', 'Only bacterial plasmids', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'Only human chromosomes', 'C', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Plant proteomics?', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'Only human chromosomes', 'Only bacterial plasmids', 'B', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('A student studying Plant proteomics should identify which statement as correct?', 'Protein changes associated with plant development or stress', 'Only human chromosomes', 'Only bacterial plasmids', 'Only animal antibodies', 'A', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Plant proteomics?', 'Only human chromosomes', 'Only bacterial plasmids', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'D', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
    ]
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO questions
        (question,option_a,option_b,option_c,option_d,correct_option,
         explanation,category,difficulty,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, [q + (now,) for q in questions])


def ensure_question_bank(conn):
    """Add any missing built-in syllabus questions without overwriting admin content."""
    questions = [
        ('What is the primary hereditary material in most cellular organisms?', 'DNA', 'ATP', 'Lipids', 'Polysaccharides', 'A', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Genetic material?', 'ATP', 'Lipids', 'Polysaccharides', 'DNA', 'D', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Genetic material?', 'Lipids', 'Polysaccharides', 'DNA', 'ATP', 'C', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Genetic material should identify which statement as correct?', 'Polysaccharides', 'DNA', 'ATP', 'Lipids', 'B', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Genetic material?', 'DNA', 'ATP', 'Lipids', 'Polysaccharides', 'A', 'DNA is the hereditary material in most cellular organisms and stores genetic information.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What holds complementary DNA bases together between the two strands?', 'Disulfide bonds', 'Hydrogen bonds', 'Peptide bonds', 'Ester bonds', 'B', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA structure?', 'Hydrogen bonds', 'Peptide bonds', 'Ester bonds', 'Disulfide bonds', 'A', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA structure?', 'Peptide bonds', 'Ester bonds', 'Disulfide bonds', 'Hydrogen bonds', 'D', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA structure should identify which statement as correct?', 'Ester bonds', 'Disulfide bonds', 'Hydrogen bonds', 'Peptide bonds', 'C', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA structure?', 'Disulfide bonds', 'Hydrogen bonds', 'Peptide bonds', 'Ester bonds', 'B', 'DNA is a double-stranded polymer in which complementary strands are held together by hydrogen bonds between bases.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which base is characteristic of RNA instead of thymine?', 'Deoxyribose', 'Guanine only', 'Uracil', 'Thymine', 'C', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with RNA structure?', 'Guanine only', 'Uracil', 'Thymine', 'Deoxyribose', 'B', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of RNA structure?', 'Uracil', 'Thymine', 'Deoxyribose', 'Guanine only', 'A', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying RNA structure should identify which statement as correct?', 'Thymine', 'Deoxyribose', 'Guanine only', 'Uracil', 'D', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of RNA structure?', 'Deoxyribose', 'Guanine only', 'Uracil', 'Thymine', 'C', 'RNA generally contains ribose sugar and uracil instead of thymine, and many RNAs are single stranded.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Where is the main chromosome located in a typical prokaryotic cell?', 'Nucleolus', 'Golgi apparatus', 'Mitochondrial matrix', 'Nucleoid region', 'D', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Prokaryotic genome organization?', 'Golgi apparatus', 'Mitochondrial matrix', 'Nucleoid region', 'Nucleolus', 'C', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Prokaryotic genome organization?', 'Mitochondrial matrix', 'Nucleoid region', 'Nucleolus', 'Golgi apparatus', 'B', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Prokaryotic genome organization should identify which statement as correct?', 'Nucleoid region', 'Nucleolus', 'Golgi apparatus', 'Mitochondrial matrix', 'A', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Prokaryotic genome organization?', 'Nucleolus', 'Golgi apparatus', 'Mitochondrial matrix', 'Nucleoid region', 'D', 'Prokaryotic chromosomes are generally located in a nucleoid region rather than a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What proteins help package eukaryotic DNA into chromatin?', 'Histones', 'Actin only', 'Collagen', 'Insulin', 'A', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Eukaryotic DNA organization?', 'Actin only', 'Collagen', 'Insulin', 'Histones', 'D', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Eukaryotic DNA organization?', 'Collagen', 'Insulin', 'Histones', 'Actin only', 'C', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Eukaryotic DNA organization should identify which statement as correct?', 'Insulin', 'Histones', 'Actin only', 'Collagen', 'B', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Eukaryotic DNA organization?', 'Histones', 'Actin only', 'Collagen', 'Insulin', 'A', 'Eukaryotic DNA is packaged with histone proteins into chromatin within a membrane-bound nucleus.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What phenomenon did Griffith demonstrate?', 'RNA splicing', 'Bacterial transformation', 'DNA sequencing', 'Protein translation', 'B', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Griffith experiment?', 'Bacterial transformation', 'DNA sequencing', 'Protein translation', 'RNA splicing', 'A', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Griffith experiment?', 'DNA sequencing', 'Protein translation', 'RNA splicing', 'Bacterial transformation', 'D', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Griffith experiment should identify which statement as correct?', 'Protein translation', 'RNA splicing', 'Bacterial transformation', 'DNA sequencing', 'C', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Griffith experiment?', 'RNA splicing', 'Bacterial transformation', 'DNA sequencing', 'Protein translation', 'B', "Griffith's transformation experiment showed that a heritable factor from virulent bacteria could transform nonvirulent bacteria.", 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What did Avery, MacLeod and McCarty identify as the transforming principle?', 'RNA', 'Lipid', 'DNA', 'Protein', 'C', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Avery MacLeod McCarty?', 'Lipid', 'DNA', 'Protein', 'RNA', 'B', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Avery MacLeod McCarty?', 'DNA', 'Protein', 'RNA', 'Lipid', 'A', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Avery MacLeod McCarty should identify which statement as correct?', 'Protein', 'RNA', 'Lipid', 'DNA', 'D', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Avery MacLeod McCarty?', 'RNA', 'Lipid', 'DNA', 'Protein', 'C', 'Avery, MacLeod and McCarty provided evidence that DNA was the transforming principle in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What did the Hershey-Chase experiment support?', 'Proteins are always genetic material', 'Lipids replicate DNA', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'D', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Hershey Chase experiment?', 'Lipids replicate DNA', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'Proteins are always genetic material', 'C', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Hershey Chase experiment?', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'Proteins are always genetic material', 'Lipids replicate DNA', 'B', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Hershey Chase experiment should identify which statement as correct?', 'DNA is the genetic material of the phage', 'Proteins are always genetic material', 'Lipids replicate DNA', 'RNA is always double stranded', 'A', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Hershey Chase experiment?', 'Proteins are always genetic material', 'Lipids replicate DNA', 'RNA is always double stranded', 'DNA is the genetic material of the phage', 'D', 'The Hershey-Chase experiment used bacteriophages to provide evidence that DNA, not protein, enters bacteria during infection and carries genetic information.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What does semiconservative replication mean?', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'Both strands are always parental', 'Only RNA is copied', 'A', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Semiconservative replication?', 'Both strands are newly synthesized', 'Both strands are always parental', 'Only RNA is copied', 'Each daughter DNA has one parental and one new strand', 'D', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Semiconservative replication?', 'Both strands are always parental', 'Only RNA is copied', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'C', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Semiconservative replication should identify which statement as correct?', 'Only RNA is copied', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'Both strands are always parental', 'B', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Semiconservative replication?', 'Each daughter DNA has one parental and one new strand', 'Both strands are newly synthesized', 'Both strands are always parental', 'Only RNA is copied', 'A', 'Semiconservative DNA replication produces daughter DNA molecules, each containing one parental strand and one newly synthesized strand.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a central function of DNA polymerase?', 'Lipid synthesis', 'DNA synthesis', 'Protein degradation', 'RNA splicing', 'B', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA polymerase?', 'DNA synthesis', 'Protein degradation', 'RNA splicing', 'Lipid synthesis', 'A', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA polymerase?', 'Protein degradation', 'RNA splicing', 'Lipid synthesis', 'DNA synthesis', 'D', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA polymerase should identify which statement as correct?', 'RNA splicing', 'Lipid synthesis', 'DNA synthesis', 'Protein degradation', 'C', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA polymerase?', 'Lipid synthesis', 'DNA synthesis', 'Protein degradation', 'RNA splicing', 'B', 'DNA polymerases synthesize DNA by adding nucleotides to a growing strand using a template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is produced directly by transcription?', 'Lipids', 'Amino acids', 'RNA', 'DNA from protein', 'C', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Transcription?', 'Amino acids', 'RNA', 'DNA from protein', 'Lipids', 'B', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Transcription?', 'RNA', 'DNA from protein', 'Lipids', 'Amino acids', 'A', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Transcription should identify which statement as correct?', 'DNA from protein', 'Lipids', 'Amino acids', 'RNA', 'D', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Transcription?', 'Lipids', 'Amino acids', 'RNA', 'DNA from protein', 'C', 'Transcription is the synthesis of RNA using a DNA template.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is the product of translation?', 'DNA', 'mRNA only', 'A phospholipid', 'A polypeptide', 'D', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Translation?', 'mRNA only', 'A phospholipid', 'A polypeptide', 'DNA', 'C', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Translation?', 'A phospholipid', 'A polypeptide', 'DNA', 'mRNA only', 'B', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Translation should identify which statement as correct?', 'A polypeptide', 'DNA', 'mRNA only', 'A phospholipid', 'A', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Translation?', 'DNA', 'mRNA only', 'A phospholipid', 'A polypeptide', 'D', 'Translation uses ribosomes to synthesize a polypeptide according to the information in mRNA.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a codon?', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'A DNA enzyme', 'A lipid group', 'A', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Codon?', 'A protein domain', 'A DNA enzyme', 'A lipid group', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'D', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Codon?', 'A DNA enzyme', 'A lipid group', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'C', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Codon should identify which statement as correct?', 'A lipid group', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'A DNA enzyme', 'B', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Codon?', 'A three-nucleotide mRNA unit specifying an amino acid or stop', 'A protein domain', 'A DNA enzyme', 'A lipid group', 'A', 'A codon is a three-nucleotide sequence in mRNA that specifies an amino acid or a stop signal.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a spontaneous mutation?', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'A protein modification', 'B', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Spontaneous mutation?', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'A protein modification', 'A normal transcription event', 'A', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Spontaneous mutation?', 'A mutation caused only by radiation', 'A protein modification', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'D', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Spontaneous mutation should identify which statement as correct?', 'A protein modification', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'C', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Spontaneous mutation?', 'A normal transcription event', 'A mutation arising naturally without deliberate induction', 'A mutation caused only by radiation', 'A protein modification', 'B', 'Spontaneous mutations arise without deliberate experimental induction and can result from natural replication or repair errors.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What is the purpose of DNA repair mechanisms?', 'To synthesize lipids', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'C', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA repair?', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'To synthesize lipids', 'B', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA repair?', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'To synthesize lipids', 'To destroy all genes', 'A', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA repair should identify which statement as correct?', 'To translate mRNA', 'To synthesize lipids', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'D', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA repair?', 'To synthesize lipids', 'To destroy all genes', 'To detect and correct DNA damage or errors', 'To translate mRNA', 'C', 'DNA repair pathways detect and correct different forms of DNA damage or replication errors.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is DNA methylation?', 'A change in amino-acid sequence', 'A type of protein translation', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'D', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with DNA methylation?', 'A type of protein translation', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'A change in amino-acid sequence', 'C', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of DNA methylation?', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'A change in amino-acid sequence', 'A type of protein translation', 'B', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying DNA methylation should identify which statement as correct?', 'An epigenetic chemical modification of DNA', 'A change in amino-acid sequence', 'A type of protein translation', 'A chromosome duplication only', 'A', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of DNA methylation?', 'A change in amino-acid sequence', 'A type of protein translation', 'A chromosome duplication only', 'An epigenetic chemical modification of DNA', 'D', 'DNA methylation is an epigenetic modification that can influence gene expression without changing the DNA sequence itself.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('How can histone modification affect gene expression?', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'By replacing mRNA with protein', 'By digesting chromosomes', 'A', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Histone modification?', 'By changing every DNA base', 'By replacing mRNA with protein', 'By digesting chromosomes', 'By altering chromatin properties and DNA accessibility', 'D', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Histone modification?', 'By replacing mRNA with protein', 'By digesting chromosomes', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'C', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Histone modification should identify which statement as correct?', 'By digesting chromosomes', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'By replacing mRNA with protein', 'B', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Histone modification?', 'By altering chromatin properties and DNA accessibility', 'By changing every DNA base', 'By replacing mRNA with protein', 'By digesting chromosomes', 'A', 'Histone modifications can alter chromatin properties and influence accessibility of DNA to transcription machinery.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What is the lac operon associated with?', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'Protein folding only', 'B', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Lac operon?', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'Protein folding only', 'Lipid synthesis in humans', 'A', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Lac operon?', 'DNA replication in mitochondria', 'Protein folding only', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'D', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Lac operon should identify which statement as correct?', 'Protein folding only', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'C', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Lac operon?', 'Lipid synthesis in humans', 'Regulation of lactose-utilization genes', 'DNA replication in mitochondria', 'Protein folding only', 'B', 'The lac operon regulates genes involved in lactose utilization in bacteria and is controlled by regulatory elements including the operator and repressor.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What does the trp operon regulate?', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'C', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Trp operon?', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'Mitochondrial ATP synthesis', 'B', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Trp operon?', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'A', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Trp operon should identify which statement as correct?', 'Genes for lactose digestion in humans', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'D', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Trp operon?', 'Mitochondrial ATP synthesis', 'Protein electrophoresis', 'Genes involved in tryptophan biosynthesis', 'Genes for lactose digestion in humans', 'C', 'The trp operon is a bacterial regulatory system for tryptophan biosynthesis and is subject to repression and attenuation mechanisms.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What is a key property of restriction enzymes?', 'They synthesize proteins', 'They translate mRNA', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'D', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Restriction enzymes?', 'They translate mRNA', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'They synthesize proteins', 'C', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Restriction enzymes?', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'They synthesize proteins', 'They translate mRNA', 'B', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Restriction enzymes should identify which statement as correct?', 'They recognize specific DNA sequences and cleave DNA', 'They synthesize proteins', 'They translate mRNA', 'They join amino acids', 'A', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Restriction enzymes?', 'They synthesize proteins', 'They translate mRNA', 'They join amino acids', 'They recognize specific DNA sequences and cleave DNA', 'D', 'Restriction endonucleases recognize specific DNA sequences and cleave DNA at or near those sites.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Why are Type II restriction enzymes useful in cloning?', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'They synthesize RNA', 'They replicate plasmids by themselves', 'A', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Type II restriction enzymes?', 'They randomly destroy all DNA', 'They synthesize RNA', 'They replicate plasmids by themselves', 'They can cut DNA at predictable recognition sites', 'D', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which of the following is a correct feature of Type II restriction enzymes?', 'They synthesize RNA', 'They replicate plasmids by themselves', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'C', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('A student studying Type II restriction enzymes should identify which statement as correct?', 'They replicate plasmids by themselves', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'They synthesize RNA', 'B', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('Which option correctly explains the role or meaning of Type II restriction enzymes?', 'They can cut DNA at predictable recognition sites', 'They randomly destroy all DNA', 'They synthesize RNA', 'They replicate plasmids by themselves', 'A', 'Type II restriction enzymes are widely used in genetic engineering because many recognize defined sequences and cut at predictable positions.', 'Genetic Engineering & Molecular Biology', 'Medium'),
        ('What does DNA ligase do in cloning?', 'Unwinds proteins', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'B', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DNA ligase?', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'Unwinds proteins', 'A', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of DNA ligase?', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'Unwinds proteins', 'Joins DNA fragments', 'D', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying DNA ligase should identify which statement as correct?', 'Synthesizes RNA', 'Unwinds proteins', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'C', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of DNA ligase?', 'Unwinds proteins', 'Joins DNA fragments', 'Cuts DNA at restriction sites', 'Synthesizes RNA', 'B', 'DNA ligase joins DNA fragments by forming phosphodiester bonds in the DNA backbone.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is a common role of a plasmid vector?', 'Separating DNA by size', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'C', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Plasmid vectors?', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'Separating DNA by size', 'B', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Plasmid vectors?', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'Separating DNA by size', 'Translating RNA', 'A', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Plasmid vectors should identify which statement as correct?', 'Digesting proteins', 'Separating DNA by size', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'D', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Plasmid vectors?', 'Separating DNA by size', 'Translating RNA', 'Carrying DNA inserts for cloning or expression', 'Digesting proteins', 'C', 'Plasmids are small circular DNA molecules commonly used as cloning or expression vectors in bacteria.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What directs Cas9 to its target sequence?', 'A ribosomal protein', 'A lipid', 'An antibody', 'A guide RNA', 'D', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with CRISPR-Cas9?', 'A lipid', 'An antibody', 'A guide RNA', 'A ribosomal protein', 'C', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of CRISPR-Cas9?', 'An antibody', 'A guide RNA', 'A ribosomal protein', 'A lipid', 'B', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying CRISPR-Cas9 should identify which statement as correct?', 'A guide RNA', 'A ribosomal protein', 'A lipid', 'An antibody', 'A', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of CRISPR-Cas9?', 'A ribosomal protein', 'A lipid', 'An antibody', 'A guide RNA', 'D', 'CRISPR-Cas9 uses a guide RNA and Cas9 nuclease to target and cleave a complementary DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is central to Sanger sequencing?', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'Protein antibodies', 'Lipid dyes', 'A', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Sanger sequencing?', 'Restriction enzymes only', 'Protein antibodies', 'Lipid dyes', 'Chain-terminating dideoxynucleotides', 'D', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which of the following is a correct feature of Sanger sequencing?', 'Protein antibodies', 'Lipid dyes', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'C', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('A student studying Sanger sequencing should identify which statement as correct?', 'Lipid dyes', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'Protein antibodies', 'B', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('Which option correctly explains the role or meaning of Sanger sequencing?', 'Chain-terminating dideoxynucleotides', 'Restriction enzymes only', 'Protein antibodies', 'Lipid dyes', 'A', 'Sanger sequencing uses chain-terminating dideoxynucleotides to determine DNA sequence.', 'Genetic Engineering & Molecular Biology', 'Easy'),
        ('What is the primary catalytic role of an enzyme?', 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'B', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Enzymes?', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'Be permanently consumed in the reaction', 'A', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Enzymes?', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'D', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Enzymes should identify which statement as correct?', "Increase the reaction's ΔG", 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', 'C', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Enzymes?', 'Be permanently consumed in the reaction', 'Increase reaction rate by lowering activation energy', 'Increase the equilibrium constant', "Increase the reaction's ΔG", 'B', 'Most enzymes are biological catalysts that increase reaction rate by lowering activation energy.', 'Enzymes & Metabolism', 'Easy'),
        ('Which model describes a relatively rigid active site that complements the substrate?', 'Operon model', 'Endosymbiotic model', 'Lock-and-key model', 'Fluid mosaic model', 'C', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Lock-and-key model?', 'Endosymbiotic model', 'Lock-and-key model', 'Fluid mosaic model', 'Operon model', 'B', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Lock-and-key model?', 'Lock-and-key model', 'Fluid mosaic model', 'Operon model', 'Endosymbiotic model', 'A', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Lock-and-key model should identify which statement as correct?', 'Fluid mosaic model', 'Operon model', 'Endosymbiotic model', 'Lock-and-key model', 'D', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Lock-and-key model?', 'Operon model', 'Endosymbiotic model', 'Lock-and-key model', 'Fluid mosaic model', 'C', 'The lock-and-key model proposes a relatively rigid active site complementary to the substrate.', 'Enzymes & Metabolism', 'Easy'),
        ('What happens in the induced-fit model when substrate binds?', 'The enzyme is degraded', 'The substrate becomes DNA', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'D', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Induced-fit model?', 'The substrate becomes DNA', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'The enzyme is degraded', 'C', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Induced-fit model?', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'The enzyme is degraded', 'The substrate becomes DNA', 'B', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Induced-fit model should identify which statement as correct?', 'The enzyme changes conformation to improve catalytic alignment', 'The enzyme is degraded', 'The substrate becomes DNA', 'The active site disappears', 'A', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Induced-fit model?', 'The enzyme is degraded', 'The substrate becomes DNA', 'The active site disappears', 'The enzyme changes conformation to improve catalytic alignment', 'D', 'The induced-fit model proposes that substrate binding causes a conformational change that improves catalytic alignment.', 'Enzymes & Metabolism', 'Medium'),
        ('What is a ribozyme?', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Ribozymes?', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A catalytically active RNA molecule', 'D', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Ribozymes?', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'C', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Ribozymes should identify which statement as correct?', 'A protein-only receptor', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'B', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Ribozymes?', 'A catalytically active RNA molecule', 'A DNA-binding lipid', 'A carbohydrate enzyme inhibitor', 'A protein-only receptor', 'A', 'Ribozymes are RNA molecules with catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('What is the basis of EC enzyme classification?', 'Subcellular color', 'The type of reaction catalyzed', "The organism's habitat only", 'Protein length only', 'B', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with EC classification?', 'The type of reaction catalyzed', "The organism's habitat only", 'Protein length only', 'Subcellular color', 'A', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of EC classification?', "The organism's habitat only", 'Protein length only', 'Subcellular color', 'The type of reaction catalyzed', 'D', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying EC classification should identify which statement as correct?', 'Protein length only', 'Subcellular color', 'The type of reaction catalyzed', "The organism's habitat only", 'C', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of EC classification?', 'Subcellular color', 'The type of reaction catalyzed', "The organism's habitat only", 'Protein length only', 'B', 'The Enzyme Commission system classifies enzymes according to the reactions they catalyze.', 'Enzymes & Metabolism', 'Medium'),
        ('What is a cofactor?', 'A ribosomal subunit', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'A substrate gene', 'C', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Cofactors?', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'A substrate gene', 'A ribosomal subunit', 'B', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Cofactors?', 'A non-protein component required by some enzymes', 'A substrate gene', 'A ribosomal subunit', 'A type of nucleic acid', 'A', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Cofactors should identify which statement as correct?', 'A substrate gene', 'A ribosomal subunit', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'D', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Cofactors?', 'A ribosomal subunit', 'A type of nucleic acid', 'A non-protein component required by some enzymes', 'A substrate gene', 'C', 'Cofactors are non-protein components required by some enzymes for activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which description best fits a coenzyme?', 'A protein that forms ribosomes', 'A membrane lipid', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'D', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Coenzymes?', 'A membrane lipid', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'A protein that forms ribosomes', 'C', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Coenzymes?', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'A protein that forms ribosomes', 'A membrane lipid', 'B', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Coenzymes should identify which statement as correct?', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'A protein that forms ribosomes', 'A membrane lipid', 'A DNA promoter', 'A', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Coenzymes?', 'A protein that forms ribosomes', 'A membrane lipid', 'A DNA promoter', 'An organic cofactor that assists catalysis by transferring electrons or groups', 'D', 'Coenzymes are organic cofactors that often transfer electrons or chemical groups during reactions.', 'Enzymes & Metabolism', 'Medium'),
        ('What are isoenzymes?', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'A', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Isoenzymes?', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'Different enzyme forms catalyzing the same reaction', 'D', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Isoenzymes?', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'C', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Isoenzymes should identify which statement as correct?', 'Different substrates for one enzyme', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'B', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Isoenzymes?', 'Different enzyme forms catalyzing the same reaction', 'Enzymes that catalyze unrelated reactions', 'Inactive RNA fragments', 'Different substrates for one enzyme', 'A', 'Isoenzymes are different molecular forms of an enzyme that catalyze the same reaction and can differ in tissue distribution or regulation.', 'Enzymes & Metabolism', 'Medium'),
        ('Which inhibitor competes directly with substrate for the active site?', 'Coenzyme', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'Allosteric activator', 'B', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Competitive inhibition?', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'Allosteric activator', 'Coenzyme', 'A', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Competitive inhibition?', 'Uncompetitive inhibitor', 'Allosteric activator', 'Coenzyme', 'Competitive inhibitor', 'D', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Competitive inhibition should identify which statement as correct?', 'Allosteric activator', 'Coenzyme', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'C', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Competitive inhibition?', 'Coenzyme', 'Competitive inhibitor', 'Uncompetitive inhibitor', 'Allosteric activator', 'B', 'A competitive inhibitor competes with substrate for the active site and can often be overcome by increasing substrate concentration.', 'Enzymes & Metabolism', 'Easy'),
        ('In ideal pure noncompetitive inhibition, what is primarily reduced?', "The enzyme's amino-acid sequence", 'The reaction temperature', 'Vmax', 'Substrate concentration at every time', 'C', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Noncompetitive inhibition?', 'The reaction temperature', 'Vmax', 'Substrate concentration at every time', "The enzyme's amino-acid sequence", 'B', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Noncompetitive inhibition?', 'Vmax', 'Substrate concentration at every time', "The enzyme's amino-acid sequence", 'The reaction temperature', 'A', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Noncompetitive inhibition should identify which statement as correct?', 'Substrate concentration at every time', "The enzyme's amino-acid sequence", 'The reaction temperature', 'Vmax', 'D', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Noncompetitive inhibition?', "The enzyme's amino-acid sequence", 'The reaction temperature', 'Vmax', 'Substrate concentration at every time', 'C', 'In pure noncompetitive inhibition, inhibitor binding reduces catalytic activity without changing substrate binding affinity in the idealized model.', 'Enzymes & Metabolism', 'Medium'),
        ('What does the Michaelis-Menten equation relate?', 'DNA length to GC content', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'D', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Michaelis-Menten?', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'DNA length to GC content', 'C', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Michaelis-Menten?', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'DNA length to GC content', 'Protein mass to pH only', 'B', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Michaelis-Menten should identify which statement as correct?', 'Initial reaction velocity to substrate concentration', 'DNA length to GC content', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'A', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Michaelis-Menten?', 'DNA length to GC content', 'Protein mass to pH only', 'ATP synthesis to chromosome number', 'Initial reaction velocity to substrate concentration', 'D', 'The Michaelis-Menten equation relates initial reaction velocity to substrate concentration for a simple enzyme-catalyzed reaction.', 'Enzymes & Metabolism', 'Easy'),
        ('What does Km represent in the basic Michaelis-Menten model?', 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'The final product concentration', "The enzyme's molecular weight", 'A', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Km?', 'The maximum enzyme concentration', 'The final product concentration', "The enzyme's molecular weight", 'The substrate concentration at half Vmax', 'D', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Km?', 'The final product concentration', "The enzyme's molecular weight", 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'C', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Km should identify which statement as correct?', "The enzyme's molecular weight", 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'The final product concentration', 'B', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Km?', 'The substrate concentration at half Vmax', 'The maximum enzyme concentration', 'The final product concentration', "The enzyme's molecular weight", 'A', 'For a simple Michaelis-Menten enzyme, Km is the substrate concentration at which velocity is half of Vmax.', 'Enzymes & Metabolism', 'Medium'),
        ('When is Vmax approached?', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'When the enzyme is denatured', 'B', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Vmax?', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'When the enzyme is denatured', 'When pH is always 7', 'A', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Vmax?', 'When no substrate is present', 'When the enzyme is denatured', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'D', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Vmax should identify which statement as correct?', 'When the enzyme is denatured', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'C', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Vmax?', 'When pH is always 7', 'When the enzyme is saturated with substrate', 'When no substrate is present', 'When the enzyme is denatured', 'B', 'Vmax is the limiting initial velocity approached when substrate concentration is sufficiently high and enzyme is saturated.', 'Enzymes & Metabolism', 'Easy'),
        ('Which axes define a Lineweaver-Burk plot?', 'pH versus temperature', 'DNA length versus GC%', '1/v versus 1/[S]', 'v versus [S]^2', 'C', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Lineweaver-Burk plot?', 'DNA length versus GC%', '1/v versus 1/[S]', 'v versus [S]^2', 'pH versus temperature', 'B', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Lineweaver-Burk plot?', '1/v versus 1/[S]', 'v versus [S]^2', 'pH versus temperature', 'DNA length versus GC%', 'A', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Lineweaver-Burk plot should identify which statement as correct?', 'v versus [S]^2', 'pH versus temperature', 'DNA length versus GC%', '1/v versus 1/[S]', 'D', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Lineweaver-Burk plot?', 'pH versus temperature', 'DNA length versus GC%', '1/v versus 1/[S]', 'v versus [S]^2', 'C', 'A Lineweaver-Burk plot is a double-reciprocal plot of 1/v against 1/[S].', 'Enzymes & Metabolism', 'Medium'),
        ('Why can enzyme activity fall at high temperature?', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'D', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Enzyme temperature?', 'ATP becomes DNA', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'Substrate concentration becomes infinite', 'C', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Enzyme temperature?', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'B', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Enzyme temperature should identify which statement as correct?', 'Protein structure can be disrupted, reducing catalytic activity', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'All enzymes become ribozymes', 'A', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Enzyme temperature?', 'Substrate concentration becomes infinite', 'ATP becomes DNA', 'All enzymes become ribozymes', 'Protein structure can be disrupted, reducing catalytic activity', 'D', 'Increasing temperature generally increases enzyme reaction rate up to an optimum, after which activity may fall because of structural disruption.', 'Enzymes & Metabolism', 'Easy'),
        ('Why does pH affect enzyme activity?', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'It always increases Vmax', 'It converts proteins into lipids', 'A', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Enzyme pH?', 'It changes the genetic code', 'It always increases Vmax', 'It converts proteins into lipids', 'It can alter ionization states, structure and catalytic residues', 'D', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Enzyme pH?', 'It always increases Vmax', 'It converts proteins into lipids', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'C', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Enzyme pH should identify which statement as correct?', 'It converts proteins into lipids', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'It always increases Vmax', 'B', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Enzyme pH?', 'It can alter ionization states, structure and catalytic residues', 'It changes the genetic code', 'It always increases Vmax', 'It converts proteins into lipids', 'A', 'Enzymes have characteristic pH ranges in which catalytic activity is optimal because ionization states affect structure and catalysis.', 'Enzymes & Metabolism', 'Medium'),
        ('Where does an allosteric regulator typically bind?', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'Only to the ribosome', 'B', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Allosteric regulation?', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'Only to the ribosome', 'Inside the substrate molecule', 'A', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Allosteric regulation?', 'Only to DNA bases', 'Only to the ribosome', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'D', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Allosteric regulation should identify which statement as correct?', 'Only to the ribosome', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'C', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Allosteric regulation?', 'Inside the substrate molecule', 'At a regulatory site distinct from the active site', 'Only to DNA bases', 'Only to the ribosome', 'B', 'Allosteric regulators bind at sites distinct from the active site and alter enzyme activity.', 'Enzymes & Metabolism', 'Easy'),
        ('What is the usual target of feedback inhibition?', 'DNA replication only', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'C', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Feedback inhibition?', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'DNA replication only', 'B', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Feedback inhibition?', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'DNA replication only', 'Ribosomal RNA only', 'A', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Feedback inhibition should identify which statement as correct?', 'The cell membrane phospholipids only', 'DNA replication only', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'D', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Feedback inhibition?', 'DNA replication only', 'Ribosomal RNA only', 'An enzyme early in a metabolic pathway', 'The cell membrane phospholipids only', 'C', "Feedback inhibition occurs when a pathway's end product inhibits an earlier enzyme, limiting unnecessary product accumulation.", 'Enzymes & Metabolism', 'Medium'),
        ('What is an advantage of enzyme immobilization?', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'D', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Immobilized enzymes?', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'The enzyme becomes DNA', 'C', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Immobilized enzymes?', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'B', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Immobilized enzymes should identify which statement as correct?', 'The enzyme can often be recovered and reused', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'A', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Immobilized enzymes?', 'The enzyme becomes DNA', 'The enzyme cannot catalyze reactions', 'The enzyme loses all specificity', 'The enzyme can often be recovered and reused', 'D', 'Immobilized enzymes are physically confined or attached to a support while retaining catalytic activity.', 'Enzymes & Metabolism', 'Easy'),
        ('What is the main carbon product of glycolysis from glucose?', 'Pyruvate', 'Urea', 'Cholesterol', 'Glycogen', 'A', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Glycolysis?', 'Urea', 'Cholesterol', 'Glycogen', 'Pyruvate', 'D', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Glycolysis?', 'Cholesterol', 'Glycogen', 'Pyruvate', 'Urea', 'C', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Glycolysis should identify which statement as correct?', 'Glycogen', 'Pyruvate', 'Urea', 'Cholesterol', 'B', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Glycolysis?', 'Pyruvate', 'Urea', 'Cholesterol', 'Glycogen', 'A', 'Glycolysis converts glucose to pyruvate through a series of reactions in the cytosol.', 'Enzymes & Metabolism', 'Easy'),
        ('What enters the citric acid cycle as the key two-carbon acetyl unit?', 'Lactose', 'Acetyl-CoA', 'Glucose-6-phosphate', 'Urea', 'B', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Citric acid cycle?', 'Acetyl-CoA', 'Glucose-6-phosphate', 'Urea', 'Lactose', 'A', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of Citric acid cycle?', 'Glucose-6-phosphate', 'Urea', 'Lactose', 'Acetyl-CoA', 'D', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying Citric acid cycle should identify which statement as correct?', 'Urea', 'Lactose', 'Acetyl-CoA', 'Glucose-6-phosphate', 'C', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of Citric acid cycle?', 'Lactose', 'Acetyl-CoA', 'Glucose-6-phosphate', 'Urea', 'B', 'The citric acid cycle oxidizes acetyl-CoA and generates reduced electron carriers such as NADH and FADH2.', 'Enzymes & Metabolism', 'Medium'),
        ('Which reduced forms carry high-energy electrons?', 'ATP and ADP', 'DNA and RNA', 'NADH and FADH2', 'NAD+ and FAD', 'C', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with NAD and FAD?', 'DNA and RNA', 'NADH and FADH2', 'NAD+ and FAD', 'ATP and ADP', 'B', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of NAD and FAD?', 'NADH and FADH2', 'NAD+ and FAD', 'ATP and ADP', 'DNA and RNA', 'A', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying NAD and FAD should identify which statement as correct?', 'NAD+ and FAD', 'ATP and ADP', 'DNA and RNA', 'NADH and FADH2', 'D', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of NAD and FAD?', 'ATP and ADP', 'DNA and RNA', 'NADH and FADH2', 'NAD+ and FAD', 'C', 'NAD+ and FAD are electron-accepting coenzymes that can become NADH and FADH2.', 'Enzymes & Metabolism', 'Easy'),
        ('What type of group is commonly carried by coenzyme A?', 'Phosphate groups only', 'DNA bases', 'Amino acids only', 'Acyl groups', 'D', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('In the syllabus context, which option is most directly associated with CoA?', 'DNA bases', 'Amino acids only', 'Acyl groups', 'Phosphate groups only', 'C', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('Which of the following is a correct feature of CoA?', 'Amino acids only', 'Acyl groups', 'Phosphate groups only', 'DNA bases', 'B', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('A student studying CoA should identify which statement as correct?', 'Acyl groups', 'Phosphate groups only', 'DNA bases', 'Amino acids only', 'A', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('Which option correctly explains the role or meaning of CoA?', 'Phosphate groups only', 'DNA bases', 'Amino acids only', 'Acyl groups', 'D', 'Coenzyme A carries acyl groups, including the acetyl group of acetyl-CoA.', 'Enzymes & Metabolism', 'Medium'),
        ('Which pair consists only of purines?', 'Adenine and guanine', 'Cytosine and thymine', 'Thymine and uracil', 'Cytosine and uracil', 'A', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Purine and pyrimidine metabolism?', 'Cytosine and thymine', 'Thymine and uracil', 'Cytosine and uracil', 'Adenine and guanine', 'D', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of Purine and pyrimidine metabolism?', 'Thymine and uracil', 'Cytosine and uracil', 'Adenine and guanine', 'Cytosine and thymine', 'C', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying Purine and pyrimidine metabolism should identify which statement as correct?', 'Cytosine and uracil', 'Adenine and guanine', 'Cytosine and thymine', 'Thymine and uracil', 'B', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of Purine and pyrimidine metabolism?', 'Adenine and guanine', 'Cytosine and thymine', 'Thymine and uracil', 'Cytosine and uracil', 'A', 'Purines include adenine and guanine, whereas pyrimidines include cytosine, thymine and uracil.', 'Enzymes & Metabolism', 'Easy'),
        ('What type of resource is BRENDA?', 'A DNA cloning vector', 'An enzyme information database', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'B', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('In the syllabus context, which option is most directly associated with BRENDA?', 'An enzyme information database', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'A DNA cloning vector', 'A', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('Which of the following is a correct feature of BRENDA?', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'A DNA cloning vector', 'An enzyme information database', 'D', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('A student studying BRENDA should identify which statement as correct?', 'A protein electrophoresis gel', 'A DNA cloning vector', 'An enzyme information database', 'A nucleotide sequencing instrument', 'C', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('Which option correctly explains the role or meaning of BRENDA?', 'A DNA cloning vector', 'An enzyme information database', 'A nucleotide sequencing instrument', 'A protein electrophoresis gel', 'B', 'BRENDA is a database of enzyme information including enzyme function, kinetics and related data.', 'Enzymes & Metabolism', 'Easy'),
        ('What is AlphaFold primarily associated with?', 'Protein staining', 'PCR amplification', 'AI-based protein structure prediction', 'DNA restriction digestion', 'C', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with AlphaFold?', 'PCR amplification', 'AI-based protein structure prediction', 'DNA restriction digestion', 'Protein staining', 'B', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of AlphaFold?', 'AI-based protein structure prediction', 'DNA restriction digestion', 'Protein staining', 'PCR amplification', 'A', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('A student studying AlphaFold should identify which statement as correct?', 'DNA restriction digestion', 'Protein staining', 'PCR amplification', 'AI-based protein structure prediction', 'D', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of AlphaFold?', 'Protein staining', 'PCR amplification', 'AI-based protein structure prediction', 'DNA restriction digestion', 'C', 'AlphaFold uses deep learning to predict protein three-dimensional structures from amino-acid sequences and related information.', 'Bioinformatics', 'Easy'),
        ('How can AI support drug discovery?', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'D', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with AI-driven drug discovery?', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'By replacing all laboratory experiments automatically', 'C', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of AI-driven drug discovery?', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'B', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('A student studying AI-driven drug discovery should identify which statement as correct?', 'By learning patterns to prioritize targets or candidate molecules', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'A', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of AI-driven drug discovery?', 'By replacing all laboratory experiments automatically', 'By converting proteins into chromosomes', 'By eliminating biological databases', 'By learning patterns to prioritize targets or candidate molecules', 'D', 'AI can assist drug discovery by learning patterns in biological and chemical datasets to prioritize candidates and targets.', 'Bioinformatics', 'Medium'),
        ('What is a common goal of biological data mining?', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'A', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Biological data mining?', 'Destroying database records', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'Finding patterns and generating testable hypotheses', 'D', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Biological data mining?', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'C', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('A student studying Biological data mining should identify which statement as correct?', 'Measuring blood pressure directly', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'Changing amino-acid sequences', 'B', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Biological data mining?', 'Finding patterns and generating testable hypotheses', 'Destroying database records', 'Changing amino-acid sequences', 'Measuring blood pressure directly', 'A', 'Data mining can identify patterns, associations and useful hypotheses in large biological datasets.', 'Bioinformatics', 'Easy'),
        ('What can a protein interaction network help reveal?', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'Only microscope magnification', 'B', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Protein interaction networks?', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'Only microscope magnification', 'Only protein molecular weight', 'A', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of Protein interaction networks?', 'Only DNA base composition', 'Only microscope magnification', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'D', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('A student studying Protein interaction networks should identify which statement as correct?', 'Only microscope magnification', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'C', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of Protein interaction networks?', 'Only protein molecular weight', 'Functional relationships, modules and highly connected proteins', 'Only DNA base composition', 'Only microscope magnification', 'B', 'Protein-protein interaction networks represent relationships among proteins and can be analyzed to identify functional modules or hubs.', 'Bioinformatics', 'Medium'),
        ('What is an SVM commonly used for in bioinformatics?', 'Protein digestion', 'Cell culture', 'Classification or prediction from labeled features', 'DNA extraction', 'C', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Support Vector Machines?', 'Cell culture', 'Classification or prediction from labeled features', 'DNA extraction', 'Protein digestion', 'B', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Support Vector Machines?', 'Classification or prediction from labeled features', 'DNA extraction', 'Protein digestion', 'Cell culture', 'A', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('A student studying Support Vector Machines should identify which statement as correct?', 'DNA extraction', 'Protein digestion', 'Cell culture', 'Classification or prediction from labeled features', 'D', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Support Vector Machines?', 'Protein digestion', 'Cell culture', 'Classification or prediction from labeled features', 'DNA extraction', 'C', 'SVMs are supervised machine-learning models that can classify data by finding a separating decision boundary.', 'Bioinformatics', 'Easy'),
        ('What is a Random Forest?', 'A sequence alignment algorithm only', 'A protein purification method', 'A DNA repair pathway', 'An ensemble of decision trees', 'D', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Random Forests?', 'A protein purification method', 'A DNA repair pathway', 'An ensemble of decision trees', 'A sequence alignment algorithm only', 'C', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Random Forests?', 'A DNA repair pathway', 'An ensemble of decision trees', 'A sequence alignment algorithm only', 'A protein purification method', 'B', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('A student studying Random Forests should identify which statement as correct?', 'An ensemble of decision trees', 'A sequence alignment algorithm only', 'A protein purification method', 'A DNA repair pathway', 'A', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Random Forests?', 'A sequence alignment algorithm only', 'A protein purification method', 'A DNA repair pathway', 'An ensemble of decision trees', 'D', 'Random Forest is an ensemble learning method that combines many decision trees.', 'Bioinformatics', 'Easy'),
        ('What is the purpose of k-means clustering?', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'A', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with K-means clustering?', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'Grouping similar observations into clusters', 'D', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of K-means clustering?', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'C', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('A student studying K-means clustering should identify which statement as correct?', 'Annotating ribosomes experimentally', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'B', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of K-means clustering?', 'Grouping similar observations into clusters', 'Aligning two DNA sequences globally', 'Sequencing a protein directly', 'Annotating ribosomes experimentally', 'A', 'K-means partitions observations into a chosen number of clusters by iteratively assigning points to cluster centers.', 'Bioinformatics', 'Easy'),
        ('What is BLAST mainly used for?', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'Measuring enzyme pH', 'B', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with BLAST?', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'Measuring enzyme pH', 'Separating proteins by charge', 'A', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of BLAST?', 'Predicting blood pressure', 'Measuring enzyme pH', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'D', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('A student studying BLAST should identify which statement as correct?', 'Measuring enzyme pH', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'C', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of BLAST?', 'Separating proteins by charge', 'Finding similar sequence regions in databases', 'Predicting blood pressure', 'Measuring enzyme pH', 'B', 'BLAST rapidly searches sequence databases for local regions of similarity between a query sequence and database sequences.', 'Bioinformatics', 'Easy'),
        ('What does ProtTrans provide?', 'A microscope image archive', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'C', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with ProtTrans?', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'A microscope image archive', 'B', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of ProtTrans?', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'A microscope image archive', 'A lipid metabolism pathway', 'A', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('A student studying ProtTrans should identify which statement as correct?', 'A restriction enzyme catalogue only', 'A microscope image archive', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'D', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of ProtTrans?', 'A microscope image archive', 'A lipid metabolism pathway', 'Machine-learned representations of protein sequences', 'A restriction enzyme catalogue only', 'C', 'ProtTrans refers to protein language-model approaches that learn representations from large protein sequence datasets.', 'Bioinformatics', 'Medium'),
        ('What distinguishes DeepBLAST from traditional BLAST-style searching?', 'It uses only wet-lab PCR', 'It is a protein gel', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'D', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with DeepBLAST?', 'It is a protein gel', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'It uses only wet-lab PCR', 'C', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of DeepBLAST?', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'It uses only wet-lab PCR', 'It is a protein gel', 'B', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('A student studying DeepBLAST should identify which statement as correct?', 'It uses deep learning to model protein sequence relationships', 'It uses only wet-lab PCR', 'It is a protein gel', 'It is a genome sequencer', 'A', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of DeepBLAST?', 'It uses only wet-lab PCR', 'It is a protein gel', 'It is a genome sequencer', 'It uses deep learning to model protein sequence relationships', 'D', 'DeepBLAST applies deep-learning approaches to protein sequence similarity and alignment-related tasks.', 'Bioinformatics', 'Medium'),
        ('What type of sequences are primarily stored in GenBank?', 'Nucleotide sequences', 'Only protein structures', 'Only microarray images', 'Only enzyme kinetic curves', 'A', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with GenBank?', 'Only protein structures', 'Only microarray images', 'Only enzyme kinetic curves', 'Nucleotide sequences', 'D', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of GenBank?', 'Only microarray images', 'Only enzyme kinetic curves', 'Nucleotide sequences', 'Only protein structures', 'C', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('A student studying GenBank should identify which statement as correct?', 'Only enzyme kinetic curves', 'Nucleotide sequences', 'Only protein structures', 'Only microarray images', 'B', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of GenBank?', 'Nucleotide sequences', 'Only protein structures', 'Only microarray images', 'Only enzyme kinetic curves', 'A', 'GenBank is a public database of annotated nucleotide sequences maintained by NCBI.', 'Bioinformatics', 'Easy'),
        ('EMBL is primarily associated with which kind of data?', 'Mass spectra only', 'Nucleotide sequence data', 'Protein gel images', 'Clinical prescriptions', 'B', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with EMBL database?', 'Nucleotide sequence data', 'Protein gel images', 'Clinical prescriptions', 'Mass spectra only', 'A', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of EMBL database?', 'Protein gel images', 'Clinical prescriptions', 'Mass spectra only', 'Nucleotide sequence data', 'D', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('A student studying EMBL database should identify which statement as correct?', 'Clinical prescriptions', 'Mass spectra only', 'Nucleotide sequence data', 'Protein gel images', 'C', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of EMBL database?', 'Mass spectra only', 'Nucleotide sequence data', 'Protein gel images', 'Clinical prescriptions', 'B', 'EMBL is one of the major international nucleotide sequence data resources associated with the European nucleotide archive tradition.', 'Bioinformatics', 'Easy'),
        ('What does DDBJ mainly store?', 'Enzyme reaction temperatures only', 'Microscope videos only', 'Nucleotide sequence records', 'Protein crystal images only', 'C', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with DDBJ?', 'Microscope videos only', 'Nucleotide sequence records', 'Protein crystal images only', 'Enzyme reaction temperatures only', 'B', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of DDBJ?', 'Nucleotide sequence records', 'Protein crystal images only', 'Enzyme reaction temperatures only', 'Microscope videos only', 'A', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('A student studying DDBJ should identify which statement as correct?', 'Protein crystal images only', 'Enzyme reaction temperatures only', 'Microscope videos only', 'Nucleotide sequence records', 'D', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of DDBJ?', 'Enzyme reaction temperatures only', 'Microscope videos only', 'Nucleotide sequence records', 'Protein crystal images only', 'C', 'DDBJ is a major international nucleotide sequence database and exchanges sequence data with other international repositories.', 'Bioinformatics', 'Easy'),
        ('What is a key feature of Swiss-Prot?', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'D', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Swiss-Prot?', 'It is a clustering algorithm', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'It stores only raw DNA reads', 'C', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of Swiss-Prot?', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'B', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('A student studying Swiss-Prot should identify which statement as correct?', 'Manual curation and reviewed protein records', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'It is a sequencing machine', 'A', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of Swiss-Prot?', 'It stores only raw DNA reads', 'It is a clustering algorithm', 'It is a sequencing machine', 'Manual curation and reviewed protein records', 'D', 'Swiss-Prot is the manually reviewed, curated protein sequence component of UniProt.', 'Bioinformatics', 'Medium'),
        ('How does TrEMBL differ from Swiss-Prot?', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'A', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with TrEMBL?', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'TrEMBL contains computationally annotated records awaiting manual review', 'D', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of TrEMBL?', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'C', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('A student studying TrEMBL should identify which statement as correct?', 'They are both sequencing instruments', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'B', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of TrEMBL?', 'TrEMBL contains computationally annotated records awaiting manual review', 'TrEMBL contains only DNA structures', 'Swiss-Prot is always unreviewed', 'They are both sequencing instruments', 'A', 'TrEMBL contains computationally annotated protein sequence records that have not yet undergone Swiss-Prot manual review.', 'Bioinformatics', 'Medium'),
        ('How are derived databases generally created?', 'By deleting annotations', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'By replacing all primary records', 'B', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Derived databases?', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'By replacing all primary records', 'By deleting annotations', 'A', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of Derived databases?', 'By sequencing samples without computers', 'By replacing all primary records', 'By deleting annotations', 'By processing and organizing data from primary databases', 'D', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('A student studying Derived databases should identify which statement as correct?', 'By replacing all primary records', 'By deleting annotations', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'C', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of Derived databases?', 'By deleting annotations', 'By processing and organizing data from primary databases', 'By sequencing samples without computers', 'By replacing all primary records', 'B', 'Derived databases are constructed by processing information from primary databases to create higher-level classifications or patterns.', 'Bioinformatics', 'Medium'),
        ('What does PROSITE help identify?', 'Only lipid droplets', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'C', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with PROSITE?', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'Only lipid droplets', 'B', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of PROSITE?', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'Only lipid droplets', 'Only cell organelles', 'A', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('A student studying PROSITE should identify which statement as correct?', 'Only DNA sequencing errors', 'Only lipid droplets', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'D', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of PROSITE?', 'Only lipid droplets', 'Only cell organelles', 'Protein functional sites, domains or families using patterns/profiles', 'Only DNA sequencing errors', 'C', 'PROSITE is a database of protein families, domains and functional sites represented by patterns and profiles.', 'Bioinformatics', 'Medium'),
        ('What is a major focus of Pfam?', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'Protein families and conserved domains', 'D', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Pfam?', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'Protein families and conserved domains', 'Genome sequencing hardware', 'C', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Pfam?', 'DNA cloning vectors', 'Protein families and conserved domains', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'B', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('A student studying Pfam should identify which statement as correct?', 'Protein families and conserved domains', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'A', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Pfam?', 'Genome sequencing hardware', 'Metabolic disease diagnosis only', 'DNA cloning vectors', 'Protein families and conserved domains', 'D', 'Pfam is a database of protein families represented by conserved domains using profile hidden Markov models.', 'Bioinformatics', 'Easy'),
        ('Which organization provides GenBank and BLAST resources?', 'NCBI', 'PDB only', 'WHO only', 'EMBL-EBI only', 'A', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with NCBI?', 'PDB only', 'WHO only', 'EMBL-EBI only', 'NCBI', 'D', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of NCBI?', 'WHO only', 'EMBL-EBI only', 'NCBI', 'PDB only', 'C', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('A student studying NCBI should identify which statement as correct?', 'EMBL-EBI only', 'NCBI', 'PDB only', 'WHO only', 'B', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of NCBI?', 'NCBI', 'PDB only', 'WHO only', 'EMBL-EBI only', 'A', 'NCBI provides major biological databases and computational resources, including GenBank and BLAST.', 'Bioinformatics', 'Easy'),
        ('What is sequence retrieval?', 'Measuring pH', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'Separating proteins by size', 'B', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Sequence retrieval?', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'Separating proteins by size', 'Measuring pH', 'A', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of Sequence retrieval?', 'Digesting DNA with enzymes', 'Separating proteins by size', 'Measuring pH', 'Searching a database and obtaining sequence records', 'D', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('A student studying Sequence retrieval should identify which statement as correct?', 'Separating proteins by size', 'Measuring pH', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'C', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of Sequence retrieval?', 'Measuring pH', 'Searching a database and obtaining sequence records', 'Digesting DNA with enzymes', 'Separating proteins by size', 'B', 'Sequence retrieval systems allow users to search biological databases and obtain records by identifiers or other queries.', 'Bioinformatics', 'Easy'),
        ('Which feature identifies a standard FASTA record?', 'A gel image', 'A binary executable', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'C', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with FASTA format?', 'A binary executable', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'A gel image', 'B', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of FASTA format?', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'A gel image', 'A binary executable', 'A', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('A student studying FASTA format should identify which statement as correct?', 'A four-column spreadsheet only', 'A gel image', 'A binary executable', 'A header line beginning with > followed by the sequence', 'D', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of FASTA format?', 'A gel image', 'A binary executable', 'A header line beginning with > followed by the sequence', 'A four-column spreadsheet only', 'C', 'FASTA represents biological sequences using a header line beginning with > followed by sequence characters.', 'Bioinformatics', 'Easy'),
        ('What is KEGG especially useful for?', 'Protein staining', 'DNA extraction', 'Microscope calibration', 'Pathway and systems-level biological information', 'D', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with KEGG?', 'DNA extraction', 'Microscope calibration', 'Pathway and systems-level biological information', 'Protein staining', 'C', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of KEGG?', 'Microscope calibration', 'Pathway and systems-level biological information', 'Protein staining', 'DNA extraction', 'B', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('A student studying KEGG should identify which statement as correct?', 'Pathway and systems-level biological information', 'Protein staining', 'DNA extraction', 'Microscope calibration', 'A', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of KEGG?', 'Protein staining', 'DNA extraction', 'Microscope calibration', 'Pathway and systems-level biological information', 'D', 'KEGG links genes and proteins to metabolic pathways and other biological systems information.', 'Bioinformatics', 'Easy'),
        ('What does the CATH acronym represent?', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'A', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with CATH?', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'Class, Architecture, Topology, Homologous superfamily', 'D', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of CATH?', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'C', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('A student studying CATH should identify which statement as correct?', 'Chromosome, ATP, Transfer, Histone', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'B', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of CATH?', 'Class, Architecture, Topology, Homologous superfamily', 'Cell, Amino acid, Translation, Helix', 'Coding, Annotation, Taxonomy, Homology', 'Chromosome, ATP, Transfer, Histone', 'A', 'CATH classifies protein structures hierarchically using Class, Architecture, Topology and Homologous superfamily.', 'Bioinformatics', 'Medium'),
        ('What is SCOP used for?', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'Measuring enzyme Km', 'B', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with SCOP?', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'Measuring enzyme Km', 'Storing clinical images only', 'A', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('Which of the following is a correct feature of SCOP?', 'Running PCR', 'Measuring enzyme Km', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'D', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('A student studying SCOP should identify which statement as correct?', 'Measuring enzyme Km', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'C', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('Which option correctly explains the role or meaning of SCOP?', 'Storing clinical images only', 'Classifying protein structures and evolutionary relationships', 'Running PCR', 'Measuring enzyme Km', 'B', 'SCOP is a structural classification resource that organizes proteins into structural and evolutionary categories.', 'Bioinformatics', 'Medium'),
        ('What is the PDB primarily used for?', 'Running machine-learning models', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'C', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with PDB?', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'Running machine-learning models', 'B', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('Which of the following is a correct feature of PDB?', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'Running machine-learning models', 'Storing patient passwords', 'A', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('A student studying PDB should identify which statement as correct?', 'Storing only DNA primers', 'Running machine-learning models', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'D', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('Which option correctly explains the role or meaning of PDB?', 'Running machine-learning models', 'Storing patient passwords', 'Storing three-dimensional macromolecular structures', 'Storing only DNA primers', 'C', 'The Protein Data Bank stores experimentally determined and computationally modeled three-dimensional structures of biological macromolecules.', 'Bioinformatics', 'Easy'),
        ('What is the proteome?', 'The complete DNA sequence only', 'All cellular lipids only', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'D', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Proteome?', 'All cellular lipids only', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'The complete DNA sequence only', 'C', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Proteome?', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'The complete DNA sequence only', 'All cellular lipids only', 'B', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('A student studying Proteome should identify which statement as correct?', 'The complete set of proteins expressed under a defined condition', 'The complete DNA sequence only', 'All cellular lipids only', 'All metabolites only', 'A', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Proteome?', 'The complete DNA sequence only', 'All cellular lipids only', 'All metabolites only', 'The complete set of proteins expressed under a defined condition', 'D', 'The proteome is the complete set of proteins expressed by a cell, tissue or organism under a defined condition.', 'Proteomics', 'Easy'),
        ('What defines primary protein structure?', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'The DNA promoter', 'The lipid bilayer', 'A', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Primary protein structure?', 'The arrangement of multiple subunits only', 'The DNA promoter', 'The lipid bilayer', 'The linear amino-acid sequence', 'D', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Primary protein structure?', 'The DNA promoter', 'The lipid bilayer', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'C', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('A student studying Primary protein structure should identify which statement as correct?', 'The lipid bilayer', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'The DNA promoter', 'B', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Primary protein structure?', 'The linear amino-acid sequence', 'The arrangement of multiple subunits only', 'The DNA promoter', 'The lipid bilayer', 'A', 'Primary protein structure is the linear amino-acid sequence linked by peptide bonds.', 'Proteomics', 'Easy'),
        ('Which are major protein secondary structures?', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'Micelles and liposomes', 'B', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Secondary structure?', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'Micelles and liposomes', 'Chromosomes and centromeres', 'A', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Secondary structure?', 'DNA double helices and plasmids', 'Micelles and liposomes', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'D', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('A student studying Secondary structure should identify which statement as correct?', 'Micelles and liposomes', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'C', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Secondary structure?', 'Chromosomes and centromeres', 'Alpha helices and beta sheets', 'DNA double helices and plasmids', 'Micelles and liposomes', 'B', 'Alpha helices and beta sheets are major forms of protein secondary structure stabilized mainly by backbone hydrogen bonding.', 'Proteomics', 'Easy'),
        ('What does tertiary structure describe?', 'Only interactions between chromosomes', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'C', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Tertiary structure?', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'Only interactions between chromosomes', 'B', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Tertiary structure?', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'Only interactions between chromosomes', 'Only RNA splicing', 'A', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('A student studying Tertiary structure should identify which statement as correct?', 'Only the amino-acid sequence', 'Only interactions between chromosomes', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'D', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Tertiary structure?', 'Only interactions between chromosomes', 'Only RNA splicing', 'The three-dimensional fold of a single polypeptide', 'Only the amino-acid sequence', 'C', 'Tertiary structure is the overall three-dimensional folding of a single polypeptide chain.', 'Proteomics', 'Easy'),
        ('What does quaternary structure involve?', 'Only peptide-bond formation', 'Only DNA methylation', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'D', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Quaternary structure?', 'Only DNA methylation', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'Only peptide-bond formation', 'C', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Quaternary structure?', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'Only peptide-bond formation', 'Only DNA methylation', 'B', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('A student studying Quaternary structure should identify which statement as correct?', 'Association of multiple polypeptide subunits', 'Only peptide-bond formation', 'Only DNA methylation', 'Only membrane transport', 'A', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Quaternary structure?', 'Only peptide-bond formation', 'Only DNA methylation', 'Only membrane transport', 'Association of multiple polypeptide subunits', 'D', 'Quaternary structure describes how multiple polypeptide subunits associate into a functional protein complex.', 'Proteomics', 'Medium'),
        ('When do post-translational modifications occur?', 'After translation of the polypeptide', 'Before DNA replication only', 'Only before transcription', 'Only during nucleotide synthesis', 'A', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Post-translational modification?', 'Before DNA replication only', 'Only before transcription', 'Only during nucleotide synthesis', 'After translation of the polypeptide', 'D', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Post-translational modification?', 'Only before transcription', 'Only during nucleotide synthesis', 'After translation of the polypeptide', 'Before DNA replication only', 'C', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('A student studying Post-translational modification should identify which statement as correct?', 'Only during nucleotide synthesis', 'After translation of the polypeptide', 'Before DNA replication only', 'Only before transcription', 'B', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Post-translational modification?', 'After translation of the polypeptide', 'Before DNA replication only', 'Only before transcription', 'Only during nucleotide synthesis', 'A', 'Post-translational modifications alter proteins after translation and can regulate activity, localization or stability.', 'Proteomics', 'Easy'),
        ('What is a systems-biology perspective?', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'Studying only microscopy optics', 'B', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Systems biology?', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'Studying only microscopy optics', 'Studying only DNA extraction', 'A', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Systems biology?', 'Studying one amino acid only', 'Studying only microscopy optics', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'D', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('A student studying Systems biology should identify which statement as correct?', 'Studying only microscopy optics', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'C', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Systems biology?', 'Studying only DNA extraction', 'Studying interactions and behavior of biological components as a system', 'Studying one amino acid only', 'Studying only microscopy optics', 'B', 'Systems biology studies interactions among components of biological systems rather than isolated components alone.', 'Proteomics', 'Easy'),
        ('Why integrate proteomics with genomics?', 'To sequence only lipids', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'C', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Proteomics-genomics integration?', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'To sequence only lipids', 'B', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Proteomics-genomics integration?', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'To sequence only lipids', 'To replace all databases', 'A', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('A student studying Proteomics-genomics integration should identify which statement as correct?', 'To eliminate protein measurements', 'To sequence only lipids', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'D', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Proteomics-genomics integration?', 'To sequence only lipids', 'To replace all databases', 'To connect protein observations with their genomic origins and annotations', 'To eliminate protein measurements', 'C', 'Integrating proteomics with genomics connects observed proteins with genes and genomic information.', 'Proteomics', 'Medium'),
        ('What are the two dimensions in 2-DE?', 'DNA length followed by GC content', 'pH followed by temperature', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'D', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Two-dimensional gel electrophoresis?', 'pH followed by temperature', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'DNA length followed by GC content', 'C', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Two-dimensional gel electrophoresis?', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'DNA length followed by GC content', 'pH followed by temperature', 'B', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('A student studying Two-dimensional gel electrophoresis should identify which statement as correct?', 'Isoelectric point followed by molecular mass', 'DNA length followed by GC content', 'pH followed by temperature', 'RNA length followed by charge only', 'A', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Two-dimensional gel electrophoresis?', 'DNA length followed by GC content', 'pH followed by temperature', 'RNA length followed by charge only', 'Isoelectric point followed by molecular mass', 'D', '2-DE separates proteins first by isoelectric point and then by molecular mass.', 'Proteomics', 'Easy'),
        ('What property is used in isoelectric focusing?', 'Isoelectric point', 'DNA sequence', 'Protein gene count', 'Cell diameter', 'A', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Isoelectric focusing?', 'DNA sequence', 'Protein gene count', 'Cell diameter', 'Isoelectric point', 'D', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Isoelectric focusing?', 'Protein gene count', 'Cell diameter', 'Isoelectric point', 'DNA sequence', 'C', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('A student studying Isoelectric focusing should identify which statement as correct?', 'Cell diameter', 'Isoelectric point', 'DNA sequence', 'Protein gene count', 'B', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Isoelectric focusing?', 'Isoelectric point', 'DNA sequence', 'Protein gene count', 'Cell diameter', 'A', 'Isoelectric focusing separates proteins according to their isoelectric points.', 'Proteomics', 'Easy'),
        ('What is the major basis of SDS-PAGE separation?', 'Protein fluorescence only', 'Molecular mass', 'Isoelectric point only', 'DNA sequence', 'B', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with SDS-PAGE?', 'Molecular mass', 'Isoelectric point only', 'DNA sequence', 'Protein fluorescence only', 'A', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of SDS-PAGE?', 'Isoelectric point only', 'DNA sequence', 'Protein fluorescence only', 'Molecular mass', 'D', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('A student studying SDS-PAGE should identify which statement as correct?', 'DNA sequence', 'Protein fluorescence only', 'Molecular mass', 'Isoelectric point only', 'C', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of SDS-PAGE?', 'Protein fluorescence only', 'Molecular mass', 'Isoelectric point only', 'DNA sequence', 'B', 'SDS-PAGE separates proteins primarily by molecular mass after SDS treatment gives proteins a similar charge-to-mass ratio.', 'Proteomics', 'Easy'),
        ('Why is protein solubilization important in 2-DE?', 'To synthesize lipids', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'C', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Protein sample solubilization?', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'To synthesize lipids', 'B', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Protein sample solubilization?', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'To synthesize lipids', 'To sequence chromosomes', 'A', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('A student studying Protein sample solubilization should identify which statement as correct?', 'To replicate DNA', 'To synthesize lipids', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'D', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Protein sample solubilization?', 'To synthesize lipids', 'To sequence chromosomes', 'To bring proteins into a suitable soluble form for separation', 'To replicate DNA', 'C', 'Protein solubilization uses suitable detergents, chaotropes or other agents to bring proteins into solution without excessive aggregation.', 'Proteomics', 'Medium'),
        ('What can a reducing agent do during protein sample preparation?', 'Add DNA bases', 'Digest RNA into nucleotides', 'Create peptide bonds', 'Break disulfide bonds', 'D', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Reduction in sample preparation?', 'Digest RNA into nucleotides', 'Create peptide bonds', 'Break disulfide bonds', 'Add DNA bases', 'C', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Reduction in sample preparation?', 'Create peptide bonds', 'Break disulfide bonds', 'Add DNA bases', 'Digest RNA into nucleotides', 'B', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('A student studying Reduction in sample preparation should identify which statement as correct?', 'Break disulfide bonds', 'Add DNA bases', 'Digest RNA into nucleotides', 'Create peptide bonds', 'A', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Reduction in sample preparation?', 'Add DNA bases', 'Digest RNA into nucleotides', 'Create peptide bonds', 'Break disulfide bonds', 'D', 'Reducing agents can break disulfide bonds, helping unfold proteins for electrophoretic analysis.', 'Proteomics', 'Medium'),
        ('Which factor improves reproducibility of 2-DE?', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'Avoiding normalization', 'A', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with 2-DE reproducibility?', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'Avoiding normalization', 'Consistent experimental and image-analysis conditions', 'D', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of 2-DE reproducibility?', 'Using different sample amounts randomly', 'Avoiding normalization', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'C', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('A student studying 2-DE reproducibility should identify which statement as correct?', 'Avoiding normalization', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'B', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of 2-DE reproducibility?', 'Consistent experimental and image-analysis conditions', 'Changing all conditions between runs', 'Using different sample amounts randomly', 'Avoiding normalization', 'A', 'Reproducibility in 2-DE requires consistent sample preparation, electrophoresis conditions and image analysis.', 'Proteomics', 'Medium'),
        ('What is a key feature of shotgun proteomics?', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'Protein analysis without peptides', 'B', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Shotgun proteomics?', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'Protein analysis without peptides', 'Only microscopy', 'A', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Shotgun proteomics?', 'Separation only by DNA length', 'Protein analysis without peptides', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'D', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('A student studying Shotgun proteomics should identify which statement as correct?', 'Protein analysis without peptides', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'C', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Shotgun proteomics?', 'Only microscopy', 'Peptide analysis by mass spectrometry after protein digestion', 'Separation only by DNA length', 'Protein analysis without peptides', 'B', 'Shotgun proteomics analyzes complex protein mixtures by digesting proteins into peptides and identifying the peptides by mass spectrometry.', 'Proteomics', 'Medium'),
        ('How can MS identify a protein?', 'By observing chromosome shape', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'C', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Mass spectrometry protein identification?', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'By observing chromosome shape', 'B', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Mass spectrometry protein identification?', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'By observing chromosome shape', 'By staining RNA', 'A', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('A student studying Mass spectrometry protein identification should identify which statement as correct?', 'By measuring only cell size', 'By observing chromosome shape', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'D', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Mass spectrometry protein identification?', 'By observing chromosome shape', 'By staining RNA', 'By matching measured peptide spectra to sequence information', 'By measuring only cell size', 'C', 'Mass spectrometry can identify proteins by measuring peptide mass-to-charge ratios and matching spectra to sequence databases.', 'Proteomics', 'Medium'),
        ('What is de novo peptide sequencing?', 'Copying DNA by PCR', 'Separating proteins by pI only', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'D', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with De novo sequencing?', 'Separating proteins by pI only', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'Copying DNA by PCR', 'C', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of De novo sequencing?', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'Copying DNA by PCR', 'Separating proteins by pI only', 'B', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('A student studying De novo sequencing should identify which statement as correct?', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'Copying DNA by PCR', 'Separating proteins by pI only', 'Predicting cell division', 'A', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of De novo sequencing?', 'Copying DNA by PCR', 'Separating proteins by pI only', 'Predicting cell division', 'Inferring peptide sequence from mass spectra without relying on an exact database match', 'D', 'De novo peptide sequencing infers peptide sequence information directly from tandem mass spectra without requiring an exact database match.', 'Proteomics', 'Medium'),
        ('What does tandem MS add to mass spectrometry?', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'DNA replication', 'RNA splicing', 'A', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Tandem mass spectrometry?', 'A second gel electrophoresis dimension', 'DNA replication', 'RNA splicing', 'Fragmentation that provides sequence-informative product ions', 'D', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Tandem mass spectrometry?', 'DNA replication', 'RNA splicing', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'C', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('A student studying Tandem mass spectrometry should identify which statement as correct?', 'RNA splicing', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'DNA replication', 'B', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Tandem mass spectrometry?', 'Fragmentation that provides sequence-informative product ions', 'A second gel electrophoresis dimension', 'DNA replication', 'RNA splicing', 'A', 'Tandem MS selects precursor ions and fragments them to obtain sequence-informative product-ion spectra.', 'Proteomics', 'Medium'),
        ('What is a microarray designed to do?', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'B', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Microarray technology?', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'Culture bacteria automatically', 'A', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Microarray technology?', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'D', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('A student studying Microarray technology should identify which statement as correct?', 'Purify one enzyme', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'C', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Microarray technology?', 'Culture bacteria automatically', 'Measure many molecular interactions or expression signals in parallel', 'Sequence a single protein by microscopy', 'Purify one enzyme', 'B', 'Microarrays use many immobilized probes on a surface to measure hybridization or molecular signals in parallel.', 'Proteomics', 'Easy'),
        ('Which is important in microarray experiment design?', 'Ignoring controls', 'Using only one sample for every study', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'C', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Microarray experiment design?', 'Using only one sample for every study', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'Ignoring controls', 'B', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Microarray experiment design?', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'Ignoring controls', 'Using only one sample for every study', 'A', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('A student studying Microarray experiment design should identify which statement as correct?', 'Changing probe sequences during the experiment', 'Ignoring controls', 'Using only one sample for every study', 'Controls, replicates and normalization', 'D', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Microarray experiment design?', 'Ignoring controls', 'Using only one sample for every study', 'Controls, replicates and normalization', 'Changing probe sequences during the experiment', 'C', 'Good microarray design considers probe selection, controls, biological replicates and normalization.', 'Proteomics', 'Medium'),
        ('How can NGS complement proteomics?', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'D', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Next-generation sequencing and proteomics?', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'By directly measuring enzyme Km', 'C', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Next-generation sequencing and proteomics?', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'B', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('A student studying Next-generation sequencing and proteomics should identify which statement as correct?', 'By providing genomic or transcript information for protein interpretation', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'A', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Next-generation sequencing and proteomics?', 'By directly measuring enzyme Km', 'By replacing mass spectrometry in every application', 'By separating proteins on gels', 'By providing genomic or transcript information for protein interpretation', 'D', 'NGS can complement proteomics by providing transcript or genomic information that helps interpret observed proteins.', 'Proteomics', 'Medium'),
        ('How can proteomics support drug development?', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'By measuring only DNA length', 'By producing antibiotics directly', 'A', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Proteomics in drug development?', 'By replacing all clinical trials', 'By measuring only DNA length', 'By producing antibiotics directly', 'By identifying biomarkers and potential therapeutic targets', 'D', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Proteomics in drug development?', 'By measuring only DNA length', 'By producing antibiotics directly', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'C', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('A student studying Proteomics in drug development should identify which statement as correct?', 'By producing antibiotics directly', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'By measuring only DNA length', 'B', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Proteomics in drug development?', 'By identifying biomarkers and potential therapeutic targets', 'By replacing all clinical trials', 'By measuring only DNA length', 'By producing antibiotics directly', 'A', 'Proteomics can identify disease-associated proteins, biomarkers and drug targets.', 'Proteomics', 'Easy'),
        ('What is a use of phage display in proteomics-related applications?', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'Sequencing whole genomes only', 'B', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('In the syllabus context, which option is most directly associated with Phage antibodies?', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'Sequencing whole genomes only', 'Measuring lipid oxidation', 'A', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('Which of the following is a correct feature of Phage antibodies?', 'Separating proteins by size', 'Sequencing whole genomes only', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'D', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('A student studying Phage antibodies should identify which statement as correct?', 'Sequencing whole genomes only', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'C', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('Which option correctly explains the role or meaning of Phage antibodies?', 'Measuring lipid oxidation', 'Selecting antibody fragments that bind target molecules', 'Separating proteins by size', 'Sequencing whole genomes only', 'B', 'Phage display can present antibody fragments on bacteriophages and select binders against target molecules.', 'Proteomics', 'Medium'),
        ('What is a valid AI application in proteomics?', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'C', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with AI in proteomics?', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'Changing amino-acid chemistry', 'B', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of AI in proteomics?', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'A', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('A student studying AI in proteomics should identify which statement as correct?', 'Replacing every protein with a neural network', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'D', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of AI in proteomics?', 'Changing amino-acid chemistry', 'Preventing all post-translational modifications', 'Automated spectral interpretation and biomarker pattern discovery', 'Replacing every protein with a neural network', 'C', 'AI and machine learning can assist proteomics with spectrum interpretation, pattern recognition, biomarker discovery and prediction.', 'Proteomics', 'Easy'),
        ('What can plant proteomics investigate?', 'Only human chromosomes', 'Only bacterial plasmids', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'D', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('In the syllabus context, which option is most directly associated with Plant proteomics?', 'Only bacterial plasmids', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'Only human chromosomes', 'C', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('Which of the following is a correct feature of Plant proteomics?', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'Only human chromosomes', 'Only bacterial plasmids', 'B', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('A student studying Plant proteomics should identify which statement as correct?', 'Protein changes associated with plant development or stress', 'Only human chromosomes', 'Only bacterial plasmids', 'Only animal antibodies', 'A', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
        ('Which option correctly explains the role or meaning of Plant proteomics?', 'Only human chromosomes', 'Only bacterial plasmids', 'Only animal antibodies', 'Protein changes associated with plant development or stress', 'D', 'Plant proteomics studies protein composition and regulation in plants and can support studies of development, stress and breeding.', 'Proteomics', 'Easy'),
    ]
    existing = {r["question"] for r in conn.execute("SELECT question FROM questions").fetchall()}
    now = datetime.now().isoformat(timespec="seconds")
    rows = [q + (now,) for q in questions if q[0] not in existing]
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
                INSERT INTO mini_challenges
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
    """Return the logged-in user, cached for this request."""
    if getattr(g, "current_user_loaded", False):
        return g.current_user_value
    uid = session.get("user_pk")
    if not uid:
        g.current_user_loaded = True
        g.current_user_value = None
        return None
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    g.current_user_loaded = True
    g.current_user_value = user
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
            except psycopg2.IntegrityError:
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

    # Serialize quiz creation for the same user/day so two tabs or devices
    # cannot create two daily quizzes, without requiring a destructive cleanup
    # of any existing duplicate rows in an older database.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"bioquiz:{user_id}:{today}",)).fetchone()

    existing = conn.execute("""
        SELECT * FROM quiz_sessions
        WHERE user_id=? AND quiz_date=?
        ORDER BY id DESC LIMIT 1
    """, (user_id, today)).fetchone()

    if existing:
        conn.commit()
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
        RETURNING id
    """, (
        user_id, today, ids,
        datetime.now().isoformat(timespec="seconds"),
        len(questions)
    ))
    conn.commit()
    session_id = cur.fetchone()["id"]

    row = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (session_id,)).fetchone()
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

    # One SELECT instead of one database round-trip per question.
    placeholders = ",".join("?" * len(ids))
    qrows = conn.execute(
        f"SELECT id,correct_option FROM questions WHERE id IN ({placeholders})", ids
    ).fetchall()
    correct_map = {r["id"]: r["correct_option"] for r in qrows}

    answer_rows = []
    correct = 0
    for qid in ids:
        selected = request.form.get("q_" + str(qid), "").strip().upper()
        is_correct = int(bool(selected and selected == correct_map.get(qid)))
        correct += is_correct
        answer_rows.append((qsession["id"], qid, selected or None, is_correct))

    if answer_rows:
        conn.executemany("""
            INSERT INTO answers(session_id,question_id,selected_option,is_correct)
            VALUES (?,?,?,?)
        """, answer_rows)

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
            except psycopg2.IntegrityError:
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
                g._bio_user_loaded = False
                g._bio_user = None
                flash("Profile updated.", "success")
            except psycopg2.IntegrityError:
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
            except psycopg2.IntegrityError:
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

    questions = conn.execute("SELECT * FROM questions ORDER BY id DESC LIMIT 600").fetchall()
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
            except psycopg2.Error as e:
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
    conn = None
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        status = "ok"
    except Exception:
        status = "degraded"
    finally:
        if conn:
            conn.close()
    return jsonify({
        "status": status,
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

init_db()
if __name__ == "__main__":
    print("=" * 60)
    print("BIO WARRIORS IS STARTING")
    print("Admin login: admin / admin123")
    print("Open: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG", "0") == "1")
