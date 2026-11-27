-- QCoach v3.0 Phase 1 MVP Schema (Supabase / PostgreSQL)
-- Run this in Supabase SQL Editor when ready to enable user accounts, streaks & cloud song packs.

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  skill_level text default 'beginner' check (skill_level in ('beginner','intermediate','expert')),
  daily_streak int default 0,
  last_practice_date date,
  created_at timestamptz default now()
);

create table if not exists public.song_progress (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.profiles(id) on delete cascade,
  song_norm_key text not null,
  best_listen_score int default 0,
  stars int default 0 check (stars between 0 and 3),
  loops_completed int default 0,
  max_clean_bpm int default 72,
  updated_at timestamptz default now(),
  unique (user_id, song_norm_key)
);

create table if not exists public.community_song_submissions (
  id uuid primary key default gen_random_uuid(),
  submitted_by uuid references public.profiles(id) on delete set null,
  title text not null,
  artist text,
  language text default 'en',
  youtube_url text,
  payload jsonb not null,
  status text default 'pending' check (status in ('pending','verified','rejected')),
  created_at timestamptz default now()
);

alter table public.profiles enable row level security;
alter table public.song_progress enable row level security;
alter table public.community_song_submissions enable row level security;

create policy "Users manage own profile" on public.profiles
  for all using (auth.uid() = id) with check (auth.uid() = id);

create policy "Users manage own progress" on public.song_progress
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create policy "Users submit and read own submissions" on public.community_song_submissions
  for all using (auth.uid() = submitted_by) with check (auth.uid() = submitted_by);
