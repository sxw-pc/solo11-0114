from pathlib import Path

import pytest

from gridreproject import vector

FIXTURE=Path(__file__).resolve().parents[1]/'fixtures'/'vector-pair.json'


def test_vector_demo_registers_replayable_pair(tmp_path):
    db=tmp_path/'ledger.db'
    row=vector.demo(db,tmp_path/'input',FIXTURE)
    assert row['id']=='synthetic-current-01'
    assert row['basis']=='source_grid'
    assert row['intact'] is True
    assert vector.inspect(db,row['id'])==row
    assert vector.register(db,row['id'],row['u_path'],row['v_path'],row['basis'])['id']==row['id']


def test_vector_pair_grid_and_bytes_are_frozen(tmp_path):
    db=tmp_path/'ledger.db'
    row=vector.demo(db,tmp_path/'input',FIXTURE)
    with pytest.raises(ValueError,match='vector_components_must_differ'):
        vector.register(db,'bad',row['u_path'],row['u_path'],'source_grid')
    Path(row['u_path']).write_bytes(b'changed')
    assert vector.inspect(db,row['id'])['intact'] is False
