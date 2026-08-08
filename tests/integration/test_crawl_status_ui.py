from datetime import UTC, date, datetime

from kipu_knowledge.adapters.db import models as m
from kipu_knowledge.domain import enums as e


def test_crawl_status_page_shows_runs_counts_and_errors(api_client, ingested_session):
    run = m.CrawlRun(
        started_at=datetime(2026, 8, 8, 13, 0, tzinfo=UTC),
        completed_at=datetime(2026, 8, 8, 13, 2, tzinfo=UTC),
        status="FAILED",
        crawler_version="test/1.0",
        parameters={"publication_date": date(2026, 8, 8).isoformat(), "series": "NL"},
        error_summary="fallo controlado <sin HTML>",
    )
    ingested_session.add(run)
    ingested_session.flush()
    ingested_session.add_all(
        [
            m.CrawlItem(
                crawl_run_id=run.id,
                source_series="NL",
                publication_code="2550000-1",
                relevance=e.Relevance.RELEVANT,
                status=e.CrawlItemStatus.INGESTED,
                events_extracted=1,
                outcome_detail="eventos=1",
            ),
            m.CrawlItem(
                crawl_run_id=run.id,
                source_series="NL",
                publication_code="2550000-2",
                relevance=e.Relevance.RELEVANT,
                status=e.CrawlItemStatus.RETRY_PENDING,
                events_extracted=0,
                last_error="timeout",
            ),
        ]
    )
    ingested_session.commit()

    response = api_client.get("/review/crawls")

    assert response.status_code == 200
    assert "2026-08-08" in response.text
    assert "2550000-1" in response.text
    assert "2550000-2" in response.text
    assert "fallo controlado &lt;sin HTML&gt;" in response.text
    assert "1</strong> pendientes" in response.text
    assert "1 dispositivo(s) relevante(s) no produjeron eventos" in response.text
