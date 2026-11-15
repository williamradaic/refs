"""The brakes on automatic reading (budget.py): monthly limit, large batches, Origin."""

import pytest
from fastapi.testclient import TestClient

from refs import budget, config, index, ingest, pipeline, records
from refs.watch import Watcher
from refs.web.app import create_app

COMPLETE = b"%PDF-1.4 test\n%%EOF"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    for folder in ["phd/papers", "papers-dump"]:
        (root / folder).mkdir(parents=True)
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    # Reading is faked: each PDF "costs" $0.002 and is recorded as read.
    reads = []

    def fake_process(cfg, api, file_hash, rel, topics, previous=None):
        reads.append(rel)
        record = {"status": "extracted", "issues": [], "pending": [], "path": rel, "hash": file_hash,
                  "usage": {"input_tokens": 20_000, "output_tokens": 0}, "reused_reading": False}
        records.write_json(records.reads_dir(cfg) / f"{file_hash}.json", record)   # read once
        return record

    monkeypatch.setattr(pipeline, "process_file", fake_process)
    monkeypatch.setattr("refs.llm.client", lambda: object())
    monkeypatch.setattr("refs.works.sync", lambda cfg: {})
    monkeypatch.setattr("refs.works.load_all", lambda cfg: {})
    lib.root, lib.reads = root, reads
    return lib


def write(lib, folder: str, n: int) -> None:
    for i in range(n):         # different content in every file: no copies
        (lib.root / folder / f"p{i}.pdf").write_bytes(COMPLETE + f"{folder}{i}".encode())


def test_ledger_and_month(lib):
    cfg = config.load()
    conn = index.connect(cfg)
    budget.record(conn, 0.25)
    budget.record(conn, 0.0)                  # nothing to enter
    assert budget.spent_this_month(conn) == pytest.approx(0.25)
    assert budget.left_this_month(cfg, conn) == pytest.approx(cfg.read_monthly_cap - 0.25)


def test_ingest_stops_at_the_budget(lib):
    write(lib, "phd/papers", 5)
    cfg = config.load()
    conn = index.connect(cfg)
    from refs import scan
    scan.scan(cfg, conn)
    pending = ingest.unread_files(cfg, conn)
    results = ingest.ingest(cfg, conn, pending, budget_left=0.0045)   # room for two at $0.002
    assert len(lib.reads) == 2
    assert sum("reading limit" in (r.skipped or "") for r in results) == 3
    assert budget.spent_this_month(conn) == pytest.approx(0.004)


def test_a_large_batch_waits_for_your_ok(lib):
    write(lib, "phd/papers", budget.BULK + 5)
    write(lib, "papers-dump", 2)
    watcher = Watcher(config.load(), notifier=None)
    watcher.run_pass()
    # The two you added are read; the batch is held, with its size and cost.
    assert sorted(lib.reads) == ["papers-dump/p0.pdf", "papers-dump/p1.pdf"]
    assert watcher.status()["held"] == {"pdfs": budget.BULK + 5, "dollars": budget.estimate(budget.BULK + 5)}
    watcher.approve()
    watcher.run_pass()
    assert len(lib.reads) == budget.BULK + 7 and watcher.status()["held"] is None


def test_changes_only_from_refs_own_page(lib):
    client = TestClient(create_app())
    ok = {"X-Refs": "1", "Origin": "http://refs.localhost:8377"}
    assert client.post("/api/scan", headers=ok).status_code == 200
    assert client.post("/api/scan", headers={**ok, "Origin": "https://example.com"}).status_code == 403
    assert client.post("/api/scan", headers={**ok, "Origin": "null"}).status_code == 403
    assert client.post("/api/scan", headers={"Origin": "http://refs.localhost:8377"}).status_code == 403
