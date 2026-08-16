create table if not exists historical_match_context (
    historical_match_id bigint primary key references historical_matches(id) on delete cascade,
    season text not null,
    match_date timestamptz not null,
    opponent text not null,

    round_no integer,
    total_rounds integer,
    matches_remaining integer,
    season_progress numeric,

    lech_position_before integer,
    opponent_position_before integer,
    position_gap integer,

    lech_points_before integer,
    opponent_points_before integer,
    points_gap integer,
    points_to_leader integer,

    lech_matches_played_before integer,
    opponent_matches_played_before integer,
    lech_ppg_before numeric,
    opponent_ppg_before numeric,

    lech_last5_points integer,
    opponent_last5_points integer,
    lech_last5_goal_diff integer,
    opponent_last5_goal_diff integer,

    source text not null default 'worldfootball.net',
    source_url text,
    generated_at timestamptz not null default now()
);

create index if not exists idx_historical_match_context_season
    on historical_match_context(season);

create index if not exists idx_historical_match_context_match_date
    on historical_match_context(match_date);
