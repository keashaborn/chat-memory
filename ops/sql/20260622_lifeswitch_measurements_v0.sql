create table if not exists lifeswitch_measurement_entries (
  measurement_entry_id uuid primary key default gen_random_uuid(),

  owner_user_id text not null,

  local_date date not null,
  measured_at timestamptz null,

  weight_value numeric null,
  weight_unit text not null default 'lb',

  waist_value numeric null,
  abdomen_value numeric null,
  neck_value numeric null,
  chest_value numeric null,
  hip_value numeric null,

  left_arm_value numeric null,
  right_arm_value numeric null,
  left_thigh_value numeric null,
  right_thigh_value numeric null,
  left_calf_value numeric null,
  right_calf_value numeric null,

  body_fat_percent numeric null,
  body_fat_method text null,

  measurement_unit text not null default 'in',
  source text not null default 'manual',

  notes text null,
  skinfolds_json jsonb null,
  scan_json jsonb null,

  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_lifeswitch_measurement_entries_owner_date
  on lifeswitch_measurement_entries(owner_user_id, local_date desc, created_at desc);

create index if not exists idx_lifeswitch_measurement_entries_owner_active
  on lifeswitch_measurement_entries(owner_user_id, is_active);

create unique index if not exists ux_lifeswitch_measurement_entries_owner_date_source_active
  on lifeswitch_measurement_entries(owner_user_id, local_date, source)
  where is_active = true;
