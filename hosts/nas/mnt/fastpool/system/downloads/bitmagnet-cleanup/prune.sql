-- Thresholds/dry_run are psql variables passed in from the container's environment
-- (see the bitmagnet-cleanup service in compose/docker-compose-downloads.yml).
create temporary table prune_candidates as
with latest_source as (
  select distinct on (info_hash)
    info_hash, updated_at as last_seen, seeders
  from torrents_torrent_sources
  order by info_hash, updated_at desc
),
classified as (
  select distinct info_hash from torrent_contents
)
select ls.info_hash
from latest_source ls
left join classified c on c.info_hash = ls.info_hash
where
  (c.info_hash is null
     and (ls.last_seen < now() - :'unclassified_age'::interval
          or coalesce(ls.seeders, 0) = 0))
  or (c.info_hash is not null
     and ls.last_seen < now() - :'classified_age'::interval
     and coalesce(ls.seeders, 0) = 0);

\if :dry_run
  select count(*) as would_delete from prune_candidates;
\else
  delete from torrents t using prune_candidates p where t.info_hash = p.info_hash;
\endif

drop table prune_candidates;
