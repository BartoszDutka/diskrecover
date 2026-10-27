import time

from diskrecover.core.telemetry import ScanTelemetry, create_scanner_logger


def test_telemetry_counters_and_heartbeat(tmp_path):
    seen = []
    telemetry = ScanTelemetry(total_bytes=1000, on_update=seen.append)
    telemetry.update(stage="Głębokie skanowanie", cluster=4)
    telemetry.increment("bytes_read", 512)
    telemetry.increment("clusters_checked")

    snapshot = telemetry.snapshot()
    assert snapshot.bytes_read == 512
    assert snapshot.clusters_checked == 1
    assert snapshot.cluster == 4
    assert seen
    assert telemetry.heartbeat_age() < 1


def test_scanner_log_is_written_and_flushed(tmp_path):
    logger = create_scanner_logger(tmp_path)
    logger.info("Rozpoczęcie etapu testowego")
    for handler in logger.handlers:
        handler.flush()

    log_path = tmp_path / "Raport" / "scanner_debug.log"
    assert log_path.is_file()
    assert "Rozpoczęcie etapu testowego" in log_path.read_text(encoding="utf-8")
