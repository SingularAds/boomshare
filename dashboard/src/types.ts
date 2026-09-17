/**
 * Mirrors the Pydantic models in `app/api/schemas.py`.
 *
 * Every field here exists as a column or a COUNT over rows. There is no
 * client-side estimation: if the API cannot answer something, the UI says so
 * rather than filling the gap with a plausible-looking number.
 */

export type CountByKey = { key: string; count: number };

export type Overview = {
  customers: number;
  customers_downloaded: number;
  customers_activated: number;
  customers_opted_out: number;

  customers_from_ads: number;
  customers_direct: number;
  leads_by_source: CountByKey[];

  links_sent: number;
  links_clicked: number;
  customers_with_link: number;
  /** People who opened their download link, each counted once. */
  customers_clicked: number;

  conversations: number;
  conversations_open: number;

  messages: number;
  messages_inbound: number;
  messages_outbound: number;
  messages_ai_generated: number;

  campaigns: number;
  unanswerable_by_number: CountByKey[];
  generated_at: string;
};

export type CustomerRow = {
  id: string;
  phone: string;
  full_name: string | null;
  created_at: string;
  /** When they first opened a download link. */
  clicked_at: string | null;
  downloaded_at: string | null;
  activated_at: string | null;
  opted_out_at: string | null;
  source: string;
  campaign_name: string | null;
  numbers: string[];
  stage: string | null;
  last_activity_at: string | null;
  messages: number;
};

export type CustomerPage = {
  rows: CustomerRow[];
  total: number;
  limit: number;
  offset: number;
};

export type ChatMessage = {
  id: string;
  direction: "inbound" | "outbound";
  message_type: string;
  status: string;
  content: string | null;
  ai_generated: boolean;
  sent_by: string | null;
  created_at: string;
  sent_at: string | null;
  delivered_at: string | null;
  read_at: string | null;
};

export type Thread = {
  id: string;
  status: string;
  handling_mode: string;
  sales_stage: string;
  phone_number_id: string;
  number_label: string;
  created_at: string;
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  messages: ChatMessage[];
  truncated: boolean;
};

export type DownloadLinkOut = {
  token: string;
  url: string;
  platform: string | null;
  sent_at: string | null;
  clicked_at: string | null;
  downloaded_at: string | null;
  activated_at: string | null;
  details: Record<string, unknown> | null;
};

export type CustomerDetail = {
  id: string;
  phone: string;
  full_name: string | null;
  email: string | null;
  locale: string | null;
  language: {
    country_code: string | null;
    country_name: string | null;
    language_code: string;
    language_name: string;
    source: "customer_preference" | "phone_country" | "fallback";
  };
  created_at: string;
  downloaded_at: string | null;
  activated_at: string | null;
  opted_out_at: string | null;
  source: string;
  campaign_name: string | null;
  numbers: string[];
  conversations: Thread[];
  links: DownloadLinkOut[];
};

export type Outcome = "" | "clicked" | "activated" | "downloaded" | "not_downloaded" | "opted_out";
