"""What this turn has to accomplish.

`sales_agent.md` describes *how* to sell. This module decides *what to do now*,
and the answer is handed to the model as a single instruction for the turn.

The ladder is built for the channel it actually runs in: someone tapped an ad on
Instagram and landed in WhatsApp knowing almost nothing. That person will not
answer "what is your role and what team are you on" - they will leave. So the
order is **value first, questions later, and never a dead end**:

  1. say what it is and what it saves them
  2. connect one concrete outcome to whatever they said back
  3. offer the download - it is free, so the ask is tiny - and hand it over the
     moment they accept. Offering and delivering are two turns on purpose: a
     message that asks "shall I send it?" and carries the link anyway answers
     its own question and reads like a bot. The split is enforced in
     `guardrails.resolve_link_offer`, not left to the model to remember.
  4. handle the blocker, bridge to a specific later moment

`next_objective` covers turns the customer started. `follow_up_objective`
covers the ones they did not: an unanswered check-in has a different job from a
reply, and each rung of it has to bring something the rung before it did not.

Position in that sequence is measured by how many replies we have sent, not by
how many facts we have collected. Gating on collected facts is what produced an
interrogation: role, then team, then challenges, and a customer who answered
"nothing" and was shown the door.

Pure functions only: state in, a sentence out. Nothing here touches the database
or the model, which is what makes the sales ladder cheap to test and to change.
"""

from __future__ import annotations

from app.domain import CustomerPlatform, SalesStage, read_platform

#: Facts worth remembering when the customer volunteers them. They personalise
#: the pitch; they are never prerequisites for making it. `platform` decides
#: which installer the link points at, so it is asked alongside the download -
#: never as a condition of getting it.
QUALIFICATION_SLOTS: tuple[str, ...] = ("use_case", "role", "platform")

#: Stages where there is nothing left to advance towards, so no objective is
#: issued and the prompt's ordinary behaviour applies.
_NO_OBJECTIVE_STAGES = frozenset(
    {
        SalesStage.NOT_INTERESTED,
        SalesStage.HUMAN_HANDOFF,
        SalesStage.DOWNLOADED,
        SalesStage.ACTIVATED,
        SalesStage.CLOSED,
    }
)

_OPENING = (
    "They have just arrived, almost certainly from an ad, and know next to nothing "
    "about Boomshare. Do NOT ask what they do, what team they are on, or what "
    "problems they have - nobody answers that on WhatsApp and it reads like a form. "
    "Greet them like a person, say in one plain sentence what Boomshare is and the "
    "one thing it saves them - recording a quick video instead of typing a long "
    "explanation or booking a call - and mention it is free to start. Then ask one "
    "easy question about what they would use it for. Under 40 words."
)

_CONNECT = (
    "They have engaged. Take whatever they just told you and connect it to one "
    "concrete outcome Boomshare gives them - the result, not the feature list. If "
    "they gave you nothing to work with, do not dig: offer the most common use "
    "instead, like sending a two-minute walkthrough rather than writing three "
    "paragraphs. Then offer to send the download so they can try it themselves - "
    "ask whether they want it and request the offer_download_link action. It is "
    "free, so the ask is small - make it."
)

_NUDGE = (
    "They are still browsing and have not committed. Do not interrogate them, and "
    "never end on a dead end - a reply with no next step is exactly where people "
    "leave the conversation. Give them one more concrete reason it is worth two "
    "minutes, and offer the link directly by requesting the offer_download_link "
    "action. If they say they have no need for it, do not argue and do not give "
    "up: name a benefit they have not thought of, and leave the offer open."
)

_SEND_LINK = (
    "They want it. Hand the download over now by requesting the send_download_link "
    "action - do not make them ask twice and do not qualify them first. Word the "
    "reply as handing it over, not as a question: the backend attaches the real "
    "link to this very message, so 'would you like me to send it?' would arrive "
    "with the link already underneath it. Tell them what happens next: the "
    "installer takes about a minute, then they record from the toolbar icon. Do "
    "not write the link yourself - the backend attaches it."
)

