"""Seed script to populate local database with realistic dashboard data.

Usage:
  docker exec -i boomshare-api-1 python - < scripts/seed_dashboard.py
  or
  python scripts/seed_dashboard.py (if running locally with DATABASE_URL set)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.core.db import session_scope
from app.domain import (
    ConversationStatus,
    CustomerIntent,
    HandlingMode,
    LeadSource,
    LeadStatus,
    MessageDirection,
    MessageStatus,
    MessageType,
    SalesStage,
)
from app.models import Ad, Campaign, Conversation, Customer, DownloadLink, Lead, Message


async def seed() -> None:
    now = datetime.now(timezone.utc)
    # Both of the numbers we answer on, so the dashboard's "WhatsApp number"
    # column has something to show. Taken from configuration rather than
    # hardcoded, so this keeps working when a number is added or replaced.
    numbers = list(get_settings().whatsapp_phone_number_ids) or ["1271448969390854"]

    async with session_scope() as session:
        print("Cleaning up old data...")
        # Clean in reverse FK order
        await session.execute(Message.__table__.delete())
        await session.execute(DownloadLink.__table__.delete())
        await session.execute(Conversation.__table__.delete())
        await session.execute(Lead.__table__.delete())
        await session.execute(Customer.__table__.delete())
        await session.execute(Ad.__table__.delete())
        await session.execute(Campaign.__table__.delete())

        print("Seeding Campaigns & Ads...")
        campaign_br = Campaign(
            id=uuid.uuid4(),
            meta_campaign_id="camp_br_summer_2026",
            name="Summer Promo 2026 - Brazil CTWA",
            objective="MESSAGES",
            details={"country": "BR", "budget": 1500},
            created_at=now - timedelta(days=14),
        )
        campaign_us = Campaign(
            id=uuid.uuid4(),
            meta_campaign_id="camp_us_launch_2026",
            name="Productivity Pro Launch - US/Global",
            objective="LEAD_GENERATION",
            details={"country": "US", "budget": 3000},
            created_at=now - timedelta(days=10),
        )
        campaign_es = Campaign(
            id=uuid.uuid4(),
            meta_campaign_id="camp_es_growth_2026",
            name="Hispanic Creator Outreach - CTWA",
            objective="MESSAGES",
            details={"country": "ES", "budget": 800},
            created_at=now - timedelta(days=7),
        )
        session.add_all([campaign_br, campaign_us, campaign_es])
        await session.flush()

        ad_br_1 = Ad(
            id=uuid.uuid4(),
            meta_ad_id="ad_br_video_demo_01",
            campaign_id=campaign_br.id,
            name="Boomshare 4K Screen Record Demo",
            details={"format": "video", "placement": "instagram_reels"},
            created_at=now - timedelta(days=14),
        )
        ad_br_2 = Ad(
            id=uuid.uuid4(),
            meta_ad_id="ad_br_carousel_features_02",
            campaign_id=campaign_br.id,
            name="Top 5 Features Carousel",
            details={"format": "carousel", "placement": "facebook_feed"},
            created_at=now - timedelta(days=12),
        )
        ad_us_1 = Ad(
            id=uuid.uuid4(),
            meta_ad_id="ad_us_leadform_pro_01",
            meta_form_id="form_us_signup_01",
            campaign_id=campaign_us.id,
            name="Lead Form - Free Pro Trial",
            details={"format": "lead_ad"},
            created_at=now - timedelta(days=10),
        )
        ad_es_1 = Ad(
            id=uuid.uuid4(),
            meta_ad_id="ad_es_ctwa_creator_01",
            campaign_id=campaign_es.id,
            name="Grabador de Pantalla 4K - Reels",
            details={"format": "video"},
            created_at=now - timedelta(days=7),
        )
        session.add_all([ad_br_1, ad_br_2, ad_us_1, ad_es_1])
        await session.flush()

        customers_data = [
            {
                "name": "Lucas Silva",
                "phone": "5511987654321",
                "email": "lucas.silva@example.com",
                "locale": "pt_BR",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_br,
                "ad": ad_br_1,
                "stage": SalesStage.ACTIVATED,
                "days_ago": 6,
                "downloaded": True,
                "activated": True,
                "messages": [
                    (MessageDirection.INBOUND, "Oi! Vi o anúncio no Insta sobre o gravador de tela. Funciona no Windows?"),
                    (MessageDirection.OUTBOUND, "Olá Lucas! Sim, o Boomshare é 100% compatível com Windows 10 e 11, gravando em até 4K 60fps com link instantâneo."),
                    (MessageDirection.INBOUND, "Que show! Consegue me mandar o link pra testar?"),
                    (MessageDirection.OUTBOUND, "Com certeza! Segue seu link direto para download gratuito:\nhttps://boomshare.ai/download?ref=link_lucas"),
                    (MessageDirection.INBOUND, "Baixei e já ativei a conta. Muito rápido mesmo! Valeu!"),
                ],
            },
            {
                "name": "Beatriz Santos",
                "phone": "5521998765432",
                "email": "beatriz.santos@gmail.com",
                "locale": "pt_BR",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_br,
                "ad": ad_br_2,
                "stage": SalesStage.DOWNLOADED,
                "days_ago": 5,
                "downloaded": True,
                "activated": False,
                "messages": [
                    (MessageDirection.INBOUND, "Olá, me cadastrei pelo formulário para experimentar o Boomshare."),
                    (MessageDirection.OUTBOUND, "Olá Beatriz! Seja bem-vinda ao Boomshare. Você costuma gravar tutoriais ou reuniões?"),
                    (MessageDirection.INBOUND, "Gravo reuniões com clientes e preciso compartilhar rápido."),
                    (MessageDirection.OUTBOUND, "Perfeito! O Boomshare gera o link na nuvem assim que você para a gravação. Baixe aqui:\nhttps://boomshare.ai/download?ref=link_beatriz"),
                ],
            },
            {
                "name": "Rodrigo Oliveira",
                "phone": "5531988887777",
                "email": "rodrigo.dev@outlook.com",
                "locale": "pt_BR",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_br,
                "ad": ad_br_1,
                "stage": SalesStage.LINK_SENT,
                "days_ago": 4,
                "downloaded": False,
                "link_clicked": True,
                "messages": [
                    (MessageDirection.INBOUND, "O app grava áudio do microfone e do sistema ao mesmo tempo?"),
                    (MessageDirection.OUTBOUND, "Sim Rodrigo! Grava áudio interno do sistema e seu microfone em faixas separadas ou mixadas."),
                    (MessageDirection.INBOUND, "Top, manda o link pra mim pfv."),
                    (MessageDirection.OUTBOUND, "Aqui está seu link de instalação: https://boomshare.ai/download?ref=link_rodrigo"),
                ],
            },
            {
                "name": "Camila Ferreira",
                "phone": "5541977776666",
                "email": "camila.f@empresa.com.br",
                "locale": "pt_BR",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_br,
                "ad": ad_br_2,
                "stage": SalesStage.DOWNLOAD_SUGGESTED,
                "days_ago": 3,
                "messages": [
                    (MessageDirection.INBOUND, "Quanto custa depois do período de teste?"),
                    (MessageDirection.OUTBOUND, "Camila, o plano Starter é gratuito para sempre com vídeos de até 5 min. O Pro custa R$ 29/mês para gravações ilimitadas em 4K. Gostaria de testar sem compromisso?"),
                    (MessageDirection.INBOUND, "Gostaria sim, como faço?"),
                ],
            },
            {
                "name": "Matheus Costa",
                "phone": "5519966665555",
                "email": "matheus.costa@tech.io",
                "locale": "pt_BR",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_br,
                "ad": ad_br_1,
                "stage": SalesStage.PRODUCT_EXPLAINED,
                "days_ago": 3,
                "messages": [
                    (MessageDirection.INBOUND, "O que o Boomshare tem de diferente do OBS?"),
                    (MessageDirection.OUTBOUND, "Ótima pergunta Matheus! Diferente do OBS que precisa de configuração complexa e exportação manual, o Boomshare grava com 1 clique e cria link compartilhável na nuvem na mesma hora."),
                ],
            },
            {
                "name": "Mariana Lima",
                "phone": "5511955554444",
                "email": "mariana.lima@design.co",
                "locale": "pt_BR",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_br,
                "ad": ad_br_1,
                "stage": SalesStage.QUALIFIED,
                "days_ago": 2,
                "messages": [
                    (MessageDirection.INBOUND, "Sou designer e preciso enviar gravações de protótipos Figma para devs."),
                    (MessageDirection.OUTBOUND, "Perfeito Mariana! Nossos usuários de design adoram porque os devs podem pausar e comentar com marcação temporal direta no link."),
                ],
            },
            {
                "name": "Gabriel Souza",
                "phone": "5521944443333",
                "email": "gabriel.souza@yahoo.com",
                "locale": "pt_BR",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_br,
                "ad": ad_br_2,
                "stage": SalesStage.ENGAGED,
                "days_ago": 2,
                "messages": [
                    (MessageDirection.INBOUND, "Quero saber mais sobre os planos empresariais."),
                    (MessageDirection.OUTBOUND, "Olá Gabriel! O plano Enterprise inclui SSO, armazenamento dedicado e permissões por equipe. Quantas licenças você estima?"),
                ],
            },
            {
                "name": "Larissa Alves",
                "phone": "5531933332222",
                "email": "larissa.alves@startup.br",
                "locale": "pt_BR",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_br,
                "ad": ad_br_2,
                "stage": SalesStage.CONTACTED,
                "days_ago": 1,
                "messages": [
                    (MessageDirection.OUTBOUND, "Olá Larissa! Vimos seu interesse pelo Boomshare. Como podemos te ajudar hoje?"),
                ],
            },
            {
                "name": "Thiago Ribeiro",
                "phone": "5511922221111",
                "email": "thiago.ribeiro@gmail.com",
                "locale": "pt_BR",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_br,
                "ad": ad_br_1,
                "stage": SalesStage.NEW,
                "days_ago": 1,
                "messages": [
                    (MessageDirection.INBOUND, "Quero testar"),
                ],
            },
            # --- US/Global Leads & Direct Customers ---
            {
                "name": "Emily Davis",
                "phone": "14155550101",
                "email": "emily.davis@acmecorp.com",
                "locale": "en_US",
                "source": "direct",
                "stage": SalesStage.ACTIVATED,
                "days_ago": 7,
                "downloaded": True,
                "activated": True,
                "messages": [
                    (MessageDirection.INBOUND, "Hello! Can Boomshare record in 4K on dual monitor setups?"),
                    (MessageDirection.OUTBOUND, "Hi Emily! Yes, you can choose any monitor or specific window to record in crisp 4K 60fps."),
                    (MessageDirection.INBOUND, "Awesome, could you provide the install link?"),
                    (MessageDirection.OUTBOUND, "Here is your setup link: https://boomshare.ai/download?ref=link_emily"),
                    (MessageDirection.INBOUND, "Installed and activated. Working like a charm!"),
                ],
            },
            {
                "name": "Michael Chang",
                "phone": "12125550102",
                "email": "mchang@cloudscale.net",
                "locale": "en_US",
                "source": "direct",
                "stage": SalesStage.DOWNLOADED,
                "days_ago": 5,
                "downloaded": True,
                "activated": False,
                "messages": [
                    (MessageDirection.INBOUND, "Hey, does the free tier have a watermark on exported videos?"),
                    (MessageDirection.OUTBOUND, "Hi Michael! No watermarks ever, even on our free tier."),
                    (MessageDirection.INBOUND, "Great, send me the download link please."),
                    (MessageDirection.OUTBOUND, "Here you go: https://boomshare.ai/download?ref=link_michael"),
                ],
            },
            {
                "name": "Sarah Jenkins",
                "phone": "13125550103",
                "email": "s.jenkins@creativepulse.io",
                "locale": "en_US",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_us,
                "ad": ad_us_1,
                "stage": SalesStage.OBJECTION_HANDLING,
                "days_ago": 3,
                "messages": [
                    (MessageDirection.INBOUND, "Is the pricing recurring or one-time license?"),
                    (MessageDirection.OUTBOUND, "We offer both! A flexible $9/month sub or a lifetime license at $149 with 2 years of updates."),
                    (MessageDirection.INBOUND, "I usually prefer one-time licenses because subs get expensive."),
                    (MessageDirection.OUTBOUND, "Completely understand! The lifetime license includes all current features and cloud sharing. Would you like a 14-day full pass to test?"),
                ],
            },
            {
                "name": "David Miller",
                "phone": "16505550104",
                "email": "dmiller@enterpriseflow.org",
                "locale": "en_US",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_us,
                "ad": ad_us_1,
                "stage": SalesStage.HUMAN_HANDOFF,
                "handling_mode": HandlingMode.HUMAN,
                "days_ago": 2,
                "messages": [
                    (MessageDirection.INBOUND, "We need 50 seats with custom vendor onboarding and SOC2 compliance docs."),
                    (MessageDirection.OUTBOUND, "We support custom procurement and have our SOC2 Type II report available. Let me connect you with our Enterprise solutions lead John."),
                    (MessageDirection.OUTBOUND, "Hi David, John from Boomshare Enterprise here. I'd be happy to share our SOC2 packet and setup a pilot."),
                ],
            },
            {
                "name": "Jessica Taylor",
                "phone": "12065550105",
                "email": "jtaylor@freelance.dev",
                "locale": "en_US",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_us,
                "ad": ad_us_1,
                "stage": SalesStage.NOT_INTERESTED,
                "days_ago": 4,
                "messages": [
                    (MessageDirection.INBOUND, "Is there an iOS app to record mobile screens?"),
                    (MessageDirection.OUTBOUND, "Currently Boomshare is built for Windows and macOS desktop. Mobile recording is on our Q4 roadmap."),
                    (MessageDirection.INBOUND, "Understood, not interested for now then. Thanks."),
                ],
            },
            # --- Spanish / Creator Outreach ---
            {
                "name": "Carlos Ramirez",
                "phone": "34600112233",
                "email": "carlos.ramirez@creadores.es",
                "locale": "es_ES",
                "source": LeadSource.CLICK_TO_WHATSAPP,
                "campaign": campaign_es,
                "ad": ad_es_1,
                "stage": SalesStage.ACTIVATED,
                "days_ago": 6,
                "downloaded": True,
                "activated": True,
                "messages": [
                    (MessageDirection.INBOUND, "Hola! Vi vuestro anuncio en Reels. Graba cámara web y pantalla a la vez?"),
                    (MessageDirection.OUTBOUND, "Hola Carlos! Sí, puedes colocar tu cámara en burbuja circular o rectangular en cualquier esquina en tiempo real."),
                    (MessageDirection.INBOUND, "Perfecto, me pasas el enlace?"),
                    (MessageDirection.OUTBOUND, "Aquí tienes tu enlace directo: https://boomshare.ai/download?ref=link_carlos"),
                    (MessageDirection.INBOUND, "Instalado y activado. Es rapidísimo!"),
                ],
            },
            {
                "name": "Sofia Morales",
                "phone": "34611223344",
                "email": "sofia.morales@marketing.madrid",
                "locale": "es_ES",
                "source": LeadSource.LEAD_AD,
                "campaign": campaign_es,
                "ad": ad_es_1,
                "stage": SalesStage.LINK_SENT,
                "days_ago": 3,
                "link_clicked": True,
                "messages": [
                    (MessageDirection.INBOUND, "Hola, podéis enviarme el instalador para Mac?"),
                    (MessageDirection.OUTBOUND, "Hola Sofia! Claro que sí, compatible con chips Apple Silicon (M1/M2/M3/M4): https://boomshare.ai/download?ref=link_sofia"),
                ],
            },
            {
                "name": "Alejandro Gomez",
                "phone": "34622334455",
                "email": "alejandro@agencia.es",
                "locale": "es_ES",
                "source": LeadSource.MANUAL,
                "stage": SalesStage.CLOSED,
                "days_ago": 7,
                "messages": [
                    (MessageDirection.OUTBOUND, "Hola Alejandro, te contactamos tras tu solicitud en la web."),
                ],
            },
            {
                "name": "Daniel Wilson",
                "phone": "14155559999",
                "email": "dwilson@privacy.org",
                "locale": "en_US",
                "source": "direct",
                "stage": SalesStage.CLOSED,
                "days_ago": 6,
                "opted_out": True,
                "messages": [
                    (MessageDirection.INBOUND, "STOP"),
                    (MessageDirection.OUTBOUND, "You have been unsubscribed from Boomshare notifications. Reply START anytime to re-enable."),
                ],
            },
        ]

        print(f"Creating {len(customers_data)} customers, leads, conversations and messages...")
        customers = []
        leads = []
        conversations = []
        messages = []
        links = []

        for index, data in enumerate(customers_data):
            created_at = now - timedelta(days=data["days_ago"], hours=2)
            downloaded_at = (
                created_at + timedelta(hours=3) if data.get("downloaded") else None
            )
            activated_at = (
                downloaded_at + timedelta(minutes=45) if data.get("activated") else None
            )
            opted_out_at = (
                created_at + timedelta(hours=1) if data.get("opted_out") else None
            )

            customer = Customer(
                id=uuid.uuid4(),
                phone=data["phone"],
                wa_id=data["phone"],
                full_name=data["name"],
                email=data["email"],
                locale=data["locale"],
                created_at=created_at,
                downloaded_at=downloaded_at,
                activated_at=activated_at,
                opted_out_at=opted_out_at,
            )
            customers.append(customer)

            lead = None
            if data["source"] != "direct":
                lead = Lead(
                    id=uuid.uuid4(),
                    customer_id=customer.id,
                    source=data["source"],
                    status=LeadStatus.CONVERTED if data.get("downloaded") else LeadStatus.CONTACTED,
                    campaign_id=data["campaign"].id if "campaign" in data else None,
                    ad_id=data["ad"].id if "ad" in data else None,
                    meta_leadgen_id=f"leadgen_{secrets.token_hex(8)}" if data["source"] == LeadSource.LEAD_AD else None,
                    created_at=created_at,
                )
                leads.append(lead)

            thread_status = (
                ConversationStatus.CLOSED
                if data["stage"] in (SalesStage.CLOSED, SalesStage.ACTIVATED, SalesStage.NOT_INTERESTED)
                else ConversationStatus.OPEN
            )
            handling_mode = data.get("handling_mode", HandlingMode.AI)

            conversation = Conversation(
                id=uuid.uuid4(),
                customer_id=customer.id,
                lead_id=lead.id if lead else None,
                channel="whatsapp",
                # Alternate, so both numbers are represented.
                phone_number_id=numbers[index % len(numbers)],
                status=thread_status,
                handling_mode=handling_mode,
                sales_stage=data["stage"],
                stage_updated_at=created_at + timedelta(hours=1),
                last_inbound_at=created_at + timedelta(minutes=20),
                last_outbound_at=created_at + timedelta(minutes=25),
                created_at=created_at,
            )
            conversations.append(conversation)

            # Messages
            msg_time = created_at
            for idx, (direction, content) in enumerate(data.get("messages", [])):
                msg_time = msg_time + timedelta(minutes=5 * (idx + 1))
                is_ai = direction == MessageDirection.OUTBOUND and handling_mode != HandlingMode.HUMAN
                msg = Message(
                    id=uuid.uuid4(),
                    conversation_id=conversation.id,
                    direction=direction,
                    message_type=MessageType.TEXT,
                    status=MessageStatus.READ if direction == MessageDirection.OUTBOUND else MessageStatus.RECEIVED,
                    provider_message_id=f"wamid.HBgM{secrets.token_hex(10)}",
                    content=content,
                    ai_generated=is_ai,
                    sent_by="AI Sales Assistant" if is_ai else ("John Operator" if direction == MessageDirection.OUTBOUND else None),
                    created_at=msg_time,
                    sent_at=msg_time,
                    delivered_at=msg_time + timedelta(seconds=2),
                    read_at=msg_time + timedelta(seconds=15),
                )
                messages.append(msg)

            # Download links
            if data.get("downloaded") or data.get("link_clicked") or data["stage"] in (SalesStage.LINK_SENT, SalesStage.DOWNLOADED, SalesStage.ACTIVATED):
                token = f"dl_{secrets.token_urlsafe(12)}"
                link = DownloadLink(
                    id=uuid.uuid4(),
                    token=token,
                    customer_id=customer.id,
                    conversation_id=conversation.id,
                    url=f"https://boomshare.ai/download?ref={token}",
                    platform="windows" if "silva" in data["name"].lower() or "us" in data["locale"] else "macos",
                    sent_at=created_at + timedelta(minutes=15),
                    clicked_at=created_at + timedelta(minutes=18) if (data.get("link_clicked") or data.get("downloaded")) else None,
                    downloaded_at=downloaded_at,
                    activated_at=activated_at,
                )
                links.append(link)

        print(f"Flushing {len(customers)} customers...")
        session.add_all(customers)
        await session.flush()

        print(f"Flushing {len(leads)} leads...")
        session.add_all(leads)
        await session.flush()

        print(f"Flushing {len(conversations)} conversations...")
        session.add_all(conversations)
        await session.flush()

        print(f"Flushing {len(messages)} messages and {len(links)} download links...")
        session.add_all(messages)
        session.add_all(links)
        await session.flush()

        print("Committing all seed records to database...")

    print("Successfully seeded dashboard test data!")


if __name__ == "__main__":
    asyncio.run(seed())
