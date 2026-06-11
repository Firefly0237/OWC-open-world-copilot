from owcopilot.ab import ABConfig, render_ab_markdown, run_ab_benchmark, write_ab_report


def test_ab_benchmark_offline_candidate_beats_baseline():
    report = run_ab_benchmark(ABConfig(name="test_ab"))

    assert report.candidate.total_cost_usd < report.baseline.total_cost_usd
    assert (
        report.candidate.first_pass_consistency_rate >= report.baseline.first_pass_consistency_rate
    )
    assert report.delta["total_cost_usd"]["reduction_pct"] > 0


def test_ab_report_writes_markdown_and_json(tmp_path):
    report = run_ab_benchmark(ABConfig(name="test_ab"))
    md_path, json_path = write_ab_report(report, tmp_path)

    assert md_path.exists()
    assert json_path.exists()
    assert "offline deterministic A/B simulation" in md_path.read_text(encoding="utf-8")
    assert "total_cost_usd" in render_ab_markdown(report)