_SEND_LINK_UNKNOWN_PLATFORM = (
    _SEND_LINK
    + " You do not know yet whether they are on Windows or a Mac, so ask that in "
    "the same message - the link goes with it either way. Never hold the download "
    "back for an answer."
)

_UNSHIPPABLE_PLATFORM = (
    "They have told you they are on something Boomshare does not ship for - a "
    "phone, a tablet, or Linux. The download is not going out this turn and the "
    "backend will not attach it, so do not offer it or imply it is coming: a "
    "message that says 'there is no mobile app' and hands over an installer "
    "anyway reads as not having listened. Do three things, in one short message. "
    "First, say plainly what it does run on - Windows and Mac today, with Linux "
    "in development and no date you can promise, and no mobile app. Second, ask "
    "whether they have a Windows or Mac machine they could use. Third, if they "
    "name a moment when they will be at one - even 'in five minutes' - request "
    "the schedule_follow_up action with follow_up_minutes set to exactly that, "
    "set follow_up_reason to what you are checking back about, and tell them you "
    "will check back then. "
    "The one exception is this very message: if they have just told you they are "
    "now at a Windows or Mac machine, that changes everything - record it in "
    "customer_notes as platform and hand the download over as normal. Recording "
    "the platform is what releases it."
)

_POST_LINK = (
    "The link is already with them. Your only job now is getting them to a first "
    "recording. Do not pitch again, do not re-offer the link, and do not restate "
    "the features. Ask what is holding them back, or help with the step they are "
    "stuck on."
)

_OBJECTION = (
    "They have raised a real blocker. Do not argue with it and do not soften the "
    "facts. Acknowledge it plainly, then pivot to the benefit they would still get, "
    "and bridge to a specific later moment - request the schedule_follow_up action "
    "with follow_up_minutes set to the time they actually named, so the "
    "conversation is picked back up exactly when they expect it."
)


#: What each unanswered check-in has to achieve, in order. The customer has not
#: replied, so repeating the last message is the one thing guaranteed not to
#: work: each rung has to bring something the one before it did not. The ladder
#: ends - `reminders.FOLLOW_UP_LADDER_HOURS` decides how many rungs there are,
#: and the last one says goodbye rather than trailing off into silence.
_FOLLOW_UPS: tuple[str, ...] = (
    (
        "They have not replied since your last message. Send one short, "
        "low-pressure check-in. Do not repeat what you already said, do not "
        "apologise for following up, and do not open with 'just checking in'. "
        "One line, one easy question they can answer in three words."
    ),
    (
        "This is the second time they have not replied, so saying the same thing "
        "again will not work. Bring something new: one concrete use of Boomshare "
        "they have not heard from you yet, in a single sentence. Keep it lighter "
        "than a pitch."
    ),
    (
        "This is the last time you will message them unprompted, so say so "
        "plainly and warmly - no guilt, no pressure, no final pitch. Make it easy "
        "to pick the conversation back up whenever they want, and leave it there."
    ),
)

#: Replaces the middle rung once the link is already with them: there is nothing
#: left to sell, only an install to unblock.
_FOLLOW_UP_AFTER_LINK = (
    "The download link is already with them and they have not replied. Do not "
    "pitch, do not re-send the link and do not list features. Ask one specific "
    "question about what stopped them installing it, or offer help with the step "
    "they are most likely stuck on."
)

#: The promised callback. This is the moment the customer was told to expect,
#: so it outranks every rung of the ladder. The failure it replaces was a
#: promise delivered back verbatim four minutes later - "I'll check back with
#: you in 4 minutes" arriving *as* the check-back - which reads like a machine
#: that has lost its place.
_KEPT_PROMISE = (
    "This is the moment you promised to come back, and here is what you said you "
    'were checking back about: "{promise}". Open on that and nothing else. Do not '
    "repeat the message where you made the promise, do not re-explain Boomshare, "
    "and do not apologise for messaging. Ask the one question that moves it "
    "forward now - if they were fetching a laptop, ask whether they are at it and "
    "offer to walk them through the first recording. One or two sentences."
)

