-- Ad-hoc duckdb views over the exported Parquet partitions.
-- Usage: duckdb -c ".read deploy/duckdb_views.sql"  (replace {dir} with your parquet root)

CREATE OR REPLACE VIEW snapshots_features AS
    SELECT * FROM read_parquet('{dir}/dt=*/snapshots_features.parquet', union_by_name=true);

CREATE OR REPLACE VIEW resolutions AS
    SELECT * FROM read_parquet('{dir}/dt=*/resolutions.parquet', union_by_name=true);

-- Accuracy of the dominant side by label over the whole export.
CREATE OR REPLACE VIEW accuracy_by_label AS
    SELECT label,
           count(*)                                    AS n,
           avg(CASE WHEN was_correct_mid THEN 1 ELSE 0 END) AS win_rate,
           avg(dominant_mid)                           AS avg_dominant_mid
    FROM snapshots_features
    WHERE was_correct_mid IS NOT NULL
    GROUP BY label
    ORDER BY label;
