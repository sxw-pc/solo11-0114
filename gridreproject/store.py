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
CREATE TABLE IF NOT EXISTS vector_contract(id INTEGER PRIMARY KEY,hash TEXT NOT NULL UNIQUE,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vector_run(id INTEGER PRIMARY KEY,vector_id TEXT NOT NULL,basis TEXT NOT NULL,contract_id INTEGER NOT NULL REFERENCES vector_contract(id),status TEXT NOT NULL,started TEXT DEFAULT CURRENT_TIMESTAMP,ended TEXT);
CREATE TABLE IF NOT EXISTS vector_cell(run_id INTEGER NOT NULL REFERENCES vector_run(id),row INTEGER NOT NULL,col INTEGER NOT NULL,u_value REAL,v_value REAL,magnitude REAL,east_component REAL,coverage REAL NOT NULL,state TEXT NOT NULL,source_row REAL,source_col REAL,contribution_json TEXT NOT NULL,basis_json TEXT,PRIMARY KEY(run_id,row,col));
CREATE TABLE IF NOT EXISTS vector_output(run_id INTEGER PRIMARY KEY REFERENCES vector_run(id),path TEXT NOT NULL,hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vector_issue(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES vector_run(id),row INTEGER,col INTEGER,code TEXT NOT NULL,detail TEXT);
CREATE TABLE IF NOT EXISTS east_decision(run_id INTEGER NOT NULL REFERENCES vector_run(id),row INTEGER NOT NULL,col INTEGER NOT NULL,status TEXT NOT NULL,east_component REAL,coverage REAL NOT NULL,threshold REAL NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY(run_id,row,col));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
