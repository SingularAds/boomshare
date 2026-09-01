"""Initial schema: campaigns, ads, leads, customers, conversations,
messages, ai decisions, reminders, download links and the webhook inbox.

Revision ID: 0371510ee02e
Revises: 
Create Date: 2026-08-26 12:25:12.129488
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.db import JsonB

revision: str = '0371510ee02e'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('campaigns',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meta_campaign_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('objective', sa.String(length=64), nullable=True),
    sa.Column('details', JsonB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_campaigns')),
    sa.UniqueConstraint('meta_campaign_id', name=op.f('uq_campaigns_meta_campaign_id'))
    )
    op.create_index(op.f('ix_campaigns_created_at'), 'campaigns', ['created_at'], unique=False)
    op.create_table('customers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=False),
    sa.Column('wa_id', sa.String(length=32), nullable=True),
    sa.Column('full_name', sa.String(length=255), nullable=True),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('locale', sa.String(length=16), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('downloaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('opted_out_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attributes', JsonB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers')),
    sa.UniqueConstraint('phone', name=op.f('uq_customers_phone')),
    sa.UniqueConstraint('wa_id', name=op.f('uq_customers_wa_id'))
    )
    op.create_index(op.f('ix_customers_created_at'), 'customers', ['created_at'], unique=False)
    op.create_index(op.f('ix_customers_email'), 'customers', ['email'], unique=False)
    op.create_table('webhook_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('event_key', sa.String(length=255), nullable=False),
    sa.Column('event_type', sa.Enum('whatsapp_message', 'whatsapp_status', 'leadgen', 'unknown', name='webhook_event_type', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('pending', 'processing', 'processed', 'failed', 'ignored', name='webhook_status', native_enum=False), nullable=False),
    sa.Column('payload', JsonB, nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_webhook_events_attempts_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_events')),
    sa.UniqueConstraint('provider', 'event_key', name='uq_webhook_events_provider_event_key')
    )
    op.create_index('ix_webhook_events_status_received', 'webhook_events', ['status', 'received_at'], unique=False)
    op.create_table('ads',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meta_ad_id', sa.String(length=64), nullable=False),
    sa.Column('meta_adset_id', sa.String(length=64), nullable=True),
    sa.Column('campaign_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('meta_form_id', sa.String(length=64), nullable=True),
    sa.Column('details', JsonB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_ads_campaign_id_campaigns'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ads')),
    sa.UniqueConstraint('meta_ad_id', name=op.f('uq_ads_meta_ad_id'))
    )
    op.create_index(op.f('ix_ads_created_at'), 'ads', ['created_at'], unique=False)
    op.create_index(op.f('ix_ads_meta_adset_id'), 'ads', ['meta_adset_id'], unique=False)
    op.create_index(op.f('ix_ads_meta_form_id'), 'ads', ['meta_form_id'], unique=False)
    op.create_table('leads',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.Enum('click_to_whatsapp', 'lead_ad', 'manual', name='lead_source', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('new', 'contacted', 'responded', 'unreachable', 'converted', 'disqualified', name='lead_status', native_enum=False), nullable=False),
    sa.Column('meta_leadgen_id', sa.String(length=64), nullable=True),
    sa.Column('meta_form_id', sa.String(length=64), nullable=True),
    sa.Column('meta_page_id', sa.String(length=64), nullable=True),
    sa.Column('ctwa_clid', sa.String(length=128), nullable=True),
    sa.Column('campaign_id', sa.Uuid(), nullable=True),
    sa.Column('ad_id', sa.Uuid(), nullable=True),
    sa.Column('raw_payload', JsonB, nullable=True),
    sa.Column('field_data', JsonB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['ad_id'], ['ads.id'], name=op.f('fk_leads_ad_id_ads'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_leads_campaign_id_campaigns'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_leads_customer_id_customers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_leads')),
    sa.UniqueConstraint('meta_leadgen_id', name=op.f('uq_leads_meta_leadgen_id'))
    )
    op.create_index(op.f('ix_leads_ad_id'), 'leads', ['ad_id'], unique=False)
    op.create_index('ix_leads_campaign_created', 'leads', ['campaign_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_leads_created_at'), 'leads', ['created_at'], unique=False)
    op.create_index(op.f('ix_leads_ctwa_clid'), 'leads', ['ctwa_clid'], unique=False)
    op.create_index(op.f('ix_leads_customer_id'), 'leads', ['customer_id'], unique=False)
    op.create_index(op.f('ix_leads_meta_form_id'), 'leads', ['meta_form_id'], unique=False)
    op.create_table('conversations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('lead_id', sa.Uuid(), nullable=True),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('status', sa.Enum('open', 'closed', name='conversation_status', native_enum=False), nullable=False),
    sa.Column('handling_mode', sa.Enum('ai', 'human', 'paused', name='handling_mode', native_enum=False), nullable=False),
    sa.Column('sales_stage', sa.Enum('new', 'contacted', 'engaged', 'qualified', 'product_explained', 'objection_handling', 'download_suggested', 'link_sent', 'downloaded', 'activated', 'not_interested', 'human_handoff', 'closed', name='sales_stage', native_enum=False), nullable=False),
    sa.Column('stage_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_inbound_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_outbound_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_intent', sa.Enum('greeting', 'information_request', 'pricing_question', 'feature_question', 'comparison', 'objection', 'buying_intent', 'download_request', 'support_issue', 'human_request', 'not_interested', 'opt_out', 'small_talk', 'unclear', name='customer_intent', native_enum=False), nullable=True),
    sa.Column('handoff_reason', sa.Text(), nullable=True),
    sa.Column('handoff_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('assigned_agent', sa.String(length=255), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('context_notes', JsonB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_conversations_customer_id_customers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_conversations_lead_id_leads'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations'))
    )
    op.create_index(op.f('ix_conversations_created_at'), 'conversations', ['created_at'], unique=False)
    op.create_index(op.f('ix_conversations_customer_id'), 'conversations', ['customer_id'], unique=False)
    op.create_index('ix_conversations_stage_status', 'conversations', ['sales_stage', 'status'], unique=False)
    op.create_index('uq_conversations_open_per_customer', 'conversations', ['customer_id', 'channel'], unique=True, postgresql_where=sa.text("status = 'open'"), sqlite_where=sa.text("status = 'open'"))
    op.create_table('download_links',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('token', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('url', sa.String(length=1024), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('clicked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('downloaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('details', JsonB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_download_links_conversation_id_conversations'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_download_links_customer_id_customers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_download_links')),
    sa.UniqueConstraint('token', name=op.f('uq_download_links_token'))
    )
    op.create_index(op.f('ix_download_links_created_at'), 'download_links', ['created_at'], unique=False)
    op.create_index(op.f('ix_download_links_customer_id'), 'download_links', ['customer_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('direction', sa.Enum('inbound', 'outbound', name='message_direction', native_enum=False), nullable=False),
    sa.Column('message_type', sa.Enum('text', 'template', 'image', 'audio', 'video', 'document', 'sticker', 'location', 'contacts', 'interactive', 'button', 'reaction', 'system', 'unsupported', name='message_type', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('received', 'queued', 'sent', 'delivered', 'read', 'failed', name='message_status', native_enum=False), nullable=False),
    sa.Column('provider_message_id', sa.String(length=128), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('payload', JsonB, nullable=True),
    sa.Column('error', JsonB, nullable=True),
    sa.Column('ai_generated', sa.Boolean(), nullable=False),
    sa.Column('sent_by', sa.String(length=255), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_messages_conversation_id_conversations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('provider_message_id', name='uq_messages_provider_message_id')
    )
    op.create_index('ix_messages_conversation_created', 'messages', ['conversation_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_messages_created_at'), 'messages', ['created_at'], unique=False)
    op.create_table('reminders',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('customer_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('follow_up', 'no_reply_nudge', 'download_check', name='reminder_kind', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('pending', 'processing', 'sent', 'cancelled', 'failed', name='reminder_status', native_enum=False), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('payload', JsonB, nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_reminders_conversation_id_conversations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_reminders_customer_id_customers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reminders'))
    )
    op.create_index(op.f('ix_reminders_conversation_id'), 'reminders', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_reminders_created_at'), 'reminders', ['created_at'], unique=False)
    op.create_index('ix_reminders_due', 'reminders', ['status', 'due_at'], unique=False)
    op.create_table('ai_decisions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('inbound_message_id', sa.Uuid(), nullable=True),
    sa.Column('outbound_message_id', sa.Uuid(), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('intent', sa.Enum('greeting', 'information_request', 'pricing_question', 'feature_question', 'comparison', 'objection', 'buying_intent', 'download_request', 'support_issue', 'human_request', 'not_interested', 'opt_out', 'small_talk', 'unclear', name='customer_intent', native_enum=False), nullable=True),
    sa.Column('suggested_stage', sa.Enum('new', 'contacted', 'engaged', 'qualified', 'product_explained', 'objection_handling', 'download_suggested', 'link_sent', 'downloaded', 'activated', 'not_interested', 'human_handoff', 'closed', name='sales_stage', native_enum=False), nullable=True),
    sa.Column('applied_stage', sa.Enum('new', 'contacted', 'engaged', 'qualified', 'product_explained', 'objection_handling', 'download_suggested', 'link_sent', 'downloaded', 'activated', 'not_interested', 'human_handoff', 'closed', name='sales_stage', native_enum=False), nullable=True),
    sa.Column('requested_actions', JsonB, nullable=True),
    sa.Column('executed_actions', JsonB, nullable=True),
    sa.Column('rejected_reasons', JsonB, nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('raw_response', JsonB, nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_ai_decisions_conversation_id_conversations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inbound_message_id'], ['messages.id'], name=op.f('fk_ai_decisions_inbound_message_id_messages'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['outbound_message_id'], ['messages.id'], name=op.f('fk_ai_decisions_outbound_message_id_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_decisions'))
    )
    op.create_index(op.f('ix_ai_decisions_conversation_id'), 'ai_decisions', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_ai_decisions_created_at'), 'ai_decisions', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ai_decisions_created_at'), table_name='ai_decisions')
    op.drop_index(op.f('ix_ai_decisions_conversation_id'), table_name='ai_decisions')
    op.drop_table('ai_decisions')
    op.drop_index('ix_reminders_due', table_name='reminders')
    op.drop_index(op.f('ix_reminders_created_at'), table_name='reminders')
    op.drop_index(op.f('ix_reminders_conversation_id'), table_name='reminders')
    op.drop_table('reminders')
    op.drop_index(op.f('ix_messages_created_at'), table_name='messages')
    op.drop_index('ix_messages_conversation_created', table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_download_links_customer_id'), table_name='download_links')
    op.drop_index(op.f('ix_download_links_created_at'), table_name='download_links')
    op.drop_table('download_links')
    op.drop_index('uq_conversations_open_per_customer', table_name='conversations', postgresql_where=sa.text("status = 'open'"), sqlite_where=sa.text("status = 'open'"))
    op.drop_index('ix_conversations_stage_status', table_name='conversations')
    op.drop_index(op.f('ix_conversations_customer_id'), table_name='conversations')
    op.drop_index(op.f('ix_conversations_created_at'), table_name='conversations')
    op.drop_table('conversations')
    op.drop_index(op.f('ix_leads_meta_form_id'), table_name='leads')
    op.drop_index(op.f('ix_leads_customer_id'), table_name='leads')
    op.drop_index(op.f('ix_leads_ctwa_clid'), table_name='leads')
    op.drop_index(op.f('ix_leads_created_at'), table_name='leads')
    op.drop_index('ix_leads_campaign_created', table_name='leads')
    op.drop_index(op.f('ix_leads_ad_id'), table_name='leads')
    op.drop_table('leads')
    op.drop_index(op.f('ix_ads_meta_form_id'), table_name='ads')
    op.drop_index(op.f('ix_ads_meta_adset_id'), table_name='ads')
    op.drop_index(op.f('ix_ads_created_at'), table_name='ads')
    op.drop_table('ads')
    op.drop_index('ix_webhook_events_status_received', table_name='webhook_events')
    op.drop_table('webhook_events')
    op.drop_index(op.f('ix_customers_email'), table_name='customers')
    op.drop_index(op.f('ix_customers_created_at'), table_name='customers')
    op.drop_table('customers')
    op.drop_index(op.f('ix_campaigns_created_at'), table_name='campaigns')
    op.drop_table('campaigns')
