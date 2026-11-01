"""Build the local question-bank database from the downloaded source fixtures.

The output is a disposable local artifact. Source databases remain in
``data/external`` and are deliberately excluded from version control.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GONGKAO_DIR = ROOT / "data/external/gongkao-naoku"
ERRRC_DIR = ROOT / "data/external/errrc-kaogong-shuati"
DEFAULT_OUTPUT = ROOT / "data/processed/question_bank.db"

GONGKAO_SOURCE_ID = "gongkao-naoku"
ERRRC_SOURCE_ID = "errrc-kaogong-shuati"

XINGCE = "xingce"
SHENLUN = "shenlun"

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
MATERIAL_TAG_RE = re.compile(r"\[materialid\]\d+\[/materialid\]")
MATERIAL_RANGE_RE = re.compile(
    r"\[materialid\]\d+\[/materialid\]\s*[-—~至]\s*"
    r"\[materialid\]\d+\[/materialid\]"
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    license_note TEXT NOT NULL,
    allowed_use TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE papers (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_paper_key TEXT NOT NULL,
    subject TEXT NOT NULL CHECK (subject IN ('xingce', 'shenlun')),
    region TEXT,
    year INTEGER,
    title TEXT NOT NULL,
    duration_seconds INTEGER,
    declared_question_count INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_id, source_paper_key)
);

CREATE TABLE questions (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_question_key TEXT NOT NULL,
    primary_paper_id INTEGER REFERENCES papers(id),
    subject TEXT NOT NULL CHECK (subject IN ('xingce', 'shenlun')),
    module TEXT,
    category TEXT,
    knowledge_point TEXT,
    stem_html TEXT NOT NULL,
    stem_text TEXT NOT NULL,
    passage_html TEXT NOT NULL DEFAULT '',
    answer_text TEXT,
    answer_index INTEGER,
    analysis_text TEXT NOT NULL DEFAULT '',
    analysis_html TEXT,
    question_type TEXT,
    difficulty INTEGER,
    content_hash TEXT NOT NULL,
    has_image INTEGER NOT NULL DEFAULT 0,
    quality_status TEXT NOT NULL DEFAULT 'unverified',
    license_status TEXT NOT NULL DEFAULT 'personal_only',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_id, source_question_key)
);

CREATE TABLE question_papers (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    sequence INTEGER,
    source_path TEXT,
    PRIMARY KEY (question_id, paper_id)
);

CREATE TABLE question_options (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    option_key TEXT,
    label TEXT,
    html TEXT,
    is_answer INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL,
    UNIQUE (question_id, sort_order)
);

CREATE TABLE materials (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source_material_key TEXT,
    material_index INTEGER,
    title TEXT,
    text TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (paper_id, material_index)
);

CREATE TABLE assets (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_ref TEXT NOT NULL,
    local_path TEXT NOT NULL,
    exists_locally INTEGER NOT NULL,
    UNIQUE (source_id, local_path)
);

CREATE TABLE question_assets (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (question_id, asset_id)
);

CREATE TABLE answer_reviews (
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_question_key TEXT NOT NULL,
    official_answer TEXT,
    verified_answer TEXT,
    status TEXT,
    reasoning TEXT,
    supplement TEXT,
    source_url TEXT,
    source_name TEXT,
    verified_at TEXT,
    PRIMARY KEY (source_id, source_question_key)
);

CREATE INDEX idx_papers_filter
    ON papers(subject, region, year);
CREATE INDEX idx_questions_module
    ON questions(subject, module, category);
CREATE INDEX idx_questions_hash
    ON questions(content_hash);
CREATE INDEX idx_question_papers_paper
    ON question_papers(paper_id);
CREATE INDEX idx_materials_paper
    ON materials(paper_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    return WHITESPACE_RE.sub(
        " ",
        unicodedata.normalize(
            "NFKC",
            html.unescape(HTML_TAG_RE.sub(" ", value)),
        ),
    ).strip()


def clean_material_refs(value: str | None) -> str:
    """Remove Fenbi-internal material markers while retaining readable prose."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = MATERIAL_RANGE_RE.sub("", value)
    value = MATERIAL_TAG_RE.sub("", value)
    return value.replace("给定资料-", "给定资料")


def parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def content_hash(*parts: str) -> str:
    """Fingerprint the source question without discarding image references."""
    payload = "\n".join(
        WHITESPACE_RE.sub(" ", html.unescape(part)).strip()
        for part in parts
        if part
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_gongkao_options(value: str | None) -> list[dict[str, Any]]:
    parsed = parse_json(value, [])
    if not isinstance(parsed, list):
        return []
    options: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("key") or "").strip()
        option_html = unicodedata.normalize(
            "NFKC", str(item.get("html") or label)
        )
        options.append(
            {
                "key": str(item.get("key") or label).strip() or None,
                "label": label or None,
                "html": option_html,
                "text": html_to_text(option_html),
                "is_answer": bool(item.get("is_answer")),
                "sort_order": index,
            }
        )
    return options


def parse_errrc_options(value: str | None) -> list[dict[str, Any]]:
    parsed = parse_json(value, [])
    if not isinstance(parsed, list):
        return []
    options: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        label = unicodedata.normalize(
            "NFKC", str(item.get("optionDesc") or "")
        ).strip()
        if not label:
            continue
        key = chr(ord("A") + index)
        options.append(
            {
                "key": key,
                "label": label,
                "html": label,
                "text": label,
                "is_answer": False,
                "sort_order": index,
            }
        )
    return options


def parse_year(value: Any, title: str = "") -> int | None:
    if value is not None and str(value).strip().isdigit():
        return int(value)
    match = re.search(r"(19|20)\d{2}", title)
    return int(match.group(0)) if match else None


