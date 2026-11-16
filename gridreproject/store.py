import sqlite3
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
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
