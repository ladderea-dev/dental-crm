-- AirZen notification outbox rollback
-- Idempotent. Removes only outbox objects and does not grant/reopen browser access.

begin;

do $$
declare
  v_fn text;
  v_functions text[] := array[
    'public.cancel_appointment_with_notification_outbox(text,jsonb,jsonb,text)',
    'public.reschedule_appointment_with_notification_outbox(text,jsonb,jsonb,jsonb,text)',
    'public.create_appointment_with_notification_outbox(jsonb,jsonb,jsonb,text)',
    'public.insert_notification_outbox_intent(text,text,text,text,text,text,jsonb,text,text,text,timestamptz)',
    'public.mark_notification_outbox_failed(uuid,uuid,text,text,timestamptz)',
    'public.mark_notification_outbox_sent(uuid,uuid,text)',
    'public.claim_notification_outbox(integer,integer)',
    'public.airzen_notification_dedupe_key(text,text,text,text)',
    'public.touch_notification_outbox_updated_at()'
  ];
begin
  if to_regclass('public.notification_outbox') is not null then
    revoke all on public.notification_outbox from public, anon, authenticated;
    revoke all on public.notification_outbox from service_role;
    drop trigger if exists trg_notification_outbox_updated_at on public.notification_outbox;
  end if;

  foreach v_fn in array v_functions loop
    if to_regprocedure(v_fn) is not null then
      execute format('revoke all on function %s from public, anon, authenticated, service_role', v_fn);
      execute format('drop function %s', v_fn);
    end if;
  end loop;

  if to_regclass('public.notification_outbox') is not null then
    drop table public.notification_outbox;
  end if;
end $$;

commit;
