import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,width INT,height INT,crs TEXT,nodata REAL,unit TEXT);
CREATE TABLE IF NOT EXISTS contract(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(
    id INTEGER PRIMARY KEY,
    source_id INT REFERENCES source(id),
    contract_id INT REFERENCES contract(id),
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    started TEXT DEFAULT CURRENT_TIMESTAMP,
    ended TEXT,
    error_model_version TEXT,
    error_model TEXT,
    error_hash TEXT,
    error_grid TEXT,
    error_grid_relation TEXT
);
CREATE TABLE IF NOT EXISTS cell(
    run_id INT REFERENCES run(id),
    row INT,
    col INT,
    value REAL,
    coverage REAL,
    source_row REAL,
    source_col REAL,
    weight REAL,
    value_lower REAL,
    value_upper REAL,
    error_known INT,
    weight_evidence TEXT,
    PRIMARY KEY(run_id,row,col)
);
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,run_id INT REFERENCES run(id),row INT,col INT,code TEXT NOT NULL,detail TEXT);
CREATE TABLE IF NOT EXISTS output(run_id INT PRIMARY KEY REFERENCES run(id),path TEXT NOT NULL,hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zone(
    id TEXT PRIMARY KEY,
    cell_ids TEXT NOT NULL,
    min_coverage REAL NOT NULL,
    limit_value REAL NOT NULL,
    strategy TEXT,
    strategy_version TEXT
);
CREATE TABLE IF NOT EXISTS zone_decision(
    run_id INT NOT NULL REFERENCES run(id),
    zone_id TEXT NOT NULL REFERENCES zone(id),
    status TEXT NOT NULL,
    measured REAL,
    coverage REAL NOT NULL,
    evidence TEXT NOT NULL,
    strategy TEXT,
    strategy_version TEXT,
    lower_bound REAL,
    upper_bound REAL,
    model_version TEXT,
    model_hash TEXT,
    PRIMARY KEY(run_id,zone_id)
);
CREATE TABLE IF NOT EXISTS release_request(
    id TEXT PRIMARY KEY,
    run_id INT NOT NULL REFERENCES run(id),
    zone_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    policy TEXT,
    lower_bound REAL,
    upper_bound REAL
);
CREATE TABLE IF NOT EXISTS trend(id TEXT PRIMARY KEY,baseline_run INT NOT NULL REFERENCES run(id),target_run INT NOT NULL REFERENCES run(id),zone_id TEXT NOT NULL REFERENCES zone(id),max_mean_increase REAL NOT NULL,max_cell_increase REAL NOT NULL,max_hotspot_fraction REAL NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trend_cell(trend_id TEXT NOT NULL REFERENCES trend(id),row INT NOT NULL,col INT NOT NULL,baseline REAL,target REAL,delta REAL,coverage REAL NOT NULL,hotspot INT NOT NULL,PRIMARY KEY(trend_id,row,col));
"""

# Columns added after the original ledger format. Existing databases are
# upgraded in place so old runs keep their original meaning (all new columns
# stay NULL for them).
_ADDED_COLUMNS = {
    "run": [
        ("error_model_version", "TEXT"),
        ("error_model", "TEXT"),
        ("error_hash", "TEXT"),
        ("error_grid", "TEXT"),
        ("error_grid_relation", "TEXT"),
    ],
    "cell": [
        ("value_lower", "REAL"),
        ("value_upper", "REAL"),
        ("error_known", "INT"),
        ("weight_evidence", "TEXT"),
    ],
    "zone": [
        ("strategy", "TEXT"),
        ("strategy_version", "TEXT"),
    ],
    "zone_decision": [
        ("strategy", "TEXT"),
        ("strategy_version", "TEXT"),
        ("lower_bound", "REAL"),
        ("upper_bound", "REAL"),
        ("model_version", "TEXT"),
        ("model_hash", "TEXT"),
    ],
    "release_request": [
        ("policy", "TEXT"),
        ("lower_bound", "REAL"),
        ("upper_bound", "REAL"),
    ],
}


def _upgrade(db):
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
        for name, decl in columns:
            if name not in existing:
                db.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {decl}')
    db.commit()


def connect(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _upgrade(db)
    return db
