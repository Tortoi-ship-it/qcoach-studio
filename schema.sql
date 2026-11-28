-- QCoach Studio v4.12 Cloud Sync, Dynamic Admin Roles & Song Request Pipeline (Supabase / PostgreSQL)
-- Run this in Supabase SQL Editor. Safe to re-run on an existing project.

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  skill_level text default 'beginner' check (skill_level in ('beginner','intermediate','expert')),
  role text default 'user' check (role in ('user','admin')),
  subscription_tier text default 'free' check (subscription_tier in ('free','pro')),
  daily_streak int default 0,
  last_practice_date date,
  learning_state jsonb default '{}'::jsonb,
  created_at timestamptz default now()
);

alter table public.profiles add column if not exists role text default 'user';
alter table public.profiles add column if not exists subscription_tier text default 'free';
alter table public.profiles add column if not exists learning_state jsonb default '{}'::jsonb;

-- Helper function: checks if current logged-in user has role = 'admin' in public.profiles
create or replace function public.is_admin()
returns boolean
language sql
security definer
stable
as $$
  select exists (
    select 1 from public.profiles
    where id = auth.uid() and role = 'admin'
  );
$$;

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
  payload jsonb not null default '{}'::jsonb,
  status text default 'pending' check (status in ('pending','approved','published','rejected')),
  created_at timestamptz default now()
);

-- Allow new 'approved' and 'published' workflow states on existing tables
alter table public.community_song_submissions drop constraint if exists community_song_submissions_status_check;
alter table public.community_song_submissions
  add constraint community_song_submissions_status_check
  check (status in ('pending','approved','published','rejected'));

alter table public.profiles enable row level security;
alter table public.song_progress enable row level security;
alter table public.community_song_submissions enable row level security;

drop policy if exists "Users manage own profile" on public.profiles;
create policy "Users manage own profile" on public.profiles
  for all using (auth.uid() = id) with check (auth.uid() = id);

drop policy if exists "Users manage own progress" on public.song_progress;
create policy "Users manage own progress" on public.song_progress
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "Users submit and read own submissions" on public.community_song_submissions;
create policy "Users submit and read own submissions" on public.community_song_submissions
  for all using (auth.uid() = submitted_by) with check (auth.uid() = submitted_by);

drop policy if exists "Admins manage all song submissions" on public.community_song_submissions;
create policy "Admins manage all song submissions" on public.community_song_submissions
  for all using (public.is_admin()) with check (public.is_admin());

-- Bootstrap primary owner as admin (add more admins anytime in Table Editor -> profiles -> role = 'admin')
update public.profiles set role = 'admin' where lower(display_name) = 'joy.tuscano@gmail.com';