def paper_key(source: str, region: str | None, year: int | None, title: str) -> str:
    raw = f"{source}|{region or ''}|{year or ''}|{title}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def insert_source(
    db: sqlite3.Connection,
    source_id: str,
    repository: str,
    commit_sha: str,
    license_note: str,
) -> None:
    db.execute(
        """
        INSERT INTO sources
            (id, repository, commit_sha, license_note, allowed_use, imported_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            repository,
            commit_sha,
            license_note,
            "local-evaluation-only",
            utc_now(),
        ),
    )


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            ("schema_version", "1"),
            ("generated_at", utc_now()),
            ("scope", "civil-service-xingce-and-shenlun"),
            ("usage", "local-evaluation-only"),
        ],
    )


def import_gongkao(db: sqlite3.Connection) -> dict[str, int]:
    source_path = GONGKAO_DIR / "kaogong.db"
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    insert_source(
        db,
        GONGKAO_SOURCE_ID,
        "https://github.com/jangviktor-web/GongkaoNaoku",
        "8ce742aa3513d1165625a471ac2f0b5298be248c",
        "Apache-2.0 code; question data is marked for personal study only.",
    )

    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row

    paper_ids: dict[str, int] = {}

    def ensure_paper(
        title: str,
        region: str | None,
        year: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        key = paper_key("xingce", region, year, title)
        existing = paper_ids.get(key)
        if existing is not None:
            return existing
        cursor = db.execute(
            """
            INSERT INTO papers
                (source_id, source_paper_key, subject, region, year, title,
                 metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                GONGKAO_SOURCE_ID,
                key,
                XINGCE,
                region,
                year,
                unicodedata.normalize("NFKC", title),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        paper_ids[key] = int(cursor.lastrowid)
        return paper_ids[key]

    question_ids: dict[str, int] = {}
    primary_papers: dict[str, int] = {}
    reviews = {
        str(row["qid"]): dict(row)
        for row in source.execute("SELECT * FROM review_verified")
    }
    asset_ids: dict[str, int] = {}
    question_asset_links: set[tuple[int, int]] = set()

    rows = source.execute("SELECT * FROM questions ORDER BY id")
    for row in rows:
        qid = str(row["qid"])
        paper_id = ensure_paper(
            title=str(row["paper"]),
            region=row["region"],
            year=parse_year(row["year"], str(row["paper"])),
        )
        primary_papers[qid] = paper_id

        options = parse_gongkao_options(row["options"])
        option_fingerprint = "\n".join(
            f"{option['key']}:{option['html']}" for option in options
        )
        metadata = {
            "ask_model": row["ask_model"],
            "reasoning": row["reasoning"],
            "fastest": row["fastest"],
            "pitfalls": row["pitfalls"],
            "mother_question": row["mother"],
            "related_question_ids": row["related"],
            "paper_count": row["n_paper"],
            "source_kind": row["source"],
            "source_path": row["path"],
            "frequency": row["freq"],
        }
        review = reviews.get(qid)
        quality_status = "human_reviewed" if review else "unverified"
        cursor = db.execute(
            """
            INSERT INTO questions
                (source_id, source_question_key, primary_paper_id, subject,
                 module, category, knowledge_point, stem_html, stem_text,
                 passage_html, answer_text, answer_index, analysis_text,
                 analysis_html, question_type, difficulty, content_hash,
                 has_image, quality_status, license_status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?)
            """,
            (
                GONGKAO_SOURCE_ID,
                qid,
                paper_id,
                XINGCE,
                row["module"],
                row["category"],
                row["kadian"],
                clean_material_refs(row["stem"]),
                html_to_text(row["stem"]),
                clean_material_refs(row["material"]),
                row["answer"],
                None,
                clean_material_refs(row["analysis"]),
                None,
                row["qtype"],
                int(row["freq"] or 0),
                content_hash(
                    row["stem"] or "",
                    row["material"] or "",
                    row["img_refs"] or "",
                    option_fingerprint,
                ),
                int(row["has_image"] or 0),
                quality_status,
                "personal_only",
                json.dumps(metadata, ensure_ascii=False, default=str),
            ),
        )
        question_id = int(cursor.lastrowid)
        question_ids[qid] = question_id

        db.executemany(
            """
            INSERT INTO question_options
                (question_id, option_key, label, html, is_answer, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    question_id,
                    option["key"],
                    option["label"],
                    option["html"],
                    int(option["is_answer"]),
                    option["sort_order"],
                )
                for option in options
            ],
        )

        for raw_ref in str(row["img_refs"] or "").split(","):
            source_ref = raw_ref.strip()
            if not source_ref:
                continue
            relative = source_ref.replace("\\", "/").strip("/").rstrip("/")
            local_path = f"data/external/gongkao-naoku/{relative}"
            asset_id = asset_ids.get(local_path)
            if asset_id is None:
                cursor = db.execute(
                    """
                    INSERT INTO assets
                        (source_id, source_ref, local_path, exists_locally)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        GONGKAO_SOURCE_ID,
                        source_ref,
                        local_path,
                        int((ROOT / local_path).is_file()),
                    ),
                )
                asset_id = int(cursor.lastrowid)
                asset_ids[local_path] = asset_id
            question_asset_links.add((question_id, asset_id))

        if review:
            db.execute(
                """
                INSERT INTO answer_reviews
                    (source_id, source_question_key, official_answer,
                     verified_answer, status, reasoning, supplement, source_url,
                     source_name, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    GONGKAO_SOURCE_ID,
                    qid,
                    review["official_answer"],
                    review["verified_answer"],
                    review["status"],
                    review["reasoning"],
                    review["supplement"],
                    review["source_url"],
                    review["source_name"],
                    review["verified_at"],
                ),
            )

    paper_link_count = 0
    for row in source.execute("SELECT * FROM appearances ORDER BY id"):
        qid = str(row["qid"])
        question_id = question_ids.get(qid)
        if question_id is None:
            continue
        paper_id = ensure_paper(
            title=str(row["paper"]),
            region=row["region"],
            year=parse_year(row["year"], str(row["paper"])),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO question_papers
                (question_id, paper_id, sequence, source_path)
            VALUES (?, ?, ?, ?)
            """,
            (question_id, paper_id, row["seq"], row["path"]),
        )
        paper_link_count += 1

    # A small number of questions have metadata but no appearance row. Keep
    # them linked through their primary paper so no question becomes orphaned.
    for qid, question_id in question_ids.items():
        db.execute(
            """
            INSERT OR IGNORE INTO question_papers
                (question_id, paper_id, sequence, source_path)
            VALUES (?, ?, NULL, NULL)
            """,
            (question_id, primary_papers[qid]),
        )

    db.executemany(
        "INSERT INTO question_assets(question_id, asset_id) VALUES (?, ?)",
        sorted(question_asset_links),
    )
    source.close()

    return {
        "questions": len(question_ids),
        "papers": len(paper_ids),
        "question_paper_links": paper_link_count,
        "assets": len(asset_ids),
        "answer_reviews": len(reviews),
    }


def import_errrc_shenlun(db: sqlite3.Connection) -> dict[str, int]:
    tiku_path = ERRRC_DIR / "tiku.db"
    materials_path = ERRRC_DIR / "materials.db"
    if not tiku_path.exists() or not materials_path.exists():
        raise FileNotFoundError("ERRRC tiku.db or materials.db is missing")

    insert_source(
        db,
        ERRRC_SOURCE_ID,
        "https://github.com/ERRRC/kaogong-shuati",
        "12cf8e73be8fd18f615782e7a5748b0ca7f39ce2",
        "MIT code; question data is marked for personal study only.",
    )

    source = sqlite3.connect(f"file:{tiku_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute(
        "ATTACH DATABASE ? AS material_db",
        (f"file:{materials_path.as_posix()}?mode=ro",),
    )

    paper_ids: dict[int, int] = {}
    question_ids: dict[int, int] = {}
    imported_shenlun = "公务员·申论"

    papers = source.execute(
        "SELECT * FROM papers WHERE subjectName = ? ORDER BY id",
        (imported_shenlun,),
    )
    for row in papers:
        key = f"paper:{row['id']}"
        metadata = {
            "source_subject": row["subject"],
            "source_subject_name": row["subjectName"],
            "source_category": row["category"],
            "chapters": parse_json(row["chapters"], []),
            "source_crawled_at": row["crawledAt"],
        }
        cursor = db.execute(
            """
            INSERT INTO papers
                (source_id, source_paper_key, subject, region, year, title,
                 duration_seconds, declared_question_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ERRRC_SOURCE_ID,
                key,
                SHENLUN,
                row["category"],
                parse_year(None, row["name"]),
                unicodedata.normalize("NFKC", row["name"]),
                row["time"],
                row["questionCount"],
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        paper_ids[int(row["id"])] = int(cursor.lastrowid)

    questions = source.execute(
        """
        SELECT q.*, p.name AS paper_name, p.category AS paper_category,
               p.subjectName AS subject_name
        FROM questions q
        JOIN papers p ON p.id = q.paperId
        WHERE p.subjectName = ?
        ORDER BY q.id
        """,
        (imported_shenlun,),
    )
    for row in questions:
        source_question_id = int(row["id"])
        paper_id = paper_ids[int(row["paperId"])]
        options = parse_errrc_options(row["options"])
        metadata = {
            "source_question_id": row["questionId"],
            "source_paper_id": row["paperId"],
            "source_type_id": row["type"],
            "source_option_type": row["optionType"],
            "source_difficulty": row["difficulty"],
            "source": row["source"],
            "has_video": bool(row["hasVideo"]),
        }
        cursor = db.execute(
            """
            INSERT INTO questions
                (source_id, source_question_key, primary_paper_id, subject,
                 module, category, knowledge_point, stem_html, stem_text,
                 passage_html, answer_text, answer_index, analysis_text,
                 analysis_html, question_type, difficulty, content_hash,
                 has_image, quality_status, license_status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?)
            """,
            (
                ERRRC_SOURCE_ID,
                str(source_question_id),
                paper_id,
                SHENLUN,
                row["chapter"],
                None,
                None,
                clean_material_refs(row["contentHtml"])
                or clean_material_refs(row["content"]),
                clean_material_refs(row["content"]) or html_to_text(row["contentHtml"]),
                clean_material_refs(row["material"]),
                row["answer"],
                row["answerIndex"],
                clean_material_refs(row["analysis"]),
                clean_material_refs(row["analysisHtml"]),
                "subjective",
                row["difficulty"],
                content_hash(
                    row["contentHtml"] or row["content"] or "",
                    row["paper_name"] or "",
                    "\n".join(f"{o['key']}:{o['html']}" for o in options),
                ),
                int("<img" in ((row["contentHtml"] or "") + (row["analysisHtml"] or "")).lower()),
                "unverified",
                "personal_only",
                json.dumps(metadata, ensure_ascii=False, default=str),
            ),
        )
        question_id = int(cursor.lastrowid)
        question_ids[source_question_id] = question_id
        db.execute(
            """
            INSERT INTO question_papers
                (question_id, paper_id, sequence, source_path)
            VALUES (?, ?, ?, ?)
            """,
            (question_id, paper_id, row["questionId"], None),
        )
        db.executemany(
            """
            INSERT INTO question_options
                (question_id, option_key, label, html, is_answer, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    question_id,
                    option["key"],
                    option["label"],
                    option["html"],
                    int(option["is_answer"]),
                    option["sort_order"],
                )
                for option in options
            ],
        )

    material_rows = source.execute(
        """
        SELECT m.*
        FROM material_db.materials m
        JOIN papers p ON p.id = m.paperId
        WHERE p.subjectName = ?
        ORDER BY m.paperId, m.idx
        """,
        (imported_shenlun,),
    )
    material_count = 0
    for row in material_rows:
        paper_id = paper_ids.get(int(row["paperId"]))
        if paper_id is None:
            continue
        db.execute(
            """
            INSERT INTO materials
                (paper_id, source_material_key, material_index, title, text,
                 metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                f"{row['paperId']}:{row['idx']}",
                row["idx"],
                unicodedata.normalize("NFKC", row["title"] or ""),
                unicodedata.normalize("NFKC", row["text"] or ""),
                json.dumps({"source_pdf": row["pdf"]}, ensure_ascii=False),
            ),
        )
        material_count += 1

    source.close()
    return {
        "questions": len(question_ids),
        "papers": len(paper_ids),
        "materials": material_count,
    }


def collect_stats(db: sqlite3.Connection) -> dict[str, Any]:
    return {
        "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        "papers": db.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
        "questions": db.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
        "xingce_questions": db.execute(
            "SELECT COUNT(*) FROM questions WHERE subject = ?", (XINGCE,)
        ).fetchone()[0],
        "shenlun_questions": db.execute(
            "SELECT COUNT(*) FROM questions WHERE subject = ?", (SHENLUN,)
        ).fetchone()[0],
        "question_paper_links": db.execute(
            "SELECT COUNT(*) FROM question_papers"
        ).fetchone()[0],
        "options": db.execute("SELECT COUNT(*) FROM question_options").fetchone()[0],
        "materials": db.execute("SELECT COUNT(*) FROM materials").fetchone()[0],
        "assets": db.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
        "missing_assets": db.execute(
            "SELECT COUNT(*) FROM assets WHERE exists_locally = 0"
        ).fetchone()[0],
        "questions_missing_answer": db.execute(
            "SELECT COUNT(*) FROM questions WHERE answer_text IS NULL OR TRIM(answer_text) = ''"
        ).fetchone()[0],
        "duplicate_content_hashes": db.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT content_hash
                FROM questions
                GROUP BY content_hash
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0],
        "paper_orphans": db.execute(
            """
            SELECT COUNT(*)
            FROM questions q
            LEFT JOIN question_papers qp ON qp.question_id = q.id
            WHERE qp.question_id IS NULL
            """
        ).fetchone()[0],
        "integrity": db.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_violations": len(
            db.execute("PRAGMA foreign_key_check").fetchall()
        ),
    }


def build(output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_suffix(output.suffix + ".tmp")
    if temp_output.exists():
        temp_output.unlink()

    db = sqlite3.connect(temp_output)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    try:
        create_schema(db)
        gongkao_report = import_gongkao(db)
        errrc_report = import_errrc_shenlun(db)
        db.commit()
        stats = collect_stats(db)
        report = {
            "generated_at": utc_now(),
            "sources": {
                GONGKAO_SOURCE_ID: gongkao_report,
                ERRRC_SOURCE_ID: errrc_report,
            },
            "output": str(output),
            **stats,
        }
        db.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            ("build_report", json.dumps(report, ensure_ascii=False)),
        )
        db.commit()
    finally:
        db.close()

    os.replace(temp_output, output)
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build(args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
