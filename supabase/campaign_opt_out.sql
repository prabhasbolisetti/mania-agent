ALTER TABLE public.leads
ADD COLUMN IF NOT EXISTS marketing_opt_out BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.leads.marketing_opt_out IS
'When true, the lead must be excluded from all marketing campaigns.';
