import job_cli


def _rows(*types):
    # sqlite3.Row-like access via dicts is enough for _verify_filter.
    return [{"location_type": t} for t in types]


def test_verify_hidden_by_default():
    rows = _rows("remote", "verify", "onsite/hybrid", "verify")
    kept, hidden = job_cli._verify_filter(rows, show_all=False)
    assert hidden == 2
    assert all((r["location_type"] or "") != "verify" for r in kept)


def test_show_all_keeps_verify():
    rows = _rows("remote", "verify")
    kept, hidden = job_cli._verify_filter(rows, show_all=True)
    assert hidden == 0
    assert len(kept) == 2


def test_null_location_type_is_visible():
    # Rows ingested before the column existed have NULL/empty type -> visible.
    rows = [{"location_type": None}, {"location_type": ""}]
    kept, hidden = job_cli._verify_filter(rows, show_all=False)
    assert hidden == 0
    assert len(kept) == 2