#: Replaces the first rung when they never got the link: the useful follow-up is
#: the offer they have not had yet, not another nudge about nothing.
_FOLLOW_UP_OFFER_LINK = (
    "They have not replied, and the download has never been offered to them. "
    "Give them one concrete reason it is worth two minutes and offer to send it, "
    "requesting the offer_download_link action. Keep it to two sentences."
)


def follow_up_objective(
    attempt: int,
    *,
    download_link_sent: bool = False,
    link_offered: bool = False,
    promised: str | None = None,
) -> str:
    """What the `attempt`-th unanswered check-in has to accomplish.

    `attempt` counts from 1. Beyond the last rung the final one is reused, but
    `reminders.follow_up_delay` stops scheduling before that happens - the cap
    lives with the scheduling, not with the wording.

    `promised` is what the model said it was checking back about when it asked
    for this follow-up. When there is one it outranks everything else: a
    callback the customer is expecting has a specific job, and the generic
    rungs are for silence nobody agreed to.
    """
    if promised:
        return _KEPT_PROMISE.format(promise=promised.strip())
    if download_link_sent:
        return _FOLLOW_UP_AFTER_LINK
    if attempt <= 1 and not link_offered:
        return _FOLLOW_UP_OFFER_LINK
    return _FOLLOW_UPS[min(max(attempt, 1), len(_FOLLOW_UPS)) - 1]


def missing_slots(notes: dict[str, str] | None) -> list[str]:
    """Facts we have not learned yet, in the order they become useful.

    `platform` counts as known only when the model has placed them on a build we
    actually ship. "They are on a phone" answers the question without making
    them installable, and treating that as known would pick an installer for
    someone who has nothing to install.
    """
    known = notes or {}
    missing = [slot for slot in QUALIFICATION_SLOTS if not str(known.get(slot) or "").strip()]
    if "platform" not in missing and not read_platform(known.get("platform")).has_installer:
        missing.append("platform")
    return missing


def next_objective(
    stage: SalesStage,
    notes: dict[str, str] | None,
    *,
    download_link_sent: bool = False,
    replies_sent: int = 0,
) -> str | None:
    """The one thing this turn should achieve, or None to leave the model alone.

    `replies_sent` is how many messages we have already sent in this
    conversation. It is what places us on the ladder: nobody is ready to be sold
    to on the first reply, and nobody wants to be qualified on the fourth.
    """
    if stage in _NO_OBJECTIVE_STAGES:
        return None

    if download_link_sent or stage == SalesStage.LINK_SENT:
        return _POST_LINK

    # The machine they are on outranks where they are in the funnel. It used to
    # be checked inside the `download_suggested` branch, which meant a customer
    # who said "yes, on mobile" was judged by the stage they were in *before*
    # they said it - so the turn that needed this objective most never got it,
    # and the one after it was too late.
    if read_platform((notes or {}).get("platform")) is CustomerPlatform.OTHER:
        return _UNSHIPPABLE_PLATFORM

    if stage == SalesStage.OBJECTION_HANDLING:
        return _OBJECTION

    if stage == SalesStage.DOWNLOAD_SUGGESTED:
        # They have signalled they want it, so the link goes out this turn -
        # not knowing which build changes what else the message says, never
        # whether the download is in it.
        if "platform" not in missing_slots(notes):
            return _SEND_LINK
        return _SEND_LINK_UNKNOWN_PLATFORM

    if replies_sent == 0:
        return _OPENING
    if replies_sent == 1:
        return _CONNECT
    return _NUDGE
