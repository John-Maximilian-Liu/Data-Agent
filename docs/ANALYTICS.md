# Analytics definitions

| Metric | Definition |
|--------|------------|
| session_volume | Unique production sessions by project-local date; dimensions by state |
| completion_rate | technical_completed true / known technical outcomes; publishes coverage |
| success_rate | business_success true / known terminal business outcomes |
| ratings | Latest effective feedback per rater+subject+scale; histogram, mean, coverage; retractions excluded |
| dau | Distinct production user pseudonyms that day |
| mau | Distinct production user pseudonyms in rolling 30 local days ending that day |
| tokens | Sum input/output/cache by usage_class |
| cost | Sum cost_usd; tracks estimated and rate_unknown counts |
| top_kb / top_tools / top_skills | Rank by calls, tie-break by component_id; sessions + reliability |
| latency | P50/P95 of run wall_ms (end-to-end; not nested spans) |

Authorized agent filters apply **before** distinct counts, aggregations, and percentiles. Cross-agent DAU/MAU re-deduplicate.
