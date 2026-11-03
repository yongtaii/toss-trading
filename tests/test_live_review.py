"""Loss diagnosis preserves reported metrics and rejects unsafe downloaded artifacts."""

import io
import json
import tarfile
from decimal import Decimal as D

import pytest

from toss_trading.live.review import RESULT_FILES, diagnosis, extract_results, review


def archive(path, unsafe=None):
    with tarfile.open(path, "w:gz") as handle:
        for name in sorted(RESULT_FILES):
            data = b"{}"
            member = tarfile.TarInfo(name)
            if unsafe == "path" and name == "history.csv":
                member.name = "../.env"
            if unsafe == "link" and name == "history.csv":
                member.type, member.linkname = tarfile.SYMTYPE, "../.env"
                handle.addfile(member)
            else:
                member.size = len(data)
                handle.addfile(member, io.BytesIO(data))


def metrics():
    return {
        "total_return": "-0.02",
        "net_profit_after_fees": "-29.8",
        "win_rate": "0.35",
        "profit_factor": "0.64",
        "total_trades": 1,
        "mdd": "0.03",
        "trade_log": [
            {"symbol": "TQQQ", "opened": "same entry", "reason": "STOP", "pnl": "-10"},
            {"symbol": "TQQQ", "opened": "same entry", "reason": "STOP", "pnl": "-19.8"},
        ],
    }


@pytest.mark.parametrize("unsafe", ["path", "link"])
def test_collection_rejects_tar_paths_and_links_before_writing(tmp_path, unsafe):
    path = tmp_path / "results.tar.gz"
    archive(path, unsafe)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="unsafe"):
        extract_results(path, output)
    assert not output.exists()


def test_collection_does_not_overwrite_previous_evidence(tmp_path):
    path = tmp_path / "results.tar.gz"
    archive(path)
    output = tmp_path / "output"
    extract_results(path, output)
    assert {p.name for p in output.iterdir()} == RESULT_FILES
    with pytest.raises(FileExistsError):
        extract_results(path, output)


def test_diagnosis_does_not_count_partial_sell_fills_as_more_completed_trades():
    result = diagnosis(metrics())
    assert result["trades"] == 1 and result["return_percent"] == -2
    reason = result["by_exit_reason_sell_fills"]["STOP"]
    assert reason["sell_fills"] == 2 and reason["net_pnl"] == D("-29.8")


def test_review_records_losses_without_changing_parameters_or_enabling_live(tmp_path):
    summary = {
        "source": "broker_historical_1m",
        "requested_sessions": 60,
        "complete_sessions": 49,
        "excluded_sessions": {},
        "split_us_date": "2026-09-21",
        "initial_cash": "1490",
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    for name in ("training", "validation"):
        (tmp_path / f"{name}.json").write_text(json.dumps(metrics()))
    result = review(tmp_path)
    assert result["live_activation_performed"] is False and not result["parameter_search"]
    assert result["validation"]["net_profit_usd"] == "-29.8"
    assert (tmp_path / "loss-diagnosis.json").is_file()
