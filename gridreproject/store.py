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
CREATE TABLE IF NOT EXISTS trend(id TEXT PRIMARY KEY,baseline_run INT NOT NULL REFERENCES run(id),target_run INT NOT NULL REFERENCES run(id),zone_id TEXT NOT NULL REFERENCES zone(id),max_mean_increase REAL NOT NULL,max_cell_increase REAL NOT NULL,max_hotspot_fraction REAL NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trend_cell(trend_id TEXT NOT NULL REFERENCES trend(id),row INT NOT NULL,col INT NOT NULL,baseline REAL,target REAL,delta REAL,coverage REAL NOT NULL,hotspot INT NOT NULL,PRIMARY KEY(trend_id,row,col));
CREATE TABLE IF NOT EXISTS event_contract(id TEXT PRIMARY KEY,run_ids TEXT NOT NULL,zone_id TEXT NOT NULL REFERENCES zone(id),persistence TEXT NOT NULL,min_occurrences INT NOT NULL,window INT NOT NULL,max_cell_increase REAL NOT NULL,area_unit TEXT NOT NULL,cell_area REAL NOT NULL,evidence_digest TEXT,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES event_contract(id),event_key TEXT NOT NULL,status TEXT NOT NULL,first_transition INT NOT NULL,last_transition INT NOT NULL,deteriorations INT NOT NULL,max_consecutive INT NOT NULL,exposed_area REAL NOT NULL,peak_area REAL NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_snapshot(contract_id TEXT NOT NULL,event_key TEXT NOT NULL,seq INT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,taken TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(contract_id,event_key,seq));
CREATE TABLE IF NOT EXISTS event_cell(contract_id TEXT NOT NULL,transition INT NOT NULL,row INT NOT NULL,col INT NOT NULL,prev_value REAL,curr_value REAL,delta REAL,coverage REAL NOT NULL,valid INT NOT NULL,deteriorating INT NOT NULL,PRIMARY KEY(contract_id,transition,row,col));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
