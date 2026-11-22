import sqlite3
from contextlib import contextmanager
from pathlib import Path
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,width INT,height INT,crs TEXT,nodata REAL,unit TEXT);
CREATE TABLE IF NOT EXISTS contract(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY,source_id INT REFERENCES source(id),contract_id INT REFERENCES contract(id),status TEXT NOT NULL,method TEXT NOT NULL,started TEXT DEFAULT CURRENT_TIMESTAMP,ended TEXT);
CREATE TABLE IF NOT EXISTS cell(run_id INT REFERENCES run(id),row INT,col INT,value REAL,coverage REAL,source_row REAL,source_col REAL,weight REAL,PRIMARY KEY(run_id,row,col));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,run_id INT REFERENCES run(id),row INT,col INT,code TEXT NOT NULL,detail TEXT);
CREATE TABLE IF NOT EXISTS output(run_id INT PRIMARY KEY REFERENCES run(id),path TEXT NOT NULL,hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zone(id TEXT PRIMARY KEY,cell_ids TEXT NOT NULL,min_coverage REAL NOT NULL,limit_value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS zone_decision(run_id INT NOT NULL REFERENCES run(id),zone_id TEXT NOT NULL REFERENCES zone(id),status TEXT NOT NULL,measured REAL,coverage REAL NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(run_id,zone_id));
CREATE TABLE IF NOT EXISTS release_request(id TEXT PRIMARY KEY,run_id INT NOT NULL REFERENCES run(id),zone_ids TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS trend(id TEXT PRIMARY KEY,baseline_run INT NOT NULL REFERENCES run(id),target_run INT NOT NULL REFERENCES run(id),zone_id TEXT NOT NULL REFERENCES zone(id),max_mean_increase REAL NOT NULL,max_cell_increase REAL NOT NULL,max_hotspot_fraction REAL NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trend_cell(trend_id TEXT NOT NULL REFERENCES trend(id),row INT NOT NULL,col INT NOT NULL,baseline REAL,target REAL,delta REAL,coverage REAL NOT NULL,hotspot INT NOT NULL,PRIMARY KEY(trend_id,row,col));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db

@contextmanager
def frozen_view(path):
    """Yield a connection holding a read-only snapshot of the whole ledger.

    ``BEGIN IMMEDIATE`` takes the write lock up front, and SQLite's default
    ``deferred``/``rollback journal`` isolation makes every read inside the
    transaction see one consistent version of every table (including
    source/contract/run/cell/issue/output). Old cell rows and a replaced
    output file therefore cannot be stitched together into a forged bundle.
    The lock is always released, even when the caller rolls back.
    """
    db=connect(path)
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
    finally:
        db.rollback()
        db.close()
