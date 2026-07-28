begin;

do $$
begin
  if not exists (
    select 1
    from pg_roles
    where rolname='brains_app'
      and rolcanlogin
      and not rolsuper
      and not rolbypassrls
  ) then
    raise exception
      'required login role brains_app is missing or violates the usage boundary';
  end if;
end
$$;

grant lifeswitch_usage_writer_v1 to brains_app;
grant lifeswitch_usage_admin_v1 to brains_app;

commit;
